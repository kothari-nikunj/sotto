#!/usr/bin/env python3
"""
dashboard.py — The Window, M1–M3 (docs/plans/web-dashboard-the-window.md): security rails, the
read-only JSON API, the M2 write endpoints, and the M3 live views (calendar via an on-demand
gather, persisted research cards, the action-ledger timeline) behind the /app web dashboard.
Stdlib only, same posture as receiver.py.

Loaded by receiver.py via importlib (the relay.py/connectors.py pattern). The receiver wires
/app, /app/login, /static/* and /api/* into its handler through owns()/handle(), and injects
HOOKS — late-bound lambdas over ITS module globals — so this module never imports the receiver
back and test monkeypatches on the receiver (DATA, google_connected, …) are seen here too.

Security model (M1 is read-only; the CSRF token is minted now for M2's writes):
  * The setup code stays the bootstrap secret only. Presenting it once (the login form, a valid
    `?code=` on /app, or the wizard's `sotto_setup` cookie) mints a random 32-byte session token.
    Only sha256(token) is stored — $SOTTO_DATA/dashboard_sessions.json, written atomically at
    0600 (mirrors connectors._write_json). Cookie: HttpOnly, Secure, SameSite=Lax.
  * 30-day idle expiry, checked on every authed request; last_seen bumped at most once/hour so a
    busy dashboard doesn't hammer the volume.
  * Brute-force damping: global in-memory counter, 5 failures → 60s lockout (single-user box).
    The setup surface itself has no counter today, so this covers only the dashboard login.
  * CSP (default-src 'self' + nosniff + no-referrer) on every dashboard response; the login page
    is the ONE page allowed inline styles ('unsafe-inline' in style-src only there — it renders
    before any static asset exists). /api/* additionally sends Cache-Control: no-store.
  * Kill switch: SOTTO_DASHBOARD=0 → every dashboard path answers 404.
  * Audit: login_ok / login_fail / lockout appended to $SOTTO_DATA/dashboard_audit.jsonl.
  * Reads ONLY knowledge/, briefs/, style.json, preferences.json. Never connectors/*.json token
    fields, never the setup code or any bearer in a response body. Static serving is a two-name
    whitelist — no path ever reaches the filesystem unvalidated.

Write model (M2 — "edits feed the flywheel, not a side channel"):
  * Every POST /api/* is session-gated AND CSRF-enforced: header X-Sotto-CSRF must equal the
    session's minted token (403 on mismatch/absence; SameSite=Lax already blocks the classic
    cases, this covers the rest).
  * Fact + loop writes execute knowledge_edit.py in the SKILLS TREE via subprocess (located by the
    receiver's HOOKS["find_script"], the _find_sotto_script pattern) — so a dashboard correction
    rides the identical knowledge_update.apply() / ledger code path a chat correction does. The
    receiver image carries no PyYAML; the skills tree does. Skills tree absent → 503.
  * Preferences have no chat-equivalent write path, so /api/prefs edits preferences.json directly
    (whitelisted lists only, atomic 0600 write).
  * Every successful write appends {ts, event: "write", endpoint, target, op} to
    dashboard_audit.jsonl. Input caps: text <= 500 chars; empty/whitespace ops rejected.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import html as _htmlmod
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta

# ── Wiring surface (receiver overrides these; the defaults keep the module import-safe) ──────────

HOOKS = {
    "data_root": lambda: os.environ.get("SOTTO_DATA", "/data"),
    "setup_code": lambda: "",
    "bridge_connected": lambda: False,
    "last_event_at": lambda: None,
    "google_ok": lambda: False,
    "whatsapp_ok": lambda: False,
    "connector_status": lambda: [],
    "connector_error": lambda service: None,
    "connector_has_refresh": lambda service: True,
    # Locates a skills-tree script (receiver wires _find_sotto_script). None → the skills tree is
    # not on this box and every write that needs it answers 503.
    "find_script": lambda *rel: None,
    # The ONE receiver-side calendar cache (calcache.py) — {events, generated_at, cached} or None
    # when the skills tree is absent. /api/calendar OWNS no gather of its own any more: the same
    # fetch + 10-min TTL also feeds the refresh thread that writes cache/calendar_today.json for
    # the triage in-meeting hold ("two competing caches is how drift starts" — ROADMAP Step 2).
    "calendar_snapshot": lambda: None,
}

# Frontend assets live next to this file; a parallel build produces them. Tests point this at a
# fixture dir. Serving is whitelist-only (STATIC_FILES) — the name never touches path joins unless
# it is exactly one of these keys, so traversal is structurally impossible.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    # The Connections surface (/setup and its satellite pages in receiver.py) layers this on top of
    # app.css — one product, one set of design tokens across /app and /setup.
    "setup.css": "text/css; charset=utf-8",
    # Self-hosted typefaces (the CSP forbids Google Fonts). The "fonts/" prefix is part of the
    # whitelist KEY — still an exact-match lookup, so traversal stays structurally impossible.
    # Besley (reading serif) + Martian Mono (label layer) — variable, latin subset, OFL.
    "fonts/Besley.woff2": "font/woff2",
    "fonts/Besley-italic.woff2": "font/woff2",
    "fonts/MartianMono.woff2": "font/woff2",
}

SESSION_IDLE_SECS = 30 * 24 * 3600     # 30-day idle expiry
LAST_SEEN_BUMP_SECS = 3600             # bump last_seen at most hourly (limits volume writes)
LOCKOUT_AFTER = 5                      # failed codes before the lockout engages
LOCKOUT_SECS = 60
LOGIN_BODY_MAX = 8192                  # a form with one short code; anything bigger is garbage
API_BODY_MAX = 16384                   # write bodies: an op + <=500 chars of text
TEXT_MAX = 500                         # fact/preference text cap (the plan's input cap)
EDIT_TIMEOUT_SECS = 30                 # knowledge_edit.py subprocess budget
PEOPLE_INDEX_TTL_SECS = 600            # email → person join cache, alongside the calendar cache
LEDGER_DEFAULT_DAYS = 7                # /api/ledger window default
LEDGER_MAX_DAYS = 30                   # /api/ledger window cap
LEDGER_MAX_ROWS = 200                  # /api/ledger row cap after the merge
# Per-source shares, applied BEFORE the merge (newest rows of each source). The Record braids three
# streams and they grow at wildly different rates — a chatty day of triage verdicts is dozens per
# hour, drafts and outcomes a handful per day — so one shared 200-row cap let triage push the other
# two off the page entirely. The shares sum to LEDGER_MAX_ROWS, which stays as the backstop.
# In one sentence: no single source can flood the Record.
LEDGER_MAX_PER_SOURCE = {"triage": 120, "outcome": 40, "dashboard": 40}

SLUG_RE = re.compile(r"\A[a-z0-9_-]{1,128}\Z")
DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
KIND_RE = re.compile(r"\A[a-z0-9_-]{1,32}\Z")
BRIEF_FILE_RE = re.compile(r"\A(\d{4}-\d{2}-\d{2})_([a-z0-9_-]{1,32})\.json\Z")
PERSON_API_RE = re.compile(r"\A/api/people/([a-z0-9_-]{1,128})\Z")
PERSON_FACTS_RE = re.compile(r"\A/api/people/([a-z0-9_-]{1,128})/facts\Z")
FACT_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
MEMORY_TYPE_RE = re.compile(r"\A[a-z_]{1,32}\Z")
# Real anchor keys carry colons ("email:reply:name:sarah chen", "thread:<gmail id>") plus @/./
# spaces in the id:/name: forms — so anchors travel in the JSON BODY (no URL-encoding pain) and
# are validated as printable-no-control. They are only ever compared against ledger frontmatter
# (by knowledge_edit.py), never joined into a filesystem path.
ANCHOR_RE = re.compile(r"\A[^\x00-\x1f\x7f]{1,256}\Z")

ACTIVE_LOOP_STATUSES = frozenset({"open", "waiting", "failed", "blocked"})
# Meeting-prep/meeting-info entries are calendar shadows, not asks — the docket is their surface.
# They never leave /api/loops (and so never count in the overview), even when old volume files
# still carry them.
EXCLUDED_LOOP_ACTION_TYPES = frozenset({"meeting_prep", "meeting_info"})

# preferences.json real shape (learn_preferences.py + preferences.py): top-level rule lists
# `deprioritization_hints` / `edit_heavy` (["contact|action_type", ...]), dict `approval_defaults`
# ({"contact|action_type": tier}), and the user-stated `explicit` block's four lists. No rule
# carries an enabled flag, so the one safe op is delete; `analytics`/`version`/`style` stay
# untouchable. Deleting from a dict list uses the entry VALUE as its id (stable across the
# learner's wholesale rewrites, unlike an index).
PREF_TOP_LISTS = frozenset({"deprioritization_hints", "edit_heavy"})
PREF_EXPLICIT_LISTS = frozenset({"mute_senders", "mute_people", "mute_sections", "tone_notes"})
PREF_DICTS = frozenset({"approval_defaults"})

# XSS note (the plan's rule 4): every byte of user-adjacent data leaves this module as JSON only.
# The two HTML surfaces (_login_page, the assets-missing stub) contain zero user data.

_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; frame-ancestors 'none'")
_CSP_LOGIN = ("default-src 'self'; script-src 'self'; style-src 'unsafe-inline'; "
              "img-src 'self' data:; frame-ancestors 'none'")


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _root() -> str:
    return HOOKS["data_root"]()


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if ts is None else ts))


def _from_iso(s):
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


# ── Tolerant frontmatter parser (stdlib; no PyYAML — the receiver image doesn't carry it) ────────
# Parses the exhaust-schema subset: `key: value` scalars, inline ["a","b"] lists, and nested maps
# by indentation (facts: → f_id: → fields). Anything it can't read is skipped, never raised — the
# skills-tree knowledge.py (which hard-imports yaml) stays the writer; this is a read-only window.

def _parse_scalar(s: str):
    s = s.strip()
    if not s:
        return ""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        try:
            return json.loads(s)
        except (json.JSONDecodeError, ValueError):
            inner = s[1:-1].strip()
            return [_parse_scalar(x) for x in inner.split(",")] if inner else []
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    return s


def parse_frontmatter(text: str):
    """`---\\n…\\n---\\n<body>` → (meta dict, body str). Tolerant: no frontmatter → ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in lines[1:end]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        key, _, val = stripped.partition(":")
        key = key.strip().strip("\"'")
        if not key:
            continue
        if val.strip() == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(val)
    return root, "\n".join(lines[end + 1:])


