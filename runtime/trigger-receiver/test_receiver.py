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
    assert "Link your Mac" in page and "Connect Google" in page
    assert "Timezone" in page


def test_setup_page_shares_the_app_shell(monkeypatch):
    """/setup is the Connections view of the SAME site as /app: it links the dashboard stylesheet
    first, then the setup layer, renders the shared sidebar nav (with the /app section links and
    Connections marked current), and carries no inline <style> blocks or style= attributes."""
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    monkeypatch.setattr(rec, "google_connected", lambda: (False, "nope"))
    page = rec._setup_page("abc")
    # both stylesheets, app.css first
    assert "href='/static/app.css'" in page and "href='/static/setup.css'" in page
    assert page.index("/static/app.css") < page.index("/static/setup.css")
    # shared shell + nav markup contract
    assert "<body class='setup'>" in page
    assert "class='site'" in page and "class='sidebar'" in page
    assert "class='wordmark' href='/app'" in page
    for dest in ("/app#today", "/app#loops", "/app#briefs", "/app#people", "/app#learned"):
        assert f"href='{dest}'" in page, dest
    assert "href='/setup' class='active' aria-current='page'>Connections</a>" in page
    assert "class='content setup-page'" in page
    assert "class='eyebrow'" in page and "class='page-title'" in page and "class='page-sub'" in page
    # five tiles in contract markup; no leftover inline styling
    assert page.count("<section class='tile'") == 5
    assert page.count("class='tile-head'") == 5 and page.count("class='tile-body'") == 5
    assert "<style>" not in page and " style='" not in page and ' style="' not in page


def test_setup_page_hero_cta_only_when_steps_1_to_4_done(monkeypatch):
    """The wizard→app handoff: .hero-cta renders iff Mac + Google + timezone are done and WhatsApp
    is not mid-pairing. Tile 5 (optional services) never gates it."""
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    st = {"bridge_connected": True, "google_connected": True, "google_detail": "ok",
          "google_client_present": True, "timezone": "America/Los_Angeles", "whatsapp": "unknown"}
    monkeypatch.setattr(rec, "setup_status", lambda: dict(st))
    page = rec._setup_page("abc")
    assert "class='hero-cta' href='/app'" in page and "Open your dashboard" in page
    # WhatsApp mid-pairing → no handoff yet
    monkeypatch.setattr(rec, "setup_status", lambda: dict(st, whatsapp="pairing"))
    assert "hero-cta" not in rec._setup_page("abc")
    # any of steps 1-3 missing → no handoff either
    monkeypatch.setattr(rec, "setup_status", lambda: dict(st, google_connected=False))
    assert "hero-cta" not in rec._setup_page("abc")
    monkeypatch.setattr(rec, "setup_status", lambda: dict(st, timezone=""))
    assert "hero-cta" not in rec._setup_page("abc")


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


# ── Event-driven ingestion (Phase 2: POST /bridge/events) ────────────────────────────────────────

def _ev(rowid=1, **kw):
    e = {"source": "imessage", "rowid": rowid, "handle": "+14155551234", "is_from_me": False,
         "timestamp": "2026-08-06T10:00:00Z", "text": "hey", "is_group_chat": False,
         "chat_guid": None, "group_name": None, "group_participants": []}
    e.update(kw)
    return e


