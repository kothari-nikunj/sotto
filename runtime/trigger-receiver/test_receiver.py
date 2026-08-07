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
    # deterministic regardless of a `hermes` binary on PATH: the live config-set fails cleanly
    def no_hermes(*a, **kw):
        raise FileNotFoundError("hermes missing")
    monkeypatch.setattr(rec.subprocess, "run", no_hermes)
    # rejects junk / bare offsets (we want a real IANA zone for DST correctness)
    assert rec.set_timezone("")[0] is False
    assert rec.set_timezone("Mars/Phobos zzz")[0] is False
    assert rec.set_timezone("+05:30")[0] is False          # no '/', not IANA
    ok, val = rec.set_timezone("America/Los_Angeles")
    assert ok and val == "America/Los_Angeles"
    assert rec.read_settings()["timezone"] == "America/Los_Angeles"


class _CronCLI:
    """Records every `hermes …` invocation; returncode configurable per-prefix."""
    def __init__(self, config_rc=0):
        self.calls = []
        self.config_rc = config_rc

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        class R:
            stdout = ""
            stderr = ""
        R.returncode = self.config_rc if cmd[:3] == ["hermes", "config", "set"] else 0
        return R()

    def cron(self, verb):
        return [c for c in self.calls if c[:3] == ["hermes", "cron", verb]]


def test_set_timezone_reregisters_crons_on_change(tmp_path, monkeypatch):
    """Root fix for first-night UTC briefs: boot registered the crons under UTC; when the wizard's
    tz lands (config set succeeds, zone changed), every sotto cron is re-registered — removed by
    stable --name and recreated with EXACTLY start.sh's schedule/skill/deliver — under the new zone."""
    monkeypatch.setattr(rec, "SETTINGS_FILE", os.path.join(str(tmp_path), "config", "settings.json"))
    for k in ("SOTTO_TIMEZONE", "SOTTO_PROACTIVE", "SOTTO_DIGEST", "SOTTO_PROACTIVE_CRON",
              "SOTTO_CRON_DELIVER"):
        monkeypatch.delenv(k, raising=False)
    cli = _CronCLI()
    monkeypatch.setattr(rec.subprocess, "run", cli)
    ok, val = rec.set_timezone("America/Los_Angeles")
    assert ok and val == "America/Los_Angeles"
    assert ["hermes", "config", "set", "timezone", "America/Los_Angeles"] in cli.calls
    names = {"sotto-morning-brief", "sotto-evening-brief", "sotto-relationship-pulse",
             "sotto-proactive", "sotto-midday-digest"}
    assert {c[3] for c in cli.cron("remove")} == names
    creates = {c[c.index("--name") + 1]: c for c in cli.cron("create")}
    assert set(creates) == names
    # schedules + skills mirror start.sh step 3 exactly; deliver defaults to whatsapp
    assert creates["sotto-morning-brief"][3] == "30 6 * * *"
    assert creates["sotto-evening-brief"][3] == "30 17 * * *"
    assert creates["sotto-relationship-pulse"][3] == "0 9 * * 1"
    assert creates["sotto-proactive"][3] == "*/15 * * * *"
    assert creates["sotto-midday-digest"][3] == "30 12 * * *"
    assert creates["sotto-midday-digest"][creates["sotto-midday-digest"].index("--skill") + 1] == "sotto-event"
    for c in creates.values():
        assert c[c.index("--deliver") + 1] == "whatsapp"
    # every remove precedes its create (never leave a duplicate pair behind)
    assert cli.calls.index(cli.cron("remove")[0]) < cli.calls.index(cli.cron("create")[0])


def test_set_timezone_skips_cron_rereg_when_unchanged(tmp_path, monkeypatch):
    """Same zone as the crons were registered under → config set still runs, but NO cron churn."""
    monkeypatch.setattr(rec, "SETTINGS_FILE", os.path.join(str(tmp_path), "config", "settings.json"))
    monkeypatch.delenv("SOTTO_TIMEZONE", raising=False)
    rec.write_setting("timezone", "Europe/Paris")     # boot registered under this zone
    cli = _CronCLI()
    monkeypatch.setattr(rec.subprocess, "run", cli)
    ok, _ = rec.set_timezone("Europe/Paris")
    assert ok
    assert ["hermes", "config", "set", "timezone", "Europe/Paris"] in cli.calls
    assert cli.cron("remove") == [] and cli.cron("create") == []


def test_set_timezone_no_cron_rereg_when_config_set_fails(tmp_path, monkeypatch):
    """If `hermes config set timezone` fails, a recreate would only re-land the OLD zone — so the
    re-registration is skipped and the boot registration stays (self-heals on the next boot). The
    settings write still succeeds (compose_brief/brief_marker read the file, not hermes config)."""
    monkeypatch.setattr(rec, "SETTINGS_FILE", os.path.join(str(tmp_path), "config", "settings.json"))
    monkeypatch.delenv("SOTTO_TIMEZONE", raising=False)
    cli = _CronCLI(config_rc=1)
    monkeypatch.setattr(rec.subprocess, "run", cli)
    ok, _ = rec.set_timezone("America/New_York")
    assert ok and rec.read_settings()["timezone"] == "America/New_York"
    assert cli.cron("remove") == [] and cli.cron("create") == []


def test_sotto_cron_jobs_honor_env_gates(monkeypatch):
    """SOTTO_PROACTIVE=0 / SOTTO_DIGEST=0 drop those jobs (mirroring start.sh's gates), and
    SOTTO_PROACTIVE_CRON overrides the watcher's schedule."""
    monkeypatch.setenv("SOTTO_PROACTIVE", "0")
    monkeypatch.setenv("SOTTO_DIGEST", "0")
    assert {j[0] for j in rec._sotto_cron_jobs()} == {
        "sotto-morning-brief", "sotto-evening-brief", "sotto-relationship-pulse"}
    monkeypatch.setenv("SOTTO_PROACTIVE", "1")
    monkeypatch.setenv("SOTTO_PROACTIVE_CRON", "*/30 * * * *")
    jobs = {j[0]: j for j in rec._sotto_cron_jobs()}
    assert jobs["sotto-proactive"][1] == "*/30 * * * *"


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
    assert "What Sotto connects to" in page and "sotto-bridge://pair?" in page
    assert "Link your Mac" in page and "Connect Google" in page
    assert "Timezone" in page


