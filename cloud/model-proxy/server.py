"""Two fixed model surfaces, tenant authentication, and durable usage reservations.

Never log prompts, credentials, URLs with keys, images or upstream response bodies.
The pilot charges a conservative $2 allowance per attempted upstream call. This is
an admission budget, not an invoice; provider billing remains authoritative. Holding
the allowance across crashes/ambiguous failures avoids freeing spend already used.
Successful 3.8 chat calls settle to reported input plus the FULL model output ceiling,
covering unreported reasoning tokens. Native/grounded calls retain their allowance.
"""
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODELS = {'gemini-3.8-flash', 'gemini-3-flash-preview', 'gemini-3.5-flash-lite'}
MAX_BODY = 8 * 1024 * 1024
MAX_OUTPUT = 65536
CALL_ALLOWANCE_CENTS = 200
LEASE_SECONDS = 30 * 86400
RATE_WINDOW_SECONDS = 60
REQUESTS_PER_WINDOW = 60
# Verified 2026-09-06: https://ai.google.dev/gemini-api/docs/latest-model?hl=en
# Introductory standard API prices through 2026-12-31; no future-price assumption.
PRICING_START = 1788307200  # 2026-09-02 UTC
PRICING_END = 1798761600    # 2027-01-01 UTC
SETTLEMENT_MIGRATION = 'settled_chat_allowance_v1'


def settled_chat_allowance(route, model, created, status, input_tokens):
    if (route == 'chat' and model == 'gemini-3.8-flash' and status == 200
            and PRICING_START <= created < PRICING_END
            and isinstance(input_tokens, int) and not isinstance(input_tokens, bool)
            and 0 <= input_tokens <= 1048576):
        return min(CALL_ALLOWANCE_CENTS, math.ceil((input_tokens * 75 + MAX_OUTPUT * 375) / 1_000_000))
    return CALL_ALLOWANCE_CENTS


class RateLimitError(RuntimeError):
    pass