def test_bridge_events_dedupes_on_source_rowid(tmp_path, monkeypatch):
    """The seen ring dedupes (source,rowid): a full duplicate batch answers drop without re-running
    triage; a mixed batch triages only the fresh events."""
    rec.DATA = str(tmp_path)
    calls = []
    monkeypatch.setattr(rec, "run_triage",
                        lambda evs, c: (calls.append(evs), {"verdict": "queue", "reason": "r", "bundle": {}})[1])
    code1, r1 = rec.handle_events({"events": [_ev(1), _ev(2)]})
    assert code1 == 200 and r1["verdict"] == "queue"
    assert len(calls) == 1 and len(calls[0]) == 2
    code2, r2 = rec.handle_events({"events": [_ev(1), _ev(2)]})
    assert code2 == 200 and r2["verdict"] == "drop" and "duplicate" in r2["reason"]
    assert len(calls) == 1                              # triage NOT re-run for known events
    code3, _ = rec.handle_events({"events": [_ev(2), _ev(3)]})
    assert code3 == 200
    assert [e["rowid"] for e in calls[1]] == [3]        # only the fresh one reaches triage
    # different source, same rowid = a DIFFERENT event (key is the pair)
    rec.handle_events({"events": [_ev(3, source="whatsapp", contact_jid="1@s.whatsapp.net")]})
    assert [e["source"] for e in calls[2]] == ["whatsapp"]


