#!/usr/bin/env python3
"""
Sotto trigger receiver (SPEC §4.1). The endpoint beside Sotto's chat runtime.

The Bridge POSTs `{type:"morning_ready"|"evening_ready", date, local_data}` here when the Mac comes
up. The receiver (1) authenticates the bearer (constant-time), (2) dedupes against the per-day
delivered flag — and against the cron's own compose window, so a wake minutes behind the scheduled
brief folds into the snapshot instead of racing it, (3) stages local_data, (4) enqueues the brief
run. Whatever still gets spawned is gated once more at the send seam: the outbox
claims the day's deliver-once marker itself, so only one brief per day ever leaves the box.

Security: binds 0.0.0.0 on Railway behind its TLS proxy (127.0.0.1 locally), caps body size, strictly
validates `date` before using it in any path, and only writes the delivered flag AFTER the skill
is successfully enqueued.

It also owns the SCHEDULE: every crons.json row fires on this process's own heartbeat
(_cron_tick), so a morning or evening brief has exactly one delivery lane — spawn, outbox,
deliver-once gate — however it was started. /mcp and /bridge/* take the MCP bearer; /sotto/trigger
takes the trigger token; the setup/pairing/debug-status pages (which surface the pairing link = the
bearer) take a per-deploy setup code printed to the boot log. /health is open. Every response
leaves through _write/_redirect, which stamp the dashboard's security headers (nosniff, no-referrer,
frame-deny, no-store, plus SETUP_CSP on HTML) and set the wizard cookie with the dashboard's exact
attributes (Secure; HttpOnly; SameSite=Lax). Stdlib only.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import signal
import shutil
import subprocess
import socket
import select
import sys
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from functools import lru_cache, partial


DATA = os.environ.get("SOTTO_DATA", "/data")
# Captured before any child can inherit it. Managed execution makes this process
# nondumpable before importing us; the attended gateway holds its own private copy.
CHAT_SEND_TOKEN = os.environ.pop('SOTTO_CHAT_SEND_TOKEN', '')
_SOTTO_ADAPTERS = {}
_SOTTO_ADAPTERS_LOCK = threading.RLock()


if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared_lib import load_shared_lib  # noqa: E402

_load_shared_lib = partial(load_shared_lib, anchor=__file__)


def _load_tzchain():
    return _load_shared_lib('tzchain')


TZCHAIN = _load_tzchain()
SETUP_MARKUP = _load_shared_lib('setup_markup')
_FAVICON = SETUP_MARKUP._FAVICON
_page_head = SETUP_MARKUP._page_head
_nav = SETUP_MARKUP._nav
_narrow_page = SETUP_MARKUP._narrow_page
_connect_page = SETUP_MARKUP._connect_page
_connect_error_page = SETUP_MARKUP._connect_error_page
_humanize_ago = SETUP_MARKUP._humanize_ago
_tile = SETUP_MARKUP._tile
_plain_tile = SETUP_MARKUP._plain_tile
WORK_QUEUE = _load_shared_lib('work_queue')
REQUESTED_BRIEF = _load_shared_lib('requested_brief')
RUNTIME_OWNER_BRIEF_REQUEST = _load_shared_lib('runtime_owner_brief_request')
RUNTIME_OWNER_MEETING_PREP_REQUEST = _load_shared_lib('runtime_owner_meeting_prep_request')
RUNTIME_OWNER_SKILL_REQUEST = _load_shared_lib('runtime_owner_skill_request')
RUNTIME_OWNER_WEB_READ = _load_shared_lib('runtime_owner_web_read')
RUNTIME_WEB_READER = _load_shared_lib('attended_web_read')
RUNTIME_KNOWLEDGE_READ = _load_shared_lib('runtime_knowledge_read')
CLIENT_MCP = _load_shared_lib('client_mcp')
CLIENT_OAUTH = _load_shared_lib('client_oauth')
CLIENT_MCP_BACKEND = _load_shared_lib('client_mcp_backend')
RUNTIME_OWNER_OFFER_REPLY = _load_shared_lib('runtime_owner_offer_reply')
RUNTIME_ACTIVATION = _load_shared_lib('runtime_activation')
OWNER_LOOP_CAPABILITY = _load_shared_lib('owner_loop_capability')
RUNTIME_OWNER_LOOP = _load_shared_lib('runtime_owner_loop')
RUNTIME_OWNER_REMINDER = _load_shared_lib('runtime_owner_reminder')
RUNTIME_OWNER_REMINDER_TOOL = _load_shared_lib('runtime_owner_reminder_tool')
RUNTIME_OWNER_LOOP_TOOL = _load_shared_lib('runtime_owner_loop_tool')
RUNTIME_OWNER_PREFERENCE_TOOL = _load_shared_lib('runtime_owner_preference_tool')
FRESH_ROUTINES = _load_shared_lib('fresh_routines')
RUNTIME_OWNER_ROUTINE_TOOL = _load_shared_lib('runtime_owner_routine_tool')
RUNTIME_OWNER_GMAIL_DRAFT_TOOL = _load_shared_lib('runtime_owner_gmail_draft_tool')
RUNTIME_OWNER_CALENDAR_TOOL = _load_shared_lib('runtime_owner_calendar_tool')
RUNTIME_OWNER_BRIDGE_SEND_TOOL = _load_shared_lib('runtime_owner_bridge_send_tool')
RUNTIME_OWNER_CONTACTS_TOOL = _load_shared_lib('runtime_owner_contacts_tool')
RUNTIME_OWNER_QUIET = _load_shared_lib('runtime_owner_quiet')
SELFHOST_SOURCE_AUTHORITY = _load_shared_lib('selfhost_source_authority')
OWNER_ENDPOINT_HTTP = _load_shared_lib('owner_endpoint_http')


def _sotto_adapter(name):
    """Load one Sotto adapter module (runtime API, Photon owner policy, model lease) from the image
    or the source checkout."""
    for directory in (Path(__file__).resolve().parents[2] / 'adapters' / 'sotto',
                      Path('/app/adapters/sotto')):
        path = directory / (name + '.py')
        if path.is_file():
            key = (name, str(path.resolve()))
            with _SOTTO_ADAPTERS_LOCK:
                if key not in _SOTTO_ADAPTERS:
                    spec = importlib.util.spec_from_file_location('sotto_' + name, path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    _SOTTO_ADAPTERS[key] = module
                return _SOTTO_ADAPTERS[key]
    raise RuntimeError(f'Sotto adapter {name} is missing')


def _runtime_adapter():
    """Sotto's runtime boundary: the loopback chat service and its packaged tools."""
    return _sotto_adapter('runtime_api')


# One shared SECRET by default: the Bridge's wake-push sends the same token it dials in with
# (BRIDGE_TOKEN → SOTTO_MCP_TOKEN), so /sotto/trigger accepts it unless a dedicated
# SOTTO_TRIGGER_TOKEN is set — otherwise default-on wake-push would silently 401.
_TRIGGER_TOKEN = os.environ.get("SOTTO_TRIGGER_TOKEN", "")
_MCP_TOKEN_ENV = os.environ.get("SOTTO_MCP_TOKEN", "")
TOKEN = _TRIGGER_TOKEN or _MCP_TOKEN_ENV
# RELAY_TOKEN is the ROOT bearer — the trust anchor the pairing link hands the Mac. It authenticates
# the BRIDGE's lanes (the reverse-relay dial-in, event ingestion) plus the operator surfaces (setup,
# diagnostics). Falls back to the trigger token.
RELAY_TOKEN = _MCP_TOKEN_ENV or _TRIGGER_TOKEN


GOOGLE_READ_DIAGNOSTICS = _sotto_adapter('google_read_contract')


def derive_mcp_token(secret: str) -> str:
    """The bearer the chat runtime presents on /mcp: HMAC-SHA256(root, "sotto-mcp"), hex. The chat
    runtime is the most exposed principal in the image — it converses with prompt-injectable
    content — so it must never hold the root: with only the derived token, a compromised agent
    cannot ingest fake Bridge events, dial the relay, or open the setup page (which renders the
    pairing link, i.e. the root). One-way on purpose; the Bridge and operator keep the root and pay
    nothing. adapters/sotto/startup_config.py renders the runtime's `bridge.readBearer` with the
    SAME derivation — a parity test pins the two."""
    return hmac.new(secret.encode(), b"sotto-mcp", hashlib.sha256).hexdigest() if secret else ""


# What /mcp accepts — and the ONLY thing it accepts: presenting the root there is refused, which is
# what makes the boundary real rather than a naming convention.
MCP_TOKEN = derive_mcp_token(RELAY_TOKEN)
SKILL = {"morning_ready": "sotto-morning-brief", "evening_ready": "sotto-evening-brief"}
MAX_BYTES = 8 * 1024 * 1024  # 8 MB — a LocalData snapshot is KBs; reject anything larger
DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
# A `.claim` this old with no `.delivered` marker means the enqueued run died silently (Popen
# succeeded, the skill never delivered). A fresh trigger may then reclaim and retry.
CLAIM_STALE_SECS = _load_shared_lib("funnel_policy").CLAIM_STALE_SECS

# The setup code that gates the setup/pairing/debug-status surface (/setup, /pair, /google/*,
# /debug/google). Those pages leak the MCP bearer (the pairing link), so "the Railway URL is the
# secret" is not enough. Resolved lazily: env override → persisted file on the volume → generated
# once and persisted (0600). Printed to stdout at boot as a full setup URL, so the user grabs it
# from the deploy logs.
SETUP_CODE = None


def resolve_setup_code() -> str:
    """Resolve (and cache) the setup code. Never raises; always returns a non-empty code."""
    global SETUP_CODE
    if SETUP_CODE:
        return SETUP_CODE
    code = (os.environ.get("SOTTO_SETUP_CODE") or "").strip()
    if not code:
        path = os.path.join(DATA, "setup_code")
        try:
            with open(path, encoding="utf-8") as f:
                code = f.read().strip()
        except OSError:
            code = ""
        if not code:
            code = secrets.token_urlsafe(9)  # 12 URL-safe chars
            try:
                os.makedirs(DATA, exist_ok=True)
                fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(code)
            except OSError:
                pass  # no volume yet — the code still holds for this process's lifetime
    SETUP_CODE = code  # noqa: N806 — assignment to the process-global setup credential
    return code

# Reverse-MCP relay: the Mac dials OUT to /bridge/poll|respond; the chat runtime calls /mcp locally.
_relay_spec = importlib.util.spec_from_file_location(
    "relay", os.path.join(os.path.dirname(__file__), "relay.py"))
_relay_mod = importlib.util.module_from_spec(_relay_spec)
_relay_spec.loader.exec_module(_relay_mod)
RELAY = _relay_mod.Relay()

def _source_context():
    """Load the shared permission owner in source checkouts and flattened runtime images."""
    module = getattr(_source_context, '_module', None)
    if module is not None:
        return module
    script = _find_sotto_script('_shared', 'lib', 'source_context.py')
    if not script:
        raise RuntimeError('source context helper missing')
    directory = os.path.dirname(script)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    module = importlib.import_module('source_context')
    _source_context._module = module
    return module



def _source_corrections():
    _source_context()  # install the existing shared library path
    return importlib.import_module('source_corrections')


def _knowledge_module(name):
    script = _find_sotto_script('_shared', 'knowledge', name + '.py')
    if not script:
        raise RuntimeError('knowledge helper missing')
    _source_context()
    directory = os.path.dirname(script)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    return importlib.import_module(name)


def _project_dashboard_person(content):
    if not _source_corrections().enabled():
        return content
    return _knowledge_module('knowledge').project_person_content(content, DATA)


def _admit_source_corrections(body):
    # Compatibility response for old native payloads; never admit new repair work.
    return {'source_corrections': {'status': 'disabled', 'accepted': 0}} if 'source_corrections' in body else {}


def _owner_continuity():
    """Load the Python-owned bounded projection from the selected packaged skills tree."""
    module = getattr(_owner_continuity, '_module', None)
    if module is not None:
        return module
    expected = {
        name: _find_sotto_script('_shared', subdir, name + '.py')
        for name, subdir in (('owner_continuity', 'lib'), ('protected_ledger_io', 'lib'),
                             ('ledger_provenance', 'lib'), ('ledger_io', 'scripts'),
                             ('loops_query', 'scripts'), ('retune_scan', 'scripts'))
    }
    if any(path is None for path in expected.values()):
        raise RuntimeError('owner continuity helper missing')
    for directory in {os.path.dirname(path) for path in expected.values()}:
        if directory not in sys.path:
            sys.path.insert(0, directory)
    # Python's flat module cache can retain a same-named module from another
    # skills tree. Refuse it before any protected read instead of trusting the
    # ambient search path or invoking a CLI.
    for name, expected_path in expected.items():
        loaded = importlib.import_module(name)
        if Path(loaded.__file__).resolve() != Path(expected_path).resolve():
            raise RuntimeError('owner continuity helper path mismatch')
    module = sys.modules['owner_continuity']
    _owner_continuity._module = module
    return module


def _dashboard_loop_rows():
    """Project the protected ledger under one unchanged current source authority."""
    before = _runtime_source_authority()
    if before.get('complete') is not True or not before.get('revision'):
        raise RuntimeError('loop source authority unavailable')
    grants = (before.get('bridge_sources', []) + before.get('google_sources', [])
              + before.get('connector_sources', []))
    # Reuse the existing packaged-helper path checks; never import a writable skills copy.
    _owner_continuity()
    from loops_query import permitted_rows
    rows = permitted_rows(DATA, grants)
    after = _runtime_source_authority()
    if after.get('complete') is not True or after.get('revision') != before['revision']:
        raise RuntimeError('loop source authority changed')
    return rows


# Last live Bridge consent tokens seen by this process, keyed by data root, mode and tenant.
# They keep the revision about consent, not Mac availability; they never grant a Bridge source.
_BRIDGE_CONSENT_SEEN = {}


def _runtime_source_authority(*, include_search_policy=False) -> dict:
    """Bind independent Bridge, Google and connector evidence to current consent generations.

    `bridge_sources` is live availability; the revision binds Bridge *consent* tokens. A sleeping
    Mac or a timed-out probe empties `bridge_sources` but keeps the revision, so a Google-only
    approval survives it, while any Bridge, cloud, ledger, Google or connector consent change still
    changes the revision.
    """
    unavailable = {'version': 2, 'revision': None, 'complete': False,
                   'bridge_sources': [], 'google_sources': [], 'connector_sources': []}
    import managed
    import google_source_authority
    managed_mode = managed.enabled()
    ledger_before = managed.source_authority_state(DATA) if managed_mode else None
    if managed_mode and ledger_before is None:
        return unavailable
    try:
        corrections_before = _source_corrections().revision(DATA)
        granola_path = Path(DATA) / 'connectors/granola.json'
        granola_before = SELFHOST_SOURCE_AUTHORITY.granola_generation(granola_path)
        explicit_google_root = os.environ.get('SOTTO_HOME', '').strip()
        # Bridge authority does not require a Google credential home. Sotto's
        # home_path correctly refuses an absent home, so resolve it only when
        # Google could contribute an independently authorized source.
        google_path = (_runtime_adapter().home_path('google_token.json')
            if explicit_google_root else None)
        google_before = (google_source_authority.state(DATA, google_path, managed_mode=managed_mode,
            tenant=os.environ.get('SOTTO_TENANT_ID', ''), managed_ledger=ledger_before)
            if explicit_google_root else None)
        if explicit_google_root and google_before is None:
            return unavailable
        calendar_before = _load_shared_lib('google_calendar_read').authority_selection(DATA, generation=google_before['generation'] if google_before else 'unavailable')
        selfhost_before = (None if managed_mode or google_path is None else SELFHOST_SOURCE_AUTHORITY.generations(
            google_path,
            Path(DATA) / 'connectors/granola.json'))
        policy = None
        local_search = {'status': 'unknown'}
        if RELAY.bridge_connected():
            try:
                request = {'jsonrpc': '2.0', 'id': 'source-authority', 'method': 'tools/call',
                           'params': {'name': 'health', 'arguments': {}}}
                response = RELAY.mcp_call(request, timeout=2.0)
                result = response.get('result') if isinstance(response, dict) else None
                if (isinstance(result, dict) and not response.get('error')
                        and result.get('isError') is not True):
                    source = _source_context()
                    health = source.unwrap_tool_result(result)
                    policy = source.bridge_source_policy(health, managed_mode)
                    local_search = source.bridge_search_policy(health)
            except Exception:  # noqa: BLE001 - a failed Mac probe never erases independent Google consent
                pass
        ledger_after = managed.source_authority_state(DATA) if managed_mode else None
        if managed_mode and (ledger_after is None or ledger_after != ledger_before):
            return unavailable
        selfhost_after = (None if managed_mode or google_path is None else SELFHOST_SOURCE_AUTHORITY.generations(
            google_path,
            Path(DATA) / 'connectors/granola.json'))
        if not managed_mode and selfhost_after != selfhost_before:
            return unavailable
        google_after = (google_source_authority.state(DATA, google_path, managed_mode=managed_mode,
            tenant=os.environ.get('SOTTO_TENANT_ID', ''), managed_ledger=ledger_after)
            if explicit_google_root and os.environ.get('SOTTO_HOME', '').strip() == explicit_google_root else None)
        if explicit_google_root and google_after is None:
            return unavailable
        calendar_after = _load_shared_lib('google_calendar_read').authority_selection(DATA, generation=google_after['generation'] if google_after else 'unavailable')
        if calendar_after != calendar_before:
            return unavailable
        # Refreshing only the access token/expiry does not change consent. Other
        # credential-state consumers retain the full body digest; compare grants here.
        grant_before = {key: value for key, value in google_before.items() if key != 'body_digest'} if google_before else None
        grant_after = {key: value for key, value in google_after.items() if key != 'body_digest'} if google_after else None
        if grant_after != grant_before:
            return unavailable
        granola_after = SELFHOST_SOURCE_AUTHORITY.granola_generation(granola_path)
        if granola_after != granola_before:
            return unavailable
        corrections_after = _source_corrections().revision(DATA)
        if corrections_after != corrections_before:
            return unavailable
        bridge_usable = managed_mode or selfhost_before is not None
        seen_key = (str(DATA), managed_mode, os.environ.get('SOTTO_TENANT_ID', '') if managed_mode else '')
        if policy and bridge_usable:
            _BRIDGE_CONSENT_SEEN[seen_key] = policy
        consent = policy or _BRIDGE_CONSENT_SEEN.get(seen_key)
        permitted = set(policy['allowed_sources']) if policy and bridge_usable else set()
        consented = set(consent['allowed_sources']) if consent and bridge_usable else set()
        if managed_mode:
            permitted &= set(ledger_after['allowed_sources'])
            consented &= set(ledger_after['allowed_sources'])
        allowed = sorted(permitted)
        consented = sorted(consented)
        google_allowed = list(google_after['sources']) if google_after else []
        if calendar_after.get('needs_reselection'):
            google_allowed = [source for source in google_allowed if source != 'calendar']
        connector_allowed = (['granola'] if granola_after not in (None, SELFHOST_SOURCE_AUTHORITY.DISCONNECTED)
            and (not managed_mode or 'granola' in ledger_after['connector_sources']) else [])
        basis = ['sotto-source-authority-v2', managed_mode,
                 consent['bridge_revision'] if consent and consented else None,
                 consent['cloud_revision'] if consent and consented else None,
                 ledger_after['revision'] if managed_mode else None, consented,
                 google_after['generation'] if google_after else 'unknown', google_allowed,
                 granola_after, connector_allowed, calendar_after]
        if not managed_mode:
            basis.append(list(selfhost_after) if selfhost_after is not None else 'unknown')
        if corrections_after:
            basis.append(['source_corrections', corrections_after])
        revision = hashlib.sha256(json.dumps(basis, separators=(',', ':')).encode()).hexdigest()
        result = {'version': 2, 'revision': revision, 'complete': True,
                  'bridge_sources': allowed, 'google_sources': google_allowed, 'connector_sources': connector_allowed}
        if include_search_policy:
            # MCP status needs capability metadata; the chat authority wire contract is strict.
            result['local_search'] = local_search if policy and bridge_usable else {'status': 'unknown'}
        return result
    except Exception:  # noqa: BLE001 - unavailable policy is never an access grant
        return unavailable


def _record_source_response(request, result):
    if request.get('method') != 'tools/call' or (request.get('params') or {}).get('name') not in ('health', 'read_local'):
        return
    from textutil import unwrap_tool_result
    if result.get('isError'):
        return
    unwrapped = unwrap_tool_result(result)
    if not isinstance(unwrapped, dict) or not any(
            isinstance(unwrapped.get(key), dict) for key in ('source_status', 'sources')):
        return
    # `unwrap_tool_result` may legitimately return `result` itself for a raw payload. Detach the
    # payload before adding MCP wrapper fields below, or structuredContent would point back to its
    # own parent and make the response impossible to JSON-encode.
    payload = dict(unwrapped)
    marker_key = _relay_mod.BRIDGE_REQUEST_STARTED_AT
    marker = request.get(marker_key)
    if isinstance(marker, str) and marker:
        payload[marker_key] = marker
    else:
        # A marker supplied by the Bridge has no ordering authority.
        payload.pop(marker_key, None)

    # Keep MCP's two payload representations identical. Downstream adapters may unwrap either
    # lane, and a later recorder must retain the original server-authored request start.
    result['structuredContent'] = payload
    encoded = json.dumps(payload)
    content = result.get('content')
    if not isinstance(content, list):
        content = []
        result['content'] = content
    text_entry = next((entry for entry in content
                       if isinstance(entry, dict) and entry.get('type') == 'text'), None)
    if text_entry is None:
        content.append({'type': 'text', 'text': encoded})
    else:
        text_entry['text'] = encoded
    _source_context().record_bridge_status(payload, data_root=DATA)


RELAY.on_response = _record_source_response
# Request gate FIRST: a tool managed mode denies is refused here and is
# never forwarded to the Mac. The response gate re-checks the same decision plus the payload.
RELAY.validate_request = lambda request: __import__('managed').validate_tool_request(DATA, request)
RELAY.filter_tools = lambda result: __import__('managed').filter_tool_list(DATA, result)
RELAY.validate_response = lambda request, result: __import__('managed').validate_tool_response(
    DATA, request, result)


# Remote-MCP service connectors (OAuth 2.1 DCR + PKCE) — the generic "Connect a service" lane.
# CONNECTORS.SERVICES drives the /setup tiles; tokens land at $SOTTO_DATA/connectors/<service>.json.
_conn_spec = importlib.util.spec_from_file_location(
    "connectors", os.path.join(os.path.dirname(__file__), "connectors.py"))
CONNECTORS = importlib.util.module_from_spec(_conn_spec)
_conn_spec.loader.exec_module(CONNECTORS)

# The Window (M1): session-gated read-only web dashboard — /app, /app/login, /static/*, /api/*.
# All the session/CSRF/CSP/lockout/API logic lives in dashboard.py; the HOOKS lambdas late-bind
# THIS module's globals, so monkeypatched DATA/google_connected/… are seen by the dashboard too.
_dash_spec = importlib.util.spec_from_file_location(
    "dashboard", os.path.join(os.path.dirname(__file__), "dashboard.py"))
DASHBOARD = importlib.util.module_from_spec(_dash_spec)
_dash_spec.loader.exec_module(DASHBOARD)
DASHBOARD.HOOKS.update({
    "data_root": lambda: DATA,
    "project_person": lambda raw: _project_dashboard_person(raw),
    "setup_code": lambda: resolve_setup_code(),
    "bridge_connected": lambda: RELAY.bridge_connected(),
    "last_event_at": lambda: _last_event_at(),
    "google_ok": lambda: google_connected()[0],
    "calendar_settings": lambda value=None: _google_calendar_settings(value),
    "connector_status": lambda: CONNECTORS.service_status(),
    "connector_error": lambda s: _connector_error(s),
    "connector_has_refresh": lambda s: _connector_has_refresh(s),
    "source_allowed": lambda s: _source_context().allowed(s),
    "source_health": lambda: _source_context().source_health(DATA),
    "runtime_observation": lambda: _runtime_adapter().runtime_observation(),
    "client_mcp_diagnostics": lambda: CLIENT_MCP.diagnostics(),
    "feedback": lambda: _runtime_adapter().feedback_status(),
    "personal_routines_supported": lambda: _runtime_adapter().personal_routines_supported(),
    # M2 writes: dashboard.py shells out to the skills tree's knowledge_edit.py, located with the
    # same discovery run_triage uses (late-bound so test monkeypatches on _find_sotto_script land).
    "find_script": lambda *rel: _find_sotto_script(*rel),
    "delivery_effects": lambda: _shared_effects(),
    "permitted_loop_rows": lambda: _dashboard_loop_rows(),
    "relationship_importance": lambda contact, history, now: _load_shared_lib(
        "relationship_importance").for_contact(contact, history, [], now),
    # THE atomic JSON write (connectors.write_json). dashboard.py/calcache.py don't import
    # connectors, so it arrives the way every other cross-module call here does — a HOOKS lambda.
    "write_json": lambda p, o, mode=0o600, indent=None: CONNECTORS.write_json(p, o, mode, indent),
    # The Cadence panel's write half: "nudge me now" on a held item runs the funnel's OWN
    # promotion (triage_event.py --promote) and then takes the identical stage → spawn path the
    # release valve and a fresh agent verdict take. The dashboard never spawns anything itself.
    "promote_queued": lambda key: _promote_queued(key),
    # "Run it now" on Briefs: the same prompt crons.json holds, fired through the same runner.
    "run_job": lambda name: _run_dashboard_job(name),
    "job_names": lambda: [j[0] for j in _sotto_cron_jobs()],
    "personal_routines": lambda: _personal_routines(),
    # Delivery honesty for the Cadence panel — the channel and whether it's live right now.
    "delivery_channel": lambda: _deliver_target(),
    "delivery_ready": lambda: _delivery_ready(),
    "channel_status": lambda: _channel_status(),
    # "A newer Sotto is published" — the freshness-gated half of the daily check (update_notice),
    # so /api/overview can carry the Today banner without a second checker or a second cache.
    "update_notice": lambda: update_notice(),
})

# The ONE calendar cache (ROADMAP Step 2 item 2 + its post-audit amendment: "two competing caches is
# how drift starts"). calcache.py owns the gather_google --skip-gmail fork, the 10-minute TTL and
# the refresh thread that writes cache/calendar_today.json for the triage in-meeting hold; the
# dashboard's /api/calendar is now a VIEW over the same snapshot, wired below. `local_today` is a
# hook rather than a second implementation so the wall-clock features read the SAME resolved tz path
# the rest of the receiver does (the ROADMAP's first-night-timezone amendment).
_cal_spec = importlib.util.spec_from_file_location(
    "calcache", os.path.join(os.path.dirname(__file__), "calcache.py"))
CALCACHE = importlib.util.module_from_spec(_cal_spec)
_cal_spec.loader.exec_module(CALCACHE)
CALCACHE.HOOKS.update({
    "data_root": lambda: DATA,
    "find_script": lambda *rel: _find_sotto_script(*rel),
    "write_json": lambda p, o, mode=0o600, indent=None: CONNECTORS.write_json(p, o, mode, indent),
    "json_transaction": lambda p, **kw: CONNECTORS.json_transaction(p, **kw),
    "local_today": lambda: DASHBOARD._local_today(),
    "calendar_change_batch": lambda events: _dispatch_synthetic_batch(events, "calendar change"),
    "event_handled": lambda event: _synthetic_event_handled(event),
    # The post-meeting tap (Step 2 item 3): calcache detects the event-END on the refresh tick, the
    # receiver relays it into the ordinary triage funnel. Late-bound like the rest.
    "meeting_tap": lambda ev: _dispatch_meeting_tap(ev),
})
DASHBOARD.HOOKS["calendar_snapshot"] = lambda: CALCACHE.snapshot()
CALCACHE.HOOKS["source_revision"] = lambda: _runtime_source_authority().get("revision")

# The durable delivery outbox (ROADMAP § Reliability P0 item 2): nothing Sotto says is marked
# delivered until the channel says so; what fails waits its turn instead of dying. It owns
# events/outbox.json and every constant about retrying — this module owns only the CHANNEL
# (`_send_with_identity`) and the receipt (`_record_delivery`), handed over as hooks.
_outbox_spec = importlib.util.spec_from_file_location(
    "outbox", os.path.join(os.path.dirname(__file__), "outbox.py"))
OUTBOX = importlib.util.module_from_spec(_outbox_spec)
_outbox_spec.loader.exec_module(OUTBOX)
OUTBOX.HOOKS.update({
    "data_root": lambda: DATA,
    "json_transaction": lambda p, **kw: CONNECTORS.json_transaction(p, **kw),
    # The ONE call that touches the channel. When a channel offers a real receipt (a gateway send
    # API returning an id), this is the single function that gets stronger and every lane inherits it.
    "send": lambda body, target, dispatch_id, attempt, **context: _send_with_identity(
        body, target, dispatch_id, attempt, **context),
    "send_gallery": lambda presentation, target, dispatch_id: _send_gallery_with_current_route(
        presentation, target, dispatch_id),
    "gallery_receipt": lambda dispatch_id: _runtime_adapter().gallery_receipt(dispatch_id),
    "record": lambda label, status, detail="", usage=None, decision_ids=None, run_id="": _record_delivery(
        label, status, detail, usage=usage, decision_ids=decision_ids, run_id=run_id),
    # A chase is only counted once the message that chased actually landed — wherever it landed,
    # first try or fifth. The ack is what finalizes effects, so this rides the ack.
    "on_delivered": lambda payload: _on_delivered(payload),
    "offer_route": lambda payload: _capture_offer_route(payload),
    "valid": lambda payload: _delivery_valid(payload),
    "delivery_ready": lambda payload: _history_discovery_delivery_ready(payload),
    "on_invalid": lambda payload: _invalid_delivery(payload),
    "on_terminal": lambda row: _owner_timer_terminal(row),
    # A run that exited 0 with a non-brief body is a run that died, as far as the day is concerned:
    # the same one bounded re-fire a crash gets (review, Sep 3 — without it the cron lane on a
    # no-Mac deploy had a nicer receipt and still no brief).
    "on_not_a_brief": lambda payload: None,
    "local_today": lambda: DASHBOARD._local_today(),
    # The deliver-once gate as machinery: the send seam itself claims the day's brief marker, so a
    # run that forgot its own claim can no longer double-deliver. This side owns the marker path.
    "brief_gate": lambda label, day, run_id: _brief_delivery_gate(label, day, run_id),
    "routine_authorized": lambda payload: _routine_outbox_authorized(payload),
    "retained_proof_current": lambda proof: _retained_proof_current(proof),
})
DASHBOARD.HOOKS["outbox_counts"] = lambda: OUTBOX.counts()

# The retention sweep (external review finding #5: the volume accumulated exhaust with no automatic
# expiry). It owns THE table of what the volume keeps and for how long; this module owns only the
# CLOCK — _retention_tick below, on the cron thread. Loaded like the outbox, wired like the outbox.
_ret_spec = importlib.util.spec_from_file_location(
    "retention", os.path.join(os.path.dirname(__file__), "retention.py"))
RETENTION = importlib.util.module_from_spec(_ret_spec)
_ret_spec.loader.exec_module(RETENTION)
_runtime_ret_spec = importlib.util.spec_from_file_location(
    "runtime_retention", os.path.join(os.path.dirname(__file__), "runtime_retention.py"))
RUNTIME_RETENTION = importlib.util.module_from_spec(_runtime_ret_spec)
_runtime_ret_spec.loader.exec_module(RUNTIME_RETENTION)
RETENTION.HOOKS.update({
    "data_root": lambda: DATA,
    "write_text": lambda p, t, mode=0o600: CONNECTORS.write_text(p, t, mode),
    "jsonl_lock": lambda p: CONNECTORS.file_lock(p),
    "runtime_prune": lambda cutoffs: _runtime_retention_prune(cutoffs),
})


def _runtime_retention_prune(cutoffs: dict):
    return RUNTIME_RETENTION.prune(
        os.environ.get('SOTTO_CHAT_RUNTIME_URL', ''),
        os.environ.get('SOTTO_CHAT_RUNTIME_TOKEN', ''), cutoffs)


def delivered_flag(date: str, kind: str) -> str:
    # The TRIGGER-dedup claim (prevents two near-simultaneous triggers double-enqueuing). Distinct from
    # the brief skill's `.delivered` marker (delivered_marker below), which is the deliver-once gate the
    # cron and wake-push share — so a `.claim` here never makes the skill think it already delivered.
    return os.path.join(DATA, "briefs", f"{date}.{kind}.claim")


def delivered_marker(date: str, kind: str) -> str:
    """brief_marker.py's deliver-once marker for one day's morning|evening brief. THE spelling of
    that path on this side — the trigger's fold check, the stale-claim check and the send seam's
    gate all read it from here, so the format has one writer (brief_marker.py) and one reader."""
    return os.path.join(DATA, "briefs", f"{date}.{kind}.delivered")


# Which delivery labels the deliver-once marker governs, and under which of its two kinds. The
# label's last segment is the skill, so `brief:sotto-evening-brief` (the wake-push lane),
# `cron:sotto-evening-brief` (the receiver's own scheduler) and `run-now:sotto-evening-brief` (the
# dashboard button) are the same brief and share one marker — which is what lets a lane be named
# honestly in the Record without inventing a second gate.
# The weekly pulse and the midday digest have no marker and are never gated.
MARKED_BRIEF_KINDS = {"sotto-morning-brief": "morning", "sotto-evening-brief": "evening",
                      "sotto-welcome-brief": "welcome"}


