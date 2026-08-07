"""Tests for the remote-MCP connector lane: connectors.py (discovery / DCR / PKCE flow / the pinned
token file) + the receiver endpoints that drive it (/connect/<service>/start, /connect/oauth/callback,
and the /setup 'Connected services' tiles). All HTTP is mocked via the injectable `_http` — the
sandbox (and CI) never talks to granola.ai."""
import hashlib
import base64
import importlib.util
import json
import os
import time
import urllib.parse

HERE = os.path.dirname(__file__)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


con = _load("connectors_t", "connectors.py")
rec = _load("receiver_conn", "receiver.py")

MCP_ORIGIN = "https://mcp.granola.ai"
AUTH_FALLBACK = "https://mcp-auth.granola.ai"
REDIRECT = "https://myapp.up.railway.app/connect/oauth/callback"


class MockHttp:
    """Routes by URL substring → (status, body). Records every call as (url, method, body)."""

    def __init__(self, routes):
        self.routes = routes            # list[(substr, (status, bytes))] — first match wins
        self.calls = []

    def __call__(self, url, method="GET", headers=None, body=None):
        self.calls.append((url, method, body))
        for substr, resp in self.routes:
            if substr in url:
                return resp
        return 404, b"not found"

    def urls(self):
        return [c[0] for c in self.calls]


def _meta(auth_base=AUTH_FALLBACK, **extra):
    m = {"authorization_endpoint": f"{auth_base}/oauth2/authorize",
         "token_endpoint": f"{auth_base}/oauth2/token",
         "registration_endpoint": f"{auth_base}/oauth2/register"}
    m.update(extra)
    return m


def _use(monkeypatch, mod, tmp_path, http):
    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    monkeypatch.setattr(mod, "_http", http)
    return http


# ── discovery fallback chain (RFC 9728 → RFC 8414 → OIDC spelling → conventional) ────────────────

def test_discovery_full_rfc_chain_and_cache(tmp_path, monkeypatch):
    http = _use(monkeypatch, con, tmp_path, MockHttp([
        ("/.well-known/oauth-protected-resource",
         (200, json.dumps({"authorization_servers": ["https://auth.example.com"]}).encode())),
        ("https://auth.example.com/.well-known/oauth-authorization-server",
         (200, json.dumps(_meta("https://auth.example.com")).encode())),
    ]))
    d = con.discover("granola")
    assert d["authorization_endpoint"] == "https://auth.example.com/oauth2/authorize"
    assert d["token_endpoint"] == "https://auth.example.com/oauth2/token"
    assert d["registration_endpoint"] == "https://auth.example.com/oauth2/register"
    assert d["supports_resource"] is True            # RFC 9728 metadata found → RFC 8707 applies
    # the RFC 9728 probe hit the MCP origin, not the auth host
    assert http.urls()[0].startswith(MCP_ORIGIN)
    # cached on the volume: a second discover makes NO http calls
    http.calls.clear()
    d2 = con.discover("granola")
    assert d2["token_endpoint"] == d["token_endpoint"] and http.calls == []


def test_discovery_falls_back_to_auth_fallback_metadata(tmp_path, monkeypatch):
    """No RFC 9728 doc → the registry's auth_fallback host serves the RFC 8414 metadata."""
    _use(monkeypatch, con, tmp_path, MockHttp([
        (AUTH_FALLBACK + "/.well-known/oauth-authorization-server",
         (200, json.dumps(_meta()).encode())),
    ]))
    d = con.discover("granola")
    assert d["authorization_endpoint"] == AUTH_FALLBACK + "/oauth2/authorize"
    assert d["supports_resource"] is False


def test_discovery_openid_configuration_fallback(tmp_path, monkeypatch):
    """oauth-authorization-server 404s → the OIDC spelling is tried next."""
    http = _use(monkeypatch, con, tmp_path, MockHttp([
        (AUTH_FALLBACK + "/.well-known/openid-configuration",
         (200, json.dumps(_meta(resource_indicators_supported=True)).encode())),
    ]))
    d = con.discover("granola")
    assert d["token_endpoint"] == AUTH_FALLBACK + "/oauth2/token"
    assert d["supports_resource"] is True            # advertised in the AS metadata
    assert any("oauth-authorization-server" in u for u in http.urls())  # tried in order


