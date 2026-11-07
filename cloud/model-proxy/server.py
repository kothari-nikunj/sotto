"""Two fixed model surfaces, tenant authentication, and durable usage reservations.

Never log prompts, credentials, URLs with keys, images or upstream response bodies.
The pilot charges a conservative $2 allowance per attempted upstream call. This is
an admission budget, not an invoice; provider billing remains authoritative. Holding
the allowance across crashes/ambiguous failures avoids freeing spend already used.
Successful proven 3.8 text chat and native calls settle against complete provider
receipts, including thinking tokens. Grounded, multimodal, ambiguous or incomplete
responses retain their allowance. The older one-time chat ceiling migration is unchanged.
"""
import calendar
import hashlib
import hmac
import http.client
import io
import json
import math
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from usage_accounting import (normalize, normalize_google_openai, google_openai_receipt,
                              estimate, estimate_range, PRICING_VERSION, PRICES, INTRO_END)

MODELS = {'gemini-3.8-flash', 'gemini-3-flash-preview', 'gemini-3.5-flash-lite'}
MAX_BODY = 8 * 1024 * 1024
BRIEF_UPSTREAM_WAIT_SECONDS = 120
BRIEF_WORKLOADS = frozenset({'brief_extract', 'brief_critic', 'brief_revise'})
MAX_OUTPUT = 65536
CALL_ALLOWANCE_CENTS = 200
LEASE_SECONDS = 30 * 86400
RATE_WINDOW_SECONDS = 60
REQUESTS_PER_WINDOW = 60
# Keep room for due briefs and owner chat while routine ingestion catches up.
BACKGROUND_REQUESTS_PER_WINDOW = 48
MAX_ACTIVE_CALLS = 8
MAX_ACTIVE_BACKGROUND_CALLS = 5
ACTIVE_LEASE_SECONDS = 360  # longer than the upstream request ceiling
INPUT_CHARS_PER_WINDOW = 12_000_000
BACKGROUND_INPUT_CHARS_PER_WINDOW = 6_000_000
# One tenant may use at most three quarters of a shared request or concurrency limit, so another
# tenant's chat and due brief always keep room on the single provider key.
TENANT_SHARE_PERCENT = 75
FOREGROUND_WORKLOADS = frozenset({'brief', 'brief_extract', 'brief_critic',
                                  'brief_revise', 'chat', 'meeting_prep'})
# Verified 2026-09-06: https://ai.google.dev/gemini-api/docs/latest-model?hl=en
# Introductory standard API prices through 2026-12-31; no future-price assumption.
PRICING_START = 1788307200  # 2026-09-02 UTC
PRICING_END = 1798761600    # 2027-01-01 UTC
SETTLEMENT_MIGRATION = 'settled_chat_allowance_v1'
MAX_TOOL_RESULT_CATEGORIES = 32
NATIVE_USAGE_KEYS = frozenset({
    'promptTokenCount', 'candidatesTokenCount', 'totalTokenCount',
    'thoughtsTokenCount', 'cachedContentTokenCount', 'toolUsePromptTokenCount',
    'promptTokensDetails', 'cacheTokensDetails', 'candidatesTokensDetails',
    'serviceTier'})


def tool_result_sizes(payload):
    """Content-free tool-result sizes, attributed only through declared OpenAI tool identities."""
    tools = payload.get('tools')
    tools = tools if isinstance(tools, list) else []
    messages = payload.get('messages')
    messages = messages if isinstance(messages, list) else []
    declared = []
    for tool in tools:
        function = tool.get('function') if isinstance(tool, dict) else None
        name = function.get('name') if isinstance(function, dict) else None
        if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
            declared.append(name)
    allowed = set(declared[:MAX_TOOL_RESULT_CATEGORIES])
    by_call = {}
    for message in messages:
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        calls = message.get('tool_calls')
        calls = calls if isinstance(calls, list) else []
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = call.get('id')
            function = call.get('function')
            name = function.get('name') if isinstance(function, dict) else None
            if isinstance(call_id, str) and isinstance(name, str) and name in allowed:
                by_call[call_id] = name
    result = {}
    for message in messages:
        if not isinstance(message, dict) or message.get('role') != 'tool':
            continue
        call_id = message.get('tool_call_id')
        name = by_call.get(call_id, 'unknown') if isinstance(call_id, str) else 'unknown'
        size = len(json.dumps(message.get('content')))
        bucket = result.setdefault(name, {'count': 0, 'chars': 0, 'max_chars': 0})
        bucket['count'] += 1
        bucket['chars'] += size
        bucket['max_chars'] = max(bucket['max_chars'], size)
    return dict(sorted(result.items()))


def settled_chat_allowance(route, model, created, status, input_tokens):
    return (settled_text_allowance(model, created, status, input_tokens)
            if route == 'chat' else CALL_ALLOWANCE_CENTS)


def settled_text_allowance(model, created, status, input_tokens):
    if (model == 'gemini-3.8-flash' and status == 200
            and PRICING_START <= created < PRICING_END
            and isinstance(input_tokens, int) and not isinstance(input_tokens, bool)
            and 0 <= input_tokens <= 1048576):
        return min(CALL_ALLOWANCE_CENTS, math.ceil((input_tokens * 75 + MAX_OUTPUT * 375) / 1_000_000))
    return CALL_ALLOWANCE_CENTS


def chat_receipt_allowance(model, created, status, usage):
    """Settle only a complete Google text-chat receipt, including hidden thinking."""
    receipt = google_openai_receipt(usage, MAX_OUTPUT)
    if (status != 200 or model != 'gemini-3.8-flash'
            or not PRICING_START <= created < min(PRICING_END, INTRO_END)
            or receipt is None):
        return None
    prompt, cached = receipt['prompt'], receipt['cached'] or 0
    output = receipt['total'] - prompt
    input_rate, cached_rate, output_rate = PRICES[model]
    return min(CALL_ALLOWANCE_CENTS, math.ceil(100 * (
        (prompt - cached) * input_rate + cached * cached_rate + output * output_rate) / 1_000_000))