class AuthenticationError(RuntimeError):
    pass


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
            db.execute('CREATE INDEX IF NOT EXISTS calls_tenant_created ON calls(tenant,created)')
            db.execute('CREATE TABLE IF NOT EXISTS model_leases (tenant TEXT NOT NULL, token_hash TEXT NOT NULL, '
                       'expires REAL NOT NULL, PRIMARY KEY(tenant,token_hash))')
            db.execute('CREATE TABLE IF NOT EXISTS proxy_migrations (name TEXT PRIMARY KEY, applied REAL NOT NULL)')

            # Backfill only receipted chat attempts, idempotently. Failed, interrupted,
            # unknown-usage and grounded/native calls keep the original reservation. Record the
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

    def reserve(self, tenant, budget, route, model, *, credential=None):
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
            recent = db.execute('SELECT COUNT(*) FROM calls WHERE tenant=? AND created>?',
                                (tenant, now - RATE_WINDOW_SECONDS)).fetchone()[0]
            if recent >= REQUESTS_PER_WINDOW:
                raise RateLimitError('tenant request rate exceeded')
            used = db.execute('SELECT COALESCE(SUM(allowance),0) FROM calls WHERE tenant=?', (tenant,)).fetchone()[0]
            if budget is not None and used + CALL_ALLOWANCE_CENTS > budget:
                raise PermissionError('tenant request budget exhausted')
            cursor = db.execute('INSERT INTO calls(tenant,route,model,created,allowance) VALUES(?,?,?,?,?)',
                                (tenant, route, model, time.time(), CALL_ALLOWANCE_CENTS))
            return cursor.lastrowid

    def finish(self, call, status, usage):
        # Missing usage or ambiguous outcomes never release an allowance.
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
        with self.connect() as db:
            db.execute('UPDATE calls SET status=?,input_tokens=?,output_tokens=? WHERE id=?',
                       (status, count('promptTokenCount', 'prompt_tokens'),
                        output, call))
            row = db.execute('SELECT route,model,created,status,input_tokens FROM calls WHERE id=?', (call,)).fetchone()
            if row:
                db.execute('UPDATE calls SET allowance=? WHERE id=?', (settled_chat_allowance(*row), call))

    def budget_capability(self, tenant, budget):
        with self.connect() as db:
            used = db.execute('SELECT COALESCE(SUM(allowance),0) FROM calls WHERE tenant=?',
                              (tenant,)).fetchone()[0]
        remaining = None if budget is None else max(0, budget - used)
        return {'version': 1, 'finite': budget is not None, 'remaining_cents': remaining,
                'can_admit': remaining is not None and remaining >= CALL_ALLOWANCE_CENTS}


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


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def respond(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        if status == 429:
            self.send_header('Retry-After', str(RATE_WINDOW_SECONDS))
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
                       and self.server.ledger.expiry(t) > time.time()
                       and hmac.compare_digest(t['token_sha256'], digest)), None)
        if not tenant:
            return self.respond(401, {'error': 'unauthorized'})
        return self.respond(200, self.server.ledger.budget_capability(
            tenant['id'], tenant['budget_cents']))

    def do_POST(self):
        self.connection.settimeout(330)
        authorization = self.headers.get('Authorization', '')
        if not authorization.startswith('Bearer '):
            return self.respond(401, {'error': 'unauthorized'})
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        if self.path == '/v1/lease/renew':
            return self.renew_lease(digest)
        tenant = next((t for t in self.server.tenants if t.get('enabled') is True
                       and self.server.ledger.expiry(t) > time.time()
                       and hmac.compare_digest(t['token_sha256'], digest)), None)
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
            raw = json.dumps(payload, allow_nan=False, separators=(',', ':')).encode()
            lane, model, endpoint = route(self.path, payload)
            if (self.headers.get('X-Sotto-Require-Finite-Budget') == 'true'
                    and tenant['budget_cents'] is None):
                raise PermissionError('finite tenant budget required')
            call = self.server.ledger.reserve(tenant['id'], tenant['budget_cents'], lane, model,
                                              credential=(tenant, digest))
        except AuthenticationError:
            return self.respond(401, {'error': 'unauthorized'})
        except RateLimitError:
            return self.respond(429, {'error': {'code': 'sotto_rate_limited', 'message': 'Tenant model requests are busy; retry shortly.'}})
        except PermissionError:
            return self.respond(402, {'error': {'code': 'sotto_budget_exhausted', 'type': 'budget_exhausted',
                                              'message': 'Sotto pilot model allowance reached; waiting will not reset it.'}})
        except (ValueError, TypeError, AttributeError):
            return self.respond(400, {'error': 'invalid model request'})
        headers = {'Content-Type': 'application/json', 'Accept-Encoding': 'identity'}
        headers['x-goog-api-key' if lane == 'native' else 'Authorization'] = (
            self.server.upstream_key if lane == 'native' else 'Bearer ' + self.server.upstream_key)
        req = urllib.request.Request(endpoint, data=raw, headers=headers, method='POST')
        try:
            response = self.server.open_upstream(req, timeout=300)
        except urllib.error.HTTPError as error:
            self.server.ledger.finish(call, error.code, {})
            # Preserve the native provider error envelope/status, including retry reasons.
            data = error.read(MAX_BODY)
            self.send_response(error.code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except (OSError, http.client.HTTPException):
            self.server.ledger.finish(call, 502, {})
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
            try:
                while chunk := response.read1(65536):
                    if len(captured) < MAX_BODY:
                        captured.extend(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, TimeoutError):
                self.server.ledger.finish(call, 502, {})
                return
            usage = {}
            try:
                if 'text/event-stream' in content_type:
                    for line in captured.decode().splitlines():
                        if line.startswith('data: ') and line[6:] != '[DONE]':
                            item = json.loads(line[6:])
                            usage = item.get('usage') or usage
                else:
                    item = json.loads(captured)
                    usage = item.get('usageMetadata') or item.get('usage') or {}
            except (ValueError, UnicodeError):
                pass
            self.server.ledger.finish(call, 200, usage)

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
        if not isinstance(tenant.get('enabled'), bool):
            raise ValueError('tenant enabled flag is required')
        expiry = tenant.get('expires_at')
        if (not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
                or not math.isfinite(expiry)):
            raise ValueError('tenant expiry is required')
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