def test_setup_page_shares_the_app_shell(monkeypatch):
    """/setup is the Integrations view of the SAME site as /app: it links the dashboard stylesheet
    first, then the setup layer, renders the shared sidebar nav (with the /app section links and
    Integrations marked current), and carries no inline <style> blocks or style= attributes."""
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
    for dest in ("/app#today", "/app#loops", "/app#briefs", "/app#people", "/app#learned",
                 "/app#record"):
        assert f"href='{dest}'" in page, dest
    assert "href='/setup' class='active' aria-current='page'>Integrations</a>" in page
    assert "class='content setup-page'" in page
    assert "class='eyebrow'" in page and "class='page-title'" in page and "class='page-sub'" in page
    # five tiles in contract markup; no leftover inline styling
    assert page.count("<section class='tile'") == 5
    assert page.count("class='tile-head'") == 5 and page.count("class='tile-body'") == 5
    assert "<style>" not in page and " style='" not in page and ' style="' not in page


def test_setup_page_pairing_not_ready_without_domain_or_token(monkeypatch):
    """With no public domain or no bearer token, the Mac tile must NOT render a dead
    sotto-bridge://pair?host=&token= link — it names what's missing (and points at RAILWAY.md)
    instead of handing out a pairing code that can't pair."""
    st = {"bridge_connected": False, "google_connected": False, "google_detail": "nope",
          "google_client_present": False, "timezone": "", "whatsapp": "unknown"}
    monkeypatch.setattr(rec, "setup_status", lambda: dict(st))
    # domain missing, token present → only the domain half is named
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    page = rec._setup_page()
    assert "sotto-bridge://pair" not in page
    assert "Pairing isn't ready" in page and "RAILWAY.md" in page
    assert "public domain" in page and "BRIDGE_TOKEN" not in page
    # token missing, domain present → BRIDGE_TOKEN is named
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "")
    page = rec._setup_page()
    assert "sotto-bridge://pair" not in page and "BRIDGE_TOKEN" in page
    # both missing → both named
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "")
    page = rec._setup_page()
    assert "BRIDGE_TOKEN" in page and "public domain" in page
    # both present → the real pairing link is back, no warning
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    page = rec._setup_page()
    assert "sotto-bridge://pair?" in page and "Pairing isn't ready" not in page


def test_setup_page_hero_cta_only_when_steps_1_to_4_done(monkeypatch):
    """The wizard→app handoff: .hero-cta (and the connected footer) render iff Mac + Google + timezone are
    done and WhatsApp is POSITIVELY linked — never over a never-scanned ("unknown") or mid-pairing
    WhatsApp. Tile 5 (optional services) never gates it."""
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    st = {"bridge_connected": True, "google_connected": True, "google_detail": "ok",
          "google_client_present": True, "timezone": "America/Los_Angeles", "whatsapp": "linked"}
    monkeypatch.setattr(rec, "setup_status", lambda: dict(st))
    page = rec._setup_page("abc")
    assert "class='hero-cta' href='/app'" in page and "Open your dashboard" in page
    assert "You're connected" in page
    # WhatsApp never linked ("unknown" — no session creds on disk) → NO celebration
    monkeypatch.setattr(rec, "setup_status", lambda: dict(st, whatsapp="unknown"))
    page = rec._setup_page("abc")
    assert "hero-cta" not in page and "You're connected" not in page
    # WhatsApp mid-pairing → no handoff yet
    monkeypatch.setattr(rec, "setup_status", lambda: dict(st, whatsapp="pairing"))
    assert "hero-cta" not in rec._setup_page("abc")
    # any of steps 1-3 missing → no handoff either
    monkeypatch.setattr(rec, "setup_status", lambda: dict(st, google_connected=False))
    assert "hero-cta" not in rec._setup_page("abc")
    monkeypatch.setattr(rec, "setup_status", lambda: dict(st, timezone=""))
    assert "hero-cta" not in rec._setup_page("abc")


def test_whatsapp_status_positive_probe(tmp_path, monkeypatch):
    """_whatsapp_status has a POSITIVE linked probe: the gateway session creds.json (the exact file
    start.sh gates pairing on, under $SOTTO_DATA/hermes since ~/.hermes symlinks there) → "linked";
    a live QR mirror alone → "pairing"; neither → "unknown"."""
    rec.DATA = str(tmp_path)
    monkeypatch.setattr(rec, "QR_FILE", os.path.join(str(tmp_path), "whatsapp-pairing.txt"))
    # keep the probe inside tmp_path — a dev Mac's real ~/.hermes must not leak into the test
    creds = os.path.join(str(tmp_path), "hermes", "platforms", "whatsapp", "session", "creds.json")
    monkeypatch.setattr(rec, "_wa_creds_paths", lambda: [creds])
    assert rec._whatsapp_status() == "unknown"
    open(rec.QR_FILE, "w").close()
    assert rec._whatsapp_status() == "pairing"
    os.makedirs(os.path.dirname(creds), exist_ok=True)
    open(creds, "w").close()
    assert rec._whatsapp_status() == "linked"     # creds win over a lingering QR mirror
    os.remove(rec.QR_FILE)
    assert rec._whatsapp_status() == "linked"


