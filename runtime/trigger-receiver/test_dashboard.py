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
"""


def _loop_md(anchor, status, created, surfaced=2, meeting=""):
    meet = f'meeting_time: "{meeting}"\n' if meeting else ""
    return (f"---\nanchor_key: \"{anchor}\"\naction_type: reply\nchannel: email\n"
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
    _write(os.path.join(root, "briefs", f"{today}_morning.json"), json.dumps(
        {"brief_markdown": "## Morning\n- hello", "brief_text": "*Morning*\nhello chat",
         "actions": [{"channel": "email"}]}))
    _write(os.path.join(root, "briefs", "2026-08-01_evening.json"), json.dumps(
        {"brief_markdown": "old", "actions": []}))
    # non-archive siblings in briefs/ must never surface in listings
    _write(os.path.join(root, "briefs", f"{today}.morning.delivered"), "")
    _write(os.path.join(root, "briefs", f"{today}.morning_ready.payload.json"), "{}")
    _write(os.path.join(root, "style.json"), json.dumps(
        {"schema_version": 2, "updated_at": "2026-08-01T00:00:00Z", "master": {"tone": "warm"},
         "canonical": {"greeting": [{"text": "hey hey"}]},
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
        assert ov["loops_active"] == 2                      # resolved loop excluded
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
    finally:
        srv.shutdown()


def test_api_loops_active_only_newest_first(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    try:
        cookie = _login(base)
        loops = json.loads(_get(base, "/api/loops", headers=cookie)[1])["loops"]
        assert [l["anchor_key"] for l in loops] == ["email:reply:sarah", "thread:abc"]
        top = loops[0]
        assert top == {"anchor_key": "email:reply:sarah", "action_type": "reply",
                       "channel": "email", "contact_name": "Sarah Chen", "status": "waiting",
                       "created_at": "2026-08-04", "times_surfaced": 5,
                       "summary": "Reply about the deck", "meeting_time": None}
        assert loops[1]["meeting_time"] == "Tomorrow 3pm"
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


def test_api_learned_summarizes_style_never_dumps_it(tmp_path):
    m, srv, base = _server(tmp_path)
    _fixtures(str(tmp_path))
    try:
        cookie = _login(base)
        code, body, _ = _get(base, "/api/learned", headers=cookie)
        learned = json.loads(body)
        assert code == 200
        assert learned["style"] == {"buckets": ["canonical", "master", "schema_version", "updated_at"],
                                    "per_person": 2, "updated_at": "2026-08-01T00:00:00Z"}
        assert "hey hey" not in body                          # sample text never leaves
        assert learned["preferences"] == {"rules": [{"id": "p1", "rule": "deprioritize newsletters"}]}
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
        # normalized server-side: exactly the Today-view contract, sorted by start, +2d filtered
        assert cal["events"] == [
            {"summary": "Board sync", "start": f"{today}T09:00:00-07:00",
             "end": f"{today}T10:00:00-07:00",
             "attendees": [{"name": "Sarah Chen", "email": "sarah@acme.com"},
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
        m.DASHBOARD._CAL_CACHE["ts"] = time.time() - m.DASHBOARD.CALENDAR_TTL_SECS - 1
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
        # row cap: 205 fresh outcome rows + the audit rows → exactly 200 come back
        many = "\n".join(json.dumps({"ts": _iso_ago(m, i), "outcome": "viewed", "n": i})
                         for i in range(205))
        _write(os.path.join(root, "outcomes.jsonl"), many + "\n")
        assert len(json.loads(_get(base, "/api/ledger", headers=cookie)[1])["entries"]) == 200
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