def _advance_digest_stamp(coverage_until=None) -> bool:
    """The deliver-once claim's second half: move the midday-digest window to now, so the 12:30
    digest never re-surfaces what the brief just covered. digest_check.advance_stamp stays the ONE
    owner of that stamp (forward-only); it is loaded from the skills tree like every other
    cross-tree script. Best-effort — a missing tree costs the stamp, never the send."""
    try:
        path = _find_sotto_script("event-triage", "scripts", "digest_check.py")
        if not path:
            return False
        directory = os.path.dirname(path)
        if directory not in sys.path:
            sys.path.insert(0, directory)
        spec = importlib.util.spec_from_file_location("digest_check", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        covered = (datetime.fromisoformat(str(coverage_until).replace('Z', '+00:00'))
                   if coverage_until else datetime.now(timezone.utc))
        mod.advance_stamp(covered)
        return True
    except Exception:
        return False


# How recently compose_brief.py must have archived a brief of this kind for a brief-kind row to
# count as composed. The two briefs are 24 h apart, and a same-day retry never trails its compose
# by more than ~18 h (a 06:30 morning brief retried until the day ends), so 20 h separates "today's
# compose" from "yesterday's" without either side's date.
COMPOSED_BRIEF_MAX_AGE_SECS = 20 * 3600


def _composed_brief_recently(kind: str, day: str) -> bool:
    """Did compose_brief.py archive a <kind> brief within COMPOSED_BRIEF_MAX_AGE_SECS?

    Checked by MTIME across the row's day and its neighbours, never by matching the row's date to
    the archive's: the archive is dated by the composer's timezone chain inside the agent sandbox
    and the row by the receiver's, and a deploy whose sandbox strips SOTTO_TIMEZONE would date an
    evening brief tomorrow (17:30 PT is 00:30 UTC) — a strict equality then refused every evening
    brief (review, Sep 3). An unreadable briefs/ answers True: fail-open, like the marker."""
    return _brief_file_recent(f"{{d}}_{kind}.json", day)


def _brief_file_recent(name: str, day: str) -> bool:
    """Is briefs/<name with {d} = the row's day or a neighbour> younger than
    COMPOSED_BRIEF_MAX_AGE_SECS? The one mtime check behind both the composed-archive gate and
    the Learn-receipt check. Fail-open on an unreadable directory."""
    try:
        base = datetime.strptime(day, "%Y-%m-%d")
        days = [(base + timedelta(days=d)).strftime("%Y-%m-%d") for d in (-1, 0, 1)]
    except (TypeError, ValueError):
        days = [day]
    now = time.time()
    for d in days:
        path = os.path.join(DATA, "briefs", name.format(d=d))
        try:
            if now - os.path.getmtime(path) < COMPOSED_BRIEF_MAX_AGE_SECS:
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def _learn_receipt(kind: str, day: str):
    """The run's Learn receipt — briefs/<day>.<kind>.learned.json, written by learn_step.py — when
    one is recent (same window and neighbour-day tolerance as the composed-archive check), else
    None. Returns the parsed receipt (or {} when it exists but is unreadable) so the caller can say
    which writers failed, not only whether the step ran."""
    if not _brief_file_recent(f"{{d}}.{kind}.learned.json", day):
        return None
    try:
        base = datetime.strptime(day, "%Y-%m-%d")
        days = [(base + timedelta(days=d)).strftime("%Y-%m-%d") for d in (0, -1, 1)]
    except (TypeError, ValueError):
        days = [day]
    for d in days:
        try:
            with open(os.path.join(DATA, "briefs", f"{d}.{kind}.learned.json"), encoding="utf-8") as f:
                receipt = json.load(f)
            return receipt if isinstance(receipt, dict) else {}
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return {}
    return {}


def _on_delivered(payload: dict) -> bool:
    """The channel ACKED this row. Two follow-ups, both only now: the run's chase/hand-off effects
    (a chase is counted when the message that chased actually landed), and — for a brief — the
    midday-digest window, which starts at the brief the user RECEIVED. Advancing it at the claim
    hid the whole morning from the 12:30 digest whenever the send then failed and retried into
    the afternoon (Day-0 simulation, Sep 2026: an unlinked Telegram, a brief that composed at
    17:30 and never landed, and a digest window that started there anyway).

    A brief also has to have LEARNED: the skill's Learn step is one command (learn_step.py) that
    leaves a receipt, and a delivered brief with no receipt is logged loudly here — the graph, the
    ledger, the rules and the voice all rest on that step, and a run that skipped it used to leave
    every page green while the memory went a day colder."""
    effects = payload.get('effects') or []
    receipt = payload.get('receipt') or {}
    if _requires_offer_route(payload):
        captured = payload.get('offer_route_at_contact')
        if (not isinstance(captured, dict)
                or not isinstance(captured.get('chat_id'), str) or not captured['chat_id']
                or not isinstance(captured.get('route_digest'), str)
                or re.fullmatch(r'[a-f0-9]{64}', captured['route_digest']) is None
                or type(receipt.get('route_proof_version')) is not int
                or receipt['route_proof_version'] != 1
                or receipt.get('route_digest') != captured['route_digest']):
            return False
        # Only the protected route captured BEFORE contact may bind this accepted offer.
        # Provider fields and a newly selected route after delivery are not authority.
        receipt = {**receipt, 'chat_id': captured['chat_id']}
    finalized = _finalize_delivery_effects(effects)
    finalized = (not effects or _shared_effects().finalize(effects, receipt)) and finalized
    for effect in effects:
        if effect.get('kind') == 'digest_stamp':
            finalized = _advance_digest_stamp(effect.get('coverage_until')) and finalized
        elif effect.get('kind') == 'digest_accept':
            try:
                path = _find_sotto_script("event-triage", "scripts", "digest_check.py")
                spec = importlib.util.spec_from_file_location("digest_check_accept", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                finalized = mod.accept_items(effect.get('item_ids') or []) and finalized
            except Exception:  # noqa: BLE001 — an unrecorded acceptance must remain retryable
                finalized = False
    label = str(payload.get("label") or "")
    kind = MARKED_BRIEF_KINDS.get(label.rsplit(":", 1)[-1].strip())
    if kind and not REQUESTED_BRIEF.LABEL.fullmatch(label):
        if kind == 'welcome':
            import onboarding
            if label == onboarding.DISCOVERY_LABEL:
                onboarding.discovery_delivered(DATA)
            else:
                onboarding.delivered(DATA)
        else:
            finalized = _advance_digest_stamp(payload.get('coverage_until')) and finalized
        day = _local_now().strftime("%Y-%m-%d")
        receipt = _learn_receipt(kind, day)
        if receipt is None:
            print(f"[sotto] {label}: delivered with NO Learn receipt "
                  f"(briefs/{day}.{kind}.learned.json) — the run skipped learn_step.py, so nothing "
                  "it saw reached the graph, the ledger, the rules or the voice", flush=True)
        else:
            failed = sorted(name for name, step in (receipt.get("steps") or {}).items()
                            if isinstance(step, dict) and step.get("status") == "failed")
            if failed:
                print(f"[sotto] {label}: delivered, but its Learn step reports "
                      f"{len(failed)} writer(s) failed: {', '.join(failed)}", flush=True)

    return bool(finalized)


@lru_cache(maxsize=1)
def _discovery_interruption_policy():
    path = _find_sotto_script('event-triage', 'scripts', 'triage_event.py')
    if not path:
        raise RuntimeError('interruption policy unavailable')
    spec = importlib.util.spec_from_file_location('discovery_triage_policy', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _history_discovery_delivery_ready(payload):
    import onboarding
    reminder_notice = payload.get('label', '').startswith('owner-reminder-status:')
    if payload.get('label') != onboarding.DISCOVERY_LABEL and not reminder_notice:
        return True
    policy = _discovery_interruption_policy()
    now = _local_now()
    explicit = policy._prefs.load_explicit()
    if (policy._in_quiet_hours(now)
            or policy._prefs.snooze_active(now_local=now, explicit=explicit)
            or policy._prefs.effective_nudge_budget(explicit) <= 0):
        return False
    if reminder_notice:
        return True  # status of an explicitly requested timer; quiet hours still apply
    # The same daily interrupt budget as every proactive send. The admission check only asks;
    # the send seam spends one unit under the run's stable reservation, so retries ride it free.
    day = policy._local_day(now)
    run_id = str(payload.get('run_id') or '')
    if not run_id:
        return policy._budget_left(day) > 0
    return policy._budget_try_spend(day, 1, reservation='history-discovery:' + run_id)


def _brief_delivery_gate(label: str, day: str, run_id: str) -> str:
    """The marker gate below; a history discovery also records its lifetime send claim.

    The discovery's day marker only fences one day. Before any provider contact the claim is
    written to the onboarding record (never pruned), so an accepted send whose delivery callback
    failed still blocks every later discovery after the outbox row ages out. Failing to record it
    holds the row rather than sending unrecorded.
    """
    verdict = _brief_marker_gate(label, day, run_id)
    import onboarding
    if label != onboarding.DISCOVERY_LABEL or verdict != OUTBOX.GATE_SEND:
        return verdict
    try:
        claimed = onboarding.discovery_claimed(DATA, run_id or '', day or DASHBOARD._local_today())
    except Exception as error:  # noqa: BLE001 — an unrecorded claim must not reach the provider
        print(f'[sotto] discovery claim not recorded ({type(error).__name__})', flush=True)
        return OUTBOX.GATE_RETRY
    return OUTBOX.GATE_SEND if claimed else OUTBOX.GATE_SUPERSEDED


def _brief_marker_gate(label: str, day: str, run_id: str) -> str:
    """"send" | "superseded" | "not_a_brief" — does THIS run still own today's brief, at the
    moment of sending, and is what it is about to send a brief at all?

    THE BUG THIS EXISTS FOR (Aug 30, 2026, fourth instruction-adherence failure of the week): the
    17:30 cron run claimed `briefs/<day>.evening.delivered` at 17:34 and delivered itself; the
    wake-push run spawned at 17:31 sent its own composition at 17:35 anyway, because the claim was
    an INSTRUCTION in the skill's step 6 and the run skipped it. Deliver-once must therefore be
    machinery at the seam where words actually leave the box, never a prompt.

    So: no marker → this row creates it atomically (O_EXCL) with its own run id and sends; the
    marker already carries this run id → the run claimed properly, send exactly as before; the
    marker carries anything else (another run's id, or the empty/`unlabeled` content of a claimer
    that could not name itself) → another lane owns today's brief and this copy is superseded.
    Fail-open on an unwritable or unreadable volume: a rare duplicate beats a silenced brief, which
    is the same posture brief_marker.claim already takes."""
    kind = MARKED_BRIEF_KINDS.get((label or "").rsplit(":", 1)[-1].strip(), "")
    if not kind:
        return OUTBOX.GATE_SEND
    if REQUESTED_BRIEF.LABEL.fullmatch(label or ""):
        return OUTBOX.GATE_SEND  # Explicit owner request; outbox run identity still deduplicates.
    today = day or DASHBOARD._local_today()
    import onboarding
    marker = delivered_marker(today, 'history-discovery' if label == onboarding.DISCOVERY_LABEL else kind)
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
    except OSError:
        return OUTBOX.GATE_SEND
    # IS THIS A BRIEF? compose_brief.py archives what it composed to briefs/<day>_<kind>.json before
    # it prints a word, so a brief-kind body with no archive behind it was never composed: an
    # improvised recap, or the run's one-line "I could not run the composer". Sep 2: exactly that
    # claimed the day (calls=0, no archive) and the lane that would have sent the real brief stood
    # down. Refuse to claim, receipt it failed, leave the day open for the next lane.
    archive_kind = 'history-discovery' if label == onboarding.DISCOVERY_LABEL else kind
    if not _composed_brief_recently(archive_kind, today):
        return OUTBOX.GATE_NOT_A_BRIEF
    try:
        with CONNECTORS.file_lock(marker):
            return _claim_brief_marker(marker, run_id)
    except TimeoutError:
        # Somebody else is holding the marker's lock right now — the one situation the gate
        # exists for. Sending anyway would answer "who owns today?" with a guess; hold the row
        # for the next pass instead (TimeoutError is an OSError, so it must be caught first).
        return OUTBOX.GATE_RETRY
    except OSError:
        return OUTBOX.GATE_SEND


def _claim_brief_marker(marker, run_id):
    """Marker creation and owner comparison; caller holds its cross-process file lock."""
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(run_id or "")
        # THE claim now happens here, at the send (receiver-spawned runs no longer write the marker
        # skill-side — a claim before the outbox row exists left a crash window where the day read
        # as delivered with nothing queued). The claim's second half — the digest window — does NOT
        # move here: it moves when the channel ACKS (_on_delivered), because a claim whose send
        # then fails for a day of retries must not hide the morning from the 12:30 digest.
        return OUTBOX.GATE_SEND
    except FileExistsError:
        pass
    except OSError:
        return OUTBOX.GATE_SEND
    try:
        with open(marker, encoding="utf-8") as f:
            owner = f.read().strip()
    except OSError:
        return OUTBOX.GATE_SEND
    return OUTBOX.GATE_SEND if owner and owner == (run_id or "") else OUTBOX.GATE_SUPERSEDED


# ── Spawning a procedure, and actually DELIVERING what it says ──────────────────────────────────
# THE BUG THIS EXISTS FOR (Aug 2026, found from a real Record): spawned runs used to be
# fire-and-forget, and the stdout of a detached Popen goes nowhere. So the funnel would classify an
# email as a real ask, record "Nudged you" in The Record, spawn the skill — and the user would never
# receive it. Now every run's final text is captured and handed to the runtime adapter's send for
# the home channel SOTTO_CRON_DELIVER names, and the receiver RECORDS WHETHER IT LANDED. The receipt
# is the point as much as the fix — The Record said "Nudged you" for weeks about messages that went
# to /dev/null, because deciding to nudge and delivering a nudge were the same fact. They are now two.
SEND_TIMEOUT_SECS = 60           # one loopback send to the chat runtime, not an agent turn
ONESHOT_TIMEOUT_SECS = 15 * 60   # a brief runs the whole pipeline; generous, but never unbounded

# The silence sentinel. "Say nothing and end the turn" is an instruction models reliably ignore —
# they hate ending with zero text, so a no-nudge proactive run closed with "All clear — nothing
# urgent" and, once the send seam was fixed (Aug 2026), three of those landed in the owner's
# Telegram in one evening. So silence is now a TOKEN the model can emit: a spawned run with
# nothing to deliver replies exactly NO_NUDGES, and the seam records it as an empty run instead
# of sending. Deterministic at the seam, one sentence to explain.
SILENCE_SENTINEL = "NO_NUDGES"


def _is_silence(body: str) -> bool:
    """True iff the run's final text is the sentinel — tolerating the markdown/punctuation wrappers
    a chatty model puts around a bare token (*NO_NUDGES*, `no_nudges.`), but NEVER a sentence that
    merely contains it (that's a real message, deliver it)."""
    return body.strip().strip("*_`'\".!,() \n\t").upper() == SILENCE_SENTINEL


def _deliver_target() -> str:
    """The home channel every lane delivers to, so a nudge and a brief can never land in different
    places. The supervisor always exports SOTTO_CRON_DELIVER (photon or telegram); a process started
    without it reads as Telegram, whose own link probe then holds delivery until it is linked."""
    return (os.environ.get("SOTTO_CRON_DELIVER") or "").strip() or "telegram"


def _record_delivery(label: str, status: str, detail: str = "", usage: dict | None = None,
                     decision_ids: list | None = None, run_id: str = "", once: bool = False) -> None:
    """One line per spawned skill, in $SOTTO_DATA/events/delivery.jsonl — the receiver is its ONLY
    writer, and the dashboard's Record reads it beside the triage verdicts. `status` is one of
    spawned / delivered / empty / failed / skipped (nothing spawned — e.g. a wake trigger after the
    day's brief already went out) / expired (it aged past its kind's window in the outbox and was
    never sent) / superseded (a composed brief the other lane had already delivered; the send seam
    refused it). Since the outbox, a `failed` row says in its detail whether the failure is final
    ("gave up after N attempts") or the latest of several ("queued, retry 2/96") — so the Record
    can never read one attempt's failure as the message being gone.
    `usage` is the run's ground-truth spend (see _read_usage),
    present only on the row that closes a run. Best-effort: a receipt that can't be written must
    never cost the delivery it is describing."""
    try:
        import managed
        if managed.enabled() and label == managed.NOTICE_LABEL and status == 'delivered':
            with CONNECTORS.json_transaction(os.path.join(DATA, 'config', 'managed-status.json'), default={}) as state:
                state.update(tenant_id=os.environ['SOTTO_TENANT_ID'], no_sources_sent=True)
        os.makedirs(_events_dir(), exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "label": label, "status": status, "target": _deliver_target()}
        if detail:
            row["detail"] = detail[:300]
        if usage:
            row["usage"] = usage
        # Durable-work IDs are the same 32-hex identity exported to model_work as run_id. Keep only
        # that fixed shape: arbitrary labels/details must never become an accidental join key.
        if re.fullmatch(r"[0-9a-f]{32}", str(run_id or "")):
            row["run_id"] = run_id
        ids = [str(v) for v in (decision_ids or []) if str(v).strip()]
        if ids:
            row["decision_ids"] = ids[:20]
        path = os.path.join(_events_dir(), "delivery.jsonl")
        with CONNECTORS.file_lock(path):   # the retention sweep rewrites this ledger under the same lock
            if once:
                # A withheld legacy notice keeps its original queue evidence. Repeated
                # reconciliations (including restart) need only one bounded journal note.
                key = ('label', 'status', 'detail', 'run_id')
                try:
                    with open(path, encoding='utf-8') as prior:
                        for line in prior:
                            try:
                                saved = json.loads(line)
                            except ValueError:
                                continue
                            if (isinstance(saved, dict)
                                    and all(saved.get(field) == row.get(field) for field in key)):
                                return
                except FileNotFoundError:
                    pass
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001
        pass


# Machine markers (<!--id:…-->, <!--meeting:…-->) are dashboard/tap-link plumbing the composer
# strips when it renders the chat artifact (chatfmt.to_chat) — but the spawned run CHOOSES which
# artifact it prints, and one runtime upgrade was enough for a run to print the marker-laden
# markdown instead (Aug 28: an evening brief arrived with every id inline). The send seam now
# enforces what the composer intends: nothing shaped like a comment marker ever leaves the box,
# whichever artifact a run printed. Vendored from chatfmt._MARKER_RE, its owner — test_docs_drift
# pins the two patterns byte-identical, the same contract keys.py lives under.
_MARKER_RE = re.compile(r"<!--.*?-->", re.S)

# Email asks, every other channel links — one rule, stated in every skill that hands a draft over,
# and on Sep 4, 2026 a nudge still arrived with BOTH the ask and a 200-character percent-encoded
# `mailto:` on a "Tap to send:" line, which Telegram does not linkify (a mailto with a query is
# plain text there) and which on a phone opens whatever mail app the OS picks. Every run this
# process delivers is unattended, and an unattended run's email path is the Gmail-draft offer the
# user answers in chat — so a `mailto:` has no legitimate way to reach the channel from here. The
# seam removes it: a markdown link keeps its label, a bare URL disappears, and a line that was only
# the URL and its "tap to send" prefix goes with it.
_MAILTO_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*mailto:[^)]*\)")
_MAILTO_URL_RE = re.compile(r"<?mailto:[^\s<>\]\)]+>?")
_MAILTO_STUB_RE = re.compile(r"[\W_]*(?:tap\s+(?:to\s+send|here)|send|reply)?[\W_]*", re.I)
# Interactive replies and scheduled messages use the same recipient rules.
MESSAGE_TARGETS = _load_shared_lib('message_targets')


def _strip_mailto(text: str) -> str:
    """Remove every `mailto:` link, and every other tap link whose recipient message_targets does
    not validate (phone 7-15 digits, a Messages short code, or an Apple ID email) — keeping its
    label text if it had one — from a message body, and any line that was nothing but one."""
    text = MESSAGE_TARGETS.strip_invalid_links(text)
    if "mailto:" not in text:
        return text
    kept = []
    for line in text.split("\n"):
        rest = line
        if "mailto:" in rest:
            rest = _MAILTO_URL_RE.sub("", _MAILTO_MD_LINK_RE.sub(r"\1", rest))
        if rest == line:
            kept.append(line)
            continue
        if _MAILTO_STUB_RE.fullmatch(rest):
            continue
        kept.append(rest.rstrip())
    return "\n".join(kept)


def _send_with_identity(body: str, target: str, dispatch_id: str, attempt: int, *, source_revision=None):
    """Transport acceptance comes from the same adapter in Cloud and self-host."""
    if (os.environ.get('SOTTO_RUNTIME_PROFILE') in (
            'self-host-telegram-fixed-five-v1', 'self-host-photon-fixed-five-v1')
            and _candidate_retained_route_digest() is None):
        return False, 'Owner route unavailable', {'acceptance': 'not_attempted'}
    return _runtime_adapter().send_receipt(body, target, SEND_TIMEOUT_SECS, dispatch_id, attempt, **({'source_revision': source_revision} if source_revision is not None else {}))


def _send_gallery_with_current_route(presentation, target: str, dispatch_id: str):
    if (os.environ.get('SOTTO_RUNTIME_PROFILE') in (
            'self-host-telegram-fixed-five-v1', 'self-host-photon-fixed-five-v1')
            and _candidate_retained_route_digest() is None):
        return False, 'Owner route unavailable', {'acceptance': 'not_attempted'}
    return _runtime_adapter().send_gallery(presentation, target, dispatch_id, SEND_TIMEOUT_SECS)


def _release_invalid_brief_claim(payload):
    """Release only our invalidated, demonstrably unaccepted claim; never infer from absence of ACK."""
    label = payload.get('label') or ''
    name = label.rsplit(':', 1)[-1]
    kind = MARKED_BRIEF_KINDS.get(name)
    run_id, day = payload.get('run_id'), payload.get('day')
    if not kind or not run_id or not day:
        return 'open'  # old pre-claim invalidation payloads carry no reservation to release
    import onboarding
    marker = delivered_marker(day, 'history-discovery' if label == onboarding.DISCOVERY_LABEL else kind)
    with CONNECTORS.file_lock(marker):
        try:
            owner = Path(marker).read_text().strip()
        except FileNotFoundError:
            return 'open'  # idempotent retry after release but before replacement enqueue
        if owner != run_id:
            return 'other_owner'
        if not OUTBOX.invalidated_claim(run_id, day, label):
            return 'uncertain'
        os.unlink(marker)
    _CRON_FIRED.pop(name, None)
    return 'open'


def _invalid_delivery(payload):
    label = payload.get('label') or ''
    name = label.rsplit(':', 1)[-1]
    claim = _release_invalid_brief_claim(payload)
    if claim == 'other_owner':
        return True  # another accepted or active run owns this day's brief
    if claim == 'uncertain':
        _record_delivery(label, 'failed', 'Brief changed after a send with unknown acceptance; '
                         'its delivery reservation was preserved to avoid a second brief.',
                         run_id=payload.get('run_id') or '')
        return True
    if payload.get('valid_until') and time.time() >= payload['valid_until']:
        return True
    if name in MARKED_BRIEF_KINDS and label.startswith('cron:'):
        import managed
        if managed.brief_hold(DATA):
            return True  # no connected sources means no scheduled brief is owed
        # Invalidation can run synchronously inside a saved result's first outbox handoff.
        # Wait for that worker to finish: admission otherwise sees its lease and reports success
        # without enqueuing a replacement. The outbox retains and retries this effect.
        active = WORK_QUEUE.active_job(DATA, 'run', label)
        if active is not None and active['id'] == payload.get('run_id'):
            return False
        try:
            if _validated_invalid_brief_recovery(payload, label):
                return _fire_cron_job(name, label, _invalid_recovery=True,
                                      _fixed_recovery_from=payload.get('run_id')).get('ok') is True
            return _fire_cron_job(name, label).get('ok') is True
        except RuntimeError as error:
            if _is_terminal_work(error):
                return True  # the slot's budget is spent; nothing is left to recompose from
            raise
    return True


def _validated_invalid_brief_recovery(payload, label):
    """Only an exact, unaccepted old handoff can resume through first-use setup gating."""
    run_id, day = payload.get('run_id'), payload.get('day')
    if not run_id or day != str(_local_now().date()):
        return False
    if not OUTBOX.invalidated_claim(run_id, day, label):
        return False
    job = WORK_QUEUE.get(DATA, run_id)
    # A completed handoff deliberately scrubs its work payload and result. The outbox retains
    # the exact label/day/claim and rejection evidence until this effect completes.
    return bool(job and job['kind'] == 'run' and job['status'] == 'done')


def _owner_timer_authority():
    """Reconstruct the existing nonsecret capability identity, never an ambient send grant."""
    loader = _load_shared_lib('runtime_channel_proof')._startup_module()
    path = Path(DATA) / 'runtime' / 'expected-channel.json'
    expected = loader.read_receiver_channel_expectation(path)
    proof = _candidate_retained_route_proof()
    if not proof or loader.read_receiver_channel_expectation(path) != expected:
        raise ValueError('timer route unavailable')
    route = expected['route']
    identity = {'tenant_id': expected['tenantId'], 'channel': expected['channel'],
        'account_id': expected['owner']['accountId'], 'owner_id': expected['owner']['ownerId'],
        'line_key': expected['lineKey'], 'target': expected['target'],
        'route_mode': route['mode'],
        'route_id': (route['providerThreadId'] if route['mode'] == 'fixed' else
                     route['expectedProviderLine'] if route['mode'] == 'first_contact' else 'shared_project')}
    fingerprint = 'bot' if expected['channel'] == 'telegram' else 'project'
    identity[fingerprint + '_fingerprint'] = expected[fingerprint + 'Fingerprint']
    identity = OWNER_LOOP_CAPABILITY.validate_identity(identity)
    if any(proof.get(key) != identity[key] for key in
           ('tenant_id', 'account_id', 'owner_id', 'line_key', fingerprint + '_fingerprint')):
        raise ValueError('timer identity changed')
    RUNTIME_OWNER_REMINDER.validate_authority(identity, os.environ, DATA)
    return identity, proof['provider_thread_id']


def _owner_timer_reserved(reminder_id):
    # A pre-upgrade proactive run may have contacted the provider already. Its old
    # reservation remains the owner, even if its clock expiry has passed. Never replay it
    # under a new delivery id. New scans exclude this class entirely.
    key = 'intention:' + reminder_id
    for path in (Path(DATA) / 'proactive').glob('*.json'):
        with path.open() as stream:
            state = json.load(stream)
        if key in state.get('pending', {}) or key in state.get('nudged', []):
            return True
        if any(any(n.get('key') == key for n in run.get('nudges', []))
               for run in state.get('runs', {}).values()):
            return True
    with OUTBOX.HOOKS['json_transaction'](OUTBOX.path(), default={'rows': []}) as doc:
        return any(any(e.get('kind') == 'intention' and e.get('id') == reminder_id
                       for e in (row.get('payload', {}).get('effects') or []))
                   and row.get('payload', {}).get('label') != 'owner-reminder:' + reminder_id
                   for row in OUTBOX._rows(doc))


# An explicit timer loses its purpose quickly; retry from its requested time, not enqueue time.
OWNER_REMINDER_GRACE_SECS = 15 * 60
OWNER_REMINDER_NOTICE_MAX_AGE_SECS = 24 * 60 * 60
_OWNER_REMINDER_NOTICE_LOCK = threading.Lock()


def _owner_timer_deadline(row):
    return datetime.fromisoformat(row['due'].replace('Z', '+00:00')).timestamp() + OWNER_REMINDER_GRACE_SECS


def _owner_timer_notice_deadline(row):
    return datetime.fromisoformat(row['due'].replace('Z', '+00:00')).timestamp() + OWNER_REMINDER_NOTICE_MAX_AGE_SECS


def _owner_timer_notice_ids(label):
    if not label.startswith('owner-reminder-status:'):
        return []
    ids = label.split(':', 1)[1].split(',')
    return ids if 1 <= len(ids) <= 5 and len(set(ids)) == len(ids) and all(
        re.fullmatch(r'int_[a-f0-9]{32}', ident) for ident in ids) else []


def _owner_timer_notice(row, zone=None):
    scheduled = datetime.fromisoformat(row['due'].replace('Z', '+00:00')).astimezone(zone or _local_now().tzinfo or timezone.utc)
    return f"{row['action']} (scheduled for {scheduled.strftime('%b %-d at %-I:%M %p %Z')})"


def _owner_timer_zone_name(zone):
    """A name tzchain resolves back to `zone`: its IANA key, else its fixed `+HH:MM` offset."""
    if getattr(zone, 'key', None):
        return zone.key
    minutes = int(zone.utcoffset(None).total_seconds()) // 60
    return f"{'-' if minutes < 0 else '+'}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _owner_timer_notice_body(rows, zone=None):
    if len(rows) == 1:
        return ("I couldn't deliver your reminder on time: " + _owner_timer_notice(rows[0], zone)
                + ". It has been canceled; I won't send it late.")
    return ("I couldn't deliver these reminders on time:\n"
            + '\n'.join('• ' + _owner_timer_notice(row, zone) for row in rows)
            + "\nThey have been canceled; I won't send them late.")


def _owner_timer_notices(store, identity, thread, notice_ids):
    # One receiver owns this outbox. Serialize selection with enqueue so overlapping ticks
    # cannot put a reminder in two different batches. Restart recovery uses the saved outbox.
    with _OWNER_REMINDER_NOTICE_LOCK:
        with OUTBOX.HOOKS['json_transaction'](OUTBOX.path(), default={'rows': []}) as doc:
            reserved = {ident for delivery in OUTBOX._rows(doc)
                        if not (delivery.get('status') == OUTBOX.STATUS_EXPIRED
                                and delivery.get('acceptance') in ('not_attempted', 'rejected')
                                and not delivery.get('acceptance_uncertain'))
                        for ident in _owner_timer_notice_ids(delivery.get('payload', {}).get('label', ''))}
        candidates = []
        for row in store.current(strict=True):
            if (row.get('id') not in notice_ids or row.get('id') in reserved or row.get('status') != 'canceled'
                    or not row.get('delivery_outcome') or row.get('delivery_notice_outcome')
                    or row.get('owner_cancel') or not store.owner_timer(row)):
                continue
            if time.time() >= _owner_timer_notice_deadline(row):
                store.mark_delivery_notice(row['id'], 'expired')
            elif identity is not None and RUNTIME_OWNER_REMINDER_TOOL.timer_current(row, identity, thread, delivery_notice=True):
                candidates.append(row)
        rows = sorted(candidates, key=lambda row: (row['due'], row['id']))[:5]
        if rows:
            label = 'owner-reminder-status:' + ','.join(row['id'] for row in rows)
            zone_name = _owner_timer_zone_name(_local_now().tzinfo or timezone.utc)
            _deliver_text(_owner_timer_notice_body(rows, TZCHAIN.resolve(zone_name)), label, run_id=label,
                          valid_until=min(_owner_timer_notice_deadline(row) for row in rows),
                          reminder_timezone=zone_name)


def _owner_timer_terminal(delivery):
    label = (delivery.get('payload') or {}).get('label', '')
    notice = label.startswith('owner-reminder-status:')
    if not notice and not label.startswith('owner-reminder:'):
        return True
    if (delivery.get('acceptance') in ('unknown', 'in_flight')
            or delivery.get('acceptance_uncertain') and delivery.get('acceptance') != 'accepted'):
        return False
    ident = label.split(':', 1)[1]
    store = RUNTIME_OWNER_REMINDER._writer()
    if notice:
        if delivery.get('status') == OUTBOX.STATUS_DELIVERED and not (delivery.get('receipt') or {}).get('accepted_at'):
            return False
        rows = {row['id']: row for row in store.current(strict=True)}
        for notice_id in _owner_timer_notice_ids(label):
            # Only the expired members end here; a definitely unaccepted batch can be
            # replaced by its still-useful subset on the next heartbeat. Unknowns never can.
            if (delivery['status'] == OUTBOX.STATUS_EXPIRED
                    and delivery.get('acceptance') in ('not_attempted', 'rejected')
                    and notice_id in rows and time.time() < _owner_timer_notice_deadline(rows[notice_id])):
                continue
            store.mark_delivery_notice(notice_id, delivery['status'])
        return True
    if delivery.get('status') == OUTBOX.STATUS_DELIVERED:
        accepted_at = (delivery.get('receipt') or {}).get('accepted_at')
        if not accepted_at:
            return False
        store.mark_accepted(ident, accepted_at)
    else:
        store.mark_undelivered(ident, delivery['status'])
    return True


def _owner_reminder_tick():
    """Explicit owner timers use this existing 60-second heartbeat and sole delivery outbox."""
    OUTBOX.reconcile_terminal()
    store = RUNTIME_OWNER_REMINDER._writer()
    due = [row for row in store.due() if store.owner_timer(row)]
    notices = [row for row in store.current(strict=True) if row.get('status') == 'canceled'
               and row.get('delivery_outcome') and not row.get('delivery_notice_outcome')
               and not row.get('owner_cancel') and store.owner_timer(row)]
    if not due and not notices:
        return
    # Retire only unreserved expired notices even while the owner route is unavailable.
    # The no-authority pass cannot enqueue; uncertain outbox reservations remain held.
    _owner_timer_notices(store, None, None, {row['id'] for row in notices})
    if not due and not any(time.time() < _owner_timer_notice_deadline(row) for row in notices):
        return
    identity, thread = _owner_timer_authority()
    for row in due:
        if (RUNTIME_OWNER_REMINDER_TOOL.timer_current(row, identity, thread)
                and not _owner_timer_reserved(row['id'])):
            label = 'owner-reminder:' + row['id']
            deadline = _owner_timer_deadline(row)
            _deliver_text(row['action'], label, run_id=label, valid_until=deadline, effects=[
                {'kind': 'eligibility', 'intention_id': row['id']},
                {'kind': 'intention', 'id': row['id']}])
        OUTBOX.reconcile_terminal()
    # Never enqueue inside the terminal hook: it runs under the outbox transaction.
    _owner_timer_notices(store, identity, thread, {row["id"] for row in notices})
    OUTBOX.reconcile_terminal()


def _owner_timer_delivery_valid(payload):
    store = RUNTIME_OWNER_REMINDER._writer()
    label = payload.get('label', '')
    ids = [e.get('id') for e in (payload.get('effects') or []) if e.get('kind') == 'intention']
    rows = {r.get('id'): r for r in store.current(strict=True)}
    notice = label.startswith('owner-reminder-status:')
    if notice:
        notice_ids = _owner_timer_notice_ids(label)
        selected = [rows.get(ident) for ident in notice_ids]
        if ids or not selected or any(row is None or time.time() >= _owner_timer_notice_deadline(row) for row in selected):
            return False
        identity, thread = _owner_timer_authority()
        return (payload.get('target') == identity['target']
                and payload.get('body') == _delivery_body(_owner_timer_notice_body(selected,
                    TZCHAIN.resolve(payload['reminder_timezone']) if payload.get('reminder_timezone') else None))
                and all(RUNTIME_OWNER_REMINDER_TOOL.timer_current(row, identity, thread, delivery_notice=True)
                        for row in selected))
    if label.startswith('owner-reminder:'):
        reminder_id = label.split(':', 1)[1]
        row = rows.get(reminder_id)
        if ids != [reminder_id] or row is None or time.time() >= _owner_timer_deadline(row):
            return False
        identity, thread = _owner_timer_authority()
        return (payload.get('target') == identity['target']
                and payload.get('body') == _delivery_body(row.get('action', ''))
                and RUNTIME_OWNER_REMINDER_TOOL.timer_current(row, identity, thread))
    # Also stop cached old proactive results before contact. Unknown outcomes still
    # reconcile in the outbox before this validity check and can finalize acceptance.
    return not any(store.owner_timer(rows.get(key)) for key in ids)


def _delivery_valid(payload):
    if payload.get('label', '').startswith(('owner-reminder:', 'owner-reminder-status:')):
        # Only definitive staleness may supersede the row (its terminal effect cancels the
        # reminder). A check that cannot run, e.g. the route proof is briefly unreadable,
        # raises into the outbox, which holds the row for the next pass.
        if not _owner_timer_delivery_valid(payload):
            return False
    elif any(e.get('kind') == 'intention' for e in (payload.get('effects') or [])):
        try:
            if not _owner_timer_delivery_valid(payload):
                return False
        except Exception:
            return False
    requested_job_id = payload.get('feedback_for') or payload.get('run_id')
    requested_label = payload.get('label', '')
    if (REQUESTED_BRIEF.LABEL.fullmatch(requested_label) or payload.get('feedback_for')):
        requested_job = WORK_QUEUE.get(DATA, requested_job_id) if requested_job_id else None
        if (payload.get('feedback_for') and payload.get('target') == 'telegram'
                and not _requested_telegram_job(requested_job)):
            return False  # a pruned/legacy job cannot borrow ambient Photon owner settings
        if not _requested_telegram_job_current(requested_job, label=requested_label,
                                               target=payload.get('target')):
            return False
    if (_scheduled_pulse_label(payload.get('label')) or payload.get('label') == 'onboarding:history-discovery:sotto-welcome-brief' or payload.get('label', '').startswith('meeting-prep:') or re.fullmatch(r'skill:[a-f0-9]{64}:(?:meeting-prep|ask-research|followup|relationship-pulse|proactive)', payload.get('label', ''))) and not _retained_proof_current({
            'source_revision': payload.get('prep_source_revision'),
            'route_digest': payload.get('prep_route_digest')}):
        return False
    if payload.get('feedback_for'):
        owner = os.environ.get('PHOTON_HOME_CHANNEL', '')
        if _skill_notice_payload(payload):
            if not _skill_notice_current({'owner': payload.get('feedback_owner'),
                    'chat_id': payload.get('feedback_chat_id'), 'target': payload.get('target'),
                    'route_digest': payload.get('feedback_route_digest')}):
                return False
        elif _requested_telegram_job(requested_job):
            binding = _requested_telegram_job(requested_job)
            if (payload.get('feedback_owner') != binding['owner_id']
                    or payload.get('feedback_chat_id') != binding['owner_id']):
                return False
        elif (payload.get('feedback_owner') != owner or payload.get('target') != _deliver_target()
                or not REQUESTED_BRIEF.owner_destination(
                    DATA, owner, payload.get('feedback_chat_id'), os.environ.get('SOTTO_TENANT_ID'))):
            return False
    effects = payload.get('effects') or []
    for effect in effects:
        if (effect.get('kind') == 'prep_location_update'
                and (effect.get('baseline') or {}).get('route_digest') != _candidate_retained_route_digest()):
            return False
    return not effects or _shared_effects().valid(effects, time.time())


def _shared_effects():
    cached = getattr(_shared_effects, '_module', None)
    if cached is not None:
        return cached
    # This is a runtime library, not a user-invoked skill CLI. Resolve it through the fixed shared
    # library roots so a late-bound `find_script` override for dashboard subprocesses cannot be
    # mistaken for importable Python and executed with the receiver's argv.
    before = list(sys.path)
    try:
        module = _load_shared_lib('delivery_effects')
    finally:
        # Keep sibling paths for later effect imports, exactly once, even after a failed load.
        added = [entry for entry in sys.path if entry not in before]
        sys.path[:] = list(dict.fromkeys([*added, *before]))
    _shared_effects._module = module
    return module


def _delivery_body(text: str) -> str:
    body = _strip_mailto(_MARKER_RE.sub('', text or ''))
    return re.sub(r'\n{3,}', '\n\n', body).strip()


def _candidate_retained_route_proof():
    """Current selected channel route from the receiver-owned, private projection.

    Photon requires its current ACK and receipt. Linked self-host Telegram instead
    requires its protected owner/bot link and loaded Node channel identity.
    """
    try:
        zone = _configured_tz_name()
        profile = os.environ.get('SOTTO_RUNTIME_PROFILE')
        if profile == 'self-host-photon-fixed-five-v1' and (
                os.environ.get('SOTTO_DEPLOYMENT_MODE') != 'self_host'
                or os.environ.get('SOTTO_CRON_DELIVER') != 'photon'):
            return None
        if profile == 'self-host-telegram-fixed-five-v1':
            import telegram_channel_proof
            verifier = telegram_channel_proof.check_current_channel
        else:
            verifier = _load_shared_lib('runtime_channel_proof').check_current_channel
        proof = verifier(
            chat_client=_runtime_adapter()._client(timeout=5),
            expected_channel_path=Path(DATA) / 'runtime' / 'expected-channel.json',
            receiver_data_root=DATA, receiver_policy=os.environ,
            configured_zone_name=_configured_tz_name, expected_zone=zone)
        if (proof.get('version') != 1 or proof.get('status') != 'current_channel_proved'
                or proof.get('action_authorized') is not False):
            return None
        route = proof.get('route_digest')
        return proof if isinstance(route, str) and re.fullmatch(r'[a-f0-9]{64}', route) else None
    except Exception:
        return None


def _candidate_retained_route_digest():
    proof = _candidate_retained_route_proof()
    return proof['route_digest'] if proof else None


def _requested_telegram_job(job):
    """Only receiver-owned immutable work admission identifies the Telegram brief lane."""
    try:
        admission = json.loads(job['admission']) if job and job.get('admission') else {}
        binding = admission.get('telegram_binding') if isinstance(admission, dict) else None
        return binding if isinstance(binding, dict) else None
    except (ValueError, TypeError):
        return None


def _requested_telegram_job_current(job, *, label=None, target=None):
    binding = _requested_telegram_job(job)
    stored_label = ((job or {}).get('payload') or {}).get('label', '')
    feedback = (job or {}).get('feedback') or {}
    if isinstance(feedback, str):
        try:
            feedback = json.loads(feedback)
        except ValueError:
            feedback = {}
    if not isinstance(feedback, dict):
        feedback = {}
    label = label if label and REQUESTED_BRIEF.LABEL.fullmatch(label) else stored_label or feedback.get('label', '')
    # Legacy Telegram jobs have no original binding; do not grant today's route.
    required = binding is not None or (REQUESTED_BRIEF.LABEL.fullmatch(label)
        and (target == 'telegram' or feedback.get('target') == 'telegram' or _deliver_target() == 'telegram'))
    if not required:
        return True
    try:
        return bool(binding and _deliver_target() == 'telegram'
                    and target in (None, 'telegram')
                    and binding == REQUESTED_BRIEF.telegram_binding(
                        _candidate_retained_route_proof(), _runtime_source_authority()))
    except (ValueError, TypeError):
        return False


def _skill_notice_binding(identity, thread_id, evidence):
    """Bind content-free skill feedback to the admitted owner's independently proved route."""
    proof = _candidate_retained_route_proof()
    route = proof.get('owner_route') if proof else None
    if (not isinstance(identity, dict) or identity.get('channel') != 'photon'
            or _deliver_target() != 'photon' or not isinstance(route, dict)
            or route.get('platform') != 'imessage' or not isinstance(route.get('chat_id'), str)
            or not route['chat_id'] or identity.get('owner_id') != os.environ.get('PHOTON_HOME_CHANNEL')
            or proof.get('provider_thread_id') != thread_id
            or any(proof.get(key) != identity.get(key) for key in
                   ('tenant_id', 'project_fingerprint', 'owner_id', 'account_id', 'line_key'))
            or proof.get('route_digest') != evidence.get('route_digest')):
        return None
    return {'owner': identity['owner_id'], 'chat_id': route['chat_id'], 'target': 'photon',
            'route_digest': proof['route_digest']}


def _skill_notice_bound(meta):
    """True when skill feedback carries the route digest captured at admission."""
    digest = meta.get('route_digest')
    return isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest) is not None


def _skill_notice_current(meta, proof=None):
    """Source consent can change; only the original verified owner route may get a failure notice."""
    digest = meta.get('route_digest')
    if (meta.get('owner') != os.environ.get('PHOTON_HOME_CHANNEL')
            or meta.get('target') != _deliver_target() or meta.get('target') != 'photon'
            or not _skill_notice_bound(meta)):
        return False
    if proof is None:
        proof = _candidate_retained_route_proof()
    route = proof.get('owner_route') if proof else None
    return bool(isinstance(route, dict) and route.get('platform') == 'imessage'
                and proof.get('owner_id') == meta['owner']
                and route.get('chat_id') == meta.get('chat_id')
                and proof.get('route_digest') == digest)


def _skill_notice_payload(payload):
    if 'feedback_route_digest' in payload:
        return True
    # Before route binding, enqueued skill feedback lost its label when work feedback cleared.
    # Its immutable admission still identifies the lane. Never bless that legacy owner alias
    # with today's route; accepted/unknown sends keep their existing receipt fences.
    job = WORK_QUEUE.get(DATA, payload['feedback_for'])
    try:
        admission = json.loads(job['admission']) if job else {}
        return admission['receipt']['outcome'].get('skill') in (
            'meeting-prep', 'ask-research', 'followup', 'relationship-pulse', 'proactive')
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _requires_offer_route(payload):
    return (os.environ.get('SOTTO_CRON_DELIVER') == 'photon'
            and any(isinstance(effect, dict) and effect.get('kind') == 'pending_offer'
                    for effect in (payload.get('effects') or [])))


def _capture_offer_route(payload):
    if not _requires_offer_route(payload):
        return {}
    proof = _candidate_retained_route_proof()
    route = proof.get('owner_route') if proof else None
    chat_id = route.get('chat_id') if isinstance(route, dict) else None
    if not isinstance(chat_id, str) or not chat_id:
        return None  # no provider contact without a trustworthy offer destination
    for field in ('prep_route_digest', 'routine_route_digest'):
        if payload.get(field) is not None and payload[field] != proof['route_digest']:
            return None
    return {'route_digest': proof['route_digest'], 'chat_id': chat_id}