def test_setup_page_whatsapp_tile_states(monkeypatch):
    """Tile 3 turns "done" with "WhatsApp is linked" only on the positive probe; "unknown" keeps the
    QR button and the 'to do' state (it must not read as finished)."""
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    st = {"bridge_connected": False, "google_connected": False, "google_detail": "nope",
          "google_client_present": False, "timezone": "", "whatsapp": "linked"}
    monkeypatch.setattr(rec, "setup_status", lambda: dict(st))
    page = rec._setup_page("abc")
    assert "WhatsApp is linked" in page
    assert page.count("data-state='done'") == 1   # only the WhatsApp tile (everything else is todo)
    monkeypatch.setattr(rec, "setup_status", lambda: dict(st, whatsapp="unknown"))
    page = rec._setup_page("abc")
    assert "WhatsApp is linked" not in page and "Show WhatsApp QR" in page
    assert page.count("data-state='done'") == 0


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
    monkeypatch.setenv("SOTTO_USER_EMAIL", TAP_SELF["email"])
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
    token with no refresh token) turns the tile into Reconnect pointing at the same start URL."""
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
    assert "connected since" in page and "Reconnect" not in page   # healthy: no error file
    os.makedirs(os.path.join(str(tmp_path), "connectors"), exist_ok=True)
    with open(os.path.join(str(tmp_path), "connectors", "granola.error"), "w") as f:
        f.write("401 from mcp.granola.ai")
    page = rec._setup_page("abc")
    assert "Reconnect →" in page and "/connect/granola/start?code=abc" in page
    assert "connected since" not in page
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
    assert "connected since" in rec._setup_page("abc")            # expired but refreshable → still connected
    with open(tok_path, "w") as f:
        json.dump({"access_token": "a", "refresh_token": None}, f)
    assert "Reconnect →" in rec._setup_page("abc")                # expired AND no refresh → downgrade


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


# ── Deferred-queue release valve (the */15 heartbeat that lets held nudges out) ──────────────────

def test_valve_tick_stages_bundle_and_spawns_like_a_fresh_agent_verdict(tmp_path, monkeypatch):
    """An agent verdict from the valve rides the IDENTICAL path handle_events takes: bundle staged
    under $SOTTO_DATA/events/, sotto-event spawned with that path."""
    rec.DATA = str(tmp_path)
    bundle = {"promoted": True, "events": [{"sender": "Sarah Chen", "class": "urgent"}]}
    monkeypatch.setattr(rec, "_whatsapp_status", lambda: "linked")
    monkeypatch.setattr(rec, "run_valve",
                        lambda: {"verdict": "agent", "reason": "promoted", "bundle": bundle})
    spawned = []
    monkeypatch.setattr(rec, "run_event_skill", lambda p: spawned.append(p))
    rec._valve_tick()
    deadline = rec.time.time() + 5                      # spawn happens on a daemon thread
    while not spawned and rec.time.time() < deadline:
        rec.time.sleep(0.01)
    assert len(spawned) == 1
    assert os.path.dirname(spawned[0]) == os.path.join(str(tmp_path), "events")
    assert os.path.basename(spawned[0]).startswith("bundle-")
    assert json.load(open(spawned[0])) == bundle


def test_valve_tick_checks_channel_health_before_spending_a_promotion(tmp_path, monkeypatch):
    """No positive WhatsApp probe → the valve is never even run (a promotion must not be burned on
    an undeliverable nudge)."""
    rec.DATA = str(tmp_path)
    ran = []
    monkeypatch.setattr(rec, "run_valve", lambda: (ran.append(1), {"verdict": "drop"})[1])
    for status in ("unknown", "pairing"):
        monkeypatch.setattr(rec, "_whatsapp_status", lambda s=status: s)
        rec._valve_tick()
    assert ran == []
    monkeypatch.setattr(rec, "_whatsapp_status", lambda: "linked")
    rec._valve_tick()
    assert ran == [1]


def test_delivery_gate_applies_only_to_the_whatsapp_delivery_channel(tmp_path, monkeypatch, capsys):
    """The gate asks "can this be delivered?", not "is WhatsApp linked?". On a non-WhatsApp
    SOTTO_CRON_DELIVER there is nothing to probe, so the valve and the tap must not be silently
    dead forever. And a shut gate logs on the state CHANGE, not on every tick."""
    rec.DATA = str(tmp_path)
    rec._DELIVERY_GATE_STATE.clear()
    monkeypatch.setattr(rec, "_whatsapp_status", lambda: "unknown")
    monkeypatch.setenv("SOTTO_CRON_DELIVER", "telegram")
    assert rec._delivery_channel_ready("valve") is True       # other channel → no WhatsApp gate
    monkeypatch.setenv("SOTTO_CRON_DELIVER", "whatsapp")
    assert rec._delivery_channel_ready("valve") is False
    assert rec._delivery_channel_ready("valve") is False
    assert capsys.readouterr().out.count("whatsapp not linked") == 1   # once, not per tick
    monkeypatch.setattr(rec, "_whatsapp_status", lambda: "linked")
    assert rec._delivery_channel_ready("valve") is True
    monkeypatch.delenv("SOTTO_CRON_DELIVER")                  # unset ⇒ whatsapp (the default)
    monkeypatch.setattr(rec, "_whatsapp_status", lambda: "pairing")
    assert rec._delivery_channel_ready("valve") is False


def test_valve_and_tap_run_when_delivery_is_not_whatsapp(tmp_path, monkeypatch):
    """The user-visible half of the same rule: a Telegram/local user still gets promotions + taps."""
    rec.DATA = str(tmp_path)
    rec._DELIVERY_GATE_STATE.clear()
    monkeypatch.setenv("SOTTO_CRON_DELIVER", "local")
    monkeypatch.setattr(rec, "_whatsapp_status", lambda: "unknown")
    ran = []
    monkeypatch.setattr(rec, "run_valve", lambda: (ran.append("valve"), {"verdict": "drop"})[1])
    monkeypatch.setattr(rec, "run_triage",
                        lambda evs, catchup: (ran.append("tap"), {"verdict": "queue"})[1])
    rec._valve_tick()
    assert rec._dispatch_meeting_tap({"source": "meeting_end"}) is True
    assert ran == ["valve", "tap"]


def test_valve_tick_non_agent_and_failure_never_spawn(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    monkeypatch.setattr(rec, "_whatsapp_status", lambda: "linked")
    spawned = []
    monkeypatch.setattr(rec, "run_event_skill", lambda p: spawned.append(p))
    monkeypatch.setattr(rec, "run_valve",
                        lambda: {"verdict": "drop", "reason": "nothing promotable", "bundle": {}})
    rec._valve_tick()

    def boom():
        raise RuntimeError("valve script missing")
    monkeypatch.setattr(rec, "run_valve", boom)
    rec._valve_tick()                                    # logged, never raises
    rec.time.sleep(0.05)
    assert spawned == []


def test_valve_thread_disabled_by_knob_and_interval(monkeypatch):
    monkeypatch.setenv("SOTTO_VALVE", "0")
    assert rec.start_valve_thread() is None              # SOTTO_VALVE=0 disables
    monkeypatch.delenv("SOTTO_VALVE", raising=False)
    monkeypatch.setenv("SOTTO_VALVE_INTERVAL_SECS", "0")
    assert rec.start_valve_thread() is None              # non-positive interval disables
    monkeypatch.setenv("SOTTO_VALVE_INTERVAL_SECS", "junk")
    assert rec._valve_secs() == rec.VALVE_INTERVAL_SECS_DEFAULT


def test_run_valve_raises_when_script_missing(monkeypatch):
    """A broken valve must RAISE (so the tick logs it), never masquerade as an empty queue."""
    monkeypatch.setattr(rec, "_find_sotto_script", lambda *a: None)
    try:
        rec.run_valve()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "not found" in str(e)


# ── The shared calendar cache (Editor Step 2 item 2) ─────────────────────────────────────────────
# The gather + TTL live in calcache.py; the receiver owns the WIRING (one cache, two consumers) and
# the refresh thread. Endpoint behavior and the cache mechanics are covered in test_dashboard.py.

def test_calendar_cache_is_wired_as_the_dashboard_s_only_calendar_source():
    """/api/calendar must read the SAME module the refresh thread writes from — the amendment's
    "two competing caches is how drift starts" is a WIRING property, so assert the wiring."""
    calls = []
    rec.DASHBOARD.HOOKS["calendar_snapshot"] = lambda: (calls.append(1), None)[1]
    assert rec.DASHBOARD.api_calendar() == {"events": [], "unavailable": True}
    assert calls == [1]                                  # the endpoint owns no gather of its own
    rec.DASHBOARD.HOOKS["calendar_snapshot"] = lambda: rec.CALCACHE.snapshot()
    # dashboard.py no longer carries any calendar-gather machinery at all
    for gone in ("_CAL_CACHE", "_run_calendar_gather", "_norm_cal_event", "CALENDAR_TTL_SECS"):
        assert not hasattr(rec.DASHBOARD, gone), f"dashboard.py still owns {gone}"


def test_calendar_cache_hooks_resolve_to_the_receiver_s_own_state(tmp_path, monkeypatch):
    """data_root / find_script / local_today are late-bound over the receiver's globals, so a
    monkeypatched DATA or _find_sotto_script is seen by the cache too — and local_today is the
    dashboard's ONE tz resolution (the ROADMAP's first-night-timezone amendment), not a copy."""
    monkeypatch.setattr(rec, "DATA", str(tmp_path))
    monkeypatch.setattr(rec, "_find_sotto_script", lambda *rel: "/nope/" + rel[-1])
    assert rec.CALCACHE.HOOKS["data_root"]() == str(tmp_path)
    assert rec.CALCACHE.HOOKS["find_script"]("_shared", "scripts", "x.py") == "/nope/x.py"
    assert rec.CALCACHE.HOOKS["local_today"]() == rec.DASHBOARD._local_today()
    assert rec.CALCACHE.cache_path() == os.path.join(str(tmp_path), "cache",
                                                     "calendar_today.json")


def test_calendar_refresh_thread_knob_and_quiet_idle(tmp_path, monkeypatch):
    """SOTTO_CALENDAR_REFRESH_SECS=0 disables the thread; with no skills tree on the box a tick is
    a silent no-op (no file, no raise) — the hold then reads "no cache" and simply never engages."""
    monkeypatch.setenv("SOTTO_CALENDAR_REFRESH_SECS", "0")
    assert rec.CALCACHE.start_refresh_thread() is None
    monkeypatch.setenv("SOTTO_CALENDAR_REFRESH_SECS", "junk")
    assert rec.CALCACHE.refresh_secs() == rec.CALCACHE.REFRESH_SECS_DEFAULT
    monkeypatch.setattr(rec, "DATA", str(tmp_path))
    monkeypatch.setattr(rec, "_find_sotto_script", lambda *rel: None)
    rec.CALCACHE._CAL_CACHE.update({"ts": 0.0, "value": None})
    assert rec.CALCACHE.refresh_once() is False
    assert not os.path.exists(rec.CALCACHE.cache_path())


# ── The post-meeting tap (Editor Step 2 item 3) ──────────────────────────────────────────────────
# calcache.py owns DETECTION (which meetings ended since the last tick, exactly once, capped); the
# receiver owns the one relay step — hand the synthetic event to the same triage funnel every other
# nudge goes through. Discipline (budget/quiet/snooze/in-meeting hold) is tested in
# sotto-chief-of-staff/tests/test_event_triage.py, where it lives.

import datetime as _dt

CAL = None  # bound per test to rec.CALCACHE for brevity
TAP_SELF = {"name": "You", "email": "me@acme.com"}
TAP_TODAY = "2026-08-06"
TAP_NOW = _dt.datetime(2026, 8, 6, 10, 6, tzinfo=_dt.timezone.utc)   # 6 min after a 10:00Z end


def _cal_ev(summary="Product sync", start="2026-08-06T09:00:00+00:00",
            end="2026-08-06T10:00:00+00:00", others=(("Sarah Chen", "sarah@acme.com"),),
            with_self=True):
    att = ([dict(TAP_SELF)] if with_self else [])
    att += [{"name": n, "email": e} for n, e in others]
    return {"summary": summary, "start": start, "end": end, "attendees": att}


def _tap_env(tmp_path, monkeypatch, events, **env):
    """Point calcache at a tmp data root, a fixed local date, and a pre-warmed snapshot (so no
    gather_google fork happens), and collect every dispatched tap."""
    monkeypatch.setattr(rec, "DATA", str(tmp_path))
    monkeypatch.setitem(rec.CALCACHE.HOOKS, "local_today", lambda: TAP_TODAY)
    monkeypatch.setattr(rec.CALCACHE, "_CAL_CACHE",
                        {"ts": rec.time.time(), "value": {"events": list(events),
                                                          "generated_at": "2026-08-06T10:05:00Z"}})
    monkeypatch.setenv("SOTTO_USER_EMAIL", TAP_SELF["email"])
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    taps = []
    monkeypatch.setitem(rec.CALCACHE.HOOKS, "meeting_tap",
                        lambda ev: (taps.append(ev), True)[1])
    return taps


def _tap_state(tmp_path):
    p = os.path.join(str(tmp_path), "cache", "meeting_taps.json")
    return json.load(open(p)) if os.path.exists(p) else None


def test_meeting_tap_fires_exactly_once_per_event_end(tmp_path, monkeypatch):
    """The defining property: a meeting end fires ONE tap ever. The date-keyed state file is what
    guarantees it across ticks (and across a receiver restart)."""
    taps = _tap_env(tmp_path, monkeypatch, [_cal_ev()])
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 1
    assert len(taps) == 1
    ev = taps[0]
    assert ev["source"] == rec.CALCACHE.MEETING_END_SOURCE == "meeting_end"
    assert ev["summary"] == "Product sync" and ev["timestamp"] == ev["end"]
    assert ev["attendees"] == [{"name": "Sarah Chen", "email": "sarah@acme.com"}]  # self dropped
    st = _tap_state(tmp_path)
    assert st["date"] == TAP_TODAY and len(st["fired"]) == 1
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 0        # same tick data, no second tap
    assert len(taps) == 1


def test_meeting_tap_drops_the_user_from_their_own_meeting(tmp_path, monkeypatch):
    """Who counts as an "other" attendee is the docket's rule, with SOTTO_USER_EMAIL as the first
    source (a light day has too few peopled events to infer from). Both paths must leave the user
    out of the nudge's "with …" line."""
    events = [_cal_ev(), _cal_ev(summary="Design review", end="2026-08-06T09:58:00+00:00",
                                 others=(("Ben Butler", "ben@other.com"),))]
    taps = _tap_env(tmp_path, monkeypatch, events)
    monkeypatch.delenv("SOTTO_USER_EMAIL")                  # fall back to the docket's inference
    assert rec.CALCACHE._self_email(events) == "me@acme.com"
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 2
    assert [[a["name"] for a in t["attendees"]] for t in taps] == [["Ben Butler"], ["Sarah Chen"]]


