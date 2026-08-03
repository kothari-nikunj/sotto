import importlib.util
import json
import os
import sys

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("receiver", os.path.join(HERE, "receiver.py"))
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)


def test_unknown_type_400(tmp_path):
    rec.DATA = str(tmp_path)
    code, _ = rec.handle_trigger({"type": "lunch_ready"})
    assert code == 400


def test_rejects_path_traversal_date(tmp_path):
    rec.DATA = str(tmp_path)
    code, r = rec.handle_trigger({"type": "morning_ready", "date": "../../etc/cron.d/x"})
    assert code == 400 and r["error"] == "bad date"
    # nothing written outside the briefs dir
    assert not os.path.exists(os.path.join(str(tmp_path), "..", "etc"))


def test_enqueue_failure_leaves_no_delivered_flag(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    def boom(*_):
        raise FileNotFoundError("hermes missing")
    monkeypatch.setattr(rec, "run_skill", boom)
    code, r = rec.handle_trigger({"type": "morning_ready", "date": "2026-06-23"})
    assert code == 500
    # the day is NOT marked delivered, so a later working push still fires
    assert not os.path.exists(rec.delivered_flag("2026-06-23", "morning"))


def test_pairing_link_carries_scheme_host_and_token(monkeypatch):
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    link = rec.pairing_link()
    assert link.startswith("sotto-bridge://pair?")
    # full https host (prevents the schemeless-downgrade bug) + the bearer, both URL-encoded
    assert "host=https%3A%2F%2Fmyapp.up.railway.app" in link
    assert "token=tok123" in link


def test_exchange_google_code_rejects_empty():
    ok, msg = rec.exchange_google_code("")
    assert ok is False and "No code" in msg


def test_exchange_google_code_handles_missing_setup(monkeypatch):
    monkeypatch.setattr(rec, "_google_setup_py", lambda: None)
    ok, msg = rec.exchange_google_code("abc")
    assert ok is False and "setup tool not found" in msg


def test_set_timezone_validates_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "SETTINGS_FILE", os.path.join(str(tmp_path), "config", "settings.json"))
    # rejects junk / bare offsets (we want a real IANA zone for DST correctness)
    assert rec.set_timezone("")[0] is False
    assert rec.set_timezone("Mars/Phobos zzz")[0] is False
    assert rec.set_timezone("+05:30")[0] is False          # no '/', not IANA
    ok, val = rec.set_timezone("America/Los_Angeles")
    assert ok and val == "America/Los_Angeles"
    assert rec.read_settings()["timezone"] == "America/Los_Angeles"


def test_setup_google_client_rejects_bad_input(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "_google_setup_py", lambda: "/nonexistent/setup.py")
    assert rec.setup_google_client("")[0] is False
    assert rec.setup_google_client("not json")[0] is False
    ok, msg = rec.setup_google_client('{"nope": 1}')      # valid JSON, not an OAuth client
    assert ok is False and "OAuth client" in msg


def test_setup_google_client_missing_tool(monkeypatch):
    monkeypatch.setattr(rec, "_google_setup_py", lambda: None)
    ok, msg = rec.setup_google_client('{"installed": {"client_id": "x"}}')
    assert ok is False and "setup tool not found" in msg