def _retained_proof_current(proof):
    if (not isinstance(proof, dict) or not isinstance(proof.get('source_revision'), str)
            or not isinstance(proof.get('route_digest'), str)):
        return False
    try:
        route_before = _candidate_retained_route_digest()
        current = _runtime_source_authority()
        route_after = _candidate_retained_route_digest()
    except Exception:
        return False
    return (current.get('complete') is True
            and current.get('revision') == proof['source_revision']
            and route_before == route_after == proof['route_digest'])


def _scheduled_pulse_label(label):
    return label in ('cron:sotto-relationship-pulse', 'run-now:sotto-relationship-pulse')


def _composition_evidence(label):
    if (label.startswith('preview:')
            or not (_scheduled_pulse_label(label) or label == 'onboarding:history-discovery:sotto-welcome-brief' or label.startswith('routine:fresh:') or 'prep' in label or re.fullmatch(r'skill:[a-f0-9]{64}:(?:ask-research|followup|relationship-pulse|proactive)', label) or label.split(':')[-1] in (
                'sotto-morning-brief', 'sotto-evening-brief', 'morning-brief', 'evening-brief'))):
        return None
    try:
        target = _deliver_target()
        if target != 'photon' and not (target == 'telegram' and (_scheduled_pulse_label(label) or label.startswith('routine:fresh:') or REQUESTED_BRIEF.LABEL.fullmatch(label) or label == 'onboarding:history-discovery:sotto-welcome-brief')):
            return None
        route_before = _candidate_retained_route_digest()
        if not isinstance(route_before, str) or re.fullmatch(r'[a-f0-9]{64}', route_before) is None:
            return None  # unmounted route proof never slows or changes ordinary delivery
        authority = _runtime_source_authority()
        route_after = _candidate_retained_route_digest()
    except Exception:
        return None
    revision = authority.get('revision')
    if (authority.get('complete') is not True or not isinstance(revision, str)
            or re.fullmatch(r'[a-f0-9]{64}', revision) is None or route_before != route_after):
        return None
    return {'source_revision': revision, 'route_digest': route_before}


def _deliver_text(text: str, label: str, usage: dict | None = None,
                  decision_ids: list | None = None, effects: list | None = None,
                  run_id: str = "", valid_until: float | None = None, not_before: float | None = None,
                  coverage_until: str | None = None, *, routine_proof: bool = False,
                  source_revision_at_composition: str | None = None,
                  route_digest_at_composition: str | None = None,
                  source_text_sha256: str | None = None,
                  research_evidence: dict | None = None,
                  reminder_timezone: str | None = None) -> bool | dict:
    """Hand one skill's final text to the channel, THROUGH THE OUTBOX. Silence is a legitimate
    outcome for every one of these skills ("if there's nothing, say nothing"), so an empty run is
    recorded and never enqueued — an empty message would be the busywork theater the standing bars
    forbid, and an outbox row for it would retry that theater for hours.

    Returns True only when the channel acknowledged the message. False no longer means lost: the row
    is on file and the drain owns it until it lands, ages out, or gives up loudly.

    `run_id` rides the row so the outbox's deliver-once gate can ask whether THIS run owns today's
    brief marker — it is the same id `_spawn_env` handed the run as SOTTO_DELIVERY_RUN_ID, which is
    what that run's own `brief_marker.py --claim` writes into the marker."""
    # Strip BEFORE the empty check: a text that was nothing but markers is an empty run, and the
    # honest receipt for it is "empty", not a delivered blank. Removed marker lines leave doubled
    # blank lines behind — collapse those too, so the reader never sees the surgery.
    body = _delivery_body(text)
    if not body:
        _record_delivery(label, "empty", usage=usage, decision_ids=decision_ids, run_id=run_id)
        return {'kind': 'silent'} if routine_proof else False
    if _is_silence(body):
        _record_delivery(label, "empty", f"{SILENCE_SENTINEL} sentinel — nothing to deliver",
                         usage=usage, decision_ids=decision_ids, run_id=run_id)
        return {'kind': 'silent'} if routine_proof else False
    # An offer is actionable only if its exact question is present in the text being handed off.
    effects = [effect for effect in (effects or []) if effect.get('kind') != 'pending_offer'
               or (str(effect.get('offer', {}).get('question') or '').strip()
                   and str(effect['offer']['question']).strip() in body)]
    target = _deliver_target()
    requested_telegram = bool(REQUESTED_BRIEF.LABEL.fullmatch(label) and
        (target == 'telegram' or _requested_telegram_job(WORK_QUEUE.get(DATA, run_id))))
    if requested_telegram and not _requested_telegram_job_current(
            WORK_QUEUE.get(DATA, run_id), label=label, target=target):
        raise _WorkDeferredError('requested brief authority unavailable')
    presentation = None
    presentation_status = {}
    try:
        text_digest_matches = (isinstance(source_text_sha256, str)
            and re.fullmatch(r'[a-f0-9]{64}', source_text_sha256) is not None
            and hashlib.sha256(body.encode('utf-8')).hexdigest() == source_text_sha256)
    except UnicodeError:
        text_digest_matches = False
    evidence = (_composition_evidence(label)
                if source_revision_at_composition is not None and text_digest_matches else None)
    candidate_route = (evidence['route_digest'] if evidence
        and evidence['source_revision'] == source_revision_at_composition
        and evidence['route_digest'] == route_digest_at_composition else None)
    offers = [e.get('offer', {}) for e in effects if e.get('kind') == 'pending_offer']
    if all(offer.get('kind') == 'handoff' for offer in offers):
        # Consent/procedure offers keep their exact-text contract. Brief handoffs may
        # use photos only when the exact question is visibly rendered in full.
        prepare = _load_shared_lib('visual_delivery').prepare
        def load_visual():
            _shared_effects()  # establish the installed shared library search path
            import visual_brief
            return visual_brief
        presentation = prepare(body, label, target, load_visual,
                               _runtime_adapter().gallery_available, diagnostics=presentation_status,
                               retained_source_revision=(source_revision_at_composition
                                                         if candidate_route else None),
                               candidate_route_digest=candidate_route,
                               **({'research_evidence': research_evidence} if research_evidence is not None and candidate_route else {}),
                               **({'required_questions': tuple(o['question'] for o in offers)} if offers else {}))
    if (requested_telegram or _scheduled_pulse_label(label) or label == 'onboarding:history-discovery:sotto-welcome-brief' or label.startswith('meeting-prep:') or re.fullmatch(r'skill:[a-f0-9]{64}:(?:meeting-prep|ask-research|followup|relationship-pulse|proactive)', label)) and not candidate_route:
        raise _WorkDeferredError('meeting prep composition authority changed')
    payload = {"label": label, "body": body, "target": target,
                           "presentation": presentation, "presentation_status": presentation_status,
                           "usage": usage, "decision_ids": decision_ids,
                           "effects": effects or [], "run_id": run_id, "valid_until": valid_until,
                           "not_before": not_before, "coverage_until": coverage_until}
    if presentation is None and research_evidence is not None and candidate_route:
        try:
            payload['retained_text_artifact'] = _load_shared_lib('visual_delivery').retain_text(
                body, research_evidence, source_revision_at_composition, candidate_route,
                stamp=_load_shared_lib('retained_brief_proof').stamp)
        except Exception as error:  # optional retention follows the same fail-open rule as presentation
            print(f'text_retention_fallback error={type(error).__name__}', file=sys.stderr)
    if reminder_timezone is not None and _owner_timer_notice_ids(label):
        payload["reminder_timezone"] = reminder_timezone
    if (requested_telegram or _scheduled_pulse_label(label) or label == 'onboarding:history-discovery:sotto-welcome-brief' or label.startswith('meeting-prep:') or re.fullmatch(r'skill:[a-f0-9]{64}:(?:meeting-prep|ask-research|followup|relationship-pulse|proactive)', label)):
        payload['prep_source_revision'] = source_revision_at_composition
        payload['prep_route_digest'] = route_digest_at_composition
    if routine_proof:
        if not run_id:
            raise RuntimeError('routine delivery requires a work identity')
        if label.startswith('routine:fresh:'):
            if not candidate_route:
                raise _WorkDeferredError('routine composition authority changed')
            payload['routine_source_revision'] = source_revision_at_composition
            payload['routine_route_digest'] = route_digest_at_composition
        payload['routine_handoff_digest'] = OUTBOX.routine_payload_digest(payload)
    accepted = OUTBOX.deliver(payload)
    return {'kind': 'outbox', 'digest': payload['routine_handoff_digest']} if routine_proof else accepted


def _read_usage(path: str | None) -> dict | None:
    """Optional accounting normalized by the pinned runtime adapter."""
    return _runtime_adapter().read_usage(path)


def _spawn_env(run_id: str = "") -> dict:
    """The environment a spawned one-shot inherits. SOTTO_UNATTENDED=1 is the contract that marks
    an UNATTENDED lane: nobody is at the keyboard, so the send-gate downstream must hold anything
    that would reach a human. The interactive gateway is not spawned by us and therefore never
    carries it — that asymmetry IS the design, not an omission.

    SOTTO_DELIVERY_RUN_ID is this run's IDENTITY, and both directions of it matter: the run writes
    back its delivery effects under that id, and its `brief_marker.py --claim` stamps that id into
    the day's deliver-once marker — which is how the send seam later tells "this run claimed" from
    "another lane claimed"."""
    env = {**os.environ, "SOTTO_UNATTENDED": "1"}
    env.pop('SOTTO_CHAT_SEND_TOKEN', None)
    env.pop("SOTTO_CONTROL_TOKEN", None)
    if run_id:
        env["SOTTO_DELIVERY_RUN_ID"] = run_id
    return env


def _delivery_effects_path(run_id: str) -> str:
    return os.path.join(_events_dir(), f"delivery-effects-{run_id}.json")