def test_meeting_tap_waits_out_the_grace_and_expires_after_the_lookback(tmp_path, monkeypatch):
    """Five minutes of grace so the user is actually out of the room; and a receiver that boots
    hours later must not replay the whole day as a burst of nudges."""
    taps = _tap_env(tmp_path, monkeypatch, [_cal_ev()])
    early = _dt.datetime(2026, 8, 6, 10, 2, tzinfo=_dt.timezone.utc)   # 2 min after the end
    assert rec.CALCACHE.tap_tick(now_utc=early) == 0
    during = _dt.datetime(2026, 8, 6, 9, 30, tzinfo=_dt.timezone.utc)  # still in it
    assert rec.CALCACHE.tap_tick(now_utc=during) == 0
    late = _dt.datetime(2026, 8, 6, 13, 0, tzinfo=_dt.timezone.utc)    # 3h later, outside lookback
    assert rec.CALCACHE.tap_tick(now_utc=late) == 0
    assert taps == []
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 1                 # the tick that owns it
    assert len(taps) == 1


def test_meeting_tap_skips_solo_blocks_all_day_events_and_naive_times(tmp_path, monkeypatch):
    """A focus block is not a meeting, an all-day event is a label on the day, and an offset-less
    time can't be dated without a second tz resolution — the tap is a delight, so it stays silent
    rather than guessing (the in-meeting hold, which must never MISS, keeps its naive branch)."""
    taps = _tap_env(tmp_path, monkeypatch, [
        _cal_ev(summary="Focus block", others=()),                       # no other humans
        _cal_ev(summary="Company holiday", start="2026-08-06", end="2026-08-07"),
        _cal_ev(summary="Naive sync", start="2026-08-06T09:00:00", end="2026-08-06T10:00:00"),
        _cal_ev(summary="Yesterday's sync", start="2026-08-05T09:00:00+00:00",
                end="2026-08-05T10:00:00+00:00"),
    ])
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 0
    assert taps == []
    assert _tap_state(tmp_path) is None                    # nothing detected ⇒ nothing recorded