def native_usage_features_safe(usage):
    """Reject usage features that could add an unpriced modality or service tier."""
    if (not isinstance(usage, dict) or not set(usage) <= NATIVE_USAGE_KEYS
            or usage.get('serviceTier', 'standard') not in ('standard', 'unspecified')
            or type(usage.get('toolUsePromptTokenCount', 0)) is not int
            or usage.get('toolUsePromptTokenCount', 0) != 0):
        return False
    for name in ('promptTokensDetails', 'cacheTokensDetails', 'candidatesTokensDetails'):
        if name in usage and (not isinstance(usage[name], list) or any(
                not isinstance(item, dict) or set(item) != {'modality', 'tokenCount'}
                or item['modality'] != 'TEXT' or type(item['tokenCount']) is not int
                or item['tokenCount'] < 0 for item in usage[name])):
            return False
    return True


def native_receipt_allowance(model, created, status, usage):
    """Price only a complete native text receipt under the known standard tariff."""
    if (status != 200 or model not in MODELS or model not in PRICES
            or not PRICING_START <= created < min(PRICING_END, INTRO_END)
            or not native_usage_features_safe(usage)):
        return None
    prompt = usage.get('promptTokenCount')
    candidates = usage.get('candidatesTokenCount')
    total = usage.get('totalTokenCount')
    if (type(prompt) is not int or not 0 <= prompt <= 1048576
            or type(candidates) is not int or not 0 <= candidates <= MAX_OUTPUT
            or type(total) is not int):
        return None
    output = total - prompt
    if not candidates <= output <= MAX_OUTPUT:
        return None
    if 'thoughtsTokenCount' in usage:
        thoughts = usage['thoughtsTokenCount']
        if type(thoughts) is not int or thoughts < 0 or total != prompt + candidates + thoughts:
            return None
    if 'cachedContentTokenCount' in usage:
        cached = usage['cachedContentTokenCount']
        if type(cached) is not int or not 0 <= cached <= prompt:
            return None
    input_rate, _, output_rate = PRICES[model]
    return min(CALL_ALLOWANCE_CENTS, math.ceil(
        100 * (prompt * input_rate + output * output_rate) / 1_000_000))


def native_sparse_usage_safe(usage):
    """The legacy ceiling policy applies only when no total receipt was supplied."""
    if not native_usage_features_safe(usage) or 'totalTokenCount' in usage:
        return False
    prompt = usage.get('promptTokenCount')
    candidates = usage.get('candidatesTokenCount')
    if (type(prompt) is not int or not 0 <= prompt <= 1048576
            or type(candidates) is not int or not 0 <= candidates <= MAX_OUTPUT):
        return False
    thoughts = usage.get('thoughtsTokenCount', 0)
    if type(thoughts) is not int or thoughts < 0 or candidates + thoughts > MAX_OUTPUT:
        return False
    if 'cachedContentTokenCount' in usage:
        cached = usage['cachedContentTokenCount']
        if type(cached) is not int or not 0 <= cached <= prompt:
            return False
    return True


def _text_parts(value):
    return (isinstance(value, dict) and set(value) <= {'parts', 'role'}
            and isinstance(value.get('parts'), list) and bool(value['parts'])
            and all(isinstance(part, dict) and set(part) == {'text'}
                    and isinstance(part['text'], str) for part in value['parts']))


def _text_response_parts(value):
    # Thinking models can annotate otherwise text-only output with an opaque
    # signature or a thought marker. Keep request proof and media exclusion strict.
    return (isinstance(value, dict) and set(value) <= {'parts', 'role'}
            and isinstance(value.get('parts'), list) and bool(value['parts'])
            and all(isinstance(part, dict) and 'text' in part
                    and set(part) <= {'text', 'thoughtSignature', 'thought'}
                    and isinstance(part['text'], str)
                    and ('thoughtSignature' not in part
                         or isinstance(part['thoughtSignature'], str))
                    and ('thought' not in part or type(part['thought']) is bool)
                    for part in value['parts']))


def native_text_request(payload):
    """Only the ordinary Gemini text/JSON shape; unknown billable features keep $2."""
    if not isinstance(payload, dict) or not set(payload) <= {
            'contents', 'systemInstruction', 'generationConfig'}:
        return False
    contents = payload.get('contents')
    if not isinstance(contents, list) or not contents or not all(_text_parts(c) for c in contents):
        return False
    if 'systemInstruction' in payload and not _text_parts(payload['systemInstruction']):
        return False
    config = payload.get('generationConfig', {})
    return (isinstance(config, dict) and set(config) <= {
        'candidateCount', 'maxOutputTokens', 'temperature',
        'thinkingConfig', 'responseSchema', 'response_mime_type', 'responseMimeType'}
        and type(config.get('candidateCount', 1)) is int and config.get('candidateCount', 1) == 1
        and config.get('response_mime_type', 'application/json') in ('text/plain', 'application/json')
        and config.get('responseMimeType', 'application/json') in ('text/plain', 'application/json')
        and ('thinkingConfig' not in config or (
            isinstance(config['thinkingConfig'], dict)
            and set(config['thinkingConfig']) <= {'thinkingLevel', 'includeThoughts'})))


def chat_function_call_supported(call):
    """Permit only ordinary function calls and Google's opaque continuation signature."""
    if (not isinstance(call, dict) or call.get('type') != 'function'
            or not set(call) <= {'id', 'type', 'function', 'extra_content'}):
        return False
    if 'extra_content' not in call:
        return True
    return chat_google_signature_supported(call['extra_content'])


def chat_google_signature_supported(extra):
    """Accept only Google's opaque continuation metadata, without retaining its value."""
    if not isinstance(extra, dict) or set(extra) != {'google'}:
        return False
    google = extra['google']
    return (isinstance(google, dict) and set(google) == {'thought_signature'}
            and isinstance(google['thought_signature'], str) and bool(google['thought_signature']))