# ── Sessions ($SOTTO_DATA/dashboard_sessions.json: sha256(token) → record) ───────────────────────

_SESS_LOCK = threading.Lock()


def _sessions_path() -> str:
    return os.path.join(_root(), "dashboard_sessions.json")


def _load_sessions() -> dict:
    try:
        with open(_sessions_path(), encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_sessions(sess: dict) -> None:
    """Atomic (tmp + replace) 0600 write — mirrors connectors._write_json: a crash mid-write can't
    corrupt the store, and the volume never holds it world-readable."""
    path = _sessions_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(sess, f)
    os.replace(tmp, path)


def mint_session() -> str:
    """Mint a session: 32 random bytes out to the cookie, only the hash (plus a per-session CSRF
    token for M2) on disk. Expired sessions are pruned on the way through. Returns the raw token."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    now_iso = _iso()
    cutoff = time.time() - SESSION_IDLE_SECS
    with _SESS_LOCK:
        sess = {k: v for k, v in _load_sessions().items()
                if isinstance(v, dict) and (_from_iso(v.get("last_seen")) or 0) >= cutoff}
        sess[digest] = {"created": now_iso, "last_seen": now_iso,
                        "csrf": secrets.token_urlsafe(32)}
        try:
            _write_sessions(sess)
        except OSError:
            pass  # unwritable volume: the mint still answers, the session just won't persist
    return token


def _cookie(h, name: str):
    for part in (h.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name and v:
            return v
    return None


def _session_record(h):
    """The live session record for this request's sotto_session cookie, or None. Enforces the
    30-day idle expiry (expired entries are dropped) and bumps last_seen at most once an hour."""
    token = _cookie(h, "sotto_session")
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _SESS_LOCK:
        sess = _load_sessions()
        rec = sess.get(digest)
        if not isinstance(rec, dict):
            return None
        last = _from_iso(rec.get("last_seen")) or 0
        if now - last > SESSION_IDLE_SECS:
            sess.pop(digest, None)
            try:
                _write_sessions(sess)
            except OSError:
                pass
            return None
        if now - last >= LAST_SEEN_BUMP_SECS:
            rec["last_seen"] = _iso(now)
            try:
                _write_sessions(sess)
            except OSError:
                pass
        return rec


# ── Brute-force damping + audit ──────────────────────────────────────────────────────────────────

_LOGIN_LOCK = threading.Lock()
_LOGIN_STATE = {"fails": 0, "locked_until": 0.0}


def _lockout_remaining() -> float:
    with _LOGIN_LOCK:
        return max(0.0, _LOGIN_STATE["locked_until"] - time.time())


def _note_login_failure() -> bool:
    """Count a failed code presentation. True when THIS failure engaged the lockout."""
    with _LOGIN_LOCK:
        _LOGIN_STATE["fails"] += 1
        if _LOGIN_STATE["fails"] >= LOCKOUT_AFTER:
            _LOGIN_STATE["locked_until"] = time.time() + LOCKOUT_SECS
            _LOGIN_STATE["fails"] = 0
            return True
    return False


def _note_login_success() -> None:
    with _LOGIN_LOCK:
        _LOGIN_STATE["fails"] = 0


def _audit(event: str, **fields) -> None:
    """Append {ts, event, **fields} to $SOTTO_DATA/dashboard_audit.jsonl (best-effort, never
    raises). Writes use _audit("write", endpoint=…, target=…, op=…) per the plan's audit rail."""
    try:
        path = os.path.join(_root(), "dashboard_audit.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _iso(), "event": event, **fields}) + "\n")
    except OSError:
        pass


# ── Response plumbing (every dashboard byte leaves through these — headers are non-optional) ─────

def _headers(api: bool = False, login: bool = False) -> list:
    hs = [("Content-Security-Policy", _CSP_LOGIN if login else _CSP),
          ("X-Content-Type-Options", "nosniff"),
          ("Referrer-Policy", "no-referrer")]
    if api:
        hs.append(("Cache-Control", "no-store"))
    return hs


def _respond(h, code: int, ctype, data, headers=()):
    try:
        h.send_response(code)
        for k, v in headers:
            h.send_header(k, v)
        if ctype is not None and data is not None:
            h.send_header("Content-Type", ctype)
            h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        if data is not None:
            h.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
        pass  # client hung up — expected, not an error (same posture as receiver._write)


def _json(h, code: int, obj: dict):
    _respond(h, code, "application/json", json.dumps(obj).encode(), _headers(api=True))


def _page(h, code: int, markup: str, login: bool = False):
    _respond(h, code, "text/html; charset=utf-8", markup.encode(), _headers(login=login))


def _login_redirect(h, token: str):
    """302 → clean /app carrying the freshly minted session cookie. The setup code NEVER appears
    in a dashboard URL — this redirect is what scrubs it after ?code=/cookie auto-login."""
    _respond(h, 302, None, None,
             _headers() + [("Location", "/app"),
                           ("Set-Cookie", f"sotto_session={token}; Path=/; HttpOnly; Secure; "
                                          "SameSite=Lax")])


# ── Login page (the one inline-CSS exception; zero user data, zero JS) ───────────────────────────

def _login_page(msg: str = "") -> str:
    # The dashboard's design language (oat parchment / iron ink / banker's-green, Besley serif),
    # self-contained: inline CSS only (this page's CSP allows inline styles and nothing else).
    # /static/* serves without a session, so the vendored fonts CAN be declared here via
    # @font-face — font-src falls back to default-src 'self'. Georgia covers a cold start.
    err = f"<p class='err'>{_htmlmod.escape(msg)}</p>" if msg else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='color-scheme' content='light dark'>"
        "<title>Sotto — sign in</title><style>"
        "@font-face{font-family:'Besley';src:url('/static/fonts/Besley.woff2') format('woff2');"
        "font-weight:400 900;font-style:normal;font-display:swap}"
        "@font-face{font-family:'Besley';src:url('/static/fonts/Besley-italic.woff2') format('woff2');"
        "font-weight:400 900;font-style:italic;font-display:swap}"
        "@font-face{font-family:'Martian Mono';src:url('/static/fonts/MartianMono.woff2') format('woff2');"
        "font-weight:100 800;font-style:normal;font-display:swap}"
        "body{font-family:'Besley',Georgia,serif;background:#f4efe4;color:#221c12;"
        "max-width:400px;margin:16vh auto 0;padding:0 16px;line-height:1.55}"
        ".card{background:#fbf8f0;border-radius:12px;padding:28px 24px;"
        "box-shadow:0 0 0 1px rgba(56,46,28,.09),0 2px 12px rgba(56,46,28,.07);"
        "border-top:2.5px solid #221c12}"
        "h1{font-size:26px;font-style:italic;font-weight:700;letter-spacing:-.02em;margin:0 0 4px}"
        "h1 span{color:#26604e;font-style:normal}"
        "p{color:#6b6250;font-style:italic;margin:4px 0 16px;font-size:15px}"
        "label{display:block;font-family:'Martian Mono',ui-monospace,monospace;font-size:10px;"
        "text-transform:uppercase;letter-spacing:.04em;color:#746b57;margin:0 0 6px}"
        "input{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ddd4c0;"
        "border-radius:8px;background:#fbf8f0;color:#221c12;"
        "font-family:'Martian Mono',ui-monospace,monospace;font-size:16px}"
        "input:focus{outline:none;border-color:#26604e;box-shadow:0 0 0 3px rgba(38,96,78,.12)}"
        "button{margin-top:12px;width:100%;padding:11px;border-radius:8px;"
        "border:1px solid #a89d86;"
        "background:#f4efe4;color:#221c12;font-family:'Martian Mono',ui-monospace,monospace;"
        "font-size:12px;text-transform:uppercase;letter-spacing:.04em;font-weight:600;"
        "cursor:pointer}button:hover{background:#ece5d6}button:active{transform:scale(.98)}"
        ".err{font-family:'Martian Mono',ui-monospace,monospace;font-style:normal;font-size:11px;"
        "text-transform:uppercase;letter-spacing:.02em;color:#a4321f}"
        "@media(prefers-color-scheme:dark){body{background:#191510;color:#eae3d3}"
        ".card{background:#251f17;border-top-color:#eae3d3;"
        "box-shadow:0 0 0 1px rgba(234,227,211,.08)}"
        "p{color:#a89d86}label{color:#998e75}.err{color:#e2705a}"
        "h1 span{color:#95b2a4}"
        "input{background:#191510;border-color:#362e20;color:#eae3d3}"
        "input:focus{border-color:#64887a;box-shadow:0 0 0 3px rgba(149,178,164,.16)}"
        "button{background:#201b14;border-color:#6e6450;color:#eae3d3}"
        "button:hover{background:#2a2419}}"
        "</style></head><body><div class='card'>"
        "<h1>Sotto<span>.</span></h1><p>Your chief of staff keeps this door locked.</p>"
        f"{err}"
        "<form method='post' action='/app/login'>"
        "<label for='code'>Setup code — printed in your deploy logs</label>"
        "<input id='code' name='code' type='password' placeholder='••••••••••••' "
        "autocomplete='current-password' "
        "autocapitalize='off' autocorrect='off' spellcheck='false' autofocus>"
        "<button>Open the dashboard</button></form></div></body></html>")


# ── Route dispatch (receiver.do_GET/do_POST call owns() then handle()) ───────────────────────────

def owns(path: str) -> bool:
    return (path in ("/app", "/app/login")
            or path.startswith("/static/")
            or path.startswith("/api/"))


def handle(h, method: str, path: str):
    # Kill switch first: SOTTO_DASHBOARD=0 makes the whole surface indistinguishable from absent.
    if (os.environ.get("SOTTO_DASHBOARD") or "").strip() == "0":
        return _json(h, 404, {"error": "not found"})
    if method == "POST":
        if path == "/app/login":
            return _handle_login(h)
        if path.startswith("/api/"):
            return _handle_api_post(h, path)
        return _json(h, 404, {"error": "not found"})
    if path == "/app":
        return _handle_app(h)
    if path == "/app/login":  # GETting the login endpoint just lands you on /app's login flow
        return _respond(h, 302, None, None, _headers() + [("Location", "/app")])
    if path.startswith("/static/"):
        return _handle_static(h, path)
    return _handle_api(h, path)


def _handle_app(h):
    if _session_record(h) is not None:
        return _serve_app_html(h)
    # Auto-login: a valid ?code= or the wizard's sotto_setup cookie is the same proof as the login
    # form — a user arriving from /setup must not type the code twice. Both mint a real session and
    # 302 to the CLEAN /app URL (the code never lives in dashboard links/history).
    q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
    supplied = _s((q.get("code") or [""])[0])
    want = (HOOKS["setup_code"]() or "").encode()
    if want and _lockout_remaining() <= 0:
        wizard = _s(_cookie(h, "sotto_setup") or "")
        for cand in (supplied, wizard):
            if cand and hmac.compare_digest(cand.encode(), want):
                _note_login_success()
                _audit("login_ok")
                return _login_redirect(h, mint_session())
        if supplied:  # an explicit wrong ?code= is a brute-force attempt — it counts
            _audit("login_fail")
            if _note_login_failure():
                _audit("lockout")
    return _page(h, 200, _login_page(), login=True)


def _read_form(h) -> dict:
    try:
        n = int(h.headers.get("Content-Length", 0))
    except ValueError:
        return {}
    if n <= 0 or n > LOGIN_BODY_MAX:
        return {}
    return urllib.parse.parse_qs(h.rfile.read(n).decode("utf-8", "replace"))


def _handle_login(h):
    if _lockout_remaining() > 0:
        return _page(h, 429, _login_page("Too many attempts — try again in a minute."), login=True)
    supplied = _s((_read_form(h).get("code") or [""])[0])
    want = (HOOKS["setup_code"]() or "").encode()
    if want and supplied and hmac.compare_digest(supplied.encode(), want):
        _note_login_success()
        _audit("login_ok")
        return _login_redirect(h, mint_session())
    _audit("login_fail")
    if _note_login_failure():
        _audit("lockout")
        return _page(h, 429, _login_page("Too many attempts — try again in a minute."), login=True)
    return _page(h, 403, _login_page("That code didn't match."), login=True)


def _serve_app_html(h):
    try:
        with open(os.path.join(STATIC_DIR, "app.html"), "rb") as f:
            data = f.read()
    except OSError:  # frontend not built/deployed yet — still a working (if bare) page
        data = b"<!doctype html><title>Sotto</title><p>dashboard assets missing</p>"
    return _respond(h, 200, "text/html; charset=utf-8", data, _headers())


def _handle_static(h, path: str):
    """Whitelist-only static serving: the request name must EQUAL a STATIC_FILES key before any
    filesystem path is built, so ../ (encoded or not) can never traverse — it just isn't a key."""
    name = path[len("/static/"):]
    ctype = STATIC_FILES.get(name)
    if ctype is None:
        return _json(h, 404, {"error": "not found"})
    try:
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            data = f.read()
    except OSError:
        return _respond(h, 200, "text/plain; charset=utf-8", b"dashboard assets missing", _headers())
    hdrs = _headers()
    if name.startswith("fonts/"):   # immutable vendored files — spare the phone a re-download
        hdrs.append(("Cache-Control", "public, max-age=604800, immutable"))
    else:                           # js/css must revalidate, or a redeploy ships a stale UI
        hdrs.append(("Cache-Control", "no-cache"))
    return _respond(h, 200, ctype, data, hdrs)


# ── Read-only JSON API (all session-gated; tolerant readers — missing data is empty, never 500) ──

def _handle_api(h, path: str):
    rec = _session_record(h)
    if rec is None:
        return _json(h, 401, {"error": "unauthorized"})
    try:
        if path == "/api/session":
            return _json(h, 200, {"csrf": _s(rec.get("csrf"))})
        if path == "/api/overview":
            return _json(h, 200, api_overview())
        if path == "/api/loops":
            return _json(h, 200, api_loops())
        if path == "/api/briefs":
            return _api_briefs(h)
        if path == "/api/people":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
            return _json(h, 200, api_people((q.get("q") or [""])[0]))
        m = PERSON_API_RE.match(path)
        if m:
            obj = api_person(m.group(1))
            return _json(h, 200, obj) if obj is not None else _json(h, 404, {"error": "not found"})
        if path == "/api/learned":
            return _json(h, 200, api_learned())
        if path == "/api/calendar":
            return _json(h, 200, api_calendar())
        if path == "/api/research":
            return _json(h, 200, api_research())
        if path == "/api/ledger":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
            return _json(h, 200, api_ledger((q.get("days") or [""])[0]))
        return _json(h, 404, {"error": "not found"})
    except Exception as e:  # noqa: BLE001 — a data-shape surprise must not 500 the dashboard
        print(f"[sotto] dashboard api error on {path}: {e}", flush=True)
        return _json(h, 200, {"error": "unreadable"})


def _read_json_file(*parts, default=None):
    try:
        with open(os.path.join(_root(), *parts), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def _list_md(dirname: str):
    """[(slug, frontmatter)] for knowledge/<dirname>/*.md — unreadable/unparseable files skipped."""
    d = os.path.join(_root(), "knowledge", dirname)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = []
    for n in names:
        if not n.endswith(".md"):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                meta, _ = parse_frontmatter(f.read())
        except OSError:
            continue
        out.append((n[:-3], meta))
    return out


def _local_today() -> str:
    """The user's local date: SOTTO_TIMEZONE / TZ / settings.json timezone via zoneinfo, else server
    local. Every wall-clock feature resolves the day the same way — the skills tree's
    `timeutil.configured_tz()` is the CANONICAL resolution (env SOTTO_TIMEZONE or TZ, then the
    wizard's config/settings.json), and this mirrors it because the receiver image can't import the
    skills tree (it may not be on the box at all). Change one, change the other."""
    tz = _s(os.environ.get("SOTTO_TIMEZONE") or os.environ.get("TZ") or "")
    if not tz:
        settings = _read_json_file("config", "settings.json", default={}) or {}
        tz = _s(settings.get("timezone")) if isinstance(settings, dict) else ""
    if tz:
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415 — stdlib; imported lazily, may miss tzdata
            return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            pass
    return time.strftime("%Y-%m-%d")


def _brief_files():
    """[(date, kind, filename)] for briefs/<date>_<kind>.json, newest first. The trigger's
    .claim/.delivered/.payload.json siblings live in the same dir and are excluded by the regex."""
    try:
        names = os.listdir(os.path.join(_root(), "briefs"))
    except OSError:
        return []
    out = [(m.group(1), m.group(2), n) for n in names if (m := BRIEF_FILE_RE.match(n))]
    out.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return out


def _granola_state() -> str:
    """'ok' | 'reconnect' | 'absent' — the same honesty rules as the /setup tile: an error file
    from the gather, or an expired token with no refresh token, means Reconnect, not ok."""
    try:
        for s in HOOKS["connector_status"]():
            if s.get("service") != "granola":
                continue
            if not s.get("connected"):
                return "absent"
            if HOOKS["connector_error"]("granola") is not None:
                return "reconnect"
            exp = s.get("expires_at")
            if exp and exp < time.time() and not HOOKS["connector_has_refresh"]("granola"):
                return "reconnect"
            return "ok"
    except Exception:  # noqa: BLE001
        pass
    return "absent"


def _hook_bool(name: str) -> bool:
    try:
        return bool(HOOKS[name]())
    except Exception:  # noqa: BLE001
        return False


def api_overview() -> dict:
    today = _local_today()
    try:
        last_event = HOOKS["last_event_at"]()
    except Exception:  # noqa: BLE001
        last_event = None
    return {
        "date": today,
        "briefs_today": [{"kind": k, "file": f} for (d, k, f) in _brief_files() if d == today],
        "loops_active": len(api_loops()["loops"]),
        "last_event_at": last_event,
        "bridge_connected": _hook_bool("bridge_connected"),
        "services": {
            "google": _hook_bool("google_ok"),
            "whatsapp": _hook_bool("whatsapp_ok"),
            "granola": _granola_state(),
        },
    }


def api_loops() -> dict:
    d = os.path.join(_root(), "knowledge", "continuity")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        names = []
    loops = []
    for n in names:
        if not n.endswith(".md"):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                meta, _ = parse_frontmatter(f.read())
        except OSError:
            continue
        status = _s(meta.get("status")).lower()
        if status not in ACTIVE_LOOP_STATUSES:
            continue
        action_type = _s(meta.get("action_type"))
        if re.sub(r"[\s-]+", "_", action_type.lower()) in EXCLUDED_LOOP_ACTION_TYPES:
            continue
        surfaced = meta.get("times_surfaced")
        loops.append({
            "anchor_key": _s(meta.get("anchor_key")),
            "action_type": action_type,
            "channel": _s(meta.get("channel")),
            "contact_name": _s(meta.get("contact_name")),
            "status": status,
            "created_at": _s(meta.get("created_at")),
            "times_surfaced": surfaced if isinstance(surfaced, int) else 0,
            "summary": _s(meta.get("summary")),
            "meeting_time": _s(meta.get("meeting_time")) or None,
        })
    loops.sort(key=lambda x: x["created_at"], reverse=True)
    return {"loops": loops}


def _api_briefs(h):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
    date = _s((q.get("date") or [""])[0])
    kind = _s((q.get("kind") or [""])[0])
    if not date and not kind:
        return _json(h, 200, {"briefs": [{"date": d, "kind": k} for (d, k, _) in _brief_files()]})
    # Strict param validation BEFORE any path is built (date/kind feed a filename).
    if not DATE_RE.match(date) or not KIND_RE.match(kind):
        return _json(h, 400, {"error": "bad date/kind"})
    data = _read_json_file("briefs", f"{date}_{kind}.json", default=None)
    if data is None:
        return _json(h, 404, {"error": "not found"})
    # chat_text: the delivered chat rendering when the archive carries it. compose_brief emits it
    # as `brief_text` (render_chat_text of brief_markdown); older/other shapes may say chat_text.
    # Nothing stored → null, and the frontend renders from `data` itself.
    chat = None
    if isinstance(data, dict):
        for key in ("chat_text", "brief_text"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                chat = v
                break
    return _json(h, 200, {"date": date, "kind": kind, "data": data, "chat_text": chat})


def _title_from_slug(slug: str) -> str:
    """"acme-corp" → "Acme Corp" — the display fallback when frontmatter carries no name."""
    return " ".join(w.capitalize() for w in re.split(r"[_-]+", slug) if w)


def _body_sections(body: str):
    """Company markdown body → [{heading, text}]. Tolerant: `## Heading` (any # depth) starts a
    section; a headingless body becomes one unnamed section; heading-only sections are dropped.
    Text passes through verbatim — the frontend renders it as textContent, so any markdown that
    survives is inert."""
    sections = []
    heading, buf = "", []

    def flush():
        text = "\n".join(buf).strip()
        if text:
            sections.append({"heading": heading, "text": text})

    for line in (body or "").split("\n"):
        m = re.match(r"\A#{1,6}\s+(.*\S)\s*\Z", line)
        if m:
            flush()
            heading, buf = m.group(1), []
        else:
            buf.append(line)
    flush()
    return sections


def _active_fact_count(facts) -> int:
    if not isinstance(facts, dict):
        return 0
    return sum(1 for f in facts.values()
               if isinstance(f, dict) and _s(f.get("status") or "active") == "active")


def api_people(query: str = "") -> dict:
    ql = _s(query).lower()
    people = []
    for typ, dirname in (("person", "people"), ("company", "companies")):
        for slug, meta in _list_md(dirname):
            name = _s(meta.get("name")) or (_title_from_slug(slug) if typ == "company" else slug)
            company = _s(meta.get("company"))
            if ql and ql not in name.lower() and ql not in company.lower():
                continue
            people.append({
                "slug": slug,
                "type": typ,
                "name": name,
                "company": company or None,
                "title": _s(meta.get("title")) or None,
                "fact_count": _active_fact_count(meta.get("facts")),
                "updated_at": _s(meta.get("updated_at")) or None,
            })
    people.sort(key=lambda p: p["name"].lower())
    return {"people": people[:200]}


def api_person(slug: str):
    """Frontmatter fields + normalized facts for people/<slug>.md, else companies/<slug>.md, else
    None. The slug is regex-validated at the route (and re-checked here) before any path join.
    Companies keep their substance in the markdown BODY (About/News/Context prose, per
    contracts/exhaust-schema.md), so they additionally carry `sections` and a guaranteed name."""
    if not SLUG_RE.match(slug):
        return None
    for typ, dirname in (("person", "people"), ("company", "companies")):
        path = os.path.join(_root(), "knowledge", dirname, slug + ".md")
        try:
            with open(path, encoding="utf-8") as f:
                meta, body = parse_frontmatter(f.read())
        except OSError:
            continue
        raw_facts = meta.pop("facts", None)
        facts = []
        if isinstance(raw_facts, dict):
            for fid, fv in raw_facts.items():
                if not isinstance(fv, dict):
                    continue
                conf = fv.get("conf")
                facts.append({
                    "id": fid,
                    "text": _s(fv.get("text")),
                    "type": _s(fv.get("type")),
                    "status": _s(fv.get("status")) or "active",
                    "seen": fv.get("seen") if isinstance(fv.get("seen"), int) else 0,
                    "conf": float(conf) if isinstance(conf, (int, float)) else 0.0,
                    "source": _s(fv.get("source")),
                    "source_ref": _s(fv.get("source_ref")),
                    "first": _s(fv.get("first")),
                    "last": _s(fv.get("last")),
                })
        facts.sort(key=lambda fx: (0 if fx["status"] == "active" else 1, -fx["conf"]))
        out = dict(meta)
        out.update({"slug": slug, "type": typ, "facts": facts})
        if typ == "company":
            out["sections"] = _body_sections(body)
            out["name"] = _s(meta.get("name")) or _title_from_slug(slug)
        return out
    return None


def api_learned() -> dict:
    """Style is summarized, never dumped: the fingerprint carries verbatim message samples, so only
    the REGISTER names — the keys of style.json's `canonical` block (work_email / work_message /
    personal_message) — and the per_person count leave this endpoint. Top-level keys are schema
    bookkeeping (canonical/master/sample_keys/…), never shown. Missing canonical → no buckets."""
    style = _read_json_file("style.json", default=None)
    summary: dict = {}
    if isinstance(style, dict):
        per = style.get("per_person")
        canonical = style.get("canonical")
        summary = {
            "buckets": sorted(canonical) if isinstance(canonical, dict) else [],
            "per_person": len(per) if isinstance(per, dict) else 0,
        }
        if isinstance(style.get("updated_at"), str):
            summary["updated_at"] = style["updated_at"]
    prefs = _read_json_file("preferences.json", default=None)
    if not isinstance(prefs, (dict, list)):
        prefs = {}
    return {"style": summary, "preferences": prefs}


# ── M3 live views (session-gated GETs: calendar, research cards, action-ledger timeline) ─────────

def _date_shift(date: str, days: int) -> str:
    """YYYY-MM-DD ± days, or "" for an unparseable input."""
    try:
        return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


# The calendar gather + its 10-minute cache used to live here. It moved to calcache.py (reached
# through HOOKS["calendar_snapshot"]) so the dashboard endpoint and the triage in-meeting hold
# consume ONE cache — see that module's header. What stays here is the serve-time knowledge-graph
# join below: the cache is raw by design, and only the docket wants attendees to carry identities.

# The knowledge-graph join for the docket: email → {name, title, company, slug} from
# knowledge/people frontmatter (`identifiers` carries the emails). Cached ~10 min alongside the
# calendar cache so a dashboard refresh re-reads neither the volume nor the graph every time.
_PEOPLE_IDX_LOCK = threading.Lock()
_PEOPLE_IDX_CACHE: dict = {"ts": 0.0, "value": None}


def _people_email_index() -> dict:
    """email (lowercased) → {name, title, company, slug} for every person on file. Tolerant:
    identifiers may be absent, non-list, or carry phone numbers — only @-shaped strings index.
    First person to claim an address keeps it (slug-sorted, so the winner is stable)."""
    now = time.time()
    with _PEOPLE_IDX_LOCK:
        val = _PEOPLE_IDX_CACHE["value"]
        if val is not None and now - _PEOPLE_IDX_CACHE["ts"] < PEOPLE_INDEX_TTL_SECS:
            return val
    idx: dict = {}
    for slug, meta in _list_md("people"):
        hit = {
            "name": _s(meta.get("name")) or _title_from_slug(slug),
            "title": _s(meta.get("title")),
            "company": _s(meta.get("company")),
            "slug": slug,
        }
        cands = list(meta.get("identifiers")) if isinstance(meta.get("identifiers"), list) else []
        cands.append(meta.get("email"))
        for cand in cands:
            email = cand.strip().lower() if isinstance(cand, str) else ""
            if "@" in email and email not in idx:
                idx[email] = hit
    with _PEOPLE_IDX_LOCK:
        _PEOPLE_IDX_CACHE["ts"] = now
        _PEOPLE_IDX_CACHE["value"] = idx
    return idx


def _enrich_calendar_events(events):
    """A copy of the normalized events with each attendee joined against the people index:
    known addresses gain title/company/slug (empty join fields are dropped) and a name when the
    calendar had none. Unknown attendees pass through untouched."""
    idx = _people_email_index()
    out = []
    for ev in events:
        atts = []
        for a in ev.get("attendees") or []:
            email = _s(a.get("email")).lower()
            hit = idx.get(email) if email else None
            if hit:
                a = dict(a)
                if not _s(a.get("name")):
                    a["name"] = hit["name"]
                if hit["title"]:
                    a["title"] = hit["title"]
                if hit["company"]:
                    a["company"] = hit["company"]
                a["slug"] = hit["slug"]
            atts.append(a)
        out.append({**ev, "attendees": atts})
    return out


def api_calendar() -> dict:
    """GET /api/calendar → {events, generated_at, cached} (calcache.py's 10-min cache), or
    {events: [], unavailable: true} — 200, not 503: the Today view degrades quietly — when the
    skills tree isn't on this box. A failed/empty gather is cached too, so a broken Google setup
    can't make every dashboard refresh fork a 60s subprocess. Attendees are joined against the
    people index at serve time (its own 10-min cache), so the docket carries who each attendee
    IS — the cache itself stays raw (the in-meeting hold reads the same fetch and wants no names).
    The gather, the TTL and the normalization all live in calcache.py now; this is the view."""
    snap = HOOKS["calendar_snapshot"]()
    if not isinstance(snap, dict):      # None = skills tree absent (or the hook is unwired)
        return {"events": [], "unavailable": True}   # cheap check — nothing worth caching
    events = snap.get("events") or []
    return {"events": _enrich_calendar_events(events),
            "generated_at": _s(snap.get("generated_at")),
            "cached": bool(snap.get("cached"))}


def api_research() -> dict:
    """GET /api/research → today's research cards from $SOTTO_DATA/cache/research_<date>.json
    (research_attendees.py's persistence hook), falling back to yesterday's flagged stale.
    Entries pass through VERBATIM — recent_activity/personal/conversation_hooks/company_summary
    are exactly what the cards render. Nothing on disk → empty, never 404."""
    today = _local_today()
    for date, stale in ((today, False), (_date_shift(today, -1), True)):
        if not date:
            continue
        data = _read_json_file("cache", f"research_{date}.json", default=None)
        if not isinstance(data, dict):
            continue
        attendees = data.get("attendees")
        out = {"attendees": attendees if isinstance(attendees, list) else [],
               "date": date, "stale": stale}
        if isinstance(data.get("written_at"), str):
            out["written_at"] = data["written_at"]
        return out
    return {"attendees": [], "date": None, "stale": False}


def api_ledger(days_param="") -> dict:
    """GET /api/ledger?days=N (default 7, cap 30) → outcomes.jsonl (log_outcome.py: {ts, action_id,
    outcome, channel, contact, action_type, tier, …}) merged with dashboard_audit.jsonl ({ts,
    event, …}) and events/surfaced.jsonl (triage_event.py: {ts, sender, channel, verdict, reason,
    class} — one line per triage verdict, the "why didn't I get nudged?" trail), each row passed
    through verbatim plus source: "outcome"|"dashboard"|"triage". Newest first, LEDGER_MAX_ROWS cap.
    Each source is trimmed to its own share (LEDGER_MAX_PER_SOURCE) BEFORE the merge, so no single
    source can flood the Record. Malformed lines and rows without a parseable ts are skipped."""
    try:
        days = int(_s(str(days_param)) or LEDGER_DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = LEDGER_DEFAULT_DAYS
    days = max(1, min(days, LEDGER_MAX_DAYS))
    cutoff = time.time() - days * 86400
    entries = []
    for fname, source in (("outcomes.jsonl", "outcome"), ("dashboard_audit.jsonl", "dashboard"),
                          (os.path.join("events", "surfaced.jsonl"), "triage")):
        try:
            with open(os.path.join(_root(), fname), encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        rows = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue                       # malformed line — skip, never fail the timeline
            if not isinstance(rec, dict):
                continue
            ts = _from_iso(rec.get("ts"))
            if ts is None or ts < cutoff:
                continue
            rows.append({**rec, "source": source})
        # Each ledger is append-ordered, so the tail IS the newest rows of that source. A source
        # with no declared share falls back to the total cap — i.e. to the old behavior.
        entries.extend(rows[-LEDGER_MAX_PER_SOURCE.get(source, LEDGER_MAX_ROWS):])
    entries.sort(key=lambda r: _s(r.get("ts")), reverse=True)   # both writers emit ISO-Z — sortable
    return {"days": days, "entries": entries[:LEDGER_MAX_ROWS]}


# ── M2 write endpoints (session + CSRF gated; the dashboard is a window, not a second brain) ─────
# Fact and loop writes shell out to the skills tree's knowledge_edit.py so a dashboard edit rides
# the IDENTICAL code path a texted correction does (knowledge_update.apply / the ledger writer).
# Only /api/prefs edits a file directly — preferences have no chat-equivalent write path.

def _read_json_body(h):
    """The request's JSON object, or None when absent/oversized/not-a-dict."""
    try:
        n = int(h.headers.get("Content-Length", 0))
    except ValueError:
        return None
    if n <= 0 or n > API_BODY_MAX:
        return None
    try:
        v = json.loads(h.rfile.read(n).decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _clean_text(v, maxlen: int = TEXT_MAX):
    """Stripped non-empty string within the cap, else None (empty/whitespace ops rejected)."""
    if not isinstance(v, str):
        return None
    t = v.strip()
    return t if t and len(t) <= maxlen else None


def _run_knowledge_edit(args: list):
    """Run the skills tree's knowledge_edit.py (sys.executable, 30s budget) and parse its JSON.
    Returns None when the skills tree is absent (→ 503); otherwise the CLI's {"ok": ...} dict —
    a crashed/JSON-less run is folded into {"ok": False, "error": ...}."""
    try:
        script = HOOKS["find_script"]("_shared", "scripts", "knowledge_edit.py")
    except Exception:  # noqa: BLE001
        script = None
    if not script:
        return None
    env = {**os.environ, "SOTTO_DATA": _root()}
    try:
        r = subprocess.run([sys.executable, script, *args], capture_output=True, text=True,
                           timeout=EDIT_TIMEOUT_SECS, env=env)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "error": f"edit failed: {type(e).__name__}"}
    try:
        out = json.loads(r.stdout or "")
    except (json.JSONDecodeError, ValueError):
        out = None
    if not isinstance(out, dict):
        return {"ok": False, "error": (r.stderr or "knowledge_edit produced no JSON").strip()[:300]}
    if out.get("ok"):
        # The edit just rewrote a person file, so the serve-time people index (10-min TTL) now
        # holds the fact the user fixed. Expiring the stamp is the whole fix: the next read
        # rebuilds it. In one sentence: an edit the user just made is never served from a cache.
        _PEOPLE_IDX_CACHE["ts"] = 0.0
    return out


def _edit_failure(h, result: dict):
    """Map a CLI failure to a response: missing person/fact/loop → 404, everything else → 400.
    The error strings come from OUR CLI (not user data) and are safe to relay as JSON."""
    err = _s(result.get("error")) or "edit failed"
    return _json(h, 404 if "not found" in err else 400, {"error": err})


def _handle_api_post(h, path: str):
    rec = _session_record(h)
    if rec is None:
        return _json(h, 401, {"error": "unauthorized"})
    # CSRF: the per-session token (minted at login, fetched by app.js from /api/session) must ride
    # every write in the X-Sotto-CSRF header. Constant-time compare, same as the login code.
    want = _s(rec.get("csrf"))
    got = _s(h.headers.get("X-Sotto-CSRF") or "")
    if not want or not got or not hmac.compare_digest(got.encode(), want.encode()):
        return _json(h, 403, {"error": "bad csrf"})
    body = _read_json_body(h)
    if body is None:
        return _json(h, 400, {"error": "bad json body"})
    try:
        m = PERSON_FACTS_RE.match(path)
        if m:
            return _post_person_facts(h, m.group(1), body)
        if path == "/api/loops":
            return _post_loops(h, body)
        if path == "/api/prefs":
            return _post_prefs(h, body)
        return _json(h, 404, {"error": "not found"})
    except Exception as e:  # noqa: BLE001 — a shape surprise must not 500-loop the dashboard
        print(f"[sotto] dashboard write error on {path}: {e}", flush=True)
        return _json(h, 500, {"error": "write failed"})


def _post_person_facts(h, slug: str, body: dict):
    """POST /api/people/<slug>/facts {op: correct|archive|add, fact_id?, text?, memory_type?}.
    On success re-reads and returns the person in the GET /api/people/<slug> shape so the UI
    refreshes in place (the CLI reports the final slug — migration may re-key a legacy file)."""
    op = _s(body.get("op"))
    if op not in ("correct", "archive", "add"):
        return _json(h, 400, {"error": "bad op"})
    args = [f"--slug={slug}", f"--op={op}"]
    if op in ("correct", "archive"):
        fact_id = _s(body.get("fact_id"))
        if not FACT_ID_RE.match(fact_id):
            return _json(h, 400, {"error": "bad fact_id"})
        args.append(f"--fact-id={fact_id}")
    if op in ("correct", "add"):
        text = _clean_text(body.get("text"))
        if text is None:
            return _json(h, 400, {"error": "bad text (1-500 chars required)"})
        args.append(f"--text={text}")
    if op == "add":
        mt = _s(body.get("memory_type"))
        if mt:
            if not MEMORY_TYPE_RE.match(mt):
                return _json(h, 400, {"error": "bad memory_type"})
            args.append(f"--memory-type={mt}")
    result = _run_knowledge_edit(args)
    if result is None:
        return _json(h, 503, {"error": "skills tree unavailable"})
    if not result.get("ok"):
        return _edit_failure(h, result)
    _audit("write", endpoint=f"/api/people/{slug}/facts",
           target=_s(body.get("fact_id")) or slug, op=op)
    final = _s(result.get("slug")) or slug
    person = api_person(final) if SLUG_RE.match(final) else None
    if person is None:                       # readable person is the contract; degrade gracefully
        return _json(h, 200, {"ok": True, "slug": final})
    return _json(h, 200, person)


def _post_loops(h, body: dict):
    """POST /api/loops {anchor_key, op: resolve|dismiss} → {"ok": true}. Terminal transition via
    knowledge_edit.py, which writes the resolver's exact field semantics."""
    op = _s(body.get("op"))
    if op not in ("resolve", "dismiss"):
        return _json(h, 400, {"error": "bad op"})
    anchor = body.get("anchor_key")
    if not isinstance(anchor, str) or not ANCHOR_RE.match(anchor):
        return _json(h, 400, {"error": "bad anchor_key"})
    to = "resolved" if op == "resolve" else "dismissed"
    result = _run_knowledge_edit(["--op=loop", f"--anchor={anchor}", f"--to={to}"])
    if result is None:
        return _json(h, 503, {"error": "skills tree unavailable"})
    if not result.get("ok"):
        return _edit_failure(h, result)
    _audit("write", endpoint="/api/loops", target=anchor[:200], op=op)
    return _json(h, 200, {"ok": True})


def _write_prefs(prefs: dict) -> None:
    """Atomic 0600 write of preferences.json (the _write_sessions pattern — crash-safe, never
    world-readable)."""
    path = os.path.join(_root(), "preferences.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)
    os.replace(tmp, path)


def _post_prefs(h, body: dict):
    """POST /api/prefs {op: "delete", list, value} → the updated preferences object.
    Deletes one learned/stated rule by VALUE (rules carry no ids or enabled flags — see the
    PREF_* whitelists). Direct JSON edit: preferences have no chat-equivalent write path."""
    op = _s(body.get("op"))
    if op != "delete":
        return _json(h, 400, {"error": "bad op"})
    lst = _s(body.get("list"))
    if lst not in PREF_TOP_LISTS | PREF_EXPLICIT_LISTS | PREF_DICTS:
        return _json(h, 400, {"error": "bad list"})
    value = _clean_text(body.get("value"))
    if value is None:
        return _json(h, 400, {"error": "bad value (1-500 chars required)"})
    prefs = _read_json_file("preferences.json", default=None)
    if not isinstance(prefs, dict):
        prefs = {}
    removed = False
    if lst in PREF_DICTS:
        d = prefs.get(lst)
        if isinstance(d, dict) and value in d:
            d.pop(value)
            removed = True
    elif lst in PREF_TOP_LISTS:
        v = prefs.get(lst)
        if isinstance(v, list) and value in v:
            prefs[lst] = [x for x in v if x != value]
            removed = True
    else:  # explicit block lists (preferences.py's reserved user-stated block)
        ex = prefs.get("explicit")
        if isinstance(ex, dict) and isinstance(ex.get(lst), list) and value in ex[lst]:
            ex[lst] = [x for x in ex[lst] if x != value]
            removed = True
    if not removed:
        return _json(h, 404, {"error": "not found"})
    _write_prefs(prefs)
    _audit("write", endpoint="/api/prefs", target=f"{lst}:{value}"[:200], op="delete")
    return _json(h, 200, prefs)