def test_meeting_tap_is_capped_per_day_so_it_cannot_eat_the_interrupt_budget(tmp_path, monkeypatch):
    """A nine-meeting day must not spend the whole day's nudges on taps: SOTTO_TAP_MAX_PER_DAY
    (default 3) is the tap-specific cap the shared budget can't express."""
    assert rec.CALCACHE.tap_max_per_day() == 3              # the documented default
    ends = ["2026-08-06T09:5%d:00+00:00" % i for i in range(4)]
    events = [_cal_ev(summary=f"Sync {i}", end=e, start="2026-08-06T09:00:00+00:00")
              for i, e in enumerate(ends)]
    taps = _tap_env(tmp_path, monkeypatch, events, SOTTO_TAP_MAX_PER_DAY="2")
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 2
    assert [t["summary"] for t in taps] == ["Sync 0", "Sync 1"]   # oldest end first
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 0            # cap holds on the next tick too
    assert len(_tap_state(tmp_path)["fired"]) == 2
    monkeypatch.setenv("SOTTO_TAP_MAX_PER_DAY", "0")
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 0            # 0 = taps off entirely


def test_meeting_tap_cap_and_record_reset_on_the_local_day_rollover(tmp_path, monkeypatch):
    """The state file is date-keyed, so the day's cap resets at local midnight with no cleanup
    job — the same rollover trick the interrupt budget uses."""
    taps = _tap_env(tmp_path, monkeypatch, [_cal_ev()], SOTTO_TAP_MAX_PER_DAY="1")
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 1
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 0
    monkeypatch.setitem(rec.CALCACHE.HOOKS, "local_today", lambda: "2026-08-07")
    events = [_cal_ev(start="2026-08-07T09:00:00+00:00", end="2026-08-07T10:00:00+00:00")]
    monkeypatch.setattr(rec.CALCACHE, "_CAL_CACHE",
                        {"ts": rec.time.time(), "value": {"events": events, "generated_at": "x"}})
    assert rec.CALCACHE.tap_tick(now_utc=_dt.datetime(2026, 8, 7, 10, 6, tzinfo=_dt.timezone.utc)) == 1
    assert len(taps) == 2 and _tap_state(tmp_path)["date"] == "2026-08-07"