def test_discovery_conventional_fallback(tmp_path, monkeypatch):
    """Every metadata document 404s → the conventional /oauth2/* paths on auth_fallback (exactly
    what the Mac app used against mcp-auth.granola.ai)."""
    _use(monkeypatch, con, tmp_path, MockHttp([]))    # everything 404s
    d = con.discover("granola")
    assert d["authorization_endpoint"] == AUTH_FALLBACK + "/oauth2/authorize"
    assert d["token_endpoint"] == AUTH_FALLBACK + "/oauth2/token"
    assert d["registration_endpoint"] == AUTH_FALLBACK + "/oauth2/register"


def test_discovery_transport_dead_raises_discovery_step(tmp_path, monkeypatch):
    _use(monkeypatch, con, tmp_path,
         MockHttp([("", (0, b"proxy blocked egress"))]))  # every fetch is a transport failure
    try:
        con.discover("granola")
        assert False, "should have raised"
    except con.ConnectorError as e:
        assert e.step == "discovery" and "proxy blocked" in str(e)


# ── DCR: register once, cache, re-register on rejection ──────────────────────────────────────────

def _dcr_http(extra=()):
    return MockHttp([
        *extra,
        ("/oauth2/register", (201, json.dumps({"client_id": "dcr-client-1"}).encode())),
    ])