def test_setup_status_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "SETTINGS_FILE", os.path.join(str(tmp_path), "config", "settings.json"))
    monkeypatch.setattr(rec, "google_connected", lambda: (False, "nope"))
    monkeypatch.delenv("SOTTO_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    rec.write_setting("timezone", "Europe/Paris")
    st = rec.setup_status()
    for k in ("bridge_connected", "google_connected", "google_client_present", "timezone", "whatsapp"):
        assert k in st
    assert st["timezone"] == "Europe/Paris"


def test_setup_page_renders(monkeypatch):
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    monkeypatch.setattr(rec, "google_connected", lambda: (False, "nope"))
    page = rec._setup_page()
    assert "Set up Sotto" in page and "sotto-bridge://pair?" in page
    assert "1 · Link your Mac" in page and "2 · Connect Google" in page
    assert "4 · Timezone" in page


def test_setup_page_google_box_has_the_full_recipe(monkeypatch):
    """When no OAuth client is saved yet, the Google box must walk the user through ALL of it:
    enable the two APIs, publish the consent screen to In production (else the token dies in ~7
    days), create a Desktop-app client, download + paste the JSON. Omitting any step strands a
    fresh Google Cloud project at 'Save client' with a client that can't authorize."""
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    monkeypatch.setattr(rec, "setup_status", lambda: {
        "bridge_connected": False, "google_connected": False, "google_detail": "nope",
        "google_client_present": False, "timezone": "", "whatsapp": "unknown"})
    page = rec._setup_page("abc")
    assert "Gmail API" in page and "Google Calendar API" in page          # step 1: enable APIs
    assert "In production" in page and "~7 days" in page                  # step 2: consent published
    assert "Desktop app" in page and "Download JSON" in page              # step 3: client + JSON
    assert "/setup/google-client?code=abc" in page                        # step 4: paste form


def test_stale_claim_retries_when_never_delivered(tmp_path, monkeypatch):
    """A claim with no .delivered marker after 30 min = the spawned run died silently. A fresh
    trigger must reclaim and retry instead of losing the day's brief."""
    rec.DATA = str(tmp_path)
    calls = []
    monkeypatch.setattr(rec, "run_skill", lambda s, p: calls.append((s, p)))
    flag = rec.delivered_flag("2026-06-24", "morning")
    os.makedirs(os.path.dirname(flag), exist_ok=True)
    open(flag, "w").close()
    old = 1  # epoch — way past CLAIM_STALE_SECS
    os.utime(flag, (old, old))
    code, r = rec.handle_trigger({"type": "morning_ready", "date": "2026-06-24"})
    assert code == 202 and r["status"] == "enqueued"
    assert len(calls) == 1
    assert os.path.exists(flag)  # re-claimed (fresh mtime), so a THIRD trigger still dedupes


def test_stale_claim_not_retried_if_delivered(tmp_path, monkeypatch):
    """If brief_marker wrote .delivered, an old claim is NOT stale — never double-deliver."""
    rec.DATA = str(tmp_path)
    calls = []
    monkeypatch.setattr(rec, "run_skill", lambda s, p: calls.append((s, p)))
    flag = rec.delivered_flag("2026-06-25", "morning")
    os.makedirs(os.path.dirname(flag), exist_ok=True)
    open(flag, "w").close()
    os.utime(flag, (1, 1))
    open(os.path.join(str(tmp_path), "briefs", "2026-06-25.morning.delivered"), "w").close()
    code, r = rec.handle_trigger({"type": "morning_ready", "date": "2026-06-25"})
    assert code == 200 and r["status"] == "already_delivered"
    assert calls == []


def test_stale_reclaim_is_serialized_single_spawn(tmp_path, monkeypatch):
    """The stale-reclaim path (remove → O_EXCL create) is guarded by _CLAIM_LOCK so two triggers
    racing on a stale claim can't both reclaim: the winner reclaims (fresh mtime), the loser sees a
    fresh claim and dedupes. Exercised sequentially — the lock makes the interleaving equivalent."""
    rec.DATA = str(tmp_path)
    calls = []
    monkeypatch.setattr(rec, "run_skill", lambda s, p: calls.append((s, p)))
    flag = rec.delivered_flag("2026-06-27", "morning")
    os.makedirs(os.path.dirname(flag), exist_ok=True)
    open(flag, "w").close()
    os.utime(flag, (1, 1))                       # stale claim, never delivered
    code1, r1 = rec.handle_trigger({"type": "morning_ready", "date": "2026-06-27"})
    code2, r2 = rec.handle_trigger({"type": "morning_ready", "date": "2026-06-27"})
    assert (code1, r1["status"]) == (202, "enqueued")
    assert (code2, r2["status"]) == (200, "already_delivered")
    assert len(calls) == 1                       # exactly one brief spawned
    assert isinstance(rec._CLAIM_LOCK, type(rec.threading.Lock()))


def test_fresh_claim_still_dedupes(tmp_path, monkeypatch):
    """A recent claim (run plausibly in flight) must keep deduping even without .delivered yet."""
    rec.DATA = str(tmp_path)
    calls = []
    monkeypatch.setattr(rec, "run_skill", lambda s, p: calls.append((s, p)))
    flag = rec.delivered_flag("2026-06-26", "morning")
    os.makedirs(os.path.dirname(flag), exist_ok=True)
    open(flag, "w").close()  # fresh mtime = now
    code, r = rec.handle_trigger({"type": "morning_ready", "date": "2026-06-26"})
    assert code == 200 and r["status"] == "already_delivered"
    assert calls == []


def test_setup_code_env_override(monkeypatch):
    monkeypatch.setattr(rec, "SETUP_CODE", None)
    monkeypatch.setenv("SOTTO_SETUP_CODE", "from-env-123")
    assert rec.resolve_setup_code() == "from-env-123"


def test_setup_code_generated_and_persisted(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    monkeypatch.setattr(rec, "SETUP_CODE", None)
    monkeypatch.delenv("SOTTO_SETUP_CODE", raising=False)
    code = rec.resolve_setup_code()
    assert len(code) >= 8
    path = os.path.join(str(tmp_path), "setup_code")
    assert open(path).read().strip() == code
    assert (os.stat(path).st_mode & 0o777) == 0o600
    # survives a restart: a fresh resolve reads the SAME persisted code
    monkeypatch.setattr(rec, "SETUP_CODE", None)
    assert rec.resolve_setup_code() == code


def test_setup_pages_carry_the_code_between_wizard_pages(monkeypatch):
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    # client present but not yet authorized → the wizard shows the /google/auth link
    monkeypatch.setattr(rec, "setup_status", lambda: {
        "bridge_connected": False, "google_connected": False, "google_detail": "nope",
        "google_client_present": True, "timezone": "", "whatsapp": "unknown"})
    page = rec._setup_page("abc")
    assert "/google/auth?code=abc" in page and "/whatsapp/qr?code=abc" in page
    assert "/setup/timezone?code=abc" in page          # the auto-detect POST keeps the code too
    # client NOT present → the paste form posts with the code
    monkeypatch.setattr(rec, "setup_status", lambda: {
        "bridge_connected": False, "google_connected": False, "google_detail": "nope",
        "google_client_present": False, "timezone": "", "whatsapp": "unknown"})
    page = rec._setup_page("abc")
    assert "/setup/google-client?code=abc" in page
    # the deep link the user pairs the Mac app with is rendered on the SERVED page (/setup)
    assert "sotto-bridge://pair?" in page and "Open in Sotto Bridge" in page


def test_setup_surface_gating_over_http(tmp_path, monkeypatch):
    """Auth matrix for the setup surface: 403 without the code (no token material in the body),
    200 with ?code= (sets the wizard cookie), with the cookie, or with the MCP bearer. /health
    stays open; /whatsapp/qr and /debug/google and the setup POSTs are gated too."""
    import importlib.util as _il
    import threading
    import urllib.error as _ue
    import urllib.request as _u
    from http.server import ThreadingHTTPServer

    spec2 = _il.spec_from_file_location("receiver2", os.path.join(HERE, "receiver.py"))
    r2 = _il.module_from_spec(spec2)
    spec2.loader.exec_module(r2)
    r2.DATA = str(tmp_path)
    r2.SETTINGS_FILE = os.path.join(str(tmp_path), "config", "settings.json")
    r2.SETUP_CODE = "sekrit-code-123"
    r2.MCP_TOKEN = "bearer-tok"
    r2.TOKEN = "bearer-tok"
    r2.RAILWAY_DOMAIN = "myapp.up.railway.app"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), r2.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path, headers=None):
        try:
            with _u.urlopen(_u.Request(base + path, headers=headers or {}), timeout=10) as resp:
                return resp.status, resp.read().decode(), dict(resp.headers)
        except _ue.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def post(path, data, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        try:
            with _u.urlopen(_u.Request(base + path, data=data, headers=h, method="POST"), timeout=10) as resp:
                return resp.status, resp.read().decode()
        except _ue.HTTPError as e:
            return e.code, e.read().decode()

    try:
        # /health stays open
        code, _, _ = get("/health")
        assert code == 200
        # 403 without / with a wrong code — and NO token or code material in the response
        for path in ("/setup", "/pair", "/whatsapp/qr", "/debug/google", "/setup?code=wrong"):
            code, body, _ = get(path)
            assert code == 403, path
            assert "deploy logs" in body
            assert "bearer-tok" not in body and "sekrit-code-123" not in body
        # valid ?code= → 200 + the wizard cookie + the pairing link is present
        code, body, hdrs = get("/setup?code=sekrit-code-123")
        assert code == 200 and "sotto-bridge://pair?" in body
        assert "sotto_setup=sekrit-code-123" in hdrs.get("Set-Cookie", "")
        # cookie alone authenticates the next page
        code, body, _ = get("/whatsapp/qr", headers={"Cookie": "sotto_setup=sekrit-code-123"})
        assert code == 200
        # MCP bearer authenticates too
        code, _, _ = get("/debug/google", headers={"Authorization": "Bearer bearer-tok"})
        assert code in (200, 503)   # 503 = "not connected" detail, still authorized
        # setup POSTs are gated: 403 without, works with the cookie
        code, _ = post("/setup/timezone", b'{"timezone":"America/Los_Angeles"}')
        assert code == 403
        code, body = post("/setup/timezone", b'{"timezone":"America/Los_Angeles"}',
                          headers={"Cookie": "sotto_setup=sekrit-code-123"})
        assert code == 200 and json.loads(body)["ok"] is True
    finally:
        srv.shutdown()


def test_enqueue_then_dedupe(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    calls = []
    monkeypatch.setattr(rec, "run_skill", lambda s, p: calls.append((s, p)))
    code1, r1 = rec.handle_trigger({"type": "morning_ready", "date": "2026-06-23", "local_data": {"window_hours": 24}})
    assert code1 == 202 and r1["skill"] == "sotto-morning-brief"
    # payload staged
    payload = json.load(open(os.path.join(str(tmp_path), "briefs", "2026-06-23.morning_ready.payload.json")))
    assert payload["window_hours"] == 24
    # second push same day -> dedupe
    code2, r2 = rec.handle_trigger({"type": "morning_ready", "date": "2026-06-23"})
    assert code2 == 200 and r2["status"] == "already_delivered"
    assert len(calls) == 1


def _fresh_module(monkeypatch, env):
    import importlib.util as _il
    for k in ("SOTTO_TRIGGER_TOKEN", "SOTTO_MCP_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = _il.spec_from_file_location("receiver_tok", os.path.join(HERE, "receiver.py"))
    m = _il.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def test_trigger_token_falls_back_to_bridge_token(monkeypatch):
    # Wake-push is on by default and authenticates with the Bridge token, so with only
    # SOTTO_MCP_TOKEN (= BRIDGE_TOKEN) set, /sotto/trigger must accept it — no silent 401s.
    m = _fresh_module(monkeypatch, {"SOTTO_MCP_TOKEN": "bridge-tok"})
    assert m.TOKEN == "bridge-tok" and m.MCP_TOKEN == "bridge-tok"


def test_dedicated_trigger_token_still_wins(monkeypatch):
    m = _fresh_module(monkeypatch, {"SOTTO_MCP_TOKEN": "bridge-tok", "SOTTO_TRIGGER_TOKEN": "trig-tok"})
    assert m.TOKEN == "trig-tok" and m.MCP_TOKEN == "bridge-tok"


# ── Event-driven proactive wake (Phase 2b) ───────────────────────────────────────────────────────

def test_proactive_wake_spawns_the_proactive_skill_once(tmp_path, monkeypatch):
    """A valid proactive_wake trigger runs the proactive skill (no date/payload needed) and stamps the
    server-side throttle marker."""
    rec.DATA = str(tmp_path)
    calls = []
    monkeypatch.setattr(rec, "run_proactive_skill", lambda: calls.append(1))
    code, r = rec.handle_trigger({"type": "proactive_wake"})
    assert code == 202 and r["skill"] == "sotto-proactive"
    assert len(calls) == 1
    assert os.path.exists(rec._proactive_wake_marker())  # marker written before spawning


def test_proactive_wake_second_within_window_is_throttled(tmp_path, monkeypatch):
    """The Bridge already throttles to 30 min; the server backs it up — a second wake inside the 25-min
    window is skipped without spawning."""
    rec.DATA = str(tmp_path)
    calls = []
    monkeypatch.setattr(rec, "run_proactive_skill", lambda: calls.append(1))
    code1, r1 = rec.handle_trigger({"type": "proactive_wake"})
    code2, r2 = rec.handle_trigger({"type": "proactive_wake"})
    assert (code1, r1["status"]) == (202, "enqueued")
    assert (code2, r2["status"]) == (200, "throttled")
    assert len(calls) == 1  # only the first ran


def test_proactive_wake_fires_again_after_window(tmp_path, monkeypatch):
    """Once the marker ages past the throttle window, a fresh wake runs again."""
    rec.DATA = str(tmp_path)
    calls = []
    monkeypatch.setattr(rec, "run_proactive_skill", lambda: calls.append(1))
    rec.handle_trigger({"type": "proactive_wake"})
    marker = rec._proactive_wake_marker()
    old = rec.time.time() - rec.PROACTIVE_THROTTLE_SECS - 60
    os.utime(marker, (old, old))
    code, r = rec.handle_trigger({"type": "proactive_wake"})
    assert code == 202 and r["status"] == "enqueued"
    assert len(calls) == 2


def test_proactive_wake_enqueue_failure_returns_500(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    def boom():
        raise FileNotFoundError("hermes missing")
    monkeypatch.setattr(rec, "run_proactive_skill", boom)
    code, r = rec.handle_trigger({"type": "proactive_wake"})
    assert code == 500 and "enqueue failed" in r["error"]


def test_proactive_wake_spawn_failure_clears_marker_then_retry_runs(tmp_path, monkeypatch):
    """On spawn failure the throttle marker must be un-stamped, so an honest retry (the Bridge
    un-stamps itself on non-2xx) actually spawns instead of hitting a phantom 'throttled' — which
    would make both sides record a run that never happened."""
    rec.DATA = str(tmp_path)
    def boom():
        raise FileNotFoundError("hermes missing")
    monkeypatch.setattr(rec, "run_proactive_skill", boom)
    code, r = rec.handle_trigger({"type": "proactive_wake"})
    assert code == 500 and "enqueue failed" in r["error"]
    assert not os.path.exists(rec._proactive_wake_marker())  # marker cleared, no phantom throttle
    # immediate retry with a working spawn actually runs (NOT throttled) and stamps the marker
    calls = []
    monkeypatch.setattr(rec, "run_proactive_skill", lambda: calls.append(1))
    code2, r2 = rec.handle_trigger({"type": "proactive_wake"})
    assert (code2, r2["status"]) == (202, "enqueued")
    assert len(calls) == 1
    assert os.path.exists(rec._proactive_wake_marker())  # success path still throttles the next


def test_morning_ready_path_unchanged_by_proactive_branch(tmp_path, monkeypatch):
    """The proactive_wake branch must not disturb the brief path: morning_ready still stages the
    payload and enqueues the brief skill."""
    rec.DATA = str(tmp_path)
    calls = []
    monkeypatch.setattr(rec, "run_skill", lambda s, p: calls.append((s, p)))
    code, r = rec.handle_trigger({"type": "morning_ready", "date": "2026-07-06",
                                  "local_data": {"window_hours": 24}})
    assert code == 202 and r["skill"] == "sotto-morning-brief"
    assert calls and calls[0][0] == "sotto-morning-brief"
    payload = json.load(open(os.path.join(str(tmp_path), "briefs", "2026-07-06.morning_ready.payload.json")))
    assert payload["window_hours"] == 24


def test_proactive_wake_requires_the_trigger_token(tmp_path, monkeypatch):
    """proactive_wake is POSTed to /sotto/trigger, so the bearer guard (TOKEN) still applies — a bad
    token 401s and never reaches handle_proactive_wake. Exercised over real HTTP."""
    import importlib.util as _il
    import threading
    import urllib.error as _ue
    import urllib.request as _u
    from http.server import ThreadingHTTPServer

    spec2 = _il.spec_from_file_location("receiver_pw", os.path.join(HERE, "receiver.py"))
    r2 = _il.module_from_spec(spec2)
    spec2.loader.exec_module(r2)
    r2.DATA = str(tmp_path)
    r2.TOKEN = "trig-tok"
    r2.MCP_TOKEN = "trig-tok"
    spawned = []
    r2.run_proactive_skill = lambda: spawned.append(1)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), r2.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def post(token):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        req = _u.Request(base + "/sotto/trigger", data=b'{"type":"proactive_wake"}', headers=h, method="POST")
        try:
            with _u.urlopen(req, timeout=10) as resp:
                return resp.status
        except _ue.HTTPError as e:
            return e.code

    try:
        assert post(None) == 401             # no bearer
        assert post("wrong") == 401          # wrong bearer
        assert spawned == []                 # neither reached the handler
        assert post("trig-tok") == 202       # correct bearer runs it
        assert len(spawned) == 1
    finally:
        srv.shutdown()