def chat_text_request(payload):
    """Exclude unknown paid features before permitting receipt-based chat settlement."""
    if not isinstance(payload, dict) or not set(payload) <= {
            'model', 'messages', 'n', 'max_tokens', 'max_completion_tokens', 'temperature',
            'top_p', 'frequency_penalty', 'presence_penalty', 'stop', 'seed',
            'response_format', 'tools', 'tool_choice', 'parallel_tool_calls',
            'reasoning_effort', 'stream', 'service_tier', 'modalities'}:
        return False
    if payload.get('stream', False) is not False:
        return False
    if payload.get('reasoning_effort', 'low') not in ('none', 'minimal', 'low', 'medium', 'high'):
        return False
    response_format = payload.get('response_format')
    if response_format is not None and (not isinstance(response_format, dict)
            or response_format.get('type') not in ('text', 'json_object', 'json_schema')
            or not set(response_format) <= {'type', 'json_schema'}):
        return False
    tools = payload.get('tools', [])
    if (not isinstance(tools, list) or any(
            not isinstance(tool, dict) or set(tool) != {'type', 'function'}
            or tool['type'] != 'function' or not isinstance(tool['function'], dict)
            or not set(tool['function']) <= {'name', 'description', 'parameters', 'strict'}
            for tool in tools)):
        return False
    messages = payload.get('messages')
    if not isinstance(messages, list) or not messages:
        return False
    for message in messages:
        if (not isinstance(message, dict) or not set(message) <= {
                'role', 'content', 'name', 'tool_call_id', 'tool_calls', 'extra_content'}
                or message.get('role') not in ('system', 'developer', 'user', 'assistant', 'tool')):
            return False
        if 'extra_content' in message and (message.get('role') != 'assistant'
                or not chat_google_signature_supported(message['extra_content'])):
            return False
        content = message.get('content')
        if not (isinstance(content, str) or content is None and message.get('role') == 'assistant'
                or isinstance(content, list) and all(
                    isinstance(part, dict) and set(part) == {'type', 'text'}
                    and part['type'] == 'text' and isinstance(part['text'], str) for part in content)):
            return False
        if 'tool_calls' in message and (not isinstance(message['tool_calls'], list)
                or any(not chat_function_call_supported(call) for call in message['tool_calls'])):
            return False
    return True


def chat_text_response(item):
    """Only a complete, single ordinary chat choice can support text settlement."""
    if not isinstance(item, dict) or not set(item) <= {
            'id', 'object', 'created', 'model', 'choices', 'usage', 'system_fingerprint', 'service_tier'}:
        return False
    if item.get('service_tier', 'default') not in ('default', 'standard'):
        return False
    choices = item.get('choices')
    if not isinstance(choices, list) or len(choices) != 1:
        return False
    choice = choices[0]
    if (not isinstance(choice, dict) or not set(choice) <= {'index', 'message', 'finish_reason', 'logprobs'}
            or choice.get('finish_reason') not in ('stop', 'length', 'tool_calls')):
        return False
    message = choice.get('message')
    return (isinstance(message, dict) and set(message) <= {'role', 'content', 'tool_calls', 'refusal', 'extra_content'}
            and message.get('role') == 'assistant'
            and ('extra_content' not in message
                 or chat_google_signature_supported(message['extra_content']))
            and (isinstance(message.get('content'), str) or message.get('content') is None)
            and ('tool_calls' not in message or (isinstance(message['tool_calls'], list)
                 and all(chat_function_call_supported(call) for call in message['tool_calls']))))


def native_text_response(item):
    """A completed nonstreamed reply must prove native usage and no paid tools/media."""
    if not isinstance(item, dict) or not set(item) <= {
            'candidates', 'usageMetadata', 'modelVersion', 'responseId', 'createTime'}:
        return False
    usage = item.get('usageMetadata')
    if not native_usage_features_safe(usage):
        return False
    prompt = usage.get('promptTokenCount')
    if type(prompt) is not int or not 0 <= prompt <= 1048576:
        return False
    candidates = item.get('candidates')
    return (isinstance(candidates, list) and len(candidates) == 1
            and isinstance(candidates[0], dict)
            and set(candidates[0]) <= {
                'content', 'finishReason', 'finishMessage', 'index', 'safetyRatings',
                'avgLogprobs', 'logprobsResult', 'tokenCount'}
            # MAX_TOKENS is a completed model response; the full output ceiling
            # already covers it. An interrupted transport never parses here.
            and candidates[0].get('finishReason') in ('STOP', 'MAX_TOKENS')
            and _text_response_parts(candidates[0].get('content')))



def request_metadata(headers, payload, lane, tenant=None):
    """Fixed labels and sizes only; source text and client secrets never enter the ledger."""
    result = {}
    for name, header, pattern in (
        ('workload', 'X-Sotto-Workload', r'[a-z_]{1,40}'),
        ('operation_id', 'X-Sotto-Operation', r'[a-f0-9]{64}'),
        ('parent_operation_id', 'X-Sotto-Parent-Operation', r'[a-f0-9]{64}'),
        ('run_id', 'X-Sotto-Run', r'[a-f0-9]{32}'),
        ('attempt', 'X-Sotto-Attempt', r'[1-9][0-9]{0,3}'),
        ('session_id', 'X-Sotto-Session', r'[A-Za-z0-9._-]{1,96}'),
        ('job_id', 'X-Sotto-Job', r'[A-Za-z0-9._-]{1,96}'),
    ):
        value = headers.get(header, '')
        if re.fullmatch(pattern, value):
            result[name] = value
    result.setdefault('workload', 'chat' if lane == 'chat' else 'unknown')
    tenant = tenant or {}
    result['application'] = tenant.get('application', 'sotto')
    result['deployment'] = tenant.get('deployment', 'unknown')
    result['proxy_deployment'] = os.environ.get('RAILWAY_DEPLOYMENT_ID', 'unknown')
    if lane == 'chat':
        raw_messages = payload.get('messages')
        messages = [message for message in (raw_messages if isinstance(raw_messages, list) else [])
                    if isinstance(message, dict)]
        result['context_chars'] = {
            'instructions': sum(len(json.dumps(m.get('content'))) for m in messages if m.get('role') in ('system', 'developer')),
            'tool_results': sum(len(json.dumps(m.get('content'))) for m in messages if m.get('role') == 'tool'),
            'conversation': sum(len(json.dumps(m.get('content'))) for m in messages if m.get('role') not in ('system', 'developer', 'tool')),
            'tools': len(json.dumps(payload.get('tools') or []))}
        result['tool_result_sizes'] = tool_result_sizes(payload)
    else:
        result['context_chars'] = {'instructions': len(json.dumps(payload.get('systemInstruction') or {})),
                                   'evidence': len(json.dumps(payload.get('contents') or [])),
                                   'schema': len(json.dumps((payload.get('generationConfig') or {}).get('responseSchema') or {}))}
    # Admission must include tool-call arguments, native tool definitions, and any future
    # request fields as well as the diagnostic component breakdown. Over-counting a small
    # amount of envelope/configuration is safer than leaving unmeasured model input.
    result['input_chars'] = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
    return result