def _read_delivery_effects(run_id: str) -> dict:
    try:
        with open(_delivery_effects_path(run_id), encoding="utf-8") as f:
            out = json.load(f)
        return out if isinstance(out, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _finalize_delivery_effects(effects: list) -> bool:
    pending = [e for e in (effects or []) if isinstance(e, dict)
               and e.get("kind") in ("chase", "handoff")
               and str(e.get("anchor_key") or "").strip()]
    if not pending:
        return True
    script = _find_sotto_script("morning-brief", "scripts", "continuity_resolve.py")
    if not script:
        print("[sotto] delivered nudge: continuity finalizer not found", flush=True)
        return False
    flags = {"chase": "--finalize-chase", "handoff": "--finalize-handoff"}
    all_ok = True
    for effect in pending:
        flag, anchor = flags.get(effect.get("kind")), str(effect.get("anchor_key") or "").strip()
        if not (flag and anchor):
            continue
        finalized, detail = False, ""
        # The operation is idempotent, so one immediate retry safely covers a transient fork or
        # volume error. A delivery that landed must not silently leave its chase uncounted.
        for _attempt in range(2):
            try:
                r = subprocess.run([sys.executable, script, flag, anchor], capture_output=True,
                                   text=True, timeout=30, env=_skill_env())
                detail = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
                payload = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
                if r.returncode == 0 and isinstance(payload, dict) and payload.get("ok") is True:
                    finalized = True
                    break
            except Exception as e:  # noqa: BLE001 — retry once, then make the mismatch loud
                detail = f"{type(e).__name__}: {e}"
        if not finalized:
            all_ok = False
            print(f"[sotto] delivered nudge: failed to finalize {effect.get('kind')} "
                  f"{anchor} — {detail[:300]}", flush=True)
    return all_ok


_WORK_OWNER = secrets.token_hex(16)
_WORK_WAKE = threading.Event()
_WORK_STOP = threading.Event()
_WORK_PROCESSES = {}
# worker thread → the (job_id, owner) lease it still holds. A worker removes its own entry once the
# job is settled, so what remains after the shutdown grace is exactly what the shutdown must refund.
_WORK_THREADS = {}
_WORK_LOCK = threading.Lock()
WORK_SHUTDOWN_GRACE_SECS = 10


def _brief_revision(kind=None):
    source_context = _source_context()
    permission = {source: source_context.allowed(source) for source in
                  (*source_context.SOURCE_FIELDS, 'gmail', 'calendar', 'granola')}
    try:
        calendar = json.loads(Path(DATA, 'cache/calendar_today.json').read_text()).get('events', [])
    except (OSError, ValueError, AttributeError):
        calendar = []
    events = [{k: event.get(k) for k in ('id', 'start', 'end', 'my_response')}
              for event in calendar if isinstance(event, dict)]
    events.sort(key=lambda event: json.dumps(event, sort_keys=True))
    return hashlib.sha256(json.dumps({'permission': permission, 'calendar': events,
                                    'invalidations': OUTBOX.invalidation_revision(kind, str(_local_now().date()))},
                                    sort_keys=True).encode()).hexdigest()[:16]


def _job_key(label, prompt):
    if re.fullmatch(r'meeting-prep:[a-f0-9]{32}(?:[a-f0-9]{32})?', label):
        return label
    if REQUESTED_BRIEF.LABEL.fullmatch(label):
        return 'requested:' + REQUESTED_BRIEF.LABEL.fullmatch(label).group(1)
    if re.fullmatch(r'skill:[a-f0-9]{64}:(?:meeting-prep|ask-research|followup|relationship-pulse|proactive)', label):
        return label.rsplit(':', 1)[0]
    import onboarding
    if label == onboarding.DISCOVERY_LABEL:
        return onboarding.DISCOVERY_KEY + ':' + str(_local_now().date())
    now = _local_now()
    name = label.rsplit(':', 1)[-1].strip()
    if name in MARKED_BRIEF_KINDS and not label.startswith('run-now:'):
        return f'brief:{name}:{now.date()}:{_brief_revision(MARKED_BRIEF_KINDS[name])}'
    if label.startswith('cron:'):
        schedule = next((j[1] for j in _sotto_cron_jobs() if j[0] == name), '')
        return f'{label}:{_cron_slot(schedule) or now.date()}'
    if label.startswith('background:'):
        return f'{label}:{int(time.time() // MEMORY_INTERVAL_SECONDS)}'
    return None


def _require_sotto_procedure(runner: list, kind: str = 'run') -> None:
    """The receiver may execute only its declared Python procedures.

    This validates a receiver-owned command, not a model-provided script path. The same
    check runs at admission and again immediately before execution.
    """
    if kind not in ('run', 'event'):
        raise RuntimeError('Sotto unattended skill procedure is unsupported')
    local = Path(__file__).resolve().parent
    expected = [local / 'brief_runner.py', local / 'procedure_runner.py'] if kind == 'run' else []
    packaged = ('memory_cycle.py',) if kind == 'run' else ('compose_notification.py',)
    for script in packaged:
        found = _find_sotto_script('_shared', 'scripts', script)
        if found:
            expected.append(Path(found))
    if (len(runner) != 2 or runner[0] != sys.executable
            or not any(Path(runner[1]).resolve() == path.resolve() for path in expected)):
        raise RuntimeError('Sotto unattended skill procedure is unsupported')


def _validate_fixed_work(kind: str, request: dict) -> None:
    _runtime_adapter().validate_fixed_work(kind, request)


def _selected_fixed_profile() -> bool:
    return os.environ.get('SOTTO_RUNTIME_PROFILE') in (
        'managed-photon-fixed-five-v1', 'self-host-telegram-fixed-five-v1',
        'self-host-photon-fixed-five-v1')


def _fixed_schedule_authority() -> str:
    """The tenant/profile identity that owns fixed slot history, never a restart authorization.

    Zone, crons.json and owner route are configuration, not identity: they live in
    `_fixed_schedule_digest`, which the WorkQueue rebinds in place (keeping slot history)
    instead of refusing, so an owner timezone change or a crons release cannot stop the
    five procedures. A fixed wall slot remains the same occurrence across epochs.
    """
    tenant = os.environ.get('SOTTO_TENANT_ID', '')
    profile = os.environ.get('SOTTO_RUNTIME_PROFILE')
    if not tenant or not profile:
        raise RuntimeError('fixed schedule authority is unavailable')
    return hashlib.sha256(json.dumps(['sotto-fixed-schedule-identity-v2', tenant, profile],
                                     ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _fixed_schedule_digest() -> str:
    """The current fixed schedule configuration; this v1 transcript is also the legacy authority."""
    route = _candidate_retained_route_digest()
    tenant = os.environ.get('SOTTO_TENANT_ID', '')
    zone = _configured_tz_name()
    jobs = [(name, schedule, prompt, skill)
            for name, schedule, prompt, skill in _sotto_cron_jobs()]
    if (not route or not tenant or not zone or len(jobs) != 5
            or len({name for name, *_ in jobs}) != 5):
        raise RuntimeError('fixed schedule authority is unavailable')
    transcript = ['sotto-fixed-schedule-v1', tenant,
                  os.environ.get('SOTTO_RUNTIME_PROFILE'),
                  route, zone, sorted(jobs)]
    return hashlib.sha256(json.dumps(transcript, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _spawn_and_deliver(runner: list, prompt: str, label: str,
                       decision_ids: list | None = None, work_not_before: float | None = None,
                       retry_terminal: bool = False,
                       fixed_recovery_from: str | None = None, feedback: dict | None = None, admission: dict | None = None) -> str:
    """Commit accepted work before returning; the bounded worker owns retries and recovery."""
    _require_sotto_procedure(runner)
    _validate_fixed_work('run', {
        'runner': runner, 'prompt': prompt, 'label': label,
        'decision_ids': decision_ids or []})
    executable = _runtime_adapter().run_argv(runner, '')[0]
    if not shutil.which(executable):
        raise FileNotFoundError('procedure runner not found')
    now = _local_now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    validity = tomorrow.timestamp() if OUTBOX.kind_for(label) != OUTBOX.KIND_NUDGE else time.time() + 4 * 3600
    if label.startswith('cron:'):
        schedule = next((job[1] for job in _sotto_cron_jobs() if job[0] == label[5:]), '')
        at = _fixed_daily_minute(schedule)
        if at is not None:
            due = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
            validity = min(validity, due.timestamp() + DAILY_CATCHUP_SECONDS)
    priority = 60 if label.startswith('background:') else 10 if 'brief' in label else 20
    payload = {'runner': runner, 'prompt': prompt, 'label': label,
               'decision_ids': decision_ids or []}
    key = _job_key(label, prompt)
    if _selected_fixed_profile() and label.startswith('cron:'):
        name = label[5:]
        schedule = next((job[1] for job in _sotto_cron_jobs() if job[0] == name), '')
        # Preparation is ten minutes before the scheduled daily fire. Both preparation and
        # the actual tick own the same local-date slot, including the second DST fold.
        wall_slot = (str(now.date()) if name in MARKED_BRIEF_KINDS
                     else _scheduled_slot(schedule))
        if wall_slot is None:
            raise RuntimeError('fixed schedule is outside its firing window')
        job_id = WORK_QUEUE.enqueue_fixed_slot(
            DATA, payload, key=key, authority_digest=_fixed_schedule_authority(),
            schedule_digest=_fixed_schedule_digest(),
            name=name, wall_slot=wall_slot, not_before=work_not_before,
            valid_until=validity, priority=priority, retry_terminal=retry_terminal,
            replace_pending=name in MARKED_BRIEF_KINDS,
            recovery_from=fixed_recovery_from)
        if job_id is None:
            raise RuntimeError('fixed schedule slot was deliberately skipped')
    else:
        job_id = WORK_QUEUE.enqueue(DATA, 'run', payload, key=key,
            not_before=work_not_before, valid_until=validity,
            priority=priority, retry_terminal=retry_terminal, feedback=feedback, admission=admission,
            shared_fixed_brief_wake=(
                _selected_fixed_profile() and label.startswith('brief:')
                and label[6:] in MARKED_BRIEF_KINDS))
    _WORK_WAKE.set()
    return job_id


def _requested_cache_current(cached, evidence):
    """Reuse only exact accepted text whose original retained source/route proof is current."""
    if evidence is None:
        return False
    try:
        with CONNECTORS.json_transaction(OUTBOX.path(), default={'rows': []}) as state:
            artifacts = [row.get('receipt', {}).get('visual_artifact_id') for row in state.get('rows', [])
                         if row.get('id') in cached.get('_delivery_run_ids', [])]
        reader = _load_shared_lib('protected_brief_detail')
        rendered = _load_shared_lib('chatfmt').to_imessage(
            _delivery_body(cached['brief_text']), normalize_style=False).strip()
        for artifact in artifacts:
            if not artifact:
                continue
            detail = reader.read(DATA, {'artifact_id': artifact, 'offset': 0, 'limit': reader.MAX_PAGE},
                                 source_revision=evidence['source_revision'], route_digest=evidence['route_digest'])
            if (detail.get('status') == 'found' and detail.get('next_offset') is None
                    and detail.get('text') == rendered):
                return True
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return False


def _execute_work(job):
    request = job['payload']
    owner = job.get('owner') or _WORK_OWNER
    process_key = (job['id'], owner)
    staged = None
    process = None
    usage_path = None
    inflight = False
    try:
        if not _requested_telegram_job_current(job):
            raise _WorkDeferredError('requested brief authority unavailable')
        _validate_fixed_work(job['kind'], request)
        eligibility = {'effects': [], 'valid_until': None}
        coverage_until = datetime.now(timezone.utc).isoformat()
        if job['kind'] == 'event':
            eligibility = _shared_effects().for_bundle(request.get('bundle') or {})
            if not _shared_effects().valid(eligibility['effects'], time.time()):
                return {'text': 'NO_NUDGES', 'label': 'event', 'effects': []}
            staged = _stage_bundle(request.get('bundle') or {})
            request = _event_request(staged)
            _validate_fixed_work('run', request)
        label = str(request.get('label') or job['kind'])
        runner = list(request['runner'])
        _require_sotto_procedure(runner, job['kind'])
        composition_before = _composition_evidence(label)
        if (_requested_telegram_job(job) or _scheduled_pulse_label(label) or label == 'onboarding:history-discovery:sotto-welcome-brief' or label.startswith(('routine:fresh:', 'meeting-prep:')) or re.fullmatch(r'skill:[a-f0-9]{64}:(?:meeting-prep|ask-research|followup|relationship-pulse|proactive)', label)) and composition_before is None:
            # A new owner event renews the existing activation ACK. Wait for that
            # current proof before composing an otherwise undeliverable result.
            raise _WorkDeferredError('routine composition authority unavailable')
        usage_path = os.path.join(_events_dir(), f"usage-{job['id']}.json")
        prompt = request.get('prompt') or ''
        try:
            procedure = json.loads(prompt)
        except (ValueError, TypeError):
            procedure = {}
        if (REQUESTED_BRIEF.LABEL.fullmatch(label) and not procedure.get('refresh')
                and not procedure.get('composition_direction')):
            cached = REQUESTED_BRIEF.accepted_artifact(
                DATA, procedure['day'], procedure['kind'], _source_context().allowed)
            if cached and _requested_cache_current(cached, composition_before):
                evidence = _composition_evidence(label)
                if composition_before is None or evidence != composition_before:
                    raise _WorkDeferredError('requested brief composition authority unavailable')
                return {'text': cached['brief_text'], 'label': label,
                        'source_revision_at_composition': (evidence['source_revision'] if evidence else None),
                        'route_digest_at_composition': (evidence['route_digest'] if evidence else None),
                        'source_text_sha256': (hashlib.sha256(_delivery_body(cached['brief_text']).encode()).hexdigest()
                                               if evidence else None),
                        'effects': [{'kind': 'source_permissions', 'sources': cached['_source_permissions']}],
                        'valid_until': job.get('valid_until'),
                        'coverage_until': cached.get('_source_cutoff')}
        # The declared procedure receives only its own arguments.
        argv = _runtime_adapter().run_argv(runner, prompt)
        _record_delivery(label, 'spawned', decision_ids=request.get('decision_ids'), run_id=job['id'])
        # Child creation and registration share the signal handler's lock. Thus shutdown either
        # wins before Popen (and no child is created) or snapshots the new child after registration.
        child_env = _spawn_env(job['id'])
        if _scheduled_pulse_label(label) or label == 'onboarding:history-discovery:sotto-welcome-brief' or label.startswith('meeting-prep:') or re.fullmatch(r'skill:[a-f0-9]{64}:(?:meeting-prep|ask-research|followup|relationship-pulse|proactive)', label):
            child_env['SOTTO_SKILL_COMPOSITION_PROOF'] = json.dumps(composition_before)
        if re.fullmatch(r'skill:[a-f0-9]{64}:proactive', label):
            captured = _runtime_source_authority()
            if (captured.get('complete') is not True
                    or captured.get('revision') != composition_before['source_revision']):
                raise RuntimeError('proactive source authority changed')
            child_env['SOTTO_PROACTIVE_CAPTURED_SOURCES'] = json.dumps(sorted(set(
                captured.get('bridge_sources', []) + captured.get('google_sources', [])
                + captured.get('connector_sources', []))))
        with _WORK_LOCK:
            if _WORK_STOP.is_set():
                raise _WorkInterruptedError('work interrupted by shutdown')
            _RUNS_INFLIGHT[label] = _RUNS_INFLIGHT.get(label, 0) + 1
            inflight = True
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, env=child_env, start_new_session=True)
            _WORK_PROCESSES[process_key] = process
        deadline = time.monotonic() + ONESHOT_TIMEOUT_SECS
        while True:
            if _WORK_STOP.is_set():
                raise _WorkInterruptedError('work interrupted by shutdown')
            if time.monotonic() >= deadline:
                raise TimeoutError('work timed out')
            try:
                stdout, _stderr = process.communicate(timeout=min(30, max(1, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                if not WORK_QUEUE.renew(DATA, job['id'], owner):
                    raise RuntimeError('work lease lost')
        # SIGTERM can arrive while communicate() is blocked. The handler terminates the child,
        # which wakes this thread with a negative return code; that is a shutdown interruption,
        # not a failed model run whose bounded attempt should be charged.
        if _WORK_STOP.is_set():
            raise _WorkInterruptedError('work interrupted by shutdown')
        if (process.returncode == 75
                and runner == [sys.executable, str(Path(__file__).with_name('brief_runner.py'))]):
            raise _WorkDeferredError('brief generation is busy')
        if (process.returncode == WORK_QUEUE.MODEL_ADMISSION_BUSY_EXIT
                and runner in ([sys.executable, str(Path(__file__).with_name('brief_runner.py'))],
                               [sys.executable, str(Path(__file__).with_name('procedure_runner.py'))],
                               [sys.executable, _find_sotto_script('_shared', 'scripts', 'compose_notification.py')])):
            raise _WorkAdmissionBusyError('model admission busy')
        if process.returncode:
            error = _WorkerError(process.returncode, _stderr)
            error.model_budget_exhausted = (
                job['kind'] == 'run' and process.returncode == WORK_QUEUE.MODEL_BUDGET_EXIT
                and runner in ([sys.executable, str(Path(__file__).with_name('brief_runner.py'))],
                               [sys.executable, str(Path(__file__).with_name('procedure_runner.py'))]))
            error.model_outcome_uncertain = (
                job['kind'] == 'run' and process.returncode == WORK_QUEUE.MODEL_OUTCOME_UNCERTAIN_EXIT
                and runner == [sys.executable, str(Path(__file__).with_name('brief_runner.py'))])
            error.retry_provider = (job['kind'] == 'event'
                                    and process.returncode == WORK_QUEUE.PROVIDER_RETRY_EXIT
                                    and runner == [sys.executable, _find_sotto_script('_shared', 'scripts', 'compose_notification.py')])
            error.usage = _read_usage(usage_path)
            raise error
        if (runner == [sys.executable, str(Path(__file__).with_name('procedure_runner.py'))]
                and (label.startswith('meeting-prep:') or re.fullmatch(r'skill:[a-f0-9]{64}:meeting-prep', label))
                and '[meeting-prep] research persistence failed; prep remains available' in _stderr):
            _record_delivery(label, 'research_persistence_failed', run_id=job['id'])
        kind = MARKED_BRIEF_KINDS.get(label.rsplit(':', 1)[-1])
        import onboarding
        archive_kind = 'history-discovery' if label == onboarding.DISCOVERY_LABEL else kind
        if kind and not _is_silence(stdout.strip()) and not _composed_brief_recently(archive_kind, str(_local_now().date())):
            raise RuntimeError('brief runner produced no archived composition')
        effect_doc = _read_delivery_effects(job['id'])
        composition_after = (_composition_evidence(label)
                             if composition_before is not None else None)
        matched_evidence = (composition_before
                            if composition_before == composition_after else None)
        try:
            source_text_sha256 = (hashlib.sha256(_delivery_body(stdout).encode('utf-8')).hexdigest()
                                  if matched_evidence else None)
        except UnicodeError:
            source_text_sha256 = None
        deadlines = [d for d in (job.get('valid_until'), eligibility.get('valid_until')) if d is not None]
        return {'text': stdout, 'label': label, 'usage': _read_usage(usage_path),
                'source_revision_at_composition': (matched_evidence['source_revision']
                                                   if matched_evidence else None),
                'route_digest_at_composition': (matched_evidence['route_digest']
                                                if matched_evidence else None),
                'source_text_sha256': source_text_sha256,
                **({'research_evidence': effect_doc['research_evidence']}
                   if effect_doc.get('research_evidence') is not None else {}),
                'decision_ids': effect_doc.get('notification_decision_ids', list(dict.fromkeys([
                    *(request.get('decision_ids') or []), *(effect_doc.get('decision_ids') or [])]))),
                'effects': [*(effect_doc.get('effects') or []), *eligibility['effects']],
                'valid_until': min(deadlines) if deadlines else None,
                'not_before': procedure.get('deliver_not_before') if isinstance(procedure, dict) else None,
                'coverage_until': effect_doc.get('coverage_until') or next(
                    (effect['coverage_until'] for effect in effect_doc.get('effects', [])
                     if effect.get('kind') == 'source_permissions' and effect.get('coverage_until')),
                    coverage_until)}
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        with _WORK_LOCK:
            _WORK_PROCESSES.pop(process_key, None)
            if inflight:
                _RUNS_INFLIGHT[label] = max(0, _RUNS_INFLIGHT.get(label, 0) - 1)
        for path in (usage_path, staged):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


class _WorkerError(RuntimeError):
    """Only allowlisted exception names and an exit code cross the worker log boundary — the
    allowlist itself lives in work_queue.diagnostic_category, shared with the runners."""
    def __init__(self, returncode, stderr):
        self.diagnostic = f'worker_exit_{int(returncode)}:{WORK_QUEUE.diagnostic_category(stderr)}'
        super().__init__(self.diagnostic)


class _WorkInterruptedError(RuntimeError):
    """The worker was asked to stop (shutdown), mid-job. Not a failure of the job."""


class _WorkDeferredError(RuntimeError):
    """The deterministic runner could not acquire its input lock before doing any work."""


def _routine_outbox_authorized(payload: dict) -> bool:
    job_id = payload.get('run_id')
    digest = payload.get('routine_handoff_digest')
    if not isinstance(job_id, str) or not isinstance(digest, str):
        return False
    if payload.get('label', '').startswith('routine:fresh:') and not _retained_proof_current({
            'source_revision': payload.get('routine_source_revision'),
            'route_digest': payload.get('routine_route_digest')}):
        return False
    slot = WORK_QUEUE.routine_slot_for_job(DATA, job_id)
    if slot is None or (slot['handoff_digest'] is not None and
                        slot['handoff_digest'] != digest):
        return False
    try:
        return _fresh_routine_authority(job_id, payload.get('target')) is not None
    except Exception:
        return False


def _reconcile_routine_job(job_id: str) -> bool:
    authority = _fresh_routine_authority(job_id, _deliver_target())
    if authority is None:
        return False
    slot = WORK_QUEUE.routine_slot_for_job(DATA, job_id)
    if not slot or slot['completion_utc'] is None:
        return False
    if slot['handoff_kind'] == 'silent':
        evidence = {'kind': 'silent'}
    elif slot['handoff_kind'] == 'outbox':
        evidence = OUTBOX.receipt_for_run(job_id, slot['handoff_digest'])
        if evidence is None:
            return False
    else:
        return False
    WORK_QUEUE.reconcile_routine_cursor(DATA, pending_job_id=job_id,
        evidence=evidence, successor_after_completion=FRESH_ROUTINES.successor_after_completion,
        **authority)
    return True


def _reconcile_pending_routines() -> None:
    if not _fresh_routines_supported():
        return
    for job_id in WORK_QUEUE.pending_routine_job_ids(DATA):
        try:
            _reconcile_routine_job(job_id)
        except Exception as error:
            print(f'[sotto] routine reconciliation held ({type(error).__name__})', flush=True)


def _fresh_routines_supported():
    return (os.environ.get('SOTTO_RUNTIME_PROFILE') in
            ('managed-photon-fixed-five-v1', 'self-host-photon-fixed-five-v1', 'self-host-telegram-fixed-five-v1')
            and re.fullmatch('[a-f0-9]{32}', os.environ.get('SOTTO_ROUTINE_EPOCH', '')) is not None)


def _fresh_row_current(row):
    return row['status'] == 'active' and row['binding'] == RUNTIME_OWNER_ROUTINE_TOOL.current_binding(
        row['binding']['identity'], dict(os.environ, _configured_tz_name=_configured_tz_name),
        DATA, RUNTIME_ACTIVATION,
        **({'telegram_verifier': _load_shared_lib('telegram_channel_proof'),
            'chat_client': _runtime_adapter()._client(timeout=5)}
           if row['binding']['identity']['channel'] == 'telegram' else
           {'chat_client': _runtime_adapter()._client(timeout=5)}
           if row['binding']['identity']['route_mode'] == 'first_contact_shared_project' else {}))


def _fresh_routine_authority(job_id, target):
    if target not in ('photon', 'telegram') or target != _deliver_target() or not _fresh_routines_supported():
        return None
    try:
        cursors = FRESH_ROUTINES.cursors(DATA)
        for row in FRESH_ROUTINES.load(DATA)['routines'].values():
            binding = FRESH_ROUTINES.cursor_binding(row)
            key = WORK_QUEUE._routine_hash(binding['owner_scope'], binding['profile_id'], binding['legacy_id'])
            cursor = cursors.get(key)
            if not cursor or cursor['pending_job_id'] != job_id or not _fresh_row_current(row):
                continue
            proof = {**binding, 'schedule_expr': FRESH_ROUTINES.expression(row['definition']),
                     'schedule_zone': row['definition']['zone'], 'expected_generation': cursor['generation']}
            if WORK_QUEUE.routine_job_matches_authority(DATA, pending_job_id=job_id, **proof):
                return proof
    except Exception:
        return None
    return None


def _fresh_routine_tick():
    if not _fresh_routines_supported():
        return
    try:
        doc = FRESH_ROUTINES.load(DATA)
    except FileNotFoundError:
        return
    for row in doc['routines'].values():
        try:
            if not _fresh_row_current(row):
                continue
            definition = row['definition']
            binding = FRESH_ROUTINES.cursor_binding(row)
            cursor = WORK_QUEUE.bind_routine_cursor(DATA, **binding, recorded_at_utc=datetime.fromisoformat(row['initial_utc'].replace('Z', '+00:00')),
                schedule_expr=FRESH_ROUTINES.expression(definition), schedule_zone=definition['zone'],
                completed_count=0, last_run_at=None)
            if cursor['pending_job_id'] is not None:
                continue
            due = datetime.fromisoformat(cursor['current_utc'].replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            if due > now:
                continue
            next_at = FRESH_ROUTINES.successor_after_completion(
                expr=FRESH_ROUTINES.expression(definition), completed_at=now, zone=definition['zone'])
            payload = {'runner': [sys.executable, str(Path(__file__).with_name('procedure_runner.py'))],
                       'prompt': json.dumps({'pack': str(Path(_find_sotto_script('_shared', 'scripts', 'memory_cycle.py')).parents[2]), 'kind': 'personal-routine',
                           'routine_id': row['routine_id'], 'definition_hash': row['definition_hash'],
                           'prompt': definition['prompt']}),
                       'label': 'routine:fresh:' + row['routine_id'], 'decision_ids': []}
            WORK_QUEUE.admit_routine_cursor_slot(DATA, **binding, expected_recorded_at_utc=due,
                next_provisional_at_utc=datetime.fromisoformat(next_at), payload=payload)
        except Exception as error:
            print(f'[sotto] fresh routine held ({type(error).__name__})', flush=True)


class _WorkAdmissionBusyError(RuntimeError):
    """The proxy rejected an unbilled call; the original work deadline still applies."""


def _work_one(job):
    settled = threading.Event()
    owner = job.get('owner') or _WORK_OWNER

    def keep_lease():
        while not settled.wait(30):
            if not WORK_QUEUE.renew(DATA, job['id'], owner):
                return

    threading.Thread(target=keep_lease, name='sotto-work-lease', daemon=True).start()
    try:
        if job['kind'] == 'source_correction':
            WORK_QUEUE.fail(DATA, job['id'], owner, 'automatic source corrections retired')
            return
        routine_slot = WORK_QUEUE.routine_slot_for_job(DATA, job['id'])
        if routine_slot:
            if _fresh_routine_authority(job['id'], _deliver_target()) is None:
                raise _WorkDeferredError('routine authority unavailable')
            if job.get('result') is None:
                request = job.get('payload') or {}
                _require_sotto_procedure(list(request['runner']), job['kind'])
        result = job.get('result')
        if not _requested_telegram_job_current(job):
            raise _WorkDeferredError('requested brief authority unavailable')
        if result is None:
            result = _execute_work(job)
            if routine_slot:
                # Capture execution completion BEFORE any outbox wait/retry. A
                # saved result keeps this exact instant across worker restart.
                result = {**result, '_routine_completed_at': datetime.now(timezone.utc).isoformat()}
            WORK_QUEUE.save_result(DATA, job['id'], owner, result)
        if not WORK_QUEUE.renew(DATA, job['id'], owner):
            raise RuntimeError('work lease lost before delivery handoff')
        if not _requested_telegram_job_current(job):
            raise _WorkDeferredError('requested brief authority unavailable')
        if routine_slot and _fresh_routine_authority(job['id'], _deliver_target()) is None:
            raise _WorkDeferredError('routine authority unavailable')
        # An already-composed result retries only this handoff; the outbox dedupes its stable ID.
        handoff = _deliver_text(result['text'], result['label'], usage=result.get('usage'),
                      decision_ids=result.get('decision_ids'), effects=result.get('effects'),
                      run_id=job['id'], valid_until=result.get('valid_until'),
                      not_before=result.get('not_before'), coverage_until=result.get('coverage_until'),
                      routine_proof=bool(routine_slot),
                      source_revision_at_composition=result.get('source_revision_at_composition'),
                      route_digest_at_composition=result.get('route_digest_at_composition'),
                      source_text_sha256=result.get('source_text_sha256'),
                      **({'research_evidence': result['research_evidence']}
                         if result.get('research_evidence') is not None else {}))
        if not WORK_QUEUE.finish(DATA, job['id'], owner,
                                 routine_handoff=handoff if routine_slot else None):
            raise RuntimeError('work lease lost before finish')
        if routine_slot:
            try:
                _reconcile_routine_job(job['id'])
            except Exception as error:
                # A missing or ambiguous receipt holds its pending cursor. The
                # ordinary work/outbox settlement already committed.
                print(f'[sotto] routine reconciliation held ({type(error).__name__})', flush=True)
        try:
            os.unlink(_delivery_effects_path(job['id']))
        except OSError:
            pass
    except (_WorkDeferredError, _WorkAdmissionBusyError) as error:
        # Learning and recomposition share an input lock. Contention must not spend the three
        # fault attempts. A typed proxy refusal is equally unbilled. Release moves the next due
        # time without changing the original relevance deadline or a saved delivery result.
        admission_busy = isinstance(error, _WorkAdmissionBusyError)
        disposition = WORK_QUEUE.defer(DATA, job['id'], owner, delay_seconds=60,
                                       max_age_seconds=3600 if admission_busy else None)
        if disposition is None:
            return  # a newer lease owns the result and its receipt
        reason = ('model admission busy' if isinstance(error, _WorkAdmissionBusyError)
                  else 'generation busy')
        detail = ('retry queued' if disposition in ('pending', 'ready')
                  else 'relevance deadline reached; no retry queued')
        _record_delivery(job['payload'].get('label', job['kind']),
                         'skipped' if disposition in ('pending', 'ready') else 'failed',
                         f'work {job["id"]}: {reason}; {detail}', run_id=job['id'])
    except _WorkInterruptedError:
        # A redeploy's SIGTERM, not the job's fault: the lease goes back uncharged so the next
        # instance simply resumes it. Charging it made a day's brief terminal in three pushes.
        WORK_QUEUE.release(DATA, job['id'], owner)
        _record_delivery(job['payload'].get('label', job['kind']), 'skipped',
                         f'work {job["id"]}: interrupted by shutdown; resumes on the next instance',
                         run_id=job['id'])
    except Exception as error:
        diagnostic = error.diagnostic if isinstance(error, _WorkerError) else type(error).__name__
        budget_denied = getattr(error, 'model_budget_exhausted', False) and job.get('result') is None
        outcome_uncertain = getattr(error, 'model_outcome_uncertain', False) and job.get('result') is None
        disposition = WORK_QUEUE.fail(DATA, job['id'], owner, diagnostic,
                                      retry_provider=getattr(error, 'retry_provider', False),
                                      model_budget_exhausted=budget_denied, model_outcome_uncertain=outcome_uncertain)
        if disposition is None:
            return  # a newer lease owns both the retry and its receipt
        if budget_denied and disposition == 'failed':
            import onboarding
            diagnostic = WORK_QUEUE.MODEL_BUDGET_EXHAUSTED
            if job['payload'].get('label') == onboarding.LABEL:
                onboarding.model_budget_held(DATA)
        detail = ('model outcome uncertain; no retry queued' if outcome_uncertain and disposition == 'failed' else
                  'model allowance reached; no retry queued' if budget_denied and disposition == 'failed' else
                  'retry queued' if disposition in ('pending', 'ready') else
                  'relevance deadline reached; no retry queued' if disposition == 'expired' else
                  'attempt limit reached; no retry queued')
        _record_delivery(job['payload'].get('label', job['kind']), 'failed',
                         f'work {job["id"]}: {diagnostic}; {detail}',
                         usage=getattr(error, 'usage', None), run_id=job['id'])
    finally:
        settled.set()
        _WORK_WAKE.set()
        # Dropping the registration is what tells the shutdown path this lease needs no refund.
        # It happens only after finish/fail/release above has settled the job.
        with _WORK_LOCK:
            _WORK_THREADS.pop(threading.current_thread(), None)


def _drain_work():
    while not _WORK_STOP.is_set():
        # A lease recovered in this same process must fence the old worker just as a restart does.
        job = WORK_QUEUE.claim(DATA, _WORK_OWNER + ':' + secrets.token_hex(16))
        if job is None:
            break
        worker = threading.Thread(target=_work_one, args=(job,), name='sotto-work', daemon=True)
        # Registration and start are one shutdown-visible step: the handler can never snapshot an
        # unstarted thread, and a claim made just before STOP is refunded without launching work.
        with _WORK_LOCK:
            if _WORK_STOP.is_set():
                WORK_QUEUE.release(DATA, job['id'], job['owner'])
                break
            _WORK_THREADS[worker] = (job['id'], job.get('owner') or _WORK_OWNER)
            try:
                worker.start()
            except Exception:
                _WORK_THREADS.pop(worker, None)
                WORK_QUEUE.release(DATA, job['id'], job['owner'])
                raise


# Legacy skill jobs whose withheld note this process already recorded (the ledger keeps it across
# restarts); avoids rescanning delivery.jsonl on every reconcile tick.
_WITHHELD_SKILL_NOTICES: set = set()


def _requested_brief_feedback():
    """Reconcile requested work even when expiry happened before a worker could claim it."""
    owner = os.environ.get('PHOTON_HOME_CHANNEL', '')
    proof = []  # one route-proof read per pass, shared by every bound skill job
    for job in WORK_QUEUE.feedback_jobs(DATA):
        meta = job['feedback']
        label = meta.get('label', '') if isinstance(meta, dict) else ''
        if (not isinstance(label, str)
                or not (REQUESTED_BRIEF.LABEL.fullmatch(label) or OUTBOX.feedback_skill(label))):
            continue
        full_job = WORK_QUEUE.get(DATA, job['id']) if REQUESTED_BRIEF.LABEL.fullmatch(label) else None
        if full_job and (_requested_telegram_job(full_job) or meta.get('target') == 'telegram'):
            binding = _requested_telegram_job(full_job)
            if (not _requested_telegram_job_current(full_job, label=label, target=meta.get('target'))
                    or not binding or meta.get('owner') != binding['owner_id']
                    or meta.get('chat_id') != binding['owner_id']):
                if OUTBOX.enqueue_work_feedback(job, enqueue=False) is True:
                    WORK_QUEUE.clear_feedback(DATA, job['id'])
                continue
            if OUTBOX.enqueue_work_feedback(job):
                WORK_QUEUE.clear_feedback(DATA, job['id'])
            continue
        if OUTBOX.feedback_skill(label):
            if not _skill_notice_bound(meta):
                # A legacy job has no admission route to bind, so it never gets a notice. Note the
                # withholding only when a notice was actually owed; the four-hour notice window
                # (or a delivered original) then clears the job's feedback.
                decision = OUTBOX.enqueue_work_feedback(job, enqueue=False)
                if decision is True:
                    WORK_QUEUE.clear_feedback(DATA, job['id'])
                elif decision == 'notice' and job['id'] not in _WITHHELD_SKILL_NOTICES:
                    _record_delivery(label, 'skipped',
                        'Terminal skill notice withheld: original verified owner route proof is unavailable.',
                        run_id=job['id'], once=True)
                    if len(_WITHHELD_SKILL_NOTICES) >= 1024:
                        _WITHHELD_SKILL_NOTICES.clear()
                    _WITHHELD_SKILL_NOTICES.add(job['id'])
                continue
            if not proof:
                proof.append(_candidate_retained_route_proof())
            if proof[0] is None or not _skill_notice_current(meta, proof[0]):
                # The route changed after admission: no notice, and none is owed once the window ends.
                if OUTBOX.enqueue_work_feedback(job, enqueue=False) is True:
                    WORK_QUEUE.clear_feedback(DATA, job['id'])
                continue
        elif (meta.get('owner') != owner or meta.get('target') != _deliver_target()
                or not REQUESTED_BRIEF.owner_destination(
                    DATA, owner, meta.get('chat_id'), os.environ.get('SOTTO_TENANT_ID'))):
            continue
        if OUTBOX.enqueue_work_feedback(job):
            WORK_QUEUE.clear_feedback(DATA, job['id'])


def start_work_thread():
    def loop():
        while not _WORK_STOP.is_set():
            try:
                _fresh_routine_tick()
                _drain_work()
                _reconcile_pending_routines()
                _requested_brief_feedback()
            except Exception as error:
                print(f'[sotto] work queue: {type(error).__name__}', flush=True)
            _WORK_WAKE.wait(5)
            _WORK_WAKE.clear()
    thread = threading.Thread(target=loop, name='sotto-work-dispatch', daemon=True)
    thread.start()
    return thread


def _signal_children(sig):
    with _WORK_LOCK:
        children = list(_WORK_PROCESSES.values())
    for child in children:
        try:
            os.killpg(child.pid, sig)
        except OSError:
            pass    # already reaped, or its group is gone — nothing left to signal


def _stop_work(signum, _frame):
    _WORK_STOP.set()
    _WORK_WAKE.set()
    _signal_children(signal.SIGTERM)
    # Daemon workers do not keep the interpreter alive after SystemExit. Give the children we just
    # interrupted a bounded chance to unwind through _WorkInterruptedError and refund their lease.
    # The grace is shared FAIRLY: a worker that ignores its SIGTERM must not spend the chance the
    # worker beside it needs, and time a worker returns early is handed on to the ones still going.
    deadline = time.monotonic() + WORK_SHUTDOWN_GRACE_SECS
    with _WORK_LOCK:
        workers = list(_WORK_THREADS)
    for index, worker in enumerate(workers):
        share = (deadline - time.monotonic()) / (len(workers) - index)
        if share > 0:
            worker.join(share)
    # A child that ignored SIGTERM would otherwise be ORPHANED against the same volume and run
    # concurrently with the next instance's reclaim. Kill before refunding, so nothing this
    # instance started can still be writing once the job is claimable again.
    _signal_children(signal.SIGKILL)
    # The shutdown refunds what its workers could not. A worker interrupted mid-composition unwinds
    # through _WorkInterruptedError and releases its own lease; one already past `save_result` and
    # inside the delivery handoff has no interrupt path at all, and a send can legitimately take
    # SEND_TIMEOUT_SECS — longer than this whole grace. Left leased, that lease ages out CHARGED,
    # and a third redeploy makes claim() call the handoff budget exhausted and DESTROY the composed
    # brief. `release` is gated on `status='leased' AND owner=?`, so a worker that settled
    # concurrently makes this a no-op, and the refunded job is claimable immediately.
    with _WORK_LOCK:
        leases = list(_WORK_THREADS.values())
    for job_id, owner in leases:
        try:
            WORK_QUEUE.release(DATA, job_id, owner)
        except Exception as error:  # noqa: BLE001 — one stuck lease must not block the others
            print(f'[sotto] work queue: shutdown refund failed ({type(error).__name__})', flush=True)
    raise SystemExit(128 + signum)


# label → spawned runs still executing in this process. The wake-push's "the cron is composing right
# now, don't burn a second brief" decision reads this: a cron run that has already DIED is not
# composing, and folding the wake into the snapshot on that assumption lost the day's brief.
_RUNS_INFLIGHT: dict = {}


def _cron_run_is_dead(skill: str) -> bool:
    """Has today's receiver-cron run for this brief fired AND finished (with whatever result)?
    A wake inside the cron window folds only while the cron is genuinely composing; once its run
    is dead with no marker, the wake is the brief's last chance and must spawn."""
    today = _local_now().strftime("%Y-%m-%d")
    return _CRON_FIRED.get(skill) == today and _RUNS_INFLIGHT.get(f"cron:{skill}", 0) == 0


def _brief_model_ready() -> bool:
    """Content-free re-admission check; auth/network failures never clear a budget hold."""
    from gemini_transport import brief_model_preflight
    try:
        brief_model_preflight()
        return True
    except Exception:
        return False


def _pending_offers():
    _shared_effects()  # establishes the fixed installed shared script roots
    import pending_offer
    return pending_offer


def request_owner_meeting_prep(offer, *, admission, feedback_identity, feedback_thread_id):
    """Only the scoped authenticated owner route can admit this exact offered occurrence."""
    _activation_tick()
    evidence = _composition_evidence('meeting-prep:')
    if _deliver_target() != 'photon' or evidence is None:
        return 503, {'status': 'unavailable'}
    if not _source_context().allowed('calendar'):
        return 409, {'status': 'needs_context'}
    composer = _find_sotto_script('meeting-prep', 'scripts', 'compose_meeting_prep.py')
    if not composer:
        return 503, {'status': 'unavailable'}
    label = 'meeting-prep:' + offer['offer_id']
    binding = _skill_notice_binding(feedback_identity, feedback_thread_id, evidence)
    if binding is None:
        return 503, {'status': 'unavailable'}
    request = {'kind': 'focused-meeting-prep', 'pack': str(Path(composer).parents[2]),
               'offer': offer, 'work_key': label}
    job_id = _spawn_and_deliver([sys.executable, str(Path(__file__).with_name('procedure_runner.py'))],
                                json.dumps(request), label, admission=admission,
                                feedback={'label': label, **binding})
    return 200, {'status': 'queued', 'request_id': job_id}


def request_owner_skill(selection, *, work_key, admission, skill='meeting-prep', question=None,
                        feedback_identity=None, feedback_thread_id=None):
    """One declared shared pipeline, never a model-supplied executable or destination."""
    _activation_tick()
    label = work_key + ':' + skill
    evidence = _composition_evidence(label)
    if _deliver_target() != 'photon' or evidence is None:
        return 503, {'status': 'unavailable'}
    if admission and admission.get('source_revision') is not None and admission['source_revision'] != evidence['source_revision']:
        return 409, {'status': 'source_unavailable'}
    if skill == 'meeting-prep' and not _source_context().allowed('calendar'):
        return 409, {'status': 'source_unavailable'}
    if skill in ('relationship-pulse','proactive') and not _runtime_source_authority().get('complete'):
        return 409, {'status': 'source_unavailable'}
    if skill == 'followup':
        authority = _runtime_source_authority()
        if not authority.get('complete'):
            return 409, {'status': 'source_unavailable'}
        if 'granola' not in authority.get('connector_sources', []):
            # A missing credential is a known disconnected state. Malformed/unknown credentials
            # and withheld grants remain the ordinary source-unavailable refusal.
            if SELFHOST_SOURCE_AUTHORITY.granola_generation(Path(DATA) / 'connectors/granola.json') == SELFHOST_SOURCE_AUTHORITY.DISCONNECTED:
                return 409, {'status': 'meeting_notes_not_connected'}
            return 409, {'status': 'source_unavailable'}
    composer = (_find_sotto_script('proactive', 'scripts', 'proactive_scan.py') if skill == 'proactive'
                else _find_sotto_script('relationship-pulse', 'scripts', 'relationship_pulse.py') if skill == 'relationship-pulse'
                else _find_sotto_script('followup', 'scripts', 'compose_followup.py') if skill == 'followup'
                else _find_sotto_script('meeting-prep', 'scripts', 'compose_meeting_prep.py'))
    if not composer:
        return 503, {'status': 'unavailable'}
    request = {'kind': 'shared-meeting-prep', 'pack': str(Path(composer).parents[2]),
               'selection': selection, 'work_key': work_key}
    if skill == 'ask-research':
        request.update(kind='shared-ask-research', question=question)
    elif skill == 'followup':
        request.update(kind='shared-followup')
    elif skill == 'relationship-pulse':
        request.update(kind='shared-relationship-pulse')
    elif skill == 'proactive':
        request.update(kind='proactive')
    feedback = None
    if feedback_identity is not None:
        binding = _skill_notice_binding(feedback_identity, feedback_thread_id, evidence)
        if binding is None:
            return 503, {'status': 'unavailable'}
        feedback = {'label': label, **binding}
    job_id = _spawn_and_deliver([sys.executable, str(Path(__file__).with_name('procedure_runner.py'))],
                                json.dumps(request), label, admission=admission, feedback=feedback)
    return 200, {'status': 'queued', 'request_id': job_id}


def request_owner_brief(body, *, admitted_when=None, admission=None):
    """Called only after bearer + gateway capability authentication; never accepts a destination."""
    if (body.get('kind') not in ('morning', 'evening', 'auto')
            or type(body.get('refresh')) is not bool
            or any(not isinstance(body.get(k), str) or not 0 < len(body[k]) <= 512
                   for k in ('owner', 'chat_id', 'message_id'))):
        return 400, {'error': 'invalid brief request'}
    if 'composition_direction' in body and (not admission
            or not isinstance(body['composition_direction'], str)
            or not 0 < len(body['composition_direction']) <= 16_000):
        return 400, {'error': 'invalid composition direction'}
    telegram = os.environ.get('SOTTO_RUNTIME_PROFILE') == 'self-host-telegram-fixed-five-v1'
    if telegram:
        candidate = {'admission': json.dumps(admission) if admission else None}
        binding = _requested_telegram_job(candidate)
        if (not binding or body['owner'] != binding.get('owner_id')
                or body['chat_id'] != binding.get('owner_id')
                or not _requested_telegram_job_current(candidate, target='telegram')):
            return 403, {'error': 'owner conversation required'}
        owner = binding['owner_id']
    else:
        owner = os.environ.get('PHOTON_HOME_CHANNEL', '')
        if body['owner'] != owner or not REQUESTED_BRIEF.owner_destination(
                DATA, owner, body['chat_id'], os.environ.get('SOTTO_TENANT_ID')):
            return 403, {'error': 'owner conversation required'}
        if _deliver_target() not in ('photon', 'photon:' + owner, 'photon:' + body['chat_id']):
            return 409, {'error': 'owner messaging is unavailable'}
        # New Photon owner DMs renew activation; Telegram uses its fixed pairing proof.
        _activation_tick()
    now = admitted_when or _local_now()
    kind = body['kind'] if body['kind'] != 'auto' else ('morning' if now.hour < 12 else 'evening')
    ident = REQUESTED_BRIEF.identity(owner, body['chat_id'], body['message_id'])
    label = f'requested:{ident}:sotto-{kind}-brief'
    prior = WORK_QUEUE.get(DATA, WORK_QUEUE.job_id_for('run', 'requested:' + ident))
    if prior is not None:
        return 200, {'status': 'known_request', 'request_id': prior['id']}
    # Same-edition or ambiguous uncertainty stays held; an exact opposite edition is unrelated.
    with CONNECTORS.json_transaction(OUTBOX.path(), default={}) as state:
        if any(row.get('day') == str(now.date()) and row.get('status') == 'pending'
               and row.get('kind') == 'brief'
               and REQUESTED_BRIEF.known_brief_kind(row.get('payload')) not in
                   ({'morning', 'evening'} - {kind})
               and (row.get('acceptance_uncertain') or row.get('acceptance') in ('unknown', 'in_flight'))
               for row in state.get('rows', [])):
            return 200, {'status': 'pending_delivery'}
    import onboarding
    if onboarding.scheduled_hold(DATA) or _fresh_selfhost_brief_hold():
        return 200, {'status': 'needs_context'}
    composer = _find_sotto_script('_shared', 'scripts', 'compose_brief.py')
    if not composer:
        return 503, {'error': 'brief composer unavailable'}
    if telegram and not _requested_telegram_job_current(candidate, target='telegram'):
        return 503, {'error': 'owner conversation changed'}
    request = {'kind': kind, 'pack': str(Path(composer).parents[2]), 'day': str(now.date()),
               'work_key': label, 'refresh': body['refresh']}
    if body.get('composition_direction'):
        request['composition_direction'] = body['composition_direction']
    job_id = _spawn_and_deliver([sys.executable, str(Path(__file__).with_name('brief_runner.py'))],
                                json.dumps(request), label, feedback={
                                    'label': label, 'owner': owner, 'chat_id': body['chat_id'],
                                    'target': _deliver_target()}, admission=admission)
    return 200, {'status': 'queued', 'request_id': job_id}




def _managed_brief(skill: str, label: str, payload_path: str = "",
                   work_not_before: float | None = None, retry_terminal: bool = False,
                   fixed_recovery_from: str | None = None) -> bool:
    if skill not in MARKED_BRIEF_KINDS:
        return False
    composer = _find_sotto_script("_shared", "scripts", "compose_brief.py")
    if not composer:
        raise FileNotFoundError("Sotto composer is missing")
    pack = os.path.dirname(os.path.dirname(os.path.dirname(composer)))
    kind = MARKED_BRIEF_KINDS[skill]
    day = str(_local_now().date())
    work_key = f'{day}:{kind}:{_brief_revision(kind)}' if not label.startswith('run-now:') else secrets.token_hex(12)
    request = {'kind': kind, 'pack': pack, 'payload_path': payload_path,
               'day': day, 'work_key': work_key}
    import onboarding
    if label == onboarding.DISCOVERY_LABEL:
        request.update(history_discovery=True, work_key=_job_key(label, ''))
    queue_key = _job_key(label, '')
    prior = WORK_QUEUE.get(DATA, WORK_QUEUE.job_id_for('run', queue_key)) if queue_key is not None else None
    if prior and prior['status'] == 'failed' and prior.get('error') == WORK_QUEUE.MODEL_BUDGET_EXHAUSTED:
        if not _brief_model_ready():
            import onboarding
            if label == onboarding.DISCOVERY_LABEL:
                return False
            if label == onboarding.LABEL:
                onboarding.model_budget_held(DATA)
                return False  # Do not claim a welcome was admitted when nothing was queued.
            return True  # Handled, but no duplicate gather or new failed attempt while held.
        retry_terminal = True  # The existing request may resume only after confirmed availability.
    if label.startswith('cron:'):
        schedule = next((job[1] for job in _sotto_cron_jobs() if job[0] == skill), '')
        at = _fixed_daily_minute(schedule)
        if at is not None:
            request['deliver_not_before'] = _local_now().replace(hour=at[0], minute=at[1], second=0, microsecond=0).timestamp()
        # ONE composition per day per kind. The key carries the context revision, so a calendar
        # edit between the T-10 admission and the T fire minted a second key — and both ran, a
        # full duplicate gather+compose for the send seam to supersede. An admitted job nobody
        # has started is replaced; one already composing (or composed) is left to deliver, and
        # if its context really is stale the outbox supersedes it and recomposes from here.
        if not _selected_fixed_profile():
            active = WORK_QUEUE.active_job(DATA, 'run', label)
            if active is not None and active['id'] != WORK_QUEUE.job_id_for('run', _job_key(label, '')):
                if active['status'] != 'pending' or not WORK_QUEUE.discard_pending(DATA, active['id']):
                    return True
    _spawn_and_deliver([sys.executable, os.path.join(os.path.dirname(__file__), "brief_runner.py")],
                       json.dumps(request), label, work_not_before=work_not_before,
                       retry_terminal=retry_terminal, fixed_recovery_from=fixed_recovery_from)
    return True


def run_skill(skill: str, payload_path: str) -> None:
    # A Bridge wake carries a newly staged source snapshot. It is explicit new ingress, so a prior
    # terminal admission for this deterministic day/revision may receive one fresh bounded budget.
    if not _managed_brief(skill, f"brief:{skill}", payload_path, retry_terminal=True):
        raise RuntimeError('Sotto unattended skill procedure is unsupported')


def _claim_is_stale(flag: str, date: str, kind_short: str) -> bool:
    """An existing claim is STALE iff the skill never delivered (no `.delivered` marker from
    brief_marker.py) AND the claim is older than CLAIM_STALE_SECS. Covers the silent-loss mode where
    Popen succeeded but the spawned run died before delivering — the claim used to block the whole
    day. brief_marker's deliver-once gate still guarantees at most one send."""
    if os.path.exists(delivered_marker(date, kind_short)):
        return False
    try:
        return (time.time() - os.path.getmtime(flag)) > CLAIM_STALE_SECS
    except OSError:
        return False


SEED_SNAPSHOT_TIMEOUT_SECS = 120  # file IO + one JSON rewrite; no LLM, no network


def _seed_snapshot_from(payload_path: str) -> bool:
    """Fold a staged wake payload into the local snapshot via compose_brief.py --seed-snapshot —
    the already-delivered path of handle_trigger. Deterministic and LLM-free: the same writer the
    brief uses (_save_local_snapshot), so contacts carry-forward and file shape can't diverge.
    Runs on a daemon thread (the Bridge's POST shouldn't wait on it); failure is logged, never
    raised — the snapshot just stays one day staler and the next delivered brief heals it."""
    script = _find_sotto_script("_shared", "scripts", "compose_brief.py")
    if not script:
        print("[sotto] snapshot seed: compose_brief.py not found in any skills tree", flush=True)
        return False
    try:
        r = subprocess.run([sys.executable, script, "--seed-snapshot", payload_path],
                           capture_output=True, text=True, env=_skill_env(),
                           timeout=SEED_SNAPSHOT_TIMEOUT_SECS)
        out = (r.stdout or "").strip().splitlines()
        print(f"[sotto] snapshot seed from {os.path.basename(payload_path)}: "
              f"{out[-1] if out else f'exit {r.returncode}'}", flush=True)
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001
        print(f"[sotto] snapshot seed FAILED ({type(e).__name__}: {e})", flush=True)
        return False


# Serializes the claim/stale-check/reclaim sequence across ThreadingHTTPServer threads: the
# remove-then-O_EXCL-create window in the stale path let two concurrent triggers BOTH reclaim
# (thread A removes, A and B both create in turn) → duplicate brief spawns.
_CLAIM_LOCK = threading.Lock()


def run_proactive_skill() -> bool:
    return _run_proactive('proactive')


def _run_proactive(label: str) -> bool:
    """Both wake and scheduled checks gather and decide before starting the agent composer."""
    if not _delivery_channel_ready(label):
        return False
    script = _find_sotto_script('proactive', 'scripts', 'proactive_scan.py')
    if not script:
        raise FileNotFoundError('Sotto proactive scanner missing')
    pack = Path(script).parents[2]
    request = {'pack': str(pack), 'kind': 'proactive'}
    _spawn_and_deliver([sys.executable, str(Path(__file__).with_name('procedure_runner.py'))],
                       json.dumps(request), label)
    return True


# Server-side throttle for event-driven proactive wakes: the Bridge already throttles to once per 30
# min, but a retry or a second Mac could still double-fire — collapse anything inside this window.
# INVARIANT: must stay BELOW the Bridge's companion WAKE_THROTTLE_SECS (30 min). This is only a
# backstop; if it were wider than the Bridge window it would reject a wake the Bridge legitimately
# re-fires (a real run silently dropped as "throttled").
PROACTIVE_THROTTLE_SECS = 25 * 60


def _proactive_wake_marker() -> str:
    return os.path.join(DATA, "proactive", "wake_run.last")


def handle_proactive_wake() -> tuple[int, dict]:
    # Event-driven proactive nudge (Phase 2b): the Bridge POSTs {type:proactive_wake} on sleep→wake.
    # No date/local_data staging — the proactive skill reads live state. We only add a server-side
    # throttle (mtime of a marker) and run the skill; quiet hours + once-per-day nudge dedup are NOT
    # duplicated here (they live deterministically in proactive_scan.py). Serialized under the same
    # _CLAIM_LOCK as the brief claims so concurrent wakes can't both slip past the throttle.
    marker = _proactive_wake_marker()
    with _CLAIM_LOCK:
        try:
            if (time.time() - os.path.getmtime(marker)) < PROACTIVE_THROTTLE_SECS:
                return 200, {"status": "throttled"}
        except OSError:
            pass  # no marker yet → first run, fall through
        # Stamp BEFORE spawning so a burst of near-simultaneous wakes throttles the rest immediately.
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        try:
            with open(marker, "w") as f:
                f.write(str(int(time.time())))
        except OSError:
            pass
    try:
        spawned = run_proactive_skill()
    except Exception as e:  # noqa: BLE001
        # Mirror handle_trigger's claim-release: the spawn failed, so un-stamp the throttle marker
        # (best-effort, under the same lock) before returning the 500. Otherwise the Bridge — which
        # correctly un-stamps itself on a non-2xx — retries on the next wake, gets 200 {"throttled"}
        # off this stale marker, stamps itself, and BOTH sides record a run that never happened.
        with _CLAIM_LOCK:
            try:
                os.remove(marker)
            except OSError:
                pass
        return 500, {"error": f"enqueue failed: {e}"}
    if spawned is False:
        # The delivery channel isn't linked, so nothing ran and nothing was spent. Un-stamp the
        # throttle (same reasoning as the spawn-failure path): the next wake should get a real try
        # once the link is back, not a phantom "throttled" off a run that never happened.
        with _CLAIM_LOCK:
            try:
                os.remove(marker)
            except OSError:
                pass
        return 200, {"status": "skipped", "reason": "delivery channel not linked"}
    return 202, {"status": "enqueued", "skill": "sotto-proactive"}


# A compose takes 3–5 minutes, so a wake this soon after the cron fired is almost certainly racing a
# run that is still writing; ten minutes covers it without meaningfully delaying a genuinely missed
# brief, because the NEXT wake outside the window spawns exactly as it does today. The receiver's own
# cron tick fires inside this same window, so the two halves of the brief lane cannot disagree about
# when "the cron is firing right now" is true.
BRIEF_CRON_WINDOW_MIN = 10
# The two cron shapes this module reads: the fixed daily `M H * * *` the briefs and the digest use,
# and the `*/N * * * *` interval the proactive watcher uses. Anything else (a weekday list) means we
# cannot say when it fires, so no window guard and no receiver-run fire at all.
_FIXED_DAILY_CRON_RE = re.compile(r"\A(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\Z")
_FIXED_WEEKLY_CRON_RE = re.compile(r"\A(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+([0-7])\Z")
_INTERVAL_CRON_RE = re.compile(r"\A\*/(\d{1,2})\s+\*\s+\*\s+\*\s+\*\Z")


def _local_now():
    """Now in the user's zone, through tzchain — THE chain, so the cron window, the dashboard's
    `_local_today`, the marker and the skills can never disagree about what day it is. Aware in
    the configured zone; naive UTC when none is configured (the shape every caller here expects)."""
    tz = TZCHAIN.resolve(_configured_tz_name())
    return datetime.now(tz) if tz else datetime.now(timezone.utc).replace(tzinfo=None)


def _fixed_daily_minute(schedule) -> tuple[int, int] | None:
    """(hour, minute) for a fixed daily `M H * * *` schedule, else None. THE one cron parse on this
    side — the wake-push window guard and the receiver's own scheduler read the same shape, so they
    can never disagree about when a job fires."""
    match = _FIXED_DAILY_CRON_RE.match(str(schedule or "").strip())
    if not match:
        return None
    minute, hour = int(match.group(1)), int(match.group(2))
    return (hour, minute) if 0 <= hour < 24 and 0 <= minute < 60 else None


def _fixed_weekly_minute(schedule) -> tuple[int, int, int] | None:
    """(hour, minute, cron weekday) for M H * * D; Sunday is 0 or 7."""
    match = _FIXED_WEEKLY_CRON_RE.match(str(schedule or "").strip())
    if not match:
        return None
    minute, hour, weekday = map(int, match.groups())
    return (hour, minute, weekday % 7) if 0 <= hour < 24 and 0 <= minute < 60 else None


def _interval_minutes(schedule) -> int | None:
    """N for a `*/N * * * *` interval schedule (1–59), else None."""
    match = _INTERVAL_CRON_RE.match(str(schedule or "").strip())
    if not match:
        return None
    every = int(match.group(1))
    return every if 0 < every < 60 else None


def _cron_slot(schedule) -> str | None:
    """The slot this schedule is firing in RIGHT NOW, or None outside one. A slot is the identity
    of one scheduled fire: the local date for a fixed daily or weekly job, the date plus the boundary minute
    for an interval job — so "already fired this slot" means the same thing for both shapes, and
    a `*/15` job fires once per quarter-hour, not once per day and not ten times per window.

    Fixed daily/weekly: the minute arrived less than BRIEF_CRON_WINDOW_MIN minutes ago;
    weekly jobs also require their configured local weekday. Interval: the most
    recent multiple of N minutes, inside that same window (a boot eight minutes past the quarter
    still fires it; a `*/5` cadence is shorter than the window and simply owns each boundary)."""
    now = _local_now()
    at = _fixed_daily_minute(schedule)
    weekly = _fixed_weekly_minute(schedule)
    if weekly is not None:
        if (now.weekday() + 1) % 7 != weekly[2]:
            return None
        at = weekly[:2]
    if at is not None:
        since = (now - now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)).total_seconds()
        return now.strftime("%Y-%m-%d") if 0 <= since < BRIEF_CRON_WINDOW_MIN * 60 else None
    every = _interval_minutes(schedule)
    if every is None:
        return None
    minute_of_day = now.hour * 60 + now.minute
    boundary = minute_of_day - (minute_of_day % every)
    if minute_of_day - boundary >= min(every, BRIEF_CRON_WINDOW_MIN):
        return None
    return f"{now.strftime('%Y-%m-%d')}T{boundary // 60:02d}:{boundary % 60:02d}"


DAILY_CATCHUP_SECONDS = 4 * 3600
BRIEF_PREPARE_SECONDS = 10 * 60
BRIEF_COMPOSE_LEAD_SECONDS = 2 * 60


def _scheduled_slot(schedule):
    now = _local_now()
    at = _fixed_daily_minute(schedule)
    weekly = _fixed_weekly_minute(schedule)
    if weekly:
        if (now.weekday() + 1) % 7 != weekly[2]:
            return None
        at = weekly[:2]
    if at is None:
        return _cron_slot(schedule)
    due = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    return str(now.date()) if 0 <= (now - due).total_seconds() < DAILY_CATCHUP_SECONDS else None


def _prepare_briefs():
    now = _local_now()
    import managed
    import onboarding
    if (managed.brief_hold(DATA) or onboarding.scheduled_hold(DATA)
            or _fresh_selfhost_brief_hold()):
        return
    composer = _find_sotto_script('_shared', 'scripts', 'compose_brief.py')
    if not composer:
        return
    pack = str(Path(composer).parents[2])
    for name, schedule, _prompt, skill in _sotto_cron_jobs():
        if _CRON_FIRED.get(name) == str(now.date()):
            continue
        at = _fixed_daily_minute(schedule)
        if skill not in MARKED_BRIEF_KINDS or at is None:
            continue
        due = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
        if not 0 < (due - now).total_seconds() <= BRIEF_PREPARE_SECONDS:
            continue
        kind = MARKED_BRIEF_KINDS[skill]
        if os.path.exists(delivered_marker(str(now.date()), kind)):
            continue
        # Admit composition now for durability, but keep it unclaimable while preparation gets an
        # eight-minute head start. At T-2 composition proceeds with whatever prep is committed and
        # still has time to reach the outbox before the declared delivery minute.
        try:
            import onboarding
            with _CLAIM_LOCK:
                if _welcome_admission_active():
                    continue
                _managed_brief(skill, f'cron:{name}',
                               work_not_before=due.timestamp() - BRIEF_COMPOSE_LEAD_SECONDS)
        except Exception as error:
            if _is_terminal_work(error):
                _CRON_FIRED[name] = str(now.date())
            print(f'[sotto] prep {name}: {type(error).__name__}', flush=True)
            continue
        request = {'kind': kind, 'pack': pack, 'prepare': True, 'day': str(now.date()),
                   'work_key': f'{now.date()}:{kind}:{_brief_revision(kind)}'}
        try:
            WORK_QUEUE.enqueue(DATA, 'run', {
                'runner': [sys.executable, str(Path(__file__).with_name('brief_runner.py'))],
                'prompt': json.dumps(request), 'label': f'background:prepare:{name}',
                'decision_ids': []},
                key=f'prepare:{name}:{now.date()}:{_brief_revision(kind)}', priority=60,
                valid_until=due.timestamp())
        except Exception as error:
            if _is_terminal_work(error):
                _CRON_FIRED[name] = str(now.date())
            print(f'[sotto] prep {name}: {type(error).__name__}', flush=True)
    _WORK_WAKE.set()


def _fires_now(schedule) -> bool:
    """True when this schedule is inside a fire window right now, in the user's zone. It is both
    "the cron is firing right now" (the wake-push guard) and "fire it now" (the receiver's tick) —
    one statement, because the receiver is the thing that fires."""
    return _cron_slot(schedule) is not None


def _in_brief_cron_window(skill: str) -> bool:
    """True when this brief's scheduled minute is less than BRIEF_CRON_WINDOW_MIN minutes past — i.e.
    the receiver's own cron tick has fired it and that run is very likely still composing right now.
    FAIL-OPEN in every uncertainty: an unreadable crons.json, a job that isn't there, or a schedule
    that isn't the fixed daily shape all answer False, which is today's behaviour unchanged."""
    for name, sched, _prompt, _skill in _sotto_cron_jobs():
        if name == skill:
            return _fires_now(sched)
    return False


def _fold_into_snapshot(kind: str, date: str, local: dict, status: str,
                        detail: str) -> tuple[int, dict]:
    """Stage the wake payload and fold it into the local snapshot INSTEAD of composing a brief —
    the ONE seeding path both no-second-brief cases take (the day's brief already went out, or the
    cron that is about to send it is still composing). Deterministic and LLM-free; from here the
    funnel does the surfacing, so nothing in the payload is lost by not composing."""
    payload_path = os.path.join(DATA, "briefs", f"{date}.{kind}.payload.json")
    try:
        os.makedirs(os.path.join(DATA, "briefs"), exist_ok=True)
        with open(payload_path, "w") as f:
            json.dump(local, f)
    except Exception as e:  # noqa: BLE001
        return 500, {"error": f"payload stage failed: {e}"}
    threading.Thread(target=_seed_snapshot_from, args=(payload_path,), daemon=True).start()
    _record_delivery(f"brief:{SKILL[kind]}", "skipped", detail)
    return 200, {"status": status, "snapshot": "seeding"}


def handle_trigger(body: dict) -> tuple[int, dict]:
    # The heartbeat's welcome reservation and wake's brief enqueue share this
    # admission lock. A wake already committed to the durable queue is checked
    # by the next welcome tick; a reserved welcome makes the wake fold context.
    if body.get('type') in SKILL:
        with _CLAIM_LOCK:
            return _handle_trigger(body)
    return _handle_trigger(body)


def _handle_trigger(body: dict) -> tuple[int, dict]:
    kind = body.get("type")
    if kind == "proactive_wake":  # event-driven proactive nudge — no date/payload needed
        return handle_proactive_wake()
    if kind not in SKILL:
        return 400, {"error": "unknown type"}
    date = body.get("date") or ""
    if not DATE_RE.match(date):
        return 400, {"error": "bad date"}
    import managed
    reason = managed.brief_hold(DATA)
    if reason:
        return 200, {'status': 'held', 'reason': reason}
    kind_short = kind.replace("_ready", "")
    if _welcome_admission_active():
        local = body.get('local_data') or {}
        if not local:
            return 200, {'status': 'onboarding'}
        return _fold_into_snapshot(kind, date, local, 'onboarding',
                                   'first brief is arriving — wake payload folded into the snapshot')
    # ── Brief already delivered? Fold the wake payload into the snapshot instead. ──────────────────
    # The owner's design (Aug 2026): the 6:31 cron brief goes out even when the Mac was asleep
    # (degraded to the snapshot), and it is THE brief for the day. When the Bridge wakes hours later
    # and pushes {kind}_ready with fresh local_data, composing again would either duplicate the brief
    # or (what actually happened) burn a full compose only to lose brief_marker's deliver-once claim
    # and discard the text. So: no second brief, ever. The fresh payload still matters — it carries
    # today's contacts/messages/calls — so seed it into the local snapshot via compose_brief.py
    # --seed-snapshot (deterministic, no LLM; the same writer the brief uses, so carry-forward and
    # shape can't diverge). From there the funnel does the surfacing: triage names senders from the
    # healed snapshot, and the midday digest/nudges flag what the morning brief missed.
    if os.path.exists(delivered_marker(date, kind_short)):
        local = body.get("local_data") or {}
        if not local:
            return 200, {"status": "already_delivered"}
        return _fold_into_snapshot(kind, date, local, "already_delivered",
                                   "already delivered — wake payload folded into the snapshot; "
                                   "nudges/digest surface the catch-up")
    if _fresh_selfhost_brief_hold():
        local = body.get('local_data') or {}
        if not local:
            return 200, {'status': 'held', 'reason':
                         'Connect a context source and delivery channel for your first brief'}
        return _fold_into_snapshot(kind, date, local, 'waiting_for_setup',
                                   'first brief waits for source and delivery setup; wake payload '
                                   'folded into the snapshot')
    # ── Inside the cron's own window? Its run is composing; don't burn a second one. ──────────────
    # The Aug 30 double-delivery started here: the Mac woke at 17:31, one minute into the 17:30
    # cron's compose, and with no `.delivered` marker yet (the cron claims just before it SENDS)
    # this trigger spawned a second full brief. The send seam now refuses to deliver that second
    # copy — but composing it at all costs minutes and real tokens for words nobody will read. So a
    # wake this close behind the cron presumes the cron run is in flight and takes the exact same
    # fold-into-the-snapshot path as an already-delivered day. A wake OUTSIDE the window still
    # spawns, which is what keeps a genuinely missed cron brief from being lost.
    if _in_brief_cron_window(SKILL[kind]) and not _cron_run_is_dead(SKILL[kind]):
        local = body.get("local_data") or {}
        if not local:
            return 200, {"status": "cron_window"}
        return _fold_into_snapshot(kind, date, local, "cron_window",
                                   f"the {kind_short} cron fired under {BRIEF_CRON_WINDOW_MIN} min "
                                   "ago and is still composing — wake payload folded into the "
                                   "snapshot instead of a second brief")
    flag = delivered_flag(date, kind_short)
    os.makedirs(os.path.dirname(flag), exist_ok=True)
    # Atomically CLAIM this (date, kind) so two near-simultaneous triggers (e.g. cron + wake-push, or a
    # retry) can't both enqueue → duplicate briefs. O_EXCL is the atomic guard the old exists()+open()
    # check raced on; _CLAIM_LOCK closes the remaining remove→re-create window in the stale-reclaim
    # path. We release the claim if enqueue fails, so a misconfigured runner never silently
    # suppresses the day's brief (the original intent).
    # handle_trigger holds _CLAIM_LOCK through this durable queue admission.
    try:
        os.close(os.open(flag, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        if not _claim_is_stale(flag, date, kind_short):
            return 200, {"status": "already_delivered"}
        # Stale claim, brief never delivered: release it and re-claim.
        try:
            os.remove(flag)
        except OSError:
            pass
        try:
            os.close(os.open(flag, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except (FileExistsError, OSError):
            return 200, {"status": "already_delivered"}
        print(f"[sotto] stale claim for {date} {kind_short}: no .delivered after "
              f"{CLAIM_STALE_SECS // 60} min — retrying the brief", flush=True)
    payload_path = os.path.join(DATA, "briefs", f"{date}.{kind}.payload.json")
    # The payload write shares the enqueue's claim-release guard: an OSError here (full/read-only
    # volume) must not leave the .claim held, or briefs are blocked for CLAIM_STALE_SECS.
    try:
        os.makedirs(os.path.join(DATA, "briefs"), exist_ok=True)
        with open(payload_path, "w") as f:
            json.dump(body.get("local_data") or {}, f)
        run_skill(SKILL[kind], payload_path)
    except Exception as e:  # noqa: BLE001
        try:
            os.remove(flag)   # release the claim so a later trigger can retry
        except OSError:
            pass
        return 500, {"error": f"enqueue failed: {e}"}
    return 202, {"status": "enqueued", "skill": SKILL[kind]}


# ── Event ingestion (Phase 2): POST /bridge/events → dedupe → triage → maybe agent ────────────────
# The Bridge watcher (and the Gmail poll thread below) push raw events here; the deterministic
# triage funnel (sotto-chief-of-staff/event-triage/scripts/triage_event.py) runs SYNCHRONOUSLY and
# its verdict is the 200 body. Verdict "agent" additionally stages a bundle and queues the
# notification composer as durable event work.

EVENTS_SEEN_MAX = 2000            # ring size for (source,rowid) idempotency keys
TRIAGE_TIMEOUT_SECS = 30          # Tier 0 is sub-second; Tier 1 is one Flash-Lite call
# The synthetic-event source strings this process mints (see handle_events' scrub). The two
# calendar ones come from calcache — the contract's single source on this side; "proactive" is
# triage_event.PROACTIVE_SOURCE, in the skills tree this image can't import.
RESERVED_SYNTHETIC_SOURCES = frozenset(
    {CALCACHE.MEETING_END_SOURCE, CALCACHE.CALENDAR_CHANGE_SOURCE, "proactive"})
_EVENTS_LOCK = threading.Lock()   # serializes seen-ring read/modify/write across handler threads
_EVENTS_INFLIGHT: set[str] = set()  # check-through-accept claims; process-local handler concurrency
EVENT_BUNDLE_RETENTION_SECS = 7 * 24 * 3600


def _events_dir() -> str:
    return os.path.join(DATA, "events")


def _seen_path() -> str:
    return os.path.join(_events_dir(), "seen.json")


def _load_seen() -> list:
    try:
        with open(_seen_path(), encoding="utf-8") as f:
            v = json.load(f)
        return [str(x) for x in v] if isinstance(v, list) else []
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def _save_seen(keys: list) -> None:
    """Capped ring, written through THE atomic helper — a crash mid-write can't corrupt the ring."""
    CONNECTORS.write_json(_seen_path(), keys[-EVENTS_SEEN_MAX:])


def _event_stamp_path() -> str:
    return os.path.join(_events_dir(), "last.stamp")


def _touch_event_stamp() -> None:
    """Best-effort 'events are flowing' stamp — its mtime is the last accepted event, surfaced on
    /setup so a linked-but-silent Bridge is distinguishable from a working one."""
    try:
        os.makedirs(_events_dir(), exist_ok=True)
        with open(_event_stamp_path(), "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def _last_event_at():
    """ISO-8601 UTC of the last accepted event, or None when no event has ever landed."""
    try:
        ts = os.path.getmtime(_event_stamp_path())
    except OSError:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _event_key(e: dict):
    """(source,rowid) idempotency key, or None when the event carries neither (undedupable —
    treated as always fresh; triage's cooldown still bounds repeats)."""
    src, rowid = e.get("source"), e.get("rowid")
    if not src or rowid in (None, ""):
        return None
    generation = e.get("store_generation")
    if src in ("imessage", "whatsapp", "calls") and isinstance(generation, str) and re.fullmatch(r"[a-f0-9]{64}", generation):
        return f"{src}:{generation}:{rowid}"
    return f"{src}:{rowid}"


# Where the skills tree lives, resolved ONCE per script name. The recursive glob below is not cheap
# and the answer only changes on a redeploy (which restarts this process), so a plain dict is the
# whole cache — there is nothing to invalidate. SOTTO_SKILLS_ROOT names the packaged skills tree
# (the supervisor sets /app/sotto-skills), and everything that shells out to it (triage, the Gmail
# poll, the calendar gather, dashboard fact edits) reads from there.
_SCRIPT_CACHE: dict = {}


def _find_sotto_script(*rel):
    """Locate a sotto chief-of-staff skill script: SOTTO_SKILLS_ROOT (when set) first, then the
    repo-relative source tree (tests / source checkouts). Memoized per script name — see
    _SCRIPT_CACHE."""
    import glob
    key = tuple(rel)
    if key in _SCRIPT_CACHE:
        return _SCRIPT_CACHE[key]
    bases = [b for b in (os.environ.get("SOTTO_SKILLS_ROOT") or "").strip().split(os.pathsep) if b]
    found = None
    for base in bases:
        hits = glob.glob(os.path.join(base, "**", *rel), recursive=True)
        if hits:
            found = hits[0]
            break
    if found is None:
        local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                             "sotto-chief-of-staff", *rel)
        found = local if os.path.exists(local) else None
    _SCRIPT_CACHE[key] = found
    return found


def _skill_env() -> dict:
    """ONE subprocess policy for every skills-tree script this module forks: run them on
    sys.executable (THIS interpreter — `shutil.which("python")` could pick a different one that
    lacks the Google client libs; start.sh's googleapiclient self-heal exists because interpreter
    ambiguity already bit once) and pass SOTTO_DATA explicitly so a child reads the same volume the
    receiver serves, whatever the parent env says. Same pattern dashboard._run_knowledge_edit and
    calcache._run_calendar_gather already use."""
    return {**os.environ, "SOTTO_DATA": DATA}


def run_triage(events: list, catchup: bool) -> dict:
    """Run triage_event.py synchronously (events JSON on stdin → verdict JSON on stdout). Raises on
    any failure so handle_events can 500 claim-free (events not marked seen → the Bridge retries)."""
    script = _find_sotto_script("event-triage", "scripts", "triage_event.py")
    if not script:
        raise RuntimeError("triage_event.py not found in this image")
    r = subprocess.run([sys.executable, script],
                       input=json.dumps({"events": events, "catchup": bool(catchup)}),
                       capture_output=True, text=True, timeout=TRIAGE_TIMEOUT_SECS,
                       env=_skill_env())
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "triage_event.py failed").strip()[:400])
    out = json.loads(r.stdout or "{}")
    if not isinstance(out, dict) or "verdict" not in out:
        raise RuntimeError("triage_event.py returned no verdict")
    return out


def _event_request(bundle_path: str) -> dict:
    script = _find_sotto_script('_shared', 'scripts', 'compose_notification.py')
    if not script:
        raise FileNotFoundError('notification composer missing')
    runner = [sys.executable, script]
    prompt = json.dumps({'kind': 'event', 'bundle_path': bundle_path})
    decision_ids = []
    try:
        with open(bundle_path, encoding="utf-8") as f:
            bundle = json.load(f) or {}
        decision_ids = [e.get("decision_id") for e in (bundle.get("events") or [])
                        if isinstance(e, dict) and e.get("decision_id")]
    except (OSError, ValueError, TypeError):
        pass
    return {'runner': runner, 'prompt': prompt, 'label': 'event', 'decision_ids': decision_ids}


def run_event_skill(bundle_path: str) -> None:
    with open(bundle_path, encoding='utf-8') as stream:
        bundle = json.load(stream)
    events = bundle.get('events') or []
    identities = sorted(str(_event_key(event) or json.dumps(event, sort_keys=True)) for event in events)
    WORK_QUEUE.enqueue(DATA, 'event', {'bundle': bundle}, key='events:' + '|'.join(identities),
                       valid_until=time.time() + 4 * 3600, priority=20, retry_terminal=True)
    _WORK_WAKE.set()


def _stage_bundle(bundle: dict) -> str:
    """Write an event bundle under $SOTTO_DATA/events/ and return its path. Raises OSError on a
    failed write — callers decide whether that's a 500 (handle_events) or a logged skip (valve)."""
    os.makedirs(_events_dir(), exist_ok=True)
    now = time.time()
    for name in os.listdir(_events_dir()):
        if not (name.startswith("bundle-") and (name.endswith(".json") or ".json.tmp." in name)):
            continue
        path = os.path.join(_events_dir(), name)
        try:
            if now - os.path.getmtime(path) > EVENT_BUNDLE_RETENTION_SECS:
                os.unlink(path)
        except OSError:
            pass
    bundle_path = os.path.join(_events_dir(), f"bundle-{secrets.token_hex(12)}.json")
    CONNECTORS.write_json(bundle_path, bundle or {})
    return bundle_path


def _spawn_event_agent(bundle_path: str) -> None:
    """Durable handoff is synchronous and fast; no acknowledged event is thread-owned."""
    run_event_skill(bundle_path)


def handle_events(body: dict) -> tuple[int, dict]:
    """POST /bridge/events (also fed by the Gmail poll thread): dedupe (source,rowid) against the
    seen ring, triage synchronously, return the verdict as the 200 body. CLAIM-FREE failure
    containment (mirrors handle_trigger): events are marked seen only after the pipeline succeeded,
    so a Bridge retry after a 500 re-triages them instead of silently losing them (triage's own
    per-thread cooldown bounds double-nudges on the rare re-run)."""
    events = body.get("events")
    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        return 400, {"error": "bad events"}
    # Historical withdrawal is disabled: v1 refs change with timezone/alias display and
    # cannot prove a deletion across store resets. Acknowledge old queued metadata without
    # loading the graph so a mixed-version Bridge can still deliver ordinary events.
    retraction_status = ({'source_retractions': {'status': 'disabled', 'applied': 0}}
                         if 'source_retractions' in body else {})
    retraction_status.update(_admit_source_corrections(body))
    # Synthetic sources are minted ONLY inside this process (calcache's meeting tap + calendar
    # diff, the proactive watcher's in-process tick) and carry gate exemptions plus field-trust —
    # the funnel reads a synthetic event's own kind/class as truth. An event arriving over HTTP
    # claiming one of those source strings would inherit all of that, so it is re-labeled and
    # triaged as ordinary content with zero exemptions. Bearer auth makes the spoof unlikely;
    # this makes it worthless.
    for e in events:
        if str(e.get("source") or "").strip().lower() in RESERVED_SYNTHETIC_SOURCES:
            e["source"] = "unknown"
    fresh, keys = [], []
    with _EVENTS_LOCK:
        seen_set = set(_load_seen())
        for e in events:
            k = _event_key(e)
            if k is not None and (k in seen_set or k in _EVENTS_INFLIGHT or k in keys):
                continue
            fresh.append(e)
            if k is not None:
                keys.append(k)
        _EVENTS_INFLIGHT.update(keys)
    if not fresh:
        return 200, {"verdict": "drop", "reason": "duplicate events (seen or in flight)", "bundle": {}, **retraction_status}
    try:
        try:
            verdict = run_triage(fresh, bool(body.get("catchup")))
        except Exception as e:  # noqa: BLE001
            return 500, {"error": f"triage failed: {e}"}
        if verdict.get('job_id'):
            _WORK_WAKE.set()
        elif verdict.get("verdict") == "agent":
            try:
                bundle_path = _stage_bundle(verdict.get("bundle") or {})
                _spawn_event_agent(bundle_path)
            except Exception as e:
                return 500, {"error": f"event handoff failed: {type(e).__name__}"}
        with _EVENTS_LOCK:
            try:
                _save_seen(_load_seen() + keys)
            except OSError:
                pass  # dedupe is best-effort; a lost ring write only risks a re-triage, never a loss
        _touch_event_stamp()   # fresh events made it through the pipeline — /setup can say so
        return 200, {**verdict, **retraction_status}
    finally:
        with _EVENTS_LOCK:
            _EVENTS_INFLIGHT.difference_update(keys)


# ── Gmail poll thread (Phase 2): server-side email events, no Pub/Sub ─────────────────────────────

def _email_poll_secs() -> int:
    try:
        return int((os.environ.get("SOTTO_EMAIL_POLL_SECS") or "").strip() or 90)
    except ValueError:
        return 90


def _poll_gmail_once() -> list:
    """One poll_gmail.py run → email events. RAISES on what's distinguishable at this layer (script
    missing, exec failure, non-zero exit, non-list/bad JSON) so the loop can count consecutive
    failures — a quiet mailbox and a broken poll must not both look like []."""
    script = _find_sotto_script("event-triage", "scripts", "poll_gmail.py")
    if not script:
        raise RuntimeError("poll_gmail.py not found in this image")
    r = subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=180,
                       env=_skill_env())
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "poll_gmail.py failed").strip()[:400])
    out = json.loads(r.stdout or "[]")
    if not isinstance(out, list):
        raise RuntimeError("poll_gmail.py returned non-list JSON")
    return out


def _ack_gmail_events(events: list) -> None:
    """Commit Gmail's cursor only after handle_events accepted the batch."""
    ids = [str(e.get("rowid") or "").strip() for e in events if isinstance(e, dict)]
    ids = [v for v in ids if v]
    if not ids:
        return
    script = _find_sotto_script("event-triage", "scripts", "poll_gmail.py")
    if not script:
        raise RuntimeError("poll_gmail.py not found while acknowledging events")
    r = subprocess.run([sys.executable, script, "--ack", *ids], capture_output=True, text=True,
                       timeout=30, env=_skill_env())
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "gmail acknowledgement failed").strip()[:400])


# One `[sotto] gmail poll: N consecutive failures` line after this many failures, then at most
# hourly while still failing — a revoked Google token must not mean silent email loss forever.
GMAIL_FAIL_ALERT_AFTER = 10
GMAIL_FAIL_ALERT_EVERY_SECS = 3600


def _gmail_poll_loop(secs: int) -> None:
    """Every `secs`, poll Gmail and feed new events through the SAME funnel as /bridge/events
    (dedupe → triage → maybe-agent). Every iteration is fully guarded — a poll failure must never
    kill the thread — but failures are COUNTED, not swallowed: after GMAIL_FAIL_ALERT_AFTER
    consecutive ones a single log line points at /setup, repeated at most hourly. Any success
    resets the counter."""
    fails = 0
    last_alert = 0.0
    while True:
        try:
            events = _poll_gmail_once()
            fails = 0
            if events:
                # Admit fresh and recovery mail separately. One old page item must not demote a
                # genuinely fresh message in the same bounded pass; acknowledge only after both
                # groups durably succeed, so a partial failure replays the whole page safely.
                groups = ([e for e in events if not e.get("_sotto_catchup")],
                          [e for e in events if e.get("_sotto_catchup")])
                for catchup, group in ((False, groups[0]), (True, groups[1])):
                    if not group:
                        continue
                    code, resp = handle_events({"events": group, "catchup": catchup})
                    if code != 200:
                        raise RuntimeError(f"gmail poll triage error: {resp}")
                _ack_gmail_events(events)
        except Exception as e:  # noqa: BLE001
            fails += 1
            if fails >= GMAIL_FAIL_ALERT_AFTER and (time.time() - last_alert) >= GMAIL_FAIL_ALERT_EVERY_SECS:
                print(f"[sotto] gmail poll: {fails} consecutive failures (last: {e}) — "
                      "check Google auth on /setup", flush=True)
                last_alert = time.time()
        time.sleep(max(secs, 5))


def start_gmail_poll_thread():
    """Start the Gmail event-poll daemon at server boot. SOTTO_EMAIL_POLL_SECS=0 disables it. The
    google-configured gate (the same google_connected() check /debug/google serves) runs INSIDE the
    daemon thread so boot never blocks on `setup.py --check`; while Google isn't configured yet the
    thread just re-checks every 10 min — authorizing via /setup later enables email events without a
    redeploy. Returns the thread (or None when disabled)."""
    secs = _email_poll_secs()
    if secs <= 0:
        return None

    def _boot():
        while True:
            try:
                ok, _ = google_connected()
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                break
            time.sleep(600)
        print(f"[sotto] gmail event poll active (every {secs}s)", flush=True)
        _gmail_poll_loop(secs)

    t = threading.Thread(target=_boot, daemon=True)
    t.start()
    return t


# ── Deferred-queue release valve (Step 2 item 3): the */15 heartbeat that lets held nudges out ────
# The audit's worst finding: an actionable event arriving during cooldown/quiet/catchup was queued
# and NEVER resurfaced same-day (digest needs 8+ known-sender signals; else the evening brief). The
# valve is deterministic and lives in triage_event.py (`--valve`) beside the queue/cooldown/quiet
# machinery it must respect; the receiver only owns the heartbeat (same daemon-thread pattern as the
# Gmail poll — a timer, not per-batch, because the defining failure is "the hold lifted and no fresh
# event arrived to trigger reconsideration") and the CHANNEL-HEALTH gate: a promotion is only spent
# when the DELIVERY channel is healthy (_delivery_channel_ready — the ROADMAP amendment, never burn
# budget on an undeliverable nudge). An agent verdict from the valve rides the IDENTICAL
# stage-bundle → spawn path a fresh agent verdict takes.

VALVE_INTERVAL_SECS_DEFAULT = 900   # the same */15 cadence as the proactive heartbeat

# One sentence: a nudge waits for the ACTIVE channel (SOTTO_CRON_DELIVER) to be linked, so a Photon or
# Telegram deploy that has not finished linking is held rather than spending a nudge on nothing.
_DELIVERY_GATE_STATE: dict = {}     # label → last logged state, so a shut gate logs once, not per tick


def _delivery_ready(status: str | None = None) -> bool:
    """THE delivery rule, in one sentence: the active channel must be one Sotto delivers to (Photon
    or Telegram) and its setup probe (an authenticated owner DM, or a bot recipient) must read
    "linked"; anything else holds. Silent — callers that run on a timer use
    _delivery_channel_ready below; the setup wizard's completion gate calls this directly, passing
    the channel state it already rendered from rather than re-probing."""
    channel = _deliver_target()
    return channel in ("telegram", "photon") and (status or _channel_status(channel)) == "linked"


def _delivery_channel_ready(label: str) -> bool:
    """_delivery_ready, plus one log line per state change (never per tick) so 'why did my nudges
    stop?' is answerable from the deploy log."""
    channel = _deliver_target()
    state = _channel_status(channel)
    ok = _delivery_ready(state)
    if _DELIVERY_GATE_STATE.get(label) != ok:
        _DELIVERY_GATE_STATE[label] = ok
        print(f"[sotto] {label}: {channel} linked — dispatching" if ok else
              f"[sotto] {label}: skipped, {channel} not linked", flush=True)
    return ok


def _valve_secs() -> int:
    """The valve heartbeat, in seconds. VALVE_INTERVAL_SECS_DEFAULT is the one writer — SOTTO_VALVE
    is the switch that turns the whole heartbeat off."""
    return VALVE_INTERVAL_SECS_DEFAULT


def run_valve() -> dict:
    """Run `triage_event.py --valve` synchronously (verdict JSON on stdout). Raises on any failure
    so the tick can log it — a broken valve must not masquerade as an empty queue."""
    script = _find_sotto_script("event-triage", "scripts", "triage_event.py")
    if not script:
        raise RuntimeError("triage_event.py not found in this image")
    r = subprocess.run([sys.executable, script, "--valve"], capture_output=True, text=True,
                       timeout=TRIAGE_TIMEOUT_SECS, env=_skill_env())
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "triage_event.py --valve failed").strip()[:400])
    out = json.loads(r.stdout or "{}")
    if not isinstance(out, dict) or "verdict" not in out:
        raise RuntimeError("triage_event.py --valve returned no verdict")
    return out


def _valve_tick() -> None:
    """One heartbeat: channel-health gate → valve → (maybe) stage + spawn, all best-effort. The
    verdict "agent" path is the same one handle_events takes for a fresh interrupt-worthy event."""
    if not _delivery_channel_ready("valve"):
        return   # no positive delivery probe — don't spend a promotion on an undeliverable nudge
    try:
        verdict = run_valve()
    except Exception as e:  # noqa: BLE001
        print(f"[sotto] release valve failed: {e}", flush=True)
        return
    if verdict.get("verdict") != "agent":
        return
    if verdict.get("job_id"):
        _WORK_WAKE.set()
        return
    try:
        bundle_path = _stage_bundle(verdict.get("bundle") or {})
    except OSError as e:
        print(f"[sotto] valve bundle stage failed: {e}", flush=True)
        return
    _spawn_event_agent(bundle_path)


def run_promote(key: str) -> dict:
    """Run `triage_event.py --promote <key>` synchronously (verdict JSON on stdout). Raises when
    the skills tree is absent or the fork fails, so the caller can say so instead of pretending the
    queue entry vanished."""
    script = _find_sotto_script("event-triage", "scripts", "triage_event.py")
    if not script:
        raise RuntimeError("triage_event.py not found in this image")
    r = subprocess.run([sys.executable, script, "--promote", key], capture_output=True, text=True,
                       timeout=TRIAGE_TIMEOUT_SECS, env=_skill_env())
    out = json.loads(r.stdout or "{}")
    if not isinstance(out, dict) or "ok" not in out:
        raise RuntimeError((r.stderr or r.stdout or "--promote returned no verdict").strip()[:400])
    return out


def _promote_queued(key: str) -> dict:
    """One user-chosen promotion, end to end: the delivery-channel gate, the funnel's own
    `--promote` (which owns every rule about what may be promoted and marks the explicit request), then
    the IDENTICAL _stage_bundle → _spawn_event_agent path a valve promotion takes. The dashboard
    calls this through HOOKS and renders whatever comes back; it never decides anything itself.

    The channel gate is checked FIRST and reported rather than swallowed: spending a nudge on an
    unlinked channel is exactly the silent loss _delivery_channel_ready exists to prevent, and a
    user who just tapped a button deserves the reason, not a shrug.
    Returns {"ok": True, "reason"} or {"ok": False, "error": <code>, "reason": <sentence>}."""
    if not _delivery_ready():
        return {"ok": False, "error": "channel",
                "reason": f"{_deliver_target()} isn't linked — the nudge would go nowhere"}
    try:
        out = run_promote(key)
    except Exception as e:  # noqa: BLE001
        print(f"[sotto] dashboard promote failed: {e}", flush=True)
        return {"ok": False, "error": "unavailable", "reason": "this deploy can't promote right now"}
    if not out.get("ok"):
        return out
    if out.get("job_id"):
        _WORK_WAKE.set()
        return {"ok": True, "reason": "queued durably"}
    try:
        bundle_path = _stage_bundle(out.get("bundle") or {})
    except OSError as e:
        print(f"[sotto] dashboard promote bundle stage failed: {e}", flush=True)
        # The funnel already transferred the entry from the queue; report the staging failure
        # instead of pretending the explicit request can be safely replayed.
        return {"ok": False, "error": "stage", "reason": "the nudge couldn't be staged"}
    _spawn_event_agent(bundle_path)
    return {"ok": True, "reason": str(out.get("reason") or "promoted")}


# ── Firing a scheduled job: the clock's lane and the dashboard's button ───────────────────────────
# The cron IS the definition of these runs, and adapters/sotto/crons.json is the ONE source for the
# crons. So "run my morning brief" — whether the clock says so or you tapped the button — is
# literally: take that job's row and fire its declared procedure. No second schedule, no second skill
# mapping — and a job the deploy has gated off (SOTTO_DIGEST=0) simply isn't in the list, so neither
# caller can offer what the box won't run.

def _fire_cron_job(name: str, label: str, *, _invalid_recovery: bool = False,
                   _fixed_recovery_from: str | None = None) -> dict:
    """Fire one crons.json job by name, now. {"ok": True, "skill": …} or {"ok": False, "error"}."""
    job = next((j for j in _sotto_cron_jobs() if j[0] == name), None)
    if job is None:
        return {"ok": False, "error": "unknown", "reason": "that job isn't registered on this box"}
    if label.startswith("cron:"):
        import managed
        import onboarding
        if name in MARKED_BRIEF_KINDS and onboarding.scheduled_hold(DATA):
            return {"ok": False, "error": "onboarding", "reason": "first useful look is arriving"}
        reason = managed.brief_hold(DATA)
        if reason:
            return {"ok": False, "error": "capability", "reason": reason}
        if name in MARKED_BRIEF_KINDS and not _invalid_recovery and _fresh_selfhost_brief_hold():
            return {"ok": False, "error": "capability",
                    "reason": "Connect a context source and delivery channel for your first brief"}
    skill = job[3]
    try:
        if label.startswith('cron:') and skill in MARKED_BRIEF_KINDS:
            import onboarding
            with _CLAIM_LOCK:
                if _welcome_admission_active():
                    return {"ok": False, "error": "onboarding", "reason": "first useful look is arriving"}
                if _managed_brief(skill, label, fixed_recovery_from=_fixed_recovery_from):
                    return {"ok": True, "skill": skill}
                raise RuntimeError('Sotto unattended skill procedure is unsupported')
        elif _managed_brief(skill, label):
            return {"ok": True, "skill": skill}
        if name == 'sotto-proactive':
            return ({'ok': True, 'skill': skill} if _run_proactive(label) else
                    {'ok': False, 'error': 'channel', 'reason': 'delivery channel not linked'})
        if name in ('sotto-midday-digest', 'sotto-relationship-pulse'):
            composer = _find_sotto_script('_shared', 'scripts', 'compose_brief.py')
            if not composer:
                raise FileNotFoundError('Sotto pipeline missing')
            request = {'pack': str(Path(composer).parents[2]),
                       'kind': 'digest' if name == 'sotto-midday-digest' else 'pulse'}
            _spawn_and_deliver([sys.executable, str(Path(__file__).with_name('procedure_runner.py'))],
                               json.dumps(request), label)
            return {'ok': True, 'skill': skill}
        raise RuntimeError('Sotto unattended skill procedure is unsupported')
    except RuntimeError as e:
        if _is_terminal_work(e):
            # Not a spawn failure: this slot's work already ran out its bounded attempts (or its
            # deadline). The tick stamps the slot on this signal; swallowing it here made every
            # minute of the catch-up window log "spawn failed" and open a fresh sqlite txn.
            raise
        print(f"[sotto] {label} spawn failed: {e}", flush=True)
        return {"ok": False, "error": "spawn", "reason": "that run couldn't be started"}
    except Exception as e:  # noqa: BLE001
        print(f"[sotto] {label} spawn failed: {e}", flush=True)
        return {"ok": False, "error": "spawn", "reason": "that run couldn't be started"}


def _is_terminal_work(error: BaseException) -> bool:
    """work_queue's refusal to mint another attempt budget for an admitted request that already
    failed or expired — the one enqueue error a scheduler must treat as 'done', not 'retry'."""
    return isinstance(error, RuntimeError) and str(error).startswith('work request is terminal (')


def _run_dashboard_job(name: str) -> dict:
    """The dashboard's "run it now" button, on the shared fire path."""
    try:
        out = _fire_cron_job(name, f"run-now:{name}")
    except RuntimeError as error:
        if not _is_terminal_work(error):
            raise
        out = {"ok": False, "error": "terminal", "reason": "that run already used up its attempts"}
    if out.get("ok"):
        print(f"[sotto] run-now from the dashboard: {name}", flush=True)
    return out


# ── The receiver's own cron: a scheduled run is scheduled AND delivered here ──────────────────────
# ONE delivery lane for everything scheduled. A scheduler that delivers on its own sits outside the
# outbox: a channel that was down at 6:30 loses the brief with no retry and no receipt, the
# deliver-once gate has a second lane to referee, and a no-nudge run can deliver the literal
# `NO_NUDGES` token (Sep 4, 2026) because the silence seam lives in _deliver_text. So this heartbeat
# fires every crons.json row down the same path the dashboard's button uses, and a scheduled run
# inherits the outbox's retries, its receipt, the silence seam, and — for a brief — the
# deliver-once gate that already governs these labels.
#
# Dedupe is two layers, deliberately: the in-memory map stops the ticks inside one window from firing
# the same job ten times, and the brief claim plus the deliver-once marker are what actually
# guarantee at-most-once DELIVERY. A restart mid-window therefore re-fires, and the gate receipts
# that second copy `superseded` — the designed outcome, not a hole to add state for.
#
# The tick asks _local_now() every time, so a timezone change moves the next fire immediately; there
# is no registration anywhere to re-register.
CRON_TICK_SECS = 60          # the schedule's resolution is one minute; slower would skip a job

_CRON_FIRED: dict = {}       # job name → the slot it last fired in (_cron_slot), this process
_CRON_UNPARSED: set = set()  # names logged once for a schedule this side can't read — never per tick

MEMORY_INTERVAL_SECONDS = 15 * 60
_CONTEXT_LAST_STARTED = 0.0
# A first-use source read that leaves setup unready doubles the wait before the next, from one
# minute up to one hour, and starts over when the Mac's source health or consent changes.
SOURCE_PROBE_MIN_WAIT_SECS = CRON_TICK_SECS
SOURCE_PROBE_MAX_WAIT_SECS = 60 * 60
_SOURCE_PROBE_LAST_STARTED = 0.0
_SOURCE_PROBE_WAIT = 0.0         # 0 until a read leaves setup unready
_SOURCE_PROBE_BASIS = None       # the source health that wait was measured against
_SOURCE_PROBE_INFLIGHT = False
_SOURCE_PROBE_LOCK = threading.Lock()


def _scheduled_brief_work_active() -> bool:
    """Durable work accepted before the outbox marker must precede welcome."""
    return any(WORK_QUEUE.active_job(DATA, 'run', f'{lane}:sotto-{kind}-brief') is not None
               for lane in ('cron', 'brief') for kind in ('morning', 'evening'))


def _welcome_admission_active() -> bool:
    import onboarding
    return (onboarding.scheduled_hold(DATA) or onboarding.status(DATA) == 'queued'
            or WORK_QUEUE.active_job(DATA, 'run', onboarding.LABEL) is not None)


def _context_sources_ready() -> bool:
    """Use the same source gate for first-use welcome and scheduled work."""
    import managed
    if managed.enabled():
        return managed.has_sources(DATA)
    google_token = _google_home_path_or_none('google_token.json')
    return (_local_source_ready() or bool(google_token and Path(google_token).exists())
            or any(s.get('connected') and s.get('service') in managed.MANAGED_CONNECTOR_SOURCES
                   and not _connector_error(s['service'])
                   and (not s.get('expires_at') or s['expires_at'] >= time.time()
                   or _connector_has_refresh(s['service']))
                   for s in CONNECTORS.service_status()))


def _local_source_ready() -> bool:
    """A live relay is transport, not evidence that an enabled Mac source was read."""
    import managed
    source = _source_context()
    health = source.source_health(DATA)
    if health.get('status') != 'ok':
        return False
    try:
        rows = source._source_rows(source._read_source_state(source._state_path(DATA)))
    except (OSError, ValueError, TypeError, AttributeError, source.jsonstore.Unreadable):
        return False

    def read_after_regrant(row):
        watermark = row.get('consent_regrant_epoch')
        if watermark is None:
            return True
        request = row['read'].get('request_epoch')
        return (type(watermark) in (int, float) and source.math.isfinite(watermark)
                and watermark > 0
                and type(request) in (int, float) and source.math.isfinite(request)
                and request > watermark)

    return any(row.get('source') in source.SOURCE_FIELDS
               and managed.eligible_context_source(row['source'])
               and row.get('status') in ('ok', 'empty')
               and isinstance(rows.get(row['source'], {}).get('read'), dict)
               and rows[row['source']]['read'].get('status') == 'ok'
               and rows[row['source']]['read'].get('payload_valid') is True
               and read_after_regrant(rows[row['source']])
               for row in health['sources'])


def _source_probe_basis():
    """Retry promptly when source health or an explicit consent generation changes."""
    source = _source_context()
    health = source.source_health(DATA)
    try:
        rows = source._source_rows(source._read_source_state(source._state_path(DATA)))
    except (OSError, ValueError, TypeError, AttributeError, source.jsonstore.Unreadable):
        rows = {}
    return tuple((row['source'], row['status'],
                  rows.get(row['source'], {}).get('consent_regrant_epoch'))
                 for row in health['sources'])


def _bootstrap_selfhost_source_read() -> None:
    """Collect one normal, consent-aware Mac receipt before admitting a first brief."""
    global _SOURCE_PROBE_INFLIGHT, _SOURCE_PROBE_WAIT, _SOURCE_PROBE_BASIS
    try:
        import managed
        source = _source_context()
        health = source.bridge_call('health')
        statuses = health.get('sources') if isinstance(health, dict) else None
        # A capability probe reports the Mac's switches before any extraction. The Mac's
        # read_local also enforces those switches; never read when it reports none enabled.
        if (isinstance(statuses, dict) and any(
                status == 'ok' and name in source.SOURCE_FIELDS
                and managed.eligible_context_source(name) and source.allowed(name)
                for name, status in statuses.items())):
            source.read_local(24)
    except Exception as error:  # best-effort first-use collection, retried by the heartbeat
        print(f'[sotto] first source read: {type(error).__name__}', flush=True)
    finally:
        try:
            basis, ready = _source_probe_basis(), _context_sources_ready()
        except Exception:
            basis, ready = None, False
        with _SOURCE_PROBE_LOCK:
            _SOURCE_PROBE_BASIS = basis
            _SOURCE_PROBE_WAIT = 0.0 if ready else min(
                max(2 * _SOURCE_PROBE_WAIT, SOURCE_PROBE_MIN_WAIT_SECS), SOURCE_PROBE_MAX_WAIT_SECS)
            _SOURCE_PROBE_INFLIGHT = False


def _maybe_bootstrap_selfhost_source_read() -> None:
    """One in-flight read while a fresh self-host is held for context, backing off as above."""
    global _SOURCE_PROBE_INFLIGHT, _SOURCE_PROBE_LAST_STARTED, _SOURCE_PROBE_WAIT
    import onboarding
    if (not _first_brief_pending() or onboarding.scheduled_inflight(DATA)
            or not RELAY.bridge_connected() or _context_sources_ready()):
        return
    basis = _source_probe_basis()
    with _SOURCE_PROBE_LOCK:
        now = time.time()
        if basis != _SOURCE_PROBE_BASIS:
            _SOURCE_PROBE_WAIT = 0.0
        if _SOURCE_PROBE_INFLIGHT or now - _SOURCE_PROBE_LAST_STARTED < _SOURCE_PROBE_WAIT:
            return
        _SOURCE_PROBE_INFLIGHT = True
        _SOURCE_PROBE_LAST_STARTED = now
    try:
        threading.Thread(target=_bootstrap_selfhost_source_read, daemon=True).start()
    except RuntimeError:
        with _SOURCE_PROBE_LOCK:
            _SOURCE_PROBE_INFLIGHT = False
            _SOURCE_PROBE_LAST_STARTED = 0.0
        raise


def _first_brief_pending() -> bool:
    """A self-host install without a delivered or adopted first brief is still in first-use setup."""
    import managed
    import onboarding
    return not managed.enabled() and onboarding.status(DATA) not in ('delivered', 'existing')


def _fresh_selfhost_brief_hold() -> bool:
    """Automatic first briefs wait for setup; accepted installations keep recovery."""
    return _first_brief_pending() and (not _context_sources_ready()
                                       or not _delivery_channel_ready('cron:first-brief'))


def _background_context_tick() -> None:
    """Quiet memory work and first-use delivery ride the existing heartbeat in both modes."""
    global _CONTEXT_LAST_STARTED
    import managed
    import onboarding
    try:
        _maybe_bootstrap_selfhost_source_read()
        has_sources = _context_sources_ready()
        # Check durable completion before touching the channel or doing any work.
        with _CLAIM_LOCK:
            # Recover a crash between the queue's committed denial and its onboarding projection.
            welcome_key = _job_key(onboarding.LABEL, '')
            welcome_job = WORK_QUEUE.get(DATA, WORK_QUEUE.job_id_for('run', welcome_key))
            if (welcome_job and welcome_job['status'] == 'failed'
                    and welcome_job.get('error') == WORK_QUEUE.MODEL_BUDGET_EXHAUSTED):
                onboarding.model_budget_held(DATA)
            onboarding.tick(DATA, lambda: has_sources and (not managed.enabled() or managed.messaging_activated(DATA))
                            and _delivery_channel_ready(onboarding.LABEL)
                            and not _scheduled_brief_work_active()
                            and not onboarding.scheduled_inflight(DATA),
                            lambda: _managed_brief('sotto-welcome-brief', onboarding.LABEL),
                            model_ready=_brief_model_ready)
        if not has_sources:
            return
        label = 'background:sotto-memory'
        now = time.time()
        if now - _CONTEXT_LAST_STARTED < MEMORY_INTERVAL_SECONDS or _RUNS_INFLIGHT.get(label):
            return
        script = _find_sotto_script('_shared', 'scripts', 'memory_cycle.py')
        if script:
            try:
                _spawn_and_deliver([sys.executable, script], '{}', label)
            except RuntimeError as error:
                if not _is_terminal_work(error):
                    raise
                # Consume this exhausted bucket, not another full interval from this retry.
                _CONTEXT_LAST_STARTED = now - now % MEMORY_INTERVAL_SECONDS
                return
            _CONTEXT_LAST_STARTED = now
        # Inspect saved history only at the existing memory cadence, never on every poll.
        with _CLAIM_LOCK:
            composer = _find_sotto_script('_shared', 'scripts', 'compose_brief.py')
            pack = Path(composer).parent.parent.parent if composer else None
            onboarding.discovery_tick(DATA, lambda: has_sources and pack is not None
                and (not managed.enabled() or managed.messaging_activated(DATA))
                and _delivery_channel_ready(onboarding.DISCOVERY_LABEL)
                and _history_discovery_delivery_ready({'label': onboarding.DISCOVERY_LABEL})
                and not _scheduled_brief_work_active() and not onboarding.scheduled_inflight(DATA)
                and onboarding.discovery_context(DATA, pack, _source_context().allowed) is not None,
                lambda: _managed_brief('sotto-welcome-brief', onboarding.DISCOVERY_LABEL, retry_terminal=True))
    except Exception as error:  # a background failure cannot stop briefs or the scheduler
        print(f'[sotto] context learning: {type(error).__name__}', flush=True)


def _cron_tick() -> None:
    """One heartbeat: fire every receiver-run job whose slot has arrived and that hasn't fired in
    it. Best-effort per job — an unreadable schedule or a failed spawn is one log line."""
    import managed
    _activation_tick()  # before this tick's first-brief delivery gate
    if (_selected_fixed_profile() and _candidate_retained_route_digest() is None):
        # Wait for the selected channel's current owner route before spending a slot.
        return
    try:
        _owner_reminder_tick()
    except Exception as error:
        print(f'[sotto] owner reminder tick: {type(error).__name__}', flush=True)
    if managed.notice_pending(DATA):
        _deliver_text(managed.NOTICE_TEXT, managed.NOTICE_LABEL)
    _background_context_tick()
    try:
        _prepare_briefs()
    except Exception as error:
        print(f'[sotto] brief preparation: {type(error).__name__}', flush=True)
    today = _local_now().strftime("%Y-%m-%d")
    for name, schedule, _prompt, _skill in _sotto_cron_jobs():
        if (_fixed_daily_minute(schedule) is None and _fixed_weekly_minute(schedule) is None
                and _interval_minutes(schedule) is None):
            if name not in _CRON_UNPARSED:
                _CRON_UNPARSED.add(name)
                print(f"[sotto] cron {name}: {schedule!r} is neither a fixed daily `M H * * *` nor "
                      "a fixed weekly `M H * * D` or `*/N * * * *` interval schedule — "
                      "the receiver leaves it unfired", flush=True)
            continue
        slot = _scheduled_slot(schedule)
        if slot is None or _CRON_FIRED.get(name) == slot:
            continue
        if _selected_fixed_profile():
            try:
                prior_slot = WORK_QUEUE.fixed_slot(
                    DATA, authority_digest=_fixed_schedule_authority(),
                    name=name, wall_slot=slot)
            except Exception as error:
                print(f'[sotto] cron {name}: fixed authority {type(error).__name__}', flush=True)
                continue
            if prior_slot and prior_slot['last_work_status'] == 'skipped_unlinked':
                _CRON_FIRED[name] = slot
                continue
        # A restart inside the window forgets it fired; the durable marker remembers whether the
        # day DELIVERED. Composing a second brief the send seam would only supersede costs minutes
        # and real tokens for words nobody reads (Day-15 simulation, Sep 2026).
        kind = MARKED_BRIEF_KINDS.get(name)
        if kind and os.path.exists(delivered_marker(today, kind)):
            _CRON_FIRED[name] = slot
            print(f"[sotto] cron {name}: today's brief is already delivered — not re-fired", flush=True)
            continue
        # A nudge lane (the watcher, the digest) spends the day's interrupt budget against the
        # channel it lands on, so it is held exactly as the wake-push and the valve hold it: an
        # unlinked channel means the slot is spent unspawned, not retried every tick. A brief is
        # never held here — the outbox keeps it until the channel comes back.
        #
        # Spending the slot is only honest for an INTERVAL lane: the `*/15` watcher's next boundary
        # is a quarter-hour away, so a held slot costs one quarter-hour of nudges. A fixed daily or
        # weekly lane has no next slot inside the day — the digest's is tomorrow, the pulse's is
        # next Monday — so its slot stays unstamped and the next tick inside the catch-up window
        # fires it as soon as the channel links. It cannot double-fire: a fixed schedule's work key
        # is `cron:<name>:<local date>` for the whole window (_cron_slot is None outside the first
        # BRIEF_CRON_WINDOW_MIN minutes, so _job_key falls back to the date), and _CRON_FIRED is
        # stamped the moment a spawn starts.
        if not kind and not _delivery_channel_ready(f"cron:{name}"):
            if _interval_minutes(schedule) is not None:
                if _selected_fixed_profile():
                    try:
                        WORK_QUEUE.skip_fixed_slot(
                            DATA, authority_digest=_fixed_schedule_authority(),
                            schedule_digest=_fixed_schedule_digest(),
                            name=name, wall_slot=slot)
                    except Exception as error:
                        print(f'[sotto] cron {name}: skip evidence {type(error).__name__}', flush=True)
                        continue
                _CRON_FIRED[name] = slot
            continue
        try:
            out = _fire_cron_job(name, f"cron:{name}")
        except Exception as error:
            if _is_terminal_work(error):
                _CRON_FIRED[name] = slot
            print(f'[sotto] cron {name}: {type(error).__name__}', flush=True)
            continue
        # Stamped only on a spawn that STARTED: a failed spawn retries on the next tick — the
        # BRIEF_CRON_WINDOW_MIN window bounds that to a handful of attempts, and stamping the whole
        # day on a failure silenced the brief until tomorrow (external review, Aug 31).
        if out.get("ok"):
            _CRON_FIRED[name] = slot
        print(f"[sotto] cron {name}: {'fired' if out.get('ok') else out.get('reason')}", flush=True)


# The retention sweep rides this same thread and the same fired-today stamp. Not a second scheduler
# and not a crons.json row: crons.json is the schedule for everything that REACHES you, and this is
# housekeeping nobody is delivered. The window is `_fires_now`'s — one statement of "has this
# minute arrived", shared with the briefs, so the two can never disagree about what time it is here.
_RETENTION_FIRED: dict = {}   # "sweep" → the local date it last ran, this process


def _retention_tick() -> None:
    """One sweep a day, at retention.SWEEP_LOCAL. A missed day (a restart across the window, a box
    that was down) self-heals: every policy is an AGE, so tomorrow's sweep removes exactly what
    today's would have plus a day's more. Best-effort — retention.sweep never raises."""
    today = _local_now().strftime("%Y-%m-%d")
    if _RETENTION_FIRED.get("sweep") == today:
        return
    hour, minute = RETENTION.SWEEP_LOCAL
    if not _fires_now(f"{minute} {hour} * * *"):
        return
    _RETENTION_FIRED["sweep"] = today
    out = RETENTION.sweep()
    # Always one line, including "0 aged out": the day housekeeping first COULD delete something is
    # the day an operator most wants proof it ran, and this stamp lives only in memory.
    print(f"[sotto] retention sweep: {out['count']} entries aged out, "
          f"{len(out['errors'])} errors", flush=True)
    for err in out["errors"]:
        print(f"[sotto] retention: {err['path']}: {err['error']}", flush=True)


def rotate_chat_sessions() -> int | None:
    """Rotate the chat runtime's sessions through its private loopback API. Returns how many were
    reset, or None when the runtime's session interface is unavailable."""
    return _runtime_adapter().archive_sessions()


def _session_archive_tick() -> None:
    """A chat session lasts a day or a deploy, whichever comes first.

    Sessions used to reset on a clock; Aug 26 tied the reset to deploys instead, because the clock
    reset broadcast a "◐ Session automatically reset" banner nobody could silence. The cost showed
    up a week later: between deploys the transcript accumulated every brief and nudge delivered
    into the chat, and interactive replies started copying it back out — first as a duplicate
    brief (Sep 1), then recursively, each reply re-emitting the previous reply's copy, Telegram's
    own (1/2)(2/2) split markers included (Sep 2, three times in one day). That is not a rule the
    model breaks; it is material sitting in context with the same authority as the rule.

    So the day boundary comes back as a silent rotation — no reset event fires, nothing is
    broadcast. It rides the nightly housekeeping slot (retention.SWEEP_LOCAL) so the day's first
    message starts clean. A same-day copy is still possible; a week-old one is not."""
    today = _local_now().strftime("%Y-%m-%d")
    if _RETENTION_FIRED.get("sessions") == today:
        return
    hour, minute = RETENTION.SWEEP_LOCAL
    if not _fires_now(f"{minute} {hour} * * *"):
        return
    _RETENTION_FIRED["sessions"] = today

    def _run():
        n = rotate_chat_sessions()
        if n is None:
            print("[sotto] daily session rotation: runtime session interface unavailable — the "
                  "transcript keeps growing until the next deploy or /new", flush=True)
        else:
            print(f"[sotto] daily session rotation: reset {n} chat session(s) — the next message "
                  "starts fresh", flush=True)

    # Its own thread: the loopback call can take up to 30 s, and the thread it would otherwise run
    # on is the one that fires the briefs inside a 10-minute window.
    threading.Thread(target=_run, name="sotto-session-archive", daemon=True).start()


def start_cron_thread():
    """Start the receiver's scheduler at boot. Ticks FIRST, then sleeps: the fire window is only
    BRIEF_CRON_WINDOW_MIN wide, and a boot in its last minute that slept before looking would fall
    off its edge and miss the day's brief (external review, Aug 31). A restart re-firing inside the
    window is safe — the deliver-once gate settles who owns the day's brief."""
    def _loop():
        while True:
            try:
                _cron_tick()
            except Exception as e:  # noqa: BLE001 — the heartbeat must never die
                print(f"[sotto] cron tick error: {e}", flush=True)
            try:
                _sotto_adapter('model_lease').tick(DATA)
            except Exception as error:
                print(f'[sotto] model lease: {type(error).__name__}', flush=True)
            time.sleep(CRON_TICK_SECS)
            # Its own try: a scheduler that failed to fire a brief must still sweep the volume.
            try:
                _retention_tick()
            except Exception as e:  # noqa: BLE001 — the heartbeat must never die
                print(f"[sotto] retention tick error: {e}", flush=True)
            try:
                _session_archive_tick()
            except Exception as e:  # noqa: BLE001 — the heartbeat must never die
                print(f"[sotto] session archive tick error: {e}", flush=True)

    t = threading.Thread(target=_loop, name="sotto-cron", daemon=True)
    t.start()
    return t


def start_valve_thread():
    """Start the release-valve heartbeat at server boot. SOTTO_VALVE=0 (or a non-positive interval)
    disables it. Sleeps FIRST: at boot the Bridge's catchup batch is usually still in flight, and
    those events must land in the queue before the first promotion pass looks at it. Returns the
    thread (or None when disabled)."""
    if (os.environ.get("SOTTO_VALVE", "").strip() or "1") == "0" or _valve_secs() <= 0:
        return None

    def _loop():
        while True:
            time.sleep(max(_valve_secs(), 60))
            try:
                _valve_tick()
            except Exception as e:  # noqa: BLE001 — the heartbeat must never die
                print(f"[sotto] valve tick error: {e}", flush=True)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


# ── Post-meeting tap (Step 2 item 3, the additive half) ───────────────────────────────────────────
# "Your 2:00 PM with Sarah Chen just wrapped — want me to send the follow-up?" calcache.py owns
# DETECTION (it rides the calendar refresh thread's tick and has the attendee names the nudge
# needs); this function owns the one step that is the receiver's: handing the detected event-end to
# the SAME funnel every other nudge goes through. It is deliberately NOT a second gate stack — the
# tap is a nudge, so quiet hours, the snooze and the in-meeting hold (back-to-back meetings: the tap
# for A holds while B runs, and rides the release valve out when B ends) all apply because
# triage_event.py applies them to a `meeting_end` event like any other. The daily interrupt budget is
# the one gate taps skip — they have their own cap (calcache.tap_max_per_day, default 3), so taps and
# interrupts can't starve each other. The channel-health gate mirrors _valve_tick's
# (_delivery_channel_ready), for the same ROADMAP-amendment reason: never spend a tap on a nudge that
# can't be delivered. Returning False leaves the event-end UNhandled, so the next tick retries it
# while it's still inside calcache's window.

def _dispatch_synthetic_batch(events: list[dict], label: str) -> set[str]:
    """Run one synthetic refresh batch through triage and return its handled rowids.

    A successful funnel return means every input was either handled now or recognized through its
    durable item ownership. An exception acknowledges nothing, so calcache retains the whole batch.
    """
    if not events or not _delivery_channel_ready(label):
        return set()
    try:
        verdict = run_triage(events, False)
    except Exception as e:  # noqa: BLE001
        print(f"[sotto] {label} triage failed: {e}", flush=True)
        return set()
    handled = {str(event.get("rowid")) for event in events if event.get("rowid")}
    if verdict.get('job_id'):
        _WORK_WAKE.set()
        return handled
    if verdict.get("verdict") != "agent":
        return handled
    try:
        bundle_path = _stage_bundle(verdict.get("bundle") or {})
    except OSError as e:
        print(f"[sotto] {label} bundle stage failed: {e}", flush=True)
        return handled
    _spawn_event_agent(bundle_path)
    return handled


def _synthetic_event_handled(event: dict) -> bool:
    """Whether a pending synthetic event already completed triage durably.

    Agent outcomes require work ownership because their surfaced row precedes queue acceptance.
    Queue/drop outcomes have no work item, so their existing surfaced receipt is the authority.
    """
    item_key = WORK_QUEUE.event_item_key(event)
    if WORK_QUEUE.owned_items(DATA, "event", [item_key]):
        return True
    path = os.path.join(DATA, "events", "surfaced.jsonl")
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if (row.get("item_key") == item_key
                        and row.get("verdict") in ("queue", "drop")):
                    return True
    except FileNotFoundError:
        return False
    return False


def _dispatch_synthetic(event: dict, label: str) -> bool:
    """Run one synthetic calcache event (a meeting-end tap OR a calendar change) through triage;
    stage + spawn on an agent verdict. True ⇒ dispatched, whatever verdict the funnel returned — an
    event held by quiet hours or a meeting still consumed its chance to fire, and its queue entry
    is the valve's to promote. ONE function for both producers so the channel-health gate and the
    failure containment can never drift between them."""
    probe = event if event.get("rowid") else {**event, "rowid": "synthetic-single"}
    return str(probe["rowid"]) in _dispatch_synthetic_batch([probe], label)


def _dispatch_meeting_tap(event: dict) -> bool:
    return _dispatch_synthetic(event, "meeting tap")


# Shared <head> for every HTML page this receiver serves. The whole web surface is ONE product:
# every page links the dashboard's stylesheet first (/static/app.css — the paper/ink/gold tokens,
# Newsreader + JetBrains Mono) and then /static/setup.css (the Connections-surface layer, built
# against the tile/nav markup below by a parallel build), and carries the SAME favicon as /app —
# so /setup and its satellite pages read as views of the same site, never a second app. No inline
# <style> blocks here: the styling contract lives entirely in those two files. (/favicon.ico still
# answers 204 for clients that ignore the link.)
# Public Railway domain (set by the platform). Used to build the one-click pairing link for the Mac app.
RAILWAY_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")


def pairing_link() -> str:
    """The `sotto-bridge://` deep link the Mac app ingests in ONE click — it carries the full host
    (with https://, so the schemeless-downgrade bug can't happen), the bearer token, and the setup
    code, so the user types nothing. The setup code rides along because the app's cloud-services
    card opens the host's /setup page in a browser — a browser sends no bearer, so without the code
    that click lands on the 403 page. The link is only ever rendered ON the setup page, which the
    reader could not have opened without the code — same trust context, nothing new exposed. Same
    string doubles as the copy-paste 'pairing code'; older Bridges ignore the extra param."""
    host = f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else ""
    q = urllib.parse.urlencode({"host": host, "token": RELAY_TOKEN, "setup": resolve_setup_code()})
    return f"sotto-bridge://pair?{q}"


def public_base() -> str:
    """This deploy's public origin, derived EXACTLY like the boot-log setup link (main): the Railway
    domain when one exists, else a localhost fallback so local runs still render working links."""
    if RAILWAY_DOMAIN:
        return f"https://{RAILWAY_DOMAIN}"
    port = os.environ.get("PORT", os.environ.get("SOTTO_TRIGGER_PORT", "8787"))
    return f"http://localhost:{port}"


def connect_redirect_uri() -> str:
    """The OAuth redirect_uri the connectors register via DCR and send on authorize + exchange. One
    shared callback for every service — the pending `state` file says which service is in flight."""
    return public_base() + "/connect/oauth/callback"


# /connect/<service>/start — setup-code-gated (it spends discovery/DCR effort and mints flow state).
CONNECT_START_RE = re.compile(r"\A/connect/([A-Za-z0-9_-]{1,64})/start\Z")


def _google_setup_py():
    import managed
    return _runtime_adapter().google_setup_path(managed=managed.enabled())


def _cloud_google_adapter():
    """Load the same packaged credential writer used by /cloud/bootstrap."""
    adapter_path = _google_setup_py()
    if not adapter_path:
        return None
    spec = importlib.util.spec_from_file_location('cloud_google_adapter', adapter_path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    return adapter


# Memoized google_connected(): every /setup GET otherwise forks a `setup.py --check` subprocess
# (30s timeout worst-case). ~20s is fresh enough for a status page; invalidated on a successful
# live exchange so "Connected ✓" shows immediately.
_GOOGLE_CHECK_TTL_SECS = 20.0
_GOOGLE_CHECK_CACHE = (0.0, None)   # (monotonic ts, (ok, msg))
_NATIVE_GOOGLE_CHECK_LOCK = threading.Lock()
_GOOGLE_HOME_UNAVAILABLE = "Google credential home is unavailable. Configure SOTTO_HOME before connecting."


def google_connected() -> tuple[bool, str]:
    """Is Google Workspace currently connected? Runs the same `setup.py --check` start.sh uses, so the
    answer matches what a cron brief sees. Google is server-side (no Bridge), so this is the single
    source of truth for 'why is Gmail/Calendar missing from my brief'. Never raises. Memoized for
    _GOOGLE_CHECK_TTL_SECS (the check forks a subprocess)."""
    global _GOOGLE_CHECK_CACHE
    if _google_home_path_or_none('google_client_secret.json') is None:
        _GOOGLE_CHECK_CACHE = (0.0, None)
        return False, _GOOGLE_HOME_UNAVAILABLE
    ts, cached = _GOOGLE_CHECK_CACHE
    if cached is not None and (time.monotonic() - ts) < _GOOGLE_CHECK_TTL_SECS:
        return cached
    result = _google_connected_uncached()
    _GOOGLE_CHECK_CACHE = (time.monotonic(), result)
    return result



def native_google_connected() -> tuple[bool, str]:
    """Bound optional OAuth work inside native readiness; concurrent polls never wait.

    Reuse the setup cache, but do not inherit its 30-second interactive check. No
    worker threads are spawned: one request checks for at most three seconds,
    while overlapping requests can still report core model/channel readiness.
    """
    global _GOOGLE_CHECK_CACHE
    if _google_home_path_or_none('google_client_secret.json') is None:
        _GOOGLE_CHECK_CACHE = (0.0, None)
        return False, _GOOGLE_HOME_UNAVAILABLE
    ts, cached = _GOOGLE_CHECK_CACHE
    if cached is not None and time.monotonic() - ts < _GOOGLE_CHECK_TTL_SECS:
        return cached
    if not _NATIVE_GOOGLE_CHECK_LOCK.acquire(blocking=False):
        return False, "Google connection check is in progress."
    try:
        ts, cached = _GOOGLE_CHECK_CACHE
        if cached is not None and time.monotonic() - ts < _GOOGLE_CHECK_TTL_SECS:
            return cached
        result = _google_connected_uncached(timeout=3)
        _GOOGLE_CHECK_CACHE = (time.monotonic(), result)
        return result
    finally:
        _NATIVE_GOOGLE_CHECK_LOCK.release()


def _google_home_path_or_none(name: str) -> str | None:
    """Selected Sotto never substitutes the process home for a missing credential root."""
    try:
        return _runtime_adapter().home_path(name)
    except ValueError:
        return None


def _google_connected_uncached(*, timeout: float = 30) -> tuple[bool, str]:
    setup = _google_setup_py()
    if not setup:
        return False, "google-workspace skill not found in this image."
    secret = _google_home_path_or_none('google_client_secret.json')
    if secret is None:
        return False, _GOOGLE_HOME_UNAVAILABLE
    if not os.path.exists(secret):
        return False, "no OAuth client — set GOOGLE_OAUTH_CLIENT_JSON in Railway, then authorize at /google/auth."
    py = shutil.which("python") or shutil.which("python3") or "python3"
    try:
        r = subprocess.run([py, setup, "--check"], capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return False, f"check failed to run: {e}"
    if r.returncode == 0:
        return True, "connected ✓"
    return False, "not connected — authorize at /google/auth (no redeploy needed)."


def _read_prep_offer_calendar(operation, arguments):
    path = _google_api_py()
    if not path:
        raise RuntimeError('Calendar reader unavailable')
    spec = importlib.util.spec_from_file_location('sotto_prep_offer_calendar_read', path)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api.read_owner(operation, arguments)


def _google_api_py():
    return _runtime_adapter().google_api_path()


_EMAIL_RE = re.compile(r"[^\s<>@,;\"]+@[^\s<>@,;\"]+\.[^\s<>@,;\".]+")


def _derive_google_account_email() -> str:
    """The address of the Google account the user just connected — LEARNED, not typed. The host CLI
    has no profile/whoami, but the `From` of any `in:sent` message IS the authorized account, so one
    `gmail search "in:sent" --max 1` (the exact shape gather_google.gather_sent already runs) answers
    it. Tolerates the same From variants normalize_email does ("Name <addr>", a bare addr, a
    {name,email} object). Returns "" on ANY failure and never raises — this is a nicety layered on
    SOTTO_USER_EMAIL, never a reason to fail a connect."""
    if _google_home_path_or_none('google_token.json') is None:
        return ''
    api = _google_api_py()
    if not api:
        return ""
    py = shutil.which("python") or shutil.which("python3") or "python3"
    try:
        r = subprocess.run([py, api, "gmail", "search", "in:sent", "--max", "1"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return ""
        items = json.loads(r.stdout or "null")
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(items, dict):
        for k in ("messages", "emails", "items", "results"):
            if isinstance(items.get(k), list):
                items = items[k]
                break
    if not (isinstance(items, list) and items and isinstance(items[0], dict)):
        return ""
    frm = items[0].get("from") or items[0].get("sender") or items[0].get("from_address") or ""
    if isinstance(frm, dict):
        frm = frm.get("email") or frm.get("address") or ""
    m = _EMAIL_RE.search(frm) if isinstance(frm, str) else None
    return m.group(0).lower() if m else ""


def capture_google_account_email() -> str:
    """Derive the connected account's address once and persist it as `google_account_email` — the
    reason SOTTO_USER_EMAIL is an OVERRIDE rather than a requirement (every consumer reads env →
    this setting → its own fallback). One honest log line either way. Never raises."""
    try:
        addr = _derive_google_account_email()
        if addr:
            write_setting("google_account_email", addr)
            print(f"[sotto] google account: {addr}", flush=True)
            return addr
    except Exception as e:  # noqa: BLE001
        print(f"[sotto] could not derive google account email ({e}) — set SOTTO_USER_EMAIL to "
              "name yourself", flush=True)
        return ""
    print("[sotto] could not derive google account email — set SOTTO_USER_EMAIL to name yourself",
          flush=True)
    return ""


def _backfill_google_account_email() -> None:
    """Deploys that connected Google BEFORE the derivation existed never passed through the connect
    moment — learn the address once at boot instead. Settings first (a cheap read), so the day the
    key exists this costs nothing and never runs again."""
    if read_settings().get("google_account_email"):
        return
    if not google_connected()[0]:
        return
    capture_google_account_email()


def _extract_google_code(raw: str) -> str:
    """Users routinely paste the ENTIRE `http://localhost:1/?code=…&scope=…` redirect URL (or just
    its query string) instead of the bare code — pull the `code` param out. A plain code (no
    `code=`) passes through untouched; parse_qs also undoes the %2F escaping in real codes."""
    raw = (raw or "").strip()
    if "code=" not in raw:
        return raw
    q = urllib.parse.urlparse(raw).query or raw.split("?", 1)[-1]
    return ((urllib.parse.parse_qs(q).get("code") or [""])[0] or "").strip()


def exchange_google_code(code: str, browser_binding: str = "") -> tuple[bool, str]:
    """Exchange a Google auth code for a token LIVE (no Railway redeploy). Runs the same
    `setup.py --auth-code` start.sh runs, against the PKCE verifier the /google/auth step persisted.
    Best-effort: on any miss it returns a clear reason so the user can fall back to the env+redeploy
    path. Never raises."""
    import managed
    # Keep callback state intact in both modes; the shared helper validates it
    # before exchanging a pasted URL. Bare codes remain bound by PKCE.
    code = (code or "").strip()
    if not _extract_google_code(code):
        return False, "No code provided."
    if not code.startswith("http"):
        code = _extract_google_code(code)
    setup = _google_setup_py()
    if not setup:
        return False, "Google setup tool not found in this image (is the google-workspace skill installed?)."
    secret = _google_home_path_or_none('google_client_secret.json')
    if secret is None:
        return False, _GOOGLE_HOME_UNAVAILABLE
    if not os.path.exists(secret):
        return False, "Google client not set up yet — set GOOGLE_OAUTH_CLIENT_JSON in Railway and redeploy, then authorize."
    py = shutil.which("python") or shutil.which("python3") or "python3"
    try:
        r = subprocess.run([py, setup, "--auth-code", code, "--format", "json",
                            *(["--browser-binding", browser_binding] if browser_binding else [])],
                           capture_output=True, text=True, timeout=60)
    except Exception:  # noqa: BLE001
        return False, "Google could not finish connecting. Please try again."
    if r.returncode == 0:
        global _GOOGLE_CHECK_CACHE
        _GOOGLE_CHECK_CACHE = (0.0, None)   # drop the memo so /setup flips to Connected right away
        # The connect moment IS when Sotto learns who you are (ROADMAP: "don't we already have email
        # with Gmail auth?"). Best-effort, never blocks the success this function just earned.
        capture_google_account_email()
        if managed.enabled():
            token_path = _google_home_path_or_none('google_token.json')
            if token_path is None:
                return False, _GOOGLE_HOME_UNAVAILABLE
            try:
                token = json.loads(Path(token_path).read_text())
            except (OSError, ValueError, TypeError):
                return False, "Google connection could not be verified. Please try again."
            managed.record_google_consent(DATA, token.get("scopes", []))
        return True, "Connected ✓"
    return False, "Google could not finish connecting. Please try again."


SETTINGS_FILE = os.path.join(DATA, "config", "settings.json")


def read_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _google_calendar_settings(value=None):
    """Authenticated dashboard callers edit only read selection, under current Google consent."""
    import google_source_authority
    import managed
    library = _load_shared_lib('google_calendar_read')
    mode = managed.enabled()
    def current():
        return google_source_authority.state(DATA, _runtime_adapter().home_path('google_token.json'),
            managed_mode=mode, tenant=os.environ.get('SOTTO_TENANT_ID', ''),
            managed_ledger=managed.source_authority_state(DATA) if mode else None)
    before = current()
    if before is None or 'calendar' not in before['sources']:
        raise ValueError('Connect Google Calendar before changing calendar selection')
    if value is not None:
        selected = library.validate_selection(value)
        if current() != before:
            raise ValueError('Google consent changed')
        write_setting('google_calendar_selection', {**selected, 'generation': before['generation']})
    try:
        return {**library.selection(DATA, generation=before['generation']), 'needs_reselection': False}
    except ValueError:
        return {'mode': 'selected', 'calendar_ids': [], 'needs_reselection': True}


def write_setting(key: str, value) -> None:
    with CONNECTORS.json_transaction(SETTINGS_FILE, default={}, mode=0o600, indent=None) as s:
        if not isinstance(s, dict):
            raise ValueError("settings.json must contain an object")
        s[key] = value


_IANA_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_+./-]{0,63}\Z")
_CLOUD_TIMEZONE_LOCK = threading.RLock()


def _configured_tz_name() -> str:
    """The user's configured zone, or "" when nothing is set — tzchain's answer, nobody else's.
    The root is the one SETTINGS_FILE lives under, so the wizard's write and this read can never
    be two different files (tests repoint SETTINGS_FILE; the chain must follow it)."""
    return TZCHAIN.configured_tz_name(os.path.dirname(os.path.dirname(SETTINGS_FILE)))


def _configured_tz_source() -> tuple[str, str]:
    """(zone, source) from the one timezone chain; an env source fixes the zone for this launch."""
    return TZCHAIN.configured_tz_source(os.path.dirname(os.path.dirname(SETTINGS_FILE)))


def _crons_file() -> str:
    """adapters/sotto/crons.json — the container copy first (/app/adapters/sotto/, where the
    Dockerfile puts it), then the repo-relative source tree (tests / source checkouts)."""
    override = (os.environ.get("SOTTO_CRONS_JSON") or "").strip()
    if override:
        return override
    for p in ("/app/adapters/sotto/crons.json",
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                           "adapters", "sotto", "crons.json")):
        if os.path.exists(p):
            return p
    return ""


USER_ROUTINE_PREFIX = "user-"   # personal routines (sotto-routines skill) — never a SYSTEM job


def _sotto_cron_jobs() -> list:
    """The enabled system jobs as (name, schedule, prompt, skill), read straight from
    adapters/sotto/crons.json — the ONE schedule this process fires on its own clock, so no second
    copy can drift. A row's `gate` env key turns it off unless it reads "1" (default on), and its
    `schedule_env` key, when set, replaces the schedule. Empty list when the file is missing or
    unreadable.

    THE USER-ROUTINE FENCE, enforced at the one place every consumer reads (the tick, the
    dashboard's run-now list): a `user-`-prefixed name is a PERSONAL routine and is never a system
    job — it is dropped here, so no consumer of this function can fire one as a system job.
    crons.json should never contain such a name."""
    path = _crons_file()
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        if not isinstance(rows, list):
            raise ValueError("cron spec must be a JSON array")
        jobs = []
        for row in rows:
            name = str(row.get("name") or "") if isinstance(row, dict) else ""
            if not name or name.startswith(USER_ROUTINE_PREFIX):
                continue
            gate = row.get("gate")
            if gate and os.environ.get(str(gate), "1") != "1":
                continue
            schedule = os.environ.get(str(row.get("schedule_env") or ""), "") or row["schedule"]
            jobs.append((name, schedule, row["prompt"], row["skill"]))
        return jobs
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as e:
        print(f"[sotto] cron spec unreadable ({path or 'not found'}): {e}", flush=True)
        return []


def _personal_routines() -> list[dict]:
    return _runtime_adapter().personal_routines()


def set_timezone(tz: str) -> tuple[bool, str]:
    with _CLOUD_TIMEZONE_LOCK:
        return _set_timezone_locked(tz)


def _set_timezone_locked(tz: str) -> tuple[bool, str]:
    """Persist an IANA zone unless the launch already fixes a different zone.

    The receiver's tick reads the zone every minute; no schedule re-registration is needed.
    """
    tz = (tz or "").strip()
    if not tz or ("/" not in tz and tz not in ("UTC", "GMT")) or not _IANA_RE.match(tz):
        return False, "That doesn't look like an IANA timezone (e.g. America/Los_Angeles)."
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return False, "That doesn't look like an IANA timezone (e.g. America/Los_Angeles)."
    fixed, source = _configured_tz_source()
    if source.startswith('env:') and tz != fixed:
        return False, "Timezone is fixed by this installation's configuration."
    # The zone in effect so far (SOTTO_TIMEZONE env, else the wizard zone persisted earlier, else
    # UTC) — read BEFORE persisting the new one.
    prev = _configured_tz_name() or "UTC"
    write_setting("timezone", tz)
    if tz != prev:
        # The receiver's own fired-today stamps are keyed by the LOCAL date, and the zone just
        # moved. A deploy at 22:00 PDT fired the 06:30 brief at 06:30 UTC (23:30 PDT) and stamped
        # today's date; after the wizard set PDT the user's real 06:30 was still "today", already
        # stamped, and no brief came (Day-0 simulation, Sep 2026). Forget the stamps: the durable
        # delivered marker is what stops a re-fire of a brief that actually went out.
        _CRON_FIRED.clear()
        _RETENTION_FIRED.clear()
    return True, tz


def accept_cloud_timezone(tz: str) -> None:
    """Set the first Cloud zone before a verified handoff becomes ready; never follow travel."""
    with _CLOUD_TIMEZONE_LOCK:
        if _configured_tz_name():
            return
        ok, detail = _set_timezone_locked(tz)
        if not ok:
            raise ValueError(detail)


_GOOGLE_FLOW_LOCK = threading.RLock()
GOOGLE_BROWSER_COOKIE = "sotto_google_browser"


def google_redirect_uri() -> str:
    # Only deployment configuration may choose where Google returns credentials.
    # Never trust the request Host or forwarded headers for this address.
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", RAILWAY_DOMAIN or ""):
        raise ValueError("Add a public domain to this deployment before connecting Google.")
    return f"https://{RAILWAY_DOMAIN}/google/oauth/callback"


def _validate_google_web_client(obj):
    if not isinstance(obj, dict) or not isinstance(obj.get("web"), dict):
        raise ValueError("Choose a Web application OAuth client in Google Cloud and paste its JSON. "
                         "Desktop clients cannot return to this Sotto page.")
    client = obj["web"]
    required = ("client_id", "client_secret", "auth_uri", "token_uri")
    if not all(isinstance(client.get(k), str) and client[k].strip() for k in required):
        raise ValueError("That OAuth client JSON is incomplete. Paste the full downloaded file.")
    if (client['auth_uri'] not in ('https://accounts.google.com/o/oauth2/auth',
                                   'https://accounts.google.com/o/oauth2/v2/auth')
            or client['token_uri'] != 'https://oauth2.googleapis.com/token'):
        raise ValueError("Use a Google OAuth client downloaded from Google Cloud.")
    redirect = google_redirect_uri()
    if not isinstance(client.get('redirect_uris'), list) or redirect not in client['redirect_uris']:
        raise ValueError("Add this Authorized redirect URI in Google Cloud, then download the JSON again: "
                         + redirect)
    return redirect


def setup_google_client(client_json: str) -> tuple[bool, str]:
    """Save a validated Web client; consent starts only in its initiating browser."""
    if not isinstance(client_json, str) or not client_json.strip():
        return False, "Paste your OAuth client JSON first."
    try:
        obj = json.loads(client_json)
        _validate_google_web_client(obj)
    except (ValueError, TypeError) as exc:
        return False, ("That doesn't look like valid JSON — paste the full downloaded file."
                       if isinstance(exc, json.JSONDecodeError) else str(exc))
    if not _google_setup_py():
        return False, "Google setup tool not found in this image."
    secret = _google_home_path_or_none('google_client_secret.json')
    pending = [_google_home_path_or_none(name) for name in (
        'google_oauth_pending.json', 'google_oauth_last_url.txt')]
    if secret is None or any(path is None for path in pending):
        return False, _GOOGLE_HOME_UNAVAILABLE
    try:
        with _GOOGLE_FLOW_LOCK:
            CONNECTORS.write_json(secret, obj, mode=0o600)
            # A client change invalidates consent in flight, never an existing grant.
            for path in pending:
                Path(path).unlink(missing_ok=True)
    except OSError:
        return False, "Your client could not be saved. Please try again."
    return True, "Client saved — now connect Google below."


def start_google_authorization(browser_binding: str) -> str:
    """Return this browser's unexpired consent URL, else a fresh one, without logging it."""
    with _GOOGLE_FLOW_LOCK:
        secret = _google_home_path_or_none('google_client_secret.json')
        if secret is None:
            raise ValueError(_GOOGLE_HOME_UNAVAILABLE)
        redirect = _validate_google_web_client(json.loads(Path(secret).read_text()))
        setup = _google_setup_py()
        if not setup:
            raise ValueError("Google setup is unavailable. Please try again later.")
        py = shutil.which("python") or shutil.which("python3") or "python3"
        r = subprocess.run([py, setup, '--auth-url', '--services', 'email,calendar',
                            '--format', 'json', '--redirect-uri', redirect,
                            '--browser-binding', browser_binding, '--reuse-pending'],
                           capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise ValueError("Google sign-in could not start. Please try again.")
        url_path = _google_home_path_or_none('google_oauth_last_url.txt')
        if url_path is None:
            raise ValueError(_GOOGLE_HOME_UNAVAILABLE)
        url = Path(url_path).read_text().strip()
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'accounts.google.com':
            raise ValueError("Google sign-in could not start. Please try again.")
        return url


def _google_client_form(qs: str = "", csrf: str = "") -> str:
    import html
    try:
        redirect = google_redirect_uri()
    except ValueError as exc:
        return f"<p>{html.escape(str(exc))}</p>"
    return (
        "<p>Connect Gmail and Calendar using your own Google account.</p>"
        "<ol class='tile-hint'><li>In <a href='https://console.cloud.google.com' target='_blank'>Google Cloud</a>, "
        "enable the Gmail API and Google Calendar API. Set up your consent screen and publish it to In production (Testing access expires after ~7 days).</li>"
        "<li>Create an OAuth client of type <b>Web application</b>.</li>"
        f"<li>Add this <b>Authorized redirect URI</b>: <code>{html.escape(redirect)}</code></li>"
        "<li>Choose Download JSON and paste the file below.</li></ol>"
        f"<form action='/setup/google-client{qs}' method='post'>"
        f"<input type='hidden' name='csrf' value='{html.escape(csrf, quote=True)}'>"
        "<textarea name='client_json' rows='4' class='field' aria-label='Google client JSON' "
        "placeholder='{&quot;web&quot;:{...}}'></textarea>"
        "<p><button class='btn-primary'>Save client</button></p></form>")


def _google_replace_client(qs: str = "", expanded: bool = False, csrf: str = "") -> str:
    """The saved client can be structurally valid yet unusable (rotated or deleted secret, another
    project, unregistered callback). Replacing it clears only consent in flight, never a grant."""
    return (f"<details{' open' if expanded else ''}><summary>Use a different Google client</summary>"
            "<p class='tile-hint'>Use this if the saved client's secret was rotated or deleted, it belongs "
            "to another Google Cloud project, or this page's redirect URI isn't registered on it. Any "
            "existing Google connection keeps working until the new client connects.</p>"
            + _google_client_form(qs, csrf) + "</details>")


def _google_authorization_page(qs: str = "", csrf: str = "", reconnect: bool = False,
                               replace_client: bool = False, setup_csrf: str = "") -> str:
    import html
    if google_connected()[0] and not (reconnect or replace_client):
        body = ("<p>Gmail and Calendar are connected. You can return to Sotto.</p>"
                f"<p><a href='/google/auth{qs}{'&' if qs else '?'}reconnect=1'>Reconnect Google</a></p>")
        title = "Google is connected"
    else:
        title = "Connect Google to Sotto"
        try:
            secret = _runtime_adapter().home_path('google_client_secret.json')
            _validate_google_web_client(json.loads(Path(secret).read_text()))
            body = ("<p>Choose your Google account and allow Gmail and Calendar access. "
                    "Google will bring you back here automatically.</p>"
                    f"<form action='/google/start{qs}' method='post'>"
                    f"<input type='hidden' name='csrf' value='{html.escape(csrf, quote=True)}'>"
                    "<button class='btn-primary'>Continue with Google</button></form>"
                    + _google_replace_client(qs, replace_client, setup_csrf))
        except (OSError, ValueError, TypeError):
            body = ("<p>Finish the one-time Google client setup below. If you previously used a Desktop "
                    "client, choose Web application instead so sign-in returns directly to Sotto.</p>"
                    + _google_client_form(qs, setup_csrf))
    return _narrow_page(title, _plain_tile(html.escape(title), body), back_href=f"/setup{qs}")


def _connector_error(service: str):
    """The pipeline's gather writes $SOTTO_DATA/connectors/<service>.error (plain text) on an auth
    failure and deletes it on the next success — we only READ it here. Present ⇒ the tile must say
    Reconnect, not ✓. Returns the message, or None when there's no error file."""
    try:
        with open(os.path.join(DATA, "connectors", f"{service}.error"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _connector_has_refresh(service: str) -> bool:
    """Does the token file hold a refresh token? An expired access token with one is usually still
    fine (the gather refreshes), so it must NOT downgrade the tile. Unreadable file → assume yes
    (don't cry wolf; the error file is the authoritative failure signal)."""
    try:
        with open(CONNECTORS.token_path(service), encoding="utf-8") as f:
            return bool((json.load(f) or {}).get("refresh_token"))
    except (OSError, json.JSONDecodeError, ValueError):
        return True


def _telegram_link(token: str = "") -> dict:
    """What telegram_link.py captured on this volume FOR THIS BOT TOKEN: {"user_id", "bot"} or {}.
    That file is the ONE record of the Telegram handshake; this is a read of the same fact, never a
    second capture.

    The token binding is the point: a rotated bot must RE-LINK, and a record captured by the old one
    is not evidence that the new one can reach anybody (external review, Sep 1 — a stale record read
    as "linked", which started a gateway that then ate the next pairing message)."""
    try:
        with open(os.path.join(DATA, "telegram-link.json"), encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    if token and str(record.get("bot_token") or "") != token:
        return {}
    user_id = record.get("allowed_user")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return {}
    return {"user_id": user_id, "bot": str(record.get("bot_username") or "")}


def _telegram_bot_token() -> str:
    """The bot token bound to this volume's captured owner link (see the runtime adapter)."""
    return _runtime_adapter().telegram_bot_token()


def _telegram_status() -> str:
    """Telegram linked-state, and a bot token is the price of admission to any of it: "linked" when
    a token is configured AND you named the recipient yourself (TELEGRAM_ALLOWED_USERS) or this
    volume holds a capture BY THAT SAME TOKEN; "pairing" while a token is set
    and no such capture exists; "unknown" when this process can see no token at all. A capture by a
    previous token is not a link: rotating the bot means re-linking, and reading the stale record as
    linked is what let a gateway start and swallow the next pairing message."""
    token = _telegram_bot_token()
    if not token:
        # A recipient with no bot is half a configuration that delivers nothing (external review,
        # Sep 1), so this is "pairing" — held, not ready — rather than the "linked" it used to read.
        return "pairing" if (os.environ.get("TELEGRAM_ALLOWED_USERS") or "").strip() else "unknown"
    if (os.environ.get("TELEGRAM_ALLOWED_USERS") or "").strip():
        return "linked"
    return "linked" if _telegram_link(token) else "pairing"


def _channel_status(channel: str | None = None) -> str:
    """The ACTIVE delivery channel's link state — "linked" | "pairing" | "unknown" — from the one
    probe that channel has. Sotto delivers only to Photon and Telegram; any other name is "unknown"."""
    channel = channel or _deliver_target()
    if channel == "telegram":
        return _telegram_status()
    if channel == "photon":
        return _photon_status()
    return "unknown"


def _photon_status() -> str:
    """Configuration is pairing; only an authenticated owner DM is linked."""
    photon = _sotto_adapter('photon_owner')
    owner = (os.environ.get('PHOTON_HOME_CHANNEL') or '').strip()
    allowed = (os.environ.get('PHOTON_ALLOWED_USERS') or '').strip()
    managed_mode = os.environ.get('SOTTO_DEPLOYMENT_MODE') == 'managed'
    fingerprint = photon.project_fingerprint(os.environ.get('PHOTON_PROJECT_ID'))
    if ((not managed_mode and not re.fullmatch(r'\+[1-9][0-9]{7,14}', owner))
            or (managed_mode and not owner) or allowed != owner
            or not photon.restricted_allowlist()
            or not os.environ.get('PHOTON_PROJECT_ID')
            or not os.environ.get('PHOTON_PROJECT_SECRET')):
        return 'unknown'
    try:
        with open(os.path.join(DATA, 'config/photon-activation.json'), encoding='utf-8') as stream:
            receipt = json.load(stream)
    except (OSError, ValueError):
        return 'pairing'
    return ('linked' if isinstance(receipt, dict) and receipt.get('activated') is True
            and receipt.get('owner') == owner
            and receipt.get('tenant_id') == os.environ.get('SOTTO_TENANT_ID', '')
            and isinstance(receipt.get('chat_id'), str) and receipt['chat_id']
            and receipt.get('project_fingerprint') == fingerprint
            else 'pairing')


def _activation_tick() -> None:
    """Pull durable owner-DM proof; the existing Photon adapter remains the only writer."""
    if (os.environ.get('SOTTO_CRON_DELIVER') or '').strip() != 'photon':
        return
    try:
        photon = _sotto_adapter('photon_owner')
        owner = (os.environ.get('PHOTON_HOME_CHANNEL') or '').strip()
        tenant = os.environ.get('SOTTO_TENANT_ID', '')
        managed_mode = os.environ.get('SOTTO_DEPLOYMENT_MODE') == 'managed'
        project = os.environ.get('PHOTON_PROJECT_ID', '')
        if ((managed_mode and (not owner or not tenant))
                or (not managed_mode and not re.fullmatch(r'\+[1-9][0-9]{7,14}', owner))
                or os.environ.get('PHOTON_ALLOWED_USERS') != owner
                or not photon.restricted_allowlist()
                or not project or not os.environ.get('PHOTON_PROJECT_SECRET')):
            return
        fingerprint = photon.project_fingerprint(project)
        if not fingerprint:
            return
        runtime = _runtime_adapter()
        pending = runtime.activation_evidence(timeout=3)
        if pending is None:
            return
        RUNTIME_ACTIVATION.consume(
            pending, tenant_id=tenant, owner_id=owner,
            project_fingerprint=fingerprint, writer=photon.record_owner_activation,
            ack=lambda event_id: runtime.activation_ack(event_id, timeout=3))
    except Exception as error:  # next tick retries; no identity or bearer in logs
        print(f'[sotto] Photon activation retry: {type(error).__name__}', flush=True)


# ── "Is this server out of date?" (a flag, never an update pipe) ─────────────────────────────────
# One sentence: the published repo carries a VERSION stamp, this image carries the stamp it was built
# from, and when they differ Sotto says so three quiet ways — the update itself is Railway's redeploy
# (merge its template-update PR, or GitHub "Sync fork"), never something Sotto does to itself.
#
# The three places, all fed by the ONE daily check below and its ONE cache file:
#   /setup   — the mono line at the foot of the Integrations page (update_status)
#   /app     — one subdued line on Today, which goes quiet if the check stops succeeding (update_notice)
#   the brief — ONE line in the next brief, once per published version (compose_brief._append_update_notice,
#              which reads this cache file across the process boundary; `current` is in it for that reason)
# It is housekeeping, so it never spends interrupt budget and is never a push of its own.
#
# The stamp is `YYYY-MM-DD.<short-sha>`, written into the distribution tree by
# tools/prepare-public-repo.sh on every publish and copied to /app/VERSION by the Dockerfile. A tree
# that carries no stamp — the monorepo checkout ships `dev` — is a DEV deploy: no thread, no network
# call, nothing rendered. Silence IS the dev case.
PUBLIC_REPO = "kothari-nikunj/sotto"        # the canonical distribution repo (tools/publish-public.sh)
VERSION_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
LATEST_VERSION_URL = f"https://raw.githubusercontent.com/{PUBLIC_REPO}/main/VERSION"
UPDATE_DOC_URL = f"https://github.com/{PUBLIC_REPO}/blob/main/RAILWAY.md#staying-updated"
VERSION_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\.[0-9a-f]{7,40}\Z")
UPDATE_CHECK_SECS = 24 * 3600               # at most one GitHub GET per day, boot included
UPDATE_NOTICE_STALE_SECS = 48 * 3600        # two missed daily checks and the /app banner goes quiet
UPDATE_FETCH_TIMEOUT = 5                    # seconds; a slow GitHub must not hold a thread open
UPDATE_CACHE_FILENAME = "update_check.json"


def _cache_file(name: str) -> str:
    return os.path.join(DATA, "cache", name)


def local_version() -> str:
    """This build's stamp, or "" when it isn't one (dev checkout, no VERSION, garbage) — the case
    where every part of this feature stays quiet."""
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            v = f.read().strip()
    except OSError:
        return ""
    return v if VERSION_RE.match(v) else ""


def _fetch_latest_version() -> str:
    """The published stamp from the canonical repo, or "" on ANY failure (offline, 404, garbage,
    slow). Never raises: an update check that can't reach GitHub must be invisible, not an error."""
    try:
        req = urllib.request.Request(LATEST_VERSION_URL, headers={"User-Agent": "sotto-update-check"})
        with urllib.request.urlopen(req, timeout=UPDATE_FETCH_TIMEOUT) as r:   # noqa: S310 — fixed https URL
            v = r.read(4096).decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""
    return v if VERSION_RE.match(v) else ""


def _read_update_cache() -> dict:
    try:
        with open(_cache_file(UPDATE_CACHE_FILENAME), encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return c if isinstance(c, dict) else {}


def _write_update_cache(rec: dict) -> dict:
    try:
        CONNECTORS.write_json(_cache_file(UPDATE_CACHE_FILENAME), rec)
    except OSError:
        pass   # no volume — the answer still holds for this render
    return rec


def check_for_update() -> dict:
    """At most one fetch a day: reuse the cached answer while it's fresh, else fetch and rewrite the
    cache ({latest, fetched_at, current}). A failed fetch keeps the previous answer rather than
    erasing it — the flag must never flicker off because GitHub was briefly unreachable.

    THE one writer of this file, and now the only place any surface learns a version from: `current`
    is the stamp of the image that wrote the record, so the brief (which runs in the skills tree and
    can't see /app/VERSION) reads the same two facts the dashboard does. It is restamped the moment
    a redeploy lands, or a freshly-updated server would keep claiming an update for another day."""
    cached = _read_update_cache()
    current = local_version()
    try:
        age = time.time() - float(cached.get("fetched_at") or 0)
    except (TypeError, ValueError):
        age = UPDATE_CHECK_SECS
    if cached.get("latest") and 0 <= age < UPDATE_CHECK_SECS:
        stamped = cached.get("current")
        if (stamped if isinstance(stamped, str) else "") == current:
            return cached
        return _write_update_cache(dict(cached, current=current))   # a redeploy landed
    latest = _fetch_latest_version()
    if not latest:
        return cached
    return _write_update_cache({"latest": latest, "fetched_at": time.time(), "current": current})


def update_status() -> dict:
    """{current, latest, available} for the status payload. Pure local reads (the daily thread owns
    the network), and all-empty on a dev build — which is what keeps the Integrations page silent."""
    current = local_version()
    latest = ""
    if current:
        v = _read_update_cache().get("latest")
        latest = v.strip() if isinstance(v, str) else ""
    # Ordinal, not `!=`: the stamp is `YYYY-MM-DD.sha`, so it sorts by date, and a server that is
    # AHEAD of the cached answer is the NORMAL case — Railway redeploys every tracking service on
    # the publish push, which is exactly when `latest` is a day stale. Inequality alone announced
    # an "update" back to the version the customer had just left, pointing at a PR that never
    # existed. Being behind is the only thing worth saying.
    return {"current": current, "latest": latest,
            "available": bool(current and latest and latest > current)}


def update_notice() -> dict:
    """The same two version facts, for the ONE surface that speaks without being asked: the /app
    banner. Difference from update_status() (which answers a page the user opened deliberately): a
    check that hasn't succeeded in UPDATE_NOTICE_STALE_SECS says nothing at all, so a dead checker
    — no volume, GitHub blocked for days, SOTTO_UPDATE_CHECK=0 flipped after a find — cannot pin a
    banner on the dashboard forever."""
    st = update_status()
    if not st["available"]:
        return {"available": False}
    try:
        age = time.time() - float(_read_update_cache().get("fetched_at") or 0)
    except (TypeError, ValueError):
        return {"available": False}
    if not 0 <= age < UPDATE_NOTICE_STALE_SECS:
        return {"available": False}
    return {"available": True, "version": st["latest"], "current": st["current"],
            "url": UPDATE_DOC_URL}


def start_update_check_thread():
    """Daily "is a newer Sotto published?" check — same daemon-thread pattern as the Gmail poll and
    the release valve. An unstamped (dev) build never starts it, so a dev box makes no outbound
    request at all; SOTTO_UPDATE_CHECK=0 turns it off everywhere. Returns the thread, or None."""
    if (os.environ.get("SOTTO_UPDATE_CHECK", "").strip() or "1") == "0" or not local_version():
        return None

    def _loop():
        while True:
            try:
                check_for_update()
            except Exception as e:  # noqa: BLE001 — a version check must never kill its thread
                print(f"[sotto] update check error: {e}", flush=True)
            time.sleep(UPDATE_CHECK_SECS)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


def setup_status() -> dict:
    import onboarding
    gok, gmsg = google_connected()
    client_path = _google_home_path_or_none('google_client_secret.json')
    client_present = bool(client_path and os.path.exists(client_path))
    tz = _configured_tz_name()   # tzchain: SOTTO_TIMEZONE → TZ → settings.json (→ UTC)
    return {
        "bridge_connected": RELAY.bridge_connected(),
        "google_connected": gok,
        # The first brief is held until a source is really read; a linked Bridge is only transport.
        "context_waiting": _first_brief_pending() and not _context_sources_ready(),
        "first_brief": onboarding.status(DATA),
        "google_detail": gmsg,
        "google_client_present": client_present,
        "timezone": tz,
        # The channel Sotto delivers to and ITS link state — the wizard, the dashboard and the
        # completion gate all read these.
        "channel": _deliver_target(),
        "channel_status": _channel_status(),
        "connectors": CONNECTORS.service_status(),
        "last_event_at": _last_event_at(),
        "update": update_status(),
    }


def selfhost_bridge_status() -> dict:
    """Native setup projection: current channel proof, no secret-bearing setup HTML."""
    import onboarding
    import selfhost_status
    runtime = _runtime_adapter().selfhost_provisioning_status()
    channel = runtime['channel']
    google_ok, _detail = native_google_connected()
    bot = (_telegram_link(_telegram_bot_token()).get('bot', '')
           if channel == 'telegram' else '')
    return selfhost_status.response(
        runtime=runtime, google_connected=google_ok,
        context_connected=_context_sources_ready(),
        messaging_active=_channel_status(channel) == 'linked',
        first_brief=onboarding.status(DATA),
        number=os.environ.get('SOTTO_IMESSAGE_NUMBER', ''), telegram_bot=bot)


def _setup_page(code: str = "", csrf: str = "") -> str:
    """The Connections view of the site — same shell/nav as the /app dashboard, so the bookmarked
    /setup entry point IS the product. Renders live status for each step (Mac · Google · your
    delivery channel · Timezone) and only the next action you need — no jumping between four URLs or
    the Railway dashboard. Google loads LIVE (paste client → authorize → paste code), so Google needs zero
    Railway vars and zero redeploys. Timezone auto-detects from the browser. Once steps 1–4 are all
    done, a hero CTA hands you to /app (the wizard's job is over). Gated behind the setup code (the
    pairing link on this page carries the MCP bearer); internal links re-carry `?code=` so one
    authentication covers the whole wizard even if the `sotto_setup` cookie is blocked.
    Dashboard sessions instead use their existing CSRF token and never render bootstrap credentials."""
    import html as _html
    qs = f"?code={urllib.parse.quote(code)}" if code else ""
    st = setup_status()
    link = "" if csrf else pairing_link()
    el = _html.escape(link)
    host = f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else "(no public domain yet — generate one in Railway → Networking)"

    # 1 · Mac — when linked, also show the last accepted event (stamped by handle_events) so a
    # linked-but-silent Bridge is visible; no stamp yet (fresh install) renders nothing.
    ev_line = ""
    if st["bridge_connected"]:
        try:
            ago = time.time() - os.path.getmtime(_event_stamp_path())
            ev_line = f"<p class='tile-meta'>last event {_humanize_ago(ago)}</p>"
        except OSError:
            pass
    waiting = st.get("context_waiting", False)
    if st["bridge_connected"]:
        mac = ("<p class='tile-status'>Your Mac is linked and reachable. "
               "(Grant Full Disk Access in the app if you haven't.)</p>"
               + ("<p class='tile-hint'>Sotto hasn't read a Mac source besides "
                  "Contacts yet — turn one on in the Bridge, or connect Google.</p>" if waiting else "")
               + ev_line)
    elif csrf:
        mac = ("<p class='tile-status'>Your Mac is not linked.</p>"
               "<p class='tile-hint'>Use your original setup link to pair Sotto Bridge. "
               "You can connect the other services below with this sign-in.</p>")
    elif not RAILWAY_DOMAIN or not RELAY_TOKEN:
        # An empty host or token would render a dead sotto-bridge://pair?host=&token= link — name
        # what's missing instead of handing out a pairing code that can't pair.
        missing = " and ".join(
            m for m, absent in (
                ("a Bridge bearer token from its launch configuration", not RELAY_TOKEN),
                ("a public domain — Railway → Networking → Generate Domain", not RAILWAY_DOMAIN),
            ) if absent)
        mac = (f"<p class='tile-status'>Pairing isn't ready yet — this deploy still needs "
               f"{missing}.</p>"
               "<p class='tile-hint'>Rerun the setup helper (see RAILWAY.md), then reload this page.</p>")
    else:
        mac = (f"<p><a class='btn-primary' href='{el}'>Open in Sotto Bridge →</a></p>"
               "<p class='tile-hint'>Not on this Mac? Copy this and paste it into the app's "
               "“Paste pairing link” field:</p>"
               f"<p><span class='code code-block' id='c'>{el}</span> <button class='btn-quiet' "
               "onclick=\"navigator.clipboard.writeText(document.getElementById('c').innerText)\">Copy</button></p>"
               "<p class='tile-hint'>Then grant <b>Full Disk Access</b> in the app so Sotto can read Messages.</p>")

    # 2 · Google
    if st["google_connected"]:
        google = ("<p class='tile-status'>Gmail + Calendar connected.</p>"
                  f"<p class='tile-hint'><a href='/google/auth{qs}{'&' if qs else '?'}replace=1'>"
                  "Use a different Google client</a></p>")
    elif not st["google_client_present"]:
        google = _google_client_form(qs, csrf)
    else:
        google = (f"<p><a class='btn-primary' href='/google/auth{qs}'>Connect Gmail + Calendar →</a></p>"
                  + _google_replace_client(qs, csrf=csrf))

    # 3 · Your channel — the tile follows SOTTO_CRON_DELIVER and REPORTS that channel's own probe; it
    # turns "done" only when _delivery_ready says the channel is linked.
    channel = st.get("channel") or _deliver_target()
    ch_state = st.get("channel_status") or _channel_status(channel)
    if channel == "telegram":
        ch_title = "Link Telegram"
        bot = _telegram_link(_telegram_bot_token()).get("bot")
        who = f"@{_html.escape(bot)}" if bot else "your bot"
        if ch_state == "linked":
            ch_body = ("<p class='tile-status'>Telegram is linked — briefs and nudges arrive in that "
                       "chat.</p>")
        elif ch_state == "pairing":
            ch_body = (f"<p class='tile-status'>Waiting for your first message to {who} — tap the "
                       "pairing link in your deploy logs, then restart (boot captures your chat "
                       "id from it).</p>")
        else:
            # No token visible here, so there is nothing this page can check — say that plainly
            # rather than calling a gateway configured elsewhere broken.
            ch_body = ("<p class='tile-status'>Nothing to check from here — this deploy sees no bot "
                       "token, so Telegram is configured elsewhere (or not yet).</p>"
                       "<p class='tile-hint'>Briefs not arriving? Set <code>TELEGRAM_BOT_TOKEN</code> "
                       "(from <a href='https://t.me/BotFather'>@BotFather</a>) in your host's "
                       "variables and tap the pairing link boot prints — it captures your chat "
                       "id.</p>")
    elif channel == "photon":
        ch_title = "Connect iMessage"
        owner = (os.environ.get('PHOTON_HOME_CHANNEL') or '').strip()
        owner_hint = (f" ending in {_html.escape(owner[-4:])}" if
                      re.fullmatch(r'\+[1-9][0-9]{7,14}', owner) else '')
        owner_label = 'your number' + owner_hint if owner_hint else 'your owner identity'
        if ch_state == 'linked':
            ch_body = (f"<p class='tile-status'>Owner iMessage{owner_hint} was received. "
                       "Confirm Sotto's reply in Messages.</p>")
        elif ch_state == 'pairing':
            ch_body = (f"<p class='tile-status'>Photon is configured for {owner_label}. "
                       "Send one hello from that number to your Sotto iMessage line, then reload "
                       "this page. Sotto will mark this step done after receiving your direct message.</p>")
        else:
            ch_body = ("<p class='tile-status'>Photon is not ready. Complete the protected "
                       "self-host setup with your owner number and Photon project credentials, "
                       "then reload this page.</p>")
    else:
        ch_title = "Your channel"
        ch_body = (f"<p class='tile-status'><b>{_html.escape(channel)}</b> is not a channel Sotto "
                   "delivers to. Set up Photon iMessage or Telegram.</p>")
    # "done" requires an authenticated owner DM for Photon, as for the other probed channels.
    ch_done = _delivery_ready(ch_state)

    # 4 · Timezone (auto-detected by the browser; posted once)
    tzv = _html.escape(st["timezone"])
    tz_fixed = " (fixed by this installation)" if _configured_tz_source()[1].startswith("env:") else ""
    tz_block = (f"<p class='tile-status'>Timezone set to <b>{tzv}</b>{tz_fixed} — your briefs will fire at your local 6:30 am / 5:30 pm.</p>"
                if st["timezone"] else
                "<p class='tile-status' id='tzmsg'>Detecting your timezone…</p>")
    write_headers = json.dumps({'Content-Type': 'application/json', **({'X-Sotto-CSRF': csrf} if csrf else {})})
    tz_js = "" if st["timezone"] else (
        "<script>(function(){try{var tz=Intl.DateTimeFormat().resolvedOptions().timeZone;"
        f"if(!tz){{return;}}fetch('/setup/timezone{qs}',{{method:'POST',headers:{write_headers},"
        "body:JSON.stringify({timezone:tz})}).then(function(r){return r.json();}).then(function(j){"
        "var m=document.getElementById('tzmsg');if(j.ok){m.innerHTML='Timezone set to <b>'+tz+'</b>';"
        "setTimeout(function(){location.reload();},700);}else{m.textContent=j.detail||'Could not set timezone automatically.';}"
        "}).catch(function(){});}catch(e){}})();</script>")

    # 5 · Connected services (optional) — generic remote-MCP OAuth tiles driven by the registry.
    # Rendered from CONNECTORS directly (not st) so a monkeypatched setup_status can't hide them.
    # "Connected" is honest, not just token-file-present: a gather-written error file, or an expired
    # token with NO refresh token to fall back on, downgrades the tile to Reconnect (an expired
    # access token alone is fine — the gather refreshes it).
    svc_rows = []
    svc_connected = False
    for s in CONNECTORS.service_status():   # one status read per render (setup_status has its own)
        lbl = _html.escape(s["label"])
        if s["connected"]:
            err = _connector_error(s["service"])
            stale = err is not None or (s.get("expires_at") and s["expires_at"] < time.time()
                                        and not _connector_has_refresh(s["service"]))
            if stale:
                detail = (f" <span class='tile-hint'>({_html.escape(err[:120])})</span>"
                          if err else "")
                svc_rows.append(f"<p>{lbl} — <a class='btn-primary' href='/connect/{s['service']}/start{qs}'>"
                                f"Reconnect →</a>{detail}</p>")
                continue
            svc_connected = True
            when = ""
            if s.get("obtained_at"):
                when = " since " + time.strftime("%b %-d, %Y", time.localtime(s["obtained_at"]))
            svc_rows.append(f"<p class='tile-status'>{lbl} — "
                            f"connected{_html.escape(when)}. "
                            f"<a href='#' class='tile-hint' onclick=\"return sdisc('{s['service']}')\">"
                            "Disconnect</a></p>")
        else:
            svc_rows.append(
                f"<p>{lbl} — <a class='btn-primary' href='/connect/{s['service']}/start{qs}'>Connect →</a> "
                "<span class='tile-hint'>(one click — approve in your browser; "
                "tokens stay on your volume)</span></p>")
    # …and the OTHER kind of connector, in the same tile. Exa and Parallel have no OAuth server to
    # click through — they are API keys set in the host's environment — but they are connectors, and
    # a page that shows only the ones with a Connect button answers "is Exa on?" with silence.
    # Read-only: this renders whether a key is set, never the key, and never offers to store one.
    for k in CONNECTORS.key_provider_status():
        lbl = _html.escape(k["label"])
        does = _html.escape(k["does"])
        if k["connected"]:
            svc_rows.append(f"<p class='tile-status'>{lbl} — connected "
                            f"<span class='tile-hint'>({does})</span></p>")
        else:
            svc_rows.append(f"<p>{lbl} — not connected "
                            f"<span class='tile-hint'>({does}) — set "
                            f"<code>{_html.escape(k['env'])}</code> in your host's environment "
                            "and redeploy</span></p>")
    # The ladder itself, so the page says which rung actually answers rather than leaving the user
    # to infer it from three rows. Named honestly when nothing can answer.
    chain_bits = []
    for c in CONNECTORS.capability_chains():
        nice = c["capability"].replace("_", " ")
        if c["live"]:
            order = " → ".join(_html.escape(CONNECTORS.KEY_PROVIDERS[p]["label"]) for p in c["live"])
            chain_bits.append(f"{_html.escape(nice)}: {order}")
        else:
            chain_bits.append(f"{_html.escape(nice)}: <b>nothing connected</b>")
    svc_rows.append("<p class='tile-hint'>Research ladder — first one set answers: "
                    + " · ".join(chain_bits) + "</p>")

    services = ("<p class='tile-hint'>Optional extras — e.g. Granola "
                "brings meeting notes + transcripts into briefs, prep, and follow-ups.</p>"
                + "".join(svc_rows)
                # The Disconnect handler: confirm, POST /setup/disconnect, reload. Inline because the
                # setup CSP deliberately allows inline script (see SETUP_CSP) — same as the timezone
                # detector and copy button.
                + "<script>function sdisc(s){if(!confirm('Disconnect '+s+'? Its token is deleted "
                  "from your volume. You can reconnect anytime.'))return false;"
                  f"fetch('/setup/disconnect{qs}',{{method:'POST',"
                  f"headers:{write_headers},body:JSON.stringify({{service:s}})}})"
                  ".then(function(){location.reload();});return false;}</script>")

    # Mac and Google are independent; before the first brief a source counts once it was read.
    # Setup can open the dashboard while showing the first brief's actual progress. The delivery step uses the SAME
    # rule the valve and the meeting tap use (_delivery_ready): the active channel must be linked —
    # an un-messaged Photon line or Telegram bot must not celebrate over a dead delivery channel.
    context_connected = not waiting and (st["bridge_connected"] or st["google_connected"] or svc_connected)
    done = (context_connected and bool(st["timezone"]) and ch_done)
    hero = "<a class='hero-cta' href='/app'>Open your dashboard →</a>" if done else ""
    first_brief = st.get('first_brief', 'waiting')
    progress = {
        'waiting': "Sotto will prepare your first brief automatically once a context source and your message connection are ready.",
        'composing': "Sotto is preparing your first brief. You can close this page.",
        'queued': "Your first brief is ready and waiting to be sent. You do not need to request another.",
        'retrying': "Your first brief is delayed. Sotto will retry automatically; you do not need to reconnect.",
        'model_held': "Sotto needs an account allowance update before preparing your first brief.",
        'delivered': "Your first brief was sent. Check your connected chat.",
        'existing': "Your setup is connected. Scheduled briefs continue in your connected chat.",
    }.get(first_brief, "Checking your first brief.")
    footer = (f"<p class='page-sub'>{progress}</p>" if done else
              "<p class='tile-hint'>Connect a context source and your message channel, then "
              f"<a href='/setup{qs}'>check again</a>. Mac sources and Google can be connected independently.</p>")

    # Version facts, in the same quiet mono zone as the Host line at the foot of the page. This page
    # is the deliberate one — you opened it — so it states the fact whenever the cache holds it; the
    # two surfaces that speak unprompted (the /app banner, the one brief line) are the ones that go
    # quiet on a stale check. Never a nudge anywhere. Both lines stay absent on a dev build.
    upd = st.get("update") or {}
    update_line = ""
    if upd.get("available"):
        update_line = (
            f"<p class='tile-meta'>Sotto {_html.escape(str(upd.get('latest') or ''))} is available — "
            f"you're on {_html.escape(str(upd.get('current') or ''))}. One redeploy updates everything: "
            f"<a href='{UPDATE_DOC_URL}'>how to update</a>.</p>")

    return (
        _page_head("Sotto — integrations", body_class="setup")
        + "<div class='site'>"
        + _nav()
        + "<main class='content'>"
        "<div class='eyebrow'>Integrations</div>"
        "<h1 class='page-title'>What Sotto connects to</h1>"
        "<p class='page-sub'>Your agent is live. Everything connects on this page — the last step is optional.</p>"
        f"{hero}"
        + _tile(1, "Link your Mac", "done" if st["bridge_connected"] and not waiting else "todo", mac)
        + _tile(2, "Connect Google", "done" if st["google_connected"] else "todo", google)
        + _tile(3, ch_title, "done" if ch_done else "todo", ch_body)
        + _tile(4, "Timezone", "done" if st["timezone"] else "todo", tz_block + tz_js)
        + _tile(5, "Connected services", "done" if svc_connected else "optional", services)
        + (_tile(6, "Use Sotto with other assistants?", "optional",
            "<p>Connect an assistant to your selected sources. Read-only; nothing is shared until you approve.</p>"
            "<p><a href='/client-oauth/connections'>Connect or manage assistants →</a></p>"
            "<p>You can skip this and return later.</p>") if CLIENT_OAUTH.origin() else "")
        + f"{footer}"
        f"<p class='tile-meta'>Host: <code>{_html.escape(host)}</code></p>"
        f"{update_line}"
        "</main></div></body></html>"
    )


# (/pair is a legacy path: it 302-redirects to /setup in do_GET — the old standalone pair page
# is gone; the deep link + copyable pairing code live in _setup_page.)

# Setup/pairing/debug-status surface — everything here can leak the MCP bearer (pairing link) or
# accept config writes, so it's gated behind the setup code (see resolve_setup_code).
SETUP_GET_PATHS = frozenset({"/setup", "/pair", "/google/auth", "/google/connected", "/google/submit-code",
                             "/debug/google"})
SETUP_POST_PATHS = frozenset({"/setup/timezone", "/setup/google-client", "/setup/disconnect", "/google/start"})
DASHBOARD_SETUP_PATHS = frozenset({"/setup", "/google/auth", "/google/connected",
    "/google/submit-code", "/debug/google", "/setup/timezone", "/setup/google-client",
    "/setup/disconnect", "/google/start"})

# The wizard cookie carries the SAME attribute set as the dashboard's session cookie
# (dashboard._login_redirect): Secure so it never rides a plaintext hop, HttpOnly so no script can
# read it, SameSite=Lax so a foreign page can't ride it. It holds the setup code — the same secret
# that opens the dashboard — so it gets the dashboard's protection, not less. (`Secure` is fine on
# http://localhost: browsers treat localhost as a secure context.)
SETUP_COOKIE_ATTRS = "Path=/; HttpOnly; Secure; SameSite=Lax"

# The setup surface's CSP, scoped to what the wizard ACTUALLY does. Three inline scripts are load-
# bearing here — the timezone detector, the pairing-link copy button, and the connector Disconnect
# handler — so script-src must allow inline; the dashboard's `script-src 'self'` would silently
# kill timezone auto-detection. Styles
# are external (/static/app.css + setup.css) and the only image is the favicon's data: URI, so
# those two stay strict. base-uri/form-action/frame-ancestors don't inherit default-src — pinned.
SETUP_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'; "
             "img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def _owner_intents_enabled(policy) -> bool:
    if policy.get('SOTTO_DEPLOYMENT_MODE') == 'managed':
        return True
    if (policy.get('SOTTO_DEPLOYMENT_MODE') != 'self_host'
            or not policy.get('GOOGLE_AI_API_KEY')
            or 'SOTTO_MODEL_PROXY_URL' in policy
            or 'SOTTO_MODEL_PROXY_TOKEN' in policy):
        return False
    return ((policy.get('SOTTO_RUNTIME_PROFILE') == 'self-host-photon-fixed-five-v1'
             and policy.get('SOTTO_CRON_DELIVER') == 'photon')
            or (policy.get('SOTTO_RUNTIME_PROFILE') == 'self-host-telegram-fixed-five-v1'
                and policy.get('SOTTO_CRON_DELIVER') == 'telegram'))


def _owner_endpoint(handler, path):
    endpoint = OWNER_ENDPOINT_HTTP.OwnerEndpoint
    table = {
        '/runtime/owner-quiet-intent': endpoint(RUNTIME_OWNER_QUIET.perform,
            RUNTIME_OWNER_QUIET.UnavailableError, 8192, 2048, 'quiet'),
        '/runtime/owner-loop-intent': endpoint(RUNTIME_OWNER_LOOP.perform,
            RUNTIME_OWNER_LOOP.UnavailableError, 8192, 2048, 'command'),
        '/runtime/owner-reminder-intent': endpoint(RUNTIME_OWNER_REMINDER.perform,
            RUNTIME_OWNER_REMINDER.UnavailableError, 8192),
        '/runtime/owner-reminder-tool': endpoint(RUNTIME_OWNER_REMINDER_TOOL.perform,
            RUNTIME_OWNER_REMINDER_TOOL.UnavailableError),
        '/runtime/owner-loop-tool': endpoint(RUNTIME_OWNER_LOOP_TOOL.perform,
            RUNTIME_OWNER_LOOP_TOOL.UnavailableError),
        '/runtime/owner-preference-tool': endpoint(RUNTIME_OWNER_PREFERENCE_TOOL.perform,
            RUNTIME_OWNER_PREFERENCE_TOOL.UnavailableError),
        '/runtime/owner-routine-tool': endpoint(RUNTIME_OWNER_ROUTINE_TOOL.perform,
            RUNTIME_OWNER_ROUTINE_TOOL.UnavailableError),
        '/runtime/owner-gmail-draft-tool': endpoint(RUNTIME_OWNER_GMAIL_DRAFT_TOOL.perform,
            RUNTIME_OWNER_GMAIL_DRAFT_TOOL.UnavailableError),
        '/runtime/owner-calendar-tool': endpoint(RUNTIME_OWNER_CALENDAR_TOOL.perform,
            RUNTIME_OWNER_CALENDAR_TOOL.UnavailableError),
        '/runtime/owner-contacts-tool': endpoint(RUNTIME_OWNER_CONTACTS_TOOL.perform,
            RUNTIME_OWNER_CONTACTS_TOOL.UnavailableError, 262144, 256 * 1024),
        '/runtime/owner-bridge-send-tool': endpoint(
            lambda body, **kw: RUNTIME_OWNER_BRIDGE_SEND_TOOL.perform(body, relay=RELAY, **kw),
            RUNTIME_OWNER_BRIDGE_SEND_TOOL.UnavailableError),
        '/runtime/owner-brief-request': endpoint(
            lambda body, **kw: RUNTIME_OWNER_BRIEF_REQUEST.perform(
                body, queue=WORK_QUEUE, admit=request_owner_brief, **kw),
            RUNTIME_OWNER_BRIEF_REQUEST.UnavailableError),
        '/runtime/owner-skill-request': endpoint(
            lambda body, **kw: RUNTIME_OWNER_SKILL_REQUEST.perform(
                body, queue=WORK_QUEUE, admit=request_owner_skill, **kw),
            RUNTIME_OWNER_SKILL_REQUEST.UnavailableError),
        '/runtime/owner-meeting-prep-request': endpoint(
            lambda body, **kw: RUNTIME_OWNER_MEETING_PREP_REQUEST.perform(
                body, queue=WORK_QUEUE, admit=request_owner_meeting_prep,
                offers=_pending_offers(), calendar_reader=_read_prep_offer_calendar, **kw),
            RUNTIME_OWNER_MEETING_PREP_REQUEST.UnavailableError),
        '/runtime/owner-offer-reply': endpoint(
            lambda body, **kw: RUNTIME_OWNER_OFFER_REPLY.perform(
                body, offers=_pending_offers(), writer=RUNTIME_OWNER_LOOP._load_writer(), **kw),
            RUNTIME_OWNER_OFFER_REPLY.UnavailableError),
        '/runtime/owner-web-read': endpoint(
            lambda body, **kw: RUNTIME_OWNER_WEB_READ.perform(
                body, reader=RUNTIME_WEB_READER,
                cancelled=lambda: bool(select.select([handler.connection], [], [], 0)[0])
                and not handler.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT), **kw),
            RUNTIME_OWNER_WEB_READ.UnavailableError),
    }
    return table.get(path)

def _handle_owner_endpoint(handler, path, endpoint):
    if handler.client_address[0] not in ('127.0.0.1', '::1'):
        return handler._send(401, {'error': 'unauthorized'})
    if handler.path != path or not _owner_intents_enabled(os.environ):
        return handler._send(404, {'error': 'not found'})
    try:
        body = OWNER_ENDPOINT_HTTP.read_body(handler, endpoint.request_limit)
        root = os.environ.get('SOTTO_MCP_TOKEN', '')
        expected = OWNER_LOOP_CAPABILITY.derive(root, body.get('identity'))
    except (OSError, ValueError, TypeError, UnicodeError, AttributeError):
        return handler._send(400, {'error': f'invalid owner {endpoint.error_name} request'})
    if not handler._authed(expected):
        return handler._send(401, {'error': 'unauthorized'})
    stage = 'receiver_perform'
    try:
        policy = dict(os.environ, _configured_tz_name=_configured_tz_name, _tz_resolve=TZCHAIN.resolve,
                      _source_authority=_runtime_source_authority,
                      _retained_route_digest=_candidate_retained_route_digest)
        telegram = policy.get('SOTTO_RUNTIME_PROFILE') == 'self-host-telegram-fixed-five-v1'
        shared_project = body['identity']['route_mode'] == 'first_contact_shared_project'
        if shared_project:
            _activation_tick()
        result = endpoint.perform(body, bearer=expected, root=root, policy=policy, data_root=DATA,
            capability=OWNER_LOOP_CAPABILITY, activation=RUNTIME_ACTIVATION,
            telegram_verifier=_load_shared_lib('telegram_channel_proof') if telegram else None,
            chat_client=_runtime_adapter()._client(timeout=5) if telegram or shared_project else None)
        stage = 'receiver_serialize'
        response_bound = (RUNTIME_OWNER_PREFERENCE_TOOL.http_response_bound(body, result)
                          if path == '/runtime/owner-preference-tool' else endpoint.response_limit)
        if len(json.dumps(result, allow_nan=False).encode('utf-8')) > response_bound:
            raise endpoint.unavailable('response exceeds bound')
        return handler._send(409 if result['status'] == 'rejected' else 200, result)
    except ValueError:
        return handler._send(400, {'error': f'invalid owner {endpoint.error_name} request'})
    except Exception as error:  # noqa: BLE001 -- only closed diagnostics leave the private route
        failure = (RUNTIME_OWNER_LOOP_TOOL.http_failure(body, error, stage)
                   if path == '/runtime/owner-loop-tool' else {'error': f'owner {endpoint.error_name} unavailable'})
        return handler._send(503, failure)


class Handler(BaseHTTPRequestHandler):
    def _bridge_authed(self):
        import managed
        if not managed.enabled():
            return self._authed(RELAY_TOKEN)
        import cloud_pairing
        authorization = self.headers.get('Authorization', '')
        return (authorization.startswith('Bearer ') and
                cloud_pairing.bridge_authenticated(DATA, authorization[7:]))

    def _authed(self, token: str) -> bool:
        return bool(token) and hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}")

    def _setup_authed(self) -> bool:
        """Auth for the setup surface: valid `?code=`/`?setup_code=` query param, OR the `sotto_setup`
        cookie (set after the first valid code), MCP bearer, or a non-Cloud owner dashboard session.
        All comparisons constant-time. (`setup_code` exists because on /google/submit-code the `code`
        param is Google's auth code — a GET form replaces the action's query string.)"""
        self._setup_session = None
        code = resolve_setup_code()
        if not code:
            return False
        if self._authed(RELAY_TOKEN):
            return True
        want = code.encode()
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        for key in ("code", "setup_code"):
            supplied = (q.get(key) or [""])[0]
            # bytes compare: compare_digest(str, str) raises on non-ASCII attacker input
            if supplied and hmac.compare_digest(supplied.encode(), want):
                self._grant_cookie = code   # emitted as Set-Cookie on the response (see _write)
                return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sotto_setup" and v and hmac.compare_digest(v.encode(), want):
                return True
        path = self.path.split('?', 1)[0]
        if ((os.environ.get('SOTTO_DASHBOARD') or '').strip() != '0'
                and (path in DASHBOARD_SETUP_PATHS or CONNECT_START_RE.fullmatch(path))):
            session = DASHBOARD._session_record(self)
            if (session and 'cloud' not in session and isinstance(session.get('csrf'), str)
                    and session['csrf']):
                self._setup_session = session
                return True
        return False

    def _forbid_setup(self):
        # No token material, no code hints — just where to find the link.
        self._write(403, "text/plain; charset=utf-8",
                    b"Forbidden. Open the setup link (with ?code=...) from your deploy logs.\n")

    def _security_headers(self, ctype=None):
        """The header set every response out of this handler carries — the same three the dashboard
        sends on its own responses, plus the setup CSP on HTML (see SETUP_CSP). One place, so a new
        page cannot be born unprotected."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        # Unconditional: this server has NO cacheable route. Every path is the pairing link (= the
        # bearer), an OAuth code exchange, brief diagnostics, or health —
        # none of it may sit in a browser's disk cache or a shared proxy. (/static/* is the one
        # cacheable surface in the image and it belongs to dashboard.py, which sets its own.)
        self.send_header("Cache-Control", "no-store")
        if ctype and ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", SETUP_CSP)

    def _grant_header(self):
        """Emit the authenticate-once wizard cookie if this request just presented a valid ?code=."""
        google_cookie = getattr(self, '_google_cookie_header', None)
        if google_cookie:
            self.send_header('Set-Cookie', google_cookie)
            self._google_cookie_header = None
        granted = getattr(self, "_grant_cookie", None)
        if granted:
            self.send_header("Set-Cookie", f"sotto_setup={granted}; {SETUP_COOKIE_ATTRS}")
            self._grant_cookie = None

    def _google_cookie(self):
        for part in (self.headers.get('Cookie') or '').split(';'):
            key, _, value = part.strip().partition('=')
            if key == GOOGLE_BROWSER_COOKIE and re.fullmatch(r'[A-Za-z0-9_-]{43}', value):
                return value
        return ''

    def _google_callback(self):
        # No setup secret in Google's URL. Single-use state + PKCE + the initiating
        # browser's separate cookie authorize this exchange, including after reboot.
        cookie = self._google_cookie()
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        ok = False
        if (cookie and set(q).issuperset({'code', 'state'}) and 'error' not in q
                and len(q['code']) == len(q['state']) == 1):
            try:
                binding = hashlib.sha256(cookie.encode()).hexdigest()
                pending = json.loads(Path(_runtime_adapter().home_path(
                    'google_oauth_pending.json')).read_text())
                # Only this browser's pending flow may spawn the exchange; google_setup owns expiry.
                if hmac.compare_digest(pending['browser_binding'], binding):
                    callback = google_redirect_uri() + '?' + urllib.parse.urlsplit(self.path).query
                    with _GOOGLE_FLOW_LOCK:
                        ok, _ = exchange_google_code(callback, binding)
            except (OSError, ValueError, TypeError, KeyError):
                pass
        self._google_cookie_header = (f'{GOOGLE_BROWSER_COOKIE}=; Path=/google; Max-Age=0; '
                                      'Secure; HttpOnly; SameSite=Lax')
        if ok:
            return self._redirect('/google/connected')
        # Fixed clean URL: no authorization code, provider error text, or state in
        # the landing page, referrers, logs, or links.
        return self._redirect('/google/retry')

    def _redirect(self, url: str):
        """302, carrying the authenticate-once wizard cookie when the request just presented a valid
        ?code= (the Connect tile links carry it) — so the post-OAuth success page's bare /setup link
        works without re-entering the code."""
        try:
            self.send_response(302)
            self._security_headers()
            self._grant_header()
            self.send_header("Location", url)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _connect_start(self, service: str):
        """GET /connect/<service>/start (setup-code-gated): discovery + DCR + pending PKCE state,
        then 302 to the provider's consent page. Failures render a step-named page — the first click
        is the validation, so 'which leg broke' must be on it."""
        if service not in CONNECTORS.SERVICES:
            return self._html(404, _connect_error_page("config", f"unknown service '{service}' — "
                              "known: " + ", ".join(sorted(CONNECTORS.SERVICES))))
        try:
            url = CONNECTORS.start_auth(service, connect_redirect_uri())
        except CONNECTORS.ConnectorError as e:
            code = 502 if e.step in ("discovery", "registration") else 400
            return self._html(code, _connect_error_page(e.step, str(e)))
        except Exception as e:  # noqa: BLE001 — never a blank 500 on the validation click
            return self._html(500, _connect_error_page("start", f"unexpected error: {e}"))
        self._redirect(url)

    def _connect_callback(self):
        """GET /connect/oauth/callback?code&state — the IdP calls this (NOT setup-code-gated). The
        single-use pending state authenticates the flow; on success the PINNED token file is written
        and a tiny page links back to /setup. Every failure branch names its step + upstream error."""
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        state = (q.get("state") or [""])[0]
        code_ = (q.get("code") or [""])[0]
        err = (q.get("error") or [""])[0]
        if err:
            CONNECTORS.cancel_auth(state)       # serialized with callback and explicit disconnect
            desc = (q.get("error_description") or [""])[0]
            return self._html(400, _connect_error_page(
                "authorization", f"the provider returned '{err}'" + (f": {desc}" if desc else "")))
        if not state or not code_:
            return self._html(400, _connect_error_page(
                "state", "missing code/state in the callback — restart from /setup"))
        try:
            def enable_consent(record):
                try:
                    __import__('managed').record_connector_consent(
                        DATA, record["service"], True)
                except Exception:  # noqa: BLE001 — keep the credential for safe reconciliation
                    print('[sotto] connector linked but managed capability write failed', flush=True)
                    raise CONNECTORS.ConnectorError(
                        "capability", "the connection was saved but access could not be enabled; "
                        "retry or contact support") from None

            record = CONNECTORS.finish_auth(
                state, code_, connect_redirect_uri(), on_connected=enable_consent)
        except CONNECTORS.ConnectorError as e:
            code = 400 if e.step == "state" else (500 if e.step == "capability" else 502)
            return self._html(code, _connect_error_page(e.step, str(e)))
        except Exception as e:  # noqa: BLE001
            return self._html(500, _connect_error_page("exchange", f"unexpected error: {e}"))
        label = CONNECTORS.SERVICES.get(record["service"], {}).get("label", record["service"])
        import html as _html2
        return self._html(200, _connect_page(
            "Connected",
            f"<p><b>{_html2.escape(label)}</b> is linked. Tokens are stored on your volume; briefs "
            "and meeting prep will use it automatically from the next run.</p>"))

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in CLIENT_OAUTH.PATHS:
            return CLIENT_OAUTH.handle(self, self.command, DATA, DASHBOARD)
        if path == CLIENT_MCP.PATH:
            return self._client_mcp('GET')
        # Healthcheck for the platform (Railway healthcheckPath=/health → restart on failure).
        if path == "/health":
            return self._send(200, {'status': 'ok', 'bridge_connected': RELAY.bridge_connected(),
                                    'work': WORK_QUEUE.status(DATA),
                                    'delivery': OUTBOX.counts(),
                                    'model_lease': {k: v for k, v in _sotto_adapter('model_lease').status(DATA).items()
                                                    if k in ('expires_at', 'last_success_at', 'error')}})
        # A human hitting the bare service URL (Railway shows it prominently). Point at the setup
        # flow WITHOUT the code or link itself — this page is unauthenticated. Other unknown paths
        # keep their JSON 404.
        if path == "/":
            return self._html(200, _page_head("Sotto")
                + "<div class='site'><main class='content narrow'>"
                + _plain_tile("Sotto is running",
                    "<p>To finish setup, open the <code>[sotto] Setup link</code> line from your Railway "
                    "deploy logs — it carries the one-time setup code this page can't show.</p>"
                    "<p class='tile-hint'>Have a session already? <a href='/app'>Open your dashboard →</a></p>")
                + "</main></div></body></html>")
        if path == "/favicon.ico":   # browsers request it on every page load; 204 beats a JSON 404
            return self._write(204, None, None)
        # The Window (read-only dashboard): sessions, static assets, JSON API — all in dashboard.py.
        if DASHBOARD.owns(path):
            return DASHBOARD.handle(self, "GET", path)
        # Reverse-MCP: the Bridge long-polls here for the next tool call (held open ~25s).
        if path == "/bridge/poll":
            if not self._bridge_authed():
                return self._send(401, {"error": "unauthorized"})
            req = RELAY.poll(timeout=25.0)
            if not self._bridge_authed():
                RELAY.cancel(req, 'Bridge connection was revoked before execution.')
                return self._send(401, {'error': 'unauthorized'})
            return self._send(200 if req else 204, req or {})
        # (No /bridge/status: it was an unauthenticated leak of Mac presence with zero clients —
        # /health already carries `bridge_connected`.)
        # Brief diagnostics. compose_brief runs as a spawned procedure whose stderr the worker does not
        # forward to the container logs — it appends them here instead. Bearer-protected (the
        # lines can carry contact identifiers). `?n=` tails N lines (default 200).
        if path == "/debug/brief-log":
            if not self._authed(RELAY_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            try:
                n = int((q.get("n") or ["200"])[0])
            except ValueError:
                n = 200
            logpath = os.path.join(DATA, "logs", "compose_brief.log")
            try:
                with open(logpath, encoding="utf-8") as f:
                    body = "".join(f.readlines()[-n:])
            except OSError:
                body = ("(no compose_brief.log yet — the composer hasn't run on this volume. If you've "
                        "since run a brief and still see this, the agent likely improvised instead of "
                        "running compose_brief.py.)\n")
            return self._write(200, "text/plain; charset=utf-8", body.encode())
        if path == '/google/oauth/callback':
            return self._google_callback()
        if path == '/google/retry':
            return self._html(200, _narrow_page('Let’s try connecting again',
                "<section class='tile'><div class='tile-body'>"
                "<h2>Google couldn’t finish connecting</h2>"
                "<p>Your existing connection hasn’t changed. Common causes:</p><ul>"
                "<li>Sign-in was cancelled, or Gmail or Calendar access wasn’t allowed.</li>"
                "<li>Sign-in started in another browser or tab, or took longer than an hour.</li>"
                "<li>The saved Google client is wrong: its secret was rotated or deleted, it belongs "
                "to another project, or this page’s redirect URI isn’t registered on it.</li></ul>"
                "<p><a class='btn-primary' href='/google/auth'>Return to setup</a></p>"
                "<p><a href='/google/auth?replace=1'>Use a different Google client</a></p>"
                "</div></section>"))
        # Connector OAuth callback — the IdP's browser redirect lands here, so it is NOT setup-code
        # gated: the single-use `state` minted by the gated /connect/<service>/start is the auth.
        if path == "/connect/oauth/callback":
            return self._connect_callback()
        connect_start = CONNECT_START_RE.match(path)
        # Everything below is the setup/pairing/debug-status surface: it can expose the MCP bearer
        # (pairing link), so bootstrap access requires the setup code / cookie / bearer.
        # Owner dashboard sessions omit the root-bearing pairing action and setup-code links.
        # /connect/<service>/start is gated too (it spends discovery/DCR effort and mints flow state).
        if (path in SETUP_GET_PATHS or connect_start) and not self._setup_authed():
            return self._forbid_setup()
        qs = (f"?code={urllib.parse.quote(resolve_setup_code())}"
              if (path in SETUP_GET_PATHS or connect_start) and not self._setup_session else "")
        # Kick off a connector OAuth flow: discovery → DCR → pending state → 302 to the consent page.
        if connect_start:
            return self._connect_start(connect_start.group(1))
        # Google connection status — the one-glance answer to "why is Gmail/Calendar missing?" Google
        # is server-side (independent of the Bridge), so a cron brief gets it iff this says connected.
        if path == "/debug/google":
            ok, msg = google_connected()
            return self._send(200 if ok else 503, {"google_connected": ok, "detail": msg,
                                                    "authorize": "/google/auth"})
        # The unified setup wizard: live status for Mac · Google · channel · Timezone, next action inline.
        if path == "/setup":
            session = self._setup_session
            return self._html(200, _setup_page('' if session else resolve_setup_code(),
                                               session['csrf'] if session else ''))
        # (No /setup/status: it was a JSON twin of /setup with no caller. setup_status() itself
        # stays — it is what _setup_page renders from.)
        # Legacy /pair → the wizard (keeps old links/QRs working; the deep link itself is in the page).
        if path == "/pair":
            return self._redirect(f"/setup{qs}")
        if path in ('/google/auth', '/google/connected'):
            # Reuse this browser's binding so a reload or second tab cannot orphan a
            # consent already in flight; mint one only when the browser has none.
            cookie = self._google_cookie() or secrets.token_urlsafe(32)
            self._google_cookie_header = (f'{GOOGLE_BROWSER_COOKIE}={cookie}; Path=/google; '
                                          'Max-Age=3600; Secure; HttpOnly; SameSite=Lax')
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            return self._html(200, _google_authorization_page(
                qs, hashlib.sha256(cookie.encode()).hexdigest(), query.get('reconnect') == ['1'],
                query.get('replace') == ['1'],
                setup_csrf=self._setup_session['csrf'] if self._setup_session else ''))
        if path == '/google/submit-code':
            # Old bookmarks should recover into the new flow, never exchange a
            # credential carried in a GET query or redisplay a broken-loopback link.
            return self._redirect('/google/auth')
        return self._send(404, {"error": "not found"})

    def _handle_setup_post(self, path: str):
        """Setup writes require bootstrap auth or owner session plus CSRF in do_POST/this handler.
        Each operation additionally validates its own input."""
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._send(400, {"ok": False, "detail": "bad length"})
        if n <= 0 or n > MAX_BYTES:
            return self._send(400, {"ok": False, "detail": "empty or too-large body"})
        raw = self.rfile.read(n)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        session = getattr(self, '_setup_session', None)
        if session:
            supplied = self.headers.get('X-Sotto-CSRF', '')
            if not supplied and ctype == 'application/x-www-form-urlencoded':
                fields = urllib.parse.parse_qs(raw.decode('utf-8', 'replace'))
                values = fields.get('csrf', [])
                supplied = values[0] if len(values) == 1 else ''
            if not supplied or not hmac.compare_digest(supplied.encode(), session['csrf'].encode()):
                return self._send(403, {'ok': False, 'detail': 'bad csrf'})
        if path == "/setup/timezone":
            try:
                tz = (json.loads(raw or b"{}") or {}).get("timezone", "")
            except (json.JSONDecodeError, ValueError):
                tz = ""
            ok, detail = set_timezone(tz)
            return self._send(200 if ok else 400,
                              {"ok": ok, "timezone": detail if ok else None, "detail": None if ok else detail})
        # /setup/disconnect — forget a connected service's token (the /setup tile's Disconnect).
        if path == "/setup/disconnect":
            try:
                service = str((json.loads(raw or b"{}") or {}).get("service", ""))
            except (json.JSONDecodeError, ValueError):
                service = ""
            if service not in CONNECTORS.SERVICES:
                return self._send(400, {"ok": False, "detail": f"unknown service '{service}' — "
                                        "known: " + ", ".join(sorted(CONNECTORS.SERVICES))})
            def revoke_consent():
                # Revoke first while holding the connector token lock. If capability storage fails,
                # retain token and pending state rather than reporting a partial disconnect.
                try:
                    __import__('managed').record_connector_consent(DATA, service, False)
                except Exception:  # noqa: BLE001
                    print('[sotto] connector disconnect blocked: capability revocation failed', flush=True)
                    raise CONNECTORS.ConnectorError(
                        "capability", "could not record revocation") from None
            try:
                res = CONNECTORS.disconnect(service, revoke_consent)
            except CONNECTORS.ConnectorError as e:
                if e.step == "capability":
                    return self._send(500, {"ok": False, "detail": str(e)})
                raise
            except OSError:
                print('[sotto] connector disconnect failed: credential removal failed', flush=True)
                return self._send(500, {"ok": False, "detail": "credential removal failed"})
            return self._send(200, {"ok": True, **res})
        # /setup/google-client — urlencoded form (no-JS friendly) or JSON
        if ctype == "application/json":
            try:
                cj = (json.loads(raw or b"{}") or {}).get("client_json", "")
            except (json.JSONDecodeError, ValueError):
                cj = ""
        else:
            cj = (urllib.parse.parse_qs(raw.decode("utf-8", "replace")).get("client_json") or [""])[0]
        ok, msg = setup_google_client(cj)
        if ctype == "application/json":
            return self._send(200 if ok else 400, {"ok": ok, "detail": msg})
        import html as _html
        qs = '' if session else f"?code={urllib.parse.quote(resolve_setup_code())}"
        more = (f"<p><a class='btn-primary' href='/google/auth{qs}{'&' if qs else '?'}reconnect=1'>Connect Google →</a></p>"
                if ok else "")
        return self._html(200 if ok else 400, _narrow_page(
            "Sotto — Google client",
            _plain_tile(_html.escape(msg), more),
            back_href=f"/setup{qs}"))

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in CLIENT_OAUTH.PATHS:
            return CLIENT_OAUTH.handle(self, self.command, DATA, DASHBOARD)
        if path == CLIENT_MCP.PATH:
            return self._client_mcp('POST')
        if path.startswith('/selfhost/'):
            import selfhost_status
            return selfhost_status.handle(self, status=selfhost_bridge_status,
                                          setup_code=resolve_setup_code)
        endpoint = _owner_endpoint(self, path)
        if endpoint is not None:
            return _handle_owner_endpoint(self, path, endpoint)
        if path == '/runtime/brief-detail':
            # Receiver-owned, bounded read of accepted text only. No script
            # execution or model-selected filesystem path crosses this route.
            if self.client_address[0] not in ('127.0.0.1', '::1') or not self._authed(MCP_TOKEN):
                return self._send(401, {'error': 'unauthorized'})
            if self.path != path:
                return self._send(404, {'error': 'not found'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if (not 0 < size <= 4096 or self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                        != 'application/json'):
                    raise ValueError('invalid protected read')
                def unique_object(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError('duplicate JSON key')
                        result[key] = value
                    return result
                body = json.loads(self.rfile.read(size), object_pairs_hook=unique_object,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
                _load_shared_lib('protected_brief_detail').validate_selector(body)
            except (OSError, ValueError, TypeError, UnicodeError):
                return self._send(400, {'error': 'invalid brief detail request'})
            try:
                # A fresh admitted owner DM re-arms Node's activation obligation.
                # Repair it through the sole receipt writer before requiring the
                # live ACK below; the next cron heartbeat must not gate a read.
                _activation_tick()
                before = _runtime_source_authority()
                route_before = _candidate_retained_route_digest()
                if (before.get('complete') is not True or not isinstance(before.get('revision'), str)
                        or re.fullmatch(r'[a-f0-9]{64}', before['revision']) is None
                        or not isinstance(route_before, str)
                        or re.fullmatch(r'[a-f0-9]{64}', route_before) is None):
                    raise RuntimeError('source or route unavailable')
                projection = _load_shared_lib('protected_brief_detail').read(
                    DATA, body, source_revision=before['revision'], route_digest=route_before,
                    **({'zone': TZCHAIN.resolve(_configured_tz_name()) or timezone.utc}
                       if any(key in body for key in ('date', 'query', 'kind')) else {}))
                after = _runtime_source_authority()
                route_after = _candidate_retained_route_digest()
                if (after != before or route_after != route_before
                        or projection.get('status') not in ('found', 'ambiguous', 'withheld', 'not_found')
                        or len(json.dumps(projection, allow_nan=False).encode('utf-8')) > 64 * 1024):
                    raise RuntimeError('protected read authority changed')
                return self._send(200, projection)
            except Exception:  # noqa: BLE001 - never expose private proof/IO details
                return self._send(503, {'error': 'brief detail unavailable'})
        if path == '/runtime/knowledge-read':
            if self.client_address[0] not in ('127.0.0.1', '::1') or not self._authed(MCP_TOKEN):
                return self._send(401, {'error': 'unauthorized'})
            if self.path != path:
                return self._send(404, {'error': 'not found'})
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= 8192 or self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                    raise ValueError('invalid knowledge request')
                def unique_object(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError('duplicate JSON key')
                        result[key] = value
                    return result
                body = json.loads(self.rfile.read(n), object_pairs_hook=unique_object)
                RUNTIME_KNOWLEDGE_READ.validate(body)
            except (OSError, ValueError, TypeError):
                return self._send(400, {'error': 'invalid knowledge request'})
            try:
                result = RUNTIME_KNOWLEDGE_READ.perform(body,
                    script=_find_sotto_script('_shared',
                        'lib' if body['operation'] in ('relationship_importance','relationship_attention') else
                        'scripts' if body['operation'] in ('loops_query', 'granola_history', 'triage_queue', 'style_apply', 'retune_scan') else 'knowledge',
                        {'master_file': 'master_file.py', 'loops_query': 'loops_query.py',
                         'knowledge_query': 'knowledge_query.py', 'granola_history': 'granola_history.py',
                         'triage_queue': 'triage_read.py', 'style_apply': 'style_apply.py', 'retune_scan': 'retune_scan.py',
                         'relationship_importance': 'relationship_importance.py',
                         'relationship_attention': 'relationship_importance.py'}[body['operation']]),
                    executable=sys.executable, env=_spawn_env(''), authority=_runtime_source_authority,
                    **({'disconnected': lambda: bool(select.select([self.connection], [], [], 0)[0])
                        and not self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)}
                       if body['operation'] in ('granola_history', 'triage_queue') else {}))
                return self._send(200, result)
            except Exception:
                return self._send(503, {'error': 'knowledge read unavailable'})
        if path in ('/runtime/owner-loops', '/runtime/owner-retune'):
            if self.client_address[0] not in ('127.0.0.1', '::1') or not self._authed(MCP_TOKEN):
                return self._send(401, {'error': 'unauthorized'})
            if self.path != path:
                return self._send(404, {'error': 'not found'})
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= 64 or self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                    raise ValueError('invalid owner continuity request')
                def unique_object(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError('duplicate JSON key')
                        result[key] = value
                    return result

                body = json.loads(self.rfile.read(n), object_pairs_hook=unique_object)
            except (OSError, ValueError, TypeError):
                body = None
            if (type(body) is not dict or
                    (path == '/runtime/owner-loops' and
                     (set(body) != {'include_parked'} or type(body['include_parked']) is not bool)) or
                    (path == '/runtime/owner-retune' and body != {})):
                return self._send(400, {'error': 'invalid owner continuity request'})
            try:
                instant = _local_now()
                if instant.tzinfo is None or instant.utcoffset() is None:
                    instant = instant.replace(tzinfo=timezone.utc)
                result = _owner_continuity().read_owner_continuity(
                    DATA, now=instant, view='retune' if path == '/runtime/owner-retune' else 'loops',
                    include_parked=body['include_parked'] if path == '/runtime/owner-loops' else False)
                if (result.get('coverage') != 'owner_authored_only'
                        or len(json.dumps(result, allow_nan=False).encode('utf-8')) > 64 * 1024):
                    raise RuntimeError('protected read shape or bound is invalid')
                return self._send(200, result)
            except Exception:
                return self._send(503, {'error': 'owner continuity unavailable'})
        if path == '/runtime/source-authority':
            # Private read-only projection. The derived MCP bearer cannot grant
            # consent, invoke a send, or make a cached tools/list authoritative.
            if self.client_address[0] not in ('127.0.0.1', '::1') or not self._authed(MCP_TOKEN):
                return self._send(401, {'error': 'unauthorized'})
            try:
                n = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(n)) if 0 < n <= 32 else None
            except (OSError, ValueError, TypeError):
                body = None
            if body != {}:
                return self._send(400, {'error': 'body must be an empty JSON object'})
            authority = _runtime_source_authority()
            return self._send(200 if authority['complete'] else 503, authority)
        if path == '/runtime/google-read':
            # Receiver owns the OAuth token and source consent. This bearer is
            # read-only; no Google write verb or general runner is reachable.
            if self.client_address[0] not in ('127.0.0.1', '::1') or not self._authed(MCP_TOKEN):
                return self._send(401, {'error': 'unauthorized'})
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= 2048:
                    raise ValueError('invalid request length')
                def unique_object(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError('duplicate request field')
                        result[key] = value
                    return result
                body = json.loads(self.rfile.read(n), object_pairs_hook=unique_object)
                if (not isinstance(body, dict) or set(body) != {'operation', 'arguments', 'source_revision'}
                        or body['operation'] not in ('gmail_search', 'gmail_get', 'gmail_thread_get', 'calendar_list', 'calendar_get', 'contacts_search', 'contacts_get', 'capabilities')
                        or not isinstance(body['arguments'], dict)
                        or body['operation'] == 'capabilities' and body['arguments'] != {}
                        or not isinstance(body['source_revision'], str)
                        or re.fullmatch(r'[a-f0-9]{64}', body['source_revision']) is None):
                    raise ValueError('invalid request')
            except (OSError, ValueError, TypeError):
                return self._send(400, {'error': 'invalid Google read request'})
            stage = 'authority_before'
            try:
                # Shared Gmail/Calendar builders must use the exact token root
                # checked by authority; their legacy implicit-home fallback is
                # not an authorization path for this typed route.
                if not os.environ.get('SOTTO_HOME', '').strip():
                    raise RuntimeError('Google token root is not explicit')
                before = _runtime_source_authority()
                source = ('google' if body['operation'] == 'capabilities' else
                          'gmail' if body['operation'].startswith('gmail_') else
                          'google_contacts' if body['operation'].startswith('contacts_') else 'calendar')
                if (not before['complete'] or before['revision'] != body['source_revision']
                        or source != 'google' and source not in before['google_sources']):
                    raise RuntimeError('Google source is not authorized')
                if source == 'google':
                    # Original saved grants, restricted to current effective source
                    # consent. This is metadata only and performs no provider call.
                    grants = RUNTIME_OWNER_GMAIL_DRAFT_TOOL._google().capabilities()
                    flags = {key: grants.get(key) is True and permission in before['google_sources']
                             for permission, keys in (
                                 ('gmail', ('gmail_read', 'gmail_draft', 'gmail_write', 'gmail_send')),
                                 ('calendar', ('calendar_read', 'calendar_write')),
                                 ('google_contacts', ('contacts_read', 'contacts_write')))
                             for key in keys}
                    result = {'version': 1, 'source': 'google', 'operation': 'capabilities',
                              'item': {'status': 'ok', 'connected': grants.get('connected') is True and any(flags.values()),
                                       **flags},
                              'observation': {'status': 'ok', 'complete': True,
                                              'observed_at': datetime.now(timezone.utc).isoformat(),
                                              'coverage': {'since': None, 'until': None}}}
                else:
                    stage = 'reader_load'
                    api_path = _google_api_py()
                    if not api_path:
                        raise RuntimeError('Google reader unavailable')
                    spec = importlib.util.spec_from_file_location('sotto_owner_google_read', api_path)
                    api = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(api)
                    stage = 'reader_call'
                    result = api.read_owner(body['operation'], body['arguments'])
                stage = 'authority_after'
                after = _runtime_source_authority()
                if after != before:
                    raise RuntimeError('Google source changed or result invalid')
                stage = 'result_contract'
                if (result.get('source') != source
                        or len(json.dumps(result, allow_nan=False).encode()) > 64 * 1024):
                    raise RuntimeError('Google source changed or result invalid')
                return self._send(200, result)
            except Exception as error:  # noqa: BLE001 - provider errors may contain private source data
                diagnostic = GOOGLE_READ_DIAGNOSTICS.receiver_failure(error, stage, body['operation'])
                category = diagnostic['read_stage']
                try:
                    print(json.dumps({'event': 'google_read_unavailable', 'operation': body['operation'],
                                      'stage': category}), file=sys.stderr, flush=True)
                except Exception:  # noqa: BLE001 - logging must never replace the failure response
                    pass  # Diagnostics cannot change the fail-closed result.
                return self._send(503, diagnostic)
        if path.startswith('/cloud/'):
            import cloud_pairing
            return cloud_pairing.handle(self, path, DATA, adapter_factory=_cloud_google_adapter,
                                        on_timezone=accept_cloud_timezone,
                                        runtime_status=lambda: _runtime_adapter().provisioning_status())
        # Dashboard POSTs (login + the M2 write API) — dashboard.py owns auth/CSRF/lockout.
        if DASHBOARD.owns(path):
            return DASHBOARD.handle(self, "POST", path)
        if path in SETUP_POST_PATHS:
            if not self._setup_authed():
                return self._forbid_setup()
            if path == '/google/start':
                # no-referrer pages can send Origin:null in Chrome. A form secret
                # paired with a separate HttpOnly browser cookie authenticates the
                # initiating page without weakening the shared setup credential.
                cookie = self._google_cookie()
                try:
                    n = int(self.headers.get('Content-Length', '0'))
                    if not 0 < n <= 4096:
                        raise ValueError('invalid form')
                    fields = urllib.parse.parse_qs(self.rfile.read(n).decode('utf-8'))
                    csrf = fields.get('csrf', [''])[0]
                    binding = hashlib.sha256(cookie.encode()).hexdigest()
                    if (not cookie or len(fields.get('csrf', [])) != 1
                            or not hmac.compare_digest(csrf.encode(), binding.encode())
                            or self.headers.get('Origin') not in (None, 'null', public_base())):
                        raise ValueError('invalid form')
                    url = start_google_authorization(binding)
                except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                    return self._redirect('/google/retry')
                # A cross-origin redirect after a form POST is blocked by Chromium
                # under form-action 'self'. Navigate from a normal page instead.
                import html
                return self._html(200, _narrow_page('Opening Google',
                    "<section class='tile'><div class='tile-body'><h2>Opening Google…</h2>"
                    f"<p><a class='btn-primary' href='{html.escape(url, quote=True)}'>Continue with Google</a></p>"
                    "</div></section><script>window.location.replace("
                    + json.dumps(url).replace('<', r'\u003c') + ");</script>"))
            return self._handle_setup_post(path)
        if path not in ("/sotto/trigger", "/mcp", "/bridge/respond", "/bridge/events", "/chat/brief"):
            return self._send(404, {"error": "not found"})
        token = (MCP_TOKEN if path in ("/mcp", "/chat/brief")
                 else RELAY_TOKEN if path in ("/bridge/respond", "/bridge/events") else TOKEN)
        if path in ('/bridge/respond', '/bridge/events'):
            authenticated = self._bridge_authed()
        elif path == '/sotto/trigger':
            import managed
            authenticated = self._bridge_authed() if managed.enabled() else self._authed(token)
        else:
            authenticated = self._authed(token)
        if not authenticated:
            return self._send(401, {"error": "unauthorized"})
        if path == '/chat/brief':
            presented = self.headers.get('X-Sotto-Chat-Send', '')
            if not CHAT_SEND_TOKEN or not hmac.compare_digest(presented.encode(), CHAT_SEND_TOKEN.encode()):
                return self._send(403, {'error': 'chat capability required'})
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._send(400, {"error": "bad length"})
        if n <= 0 or n > MAX_BYTES:
            return self._send(413, {"error": "bad or too-large body"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return self._send(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "bad json"})
        if path == '/chat/brief':
            code, response = request_owner_brief(body)
            return self._send(code, response)
        if path in ("/sotto/trigger", "/bridge/events"):
            import managed
            try:
                managed.validate_local_ingress(DATA, body, events=path == "/bridge/events")
            except ValueError:
                return self._send(403, {"error": "Bridge source is not enabled"})
        # Reverse-MCP: the chat runtime's JSON-RPC in → relay to the Bridge → JSON-RPC out.
        if path == "/mcp":
            # The read bearer cannot authorize sends. Only the gateway's private,
            # per-boot capability can; claiming an interactive run lane does nothing.
            presented = self.headers.get('X-Sotto-Chat-Send', '')
            interactive = bool(CHAT_SEND_TOKEN and hmac.compare_digest(
                presented.encode(), CHAT_SEND_TOKEN.encode()))
            params = body.get('params') or {}
            if (body.get('method') == 'tools/call' and isinstance(params, dict)
                    and params.get('name') == 'send_message' and not interactive):
                return self._send(200, {'jsonrpc': '2.0', 'id': body.get('id'),
                    'error': {'code': -32002, 'message': 'Sending requires an explicit request in chat.'}})
            resp = RELAY.mcp_call(body)
            if body.get('method') == 'tools/list' and not interactive and isinstance(resp, dict):
                result = resp.get('result') or {}
                if isinstance(result.get('tools'), list):
                    resp = {**resp, 'result': {**result, 'tools': [tool for tool in result['tools']
                        if isinstance(tool, dict) and tool.get('name') != 'send_message']}}
            return self._send(202, {}) if resp is None else self._send(200, resp)
        # Reverse-MCP: the Bridge POSTs a tool result for a pending request id.
        if path == "/bridge/respond":
            if not RELAY.respond(body):
                return self._send(403, {'error': 'Bridge response is not authorized'})
            return self._send(202, {})
        # Event-driven ingestion: raw watcher events → dedupe → triage → verdict (maybe agent spawn).
        if path == "/bridge/events":
            code, resp = handle_events(body)
            return self._send(code, resp)
        code, resp = handle_trigger(body)
        self._send(code, resp)

    def _client_mcp(self, method):
        # No grant file means disabled. Never accept the internal root/read bearer here.
        from types import SimpleNamespace
        reader = SimpleNamespace(**{key: globals()[key] for key in (
            '_runtime_source_authority', 'RELAY', 'DATA', '_load_shared_lib', 'RUNTIME_KNOWLEDGE_READ',
            '_find_sotto_script', '_spawn_env', '_source_context', '_sotto_adapter')})
        return CLIENT_MCP.handle(self, method, DATA,
            lambda name, arguments: CLIENT_MCP_BACKEND.read(reader, name, arguments),
            oauth_authenticate=lambda token: CLIENT_OAUTH.authenticate(DATA, token, DASHBOARD.session_by_digest),
            oauth_challenge=CLIENT_OAUTH.challenge)

    def _send(self, code: int, obj: dict):
        # 204 means "no content" — it must NOT carry a body. Sending one makes strict HTTP/2 clients
        # (curl over Railway's edge) reject the response. The empty long-poll returns 204.
        if code == 204:
            self._write(204, None, None)
            return
        self._write(code, "application/json", json.dumps(obj).encode())

    def _html(self, code: int, markup: str):
        self._write(code, "text/html; charset=utf-8", markup.encode())

    def _write(self, code: int, ctype, data):
        # A client that timed out and hung up (a keepalive client does this on a slow/offline tool call)
        # closes the socket before we reply → BrokenPipe/ConnectionReset on write. That's expected, not
        # an error: swallow it so it doesn't dump a traceback per disconnect into the logs.
        try:
            self.send_response(code)
            self._security_headers(ctype)
            # First valid ?code= on the setup surface → set the authenticate-once wizard cookie.
            self._grant_header()
            if ctype is not None and data is not None:
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data is not None:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_):  # quiet
        pass


def _print_setup_link(base, code):
    line = f"[sotto] Setup link (open in a browser): {base}/setup?code={urllib.parse.quote(code)}\n"
    descriptor = os.environ.pop('SOTTO_SETUP_LOG_FD', None)
    if descriptor is None:
        print(line, end='', flush=True)
        return
    # The supervisor passes one descriptor for this line, while suppressing general output.
    fd = int(descriptor)
    if fd < 3:
        raise ValueError('invalid setup log descriptor')
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def main():
    # Upgrade managed installations linked before connector consent joined the capability file.
    # The reconciler is tenant-bound, allowlisted, and never overrides an explicit revocation.
    statuses = [row for row in CONNECTORS.service_status()
                if CONNECTORS.credential_matches(row.get('service', ''))]
    __import__('managed').reconcile_connector_capabilities(DATA, statuses)
    # Railway/Render set $PORT and require binding 0.0.0.0 (their proxy terminates TLS); locally,
    # default to loopback. Security in both cases: the bearer token + TLS at the proxy.
    port = int(os.environ.get("PORT", os.environ.get("SOTTO_TRIGGER_PORT", "8787")))
    bind = os.environ.get("SOTTO_TRIGGER_BIND", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    ready, guidance = _runtime_adapter().runtime_readiness()
    if not ready:
        raise SystemExit(guidance)
    # Delivery eligibility is mandatory. A packaged import failure must stop
    # startup before /health advertises a service that cannot deliver anything.
    _shared_effects().valid([])
    # The setup surface is code-gated; print the full setup URL ONCE so the user grabs it from the
    # deploy logs (Railway → Deployments → View logs). Everything else about the code is persisted.
    code = resolve_setup_code()
    base = f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else f"http://localhost:{port}"
    _print_setup_link(base, code)
    # Email events (Phase 2): daemon poll thread, gated on google-configured inside the thread.
    start_gmail_poll_thread()
    # Deferred-queue release valve (Step 2 item 3): heartbeat thread, channel-health gated per tick.
    start_valve_thread()
    # The receiver's own scheduler: every crons.json job fires here, so a scheduled run is
    # delivered through the outbox like everything else Sotto says.
    signal.signal(signal.SIGTERM, _stop_work)
    signal.signal(signal.SIGINT, _stop_work)
    start_work_thread()
    start_cron_thread()
    # The delivery outbox's retry heartbeat: anything the channel didn't acknowledge waits here and
    # is tried again, until it lands, ages out per its kind, or gives up loudly.
    OUTBOX.start_drain_thread()
    # "A newer Sotto is published" — one GET a day, flagged on /setup only. No-op on a dev build.
    start_update_check_thread()
    # The shared calendar cache (Step 2 item 2): refreshes cache/calendar_today.json every 15 min
    # off the SAME fetch + TTL /api/calendar uses. No skills tree on the box → idles quietly. The
    # post-meeting tap (Step 2 item 3) rides the same tick — one clock, one calendar.
    CALCACHE.start_refresh_thread()
    # Who am I? Deploys that connected Google before the derivation shipped never saw the connect
    # moment — learn the address once here, then never again (guarded: a boot must not die on it).
    try:
        _backfill_google_account_email()
    except Exception as e:  # noqa: BLE001
        print(f"[sotto] google account backfill skipped: {e}", flush=True)
    # Threaded: the Bridge's /bridge/poll holds a connection open ~25s; it must not block /mcp.
    ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