def test_dcr_registers_once_and_caches(tmp_path, monkeypatch):
    http = _use(monkeypatch, con, tmp_path, _dcr_http())
    cid = con.ensure_client("granola", REDIRECT)
    assert cid == "dcr-client-1"
    reg = [c for c in http.calls if "/oauth2/register" in c[0]]
    assert len(reg) == 1 and reg[0][1] == "POST"
    sent = json.loads(reg[0][2])
    assert sent == {"client_name": "Sotto", "redirect_uris": [REDIRECT],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"], "token_endpoint_auth_method": "none"}
    # cached → a second call makes no registration POST
    n = len(http.calls)
    assert con.ensure_client("granola", REDIRECT) == "dcr-client-1"
    assert len([c for c in http.calls[n:] if "register" in c[0]]) == 0


def test_dcr_reregisters_when_redirect_uri_changes(tmp_path, monkeypatch):
    http = _use(monkeypatch, con, tmp_path, _dcr_http())
    con.ensure_client("granola", REDIRECT)
    con.ensure_client("granola", "https://newhost.example/connect/oauth/callback")
    assert len([c for c in http.calls if "register" in c[0]]) == 2


def test_dcr_4xx_raises_registration_step_with_body(tmp_path, monkeypatch):
    _use(monkeypatch, con, tmp_path,
         MockHttp([("/oauth2/register", (400, b'{"error":"invalid_redirect_uri"}'))]))
    try:
        con.ensure_client("granola", REDIRECT)
        assert False, "should have raised"
    except con.ConnectorError as e:
        assert e.step == "registration" and "invalid_redirect_uri" in str(e)
    # nothing cached → a later attempt re-registers (fresh mock now succeeds)
    _use(monkeypatch, con, tmp_path, _dcr_http())
    assert con.ensure_client("granola", REDIRECT) == "dcr-client-1"


# ── start: the authorization redirect carries the full PKCE + state + resource shape ─────────────

def test_start_auth_redirect_shape(tmp_path, monkeypatch):
    _use(monkeypatch, con, tmp_path, MockHttp([
        ("/.well-known/oauth-protected-resource",
         (200, json.dumps({"authorization_servers": [AUTH_FALLBACK]}).encode())),
        ("/.well-known/oauth-authorization-server", (200, json.dumps(_meta()).encode())),
        ("/oauth2/register", (201, json.dumps({"client_id": "cid-42"}).encode())),
    ]))
    url = con.start_auth("granola", REDIRECT)
    base, _, qs = url.partition("?")
    assert base == AUTH_FALLBACK + "/oauth2/authorize"
    q = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
    assert q["response_type"] == "code"
    assert q["client_id"] == "cid-42"
    assert q["redirect_uri"] == REDIRECT
    assert q["code_challenge_method"] == "S256"
    assert q["resource"] == "https://mcp.granola.ai/mcp"   # RFC 8707 (metadata advertised it)
    # the challenge is the REAL S256 of the verifier persisted in the pending-state file
    pend = os.path.join(str(tmp_path), "connectors", ".pending", f"{q['state']}.json")
    entry = json.load(open(pend))
    assert entry["service"] == "granola" and entry["created_at"] > 0
    expect = base64.urlsafe_b64encode(
        hashlib.sha256(entry["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expect
    assert (os.stat(pend).st_mode & 0o777) == 0o600


# ── callback: happy path writes the PINNED token file; failures name their step ──────────────────

def _flow_http(token_resp):
    return MockHttp([
        ("/.well-known/oauth-authorization-server", (200, json.dumps(_meta()).encode())),
        ("/oauth2/register", (201, json.dumps({"client_id": "cid-42"}).encode())),
        ("/oauth2/token", token_resp),
    ])


def test_finish_auth_happy_path_writes_pinned_schema(tmp_path, monkeypatch):
    http = _use(monkeypatch, con, tmp_path, _flow_http(
        (200, json.dumps({"access_token": "at-1", "refresh_token": "rt-1",
                          "expires_in": 3600, "token_type": "Bearer"}).encode())))
    url = con.start_auth("granola", REDIRECT)
    state = urllib.parse.parse_qs(url.split("?", 1)[1])["state"][0]
    before = int(time.time())
    record = con.finish_auth(state, "auth-code-xyz", REDIRECT)
    # the exchange was PKCE-public: form carries the verifier + client_id, never a client_secret
    ex = [c for c in http.calls if "/oauth2/token" in c[0]][0]
    form = {k: v[0] for k, v in urllib.parse.parse_qs(ex[2].decode()).items()}
    assert form["grant_type"] == "authorization_code" and form["code"] == "auth-code-xyz"
    assert form["client_id"] == "cid-42" and form["redirect_uri"] == REDIRECT
    assert "code_verifier" in form and "client_secret" not in form
    # THE PINNED CONTRACT: exact keys, atomic file, 0600
    path = os.path.join(str(tmp_path), "connectors", "granola.json")
    on_disk = json.load(open(path))
    assert on_disk == record
    assert set(on_disk) == {"service", "access_token", "refresh_token", "expires_at",
                            "token_endpoint", "client_id", "resource", "mcp_url", "obtained_at"}
    assert on_disk["service"] == "granola" and on_disk["access_token"] == "at-1"
    assert on_disk["refresh_token"] == "rt-1"
    assert before + 3600 <= on_disk["expires_at"] <= before + 3610
    assert on_disk["token_endpoint"] == AUTH_FALLBACK + "/oauth2/token"
    assert on_disk["client_id"] == "cid-42"
    assert on_disk["resource"] == on_disk["mcp_url"] == "https://mcp.granola.ai/mcp"
    assert on_disk["obtained_at"] >= before
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert not os.path.exists(path + ".tmp")
    # the state was single-use: replaying the same callback fails at the state step
    try:
        con.finish_auth(state, "auth-code-xyz", REDIRECT)
        assert False, "replay should have raised"
    except con.ConnectorError as e:
        assert e.step == "state"


def test_finish_auth_no_expiry_or_refresh_becomes_null(tmp_path, monkeypatch):
    _use(monkeypatch, con, tmp_path, _flow_http(
        (200, json.dumps({"access_token": "at-2"}).encode())))
    url = con.start_auth("granola", REDIRECT)
    state = urllib.parse.parse_qs(url.split("?", 1)[1])["state"][0]
    rec_ = con.finish_auth(state, "c", REDIRECT)
    assert rec_["refresh_token"] is None and rec_["expires_at"] is None


def test_finish_auth_unknown_state_raises_state_step(tmp_path, monkeypatch):
    _use(monkeypatch, con, tmp_path, MockHttp([]))
    for bad in ("no-such-state-aaaaaaaa", "", "../../etc/passwd"):
        try:
            con.finish_auth(bad, "code", REDIRECT)
            assert False, "should have raised"
        except con.ConnectorError as e:
            assert e.step == "state"
    # nothing escaped the pending dir despite the traversal-shaped state
    assert not os.path.exists(os.path.join(str(tmp_path), "..", "etc"))


def test_finish_auth_expired_state_raises_state_step(tmp_path, monkeypatch):
    _use(monkeypatch, con, tmp_path, _flow_http((200, b"{}")))
    url = con.start_auth("granola", REDIRECT)
    state = urllib.parse.parse_qs(url.split("?", 1)[1])["state"][0]
    pend = os.path.join(str(tmp_path), "connectors", ".pending", f"{state}.json")
    entry = json.load(open(pend))
    entry["created_at"] = int(time.time()) - con.PENDING_TTL_SECS - 60
    json.dump(entry, open(pend, "w"))
    try:
        con.finish_auth(state, "code", REDIRECT)
        assert False, "should have raised"
    except con.ConnectorError as e:
        assert e.step == "state"
    assert not os.path.exists(pend)   # consumed even when expired


def test_exchange_failure_surfaces_upstream_error(tmp_path, monkeypatch):
    _use(monkeypatch, con, tmp_path, _flow_http(
        (400, b'{"error":"invalid_grant","error_description":"code is used"}')))
    url = con.start_auth("granola", REDIRECT)
    state = urllib.parse.parse_qs(url.split("?", 1)[1])["state"][0]
    try:
        con.finish_auth(state, "stale-code", REDIRECT)
        assert False, "should have raised"
    except con.ConnectorError as e:
        assert e.step == "exchange"
        assert "invalid_grant" in str(e) and "HTTP 400" in str(e)
    # no token file was written on failure
    assert not os.path.exists(os.path.join(str(tmp_path), "connectors", "granola.json"))


def test_exchange_invalid_client_drops_dcr_cache_for_reregistration(tmp_path, monkeypatch):
    http = _use(monkeypatch, con, tmp_path, _flow_http(
        (401, b'{"error":"invalid_client"}')))
    url = con.start_auth("granola", REDIRECT)
    state = urllib.parse.parse_qs(url.split("?", 1)[1])["state"][0]
    client_cache = os.path.join(str(tmp_path), "connectors", "granola.client.json")
    assert os.path.exists(client_cache)
    try:
        con.finish_auth(state, "code", REDIRECT)
        assert False, "should have raised"
    except con.ConnectorError as e:
        assert e.step == "exchange"
    assert not os.path.exists(client_cache)          # cache dropped …
    n = len([c for c in http.calls if "register" in c[0]])
    con.start_auth("granola", REDIRECT)              # … so the next start re-registers (the 4xx heal)
    assert len([c for c in http.calls if "register" in c[0]]) == n + 1


def test_pending_sweep_removes_only_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(con, "DATA", str(tmp_path))
    d = os.path.join(str(tmp_path), "connectors", ".pending")
    os.makedirs(d)
    old, fresh = os.path.join(d, "old-state.json"), os.path.join(d, "fresh-state.json")
    for p in (old, fresh):
        json.dump({"service": "granola", "code_verifier": "v", "created_at": 1}, open(p, "w"))
    stale = time.time() - con.PENDING_TTL_SECS - 60
    os.utime(old, (stale, stale))
    con.sweep_pending()
    assert not os.path.exists(old) and os.path.exists(fresh)


def test_service_status_reads_presence_and_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(con, "DATA", str(tmp_path))
    st = con.service_status()
    assert st == [{"service": "granola", "label": "Granola (meeting notes)", "connected": False,
                   "obtained_at": None, "expires_at": None}]
    os.makedirs(os.path.join(str(tmp_path), "connectors"))
    json.dump({"service": "granola", "access_token": "at", "obtained_at": 1754000000,
               "expires_at": 1754003600},
              open(os.path.join(str(tmp_path), "connectors", "granola.json"), "w"))
    st = con.service_status()
    assert st[0]["connected"] is True
    assert st[0]["obtained_at"] == 1754000000 and st[0]["expires_at"] == 1754003600


# ── receiver: /setup tiles + endpoint gating + the HTTP flow end to end ──────────────────────────

def test_setup_page_renders_connect_tile_when_not_linked(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    monkeypatch.setattr(rec.CONNECTORS, "DATA", str(tmp_path))
    monkeypatch.setattr(rec, "google_connected", lambda: (False, "nope"))
    page = rec._setup_page("abc")
    assert "Connected services" in page
    assert "Granola (meeting notes)" in page
    assert "/connect/granola/start?code=abc" in page          # the Connect button carries the code
    assert "connected since" not in page.split("Connected services", 1)[1]


def test_setup_page_renders_connected_tile_with_date(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    monkeypatch.setattr(rec.CONNECTORS, "DATA", str(tmp_path))
    monkeypatch.setattr(rec, "google_connected", lambda: (False, "nope"))
    os.makedirs(os.path.join(str(tmp_path), "connectors"))
    json.dump({"service": "granola", "access_token": "at", "obtained_at": int(time.time())},
              open(os.path.join(str(tmp_path), "connectors", "granola.json"), "w"))
    page = rec._setup_page("abc")
    tail = page.split("Connected services", 1)[1]
    assert "connected since" in tail
    assert "/connect/granola/start" not in tail               # no Connect button once linked


def test_connect_redirect_uri_derivation(monkeypatch):
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    assert rec.connect_redirect_uri() == "https://myapp.up.railway.app/connect/oauth/callback"
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "")
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setenv("SOTTO_TRIGGER_PORT", "8787")
    assert rec.connect_redirect_uri() == "http://localhost:8787/connect/oauth/callback"


def _serve(tmp_path):
    """A live receiver with its own connectors instance pointed at tmp_path (test_receiver.py's
    HTTP-matrix convention)."""
    import threading
    from http.server import ThreadingHTTPServer
    r2 = _load("receiver_conn_http", "receiver.py")
    r2.DATA = str(tmp_path)
    r2.CONNECTORS.DATA = str(tmp_path)
    r2.SETUP_CODE = "sekrit-code-123"
    r2.MCP_TOKEN = "bearer-tok"
    r2.TOKEN = "bearer-tok"
    r2.RAILWAY_DOMAIN = "myapp.up.railway.app"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), r2.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return r2, srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _get(base, path, headers=None):
    import urllib.error as _ue
    import urllib.request as _u

    class NoRedirect(_u.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = _u.build_opener(NoRedirect)
    try:
        with opener.open(_u.Request(base + path, headers=headers or {}), timeout=10) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except _ue.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def test_connect_endpoints_over_http(tmp_path):
    """The full matrix: /connect/<svc>/start is setup-code-gated and 302s with the S256 shape;
    /connect/oauth/callback is public, 400s a bogus state with a step-named body, and completes the
    happy path writing the pinned token file."""
    r2, srv, base = _serve(tmp_path)
    r2.CONNECTORS._http = _flow_http(
        (200, json.dumps({"access_token": "at-9", "refresh_token": "rt-9",
                          "expires_in": 60}).encode()))
    try:
        # gated: no code → 403 (and no discovery/DCR spend)
        code, body, _ = _get(base, "/connect/granola/start")
        assert code == 403 and "deploy logs" in body
        # unknown service (with the code) → 404 naming the config step
        code, body, _ = _get(base, "/connect/nope/start?code=sekrit-code-123")
        assert code == 404 and "config" in body
        # with the code → 302 to the authorization endpoint, full param shape
        code, _, hdrs = _get(base, "/connect/granola/start?code=sekrit-code-123")
        assert code == 302
        loc = hdrs.get("Location", "")
        assert loc.startswith(AUTH_FALLBACK + "/oauth2/authorize?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(loc.split("?", 1)[1]).items()}
        assert q["code_challenge_method"] == "S256" and q["client_id"] == "cid-42"
        assert q["redirect_uri"] == "https://myapp.up.railway.app/connect/oauth/callback"
        assert "sotto_setup=sekrit-code-123" in hdrs.get("Set-Cookie", "")  # wizard cookie granted
        # callback is NOT gated: a bogus state gets a 4xx page naming the state step (no setup code)
        code, body, _ = _get(base, "/connect/oauth/callback?code=x&state=bogus-state-aaaa")
        assert code == 400 and "state" in body and "Connection failed" in body
        # provider-denied consent → authorization step named
        code, body, _ = _get(base, f"/connect/oauth/callback?error=access_denied&state={q['state']}")
        assert code == 400 and "authorization" in body and "access_denied" in body
        # the deny burned the state → a fresh start, then the REAL callback completes the flow
        code, _, hdrs = _get(base, "/connect/granola/start?code=sekrit-code-123")
        st2 = urllib.parse.parse_qs(hdrs["Location"].split("?", 1)[1])["state"][0]
        code, body, _ = _get(base, f"/connect/oauth/callback?code=good-code&state={st2}")
        assert code == 200 and "Connected" in body and "/setup" in body
        tok = json.load(open(os.path.join(str(tmp_path), "connectors", "granola.json")))
        assert tok["access_token"] == "at-9" and tok["service"] == "granola"
        # exchange failure path: fresh start, token endpoint now errors → 502 naming exchange + body
        r2.CONNECTORS._http = _flow_http((400, b'{"error":"invalid_grant"}'))
        code, _, hdrs = _get(base, "/connect/granola/start?code=sekrit-code-123")
        st3 = urllib.parse.parse_qs(hdrs["Location"].split("?", 1)[1])["state"][0]
        code, body, _ = _get(base, f"/connect/oauth/callback?code=bad&state={st3}")
        assert code == 502 and "exchange" in body and "invalid_grant" in body
    finally:
        srv.shutdown()
