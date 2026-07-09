#!/usr/bin/env python3
"""
Sotto trigger receiver (SPEC §4.1). Host-neutral endpoint beside the agent (Hermes or OpenClaw).

The Bridge POSTs `{type:"morning_ready"|"evening_ready", date, local_data}` here when the Mac comes
up. The receiver (1) authenticates the bearer (constant-time), (2) dedupes against the per-day
delivered flag, (3) stages local_data, (4) enqueues the brief skill run on Hermes.

Security: binds 0.0.0.0 on Railway behind its TLS proxy (127.0.0.1 locally), caps body size, strictly
validates `date` before using it in any path, and only writes the delivered flag AFTER the skill
is successfully enqueued. /mcp and /bridge/* take the MCP bearer; /sotto/trigger takes the trigger
token; the setup/pairing/debug-status pages (which surface the pairing link = the bearer, and the
WhatsApp QR) take a per-deploy setup code printed to the boot log. /health is open. Stdlib only.
"""
from __future__ import annotations

import hmac
import importlib.util
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.environ.get("SOTTO_DATA", "/data")
# One shared bearer by default: the Bridge's wake-push sends the same token it dials in with
# (BRIDGE_TOKEN → SOTTO_MCP_TOKEN), so /sotto/trigger accepts it unless a dedicated
# SOTTO_TRIGGER_TOKEN is set — otherwise default-on wake-push would silently 401.
_TRIGGER_TOKEN = os.environ.get("SOTTO_TRIGGER_TOKEN", "")
_MCP_TOKEN_ENV = os.environ.get("SOTTO_MCP_TOKEN", "")
TOKEN = _TRIGGER_TOKEN or _MCP_TOKEN_ENV
# The reverse-MCP relay (tunnel-free transport) authenticates with the MCP token — the same bearer
# Hermes uses for /mcp and the Bridge uses to dial in. Falls back to the trigger token.
MCP_TOKEN = _MCP_TOKEN_ENV or _TRIGGER_TOKEN
SKILL = {"morning_ready": "sotto-morning-brief", "evening_ready": "sotto-evening-brief"}
MAX_BYTES = 8 * 1024 * 1024  # 8 MB — a LocalData snapshot is KBs; reject anything larger
DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
# A `.claim` this old with no `.delivered` marker means the enqueued run died silently (Popen
# succeeded, the skill never delivered). A fresh trigger may then reclaim and retry.
CLAIM_STALE_SECS = 30 * 60

# The setup code that gates the setup/pairing/debug-status surface (/setup, /pair, /google/*,
# /whatsapp/qr, /debug/google). Those pages leak the MCP bearer (the pairing link) and the live
# WhatsApp QR, so "the Railway URL is the secret" is not enough. Resolved lazily: env override →
# persisted file on the volume → generated once and persisted (0600). Printed to stdout at boot as a
# full setup URL, so the user grabs it from the deploy logs.
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
    SETUP_CODE = code
    return code

# Reverse-MCP relay: the Mac dials OUT to /bridge/poll|respond; Hermes calls /mcp locally. No tunnel.
_relay_spec = importlib.util.spec_from_file_location(
    "relay", os.path.join(os.path.dirname(__file__), "relay.py"))
_relay_mod = importlib.util.module_from_spec(_relay_spec)
_relay_spec.loader.exec_module(_relay_mod)
RELAY = _relay_mod.Relay()


def delivered_flag(date: str, kind: str) -> str:
    # The TRIGGER-dedup claim (prevents two near-simultaneous triggers double-enqueuing). Distinct from
    # the brief skill's `.delivered` marker (brief_marker.py), which is the deliver-once gate the cron and
    # wake-push share — so a `.claim` here never makes the skill think it already delivered.
    return os.path.join(DATA, "briefs", f"{date}.{kind}.claim")


