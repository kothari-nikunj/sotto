"""Tests for The Window M1+M2 (dashboard.py + its receiver wiring): session mint/expiry/lockout,
kill switch, CSP headers, static whitelist, the read-only JSON API against a fixture $SOTTO_DATA
tree, the secrets-never-rendered scan, and the M2 write endpoints (CSRF enforcement, fact/loop
writes through a stubbed knowledge_edit.py, preference edits, audit rail). Follows
test_receiver.py's harness pattern (fresh receiver module per HTTP test, ThreadingHTTPServer on
an ephemeral port, urllib driver)."""
import http.client
import importlib.util
import itertools
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(__file__)
_seq = itertools.count()

SETUP_CODE = "dash-sekrit-77"
PLANTED_SECRET = "SECRET-connector-token-XYZ123"   # planted in connectors/granola.json


def _load_receiver():
    spec = importlib.util.spec_from_file_location(f"receiver_dash{next(_seq)}",
                                                  os.path.join(HERE, "receiver.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── Fixture $SOTTO_DATA tree (shapes per contracts/exhaust-schema.md) ────────────────────────────

PERSON_MD = """---
schema: 1
canonical_id: c_a8f3e2
name: Sarah Chen
company: Acme Corp
title: CTO
identifiers: ["+15551234567", "sarah@acme.com"]
updated_at: 2026-06-23T07:00:00Z
updated_by: brief_extraction
relations:
- type: introduced_by
  slug: vishnu-sharma
  name: Vishnu Sharma
  date: 2026-05-14
  source: brief_extraction
  confidence: 0.95
- type: works_with
  slug: dana-reed
  name: Dana Reed
  source: user_edit
  confidence: 1.0
- type: nemesis_of
  slug: someone-else
  name: Someone Else
  source: hand
  confidence: 1.0
facts:
  f_aaa:
    text: "CTO at Acme Corp"
    type: milestone
    status: active
    seen: 3
    conf: 0.95
    source: brief_extraction
    source_ref: ""
    first: 2026-01-15
    last: 2026-02-18
  f_bbb:
    text: "Old superseded fact"
    type: context
    status: archived
    seen: 1
    conf: 0.99
    source: brief_extraction
    source_ref: ""
    first: 2026-01-01
    last: 2026-01-02
  f_ccc:
    text: "Runs the platform team"
    type: context
    status: active
    seen: 2
    conf: 0.6
    source: web_research
    source_ref: "https://example.com/profile"
    first: 2026-02-01
    last: 2026-03-01
---

## Summary
Sarah is the CTO.
"""

COMPANY_MD = """---
schema: 1
canonical_id: c_b1c2d3
name: Acme Corp
updated_at: 2026-06-20T07:00:00Z
facts:
  f_ddd:
    text: "Raised a Series B"
    type: milestone
    status: active
    seen: 1
    conf: 0.8
    source: web_research
    source_ref: "https://example.com/news"
    first: 2026-05-01
    last: 2026-05-01
---

## About
Acme makes anvils.

## News
Raised a Series B to make bigger anvils.
Opened a Lisbon office.
"""


def _loop_md(anchor, status, created, surfaced=2, meeting="", action_type="reply"):
    meet = f'meeting_time: "{meeting}"\n' if meeting else ""
    return (f"---\nanchor_key: \"{anchor}\"\naction_type: {action_type}\nchannel: email\n"
            f"contact_name: Sarah Chen\nstatus: {status}\ncreated_at: {created}\n"
            f"times_surfaced: {surfaced}\nsummary: \"Reply about the deck\"\n{meet}---\n")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "w" if isinstance(content, str) else "wb"
    with open(path, mode) as f:
        f.write(content)


def _fixtures(root):
    today = time.strftime("%Y-%m-%d")
    k = os.path.join(root, "knowledge")
    _write(os.path.join(k, "people", "sarah-chen.md"), PERSON_MD)
    _write(os.path.join(k, "companies", "acme-corp.md"), COMPANY_MD)
    _write(os.path.join(k, "continuity", "thread_abc.md"),
           _loop_md("thread:abc", "open", "2026-08-01", meeting="Tomorrow 3pm"))
    _write(os.path.join(k, "continuity", "thread_def.md"),
           _loop_md("email:reply:sarah", "waiting", "2026-08-04", surfaced=5))
    _write(os.path.join(k, "continuity", "thread_old.md"),
           _loop_md("thread:old", "resolved", "2026-07-01"))
    # meeting_prep / meeting_info entries are calendar shadows: even open ones (and old volume
    # files spelled with dashes) must never surface in /api/loops or the overview count.
    _write(os.path.join(k, "continuity", "prep_thu.md"),
           _loop_md("calendar:prep:thu", "open", "2026-08-05", action_type="meeting_prep"))
    _write(os.path.join(k, "continuity", "info_fri.md"),
           _loop_md("calendar:info:fri", "open", "2026-08-06", action_type="meeting-info"))
    _write(os.path.join(root, "briefs", f"{today}_morning.json"), json.dumps(
        {"brief_markdown": "## Morning\n- hello", "brief_text": "*Morning*\nhello chat",
         "actions": [{"channel": "email"}]}))
    _write(os.path.join(root, "briefs", "2026-08-01_evening.json"), json.dumps(
        {"brief_markdown": "old", "actions": []}))
    # non-archive siblings in briefs/ must never surface in listings
    _write(os.path.join(root, "briefs", f"{today}.morning.delivered"), "")
    _write(os.path.join(root, "briefs", f"{today}.morning_ready.payload.json"), "{}")
    # Realistic fingerprint shape (style_learner): the REGISTERS live under `canonical`; the
    # top-level keys are schema internals that must never surface as "buckets".
    _write(os.path.join(root, "style.json"), json.dumps(
        {"schema_version": 2, "updated_at": "2026-08-01T00:00:00Z", "master": {"tone": "warm"},
         "sample_keys": ["k1", "k2"],
         "canonical": {"work_email": [{"text": "hey hey"}],
                       "work_message": [{"text": "on it, will circle back"}],
                       "personal_message": [{"text": "running late, sorry!"}]},
         "per_person": {"sarah": {}, "bob": {}}}))
    _write(os.path.join(root, "preferences.json"), json.dumps(
        {"rules": [{"id": "p1", "rule": "deprioritize newsletters"}]}))
    # the planted secret: no API response body may ever contain it
    _write(os.path.join(root, "connectors", "granola.json"), json.dumps(
        {"access_token": PLANTED_SECRET, "refresh_token": "r-" + PLANTED_SECRET,
         "obtained_at": 1700000000}))
    return today


def _server(tmp_path, with_static=True):
    m = _load_receiver()
    root = str(tmp_path)
    m.DATA = root
    m.CONNECTORS.DATA = root
    m.SETTINGS_FILE = os.path.join(root, "config", "settings.json")
    m.SETUP_CODE = SETUP_CODE
    m.MCP_TOKEN = "bearer-tok"
    m.TOKEN = "bearer-tok"
    m.google_connected = lambda: (True, "ok")     # HOOKS late-bind this module global
    if with_static:
        sd = os.path.join(root, "static_fixture")
        _write(os.path.join(sd, "app.html"), "<!doctype html><title>Sotto</title>APP-SHELL")
        _write(os.path.join(sd, "app.js"), "// fixture js")
        _write(os.path.join(sd, "app.css"), "body{color:red}")
        m.DASHBOARD.STATIC_DIR = sd
    srv = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return m, srv, f"http://127.0.0.1:{srv.server_address[1]}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _get(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with _opener.open(req, timeout=10) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def _post(base, path, data, headers=None):
    h = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    req = urllib.request.Request(base + path, data=data, headers=h, method="POST")
    try:
        with _opener.open(req, timeout=10) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def _login_form(code):
    return urllib.parse.urlencode({"code": code}).encode()


def _session_cookie(hdrs):
    sc = hdrs.get("Set-Cookie", "")
    assert "sotto_session=" in sc, sc
    return "sotto_session=" + sc.split("sotto_session=", 1)[1].split(";", 1)[0]


def _login(base):
    code, _, hdrs = _post(base, "/app/login", _login_form(SETUP_CODE))
    assert code == 302 and hdrs.get("Location") == "/app"
    return {"Cookie": _session_cookie(hdrs)}


API_PATHS = ("/api/session", "/api/overview", "/api/loops", "/api/briefs",
             "/api/people", "/api/people/sarah-chen", "/api/learned",
             "/api/calendar", "/api/research", "/api/ledger")


# ── Login flow ───────────────────────────────────────────────────────────────────────────────────

def test_login_flow_mints_session_and_serves_app(tmp_path):
    m, srv, base = _server(tmp_path)
    try:
        # unauthenticated /app → the login page (form posts to /app/login, no inline JS)
        code, body, _ = _get(base, "/app")
        assert code == 200 and "action='/app/login'" in body and "<script" not in body
        # right code → 302 to the CLEAN /app + a session cookie with the full attribute set
        code, _, hdrs = _post(base, "/app/login", _login_form(SETUP_CODE))
        assert code == 302 and hdrs["Location"] == "/app"
        sc = hdrs["Set-Cookie"]
        assert "HttpOnly" in sc and "Secure" in sc and "SameSite=Lax" in sc and "Path=/" in sc
        token = sc.split("sotto_session=", 1)[1].split(";", 1)[0]
        # the session cookie now opens the app shell
        code, body, _ = _get(base, "/app", headers={"Cookie": f"sotto_session={token}"})
        assert code == 200 and "APP-SHELL" in body
        # store holds only the sha256 of the token, file is 0600
        sess_path = os.path.join(str(tmp_path), "dashboard_sessions.json")
        raw = open(sess_path).read()
        assert token not in raw
        rec = json.loads(raw)
        (digest, entry), = rec.items()
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
        assert entry["created"] and entry["last_seen"] and entry["csrf"]
        assert (os.stat(sess_path).st_mode & 0o777) == 0o600
        # audit trail recorded the success
        audit = open(os.path.join(str(tmp_path), "dashboard_audit.jsonl")).read()
        assert '"login_ok"' in audit
    finally:
        srv.shutdown()


def test_bad_code_counts_then_lockout_blocks_even_the_right_code(tmp_path):
    m, srv, base = _server(tmp_path)
    try:
        for _ in range(4):
            code, body, _ = _post(base, "/app/login", _login_form("wrong"))
            assert code == 403 and "class='err'" in body     # (apostrophes are HTML-escaped)
        code, _, _ = _post(base, "/app/login", _login_form("wrong"))   # 5th failure → lockout
        assert code == 429
        # even the CORRECT code is refused while locked (and ?code= auto-login too)
        code, _, _ = _post(base, "/app/login", _login_form(SETUP_CODE))
        assert code == 429
        code, body, hdrs = _get(base, f"/app?code={SETUP_CODE}")
        assert code == 200 and "sotto_session=" not in hdrs.get("Set-Cookie", "")
        audit = open(os.path.join(str(tmp_path), "dashboard_audit.jsonl")).read()
        assert audit.count('"login_fail"') == 5 and '"lockout"' in audit
        # lockout expires → login works again
        m.DASHBOARD._LOGIN_STATE["locked_until"] = time.time() - 1
        code, _, hdrs = _post(base, "/app/login", _login_form(SETUP_CODE))
        assert code == 302 and "sotto_session=" in hdrs.get("Set-Cookie", "")
    finally:
        srv.shutdown()


def test_setup_cookie_auto_login(tmp_path):
    """A user arriving from the wizard (valid sotto_setup cookie) never types the code again:
    /app mints a session and redirects clean."""
    m, srv, base = _server(tmp_path)
    try:
        code, _, hdrs = _get(base, "/app", headers={"Cookie": f"sotto_setup={SETUP_CODE}"})
        assert code == 302 and hdrs["Location"] == "/app"
        cookie = _session_cookie(hdrs)
        code, body, _ = _get(base, "/api/session", headers={"Cookie": cookie})
        assert code == 200 and json.loads(body)["csrf"]
        # a WRONG wizard cookie stays on the login page and does not count as a brute-force fail
        code, body, hdrs = _get(base, "/app", headers={"Cookie": "sotto_setup=stale"})
        assert code == 200 and "sotto_session=" not in hdrs.get("Set-Cookie", "")
        assert not os.path.exists(os.path.join(str(tmp_path), "dashboard_audit.jsonl")) or \
            '"login_fail"' not in open(os.path.join(str(tmp_path), "dashboard_audit.jsonl")).read()
    finally:
        srv.shutdown()


def test_code_param_auto_login_redirects_clean(tmp_path):
    m, srv, base = _server(tmp_path)
    try:
        code, _, hdrs = _get(base, f"/app?code={SETUP_CODE}")
        assert code == 302 and hdrs["Location"] == "/app"        # clean URL — the code is gone
        cookie = _session_cookie(hdrs)
        code, _, _ = _get(base, "/api/overview", headers={"Cookie": cookie})
        assert code == 200
        # an explicit WRONG ?code= counts as a brute-force attempt
        code, _, _ = _get(base, "/app?code=wrong")
        assert code == 200
        audit = open(os.path.join(str(tmp_path), "dashboard_audit.jsonl")).read()
        assert '"login_fail"' in audit
    finally:
        srv.shutdown()


# ── Session lifecycle ────────────────────────────────────────────────────────────────────────────

def test_session_idle_expiry_and_last_seen_bump(tmp_path):
    m, srv, base = _server(tmp_path)
    try:
        cookie = _login(base)
        sess_path = os.path.join(str(tmp_path), "dashboard_sessions.json")

        def _rewrite_last_seen(secs_ago):
            sess = json.loads(open(sess_path).read())
            for v in sess.values():
                v["last_seen"] = m.DASHBOARD._iso(time.time() - secs_ago)
            with open(sess_path, "w") as f:
                json.dump(sess, f)

        # fresh (< 1h): a request does NOT rewrite last_seen (write throttling)
        before = open(sess_path).read()
        assert _get(base, "/api/session", headers=cookie)[0] == 200
        assert open(sess_path).read() == before
        # 2h idle: still valid, and last_seen IS bumped
        _rewrite_last_seen(2 * 3600)
        stale = open(sess_path).read()
        assert _get(base, "/api/session", headers=cookie)[0] == 200
        assert open(sess_path).read() != stale
        # 31 days idle: expired → 401, and the record is dropped from the store
        _rewrite_last_seen(31 * 24 * 3600)
        code, body, _ = _get(base, "/api/session", headers=cookie)
        assert code == 401 and json.loads(body)["error"] == "unauthorized"
        assert json.loads(open(sess_path).read()) == {}
    finally:
        srv.shutdown()


def test_kill_switch_disables_everything(tmp_path, monkeypatch):
    m, srv, base = _server(tmp_path)
    try:
        cookie = _login(base)
        monkeypatch.setenv("SOTTO_DASHBOARD", "0")
        for path in ("/app", "/static/app.js", "/api/overview", "/api/session"):
            assert _get(base, path, headers=cookie)[0] == 404, path
        assert _post(base, "/app/login", _login_form(SETUP_CODE))[0] == 404
        monkeypatch.delenv("SOTTO_DASHBOARD")
        assert _get(base, "/app", headers=cookie)[0] == 200       # checked per-request, no restart
        # /health and the rest of the receiver are untouched by the switch
        monkeypatch.setenv("SOTTO_DASHBOARD", "0")
        assert _get(base, "/health")[0] == 200
    finally:
        srv.shutdown()


# ── Headers + static ─────────────────────────────────────────────────────────────────────────────

def test_security_headers_on_every_dashboard_surface(tmp_path):
    m, srv, base = _server(tmp_path)
    try:
        cookie = _login(base)
        strict = "default-src 'self'; script-src 'self'; style-src 'self'; " \
                 "img-src 'self' data:; frame-ancestors 'none'"
        for path, hdr in (("/app", cookie), ("/static/app.js", None), ("/static/app.css", None),
                          ("/api/session", cookie), ("/api/overview", cookie)):
            code, _, hdrs = _get(base, path, headers=hdr)
            assert hdrs.get("Content-Security-Policy") == strict, path
            assert hdrs.get("X-Content-Type-Options") == "nosniff", path
            assert hdrs.get("Referrer-Policy") == "no-referrer", path
        # /api/* additionally never caches
        for path in ("/api/session", "/api/loops"):
            _, _, hdrs = _get(base, path, headers=cookie)
            assert hdrs.get("Cache-Control") == "no-store", path
        # the login page keeps script-src 'self' (no JS) and is the ONE inline-style exception
        _, _, hdrs = _get(base, "/app")
        csp = hdrs.get("Content-Security-Policy", "")
        assert "script-src 'self'" in csp and "style-src 'unsafe-inline'" in csp
        # 401s carry the headers too
        _, _, hdrs = _get(base, "/api/overview")
        assert hdrs.get("Content-Security-Policy") == strict
    finally:
        srv.shutdown()


def test_static_whitelist_rejects_everything_else(tmp_path):
    m, srv, base = _server(tmp_path)
    try:
        code, body, hdrs = _get(base, "/static/app.js")
        assert code == 200 and body == "// fixture js"
        assert hdrs["Content-Type"].startswith("text/javascript")
        code, body, hdrs = _get(base, "/static/app.css")
        assert code == 200 and body == "body{color:red}"
        assert hdrs["Content-Type"].startswith("text/css")
        # not on the whitelist → 404 (even the shell html is not directly fetchable)
        for name in ("app.html", "evil.js", "..%2freceiver.py", "app.js.bak"):
            assert _get(base, f"/static/{name}")[0] == 404, name
        # raw traversal without client-side normalization (urllib normalizes ../, http.client won't)
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        conn.request("GET", "/static/../receiver.py")
        resp = conn.getresponse()
        raw = resp.read().decode()
        assert resp.status == 404 and "def " not in raw
        conn.close()
    finally:
        srv.shutdown()


def test_setup_css_is_whitelisted_and_served(tmp_path):
    """/setup links /static/setup.css (the Connections-surface layer over app.css), so it must be on
    the static whitelist with the css content type — and reachable without a session, like app.css."""
    m, srv, base = _server(tmp_path)
    assert m.DASHBOARD.STATIC_FILES.get("setup.css") == "text/css; charset=utf-8"
    _write(os.path.join(m.DASHBOARD.STATIC_DIR, "setup.css"), ".tile{display:block}")
    try:
        code, body, hdrs = _get(base, "/static/setup.css")
        assert code == 200 and body == ".tile{display:block}"
        assert hdrs["Content-Type"].startswith("text/css")
    finally:
        srv.shutdown()


def test_fonts_are_whitelisted_served_and_cached(tmp_path):
    """The vendored typefaces (Besley + Martian Mono) carry the whole design language, so their
    whitelist entries must serve 200 with the woff2 type AND the immutable Cache-Control header.
    Regression: the header used to be assigned dict-style onto the header LIST (a TypeError), so
    every font request crashed and the dashboard silently fell back to system fonts."""
    m, srv, base = _server(tmp_path)
    for name in ("fonts/Besley.woff2", "fonts/Besley-italic.woff2", "fonts/MartianMono.woff2"):
        assert m.DASHBOARD.STATIC_FILES.get(name) == "font/woff2", name
        _write(os.path.join(m.DASHBOARD.STATIC_DIR, name), b"WOF2fixture")
    try:
        for name in ("fonts/Besley.woff2", "fonts/Besley-italic.woff2", "fonts/MartianMono.woff2"):
            code, body, hdrs = _get(base, f"/static/{name}")
            assert code == 200 and body == "WOF2fixture", name
            assert hdrs["Content-Type"] == "font/woff2", name
            assert hdrs.get("Cache-Control") == "public, max-age=604800, immutable", name
        # no stale whitelist entries for the previous typefaces
        assert not any("Newsreader" in k or "JetBrains" in k for k in m.DASHBOARD.STATIC_FILES)
    finally:
        srv.shutdown()


def test_doc_playgrounds_serve_with_their_own_csp(tmp_path):
    """The two interactive playgrounds are single self-contained files — inline CSS + inline JS is
    what "works offline" MEANS — so they cannot ride the app CSP, which forbids both. They take
    _CSP_DOC instead, which is TIGHTER everywhere else: default-src 'none' forbids every network
    fetch (connect/fetch/XHR/websocket/font/frame) that the app policy's 'self' permits, and
    base-uri/form-action are pinned because they do not fall back to default-src. Regression guard:
    if someone gives them the app CSP, both pages render as unstyled dead HTML with a console full
    of violations — which looks like "the file is broken", not like a policy change."""
    m, srv, base = _server(tmp_path)
    names = ("playground-architecture.html", "playground-feedback-loops.html")
    try:
        for name in names:
            assert m.DASHBOARD.STATIC_FILES.get(name) == "text/html; charset=utf-8", name
            _write(os.path.join(m.DASHBOARD.STATIC_DIR, name), "<!doctype html><title>P</title>OK")
            code, body, hdrs = _get(base, f"/static/{name}")
            assert code == 200 and body.endswith("OK"), name
            assert hdrs["Content-Type"] == "text/html; charset=utf-8", name
            csp = hdrs["Content-Security-Policy"]
            assert csp == m.DASHBOARD._CSP_DOC, name
            assert "default-src 'none'" in csp and "script-src 'unsafe-inline'" in csp
            assert "style-src 'unsafe-inline'" in csp and "frame-ancestors 'none'" in csp
            assert "base-uri 'none'" in csp and "form-action 'none'" in csp
            assert "'self'" not in csp, "the doc policy must not re-open the network"
            assert hdrs.get("Cache-Control") == "no-cache", name
        # the app's own surfaces keep the strict app policy — the doc CSP is not contagious
        _, _, hdrs = _get(base, "/static/app.css")
        assert hdrs["Content-Security-Policy"] == m.DASHBOARD._CSP
    finally:
        srv.shutdown()


def test_missing_static_assets_serve_a_stub_not_an_error(tmp_path):
    m, srv, base = _server(tmp_path, with_static=False)
    m.DASHBOARD.STATIC_DIR = os.path.join(str(tmp_path), "no_such_dir")
    try:
        cookie = _login(base)
        code, body, _ = _get(base, "/app", headers=cookie)
        assert code == 200 and "dashboard assets missing" in body
        code, body, _ = _get(base, "/static/app.js")
        assert code == 200 and "dashboard assets missing" in body
    finally:
        srv.shutdown()


# ── Read-only API ────────────────────────────────────────────────────────────────────────────────

def test_api_requires_a_session_401_json(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    try:
        for path in API_PATHS:
            code, body, _ = _get(base, path)
            assert code == 401 and json.loads(body) == {"error": "unauthorized"}, path
        # the setup cookie and MCP bearer are NOT api credentials — only a minted session is
        for hdr in ({"Cookie": f"sotto_setup={SETUP_CODE}"},
                    {"Authorization": "Bearer bearer-tok"}):
            assert _get(base, "/api/overview", headers=hdr)[0] == 401
    finally:
        srv.shutdown()


def test_api_overview_shape(tmp_path):
    m, srv, base = _server(tmp_path)
    today = _fixtures(str(tmp_path))
    try:
        cookie = _login(base)
        code, body, _ = _get(base, "/api/overview", headers=cookie)
        assert code == 200
        ov = json.loads(body)
        assert ov["date"] == today
        assert ov["briefs_today"] == [{"kind": "morning", "file": f"{today}_morning.json"}]
        assert ov["loops_active"] == 2       # resolved + meeting_prep/meeting-info excluded
        assert ov["last_event_at"] is None                  # no event stamp yet
        assert ov["bridge_connected"] is False
        assert ov["services"] == {"google": True, "whatsapp": True, "granola": "ok"}
        # a gather-written error file downgrades granola to reconnect; no token file → absent
        _write(os.path.join(str(tmp_path), "connectors", "granola.error"), "401 upstream")
        ov = json.loads(_get(base, "/api/overview", headers=cookie)[1])
        assert ov["services"]["granola"] == "reconnect"
        os.remove(os.path.join(str(tmp_path), "connectors", "granola.error"))
        os.remove(os.path.join(str(tmp_path), "connectors", "granola.json"))
        ov = json.loads(_get(base, "/api/overview", headers=cookie)[1])
        assert ov["services"]["granola"] == "absent"
        # an accepted event surfaces as last_event_at
        m._touch_event_stamp()
        ov = json.loads(_get(base, "/api/overview", headers=cookie)[1])
        assert ov["last_event_at"] and ov["last_event_at"].endswith("Z")
        assert ov["update"] == {"available": False}     # dev build: nothing to say
    finally:
        srv.shutdown()


def test_api_overview_carries_the_update_banner_only_when_there_is_one(tmp_path):
    """The Today banner's whole input, normalized by the API: an update the daily check found and
    still stands behind, or {"available": False} — the page renders nothing on the latter."""
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    try:
        cookie = _login(base)

        def overview():
            return json.loads(_get(base, "/api/overview", headers=cookie)[1])["update"]

        # the real wiring: a stamped build + a fresh daily check that found something newer
        _write(os.path.join(str(tmp_path), "VERSION"), "2026-08-01.aaaaaaa")
        m.VERSION_FILE = os.path.join(str(tmp_path), "VERSION")
        m._fetch_latest_version = lambda: "2026-08-07.bbbbbbb"
        m.check_for_update()
        assert overview() == {"available": True, "version": "2026-08-07.bbbbbbb",
                              "current": "2026-08-01.aaaaaaa", "url": m.UPDATE_DOC_URL}

        # a check that stopped succeeding two days ago stops claiming anything
        cache = os.path.join(str(tmp_path), "cache", "update_check.json")
        c = json.load(open(cache))
        c["fetched_at"] = c["fetched_at"] - m.UPDATE_NOTICE_STALE_SECS - 1
        json.dump(c, open(cache, "w"))
        assert overview() == {"available": False}
        os.remove(cache)
        assert overview() == {"available": False}       # no state file → no banner

        # and nothing the hook can return gets rendered on trust: a raiser, a wrong shape, and a
        # link that isn't https all read as "no update"
        for bad in [lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                    lambda: "sure",
                    lambda: {"available": True, "version": "", "url": "https://x/y"},
                    lambda: {"available": True, "version": "9", "url": "javascript:alert(1)"}]:
            m.DASHBOARD.HOOKS["update_notice"] = bad
            assert overview() == {"available": False}
    finally:
        srv.shutdown()


def test_api_loops_active_only_newest_first(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    try:
        cookie = _login(base)
        loops = json.loads(_get(base, "/api/loops", headers=cookie)[1])["loops"]
        # the open meeting_prep/meeting-info fixtures (newest by created_at) are excluded —
        # loops are action items, not calendar shadows
        assert [l["anchor_key"] for l in loops] == ["email:reply:sarah", "thread:abc"]
        assert not any(l["anchor_key"].startswith("calendar:") for l in loops)
        top = loops[0]
        assert top == {"anchor_key": "email:reply:sarah", "action_type": "reply",
                       "channel": "email", "contact_name": "Sarah Chen", "status": "waiting",
                       "created_at": "2026-08-04", "times_surfaced": 5,
                       "summary": "Reply about the deck", "meeting_time": None,
                       "deadline": None, "source": None,
                       "chased_count": 0, "last_chased_at": None, "chase_after": None,
                       "chased_out": False}
        assert loops[1]["meeting_time"] == "Tomorrow 3pm"
    finally:
        srv.shutdown()


def test_api_loops_exposes_chase_state(tmp_path):
    """The dashboard is positioned as a complete alternative to chat, and chat can already say
    "chased once, Tuesday" — so this page must carry the same four fields. `chased_out` is the
    hand-off: chased its two times with no answer, so it's the user's call now."""
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    chase = ('---\nanchor_key: "imessage:waiting_on:id:5551112222"\naction_type: waiting_on\n'
             'channel: imessage\ncontact_name: Maya Iyer\nstatus: open\ncreated_at: 2026-08-05\n'
             'times_surfaced: 3\nsummary: "the signed contract"\nchased_count: 2\n'
             'last_chased_at: 2026-08-08\nchase_after: 2026-08-11\n---\n')
    _write(os.path.join(str(tmp_path), "knowledge", "continuity", "wait_maya.md"), chase)
    try:
        cookie = _login(base)
        loops = json.loads(_get(base, "/api/loops", headers=cookie)[1])["loops"]
        maya = next(l for l in loops if l["contact_name"] == "Maya Iyer")
        assert maya["chased_count"] == 2 and maya["last_chased_at"] == "2026-08-08"
        assert maya["chase_after"] == "2026-08-11" and maya["chased_out"] is True
        assert m.DASHBOARD.CHASE_MAX_DEFAULT == 2      # mirrors continuity_resolve.CHASE_MAX
    finally:
        srv.shutdown()


def test_sottos_own_held_nudges_are_never_offered_nudge_me_now(tmp_path):
    """The event skill has no branch for a proactive bundle — "nudge me now" on a queued birthday
    would ask it to draft a reply to a birthday. Mirrors triage_event's own refusal."""
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows = [json.dumps({"ts": ts, "verdict_class": "budget", "held_class": "birthday",
                        "sender": "Jordan's birthday is today",
                        "event": {"source": "proactive", "kind": "birthday",
                                  "key": "bday:jordan:2026", "timestamp": ts}}),
            json.dumps({"ts": ts, "verdict_class": "budget", "held_class": "urgent",
                        "sender": "Sarah Chen",
                        "event": {"source": "imessage", "rowid": 1, "handle": "+14155551234",
                                  "timestamp": ts}})]
    _write(os.path.join(str(tmp_path), "events", "queue.jsonl"), "\n".join(rows) + "\n")
    try:
        cookie = _login(base)
        waiting = json.loads(_get(base, "/api/cadence", headers=cookie)[1])["waiting"]
        by_sender = {w["sender"]: w for w in waiting}
        assert by_sender["Jordan's birthday is today"]["promotable"] is False
        assert by_sender["Sarah Chen"]["promotable"] is True
    finally:
        srv.shutdown()


def test_api_briefs_list_and_single(tmp_path):
    m, srv, base = _server(tmp_path)
    today = _fixtures(str(tmp_path))
    try:
        cookie = _login(base)
        briefs = json.loads(_get(base, "/api/briefs", headers=cookie)[1])["briefs"]
        assert briefs == [{"date": today, "kind": "morning"},
                          {"date": "2026-08-01", "kind": "evening"}]   # newest first, no .claim junk
        # single: stored JSON verbatim + chat_text from compose_brief's brief_text
        code, body, _ = _get(base, f"/api/briefs?date={today}&kind=morning", headers=cookie)
        one = json.loads(body)
        assert code == 200 and one["date"] == today and one["kind"] == "morning"
        assert one["data"]["brief_markdown"] == "## Morning\n- hello"
        assert one["chat_text"] == "*Morning*\nhello chat"
        # no brief_text/chat_text stored → chat_text is null, data still present
        one = json.loads(_get(base, "/api/briefs?date=2026-08-01&kind=evening", headers=cookie)[1])
        assert one["chat_text"] is None and one["data"]["brief_markdown"] == "old"
        # strict param validation — traversal shapes are 400, never a path probe
        for qs in ("date=..%2F..%2Fetc&kind=morning", "date=2026-08-01&kind=..%2Fx",
                   "date=2026-8-1&kind=morning", "date=2026-08-01&kind=MORNING"):
            assert _get(base, f"/api/briefs?{qs}", headers=cookie)[0] == 400, qs
        assert _get(base, "/api/briefs?date=1999-01-01&kind=morning", headers=cookie)[0] == 404
    finally:
        srv.shutdown()


def test_api_people_list_search_and_detail(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    try:
        cookie = _login(base)
        people = json.loads(_get(base, "/api/people", headers=cookie)[1])["people"]
        assert [p["slug"] for p in people] == ["acme-corp", "sarah-chen"]   # name-sorted
        sarah = people[1]
        assert sarah == {"slug": "sarah-chen", "type": "person", "name": "Sarah Chen",
                         "company": "Acme Corp", "title": "CTO", "fact_count": 2,  # active only
                         "updated_at": "2026-06-23T07:00:00Z"}
        assert people[0]["type"] == "company"
        # q filters name/company case-insensitively (matching the company finds its people too)
        hits = json.loads(_get(base, "/api/people?q=SARAH", headers=cookie)[1])["people"]
        assert [p["slug"] for p in hits] == ["sarah-chen"]
        hits = json.loads(_get(base, "/api/people?q=acme", headers=cookie)[1])["people"]
        assert {p["slug"] for p in hits} == {"acme-corp", "sarah-chen"}
        assert json.loads(_get(base, "/api/people?q=zzz", headers=cookie)[1])["people"] == []
        # detail: frontmatter + facts, active first then conf desc
        det = json.loads(_get(base, "/api/people/sarah-chen", headers=cookie)[1])
        assert det["name"] == "Sarah Chen" and det["type"] == "person"
        assert det["identifiers"] == ["+15551234567", "sarah@acme.com"]
        assert [f["id"] for f in det["facts"]] == ["f_aaa", "f_ccc", "f_bbb"]
        f0 = det["facts"][0]
        assert f0 == {"id": "f_aaa", "text": "CTO at Acme Corp", "type": "milestone",
                      "status": "active", "seen": 3, "conf": 0.95, "source": "brief_extraction",
                      "source_ref": "", "first": "2026-01-15", "last": "2026-02-18"}
        # companies resolve on the same endpoint
        det = json.loads(_get(base, "/api/people/acme-corp", headers=cookie)[1])
        assert det["type"] == "company" and det["facts"][0]["text"] == "Raised a Series B"
        # unknown or invalid slugs → 404, never a filesystem touch
        for slug in ("nobody", "..", "a/../b", "Sarah", "x%2e%2e"):
            assert _get(base, f"/api/people/{slug}", headers=cookie)[0] == 404, slug
    finally:
        srv.shutdown()


def test_api_company_detail_carries_sections_and_name(tmp_path):
    """Companies keep their substance in the markdown BODY (About/News/Context — see
    contracts/exhaust-schema.md), so the detail endpoint parses it into `sections` and always
    returns a display name. Person pages are unchanged (no sections key)."""
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    # no frontmatter name + headingless body → title-cased slug, one unnamed section
    _write(os.path.join(str(tmp_path), "knowledge", "companies", "beta-labs.md"),
           "---\nschema: 1\nupdated_at: 2026-07-01T00:00:00Z\n---\nA stealth lab.\nBuilds tools.\n")
    try:
        cookie = _login(base)
        det = json.loads(_get(base, "/api/people/acme-corp", headers=cookie)[1])
        assert det["type"] == "company" and det["name"] == "Acme Corp"
        assert det["sections"] == [
            {"heading": "About", "text": "Acme makes anvils."},
            {"heading": "News",
             "text": "Raised a Series B to make bigger anvils.\nOpened a Lisbon office."},
        ]
        det = json.loads(_get(base, "/api/people/beta-labs", headers=cookie)[1])
        assert det["name"] == "Beta Labs"
        assert det["sections"] == [{"heading": "", "text": "A stealth lab.\nBuilds tools."}]
        # the People list row carries the proper name too (never a bare slug)
        people = json.loads(_get(base, "/api/people", headers=cookie)[1])["people"]
        beta = next(p for p in people if p["slug"] == "beta-labs")
        assert beta["name"] == "Beta Labs" and beta["type"] == "company"
        # person pages unchanged — no sections key grows on them
        det = json.loads(_get(base, "/api/people/sarah-chen", headers=cookie)[1])
        assert det["type"] == "person" and "sections" not in det
    finally:
        srv.shutdown()


def test_api_learned_summarizes_style_never_dumps_it(tmp_path):
    """Buckets are the REAL registers — the keys of style.json's `canonical` block — never the
    top-level schema internals (canonical/master/sample_keys/…). Samples never leave either."""
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    try:
        cookie = _login(base)
        code, body, _ = _get(base, "/api/learned", headers=cookie)
        learned = json.loads(body)
        assert code == 200
        assert learned["style"] == {"buckets": ["personal_message", "work_email", "work_message"],
                                    "per_person": 2, "updated_at": "2026-08-01T00:00:00Z"}
        for schema_key in ("canonical", "master", "sample_keys", "schema_version"):
            assert schema_key not in learned["style"]["buckets"]
        assert "hey hey" not in body                          # sample text never leaves
        assert learned["preferences"] == {"rules": [{"id": "p1", "rule": "deprioritize newsletters"}]}
        # a fingerprint without a canonical block degrades to no buckets, never a key dump
        _write(os.path.join(str(tmp_path), "style.json"), json.dumps(
            {"schema_version": 2, "updated_at": "2026-08-01T00:00:00Z",
             "per_person": {"sarah": {}}}))
        learned = json.loads(_get(base, "/api/learned", headers=cookie)[1])
        assert learned["style"]["buckets"] == [] and learned["style"]["per_person"] == 1
    finally:
        srv.shutdown()


def test_api_empty_data_root_gives_empty_results_not_500(tmp_path):
    m, srv, base = _server(tmp_path)   # NO fixtures — bare volume
    try:
        cookie = _login(base)
        assert json.loads(_get(base, "/api/loops", headers=cookie)[1]) == {"loops": []}
        assert json.loads(_get(base, "/api/briefs", headers=cookie)[1]) == {"briefs": []}
        assert json.loads(_get(base, "/api/people", headers=cookie)[1]) == {"people": []}
        learned = json.loads(_get(base, "/api/learned", headers=cookie)[1])
        assert learned == {"style": {}, "preferences": {}}
        ov = json.loads(_get(base, "/api/overview", headers=cookie)[1])
        assert ov["briefs_today"] == [] and ov["loops_active"] == 0
        assert _get(base, "/api/nope", headers=cookie)[0] == 404
    finally:
        srv.shutdown()


def test_no_secrets_in_any_response_body(tmp_path):
    """Plant a token in connectors/granola.json; no dashboard response (authed or not) may carry
    it — nor the setup code outside the wizard, nor the MCP bearer."""
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    try:
        cookie = _login(base)
        pages = [_get(base, "/app")[1], _get(base, "/app", headers=cookie)[1],
                 _post(base, "/app/login", _login_form("wrong"))[1]]
        for path in API_PATHS + ("/api/people/acme-corp", "/api/briefs?date=2026-08-01&kind=evening"):
            pages.append(_get(base, path, headers=cookie)[1])
        for body in pages:
            assert PLANTED_SECRET not in body
            assert SETUP_CODE not in body
            assert "bearer-tok" not in body
    finally:
        srv.shutdown()


# ── M2 write endpoints ───────────────────────────────────────────────────────────────────────────
# The receiver locates knowledge_edit.py via _find_sotto_script (late-bound through
# HOOKS["find_script"]), so tests point it at a tiny fake CLI that records its argv and echoes
# valid JSON — receiver tests never depend on the real skills tree.

FAKE_CLI_OK = """\
import json, sys
d = {{}}
for a in sys.argv[1:]:
    k, _, v = a.partition("=")
    d[k.lstrip("-")] = v
with open({rec!r}, "a") as f:
    f.write(json.dumps(d) + "\\n")
if d.get("op") == "loop":
    print(json.dumps({{"ok": True, "anchor_key": d.get("anchor"), "status": d.get("to")}}))
else:
    print(json.dumps({{"ok": True, "slug": "sarah-chen", "fact_count": 2}}))
"""

FAKE_CLI_FAIL = """\
import json, sys
print(json.dumps({"ok": False, "error": "fact not found: f_x"}))
sys.exit(2)
"""

REAL_PREFS = {
    "deprioritization_hints": ["Bob|reply", "Newsletter|digest"],
    "approval_defaults": {"Sarah Chen|reply": "one_tap"},
    "edit_heavy": ["Bob|reply"],
    "analytics": {"total_outcomes": 10, "completion_rate": 0.5},
    "version": 1,
    "explicit": {"mute_senders": ["@news.acme.com"], "mute_people": ["Uncle Bob"],
                 "mute_sections": [], "tone_notes": ["keep it terse"],
                 "updated_at": "2026-08-01T00:00:00Z"},
}


def _stub_cli(m, tmp_path, source=None):
    """Install a fake knowledge_edit.py + argv recorder; returns the recorder path."""
    rec = os.path.join(str(tmp_path), "cli_calls.jsonl")
    fake = os.path.join(str(tmp_path), "fake_knowledge_edit.py")
    _write(fake, source if source is not None else FAKE_CLI_OK.format(rec=rec))
    m._find_sotto_script = lambda *rel: fake
    return rec


def _post_json(base, path, obj, headers=None):
    return _post(base, path, json.dumps(obj).encode(),
                 {"Content-Type": "application/json", **(headers or {})})


def _login_with_csrf(base):
    cookie = _login(base)
    csrf = json.loads(_get(base, "/api/session", headers=cookie)[1])["csrf"]
    return cookie, {**cookie, "X-Sotto-CSRF": csrf}


def _cli_calls(rec):
    with open(rec) as f:
        return [json.loads(l) for l in f if l.strip()]


def _audit_writes(root):
    path = os.path.join(str(root), "dashboard_audit.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip() and '"write"' in l]


WRITE_REQS = (("/api/people/sarah-chen/facts", {"op": "archive", "fact_id": "f_aaa"}),
              ("/api/loops", {"anchor_key": "thread:abc", "op": "resolve"}),
              ("/api/prefs", {"op": "delete", "list": "edit_heavy", "value": "Bob|reply"}))


def test_write_endpoints_enforce_session_and_csrf(tmp_path, monkeypatch):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    _write(os.path.join(str(tmp_path), "preferences.json"), json.dumps(REAL_PREFS))
    _stub_cli(m, tmp_path)
    try:
        # unauthenticated → 401 before anything else
        for path, body in WRITE_REQS:
            code, resp, _ = _post_json(base, path, body)
            assert code == 401 and json.loads(resp) == {"error": "unauthorized"}, path
        # session but no CSRF header → 403; wrong CSRF → 403
        cookie, authed = _login_with_csrf(base)
        for path, body in WRITE_REQS:
            code, resp, _ = _post_json(base, path, body, headers=cookie)
            assert code == 403 and json.loads(resp) == {"error": "bad csrf"}, path
            code, resp, _ = _post_json(base, path, body,
                                       headers={**cookie, "X-Sotto-CSRF": "nope"})
            assert code == 403, path
        # right CSRF → the write goes through
        for path, body in WRITE_REQS:
            code, _, _ = _post_json(base, path, body, headers=authed)
            assert code == 200, path
        # kill switch blankets the write surface too
        monkeypatch.setenv("SOTTO_DASHBOARD", "0")
        for path, body in WRITE_REQS:
            assert _post_json(base, path, body, headers=authed)[0] == 404, path
    finally:
        srv.shutdown()


def test_post_facts_runs_the_cli_and_returns_the_person(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    rec = _stub_cli(m, tmp_path)
    try:
        _, authed = _login_with_csrf(base)
        # correct: CLI gets slug/op/fact-id/text; response is the GET /api/people/<slug> shape
        code, body, _ = _post_json(base, "/api/people/sarah-chen/facts",
                                   {"op": "correct", "fact_id": "f_aaa",
                                    "text": "No longer CTO, now advising"}, headers=authed)
        person = json.loads(body)
        assert code == 200
        assert person["slug"] == "sarah-chen" and person["type"] == "person"
        assert [f["id"] for f in person["facts"]] == ["f_aaa", "f_ccc", "f_bbb"]
        # add: text + memory_type travel; archive: fact-id only
        assert _post_json(base, "/api/people/sarah-chen/facts",
                          {"op": "add", "text": "Loves sailing", "memory_type": "interest"},
                          headers=authed)[0] == 200
        assert _post_json(base, "/api/people/sarah-chen/facts",
                          {"op": "archive", "fact_id": "f_ccc"}, headers=authed)[0] == 200
        calls = _cli_calls(rec)
        assert calls[0] == {"slug": "sarah-chen", "op": "correct", "fact-id": "f_aaa",
                            "text": "No longer CTO, now advising"}
        assert calls[1] == {"slug": "sarah-chen", "op": "add", "text": "Loves sailing",
                            "memory-type": "interest"}
        assert calls[2] == {"slug": "sarah-chen", "op": "archive", "fact-id": "f_ccc"}
        # every write hit the audit rail with the plan's exact fields
        writes = _audit_writes(tmp_path)
        assert [w["op"] for w in writes] == ["correct", "add", "archive"]
        w = writes[0]
        assert w["event"] == "write" and w["endpoint"] == "/api/people/sarah-chen/facts"
        assert w["target"] == "f_aaa" and w["ts"].endswith("Z")
        assert writes[1]["target"] == "sarah-chen"        # add has no fact_id → slug
    finally:
        srv.shutdown()


def test_post_facts_validation_and_error_mapping(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    rec = _stub_cli(m, tmp_path)
    try:
        _, authed = _login_with_csrf(base)
        bad = [({"op": "delete"}, "bad op"),                          # not a fact op
               ({"op": "correct", "fact_id": "f_aaa"}, "bad text"),   # text missing
               ({"op": "correct", "fact_id": "f_aaa", "text": "   "}, "bad text"),
               ({"op": "correct", "fact_id": "f_aaa", "text": "x" * 501}, "bad text"),
               ({"op": "correct", "text": "fine"}, "bad fact_id"),    # fact_id missing
               ({"op": "archive", "fact_id": "f aaa!"}, "bad fact_id"),
               ({"op": "add", "text": "ok", "memory_type": "Not Valid"}, "bad memory_type")]
        for body, err in bad:
            code, resp, _ = _post_json(base, "/api/people/sarah-chen/facts", body, headers=authed)
            assert code == 400 and err in json.loads(resp)["error"], body
        assert _cli_calls(rec) == [] if os.path.exists(rec) else True  # nothing reached the CLI
        # non-JSON body → 400; unknown write path → 404
        assert _post(base, "/api/people/sarah-chen/facts", b"op=archive",
                     {"Content-Type": "application/json", **authed})[0] == 400
        assert _post_json(base, "/api/nope", {"op": "x"}, headers=authed)[0] == 404
        # CLI-reported "not found" → 404
        _stub_cli(m, tmp_path, source=FAKE_CLI_FAIL)
        code, resp, _ = _post_json(base, "/api/people/sarah-chen/facts",
                                   {"op": "archive", "fact_id": "f_zzz"}, headers=authed)
        assert code == 404 and "not found" in json.loads(resp)["error"]
        # skills tree absent → 503, and no audit entry for any failed write
        m._find_sotto_script = lambda *rel: None
        code, resp, _ = _post_json(base, "/api/people/sarah-chen/facts",
                                   {"op": "archive", "fact_id": "f_aaa"}, headers=authed)
        assert code == 503 and json.loads(resp) == {"error": "skills tree unavailable"}
        assert _audit_writes(tmp_path) == []
    finally:
        srv.shutdown()


def test_post_loops_resolve_and_dismiss(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    rec = _stub_cli(m, tmp_path)
    try:
        _, authed = _login_with_csrf(base)
        # anchors carry colons/spaces/@ — they travel in the body and reach the CLI verbatim
        anchor = "email:reply:name:sarah chen"
        code, body, _ = _post_json(base, "/api/loops", {"anchor_key": anchor, "op": "resolve"},
                                   headers=authed)
        assert code == 200 and json.loads(body) == {"ok": True}
        code, body, _ = _post_json(base, "/api/loops",
                                   {"anchor_key": "thread:abc", "op": "dismiss"}, headers=authed)
        assert code == 200 and json.loads(body) == {"ok": True}
        calls = _cli_calls(rec)
        assert calls[0] == {"op": "loop", "anchor": anchor, "to": "resolved"}
        assert calls[1] == {"op": "loop", "anchor": "thread:abc", "to": "dismissed"}
        writes = _audit_writes(tmp_path)
        assert writes[0] == {"ts": writes[0]["ts"], "event": "write", "endpoint": "/api/loops",
                             "target": anchor, "op": "resolve"}
        # validation: bad op / missing, control-char, or oversized anchor → 400, CLI untouched
        for body in ({"anchor_key": anchor, "op": "close"}, {"op": "resolve"},
                     {"anchor_key": "bad\nanchor", "op": "resolve"},
                     {"anchor_key": "x" * 257, "op": "resolve"}):
            assert _post_json(base, "/api/loops", body, headers=authed)[0] == 400, body
        assert len(_cli_calls(rec)) == 2
    finally:
        srv.shutdown()


def test_post_prefs_deletes_rules_across_the_real_shape(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    prefs_path = os.path.join(str(tmp_path), "preferences.json")
    _write(prefs_path, json.dumps(REAL_PREFS))
    try:
        _, authed = _login_with_csrf(base)
        # explicit list rule
        code, body, _ = _post_json(base, "/api/prefs",
                                   {"op": "delete", "list": "mute_senders",
                                    "value": "@news.acme.com"}, headers=authed)
        assert code == 200
        prefs = json.loads(body)
        assert prefs["explicit"]["mute_senders"] == []
        assert prefs["explicit"]["mute_people"] == ["Uncle Bob"]      # untouched sibling
        # learned top-level list rule
        prefs = json.loads(_post_json(base, "/api/prefs",
                                      {"op": "delete", "list": "deprioritization_hints",
                                       "value": "Bob|reply"}, headers=authed)[1])
        assert prefs["deprioritization_hints"] == ["Newsletter|digest"]
        # approval_defaults dict rule (deleted by key)
        prefs = json.loads(_post_json(base, "/api/prefs",
                                      {"op": "delete", "list": "approval_defaults",
                                       "value": "Sarah Chen|reply"}, headers=authed)[1])
        assert prefs["approval_defaults"] == {}
        # analytics/version never touched; the response IS the file (atomic 0600 write)
        assert prefs["analytics"] == REAL_PREFS["analytics"] and prefs["version"] == 1
        on_disk = json.load(open(prefs_path))
        assert on_disk == prefs
        assert (os.stat(prefs_path).st_mode & 0o777) == 0o600
        # validation: toggle unsupported (no enabled flag in the shape), whitelisted lists only,
        # empty value rejected, absent value → 404
        for body, want in (({"op": "toggle", "list": "edit_heavy", "value": "Bob|reply"}, 400),
                           ({"op": "delete", "list": "analytics", "value": "x"}, 400),
                           ({"op": "delete", "list": "edit_heavy", "value": "  "}, 400),
                           ({"op": "delete", "list": "edit_heavy", "value": "zzz"}, 404)):
            assert _post_json(base, "/api/prefs", body, headers=authed)[0] == want, body
        writes = _audit_writes(tmp_path)
        assert [w["target"] for w in writes] == ["mute_senders:@news.acme.com",
                                                "deprioritization_hints:Bob|reply",
                                                "approval_defaults:Sarah Chen|reply"]
        assert all(w["endpoint"] == "/api/prefs" and w["op"] == "delete" for w in writes)
    finally:
        srv.shutdown()


def test_post_prefs_tombstones_recomputed_rules_only(tmp_path):
    """A rule you delete stays deleted: deleting from a RECOMPUTED list (the learner rebuilds
    deprioritization_hints / edit_heavy / approval_defaults from outcomes.jsonl every morning) also
    records {list, value} under the top-level `suppressed`, which learn_preferences.py filters out.
    The `explicit` block needs no tombstone — the learner carries it forward verbatim."""
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    prefs_path = os.path.join(str(tmp_path), "preferences.json")
    _write(prefs_path, json.dumps(REAL_PREFS))
    try:
        _, authed = _login_with_csrf(base)
        prefs = json.loads(_post_json(base, "/api/prefs",
                                      {"op": "delete", "list": "mute_senders",
                                       "value": "@news.acme.com"}, headers=authed)[1])
        assert "suppressed" not in prefs                       # explicit → no tombstone needed
        prefs = json.loads(_post_json(base, "/api/prefs",
                                      {"op": "delete", "list": "deprioritization_hints",
                                       "value": "Bob|reply"}, headers=authed)[1])
        prefs = json.loads(_post_json(base, "/api/prefs",
                                      {"op": "delete", "list": "approval_defaults",
                                       "value": "Sarah Chen|reply"}, headers=authed)[1])
        assert prefs["suppressed"] == [
            {"list": "deprioritization_hints", "value": "Bob|reply"},
            {"list": "approval_defaults", "value": "Sarah Chen|reply"}]
        assert json.load(open(prefs_path))["suppressed"] == prefs["suppressed"]
        # top level, NOT inside `explicit`: preferences.py's _save() reshapes that block to its own
        # LISTS/SCALARS and would silently drop an extra key.
        assert "suppressed" not in prefs["explicit"]
    finally:
        srv.shutdown()


# ── Parser unit coverage (the tolerant stdlib frontmatter reader) ────────────────────────────────

def test_frontmatter_parser_tolerates_the_exhaust_shapes(tmp_path):
    m = _load_receiver()
    fm = m.DASHBOARD.parse_frontmatter
    meta, body = fm(PERSON_MD)
    assert meta["name"] == "Sarah Chen" and meta["schema"] == 1
    assert meta["identifiers"] == ["+15551234567", "sarah@acme.com"]
    assert meta["facts"]["f_aaa"]["conf"] == 0.95
    assert meta["facts"]["f_aaa"]["source_ref"] == ""
    assert meta["facts"]["f_bbb"]["status"] == "archived"
    assert meta["updated_at"] == "2026-06-23T07:00:00Z"       # time-of-day colon survives
    assert "## Summary" in body
    # degenerate inputs never raise
    assert fm("no frontmatter at all") == ({}, "no frontmatter at all")
    assert fm("---\nunclosed: yes") == ({}, "---\nunclosed: yes")
    assert fm("---\n---\nbody")[0] == {}
    meta, _ = fm('---\nsummary: "a: b, c: d"\nweird line\n---\n')
    assert meta["summary"] == "a: b, c: d"


# ── M3 live views: /api/calendar, /api/research, /api/ledger ─────────────────────────────────────
# Calendar shells out to the skills tree's gather_google.py via the same HOOKS["find_script"]
# discovery the M2 writes use, so tests point it at a fake CLI that echoes a canned cal JSON and
# counts its invocations (the 10-minute cache must keep a dashboard refresh from forking a gather
# every time).

FAKE_GATHER = """\
import argparse, json
ap = argparse.ArgumentParser()
ap.add_argument("--skip-gmail", action="store_true")
ap.add_argument("--cal-out")
ap.add_argument("--gmail-out")
a, extra = ap.parse_known_args()
assert a.skip_gmail and a.cal_out and not extra
with open({count!r}, "a") as f:
    f.write("x")
with open({events!r}) as f:
    payload = f.read()
with open(a.cal_out, "w") as f:
    f.write(payload)
with open(a.gmail_out, "w") as f:              # gather always writes both files
    f.write("[]")
"""

FAKE_GATHER_CRASH = "import sys\nsys.exit(1)\n"   # dies before writing any output file


def _stub_gather(m, tmp_path, events=None, source=None):
    """Install a fake gather_google.py behind find_script; returns the invocation-counter path."""
    count = os.path.join(str(tmp_path), "gather_calls.txt")
    ev_path = os.path.join(str(tmp_path), "canned_cal.json")
    _write(ev_path, json.dumps(events if events is not None else []))
    fake = os.path.join(str(tmp_path), "fake_gather_google.py")
    _write(fake, source if source is not None else FAKE_GATHER.format(count=count, events=ev_path))
    m._find_sotto_script = lambda *rel: fake if rel[-1] == "gather_google.py" else None
    return count


def _gather_calls(count):
    try:
        return len(open(count).read())
    except OSError:
        return 0


def _cal_dates(m):
    today = m.DASHBOARD._local_today()
    return today, m.DASHBOARD._date_shift(today, 1), m.DASHBOARD._date_shift(today, 2)


def _raw_cal_events(today, tomorrow, later):
    """Canned cal-out in gather_google.normalize_event's REAL shape (id/summary/start/end/location/
    description/meetingLink + raw Google attendees), deliberately unsorted + one out-of-window."""
    return [
        {"id": "e3", "summary": "Too Far Out", "start": f"{later}T09:00:00-07:00",
         "end": f"{later}T10:00:00-07:00", "location": "", "description": "", "meetingLink": "",
         "attendees": []},
        {"id": "e2", "summary": "Coffee with Bob", "start": f"{tomorrow}T15:00:00-07:00",
         "end": "", "location": "", "description": "", "meetingLink": "", "attendees": []},
        {"id": "e1", "summary": "Board sync", "start": f"{today}T09:00:00-07:00",
         "end": f"{today}T10:00:00-07:00", "location": "Zoom HQ",
         "description": "PRIVATE-agenda-notes", "meetingLink": "https://meet.google.com/abc",
         "attendees": [{"email": "sarah@acme.com", "displayName": "Sarah Chen",
                        "responseStatus": "accepted", "organizer": True},
                       "bob@x.com",
                       {"responseStatus": "needsAction"}]},   # no name/email → dropped
    ]


def test_api_calendar_normalizes_today_tomorrow_and_caches(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    today, tomorrow, later = _cal_dates(m)
    count = _stub_gather(m, tmp_path, _raw_cal_events(today, tomorrow, later))
    try:
        cookie = _login(base)
        code, body, hdrs = _get(base, "/api/calendar", headers=cookie)
        assert code == 200 and hdrs.get("Cache-Control") == "no-store"
        cal = json.loads(body)
        assert cal["cached"] is False and cal["generated_at"].endswith("Z")
        # normalized server-side: exactly the Today-view contract, sorted by start, +2d filtered.
        # sarah@acme.com is on file (fixture identifiers) → the people-index join adds
        # title/company/slug; bob@x.com is unknown → passes through untouched.
        assert cal["events"] == [
            {"summary": "Board sync", "start": f"{today}T09:00:00-07:00",
             "end": f"{today}T10:00:00-07:00",
             "attendees": [{"name": "Sarah Chen", "email": "sarah@acme.com",
                            "title": "CTO", "company": "Acme Corp", "slug": "sarah-chen"},
                           {"name": "", "email": "bob@x.com"}],
             "location": "Zoom HQ", "meeting_link": "https://meet.google.com/abc"},
            {"summary": "Coffee with Bob", "start": f"{tomorrow}T15:00:00-07:00",
             "end": "", "attendees": []},
        ]
        # raw-event fields never leave the server
        assert "description" not in body and "PRIVATE-agenda-notes" not in body
        assert "responseStatus" not in body and "organizer" not in body and "Too Far Out" not in body
        assert _gather_calls(count) == 1
        # within the TTL: served from cache, NO second fork
        cal2 = json.loads(_get(base, "/api/calendar", headers=cookie)[1])
        assert cal2["cached"] is True and cal2["events"] == cal["events"]
        assert cal2["generated_at"] == cal["generated_at"]
        assert _gather_calls(count) == 1
        # TTL expiry → a fresh gather
        m.CALCACHE._CAL_CACHE["ts"] = time.time() - m.CALCACHE.CALENDAR_TTL_SECS - 1
        cal3 = json.loads(_get(base, "/api/calendar", headers=cookie)[1])
        assert cal3["cached"] is False and _gather_calls(count) == 2
    finally:
        srv.shutdown()


def test_api_calendar_degrades_quietly(tmp_path):
    m, srv, base = _server(tmp_path)
    try:
        cookie = _login(base)
        # skills tree absent → 200 (not 503) with the unavailable flag, and nothing cached
        m._find_sotto_script = lambda *rel: None
        for _ in range(2):
            code, body, _ = _get(base, "/api/calendar", headers=cookie)
            assert code == 200 and json.loads(body) == {"events": [], "unavailable": True}
        # a crashing gather → empty events, and the failure IS cached (no re-fork per refresh)
        count = _stub_gather(m, tmp_path, source=FAKE_GATHER_CRASH)
        m._find_sotto_script = lambda *rel: os.path.join(str(tmp_path), "fake_gather_google.py")
        cal = json.loads(_get(base, "/api/calendar", headers=cookie)[1])
        assert cal["events"] == [] and cal["cached"] is False and "unavailable" not in cal
        cal2 = json.loads(_get(base, "/api/calendar", headers=cookie)[1])
        assert cal2["cached"] is True
    finally:
        srv.shutdown()


def test_api_calendar_joins_attendees_against_the_people_index(tmp_path):
    """The docket's knowledge-graph join: attendee emails are matched against knowledge/people
    frontmatter identifiers — known addresses gain title/company/slug (and a name when the
    calendar carried none); unknown attendees pass through byte-identical. The join runs at
    serve time, so cached calendar responses carry it too."""
    m, srv, base = _server(tmp_path)
    root = str(tmp_path)
    _fixtures(root)
    # a person known only by email — the calendar event has no displayName for him
    _write(os.path.join(root, "knowledge", "people", "bob-lee.md"),
           "---\nschema: 1\nname: Bob Lee\ntitle: Partner\ncompany: X Capital\n"
           'identifiers: ["+15550000000", "Bob@X.com"]\nupdated_at: 2026-07-01T00:00:00Z\n---\n')
    today, tomorrow, later = _cal_dates(m)
    _stub_gather(m, tmp_path, _raw_cal_events(today, tomorrow, later))
    try:
        cookie = _login(base)
        cal = json.loads(_get(base, "/api/calendar", headers=cookie)[1])
        board = cal["events"][0]
        assert board["attendees"] == [
            {"name": "Sarah Chen", "email": "sarah@acme.com",
             "title": "CTO", "company": "Acme Corp", "slug": "sarah-chen"},
            # identifier matching is case-insensitive; the graph supplies the missing name
            {"name": "Bob Lee", "email": "bob@x.com",
             "title": "Partner", "company": "X Capital", "slug": "bob-lee"},
        ]
        # the cached response carries the same join
        cal2 = json.loads(_get(base, "/api/calendar", headers=cookie)[1])
        assert cal2["cached"] is True
        assert cal2["events"][0]["attendees"] == board["attendees"]
    finally:
        srv.shutdown()


RESEARCH_ENTRY = {
    "email": "sarah@acme.com", "title": "CTO", "company": "Acme Corp",
    "relevance": ["Owns the platform decision"], "summary": "Sarah is the CTO.",
    "company_summary": "Acme makes anvils for enterprise coyotes.",
    "recent_activity": [{"when": "last week", "what": "Published a post on agent memory",
                         "source_url": "https://example.com/post"}],
    "personal": ["Ran the SF half marathon (https://example.com/strava)"],
    "conversation_hooks": ["Her agent-memory post last week ties straight into the roadmap."],
}


def test_api_research_today_yesterday_stale_and_absent(tmp_path):
    m, srv, base = _server(tmp_path)
    root = str(tmp_path)
    today = m.DASHBOARD._local_today()
    yesterday = m.DASHBOARD._date_shift(today, -1)
    try:
        cookie = _login(base)
        # nothing persisted yet → empty, 200
        assert json.loads(_get(base, "/api/research", headers=cookie)[1]) == \
            {"attendees": [], "date": None, "stale": False}
        # yesterday's cache only → served, flagged stale
        _write(os.path.join(root, "cache", f"research_{yesterday}.json"), json.dumps(
            {"attendees": [{"email": "old@x.com", "summary": "yesterday bio"}],
             "written_at": "2026-08-05T14:00:00Z"}))
        res = json.loads(_get(base, "/api/research", headers=cookie)[1])
        assert res == {"attendees": [{"email": "old@x.com", "summary": "yesterday bio"}],
                       "date": yesterday, "stale": True, "written_at": "2026-08-05T14:00:00Z"}
        # today's cache wins, entries verbatim (cards render these fields exactly)
        _write(os.path.join(root, "cache", f"research_{today}.json"), json.dumps(
            {"attendees": [RESEARCH_ENTRY], "written_at": "2026-08-06T07:00:00Z"}))
        res = json.loads(_get(base, "/api/research", headers=cookie)[1])
        assert res["date"] == today and res["stale"] is False
        assert res["attendees"] == [RESEARCH_ENTRY]
        # a corrupt today-file falls back to yesterday's (stale), never 500s
        _write(os.path.join(root, "cache", f"research_{today}.json"), "{not json")
        res = json.loads(_get(base, "/api/research", headers=cookie)[1])
        assert res["date"] == yesterday and res["stale"] is True
    finally:
        srv.shutdown()


def _iso_ago(m, secs):
    return m.DASHBOARD._iso(time.time() - secs)


def test_api_ledger_merges_newest_first_and_skips_malformed(tmp_path):
    m, srv, base = _server(tmp_path)
    root = str(tmp_path)
    try:
        # outcomes.jsonl in log_outcome.py's real shape + junk lines; audit pre-seeded with a write
        outcomes = [
            json.dumps({"ts": _iso_ago(m, 3 * 3600), "action_id": "a2", "outcome": "dismissed",
                        "channel": "email", "contact": "bob", "action_type": "reply",
                        "tier": "approval"}),
            json.dumps({"ts": _iso_ago(m, 3600), "action_id": "a1", "outcome": "edited_and_sent",
                        "channel": "imessage", "contact": "sarah", "action_type": "reply",
                        "tier": "one_tap", "edits": "trimmed the sign-off"}),
            json.dumps({"ts": _iso_ago(m, 10 * 86400), "outcome": "executed"}),   # beyond 7d
            "{not json at all",                                                   # malformed → skip
            "42",                                                                 # non-dict → skip
            json.dumps({"outcome": "viewed"}),                                    # no ts → skip
        ]
        _write(os.path.join(root, "outcomes.jsonl"), "\n".join(outcomes) + "\n")
        _write(os.path.join(root, "dashboard_audit.jsonl"), json.dumps(
            {"ts": _iso_ago(m, 2 * 3600), "event": "write", "endpoint": "/api/prefs",
             "target": "edit_heavy:Bob|reply", "op": "delete"}) + "\n")
        cookie = _login(base)   # appends a login_ok audit row at "now"
        code, body, _ = _get(base, "/api/ledger", headers=cookie)
        assert code == 200
        led = json.loads(body)
        assert led["days"] == 7
        assert [(e["source"], e.get("event") or e.get("outcome")) for e in led["entries"]] == [
            ("dashboard", "login_ok"),            # now
            ("outcome", "edited_and_sent"),       # -1h
            ("dashboard", "write"),               # -2h
            ("outcome", "dismissed"),             # -3h
        ]
        # fields pass through verbatim
        sent = led["entries"][1]
        assert sent["action_id"] == "a1" and sent["channel"] == "imessage"
        assert sent["contact"] == "sarah" and sent["edits"] == "trimmed the sign-off"
        assert led["entries"][2]["endpoint"] == "/api/prefs"
        # ?days is clamped to [1, 30]; junk falls back to the default
        assert json.loads(_get(base, "/api/ledger?days=90", headers=cookie)[1])["days"] == 30
        assert json.loads(_get(base, "/api/ledger?days=0", headers=cookie)[1])["days"] == 1
        assert json.loads(_get(base, "/api/ledger?days=abc", headers=cookie)[1])["days"] == 7
        # a 30-day window picks up the 10-day-old outcome
        led30 = json.loads(_get(base, "/api/ledger?days=30", headers=cookie)[1])
        assert any(e.get("outcome") == "executed" for e in led30["entries"])
        # row cap: 205 fresh outcome rows (oldest first, as appended) are trimmed to the outcome
        # source's own share, newest kept — see the per-source cap test below
        many = "\n".join(json.dumps({"ts": _iso_ago(m, i), "outcome": "viewed", "n": i})
                         for i in reversed(range(205)))
        _write(os.path.join(root, "outcomes.jsonl"), many + "\n")
        capped = json.loads(_get(base, "/api/ledger", headers=cookie)[1])["entries"]
        rows = [e for e in capped if e["source"] == "outcome"]
        assert len(rows) == m.DASHBOARD.LEDGER_MAX_PER_SOURCE["outcome"]
        assert rows[0]["n"] == 0 and rows[-1]["n"] == len(rows) - 1     # the newest, not the first
        assert len(capped) <= m.DASHBOARD.LEDGER_MAX_ROWS
    finally:
        srv.shutdown()


def test_m3_endpoints_kill_switch_and_auth(tmp_path, monkeypatch):
    m, srv, base = _server(tmp_path)
    m._find_sotto_script = lambda *rel: None      # calendar degrades; no forks in this test
    try:
        # unauthenticated → 401 (also covered by the API_PATHS sweep; pinned here for M3)
        for path in ("/api/calendar", "/api/research", "/api/ledger"):
            code, body, hdrs = _get(base, path)
            assert code == 401 and json.loads(body) == {"error": "unauthorized"}, path
            assert hdrs.get("Cache-Control") == "no-store", path
        cookie = _login(base)
        for path in ("/api/calendar", "/api/research", "/api/ledger"):
            code, _, hdrs = _get(base, path, headers=cookie)
            assert code == 200 and hdrs.get("Cache-Control") == "no-store", path
        # kill switch blankets the live views too
        monkeypatch.setenv("SOTTO_DASHBOARD", "0")
        for path in ("/api/calendar", "/api/research", "/api/ledger"):
            assert _get(base, path, headers=cookie)[0] == 404, path
    finally:
        srv.shutdown()


def test_api_ledger_merges_triage_surfaced_rows(tmp_path):
    """events/surfaced.jsonl (triage_event.py's verdict-time ledger) is the third source: rows pass
    through verbatim with source "triage", merged into the same newest-first timeline."""
    m, srv, base = _server(tmp_path)
    root = str(tmp_path)
    try:
        _write(os.path.join(root, "outcomes.jsonl"), json.dumps(
            {"ts": _iso_ago(m, 3600), "outcome": "draft_created", "channel": "imessage",
             "contact": "sarah", "action_type": "reply"}) + "\n")
        surfaced = [
            json.dumps({"ts": _iso_ago(m, 30 * 60), "sender": "Sarah Chen", "channel": "imessage",
                        "verdict": "agent", "reason": "direct ask about Thursday",
                        "class": "actionable"}),
            json.dumps({"ts": _iso_ago(m, 2 * 3600), "sender": "FLASH", "channel": "whatsapp",
                        "verdict": "queue", "reason": "group message without a name-mention",
                        "class": "group"}),
            json.dumps({"ts": _iso_ago(m, 20 * 60), "sender": "Ben", "channel": "imessage",
                        "verdict": "promoted", "reason": "held: cooldown, 124m old",
                        "class": "actionable"}),
            "{not json",                                                    # malformed → skip
        ]
        _write(os.path.join(root, "events", "surfaced.jsonl"), "\n".join(surfaced) + "\n")
        cookie = _login(base)   # appends a login_ok audit row at "now"
        led = json.loads(_get(base, "/api/ledger", headers=cookie)[1])
        assert [(e["source"], e.get("verdict") or e.get("outcome") or e.get("event"))
                for e in led["entries"]] == [
            ("dashboard", "login_ok"),            # now
            ("triage", "promoted"),               # -20m
            ("triage", "agent"),                  # -30m
            ("outcome", "draft_created"),         # -1h
            ("triage", "queue"),                  # -2h
        ]
        nudge = led["entries"][2]
        assert nudge["sender"] == "Sarah Chen" and nudge["channel"] == "imessage"
        assert nudge["reason"] == "direct ask about Thursday" and nudge["class"] == "actionable"
    finally:
        srv.shutdown()


def test_api_ledger_caps_each_source_before_the_merge(tmp_path):
    """No single source can flood the Record. The three streams grow at wildly different rates —
    triage verdicts are dozens an hour, outcomes a handful a day — so each is trimmed to its own
    share (newest kept) BEFORE the merge, instead of competing for one 200-row cap."""
    m, srv, base = _server(tmp_path)
    root = str(tmp_path)
    try:
        # 300 triage rows (newest last, as the ledger is appended) and 60 outcome rows
        _write(os.path.join(root, "events", "surfaced.jsonl"), "\n".join(
            json.dumps({"ts": _iso_ago(m, 4000 - i), "sender": f"S{i}", "channel": "imessage",
                        "verdict": "queue", "reason": "ambient chatter", "class": "ambient"})
            for i in range(300)) + "\n")
        _write(os.path.join(root, "outcomes.jsonl"), "\n".join(
            json.dumps({"ts": _iso_ago(m, 7300 - i), "action_id": f"a{i}",
                        "outcome": "edited_and_sent"}) for i in range(60)) + "\n")
        cookie = _login(base)   # appends a login_ok audit row at "now"
        led = json.loads(_get(base, "/api/ledger", headers=cookie)[1])
        counts = {}
        for e in led["entries"]:
            counts[e["source"]] = counts.get(e["source"], 0) + 1
        assert counts == {"dashboard": 1, "triage": 120, "outcome": 40}
        # the NEWEST rows of each source survive, not the first ones in the file
        senders = [e["sender"] for e in led["entries"] if e["source"] == "triage"]
        assert senders[0] == "S299" and senders[-1] == "S180"
        outs = [e["action_id"] for e in led["entries"] if e["source"] == "outcome"]
        assert outs[0] == "a59" and outs[-1] == "a20"
        # and the outcomes are still on the page at all — the flood is what this prevents
        assert led["entries"][-1]["source"] == "outcome"
        # the shares sum to the total cap, so LEDGER_MAX_ROWS is a backstop, never the trimmer
        assert sum(m.DASHBOARD.LEDGER_MAX_PER_SOURCE.values()) == m.DASHBOARD.LEDGER_MAX_ROWS
    finally:
        srv.shutdown()


def test_a_successful_write_busts_the_people_index(tmp_path):
    """The email → person index is cached for 10 minutes at serve time, so a dashboard correction
    used to leave the docket showing the fact the user had just fixed. Any successful
    knowledge_edit run expires the stamp; the next read rebuilds it."""
    m, srv, base = _server(tmp_path)
    root = str(tmp_path)
    _fixtures(root)
    _stub_cli(m, tmp_path)
    try:
        _, authed = _login_with_csrf(base)
        assert m.DASHBOARD._people_email_index()["sarah@acme.com"]["title"] == "CTO"   # warmed
        # the real CLI rewrites the person file; the stub only reports ok, so stand in for it
        _write(os.path.join(root, "knowledge", "people", "sarah-chen.md"),
               "---\nschema: 1\nname: Sarah Chen\ntitle: Advisor\ncompany: Acme Corp\n"
               'identifiers: ["sarah@acme.com"]\nupdated_at: 2026-08-07T00:00:00Z\n---\n')
        assert m.DASHBOARD._people_email_index()["sarah@acme.com"]["title"] == "CTO"   # still TTL'd
        assert _post_json(base, "/api/people/sarah-chen/facts",
                          {"op": "correct", "fact_id": "f_aaa", "text": "Advising now"},
                          headers=authed)[0] == 200
        assert m.DASHBOARD._people_email_index()["sarah@acme.com"]["title"] == "Advisor"
    finally:
        srv.shutdown()


# ── The ONE calendar cache: /api/calendar and the refresh file share a single fetch ───────────────
# ROADMAP Step 2 item 2 amendment ("two competing caches is how drift starts"). The endpoint tests
# above prove the view is unchanged; these prove there is only one gather behind it.

def _cal_file(tmp_path):
    return os.path.join(str(tmp_path), "cache", "calendar_today.json")


def test_calendar_refresh_file_rides_the_dashboard_s_fetch(tmp_path):
    """One gather serves both consumers: a dashboard hit warms the cache, and the refresh thread's
    work (run inline) writes calendar_today.json from THAT snapshot — no second fork inside the
    TTL, and the file carries the belief's generated_at, not the write's."""
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    today, tomorrow, later = _cal_dates(m)
    count = _stub_gather(m, tmp_path, _raw_cal_events(today, tomorrow, later))
    try:
        cookie = _login(base)
        cal = json.loads(_get(base, "/api/calendar", headers=cookie)[1])
        assert cal["cached"] is False and _gather_calls(count) == 1
        assert m.CALCACHE.refresh_once() is True
        assert _gather_calls(count) == 1                  # single flight: the SAME fetch
        data = json.load(open(_cal_file(tmp_path)))
        assert data["date"] == today
        assert data["refresh_secs"] == m.CALCACHE.REFRESH_SECS_DEFAULT
        assert data["generated_at"] == cal["generated_at"]
        # times + a count, never people; tomorrow's event is not "today"
        assert data["events"] == [{"summary": "Board sync",
                                   "start": f"{today}T09:00:00-07:00",
                                   "end": f"{today}T10:00:00-07:00",
                                   "attendees": 2, "all_day": False}]
        assert "sarah@acme.com" not in json.dumps(data)
        # and the reverse direction: a thread refresh warms the endpoint
        m.CALCACHE._CAL_CACHE.update({"ts": 0.0, "value": None})
        assert m.CALCACHE.refresh_once() is True
        assert _gather_calls(count) == 2
        cal2 = json.loads(_get(base, "/api/calendar", headers=cookie)[1])
        assert cal2["cached"] is True and _gather_calls(count) == 2
    finally:
        srv.shutdown()


def test_calendar_cache_is_single_flight_under_concurrency(tmp_path):
    """Eight simultaneous callers (the refresh thread + dashboard requests) must produce exactly
    ONE gather — the lock is held across the fork, so waiters get the fresh value instead of
    starting a competing 60s subprocess."""
    m = _load_receiver()
    m.DATA = str(tmp_path)
    today, tomorrow, later = _cal_dates(m)
    _stub_gather(m, tmp_path, _raw_cal_events(today, tomorrow, later))
    count = os.path.join(str(tmp_path), "gather_calls.txt")
    m.CALCACHE._CAL_CACHE.update({"ts": 0.0, "value": None})
    results, start = [], threading.Barrier(8)

    def go():
        start.wait()
        results.append(m.CALCACHE.snapshot())
    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert _gather_calls(count) == 1
    assert len(results) == 8 and all(r is not None for r in results)
    assert sum(1 for r in results if r["cached"] is False) == 1   # exactly one did the work
    assert len({r["generated_at"] for r in results}) == 1


def test_calendar_today_rows_count_other_humans_and_flag_all_day(tmp_path):
    """The docket's learned distinction, server-side: the address in every peopled event is the
    user (skipped), rooms are not people, a solo block counts 0, and a bare-date start is all-day.
    These four numbers are exactly what the in-meeting hold decides on."""
    m = _load_receiver()
    m.DATA = str(tmp_path)
    today = m.CALCACHE.HOOKS["local_today"]()
    me = {"name": "Me", "email": "me@x.com"}
    events = [
        {"summary": "1:1 with Sarah", "start": f"{today}T09:00:00-07:00",
         "end": f"{today}T09:30:00-07:00",
         "attendees": [me, {"name": "Sarah Chen", "email": "sarah@acme.com"}]},
        {"summary": "Standup", "start": f"{today}T10:00:00-07:00",
         "end": f"{today}T10:15:00-07:00",
         "attendees": [me, {"name": "Bob", "email": "bob@x.com"},
                       {"name": "Room 4", "email": "c_44@resource.calendar.google.com"}]},
        {"summary": "Focus block", "start": f"{today}T11:00:00-07:00",
         "end": f"{today}T12:00:00-07:00", "attendees": [me]},
        {"summary": "Company holiday", "start": today,
         "end": m.CALCACHE._date_shift(today, 1), "attendees": []},
        {"summary": "Tomorrow", "start": f"{m.CALCACHE._date_shift(today, 1)}T09:00:00-07:00",
         "end": "", "attendees": [me, {"name": "Ann", "email": "ann@x.com"}]},
    ]
    rows = m.CALCACHE.today_rows(events, today)
    assert [r["summary"] for r in rows] == ["1:1 with Sarah", "Standup", "Focus block",
                                            "Company holiday"]
    assert [r["attendees"] for r in rows] == [1, 1, 0, 0]
    assert [r["all_day"] for r in rows] == [False, False, False, True]


# ── Spec G: Cadence, the people graph, the Voice card, and "run it now" ──────────────────────────
# The read side is a VIEW over the funnel's own state files; the write side always lands in a
# skills-tree CLI or in the receiver's own dispatch. These tests own the dashboard's half of that
# contract — shape, validation, gating, audit — while the funnel's rules are tested in the skills
# suite (tests/test_event_triage.py::promote_one).

def _queue_line(sender="Sarah Chen", cls="cooldown", held="actionable", source="imessage",
                minutes_ago=10, rowid=1):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes_ago * 60))
    return json.dumps({"ts": ts, "verdict_class": cls, "sender": sender,
                       **({"held_class": held} if held else {}),
                       "event": {"source": source, "handle": "+1415555" + str(1000 + rowid),
                                 "text": "can we move Thursday?", "timestamp": ts,
                                 "rowid": rowid}})


def _cadence_fixtures(root, queue_lines=None):
    day = time.strftime("%Y-%m-%d")
    _write(os.path.join(root, "events", "budget.json"), json.dumps({"date": day, "count": 3}))
    _write(os.path.join(root, "cache", "meeting_taps.json"),
           json.dumps({"date": day, "fired": ["k1", "k2"]}))
    _write(os.path.join(root, "events", "valve_state.json"),
           json.dumps({"promotions": [time.time() - 120, time.time() - 7200]}))
    _write(os.path.join(root, "preferences.json"), json.dumps(
        {"explicit": {"mute_people": ["Uncle Bob"], "vip_people": ["Sarah Chen"],
                      "nudge_snooze_until": "2099-01-01T07:00"}}))
    lines = queue_lines if queue_lines is not None else [
        _queue_line(rowid=1), _queue_line(sender="Unknown", cls="group", held=None, rowid=2)]
    _write(os.path.join(root, "events", "queue.jsonl"), "\n".join(lines) + "\n")
    return day


def test_api_cadence_reads_the_funnel_s_own_state_files(tmp_path):
    m, srv, base = _server(tmp_path)
    day = _cadence_fixtures(str(tmp_path))
    try:
        cookie = _login(base)
        data = json.loads(_get(base, "/api/cadence", headers=cookie)[1])
        assert data["date"] == day
        assert data["budget"] == {"spent": 3, "cap": 4, "left": 1}
        assert data["taps"] == {"fired": 2, "cap": 3}
        assert data["valve"]["promoted"] == 1 and data["valve"]["cap"] == 2   # the hour's window
        assert data["snooze"] == {"until": "2099-01-01T07:00", "active": True}
        assert data["quiet"] == {"start": 21, "end": 7}
        assert data["vip_people"] == ["Sarah Chen"]
        assert data["delivery"]["channel"] == "whatsapp"
        # the waiting room: newest first, with the funnel's own class vocabulary
        assert [w["class"] for w in data["waiting"]] == ["group", "cooldown"]
        held = data["waiting"][1]
        assert held["sender"] == "Sarah Chen" and held["held_class"] == "actionable"
        assert held["channel"] == "imessage" and held["promotable"] is True
        assert isinstance(held["key"], str) and len(held["key"]) == 16
        # an unresolved sender is never offerable — the funnel would refuse it anyway
        assert data["waiting"][0]["promotable"] is False
    finally:
        srv.shutdown()


def test_api_cadence_stamp_from_another_day_reads_as_a_fresh_day(tmp_path):
    """budget.json and meeting_taps.json are date-keyed: yesterday's stamp IS the rollover."""
    m, srv, base = _server(tmp_path)
    _write(os.path.join(str(tmp_path), "events", "budget.json"),
           json.dumps({"date": "2000-01-01", "count": 9}))
    _write(os.path.join(str(tmp_path), "cache", "meeting_taps.json"),
           json.dumps({"date": "2000-01-01", "fired": ["a", "b", "c"]}))
    try:
        cookie = _login(base)
        data = json.loads(_get(base, "/api/cadence", headers=cookie)[1])
        assert data["budget"]["spent"] == 0 and data["taps"]["fired"] == 0
        assert data["waiting"] == [] and data["waiting_total"] == 0
        assert data["snooze"] == {"until": "", "active": False}
    finally:
        srv.shutdown()


def test_waiting_room_is_bounded_and_survives_junk(tmp_path):
    m, srv, base = _server(tmp_path)
    # queue.jsonl is append-ordered, so the LAST line is the newest event
    lines = [_queue_line(rowid=i, minutes_ago=61 - i) for i in range(1, 61)]
    lines.insert(5, "{not json")
    _cadence_fixtures(str(tmp_path), queue_lines=lines)
    try:
        cookie = _login(base)
        data = json.loads(_get(base, "/api/cadence", headers=cookie)[1])
        # tail 50 lines read, 15 rendered — a chatty day can't blow up the page
        assert len(data["waiting"]) == m.DASHBOARD.QUEUE_SHOWN == 15
        assert data["waiting_total"] <= m.DASHBOARD.QUEUE_TAIL_LINES
        ages = [w["age_min"] for w in data["waiting"]]
        assert ages == sorted(ages)                       # newest first
    finally:
        srv.shutdown()


def _stub_promote(m, result):
    calls = []
    m.DASHBOARD.HOOKS["promote_queued"] = lambda key: (calls.append(key), result)[1]
    return calls


def test_post_cadence_promote_validates_relays_and_audits(tmp_path):
    m, srv, base = _server(tmp_path)
    _cadence_fixtures(str(tmp_path))
    try:
        cookie, authed = _login_with_csrf(base)
        key = json.loads(_get(base, "/api/cadence", headers=cookie)[1])["waiting"][1]["key"]
        # shape validation happens before the funnel is ever asked
        for bad in ({"op": "promote"}, {"op": "promote", "key": "../../etc"},
                    {"op": "promote", "key": "ZZZZ"}, {"op": "nope"}):
            assert _post_json(base, "/api/cadence", bad, headers=authed)[0] == 400, bad
        # the funnel's refusals travel verbatim, with their own sentence
        _stub_promote(m, {"ok": False, "error": "meeting_hold",
                          "reason": "in a meeting until 2:30 PM"})
        code, body, _ = _post_json(base, "/api/cadence", {"op": "promote", "key": key},
                                   headers=authed)
        assert code == 409 and json.loads(body) == {"error": "meeting_hold",
                                                    "reason": "in a meeting until 2:30 PM"}
        _stub_promote(m, {"ok": False, "error": "not_found", "reason": "gone"})
        assert _post_json(base, "/api/cadence", {"op": "promote", "key": key},
                          headers=authed)[0] == 404
        assert _audit_writes(tmp_path) == []              # a refusal is not a write
        # success relays the reason and re-renders the panel from the server
        calls = _stub_promote(m, {"ok": True, "reason": "user promoted from dashboard"})
        code, body, _ = _post_json(base, "/api/cadence", {"op": "promote", "key": key},
                                   headers=authed)
        resp = json.loads(body)
        assert code == 200 and calls == [key] and resp["ok"] is True
        assert resp["reason"] == "user promoted from dashboard" and "budget" in resp
        (w,) = _audit_writes(tmp_path)
        assert w["endpoint"] == "/api/cadence" and w["op"] == "promote" and w["target"] == key
    finally:
        srv.shutdown()


def test_post_cadence_snooze_rides_the_preferences_cli(tmp_path):
    """The snooze is preferences.py's — the same CLI the feedback skill writes, so the clock math
    (and "a snooze lifts when quiet hours do") has exactly one implementation."""
    m, srv, base = _server(tmp_path)
    _write(os.path.join(str(tmp_path), "preferences.json"),
           json.dumps({"explicit": {"mute_people": ["Uncle Bob"]}}))
    try:
        cookie, authed = _login_with_csrf(base)
        code, body, _ = _post_json(base, "/api/cadence", {"op": "snooze", "spec": "2099-01-01"},
                                   headers=authed)
        assert code == 200
        assert json.loads(body)["snooze"] == {"until": "2099-01-01T00:00", "active": True}
        on_disk = json.load(open(os.path.join(str(tmp_path), "preferences.json")))
        assert on_disk["explicit"]["nudge_snooze_until"] == "2099-01-01T00:00"
        assert on_disk["explicit"]["mute_people"] == ["Uncle Bob"]     # nothing else disturbed
        # an unparseable spec is the CLI's refusal, relayed — never a guess
        assert _post_json(base, "/api/cadence", {"op": "snooze", "spec": "whenever"},
                          headers=authed)[0] == 400
        assert _post_json(base, "/api/cadence", {"op": "snooze", "spec": " "},
                          headers=authed)[0] == 400
        code, body, _ = _post_json(base, "/api/cadence", {"op": "unsnooze"}, headers=authed)
        assert code == 200 and json.loads(body)["snooze"]["active"] is False
        assert [w["op"] for w in _audit_writes(tmp_path)] == ["snooze", "unsnooze"]
    finally:
        srv.shutdown()


def test_api_graph_surfaces_merge_suggestions_and_the_attention_queue(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    _write(os.path.join(str(tmp_path), "knowledge", "merge_suggestions.json"), json.dumps(
        {"updated_at": "2026-08-06T07:00:00Z", "suggestions": [
            {"from": "sarah-c", "into": "sarah-chen", "from_name": "Sarah C",
             "into_name": "Sarah Chen", "reason": "name containment", "first_seen": "2026-08-01"},
            {"from": "../etc/passwd", "into": "sarah-chen", "reason": "hostile"},
            "not a dict"]}))
    _write(os.path.join(str(tmp_path), "knowledge", "relationship_state.json"), json.dumps(
        {"attention_queue": [
            {"display_name": "Sarah Chen", "queue_type": "losing_touch",
             "reason": "Communication declining", "graph_context": {"company": "Acme"}},
            {"display_name": "", "queue_type": "lapsed"}]}))
    try:
        cookie = _login(base)
        data = json.loads(_get(base, "/api/graph", headers=cookie)[1])
        # only pairs we could actually act on are offered — a traversal-shaped slug is dropped
        assert [(s["from"], s["into"]) for s in data["merge_suggestions"]] == \
            [("sarah-c", "sarah-chen")]
        assert data["merge_suggestions"][0]["reason"] == "name containment"
        (a,) = data["attention"]
        assert a["name"] == "Sarah Chen" and a["queue_type"] == "losing_touch"
        assert a["company"] == "Acme" and a["slug"] == "sarah-chen"   # joined to the dossier
        # nothing on file → empty, never a 404
        os.remove(os.path.join(str(tmp_path), "knowledge", "merge_suggestions.json"))
        assert json.loads(_get(base, "/api/graph", headers=cookie)[1])["merge_suggestions"] == []
    finally:
        srv.shutdown()


def test_post_graph_confirms_or_dismisses_through_the_merge_cli(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    rec = _stub_cli(m, tmp_path)
    try:
        _, authed = _login_with_csrf(base)
        assert _post_json(base, "/api/graph",
                          {"op": "merge", "from": "sarah-c", "into": "sarah-chen"},
                          headers=authed)[0] == 200
        assert _post_json(base, "/api/graph",
                          {"op": "dismiss", "from": "sarah-c", "into": "sarah-chen"},
                          headers=authed)[0] == 200
        assert [(c["op"], c["from"], c["into"]) for c in _cli_calls(rec)] == [
            ("merge", "sarah-c", "sarah-chen"), ("merge-dismiss", "sarah-c", "sarah-chen")]
        for bad in ({"op": "merge", "from": "../x", "into": "sarah-chen"},
                    {"op": "merge", "from": "sarah-c"},
                    {"op": "delete", "from": "sarah-c", "into": "sarah-chen"}):
            assert _post_json(base, "/api/graph", bad, headers=authed)[0] == 400, bad
        assert [(w["op"], w["target"]) for w in _audit_writes(tmp_path)] == [
            ("merge", "sarah-c->sarah-chen"), ("dismiss", "sarah-c->sarah-chen")]
    finally:
        srv.shutdown()


VOICE_STYLE = {
    "schema_version": 2, "updated_at": "2026-08-01T00:00:00Z",
    "master": {"capitalization": "lowercase", "uses_exclamation_marks": False,
               "uses_em_dashes": True, "greetings": ["hey", "hi"], "signoffs": ["thanks"]},
    "canonical": {
        "work_email": [{"text": "x" * 400, "bucket": "work_email", "channel": "email",
                        "recipient": "sarah@acme.com", "date": "2026-07-01", "quality": 0.9}],
        "work_message": [{"text": "on it, will circle back", "bucket": "work_message",
                          "channel": "imessage", "recipient": "+1415", "date": "2026-07-02",
                          "quality": 0.6}],
        "personal_message": [],
    },
    "recent": [], "per_person": {"sarah": {}},
    "confirmed": [{"text": "sounds good, shipping today", "bucket": "work_message",
                   "channel": "imessage", "recipient": "+1415", "date": "2026-06-01",
                   "source": "confirmed"}],
}


def test_api_voice_summarizes_the_fingerprint_and_caps_what_it_shows(tmp_path):
    m, srv, base = _server(tmp_path)
    _write(os.path.join(str(tmp_path), "style.json"), json.dumps(VOICE_STYLE))
    try:
        cookie = _login(base)
        data = json.loads(_get(base, "/api/voice", headers=cookie)[1])
        assert [(r["bucket"], r["samples"], r["confirmed"]) for r in data["registers"]] == [
            ("personal_message", 0, 0), ("work_email", 1, 0), ("work_message", 1, 1)]
        assert data["traits"] == ["starts messages lowercase", "rarely uses exclamation marks",
                                  "uses em dashes", "opens with hey, hi", "signs off with thanks"]
        # samples are verbatim sent messages — bounded in count and in length
        assert len(data["candidates"]) <= m.DASHBOARD.VOICE_CANDIDATES_MAX
        long_one = next(c for c in data["candidates"] if c["bucket"] == "work_email")
        assert len(long_one["text"]) == m.DASHBOARD.VOICE_TEXT_MAX and long_one["truncated"] is True
        assert all(len(c["key"]) == 16 for c in data["candidates"])
        assert [c["text"] for c in data["confirmed"]] == ["sounds good, shipping today"]
        # nothing internal leaks: no per_person pools, no sample_keys
        raw = _get(base, "/api/voice", headers=cookie)[1]
        assert "per_person" not in raw and "sample_keys" not in raw
    finally:
        srv.shutdown()


def test_post_voice_confirm_writes_through_style_extract(tmp_path):
    """End to end against the REAL CLI: the dashboard's hash and style_extract's must agree, or
    "confirm this one" would name a different sample than the user read."""
    m, srv, base = _server(tmp_path)
    _write(os.path.join(str(tmp_path), "style.json"), json.dumps(VOICE_STYLE))
    try:
        cookie, authed = _login_with_csrf(base)
        key = next(c["key"] for c in json.loads(_get(base, "/api/voice", headers=cookie)[1])
                   ["candidates"] if c["bucket"] == "work_message")
        code, body, _ = _post_json(base, "/api/voice", {"op": "confirm", "key": key},
                                   headers=authed)
        assert code == 200
        confirmed = json.load(open(os.path.join(str(tmp_path), "style.json")))["confirmed"]
        assert [c["text"] for c in confirmed] == ["sounds good, shipping today",
                                                  "on it, will circle back"]
        assert confirmed[-1]["source"] == "confirmed" and confirmed[-1]["quality"] >= 0.95
        # the candidate is gone from the offer list (it is confirmed now)
        after = json.loads(body)
        assert all(c["key"] != key for c in after["candidates"])
        for bad in ({"op": "confirm", "key": "nope"}, {"op": "delete", "key": "a" * 16}):
            assert _post_json(base, "/api/voice", bad, headers=authed)[0] == 400, bad
        assert _post_json(base, "/api/voice", {"op": "confirm", "key": "0" * 16},
                          headers=authed)[0] == 404
        (w,) = _audit_writes(tmp_path)
        assert w["endpoint"] == "/api/voice" and w["op"] == "confirm"
    finally:
        srv.shutdown()


def test_api_runs_gates_on_the_deliver_once_marker(tmp_path):
    m, srv, base = _server(tmp_path)
    day = time.strftime("%Y-%m-%d")
    try:
        cookie = _login(base)
        jobs = json.loads(_get(base, "/api/runs", headers=cookie)[1])["jobs"]
        assert [j["kind"] for j in jobs] == ["morning", "evening", "digest"]
        assert all(j["available"] for j in jobs)
        # brief_marker's own deliver-once flag closes the button — the same file the cron reads
        _write(os.path.join(str(tmp_path), "briefs", f"{day}.morning.delivered"), "")
        jobs = json.loads(_get(base, "/api/runs", headers=cookie)[1])["jobs"]
        morning = next(j for j in jobs if j["kind"] == "morning")
        assert morning["available"] is False and morning["reason"] == "delivered" and morning["at"]
        # a live claim without that flag means a run is already in flight
        _write(os.path.join(str(tmp_path), "briefs", f"{day}.evening.claim"), "")
        evening = next(j for j in json.loads(_get(base, "/api/runs", headers=cookie)[1])["jobs"]
                       if j["kind"] == "evening")
        assert evening["available"] is False and evening["reason"] == "running"
        # a job this deploy has gated off is never offered
        os.environ["SOTTO_DIGEST"] = "0"
        try:
            kinds = [j["kind"] for j in
                     json.loads(_get(base, "/api/runs", headers=cookie)[1])["jobs"]]
            assert "digest" not in kinds
        finally:
            os.environ.pop("SOTTO_DIGEST", None)
    finally:
        srv.shutdown()


def test_post_runs_fires_the_cron_prompt_and_refuses_what_it_reported_closed(tmp_path):
    m, srv, base = _server(tmp_path)
    day = time.strftime("%Y-%m-%d")
    spawned = []
    m.DASHBOARD.HOOKS["run_job"] = lambda name: (spawned.append(name),
                                                 {"ok": True, "skill": "sotto-evening-brief"})[1]
    try:
        _, authed = _login_with_csrf(base)
        code, body, _ = _post_json(base, "/api/runs", {"name": "sotto-evening-brief"},
                                   headers=authed)
        assert code == 200 and spawned == ["sotto-evening-brief"]
        assert json.loads(body)["ok"] is True and "jobs" in json.loads(body)
        assert _post_json(base, "/api/runs", {"name": "nope"}, headers=authed)[0] == 404
        _write(os.path.join(str(tmp_path), "briefs", f"{day}.morning.delivered"), "")
        code, body, _ = _post_json(base, "/api/runs", {"name": "sotto-morning-brief"},
                                   headers=authed)
        assert code == 409 and json.loads(body)["error"] == "delivered"
        assert spawned == ["sotto-evening-brief"]         # the closed job never spawned
        (w,) = _audit_writes(tmp_path)
        assert w["endpoint"] == "/api/runs" and w["target"] == "sotto-evening-brief"
    finally:
        srv.shutdown()


def test_post_loops_add_and_deadline_ride_knowledge_edit(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    rec = _stub_cli(m, tmp_path, source="""\
import json, sys
d = {}
for a in sys.argv[1:]:
    k, _, v = a.partition("=")
    d[k.lstrip("-")] = v
with open(%r, "a") as f:
    f.write(json.dumps(d) + "\\n")
print(json.dumps({"ok": True, "anchor_key": "thread:manual:abc", "created": True,
                  "deadline": d.get("deadline")}))
""" % os.path.join(str(tmp_path), "cli_calls.jsonl"))
    try:
        _, authed = _login_with_csrf(base)
        code, body, _ = _post_json(base, "/api/loops", {
            "op": "add", "text": "Send Sarah the deck", "contact": "Sarah Chen",
            "deadline": "2026-08-20"}, headers=authed)
        assert code == 200 and json.loads(body)["anchor_key"] == "thread:manual:abc"
        assert "loops" in json.loads(body)             # the view re-renders from the server
        code, _, _ = _post_json(base, "/api/loops", {
            "op": "deadline", "anchor_key": "email:reply:sarah", "deadline": "2026-09-01"},
            headers=authed)
        assert code == 200
        calls = _cli_calls(rec)
        assert calls[0] == {"op": "loop-add", "text": "Send Sarah the deck",
                            "contact": "Sarah Chen", "deadline": "2026-08-20"}
        assert calls[1] == {"op": "loop-deadline", "anchor": "email:reply:sarah",
                            "deadline": "2026-09-01"}
        # clearing is an empty deadline, not a missing one
        _post_json(base, "/api/loops", {"op": "deadline", "anchor_key": "email:reply:sarah",
                                        "deadline": ""}, headers=authed)
        assert _cli_calls(rec)[2]["deadline"] == ""
        for bad in ({"op": "add"}, {"op": "add", "text": "  "},
                    {"op": "add", "text": "x", "deadline": "next tuesday"},
                    {"op": "deadline", "anchor_key": "a", "deadline": "soon"},
                    {"op": "deadline", "deadline": "2026-09-01"},
                    {"op": "reopen", "anchor_key": "a"}):
            assert _post_json(base, "/api/loops", bad, headers=authed)[0] == 400, bad
        assert [w["op"] for w in _audit_writes(tmp_path)] == ["add", "deadline", "deadline"]
    finally:
        srv.shutdown()


def test_post_prefs_add_rides_the_preferences_cli_and_only_for_stated_lists(tmp_path):
    """The explicit block HAS a chat verb — preferences.py — so the dashboard uses it. The learned
    lists have none, which is exactly why they stay delete-only."""
    m, srv, base = _server(tmp_path)
    try:
        _, authed = _login_with_csrf(base)
        code, body, _ = _post_json(base, "/api/prefs",
                                   {"op": "add", "list": "vip_people", "value": "Sarah Chen"},
                                   headers=authed)
        assert code == 200 and json.loads(body)["explicit"]["vip_people"] == ["Sarah Chen"]
        assert _post_json(base, "/api/prefs",
                          {"op": "add", "list": "mute_senders", "value": "News@Acme.com"},
                          headers=authed)[0] == 200
        on_disk = json.load(open(os.path.join(str(tmp_path), "preferences.json")))
        assert on_disk["explicit"]["mute_senders"] == ["news@acme.com"]   # the CLI's own norming
        # the learned lists (and freetext tone notes) are not addable from here
        for bad in ({"op": "add", "list": "edit_heavy", "value": "Bob|reply"},
                    {"op": "add", "list": "tone_notes", "value": "keep it terse"},
                    {"op": "add", "list": "analytics", "value": "x"},
                    {"op": "add", "list": "vip_people", "value": "  "}):
            assert _post_json(base, "/api/prefs", bad, headers=authed)[0] == 400, bad
        # …and removing one goes back through the same CLI
        assert _post_json(base, "/api/prefs",
                          {"op": "delete", "list": "vip_people", "value": "Sarah Chen"},
                          headers=authed)[0] == 200
        after = json.load(open(os.path.join(str(tmp_path), "preferences.json")))
        assert after["explicit"]["vip_people"] == []
        assert [w["op"] for w in _audit_writes(tmp_path)] == ["add", "add", "delete"]
    finally:
        srv.shutdown()


def test_person_carries_the_two_switches_the_funnel_reads(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    _write(os.path.join(str(tmp_path), "preferences.json"), json.dumps(
        {"explicit": {"mute_people": ["sarah chen"], "vip_people": ["Someone Else"]}}))
    try:
        cookie = _login(base)
        person = json.loads(_get(base, "/api/people/sarah-chen", headers=cookie)[1])
        assert person["controls"] == {"name": "Sarah Chen", "muted": True, "vip": False}
        # companies carry no switches — there is nothing to mute or VIP about a company
        company = json.loads(_get(base, "/api/people/acme-corp", headers=cookie)[1])
        assert "controls" not in company
    finally:
        srv.shutdown()


def test_new_endpoints_are_session_gated_csrf_gated_and_killable(tmp_path, monkeypatch):
    m, srv, base = _server(tmp_path)
    _cadence_fixtures(str(tmp_path))
    reqs = (("/api/cadence", {"op": "unsnooze"}),
            ("/api/graph", {"op": "dismiss", "from": "a", "into": "b"}),
            ("/api/voice", {"op": "confirm", "key": "a" * 16}),
            ("/api/runs", {"name": "sotto-evening-brief"}))
    reads = ("/api/cadence", "/api/graph", "/api/voice", "/api/runs")
    try:
        for path in reads:
            assert _get(base, path)[0] == 401, path
        for path, body in reqs:
            assert _post_json(base, path, body)[0] == 401, path
        cookie, authed = _login_with_csrf(base)
        for path, body in reqs:
            assert _post_json(base, path, body, headers=cookie)[0] == 403, path
            assert _post_json(base, path, body,
                              headers={**cookie, "X-Sotto-CSRF": "nope"})[0] == 403, path
        monkeypatch.setenv("SOTTO_DASHBOARD", "0")
        for path in reads:
            assert _get(base, path, headers=cookie)[0] == 404, path
        for path, body in reqs:
            assert _post_json(base, path, body, headers=authed)[0] == 404, path
    finally:
        srv.shutdown()


def test_the_new_reads_leak_no_secrets(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    _cadence_fixtures(str(tmp_path))
    _write(os.path.join(str(tmp_path), "style.json"), json.dumps(VOICE_STYLE))
    try:
        cookie = _login(base)
        for path in ("/api/cadence", "/api/graph", "/api/voice", "/api/runs"):
            body = _get(base, path, headers=cookie)[1]
            assert PLANTED_SECRET not in body and SETUP_CODE not in body, path
    finally:
        srv.shutdown()


def test_the_shipped_frontend_keeps_its_two_rules(tmp_path):
    """Two invariants the whole design rests on, checked against the REAL assets rather than a
    fixture: nothing renders user data as markup (textContent only — the plan's rule 4), and every
    nav destination has a route behind it."""
    static = os.path.join(HERE, "static")
    js = open(os.path.join(static, "app.js"), encoding="utf-8").read()
    for banned in (".innerHTML", ".outerHTML", ".insertAdjacentHTML(", "document.write("):
        assert banned not in js, banned
    html = open(os.path.join(static, "app.html"), encoding="utf-8").read()
    navs = re.findall(r'data-nav="([a-z]+)"', html)
    assert "cadence" in navs
    routes = re.search(r"var routes = \{(.*?)\n  \};", js, re.S).group(1)
    for name in navs:
        assert re.search(rf"\b{name}: function", routes), name


# ── Relations: the person page's links, and the ✕ that removes one ───────────────────────────────

def test_api_person_carries_relations_as_linkable_sentences(tmp_path):
    """Each relation arrives ready to render: a `sentence` to read and a `slug` to link to. An edge
    with a type outside the closed vocabulary is dropped here exactly as the writer drops it."""
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    try:
        cookie = _login(base)
        det = json.loads(_get(base, "/api/people/sarah-chen", headers=cookie)[1])
        assert det["relations"] == [
            {"type": "introduced_by", "slug": "vishnu-sharma", "name": "Vishnu Sharma",
             "sentence": "Introduced to you by Vishnu Sharma (May 2026)"},
            {"type": "works_with", "slug": "dana-reed", "name": "Dana Reed",
             "sentence": "Works with Dana Reed"},
        ]
        # every slug is a person-page route the index already uses
        for rel in det["relations"]:
            assert m.DASHBOARD.SLUG_RE.match(rel["slug"])
            assert rel["name"] in rel["sentence"]      # the client links the name inside it
        # the list endpoint is untouched — relations are a detail-page concern
        people = json.loads(_get(base, "/api/people", headers=cookie)[1])["people"]
        assert all("relations" not in p for p in people)
        # companies have no relations key at all
        det = json.loads(_get(base, "/api/people/acme-corp", headers=cookie)[1])
        assert "relations" not in det
    finally:
        srv.shutdown()


def test_post_relations_removes_through_the_cli_and_audits(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    rec = _stub_cli(m, tmp_path)
    try:
        _, authed = _login_with_csrf(base)
        code, body, _ = _post_json(base, "/api/people/sarah-chen/relations",
                                   {"op": "remove", "type": "introduced_by",
                                    "other_slug": "vishnu-sharma"}, headers=authed)
        assert code == 200 and json.loads(body)["slug"] == "sarah-chen"
        assert _cli_calls(rec)[-1] == {"slug": "sarah-chen", "op": "relation-remove",
                                       "other-slug": "vishnu-sharma", "type": "introduced_by"}
        # type is optional — "whatever edge these two have"
        assert _post_json(base, "/api/people/sarah-chen/relations",
                          {"op": "remove", "other_slug": "dana-reed"}, headers=authed)[0] == 200
        assert "type" not in _cli_calls(rec)[-1]
        w = _audit_writes(tmp_path)[0]
        assert w["endpoint"] == "/api/people/sarah-chen/relations"
        assert w["op"] == "relation-remove" and w["target"] == "sarah-chen->vishnu-sharma"
    finally:
        srv.shutdown()


def test_post_relations_validation_and_auth(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    rec = _stub_cli(m, tmp_path)
    path = "/api/people/sarah-chen/relations"
    try:
        # the write surface's shared gates apply here too
        assert _post_json(base, path, {"op": "remove", "other_slug": "x"})[0] == 401
        cookie, authed = _login_with_csrf(base)
        assert _post_json(base, path, {"op": "remove", "other_slug": "x"}, headers=cookie)[0] == 403
        bad = [({"op": "add", "other_slug": "vishnu-sharma"}, "bad op"),
               ({"op": "remove"}, "bad other_slug"),
               ({"op": "remove", "other_slug": "../etc"}, "bad other_slug"),
               ({"op": "remove", "other_slug": "vishnu-sharma", "type": "nemesis_of"},
                "bad relation type")]
        for body, err in bad:
            code, resp, _ = _post_json(base, path, body, headers=authed)
            assert code == 400 and err in json.loads(resp)["error"], body
        assert not os.path.exists(rec)                   # nothing reached the CLI
        assert _audit_writes(tmp_path) == []
    finally:
        srv.shutdown()


def test_frontmatter_parser_reads_block_sequences(tmp_path):
    """`relations:` is a list of MAPS — the shape a key/value-only parser turns into garbage keys.
    Both indentations PyYAML dumpers emit parse, and a scalar list still parses."""
    m = _load_receiver()
    fm = m.DASHBOARD.parse_frontmatter
    meta, _ = fm(PERSON_MD)
    assert [r["type"] for r in meta["relations"]] == ["introduced_by", "works_with", "nemesis_of"]
    assert meta["relations"][0] == {"type": "introduced_by", "slug": "vishnu-sharma",
                                    "name": "Vishnu Sharma", "date": "2026-05-14",
                                    "source": "brief_extraction", "confidence": 0.95}
    assert meta["facts"]["f_aaa"]["conf"] == 0.95        # the map after the sequence still parses
    # indented sequence (the other common dump style) + a bare scalar list
    meta, _ = fm("---\nrelations:\n  - type: works_with\n    slug: b\n"
                 "tags:\n- one\n- two\nname: X\n---\n")
    assert meta["relations"] == [{"type": "works_with", "slug": "b"}]
    assert meta["tags"] == ["one", "two"] and meta["name"] == "X"
    # a dash with no key it can belong to is skipped, not crashed on
    assert fm("---\n- orphan: 1\nname: X\n---\n")[0] == {"name": "X"}