def test_meeting_tap_retries_when_the_dispatch_did_not_take(tmp_path, monkeypatch):
    """A hook that returns False (triage unavailable, channel unhealthy) must NOT mark the end
    handled — the next tick inside the window tries again. A raising hook is the same, and never
    escapes into the refresh thread."""
    _tap_env(tmp_path, monkeypatch, [_cal_ev()])
    monkeypatch.setitem(rec.CALCACHE.HOOKS, "meeting_tap", lambda ev: False)
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 0
    assert _tap_state(tmp_path) is None

    def boom(ev):
        raise RuntimeError("triage down")
    monkeypatch.setitem(rec.CALCACHE.HOOKS, "meeting_tap", boom)
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 0
    landed = []
    monkeypatch.setitem(rec.CALCACHE.HOOKS, "meeting_tap", lambda ev: (landed.append(ev), True)[1])
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 1
    assert len(landed) == 1


def test_meeting_tap_skips_internal_only_standups(tmp_path, monkeypatch):
    """The one meeting class nobody wants a follow-up draft for. Cheap test: standup-shaped title ×
    every attendee in the user's own domain (recurrence isn't in the cache shape). Anything with an
    outside guest, or any other title, still taps."""
    internal = _cal_ev(summary="Eng standup", others=(("Dhruv Patel", "dhruv@acme.com"),))
    external = _cal_ev(summary="Partner standup", end="2026-08-06T09:58:00+00:00",
                       others=(("Ben Butler", "ben@other.com"),))
    other = _cal_ev(summary="Product sync", end="2026-08-06T09:56:00+00:00")
    taps = _tap_env(tmp_path, monkeypatch, [internal, external, other])
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 2
    assert sorted(t["summary"] for t in taps) == ["Partner standup", "Product sync"]
    monkeypatch.setenv("SOTTO_TAP_SKIP_INTERNAL", "0")       # the refinement is a knob, not a law
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 1
    assert taps[-1]["summary"] == "Eng standup"


def test_meeting_tap_disabled_by_knob_and_absent_skills_tree(tmp_path, monkeypatch):
    taps = _tap_env(tmp_path, monkeypatch, [_cal_ev()], SOTTO_MEETING_TAP="0")
    assert rec.CALCACHE.taps_enabled() is False
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 0
    monkeypatch.delenv("SOTTO_MEETING_TAP")
    monkeypatch.setattr(rec.CALCACHE, "snapshot", lambda: None)   # no skills tree on this box
    assert rec.CALCACHE.tap_tick(now_utc=TAP_NOW) == 0
    assert taps == []


def test_meeting_tap_is_wired_to_the_receiver_and_rides_the_refresh_thread(tmp_path, monkeypatch):
    """The wiring claims: the hook is the receiver's dispatcher, and the tap runs on the SAME tick
    as the calendar refresh (one clock, one calendar — no second thread)."""
    seen = []
    monkeypatch.setattr(rec, "_dispatch_meeting_tap", lambda ev: (seen.append(ev), True)[1])
    assert rec.CALCACHE.HOOKS["meeting_tap"]({"source": "probe"}) is True   # late-bound, like the rest
    assert seen == [{"source": "probe"}]
    ticks = []
    monkeypatch.setattr(rec.CALCACHE, "refresh_once", lambda: ticks.append("refresh"))
    monkeypatch.setattr(rec.CALCACHE, "tap_tick", lambda: ticks.append("tap"))
    monkeypatch.setenv("SOTTO_CALENDAR_REFRESH_SECS", "900")
    t = rec.CALCACHE.start_refresh_thread()
    assert t is not None
    deadline = rec.time.time() + 5
    while len(ticks) < 2 and rec.time.time() < deadline:
        rec.time.sleep(0.01)
    assert ticks == ["refresh", "tap"]                      # refresh first, tap on fresh data

    def boom():
        raise RuntimeError("gather exploded")
    monkeypatch.setattr(rec.CALCACHE, "refresh_once", boom)  # a broken half can't take the other down
    ticks.clear()
    t2 = rec.CALCACHE.start_refresh_thread()
    deadline = rec.time.time() + 5
    while not ticks and rec.time.time() < deadline:
        rec.time.sleep(0.01)
    assert ticks == ["tap"] and t2.is_alive()