def run_skill(skill: str, payload_path: str) -> None:
    # HOST-NEUTRAL one-shot. Hermes/OpenClaw have NO `run <skill> --input` command — the scriptable
    # entry point is a single PROMPT in, final text out (`hermes -z "<prompt>"`, the documented
    # one-shot for shell scripts/cron). So we hand the agent a prompt that names the skill and points
    # it at the staged payload; the brief SKILL loads local_data from that path instead of calling
    # read_local. Override the runner with SOTTO_RUN_SKILL (e.g. "hermes chat -q", an OpenClaw cmd).
    # shell=False (list args) — no shell is invoked; shlex.split tolerates spaces in the path.
    runner = shlex.split(os.environ.get("SOTTO_RUN_SKILL", "hermes -z"))
    # Imperative + fail-loud. A permissive "produce the brief" prompt lets the agent IMPROVISE a
    # freehand calendar/inbox recap (wrong names, fake group deep links, sms-instead-of-whatsapp) when
    # it skips the deterministic composer. The brief's quality lives ENTIRELY in compose_brief.py —
    # so mandate it, forbid hand-writing, and require failing loudly (not fabricating) if it can't run.
    prompt = (
        f"Run the {skill} skill now, following its SKILL.md procedure EXACTLY. The Sotto Bridge just "
        f"delivered its trigger; use the staged local_data payload at {payload_path} as the brief's "
        f"local context (do NOT call read_local). You MUST generate the brief by running the skill's "
        f"compose_brief.py via execute_code and delivering its brief_markdown VERBATIM. Do NOT write the "
        f"brief yourself and do NOT hand-summarize the calendar/inbox. Use each action's tap_link "
        f"verbatim — never invent sms:/wa.me links or deep-link a group chat. If you cannot run "
        f"compose_brief.py (e.g. execute_code is unavailable/unapproved), STOP and report that you "
        f"could not generate the brief — do NOT improvise one. Deliver as Sotto, never as 'Hermes Agent'."
    )
    subprocess.Popen([*runner, prompt])  # fire-and-forget; the skill delivers via the gateway (may raise FileNotFoundError)


def _claim_is_stale(flag: str, date: str, kind_short: str) -> bool:
    """An existing claim is STALE iff the skill never delivered (no `.delivered` marker from
    brief_marker.py) AND the claim is older than CLAIM_STALE_SECS. Covers the silent-loss mode where
    Popen succeeded but the spawned run died before delivering — the claim used to block the whole
    day. brief_marker's deliver-once gate still guarantees at most one send."""
    delivered = os.path.join(DATA, "briefs", f"{date}.{kind_short}.delivered")
    if os.path.exists(delivered):
        return False
    try:
        return (time.time() - os.path.getmtime(flag)) > CLAIM_STALE_SECS
    except OSError:
        return False


# Serializes the claim/stale-check/reclaim sequence across ThreadingHTTPServer threads: the
# remove-then-O_EXCL-create window in the stale path let two concurrent triggers BOTH reclaim
# (thread A removes, A and B both create in turn) → duplicate brief spawns.
_CLAIM_LOCK = threading.Lock()


def run_proactive_skill() -> None:
    # Host-neutral one-shot for the sotto-proactive skill (parallels run_skill). Unlike a brief, the
    # proactive scan needs NO staged payload — it reads live Google/continuity state itself — so we
    # just hand the agent a prompt that names the skill. quiet hours + once-per-day nudge dedup are
    # deterministic in proactive_scan.py, so this prompt only has to say "run it now, and stay silent
    # if there's nothing" (the skill's SKILL.md carries the rest).
    runner = shlex.split(os.environ.get("SOTTO_RUN_SKILL", "hermes -z"))
    prompt = (
        "Run the sotto-proactive skill now, following its SKILL.md procedure EXACTLY. The Sotto Bridge "
        "just detected your Mac waking, so check for anything genuinely time-sensitive RIGHT NOW. Run "
        "proactive_scan.py and act ONLY on the nudges it returns. If it returns no nudges, say nothing "
        "and end the turn — silence is the correct, common output. Auto-draft, never auto-send; deliver "
        "as Sotto, never as 'Hermes Agent'."
    )
    subprocess.Popen([*runner, prompt])  # fire-and-forget (may raise FileNotFoundError)


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
        run_proactive_skill()
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
    return 202, {"status": "enqueued", "skill": "sotto-proactive"}