def test_bridge_events_verdict_is_passed_through_as_the_body(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    verdict = {"verdict": "queue", "reason": "quiet hours", "bundle": {}}
    monkeypatch.setattr(rec, "run_triage", lambda evs, c: verdict)
    code, resp = rec.handle_events({"events": [_ev(20)], "catchup": True})
    assert code == 200 and resp == verdict


def test_bridge_events_catchup_flag_reaches_triage(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    seen = []
    monkeypatch.setattr(rec, "run_triage",
                        lambda evs, c: (seen.append(c), {"verdict": "drop", "reason": "", "bundle": {}})[1])
    rec.handle_events({"events": [_ev(30)], "catchup": True})
    rec.handle_events({"events": [_ev(31)]})
    assert seen == [True, False]


def test_bridge_events_agent_verdict_stages_bundle_and_spawns(tmp_path, monkeypatch):
    """verdict=="agent" → the bundle is written under $SOTTO_DATA/events/ and the sotto-event
    one-shot is spawned from a background thread with that path."""
    rec.DATA = str(tmp_path)
    bundle = {"events": [{"sender": "Sarah Chen", "class": "missed_call"}]}
    monkeypatch.setattr(rec, "run_triage",
                        lambda evs, c: {"verdict": "agent", "reason": "missed call", "bundle": bundle})
    spawned = []
    monkeypatch.setattr(rec, "run_event_skill", lambda p: spawned.append(p))
    code, resp = rec.handle_events({"events": [_ev(40, source="calls", phone="+14155551234")]})
    assert code == 200 and resp["verdict"] == "agent"
    deadline = rec.time.time() + 5                      # spawn happens on a daemon thread
    while not spawned and rec.time.time() < deadline:
        rec.time.sleep(0.01)
    assert len(spawned) == 1
    assert os.path.dirname(spawned[0]) == os.path.join(str(tmp_path), "events")
    assert os.path.basename(spawned[0]).startswith("bundle-")
    assert json.load(open(spawned[0])) == bundle        # staged bundle is the verdict's, verbatim


def test_bridge_events_non_agent_verdicts_never_spawn(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    spawned = []
    monkeypatch.setattr(rec, "run_event_skill", lambda p: spawned.append(p))
    for i, v in enumerate(("queue", "drop")):
        monkeypatch.setattr(rec, "run_triage", lambda evs, c, v=v: {"verdict": v, "reason": "", "bundle": {}})
        code, _ = rec.handle_events({"events": [_ev(50 + i)]})
        assert code == 200
    rec.time.sleep(0.05)
    assert spawned == []
    assert os.path.exists(os.path.join(str(tmp_path), "events", "seen.json"))  # ring still written


def test_bridge_events_triage_failure_is_claim_free(tmp_path, monkeypatch):
    """A triage failure 500s WITHOUT marking the events seen, so an honest Bridge retry re-triages
    the same events instead of losing them to the dedupe ring."""
    rec.DATA = str(tmp_path)

    def boom(evs, c):
        raise OSError("triage script missing")
    monkeypatch.setattr(rec, "run_triage", boom)
    code, resp = rec.handle_events({"events": [_ev(60)]})
    assert code == 500 and "triage failed" in resp["error"]
    calls = []
    monkeypatch.setattr(rec, "run_triage",
                        lambda evs, c: (calls.append(evs), {"verdict": "drop", "reason": "ok", "bundle": {}})[1])
    code2, _ = rec.handle_events({"events": [_ev(60)]})
    assert code2 == 200
    assert calls and [e["rowid"] for e in calls[0]] == [60]   # retry actually re-triaged


def test_bridge_events_bad_shapes_400(tmp_path):
    rec.DATA = str(tmp_path)
    assert rec.handle_events({})[0] == 400
    assert rec.handle_events({"events": "nope"})[0] == 400
    assert rec.handle_events({"events": [1, "x"]})[0] == 400


def test_bridge_events_seen_ring_is_capped(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    monkeypatch.setattr(rec, "run_triage", lambda evs, c: {"verdict": "drop", "reason": "", "bundle": {}})
    rec.handle_events({"events": [_ev(i) for i in range(rec.EVENTS_SEEN_MAX + 300)]})
    seen = json.load(open(os.path.join(str(tmp_path), "events", "seen.json")))
    assert len(seen) == rec.EVENTS_SEEN_MAX
    assert seen[-1] == f"imessage:{rec.EVENTS_SEEN_MAX + 299}"   # newest kept, oldest evicted


def test_bridge_events_requires_mcp_bearer_and_rejects_bad_json(tmp_path):
    """Auth matrix over real HTTP: /bridge/events takes the SAME bearer as /bridge/poll (the MCP
    token); no/wrong bearer 401s before any triage, malformed JSON 400s, and a good request returns
    the triage verdict verbatim."""
    import importlib.util as _il
    import threading
    import urllib.error as _ue
    import urllib.request as _u
    from http.server import ThreadingHTTPServer

    spec2 = _il.spec_from_file_location("receiver_ev", os.path.join(HERE, "receiver.py"))
    r2 = _il.module_from_spec(spec2)
    spec2.loader.exec_module(r2)
    r2.DATA = str(tmp_path)
    r2.MCP_TOKEN = "ev-tok"
    r2.TOKEN = "trig-tok"
    triaged = []
    r2.run_triage = lambda evs, c: (triaged.append(evs), {"verdict": "queue", "reason": "r", "bundle": {}})[1]

    srv = ThreadingHTTPServer(("127.0.0.1", 0), r2.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def post(data, token=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        req = _u.Request(base + "/bridge/events", data=data, headers=h, method="POST")
        try:
            with _u.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except _ue.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    body = json.dumps({"events": [_ev(70)]}).encode()
    try:
        assert post(body)[0] == 401                      # no bearer
        assert post(body, token="wrong")[0] == 401       # wrong bearer
        assert triaged == []                             # never reached triage
        code, resp = post(b"{not json", token="ev-tok")  # malformed JSON → 400
        assert code == 400 and resp["error"] == "bad json"
        code, resp = post(body, token="ev-tok")          # the MCP bearer works
        assert code == 200 and resp["verdict"] == "queue"
        assert len(triaged) == 1
    finally:
        srv.shutdown()


def test_email_poll_secs_parsing(monkeypatch):
    monkeypatch.delenv("SOTTO_EMAIL_POLL_SECS", raising=False)
    assert rec._email_poll_secs() == 90                  # default
    monkeypatch.setenv("SOTTO_EMAIL_POLL_SECS", "30")
    assert rec._email_poll_secs() == 30
    monkeypatch.setenv("SOTTO_EMAIL_POLL_SECS", "junk")
    assert rec._email_poll_secs() == 90                  # garbage → default
    monkeypatch.setenv("SOTTO_EMAIL_POLL_SECS", "0")
    assert rec._email_poll_secs() == 0                   # 0 disables
    assert rec.start_gmail_poll_thread() is None         # disabled → no thread


def test_extract_google_code_accepts_pasted_redirect_url():
    """Users paste the whole `http://localhost:1/?code=…&scope=…` redirect URL (or its query string)
    instead of the bare code — the code param must be extracted; a bare code passes through."""
    assert rec._extract_google_code("4/0Axyz-abc") == "4/0Axyz-abc"                     # bare code
    assert rec._extract_google_code(
        "http://localhost:1/?code=4%2F0Axyz-abc&scope=email+calendar") == "4/0Axyz-abc"  # full URL
    assert rec._extract_google_code("code=4/0Axyz&scope=email") == "4/0Axyz"             # bare query
    assert rec._extract_google_code("  4/0Axyz \n") == "4/0Axyz"                         # whitespace
    assert rec._extract_google_code("") == ""
    # extraction happens before validation: a URL with an EMPTY code param reads as "no code"
    ok, msg = rec.exchange_google_code("http://localhost:1/?code=&scope=email")
    assert ok is False and "No code" in msg


def test_poll_gmail_once_raises_when_script_missing(monkeypatch):
    """A broken poll must RAISE (so the loop's consecutive-failure counter sees it), never
    masquerade as a quiet [] mailbox."""
    monkeypatch.setattr(rec, "_find_sotto_script", lambda *a: None)
    try:
        rec._poll_gmail_once()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "not found" in str(e)


def test_events_stamp_written_and_surfaced(tmp_path, monkeypatch):
    """Accepted events touch $SOTTO_DATA/events/last.stamp; setup_status surfaces it as
    last_event_at (ISO), None before any event has landed."""
    rec.DATA = str(tmp_path)
    monkeypatch.setattr(rec, "google_connected", lambda: (False, "nope"))
    assert rec.setup_status()["last_event_at"] is None            # fresh install: no stamp
    monkeypatch.setattr(rec, "run_triage", lambda evs, c: {"verdict": "drop", "reason": "", "bundle": {}})
    code, _ = rec.handle_events({"events": [_ev(90)]})
    assert code == 200
    assert os.path.exists(os.path.join(str(tmp_path), "events", "last.stamp"))
    at = rec.setup_status()["last_event_at"]
    assert at is not None and at.endswith("Z")


def test_setup_page_shows_last_event_only_when_bridge_connected(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    st = {"bridge_connected": True, "google_connected": False, "google_detail": "nope",
          "google_client_present": False, "timezone": "", "whatsapp": "unknown"}
    monkeypatch.setattr(rec, "setup_status", lambda: st)
    assert "last event" not in rec._setup_page()                  # connected, but no stamp yet
    rec._touch_event_stamp()
    assert "last event just now" in rec._setup_page()             # connected + stamp → quiet line
    st["bridge_connected"] = False
    assert "last event" not in rec._setup_page()                  # disconnected → never rendered


def test_setup_page_connector_tile_downgrades_to_reconnect(tmp_path, monkeypatch):
    """A token file alone isn't 'Connected': a gather-written <service>.error file (or an expired
    token with no refresh token) turns the tile into ⚠️ Reconnect pointing at the same start URL."""
    rec.DATA = str(tmp_path)
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    monkeypatch.setattr(rec, "setup_status", lambda: {
        "bridge_connected": False, "google_connected": False, "google_detail": "nope",
        "google_client_present": False, "timezone": "", "whatsapp": "unknown"})
    status = [{"service": "granola", "label": "Granola (meeting notes)", "connected": True,
               "obtained_at": 1700000000, "expires_at": None}]
    monkeypatch.setattr(rec.CONNECTORS, "service_status", lambda: status)
    page = rec._setup_page("abc")
    assert "✓ Connected" in page and "Reconnect" not in page      # healthy: no error file
    os.makedirs(os.path.join(str(tmp_path), "connectors"), exist_ok=True)
    with open(os.path.join(str(tmp_path), "connectors", "granola.error"), "w") as f:
        f.write("401 from mcp.granola.ai")
    page = rec._setup_page("abc")
    assert "⚠️ Reconnect" in page and "/connect/granola/start?code=abc" in page
    assert "✓ Connected" not in page
    assert "401 from mcp.granola.ai" in page                      # the gather's message surfaces


def test_connector_expired_without_refresh_downgrades(tmp_path, monkeypatch):
    """expires_at in the past does NOT always mean dead — only downgrade when there's also no
    refresh token in the token file to fall back on."""
    rec.DATA = str(tmp_path)
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "x.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    monkeypatch.setattr(rec, "setup_status", lambda: {
        "bridge_connected": False, "google_connected": False, "google_detail": "nope",
        "google_client_present": False, "timezone": "", "whatsapp": "unknown"})
    status = [{"service": "granola", "label": "Granola (meeting notes)", "connected": True,
               "obtained_at": 1700000000, "expires_at": 1700000001}]   # long expired
    monkeypatch.setattr(rec.CONNECTORS, "service_status", lambda: status)
    tok_path = os.path.join(str(tmp_path), "connectors", "granola.json")
    monkeypatch.setattr(rec.CONNECTORS, "token_path", lambda s: tok_path)
    os.makedirs(os.path.dirname(tok_path), exist_ok=True)
    with open(tok_path, "w") as f:
        json.dump({"access_token": "a", "refresh_token": "r"}, f)
    assert "✓ Connected" in rec._setup_page("abc")                # expired but refreshable → still ✓
    with open(tok_path, "w") as f:
        json.dump({"access_token": "a", "refresh_token": None}, f)
    assert "⚠️ Reconnect" in rec._setup_page("abc")               # expired AND no refresh → downgrade


def test_google_connected_is_memoized(monkeypatch):
    """/setup polls must not fork `setup.py --check` on every GET — the result is cached ~20s."""
    calls = []
    monkeypatch.setattr(rec, "_google_connected_uncached", lambda: (calls.append(1), (False, "x"))[1])
    rec._GOOGLE_CHECK_CACHE = (0.0, None)                         # reset any earlier memo
    assert rec.google_connected() == (False, "x")
    assert rec.google_connected() == (False, "x")
    assert len(calls) == 1                                        # second hit served from the memo
    rec._GOOGLE_CHECK_CACHE = (0.0, None)


def test_root_page_and_favicon(tmp_path):
    """GET / renders an unauthenticated 'Sotto is running' page that points at the deploy-logs setup
    link WITHOUT leaking the code; /favicon.ico answers 204; other unknown paths keep the JSON 404."""
    import importlib.util as _il
    import threading
    import urllib.error as _ue
    import urllib.request as _u
    from http.server import ThreadingHTTPServer

    spec2 = _il.spec_from_file_location("receiver_root", os.path.join(HERE, "receiver.py"))
    r2 = _il.module_from_spec(spec2)
    spec2.loader.exec_module(r2)
    r2.DATA = str(tmp_path)
    r2.SETUP_CODE = "sekrit-root-1"
    r2.MCP_TOKEN = "tok"
    r2.TOKEN = "tok"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), r2.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with _u.urlopen(base + "/", timeout=10) as resp:
            body = resp.read().decode()
            assert resp.status == 200 and "Sotto is running" in body
            assert "Setup link" in body and "sekrit-root-1" not in body   # points, never leaks
        with _u.urlopen(base + "/favicon.ico", timeout=10) as resp:
            assert resp.status == 204 and resp.read() == b""
        try:
            _u.urlopen(base + "/nope", timeout=10)
            assert False, "expected 404"
        except _ue.HTTPError as e:
            assert e.code == 404 and json.loads(e.read())["error"] == "not found"
    finally:
        srv.shutdown()


def test_find_sotto_script_falls_back_to_repo_tree():
    """In a source checkout (tests, CI) the triage scripts resolve via the repo-relative path."""
    p = rec._find_sotto_script("event-triage", "scripts", "triage_event.py")
    assert p is not None and p.endswith(os.path.join("event-triage", "scripts", "triage_event.py"))
    assert os.path.exists(p)
    assert rec._find_sotto_script("event-triage", "scripts", "no_such_script.py") is None