def test_dispatch_meeting_tap_runs_the_ordinary_funnel_and_spawns_on_agent(tmp_path, monkeypatch):
    """The relay: one synthetic event through run_triage (catchup False), and an agent verdict
    rides the IDENTICAL stage-bundle → sotto-event spawn path a fresh event takes."""
    rec.DATA = str(tmp_path)
    monkeypatch.setattr(rec, "_whatsapp_status", lambda: "linked")
    seen, bundle = [], {"events": [{"sender": "Sarah Chen", "class": "post_meeting"}]}
    monkeypatch.setattr(rec, "run_triage", lambda evs, c: (seen.append((evs, c)),
                        {"verdict": "agent", "reason": "wrapped", "bundle": bundle})[1])
    spawned = []
    monkeypatch.setattr(rec, "run_event_skill", lambda p: spawned.append(p))
    ev = rec.CALCACHE.tap_event({"key": "k", "summary": "Product sync", "start": "s", "end": "e",
                                 "attendees": [{"name": "Sarah Chen", "email": "s@acme.com"}]})
    assert rec._dispatch_meeting_tap(ev) is True
    assert seen[0][1] is False and seen[0][0][0]["source"] == "meeting_end"
    deadline = rec.time.time() + 5
    while not spawned and rec.time.time() < deadline:
        rec.time.sleep(0.01)
    assert len(spawned) == 1 and json.load(open(spawned[0])) == bundle


def test_dispatch_meeting_tap_held_verdict_counts_but_never_spawns(tmp_path, monkeypatch):
    """A tap the funnel HELD (budget spent, in the next meeting, quiet hours) still used up its
    chance to fire — it is recorded as handled, and its queue entry is the valve's to promote."""
    rec.DATA = str(tmp_path)
    monkeypatch.setattr(rec, "_whatsapp_status", lambda: "linked")
    monkeypatch.setattr(rec, "run_triage",
                        lambda evs, c: {"verdict": "queue", "reason": "meeting_hold", "bundle": {}})
    spawned = []
    monkeypatch.setattr(rec, "run_event_skill", lambda p: spawned.append(p))
    assert rec._dispatch_meeting_tap({"source": "meeting_end"}) is True
    rec.time.sleep(0.05)
    assert spawned == []


def test_dispatch_meeting_tap_gates_on_channel_health_and_survives_a_broken_triage(tmp_path, monkeypatch):
    """Same ROADMAP amendment the valve honors: never spend a unit of the day's budget on a nudge
    that can't be delivered. Both refusals return False, so the end stays unhandled and the next
    tick retries it."""
    rec.DATA = str(tmp_path)
    ran = []
    monkeypatch.setattr(rec, "run_triage", lambda evs, c: (ran.append(1), {"verdict": "drop"})[1])
    for status in ("unknown", "pairing"):
        monkeypatch.setattr(rec, "_whatsapp_status", lambda s=status: s)
        assert rec._dispatch_meeting_tap({"source": "meeting_end"}) is False
    assert ran == []

    def boom(evs, c):
        raise RuntimeError("triage_event.py not found in this image")
    monkeypatch.setattr(rec, "_whatsapp_status", lambda: "linked")
    monkeypatch.setattr(rec, "run_triage", boom)
    assert rec._dispatch_meeting_tap({"source": "meeting_end"}) is False


# ── Update flag: the published VERSION stamp vs this build's ─────────────────────────────────────

def _stamp(tmp_path, monkeypatch, value: str):
    """Point the module at a VERSION file holding `value` (write nothing for the no-file case)."""
    p = os.path.join(str(tmp_path), "VERSION")
    if value is not None:
        with open(p, "w") as f:
            f.write(value)
    monkeypatch.setattr(rec, "VERSION_FILE", p)
    return p


def test_local_version_only_accepts_a_real_stamp(tmp_path, monkeypatch):
    """`YYYY-MM-DD.<short-sha>` is the stamp; `dev` (the monorepo checkout) and a missing file are
    both the DEV case, and the dev case is silence."""
    _stamp(tmp_path, monkeypatch, "2026-08-07.22cc558\n")
    assert rec.local_version() == "2026-08-07.22cc558"
    _stamp(tmp_path, monkeypatch, "dev\n")
    assert rec.local_version() == ""
    monkeypatch.setattr(rec, "VERSION_FILE", os.path.join(str(tmp_path), "nope", "VERSION"))
    assert rec.local_version() == ""