def handle_trigger(body: dict) -> tuple[int, dict]:
    kind = body.get("type")
    if kind == "proactive_wake":  # event-driven proactive nudge — no date/payload needed
        return handle_proactive_wake()
    if kind not in SKILL:
        return 400, {"error": "unknown type"}
    date = body.get("date") or ""
    if not DATE_RE.match(date):
        return 400, {"error": "bad date"}
    kind_short = kind.replace("_ready", "")
    flag = delivered_flag(date, kind_short)
    os.makedirs(os.path.dirname(flag), exist_ok=True)
    # Atomically CLAIM this (date, kind) so two near-simultaneous triggers (e.g. cron + wake-push, or a
    # retry) can't both enqueue → duplicate briefs. O_EXCL is the atomic guard the old exists()+open()
    # check raced on; _CLAIM_LOCK closes the remaining remove→re-create window in the stale-reclaim
    # path. We release the claim if enqueue fails, so a misconfigured runner never silently
    # suppresses the day's brief (the original intent).
    with _CLAIM_LOCK:
        try:
            os.close(os.open(flag, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            if not _claim_is_stale(flag, date, kind_short):
                return 200, {"status": "already_delivered"}
            # Stale claim, brief never delivered: release it and re-claim (atomic under the lock)
            # so THIS trigger retries.
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
    os.makedirs(os.path.join(DATA, "briefs"), exist_ok=True)
    payload_path = os.path.join(DATA, "briefs", f"{date}.{kind}.payload.json")
    with open(payload_path, "w") as f:
        json.dump(body.get("local_data") or {}, f)
    try:
        run_skill(SKILL[kind], payload_path)
    except Exception as e:  # noqa: BLE001
        try:
            os.remove(flag)   # release the claim so a later trigger can retry
        except OSError:
            pass
        return 500, {"error": f"enqueue failed: {e}"}
    return 202, {"status": "enqueued", "skill": SKILL[kind]}


QR_FILE = os.path.join(DATA, "whatsapp-pairing.txt")
QR_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<meta http-equiv='refresh' content='6'>"
    "<title>Scan to link WhatsApp</title>"
    "<style>body{{background:#fff;color:#000;margin:16px}}"
    "pre{{font-family:'Courier New',monospace;line-height:1;letter-spacing:0;font-size:11px;"
    "white-space:pre}}</style></head><body>"
    "<p>Open WhatsApp ▸ Linked Devices ▸ Link a Device, then scan. Page auto-refreshes.</p>"
    "<pre>{body}</pre></body></html>"
)


GAUTH_FILE = os.path.join(DATA, "google-auth-url.txt")

# Public Railway domain (set by the platform). Used to build the one-click pairing link for the Mac app.
RAILWAY_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")


def pairing_link() -> str:
    """The `sotto-bridge://` deep link the Mac app ingests in ONE click — it carries the full host
    (with https://, so the schemeless-downgrade bug can't happen) and the bearer token, so the user
    types nothing. Same string doubles as the copy-paste 'pairing code'."""
    host = f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else ""
    q = urllib.parse.urlencode({"host": host, "token": MCP_TOKEN})
    return f"sotto-bridge://pair?{q}"


def _google_setup_py():
    """Locate the Hermes google-workspace setup.py (same tool start.sh uses for the code exchange)."""
    import glob
    for base in (os.path.expanduser("~/.hermes"), "/usr/local/lib/hermes-agent", "/root/.hermes"):
        hits = glob.glob(os.path.join(base, "**", "google-workspace", "scripts", "setup.py"), recursive=True)
        if hits:
            return hits[0]
    return None


def google_connected() -> tuple[bool, str]:
    """Is Google Workspace currently connected? Runs the same `setup.py --check` start.sh uses, so the
    answer matches what a cron brief sees. Google is server-side (no Bridge), so this is the single
    source of truth for 'why is Gmail/Calendar missing from my brief'. Never raises."""
    setup = _google_setup_py()
    if not setup:
        return False, "google-workspace skill not found in this image."
    if not os.path.exists(os.path.expanduser("~/.hermes/google_client_secret.json")):
        return False, "no OAuth client — set GOOGLE_OAUTH_CLIENT_JSON in Railway, then authorize at /google/auth."
    py = shutil.which("python") or shutil.which("python3") or "python3"
    try:
        r = subprocess.run([py, setup, "--check"], capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        return False, f"check failed to run: {e}"
    if r.returncode == 0:
        return True, "connected ✓"
    return False, "not connected — authorize at /google/auth (no redeploy needed)."


def exchange_google_code(code: str) -> tuple[bool, str]:
    """Exchange a Google auth code for a token LIVE (no Railway redeploy). Runs the same
    `setup.py --auth-code` start.sh runs, against the PKCE verifier the /google/auth step persisted.
    Best-effort: on any miss it returns a clear reason so the user can fall back to the env+redeploy
    path. Never raises."""
    code = (code or "").strip()
    if not code:
        return False, "No code provided."
    setup = _google_setup_py()
    if not setup:
        return False, "Google setup tool not found in this image (is the google-workspace skill installed?)."
    secret = os.path.expanduser("~/.hermes/google_client_secret.json")
    if not os.path.exists(secret):
        return False, "Google client not set up yet — set GOOGLE_OAUTH_CLIENT_JSON in Railway and redeploy, then authorize."
    py = shutil.which("python") or shutil.which("python3") or "python3"
    try:
        r = subprocess.run([py, setup, "--auth-code", code, "--format", "json"],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        return False, f"Could not run the exchange: {e}"
    if r.returncode == 0:
        try:
            os.remove(GAUTH_FILE)
        except OSError:
            pass
        return True, "Connected ✓"
    return False, (r.stderr or r.stdout or "exchange failed").strip()[:600]


SETTINGS_FILE = os.path.join(DATA, "config", "settings.json")


def read_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def write_setting(key: str, value) -> None:
    s = read_settings()
    s[key] = value
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f)
    os.replace(tmp, SETTINGS_FILE)


_IANA_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_+./-]{0,63}\Z")


def set_timezone(tz: str) -> tuple[bool, str]:
    """Persist the browser-detected IANA zone to the volume so compose_brief/brief_marker pick it up
    (the Railway SOTTO_TIMEZONE var becomes OPTIONAL — this kills the UTC-briefs footgun). Also nudge
    the host's cron/system-prompt zone live so the times line up without a redeploy. Never raises."""
    tz = (tz or "").strip()
    if not tz or "/" not in tz or not _IANA_RE.match(tz):
        return False, "That doesn't look like an IANA timezone (e.g. America/Los_Angeles)."
    write_setting("timezone", tz)
    # Best-effort: align the host agent's clock/cron tz too. Harmless if the CLI/flag differs.
    try:
        subprocess.run(["hermes", "config", "set", "timezone", tz],
                       capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        pass
    return True, tz


def setup_google_client(client_json: str) -> tuple[bool, str]:
    """Load a Google OAuth *client* LIVE from a pasted JSON — no Railway var, no redeploy. Writes the
    client secret and mints the auth URL + PKCE verifier (same setup.py start.sh runs at boot). After
    this the user authorizes at /google/auth and pastes the code, all without touching the dashboard."""
    client_json = (client_json or "").strip()
    if not client_json:
        return False, "Paste your OAuth client JSON first."
    try:
        obj = json.loads(client_json)
    except (json.JSONDecodeError, ValueError):
        return False, "That doesn't look like valid JSON — paste the full client secret file."
    if not (isinstance(obj, dict) and ("installed" in obj or "web" in obj)):
        return False, "That JSON isn't a Google OAuth client (expected an 'installed' or 'web' key)."
    setup = _google_setup_py()
    if not setup:
        return False, "Google setup tool not found in this image (is the google-workspace skill installed?)."
    secret = os.path.expanduser("~/.hermes/google_client_secret.json")
    os.makedirs(os.path.dirname(secret), exist_ok=True)
    with open(secret, "w", encoding="utf-8") as f:
        f.write(client_json)
    py = shutil.which("python") or shutil.which("python3") or "python3"
    try:
        r = subprocess.run([py, setup, "--auth-url", "--services", "email,calendar", "--format", "json"],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        return False, f"Saved the client, but couldn't generate the auth link: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "auth-url failed").strip()[:600]
    # setup.py persists the URL (and the PKCE verifier exchange_google_code will reuse). Surface it.
    last = os.path.expanduser("~/.hermes/google_oauth_last_url.txt")
    try:
        if os.path.exists(last):
            shutil.copy(last, GAUTH_FILE)
    except OSError:
        pass
    return True, "Client saved — now authorize Google below."


def _whatsapp_status() -> str:
    """Best-effort WhatsApp linked-state. The gateway is host-native and doesn't expose a clean probe;
    a live QR file means pairing is mid-flight (so: not linked yet)."""
    return "pairing" if os.path.exists(QR_FILE) else "unknown"


def setup_status() -> dict:
    gok, gmsg = google_connected()
    client_present = os.path.exists(os.path.expanduser("~/.hermes/google_client_secret.json"))
    tz = os.environ.get("SOTTO_TIMEZONE") or os.environ.get("TZ") or read_settings().get("timezone") or ""
    return {
        "bridge_connected": RELAY.bridge_connected(),
        "google_connected": gok,
        "google_detail": gmsg,
        "google_client_present": client_present,
        "timezone": tz,
        "whatsapp": _whatsapp_status(),
    }


def _setup_page(code: str = "") -> str:
    """The one-page wizard. Renders live status for each step (Mac · Google · WhatsApp · Timezone) and
    only the next action you need — no jumping between four URLs or the Railway dashboard. Google loads
    LIVE (paste client → authorize → paste code), so Google needs zero Railway vars and zero redeploys.
    Timezone auto-detects from the browser. Gated behind the setup code (the pairing link on this page
    carries the MCP bearer); internal links re-carry `?code=` so one authentication covers the whole
    wizard even if the `sotto_setup` cookie is blocked."""
    import html as _html
    qs = f"?code={urllib.parse.quote(code)}" if code else ""
    st = setup_status()
    link = pairing_link()
    el = _html.escape(link)
    host = f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else "(no public domain yet — generate one in Railway → Networking)"

    def badge(ok, pending=False):
        if ok:
            return "<span style='color:#1a7f37;font-weight:700'>✓ done</span>"
        if pending:
            return "<span style='color:#9a6700;font-weight:700'>○ in progress</span>"
        return "<span style='color:#999;font-weight:700'>○ to do</span>"

    # 1 · Mac
    mac = (f"<p><a class='btn' href='{el}'>Open in Sotto Bridge →</a></p>"
           "<p style='color:#666;font-size:13px'>Not on this Mac? Copy this and paste it into the app's "
           "“Paste pairing link” field:</p>"
           f"<p><span class='code' id='c'>{el}</span> <button onclick=\"navigator.clipboard.writeText("
           "document.getElementById('c').innerText)\">Copy</button></p>"
           f"<p style='color:#666;font-size:13px'>Then grant <b>Full Disk Access</b> in the app so Sotto can read Messages.</p>"
           if not st["bridge_connected"] else
           "<p style='color:#1a7f37'>Your Mac is linked and reachable. (Grant Full Disk Access in the app if you haven't.)</p>")

    # 2 · Google
    if st["google_connected"]:
        google = "<p style='color:#1a7f37'>Gmail + Calendar connected.</p>"
    elif not st["google_client_present"]:
        google = (
            "<p>One-time Google Cloud setup (~2 min), then paste the client JSON below. "
            "No Railway variable, no redeploy:</p>"
            "<ol style='color:#444;font-size:13px;margin:4px 0 8px;padding-left:22px'>"
            "<li><a href='https://console.cloud.google.com' target='_blank'>console.cloud.google.com</a> → "
            "create (or pick) a project → enable the <b>Gmail API</b> and the <b>Google Calendar API</b>.</li>"
            "<li><b>OAuth consent screen</b> → External → publish to <b>In production</b> "
            "(left in Testing, your token expires after ~7 days; no Google review is needed for your own data).</li>"
            "<li><b>Create credentials → OAuth client ID → Desktop app → Download JSON</b>.</li>"
            "<li>Paste that JSON here:</li></ol>"
            f"<form action='/setup/google-client{qs}' method='post'>"
            "<textarea name='client_json' rows='4' placeholder='{\"installed\":{...}}' style='width:100%;"
            "box-sizing:border-box;padding:8px;border:1px solid #ccc;border-radius:6px;font-family:ui-monospace,monospace'></textarea>"
            "<p><button class='btnsm'>Save client →</button></p></form>")
    else:
        google = (f"<p><a class='btn' href='/google/auth{qs}'>Authorize Gmail + Calendar →</a> "
                  "<span style='color:#666;font-size:13px'>(then paste the code on that page)</span></p>")

    # 3 · WhatsApp
    wa = ("<p style='color:#9a6700'>Pairing in progress — "
          f"<a href='/whatsapp/qr{qs}'>open the QR</a> and scan with your phone.</p>" if st["whatsapp"] == "pairing"
          else f"<p><a class='btn' href='/whatsapp/qr{qs}'>Show WhatsApp QR →</a> <span style='color:#666;font-size:13px'>"
               "(WhatsApp ▸ Linked Devices ▸ Link a Device — scan with your phone)</span></p>")

    # 4 · Timezone (auto-detected by the browser; posted once)
    tzv = _html.escape(st["timezone"])
    tz_block = (f"<p style='color:#1a7f37'>Timezone set to <b>{tzv}</b> — your briefs will fire at your local 6:30 / 17:30.</p>"
                if st["timezone"] else
                "<p id='tzmsg'>Detecting your timezone…</p>")
    tz_js = "" if st["timezone"] else (
        "<script>(function(){try{var tz=Intl.DateTimeFormat().resolvedOptions().timeZone;"
        f"if(!tz){{return;}}fetch('/setup/timezone{qs}',{{method:'POST',headers:{{'Content-Type':'application/json'}},"
        "body:JSON.stringify({timezone:tz})}).then(function(r){return r.json();}).then(function(j){"
        "var m=document.getElementById('tzmsg');if(j.ok){m.innerHTML='Timezone set to <b>'+tz+'</b> ✓';"
        "setTimeout(function(){location.reload();},700);}else{m.textContent=j.detail||'Could not set timezone automatically.';}"
        "}).catch(function(){});}catch(e){}})();</script>")

    done = st["bridge_connected"] and st["google_connected"] and bool(st["timezone"])
    footer = ("<hr><p style='font-size:15px'>🎉 You're connected. Message yourself on WhatsApp: "
              "<b>“Sotto, give me my morning brief.”</b> Briefs also fire automatically at 6:30am / 5:30pm.</p>"
              if done else
              "<hr><p style='color:#666;font-size:13px'>Finish the steps above, then "
              f"<a href='/setup{qs}'>recheck</a>. Briefs deliver once your Mac is linked, Google is connected, and a timezone is set.</p>")

    return (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Set up Sotto</title>"
        "<style>body{font-family:system-ui;max-width:640px;margin:36px auto;padding:0 16px;line-height:1.55;color:#111}"
        "h2{margin-bottom:4px}h3{margin:26px 0 6px}code,.code{background:#f3f3f3;padding:2px 6px;border-radius:4px;"
        "font-family:ui-monospace,monospace;word-break:break-all;display:inline-block}"
        "a.btn{display:inline-block;background:#2f6df6;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none;"
        "font-weight:600;margin:6px 0}button{font:inherit;padding:6px 10px;border-radius:6px;border:1px solid #ccc;"
        "background:#fff;cursor:pointer}.btnsm{background:#2f6df6;color:#fff;border:1px solid #2f6df6;padding:8px 14px;font-weight:600}"
        ".row{display:flex;justify-content:space-between;align-items:baseline}</style></head><body>"
        "<h2>Set up Sotto</h2><p style='color:#666;margin-top:0'>Your agent is live. Four steps, all on this page.</p>"
        f"<div class='row'><h3>1 · Link your Mac</h3>{badge(st['bridge_connected'])}</div>{mac}"
        f"<div class='row'><h3>2 · Connect Google</h3>{badge(st['google_connected'], st['google_client_present'])}</div>{google}"
        f"<div class='row'><h3>3 · Link WhatsApp</h3>{badge(False, st['whatsapp']=='pairing')}</div>{wa}"
        f"<div class='row'><h3>4 · Timezone</h3>{badge(bool(st['timezone']))}</div>{tz_block}{tz_js}"
        f"{footer}"
        f"<p style='color:#999;font-size:12px;margin-top:24px'>Host: <code>{_html.escape(host)}</code></p>"
        "</body></html>"
    )


# (/pair is a legacy path: it 302-redirects to /setup in do_GET — the old standalone pair page
# is gone; the deep link + copyable pairing code live in _setup_page.)

# Setup/pairing/debug-status surface — everything here can leak the MCP bearer (pairing link), the
# live WhatsApp QR, or accept config writes, so it's gated behind the setup code (see resolve_setup_code).
SETUP_GET_PATHS = frozenset({"/setup", "/setup/status", "/pair", "/google/auth", "/google/submit-code",
                             "/whatsapp/qr", "/debug/google"})
SETUP_POST_PATHS = frozenset({"/setup/timezone", "/setup/google-client"})


class Handler(BaseHTTPRequestHandler):
    def _authed(self, token: str) -> bool:
        return bool(token) and hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}")

    def _setup_authed(self) -> bool:
        """Auth for the setup surface: valid `?code=`/`?setup_code=` query param, OR the `sotto_setup`
        cookie (set after the first valid code, so the wizard is authenticate-once), OR the MCP bearer.
        All comparisons constant-time. (`setup_code` exists because on /google/submit-code the `code`
        param is Google's auth code — a GET form replaces the action's query string.)"""
        code = resolve_setup_code()
        if not code:
            return False
        if self._authed(MCP_TOKEN):
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
        return False

    def _forbid_setup(self):
        # No token material, no code hints — just where to find the link.
        self._write(403, "text/plain; charset=utf-8",
                    b"Forbidden. Open the setup link (with ?code=...) from your deploy logs.\n")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        # Healthcheck for the platform (Railway healthcheckPath=/health → restart on failure).
        if path == "/health":
            return self._send(200, {"status": "ok", "bridge_connected": RELAY.bridge_connected()})
        # Reverse-MCP: the Bridge long-polls here for the next tool call (held open ~25s).
        if path == "/bridge/poll":
            if not self._authed(MCP_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            req = RELAY.poll(timeout=25.0)
            return self._send(200 if req else 204, req or {})
        if path == "/bridge/status":
            return self._send(200, {"bridge_connected": RELAY.bridge_connected()})
        # Brief diagnostics. compose_brief runs in Hermes' execute_code sandbox, so its logs go to the
        # agent, NOT Railway's container logs — it appends them here instead. Bearer-protected (the
        # lines can carry contact identifiers). `?n=` tails N lines (default 200).
        if path == "/debug/brief-log":
            if not self._authed(MCP_TOKEN):
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
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        # Everything below is the setup/pairing/debug-status surface: it can expose the MCP bearer
        # (pairing link) or the live WhatsApp QR, so it requires the setup code / cookie / bearer.
        if path in SETUP_GET_PATHS and not self._setup_authed():
            return self._forbid_setup()
        qs = f"?code={urllib.parse.quote(resolve_setup_code())}" if path in SETUP_GET_PATHS else ""
        # Google connection status — the one-glance answer to "why is Gmail/Calendar missing?" Google
        # is server-side (independent of the Bridge), so a cron brief gets it iff this says connected.
        if path == "/debug/google":
            ok, msg = google_connected()
            return self._send(200 if ok else 503, {"google_connected": ok, "detail": msg,
                                                    "authorize": "/google/auth"})
        # The unified setup wizard: live status for Mac · Google · WhatsApp · Timezone, next action inline.
        if path == "/setup":
            return self._html(200, _setup_page(resolve_setup_code()))
        if path == "/setup/status":
            return self._send(200, setup_status())
        # Legacy /pair → the wizard (keeps old links/QRs working; the deep link itself is in the page).
        if path == "/pair":
            self.send_response(302)
            self.send_header("Location", f"/setup{qs}")
            self.end_headers()
            return
        # Live Google code exchange (no Railway redeploy). The /google/auth form posts the code here.
        if path == "/google/submit-code":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            ok, msg = exchange_google_code((q.get("code") or [""])[0])
            import html as _html
            badge = "✅ Google connected" if ok else "⚠️ Not connected"
            extra = "" if ok else ("<p>Fallback: set <code>GOOGLE_AUTH_CODE</code> in Railway → Variables and "
                                   f"redeploy. <a href='/google/auth{qs}'>← back</a></p>")
            return self._html(200 if ok else 400,
                "<!doctype html><meta charset='utf-8'><style>body{font-family:system-ui;max-width:640px;"
                "margin:40px auto;padding:0 16px;line-height:1.5}code{background:#f3f3f3;padding:1px 4px;"
                f"border-radius:3px}}</style><h2>{badge}</h2><p>{_html.escape(msg)}</p>{extra}")
        # Google Workspace authorization page: a clickable auth URL + the copy-the-code instructions.
        # The deterministic flow lives in start.sh; this just presents the one-time URL it generated.
        if path == "/google/auth":
            try:
                url = open(GAUTH_FILE).read().strip()
            except OSError:
                return self._html(200, "<p>No Google authorization pending — already connected, or set "
                                       "<code>GOOGLE_OAUTH_CLIENT_JSON</code> in Railway to begin.</p>")
            import html as _html
            u = _html.escape(url)
            return self._html(200,
                "<!doctype html><html><head><meta charset='utf-8'><title>Connect Google</title>"
                "<style>body{font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px;line-height:1.5}"
                "code{background:#f3f3f3;padding:1px 4px;border-radius:3px}</style></head><body>"
                "<h2>Connect Google to Sotto</h2>"
                f"<p><a href='{u}' target='_blank'><b>1 — Authorize Gmail + Calendar →</b></a></p>"
                "<p>You'll see an \"unverified app\" screen (it's <i>your</i> client) → <b>Advanced → Continue</b> → <b>Allow</b>.</p>"
                "<p><b>2</b> — You'll land on a <code>localhost:1/?code=…</code> page that won't load. Copy the "
                "<code>code</code> value (everything after <code>code=</code>, before <code>&</code>).</p>"
                "<p><b>3</b> — Paste it here and click <b>Connect</b> — no redeploy needed:</p>"
                "<form action='/google/submit-code' method='get'>"
                # a GET form replaces the action's query string, so the setup code rides along as a
                # hidden field (`code` itself is Google's auth code here).
                f"<input type='hidden' name='setup_code' value='{_html.escape(resolve_setup_code(), quote=True)}'>"
                "<input name='code' placeholder='paste the code' style='width:70%;padding:8px;border:1px solid "
                "#ccc;border-radius:6px;font-family:ui-monospace,monospace'> "
                "<button style='padding:8px 14px;border-radius:6px;border:1px solid #2f6df6;background:#2f6df6;"
                "color:#fff;cursor:pointer'>Connect</button></form>"
                "<p style='color:#666;font-size:13px'>Fallback if that fails: set <code>GOOGLE_AUTH_CODE</code> "
                "in Railway → Variables and redeploy.</p>"
                "</body></html>")
        # Serve the live WhatsApp pairing output (incl. the QR) with tight line-height so it scans in a
        # browser — Railway's log viewer distorts the terminal QR. Only available during pairing.
        if path != "/whatsapp/qr":
            return self._send(404, {"error": "not found"})
        try:
            with open(QR_FILE) as f:
                content = f.read()
        except OSError:
            return self._html(200, "<p>No pairing in progress (already linked, or not started yet).</p>")
        import html as _html
        self._html(200, QR_PAGE.format(body=_html.escape(content)))

    def _handle_setup_post(self, path: str):
        """Setup-wizard writes — gated by the setup code in do_POST (same posture as the GET setup
        pages). Each handler additionally validates its own input."""
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._send(400, {"ok": False, "detail": "bad length"})
        if n <= 0 or n > MAX_BYTES:
            return self._send(400, {"ok": False, "detail": "empty or too-large body"})
        raw = self.rfile.read(n)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if path == "/setup/timezone":
            try:
                tz = (json.loads(raw or b"{}") or {}).get("timezone", "")
            except (json.JSONDecodeError, ValueError):
                tz = ""
            ok, detail = set_timezone(tz)
            return self._send(200 if ok else 400,
                              {"ok": ok, "timezone": detail if ok else None, "detail": None if ok else detail})
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
        qs = f"?code={urllib.parse.quote(resolve_setup_code())}"
        more = f"  ·  <a href='/google/auth{qs}'>Authorize Google →</a>" if ok else ""
        return self._html(200 if ok else 400,
            "<!doctype html><meta charset='utf-8'><style>body{font-family:system-ui;max-width:640px;"
            "margin:40px auto;padding:0 16px;line-height:1.5}</style>"
            f"<h2>{'✅ ' if ok else '⚠️ '}{_html.escape(msg)}</h2>"
            f"<p><a href='/setup{qs}'>← back to setup</a>{more}</p>")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in SETUP_POST_PATHS:
            if not self._setup_authed():
                return self._forbid_setup()
            return self._handle_setup_post(path)
        if path not in ("/sotto/trigger", "/mcp", "/bridge/respond"):
            return self._send(404, {"error": "not found"})
        token = MCP_TOKEN if path in ("/mcp", "/bridge/respond") else TOKEN
        if not self._authed(token):
            return self._send(401, {"error": "unauthorized"})
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
        # Reverse-MCP: Hermes' JSON-RPC in → relay to the Bridge → JSON-RPC out.
        if path == "/mcp":
            resp = RELAY.mcp_call(body)
            return self._send(202, {}) if resp is None else self._send(200, resp)
        # Reverse-MCP: the Bridge POSTs a tool result for a pending request id.
        if path == "/bridge/respond":
            RELAY.respond(body)
            return self._send(202, {})
        code, resp = handle_trigger(body)
        self._send(code, resp)

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
        # A client that timed out and hung up (Hermes' keepalive does this on a slow/offline tool call)
        # closes the socket before we reply → BrokenPipe/ConnectionReset on write. That's expected, not
        # an error: swallow it so it doesn't dump a traceback per disconnect into the logs.
        try:
            self.send_response(code)
            # First valid ?code= on the setup surface → set the authenticate-once wizard cookie.
            granted = getattr(self, "_grant_cookie", None)
            if granted:
                self.send_header("Set-Cookie", f"sotto_setup={granted}; Path=/; HttpOnly; SameSite=Lax")
                self._grant_cookie = None
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


def main():
    # Railway/Render set $PORT and require binding 0.0.0.0 (their proxy terminates TLS); locally,
    # default to loopback. Security in both cases: the bearer token + TLS at the proxy.
    port = int(os.environ.get("PORT", os.environ.get("SOTTO_TRIGGER_PORT", "8787")))
    bind = os.environ.get("SOTTO_TRIGGER_BIND", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    # The setup surface is code-gated; print the full setup URL ONCE so the user grabs it from the
    # deploy logs (Railway → Deployments → View logs). Everything else about the code is persisted.
    code = resolve_setup_code()
    base = f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else f"http://localhost:{port}"
    print(f"[sotto] Setup link (open in a browser): {base}/setup?code={urllib.parse.quote(code)}", flush=True)
    # Threaded: the Bridge's /bridge/poll holds a connection open ~25s; it must not block /mcp.
    ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