class RateLimitError(RuntimeError):
    def __init__(self, reason, retry_after=60):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class AuthenticationError(RuntimeError):
    pass


class RequestTooLargeError(ValueError):
    """No ledger state could ever admit this request; waiting cannot help."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, route TEXT, '
                       'model TEXT, created REAL, allowance INTEGER NOT NULL, status INTEGER, input_tokens INTEGER, output_tokens INTEGER)')
            columns = {row[1] for row in db.execute('PRAGMA table_info(calls)')}
            for column in ('usage_json', 'metadata_json', 'active_until'):
                if column not in columns:
                    db.execute(f'ALTER TABLE calls ADD COLUMN {column} {"REAL" if column == "active_until" else "TEXT"}')
            db.execute('CREATE INDEX IF NOT EXISTS calls_tenant_created ON calls(tenant,created)')
            db.execute('CREATE INDEX IF NOT EXISTS calls_created ON calls(created)')
            db.execute('CREATE INDEX IF NOT EXISTS calls_active_until ON calls(active_until) '
                       'WHERE active_until IS NOT NULL')
            db.execute('CREATE TABLE IF NOT EXISTS model_leases (tenant TEXT NOT NULL, token_hash TEXT NOT NULL, '
                       'expires REAL NOT NULL, PRIMARY KEY(tenant,token_hash))')
            db.execute('CREATE TABLE IF NOT EXISTS proxy_migrations (name TEXT PRIMARY KEY, applied REAL NOT NULL)')
            # Backfill only receipted chat attempts, idempotently. Failed, interrupted,
            # unknown-usage and legacy native calls keep the original reservation. Record the
            # migration so a growing metering ledger is not scanned on every process restart.
            applied = db.execute('SELECT 1 FROM proxy_migrations WHERE name=?',
                                 (SETTLEMENT_MIGRATION,)).fetchone()
            if not applied:
                for row in db.execute('SELECT id,route,model,created,status,input_tokens FROM calls').fetchall():
                    allowance = settled_chat_allowance(*row[1:])
                    db.execute('UPDATE calls SET allowance=MIN(allowance,?) WHERE id=?', (allowance, row[0]))
                db.execute('INSERT INTO proxy_migrations(name,applied) VALUES(?,?)',
                           (SETTLEMENT_MIGRATION, time.time()))

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    @staticmethod
    def _expiry(db, tenant):
        row = db.execute('SELECT expires FROM model_leases WHERE tenant=? AND token_hash=?',
                         (tenant['id'], tenant['token_sha256'])).fetchone()
        return max(float(tenant.get('expires_at', 0)), float(row[0]) if row else 0)

    def expiry(self, tenant):
        with self.connect() as db:
            return self._expiry(db, tenant)

    def renew(self, tenant):
        # The independent workload credential authorizes renewal, never the model bearer.
        # Binding the lease to the configured hash makes an operator rotation revoke the old
        # bearer immediately. Disabled tenants fail before reaching this transaction.
        expires = time.time() + LEASE_SECONDS
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO model_leases VALUES(?,?,?) ON CONFLICT(tenant,token_hash) '
                       'DO UPDATE SET expires=MAX(expires,excluded.expires)',
                       (tenant['id'], tenant['token_sha256'], expires))
        return self.expiry(tenant)

    @staticmethod
    def _budget_used(db, tenant, period, started_at, now):
        if period == 'calendar_month':
            utc = time.gmtime(now)
            month_start = calendar.timegm((utc.tm_year, utc.tm_mon, 1, 0, 0, 0))
            next_month = calendar.timegm((utc.tm_year + (utc.tm_mon == 12),
                                          utc.tm_mon % 12 + 1, 1, 0, 0, 0))
            return db.execute('SELECT COALESCE(SUM(allowance),0) FROM calls '
                              'WHERE tenant=? AND created>=? AND created<?',
                              (tenant, max(started_at, month_start), next_month)).fetchone()[0]
        return db.execute('SELECT COALESCE(SUM(allowance),0) FROM calls WHERE tenant=?',
                          (tenant,)).fetchone()[0]

    def reserve(self, tenant, budget, route, model, *, credential=None, metadata=None,
                budget_period=None, budget_started_at=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            if credential is not None:
                grant, digest = credential
                # Body reads and SQLite lock waits can outlive the initial header check.
                if (grant['id'] != tenant or grant.get('enabled') is not True
                        or not hmac.compare_digest(grant['token_sha256'], digest)
                        or self._expiry(db, grant) <= now):
                    raise AuthenticationError('tenant credential expired or revoked')
            metadata = metadata or {}
            foreground = metadata.get('workload') in FOREGROUND_WORKLOADS or (
                route == 'chat' and metadata.get('workload') in (None, 'unknown'))
            def background(item):
                lane, labels = item
                return (labels.get('workload') not in FOREGROUND_WORKLOADS
                        and not (lane == 'chat' and labels.get('workload') in (None, 'unknown')))
            input_chars = metadata.get('input_chars', 0)
            if type(input_chars) is not int or input_chars < 0:
                input_chars = 0
            def cap(limit, scoped):
                return limit if not scoped else max(1, limit * TENANT_SHARE_PERCENT // 100)
            tenant_input_limit = cap(INPUT_CHARS_PER_WINDOW if foreground
                                     else BACKGROUND_INPUT_CHARS_PER_WINDOW, True)
            if input_chars > tenant_input_limit:
                raise RequestTooLargeError('request input exceeds the per-window volume limit')
            # Check both the owner and the shared upstream credential pool. Every
            # configured tenant uses this proxy process's single provider key.
            for scope in (tenant, None):
                where = 'tenant=? AND ' if scope is not None else ''
                params = (scope, now - RATE_WINDOW_SECONDS, now) if scope is not None else (
                    now - RATE_WINDOW_SECONDS, now)
                rows = db.execute('SELECT route,created,metadata_json,active_until FROM calls WHERE ' + where +
                                  '(created>? OR active_until>?)', params).fetchall()
                window = [(row[0], json.loads(row[2] or '{}')) for row in rows
                          if row[1] > now - RATE_WINDOW_SECONDS]
                active = [(row[0], json.loads(row[2] or '{}')) for row in rows
                          if row[3] is not None and row[3] > now]
                label = 'tenant' if scope is not None else 'shared provider'
                scoped = scope is not None
                if len(window) >= cap(REQUESTS_PER_WINDOW, scoped) or (
                        not foreground and sum(background(item) for item in window) >= cap(BACKGROUND_REQUESTS_PER_WINDOW, scoped)):
                    raise RateLimitError(f'{label} request rate exceeded', 45 + secrets.randbelow(16))
                if len(active) >= cap(MAX_ACTIVE_CALLS, scoped) or (
                        not foreground and sum(background(item) for item in active) >= cap(MAX_ACTIVE_BACKGROUND_CALLS, scoped)):
                    raise RateLimitError(f'{label} concurrent calls busy', 3 + secrets.randbelow(5))
                window_chars = sum(item[1].get('input_chars', 0) for item in window)
                background_chars = sum(item[1].get('input_chars', 0) for item in window if background(item))
                if window_chars + input_chars > cap(INPUT_CHARS_PER_WINDOW, scoped) or (
                        not foreground and background_chars + input_chars
                        > cap(BACKGROUND_INPUT_CHARS_PER_WINDOW, scoped)):
                    raise RateLimitError(f'{label} input volume busy', 45 + secrets.randbelow(16))
            used = self._budget_used(db, tenant, budget_period, budget_started_at, now)
            if budget is not None and used + CALL_ALLOWANCE_CENTS > budget:
                raise PermissionError('tenant request budget exhausted')
            cursor = db.execute('INSERT INTO calls(tenant,route,model,created,allowance) VALUES(?,?,?,?,?)',
                                (tenant, route, model, now, CALL_ALLOWANCE_CENTS))
            db.execute('UPDATE calls SET metadata_json=? WHERE id=?',
                       (json.dumps(metadata), cursor.lastrowid))
            db.execute('UPDATE calls SET active_until=? WHERE id=?',
                       (now + ACTIVE_LEASE_SECONDS, cursor.lastrowid))
            return cursor.lastrowid

    def release(self, call):
        with self.connect() as db:
            db.execute('UPDATE calls SET active_until=NULL WHERE id=?', (call,))

    def renew_active(self, call):
        with self.connect() as db:
            db.execute('UPDATE calls SET active_until=? WHERE id=? AND active_until IS NOT NULL',
                       (time.time() + ACTIVE_LEASE_SECONDS, call))

    def finish(self, call, status, usage, *, proven_native_text=False, proven_chat_text=False,
               native_text_request_eligible=None, native_text_response_eligible=None,
               chat_text_request_eligible=None, chat_text_response_eligible=None):
        # Missing usage or ambiguous outcomes never release an allowance.
        usage = usage if isinstance(usage, dict) else {}
        def count(*names):
            for name in names:
                value = usage.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
            return None
        output = count('candidatesTokenCount', 'completion_tokens')
        thoughts = count('thoughtsTokenCount')
        if output is not None and thoughts is not None:
            output += thoughts
        # Settlement prices the fixed output ceiling, so it only holds when the reported
        # output (with separately reported thinking) is present, valid and within it.
        output_within_ceiling = (output is not None and output <= MAX_OUTPUT
                                 and ('thoughtsTokenCount' not in usage or thoughts is not None))
        with self.connect() as db:
            route = db.execute('SELECT route FROM calls WHERE id=?', (call,)).fetchone()
            chat_counts = normalize_google_openai(usage, MAX_OUTPUT) if route and route[0] == 'chat' else None
            db.execute('UPDATE calls SET status=?,input_tokens=?,output_tokens=? WHERE id=?',
                       (status, chat_counts['input_tokens'] if chat_counts is not None else
                        count('promptTokenCount') if proven_native_text else
                        count('promptTokenCount', 'prompt_tokens'),
                        chat_counts['billed_output_tokens'] if chat_counts is not None else output, call))
            row = db.execute('SELECT route,model,created,status,input_tokens FROM calls WHERE id=?', (call,)).fetchone()
            counts = chat_counts if chat_counts is not None else normalize(usage)
            allowance = CALL_ALLOWANCE_CENTS
            if row and row[0] == 'chat' and proven_chat_text:
                receipt_allowance = chat_receipt_allowance(row[1], row[2], row[3], usage)
                if receipt_allowance is not None:
                    allowance = receipt_allowance
                    counts['allowance_policy'] = 'chat_text_receipt_v1'
            if (row and row[0] == 'native' and proven_native_text and output_within_ceiling
                    and native_sparse_usage_safe(usage)):
                allowance = settled_text_allowance(row[1], row[2], row[3], row[4])
                if allowance < CALL_ALLOWANCE_CENTS:
                    counts['allowance_policy'] = 'native_text_v1'
            if row and row[0] == 'native' and proven_native_text:
                receipt_allowance = native_receipt_allowance(row[1], row[2], row[3], usage)
                if receipt_allowance is not None:
                    allowance = receipt_allowance
                    counts['allowance_policy'] = 'native_text_receipt_v2'
                    billed_output = usage['totalTokenCount'] - usage['promptTokenCount']
                    counts['billed_output_tokens'] = billed_output
                    counts['reasoning_tokens'] = billed_output - usage['candidatesTokenCount']
                    counts['visible_output_tokens'] = usage['candidatesTokenCount']
                    counts['usage_completeness'] = 'complete'
                    db.execute('UPDATE calls SET output_tokens=? WHERE id=?', (billed_output, call))
            counts.update(estimated_token_cost=estimate(row[1], counts, row[2]) if row else None,
                          token_cost_range=estimate_range(row[1], counts, row[2]) if row else None,
                          pricing_version=PRICING_VERSION, provider_fee_status='not_included')
            if row and row[0] == 'native' and native_text_request_eligible is not None:
                counts['native_text_request_eligible'] = native_text_request_eligible
                counts['native_text_response_eligible'] = native_text_response_eligible is True
                counts['native_text_settlement_reason'] = (
                    'request_ineligible' if not native_text_request_eligible else
                    'response_ineligible' if native_text_response_eligible is not True else
                    'receipt_settled' if counts.get('allowance_policy') == 'native_text_receipt_v2' else
                    'receipt_invalid' if 'totalTokenCount' in usage and allowance == CALL_ALLOWANCE_CENTS else
                    'output_unbounded' if not output_within_ceiling else
                    'settled' if allowance < CALL_ALLOWANCE_CENTS else 'tariff_unavailable')
            if row and row[0] == 'chat' and chat_text_request_eligible is not None:
                # Fixed categories only; never retain response keys, payloads, or provider text.
                counts['chat_text_settlement_reason'] = (
                    'upstream_failure' if status != 200 else
                    'request_ineligible' if not chat_text_request_eligible else
                    'response_ineligible' if chat_text_response_eligible is not True else
                    'receipt_settled' if counts.get('allowance_policy') == 'chat_text_receipt_v1' else
                    'receipt_unavailable')  # Existing usage/tariff gates remain unchanged.
            db.execute('UPDATE calls SET usage_json=? WHERE id=?', (json.dumps(counts), call))
            if row:
                db.execute('UPDATE calls SET allowance=? WHERE id=?', (allowance, call))
            db.execute('UPDATE calls SET active_until=NULL WHERE id=?', (call,))

    def budget_capability(self, tenant, budget, *, budget_period=None, budget_started_at=None):
        with self.connect() as db:
            used = self._budget_used(db, tenant, budget_period, budget_started_at, time.time())
        remaining = None if budget is None else max(0, budget - used)
        return {'version': 1, 'finite': budget is not None, 'remaining_cents': remaining,
                'can_admit': remaining is not None and remaining >= CALL_ALLOWANCE_CENTS,
                'supported_native_models': sorted(MODELS)}


def route(path, payload):
    match = re.fullmatch(r'/native/v1beta/models/([A-Za-z0-9._-]+):generateContent', path)
    if match:
        model = match.group(1)
        config = payload.get('generationConfig', {})
        if config.get('candidateCount', 1) != 1 or payload.get('cachedContent'):
            raise ValueError('multiple candidates and shared provider caches are not supported')
        cap = config.get('maxOutputTokens', MAX_OUTPUT)
        endpoint = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
        lane = 'native'
    elif path == '/openai/v1/chat/completions':
        if payload.get('n', 1) != 1:
            raise ValueError('multiple candidates are not supported')
        if payload.get('service_tier', 'default') != 'default' or payload.get('modalities', ['text']) != ['text']:
            raise ValueError('only standard text chat is supported')
        if any(tool.get('type') != 'function' for tool in payload.get('tools', [])):
            raise ValueError('chat supports function tools only')
        for message in payload.get('messages', []):
            content = message.get('content')
            if isinstance(content, list) and any(part.get('type') != 'text' for part in content):
                raise ValueError('multimodal requests use the native route')
        model = payload.get('model')
        cap = payload.get('max_completion_tokens', payload.get('max_tokens', MAX_OUTPUT))
        endpoint = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
        lane = 'chat'
    else:
        raise ValueError('unsupported route')
    if model not in MODELS or not isinstance(cap, int) or isinstance(cap, bool) or not 0 < cap <= MAX_OUTPUT:
        raise ValueError('model or output limit not allowed')
    return lane, model, endpoint


def join_leading_system_messages(payload):
    """Give Google's chat endpoint one ordered instruction message, without moving later turns."""
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return payload
    leading = 0
    for message in messages:
        if not isinstance(message, dict) or message.get('role') != 'system':
            break
        leading += 1
    if leading < 2:
        return payload
    instructions = messages[:leading]
    if any(set(message) != {'role', 'content'} or not isinstance(message['content'], str)
           for message in instructions):
        raise ValueError('ambiguous leading system messages')
    return {**payload, 'messages': [
        {'role': 'system', 'content': '\n\n'.join(message['content'] for message in instructions)},
        *messages[leading:],
    ]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def respond(self, status, body, *, retry_after=None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        if status == 429:
            self.send_header('Retry-After', str(retry_after or RATE_WINDOW_SECONDS))
            self.send_header('X-Sotto-Admission', 'busy')
        if status == 504 and isinstance(body.get('error'), dict) and body['error'].get('code') == 'sotto_brief_outcome_uncertain':
            self.send_header('X-Sotto-Outcome', 'uncertain')
        self.close_connection = True
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == '/health':
            return self.respond(200, {'status': 'ok'})
        if self.path != '/v1/capabilities/background-budget':
            return self.respond(404, {'error': 'not found'})
        authorization = self.headers.get('Authorization', '')
        if not authorization.startswith('Bearer '):
            return self.respond(401, {'error': 'unauthorized'})
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        tenant = next((t for t in self.server.tenants if t.get('enabled') is True
                       and hmac.compare_digest(t['token_sha256'], digest)
                       and self.server.ledger.expiry(t) > time.time()), None)
        if not tenant:
            return self.respond(401, {'error': 'unauthorized'})
        return self.respond(200, self.server.ledger.budget_capability(
            tenant['id'], tenant['budget_cents'], budget_period=tenant.get('budget_period'),
            budget_started_at=tenant.get('budget_started_at')))

    def do_POST(self):
        self.connection.settimeout(330)
        authorization = self.headers.get('Authorization', '')
        if not authorization.startswith('Bearer '):
            return self.respond(401, {'error': 'unauthorized'})
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        if self.path == '/v1/lease/renew':
            return self.renew_lease(digest)
        tenant = next((t for t in self.server.tenants if t.get('enabled') is True
                       and hmac.compare_digest(t['token_sha256'], digest)
                       and self.server.ledger.expiry(t) > time.time()), None)
        if not tenant:
            return self.respond(401, {'error': 'unauthorized'})
        try:
            size = int(self.headers.get('Content-Length', '-1'))
            if not 0 < size <= MAX_BODY or self.headers.get('Transfer-Encoding'):
                return self.respond(413, {'error': 'bounded content length required'})
            raw = self.rfile.read(size)
            if len(raw) != size:
                return self.respond(400, {'error': 'incomplete request'})
            payload = json.loads(raw, object_pairs_hook=unique_object)
            lane, model, endpoint = route(self.path, payload)
            if lane == 'chat':
                payload = join_leading_system_messages(payload)
            raw = json.dumps(payload, allow_nan=False, separators=(',', ':')).encode()
            if len(raw) > MAX_BODY:
                raise RequestTooLargeError('merged request exceeds body limit')
            text_request = lane == 'native' and native_text_request(payload)
            text_chat_request = lane == 'chat' and chat_text_request(payload)
            if (self.headers.get('X-Sotto-Require-Finite-Budget') == 'true'
                    and tenant['budget_cents'] is None):
                raise PermissionError('finite tenant budget required')
            call = self.server.ledger.reserve(tenant['id'], tenant['budget_cents'], lane, model,
                                              credential=(tenant, digest),
                                              budget_period=tenant.get('budget_period'),
                                              budget_started_at=tenant.get('budget_started_at'),
                                              metadata=request_metadata(
                                                  self.headers, payload, lane,
                                                  {k: tenant.get(k) for k in ('application', 'deployment')}))
        except AuthenticationError:
            return self.respond(401, {'error': 'unauthorized'})
        except RateLimitError as error:
            return self.respond(429, {'error': {'code': 'sotto_rate_limited', 'message': 'Tenant model requests are busy; retry shortly.',
                                               'reason': error.reason}}, retry_after=error.retry_after)
        except RequestTooLargeError:
            return self.respond(413, {'error': {'code': 'sotto_request_too_large',
                                                'message': 'This model request is larger than one window allows; retrying will not help.'}})
        except PermissionError:
            return self.respond(402, {'error': {'code': 'sotto_budget_exhausted', 'type': 'budget_exhausted',
                                              'period': 'calendar_month' if tenant.get('budget_period') == 'calendar_month' else 'lifetime',
                                              'message': ('Sotto monthly model allowance reached; it resets at the start of the next UTC month.'
                                                          if tenant.get('budget_period') == 'calendar_month' else
                                                          'Sotto pilot model allowance reached; waiting will not reset it.')}})
        except (ValueError, TypeError, AttributeError):
            return self.respond(400, {'error': 'invalid model request'})
        try:
            return self._forward(call, lane, endpoint, raw, text_request, text_chat_request)
        finally:
            # Also covers a disconnected client or an unexpected response parser error.
            # The metering row remains reserved; only the concurrency slot is released.
            try:
                self.server.ledger.release(call)
            except sqlite3.Error:
                pass

    def _forward(self, call, lane, endpoint, raw, text_request, text_chat_request):
        headers = {'Content-Type': 'application/json', 'Accept-Encoding': 'identity'}
        headers['x-goog-api-key' if lane == 'native' else 'Authorization'] = (
            self.server.upstream_key if lane == 'native' else 'Bearer ' + self.server.upstream_key)
        req = urllib.request.Request(endpoint, data=raw, headers=headers, method='POST')
        # These labels can only shorten a tenant-authenticated native text call's wait.
        # They grant no permission or refund. Unlabelled/other workloads keep their old path.
        labels = request_metadata(self.headers, {}, lane)
        brief = (lane == 'native' and text_request
                 and labels.get('workload') in BRIEF_WORKLOADS
                 and all(labels.get(k) for k in ('operation_id', 'run_id', 'attempt')))
        try:
            response = self.server.open_upstream(req, timeout=BRIEF_UPSTREAM_WAIT_SECONDS if brief else 300)
            if brief:
                # Native generateContent is not SSE. Wait for the complete bounded response
                # before sending 200: a body timeout must remain an explicit uncertain outcome,
                # not a truncated success which the caller might automatically retry.
                with response:
                    content_type = response.headers.get('Content-Type', 'application/json')
                    data = response.read(MAX_BODY + 1)
                    if len(data) > MAX_BODY:
                        raise TimeoutError('brief response could not be confirmed')
                response = io.BytesIO(data)
                response.headers = {'Content-Type': content_type}

        except urllib.error.HTTPError as error:
            self.server.ledger.finish(call, error.code, {},
                                      native_text_request_eligible=text_request if lane == 'native' else None,
                                      native_text_response_eligible=False,
                                      chat_text_request_eligible=text_chat_request if lane == 'chat' else None,
                                      chat_text_response_eligible=False)
            if brief and error.code == 504:
                # A provider gateway timeout also leaves paid work uncertain.
                error.close()
                return self.respond(504, {'error': {'code': 'sotto_brief_outcome_uncertain',
                    'message': 'The brief model response could not be confirmed; do not retry this request automatically.'}})
            # Preserve other provider error envelopes/statuses, including retry reasons.
            data = error.read(MAX_BODY)
            self.send_response(error.code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except (OSError, http.client.HTTPException) as error:
            # urllib wraps only connect/send failures in URLError. A raw error came after the
            # request was sent (no/partial response), so the provider may have billed it.
            uncertain = brief and (not isinstance(error, urllib.error.URLError) or
                                   isinstance(getattr(error, 'reason', None), TimeoutError))
            self.server.ledger.finish(call, 504 if uncertain else 502, {},
                                      native_text_request_eligible=text_request if lane == 'native' else None,
                                      native_text_response_eligible=False,
                                      chat_text_request_eligible=text_chat_request if lane == 'chat' else None,
                                      chat_text_response_eligible=False)
            if uncertain:
                return self.respond(504, {'error': {'code': 'sotto_brief_outcome_uncertain',
                    'message': 'The brief model response could not be confirmed; do not retry this request automatically.'}})
            return self.respond(502, {'error': 'upstream unavailable'})
        with response:
            content_type = response.headers.get('Content-Type', 'application/json')
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            captured = bytearray()
            complete_capture = True
            last_active_renewal = time.time()
            try:
                while chunk := response.read1(65536):
                    if time.time() - last_active_renewal >= 60:
                        try:
                            self.server.ledger.renew_active(call)
                        except sqlite3.Error:
                            pass  # metering failure must not interrupt the upstream reply
                        last_active_renewal = time.time()
                    available = MAX_BODY - len(captured)
                    if len(chunk) > available:
                        complete_capture = False
                    if available > 0:
                        captured.extend(chunk[:available])
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, TimeoutError):
                self.server.ledger.finish(call, 502, {},
                                          native_text_request_eligible=text_request if lane == 'native' else None,
                                          native_text_response_eligible=False,
                                          chat_text_request_eligible=text_chat_request if lane == 'chat' else None,
                                          chat_text_response_eligible=False)
                return
            usage = {}
            text_response = False
            text_chat_response = False
            try:
                if not complete_capture:
                    pass
                elif 'text/event-stream' in content_type:
                    for line in captured.decode().splitlines():
                        if line.startswith('data: ') and line[6:] != '[DONE]':
                            item = json.loads(line[6:])
                            usage = item.get('usage') or usage
                else:
                    item = json.loads(captured)
                    usage = item.get('usageMetadata') or item.get('usage') or {}
                    text_response = native_text_response(item)
                    text_chat_response = chat_text_response(item)
            except (ValueError, UnicodeError):
                pass
            self.server.ledger.finish(call, 200, usage,
                                      proven_native_text=text_request and text_response,
                                      proven_chat_text=text_chat_request and text_chat_response,
                                      native_text_request_eligible=text_request if lane == 'native' else None,
                                      native_text_response_eligible=text_response,
                                      chat_text_request_eligible=text_chat_request if lane == 'chat' else None,
                                      chat_text_response_eligible=text_chat_response)

    def renew_lease(self, digest):
        # No prompt, provider call, billing mutation or returned credential. A model token
        # cannot extend its own life; only this tenant's separately provisioned control key can.
        try:
            size = int(self.headers.get('Content-Length', '-1'))
            if not 0 < size <= 2048 or self.headers.get('Transfer-Encoding'):
                return self.respond(400, {'error': 'invalid renewal request'})
            body = json.loads(self.rfile.read(size))
            tenant = next((t for t in self.server.tenants if t.get('enabled') is True
                           and t['id'] == body.get('tenant_id')
                           and isinstance(t.get('renewal_token_sha256'), str)
                           and hmac.compare_digest(t['renewal_token_sha256'], digest)
                           and hmac.compare_digest(t['token_sha256'], body.get('token_sha256', ''))), None)
        except (ValueError, TypeError, AttributeError):
            return self.respond(400, {'error': 'invalid renewal request'})
        if tenant is None:
            return self.respond(401, {'error': 'unauthorized'})
        return self.respond(200, {'tenant_id': tenant['id'], 'expires_at': self.server.ledger.renew(tenant)})


def serve(address, tenants, key, ledger_path, opener=urllib.request.urlopen):
    validate_tenants(tenants)
    server = ThreadingHTTPServer(address, Handler)
    server.tenants = tenants
    server.upstream_key = key
    server.ledger = Ledger(ledger_path)
    server.open_upstream = opener
    return server


def validate_tenants(tenants):
    """Reject ambiguous auth or implicit budget bypasses before accepting traffic."""
    if not isinstance(tenants, list):
        raise ValueError('tenant configuration must be a list')
    ids, tokens = set(), set()
    for tenant in tenants:
        if not isinstance(tenant, dict) or not isinstance(tenant.get('id'), str) or not tenant['id']:
            raise ValueError('tenant id is required')
        token = tenant.get('token_sha256')
        if not isinstance(token, str) or re.fullmatch(r'[0-9a-f]{64}', token) is None:
            raise ValueError('tenant token hash must be lowercase SHA-256')
        budget = tenant.get('budget_cents', 'missing')
        if budget is not None and (not isinstance(budget, int) or isinstance(budget, bool) or budget < 0):
            raise ValueError('tenant budget must be nonnegative cents or explicit null')
        period = tenant.get('budget_period')
        started_at = tenant.get('budget_started_at')
        if period not in (None, 'calendar_month'):
            raise ValueError('unsupported tenant budget period')
        if period == 'calendar_month':
            if budget is None or budget == 'missing':
                raise ValueError('monthly tenant budget must be finite')
            if (not isinstance(started_at, (int, float)) or isinstance(started_at, bool)
                    or not math.isfinite(started_at) or not 0 < started_at <= time.time()):
                raise ValueError('monthly tenant budget activation must be a past Unix timestamp')
        elif started_at is not None:
            raise ValueError('tenant budget activation requires a monthly period')
        if not isinstance(tenant.get('enabled'), bool):
            raise ValueError('tenant enabled flag is required')
        expiry = tenant.get('expires_at')
        if (not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
                or not math.isfinite(expiry)):
            raise ValueError('tenant expiry is required')
        for label in ('application', 'deployment'):
            value = tenant.get(label)
            # Labels land in every ledger row and the report: a fixed short identifier, never an
            # email, an account name or a structure.
            if value is not None and (not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9._-]{1,64}', value) is None):
                raise ValueError(f'tenant {label} must be a short identifier')
        if tenant['id'] in ids or token in tokens:
            raise ValueError('tenant ids and bearer hashes must be unique')
        ids.add(tenant['id'])
        tokens.add(token)


if __name__ == '__main__':
    tenants = json.loads(os.environ['SOTTO_PROXY_TENANTS'])
    server = serve(('0.0.0.0', int(os.environ.get('PORT', '8788'))), tenants,
                   os.environ['GOOGLE_AI_API_KEY'], Path(os.environ.get('SOTTO_DATA', '/data')) / 'proxy.sqlite3')
    print('Sotto model proxy listening', flush=True)
    server.serve_forever()