def test_update_status_flags_only_a_different_published_stamp(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    _stamp(tmp_path, monkeypatch, "2026-08-01.aaaaaaa")
    monkeypatch.setattr(rec, "_fetch_latest_version", lambda: "2026-08-07.bbbbbbb")
    rec.check_for_update()
    st = rec.update_status()
    assert st == {"current": "2026-08-01.aaaaaaa", "latest": "2026-08-07.bbbbbbb", "available": True}
    # same stamp → no flag
    _stamp(tmp_path, monkeypatch, "2026-08-07.bbbbbbb")
    assert rec.update_status()["available"] is False


def test_update_check_is_daily_and_cached(tmp_path, monkeypatch):
    """One GET a day: a second call inside the window reuses the cached answer, and the cache
    survives a restart (it's a file on the volume, not memory)."""
    rec.DATA = str(tmp_path)
    _stamp(tmp_path, monkeypatch, "2026-08-01.aaaaaaa")
    calls = []
    monkeypatch.setattr(rec, "_fetch_latest_version",
                        lambda: (calls.append(1), "2026-08-07.bbbbbbb")[1])
    rec.check_for_update()
    rec.check_for_update()
    assert calls == [1]
    assert rec.update_status()["latest"] == "2026-08-07.bbbbbbb"
    # a day later the cache is stale → exactly one more fetch
    cache = os.path.join(str(tmp_path), "cache", "update_check.json")
    c = json.load(open(cache))
    c["fetched_at"] = c["fetched_at"] - rec.UPDATE_CHECK_SECS - 1
    json.dump(c, open(cache, "w"))
    rec.check_for_update()
    assert len(calls) == 2


def test_update_check_is_silent_on_any_failure(tmp_path, monkeypatch):
    """Offline, 404, garbage: the flag must never flicker on (or off) because GitHub was
    unreachable — a failed fetch keeps whatever the last good answer was."""
    rec.DATA = str(tmp_path)
    _stamp(tmp_path, monkeypatch, "2026-08-01.aaaaaaa")

    def boom(*_a, **_k):
        raise OSError("name resolution failed")
    monkeypatch.setattr(rec.urllib.request, "urlopen", boom)
    assert rec._fetch_latest_version() == ""
    assert rec.check_for_update() == {}
    assert rec.update_status() == {"current": "2026-08-01.aaaaaaa", "latest": "", "available": False}
    # a good answer, then a broken fetch: the previous answer stands
    monkeypatch.setattr(rec, "_fetch_latest_version", lambda: "2026-08-07.bbbbbbb")
    rec.check_for_update()
    monkeypatch.setattr(rec, "_fetch_latest_version", lambda: "")
    c = json.load(open(os.path.join(str(tmp_path), "cache", "update_check.json")))
    c["fetched_at"] = c["fetched_at"] - rec.UPDATE_CHECK_SECS - 1
    json.dump(c, open(os.path.join(str(tmp_path), "cache", "update_check.json"), "w"))
    rec.check_for_update()
    assert rec.update_status()["available"] is True


def test_update_check_thread_never_starts_on_a_dev_build(tmp_path, monkeypatch):
    """A dev checkout makes no outbound request at all, and SOTTO_UPDATE_CHECK=0 turns the daily
    check off everywhere."""
    rec.DATA = str(tmp_path)
    monkeypatch.delenv("SOTTO_UPDATE_CHECK", raising=False)
    _stamp(tmp_path, monkeypatch, "dev")
    assert rec.start_update_check_thread() is None
    _stamp(tmp_path, monkeypatch, "2026-08-01.aaaaaaa")
    monkeypatch.setenv("SOTTO_UPDATE_CHECK", "0")
    assert rec.start_update_check_thread() is None


def test_hermes_versions_read_from_the_boot_file(tmp_path, monkeypatch):
    """start.sh writes the same pair it prints to the boot log; a run without start.sh reads
    empty, never raises."""
    rec.DATA = str(tmp_path)
    assert rec.hermes_versions() == {"running": "", "image": ""}
    os.makedirs(os.path.join(str(tmp_path), "cache"), exist_ok=True)
    with open(os.path.join(str(tmp_path), "cache", "hermes-version.json"), "w") as f:
        f.write('{"running":"hermes 0.20.1","image":"hermes 0.21.0"}')
    assert rec.hermes_versions() == {"running": "hermes 0.20.1", "image": "hermes 0.21.0"}
    with open(os.path.join(str(tmp_path), "cache", "hermes-version.json"), "w") as f:
        f.write("not json")
    assert rec.hermes_versions() == {"running": "", "image": ""}


def test_setup_status_carries_update_and_hermes(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    monkeypatch.setattr(rec, "SETTINGS_FILE", os.path.join(str(tmp_path), "config", "settings.json"))
    monkeypatch.setattr(rec, "google_connected", lambda: (False, "nope"))
    _stamp(tmp_path, monkeypatch, "dev")
    st = rec.setup_status()
    assert set(st["update"]) == {"current", "latest", "available"}
    assert st["update"]["available"] is False          # dev build: nothing to say
    assert set(st["hermes"]) == {"running", "image"}


def test_setup_page_flags_an_available_update_and_the_hermes_pair(monkeypatch):
    """The ONE place Sotto mentions being out of date: one quiet line on the Integrations page, in
    the existing type tokens, pointing at RAILWAY.md § Staying updated."""
    monkeypatch.setattr(rec, "RAILWAY_DOMAIN", "myapp.up.railway.app")
    monkeypatch.setattr(rec, "MCP_TOKEN", "tok123")
    base = {"bridge_connected": True, "google_connected": True, "google_detail": "ok",
            "google_client_present": True, "timezone": "Europe/Paris", "whatsapp": "linked"}
    monkeypatch.setattr(rec, "setup_status", lambda: dict(
        base, update={"current": "2026-08-01.aaaaaaa", "latest": "2026-08-07.bbbbbbb",
                      "available": True},
        hermes={"running": "hermes 0.20.1", "image": "hermes 0.21.0"}))
    page = rec._setup_page("abc")
    assert "Sotto 2026-08-07.bbbbbbb is available — you're on 2026-08-01.aaaaaaa." in page
    assert "RAILWAY.md#staying-updated" in page
    assert "Hermes running: hermes 0.20.1 · image built with: hermes 0.21.0." in page
    assert "SOTTO_REFRESH_HERMES=1" in page            # the versions differ → the one action
    # no new visual vocabulary: the lines reuse the page's existing quiet meta type
    assert "class='tile-meta'>Sotto 2026-08-07" in page

    # nothing available, versions agree → one line, no update line, no refresh advice
    monkeypatch.setattr(rec, "setup_status", lambda: dict(
        base, update={"current": "2026-08-07.bbbbbbb", "latest": "2026-08-07.bbbbbbb",
                      "available": False},
        hermes={"running": "hermes 0.21.0", "image": "hermes 0.21.0"}))
    page = rec._setup_page("abc")
    assert "is available" not in page and "SOTTO_REFRESH_HERMES" not in page
    assert "Hermes running: hermes 0.21.0 · image built with: hermes 0.21.0." in page

    # dev build (no update/hermes facts at all) → the page says nothing about versions
    monkeypatch.setattr(rec, "setup_status", lambda: dict(
        base, update={"current": "", "latest": "", "available": False},
        hermes={"running": "", "image": ""}))
    page = rec._setup_page("abc")
    assert "is available" not in page and "Hermes" not in page
