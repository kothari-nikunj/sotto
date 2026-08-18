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
WhatsApp QR) take a per-deploy setup code printed to the boot log. /health is open. Every response
leaves through _write/_redirect, which stamp the dashboard's security headers (nosniff, no-referrer,
frame-deny, no-store, plus SETUP_CSP on HTML) and set the wizard cookie with the dashboard's exact
attributes (Secure; HttpOnly; SameSite=Lax). Stdlib only.
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
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
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
    "setup_code": lambda: resolve_setup_code(),
    "bridge_connected": lambda: RELAY.bridge_connected(),
    "last_event_at": lambda: _last_event_at(),
    "google_ok": lambda: google_connected()[0],
    "whatsapp_ok": lambda: _whatsapp_status() != "pairing",
    "connector_status": lambda: CONNECTORS.service_status(),
    "connector_error": lambda s: _connector_error(s),
    "connector_has_refresh": lambda s: _connector_has_refresh(s),
    # M2 writes: dashboard.py shells out to the skills tree's knowledge_edit.py, located with the
    # same discovery run_triage uses (late-bound so test monkeypatches on _find_sotto_script land).
    "find_script": lambda *rel: _find_sotto_script(*rel),
    # THE atomic JSON write (connectors.write_json). dashboard.py/calcache.py don't import
    # connectors, so it arrives the way every other cross-module call here does — a HOOKS lambda.
    "write_json": lambda p, o, mode=0o600, indent=None: CONNECTORS.write_json(p, o, mode, indent),
    "json_transaction": lambda p, **kw: CONNECTORS.json_transaction(p, **kw),
    # The Cadence panel's write half: "nudge me now" on a held item runs the funnel's OWN
    # promotion (triage_event.py --promote) and then takes the identical stage → spawn path the
    # release valve and a fresh agent verdict take. The dashboard never spawns anything itself.
    "promote_queued": lambda key: _promote_queued(key),
    # "Run it now" on Briefs: the same prompt crons.json holds, fired through the same runner.
    "run_job": lambda name: _run_dashboard_job(name),
    "job_names": lambda: [j[0] for j in _sotto_cron_jobs()],
    # Delivery honesty for the Cadence panel — the channel and whether it's live right now.
    "delivery_channel": lambda: (os.environ.get("SOTTO_CRON_DELIVER") or "whatsapp").strip(),
    "delivery_ready": lambda: _delivery_ready(),
    "whatsapp_status": lambda: _whatsapp_status(),
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
    # Calendar-diff nudges: same dispatch as the tap — a decline/last-minute invite/move/cancel is
    # a synthetic event through the one funnel; calcache owns detection + exactly-once.
    "calendar_change": lambda ev: _dispatch_synthetic(ev, "calendar change"),
    # The post-meeting tap (Step 2 item 3): calcache detects the event-END on the refresh tick, the
    # receiver relays it into the ordinary triage funnel. Late-bound like the rest.
    "meeting_tap": lambda ev: _dispatch_meeting_tap(ev),
})
DASHBOARD.HOOKS["calendar_snapshot"] = lambda: CALCACHE.snapshot()


def delivered_flag(date: str, kind: str) -> str:
    # The TRIGGER-dedup claim (prevents two near-simultaneous triggers double-enqueuing). Distinct from
    # the brief skill's `.delivered` marker (brief_marker.py), which is the deliver-once gate the cron and
    # wake-push share — so a `.claim` here never makes the skill think it already delivered.
    return os.path.join(DATA, "briefs", f"{date}.{kind}.claim")


# ── Spawning a skill, and actually DELIVERING what it says ──────────────────────────────────────
# THE BUG THIS EXISTS FOR (Aug 2026, found from a real Record): every lane but the crons spawned
# `hermes -z "<prompt>"` fire-and-forget. `-z` is documented as "print ONLY the final response text
# to stdout" — and the stdout of a detached Popen goes nowhere. Crons were fine because
# `hermes cron create --deliver <platform>` gives them a channel; nothing else had one. So the funnel
# would classify an email as a real ask, record "Nudged you" in The Record, spawn the skill — and the
# user would never receive it. Five of the six nudge producers were writing to a sink: Bridge events,
# the Gmail poll, the release valve, the post-meeting tap, and the dashboard's run-now.
#
# `hermes send` is the counterpart to `-z`: "pipe text from any shell script to any messaging
# platform Hermes is already configured for", targeting `platform` (the home channel) — the same
# channel SOTTO_CRON_DELIVER names for the crons. So: capture the one-shot's text, send it, and
# RECORD WHETHER IT LANDED. The receipt is the point as much as the fix — The Record said "Nudged
# you" for weeks about messages that went to /dev/null, because deciding to nudge and delivering a
# nudge were the same fact. They are now two.
SEND_TIMEOUT_SECS = 60           # `hermes send` is one HTTP POST to a platform, not an agent turn
ONESHOT_TIMEOUT_SECS = 15 * 60   # a brief runs the whole pipeline; generous, but never unbounded


def _deliver_target() -> str:
    """The platform `hermes send --to` addresses, i.e. the home channel — the SAME variable the
    crons are registered with, so a nudge and a brief can never land in different places."""
    return (os.environ.get("SOTTO_CRON_DELIVER") or "whatsapp").strip()


def _record_delivery(label: str, status: str, detail: str = "", usage: dict | None = None,
                     decision_ids: list | None = None) -> None:
    """One line per spawned skill, in $SOTTO_DATA/events/delivery.jsonl — the receiver is its ONLY
    writer, and the dashboard's Record reads it beside the triage verdicts. `status` is one of
    spawned / delivered / empty / failed; `usage` is the run's ground-truth spend (see _read_usage),
    present only on the row that closes a run. Best-effort: a receipt that can't be written must
    never cost the delivery it is describing."""
    try:
        os.makedirs(_events_dir(), exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "label": label, "status": status, "target": _deliver_target()}
        if detail:
            row["detail"] = detail[:300]
        if usage:
            row["usage"] = usage
        ids = [str(v) for v in (decision_ids or []) if str(v).strip()]
        if ids:
            row["decision_ids"] = ids[:20]
        with open(os.path.join(_events_dir(), "delivery.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _deliver_text(text: str, label: str, usage: dict | None = None,
                  decision_ids: list | None = None) -> bool:
    """Hand one skill's final text to `hermes send`. Silence is a legitimate outcome for every one
    of these skills ("if there's nothing, say nothing"), so an empty run is recorded and NOT sent —
    an empty message would be the busywork theater the standing bars forbid."""
    body = (text or "").strip()
    if not body:
        _record_delivery(label, "empty", usage=usage, decision_ids=decision_ids)
        return False
    target = _deliver_target()
    try:
        r = subprocess.run(["hermes", "send", "--to", target, "--quiet", "-"],
                           input=body, capture_output=True, text=True, timeout=SEND_TIMEOUT_SECS)
    except Exception as e:  # noqa: BLE001
        print(f"[sotto] {label}: delivery FAILED ({type(e).__name__}: {e})", flush=True)
        _record_delivery(label, "failed", f"{type(e).__name__}: {e}", usage=usage,
                         decision_ids=decision_ids)
        return False
    if r.returncode == 0:
        _record_delivery(label, "delivered", usage=usage, decision_ids=decision_ids)
        return True
    detail = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
    # LOUD: a nudge that was decided and then lost is the failure this whole seam exists to end.
    print(f"[sotto] {label}: delivery FAILED to {target} — {detail[:300]}", flush=True)
    _record_delivery(label, "failed", detail, usage=usage, decision_ids=decision_ids)
    return False


# Which of the four numbers a usage report may spell differently. We do not own hermes' schema, so
# each field is looked up under its known aliases and anything unrecognized is simply not recorded.
_USAGE_FIELDS = {"model": ("model",),
                 "cost": ("cost", "cost_usd", "total_cost"),
                 "input_tokens": ("input_tokens", "prompt_tokens"),
                 "output_tokens": ("output_tokens", "completion_tokens")}


def _read_usage(path: str | None) -> dict | None:
    """Best-effort read of the runner's `--usage-file` JSON → {model, cost, input_tokens,
    output_tokens}. A missing, empty, or unparseable file yields None (no `usage` key on the
    receipt) and NEVER an error: cost is a nice-to-have on a receipt, the delivery is not."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    # Totals may sit at the top level or under a `usage`/`totals` envelope; top level wins.
    src = {}
    for envelope in ("totals", "usage"):
        if isinstance(doc.get(envelope), dict):
            src.update(doc[envelope])
    src.update({k: v for k, v in doc.items() if not isinstance(v, dict)})
    out = {}
    for field, aliases in _USAGE_FIELDS.items():
        for alias in aliases:
            v = src.get(alias)
            if field == "model" and isinstance(v, str) and v.strip():
                out[field] = v.strip()[:80]
                break
            if field != "model" and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[field] = v
                break
    return out or None


def _spawn_env(run_id: str = "") -> dict:
    """The environment a spawned one-shot inherits. SOTTO_UNATTENDED=1 is the contract that marks
    an UNATTENDED lane: nobody is at the keyboard, so the send-gate downstream must hold anything
    that would reach a human. The interactive gateway is not spawned by us and therefore never
    carries it — that asymmetry IS the design, not an omission."""
    env = {**os.environ, "SOTTO_UNATTENDED": "1"}
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


def _spawn_and_deliver(runner: list, prompt: str, label: str,
                       decision_ids: list | None = None) -> None:
    """Run one skill one-shot and deliver whatever it says. Returns IMMEDIATELY — the work happens
    on a daemon thread, because every caller is either an HTTP handler or a heartbeat tick and none
    of them may block for a brief. The thread swallows its own exceptions, like every other daemon
    in this image.

    `Popen` is still what starts the process; the only change is that somebody now reads its stdout
    and forwards it, instead of letting the pipe die with the process."""
    # "Can we even start it?" is answerable NOW and must stay synchronous: handle_trigger releases
    # its brief claim on a failed spawn, and the dashboard's run-now button reports one. Only "did
    # it succeed?" moves to the thread. Popen used to raise FileNotFoundError for a missing runner;
    # this preserves that contract exactly, before any thread exists to lose it in.
    if not shutil.which(runner[0]):
        raise FileNotFoundError(f"{runner[0]}: not found on PATH (SOTTO_RUN_SKILL)")

    # CAREFUL: `hermes -z` is only the DEFAULT runner — SOTTO_RUN_SKILL can name anything (an
    # OpenClaw command, a wrapper script), and a foreign binary handed a flag it has never heard of
    # would fail every brief. So the two flags below are added ONLY when the runner's argv[0]
    # basename is `hermes`; every other runner gets today's argv, byte for byte.
    argv = list(runner)
    usage_path = None
    if os.path.basename(argv[0]) == "hermes":
        # Toolset ids vary per install, so there is no safe default to guess — unset means today's
        # behavior (whatever toolsets the runner picks itself). `hermes tools --summary` lists them.
        toolsets = (os.environ.get("SOTTO_SPAWN_TOOLSETS") or "").strip()
        if toolsets:
            argv += ["-t", toolsets]
        # Ground truth for what the run cost — hermes writes this report even when the run fails.
        # If tmp is unwritable we simply go without: a receipt is never worth losing a brief over.
        try:
            fd, usage_path = tempfile.mkstemp(prefix="sotto-usage-", suffix=".json")
            os.close(fd)
            argv += ["--usage-file", usage_path]
        except OSError:
            usage_path = None

    run_id = secrets.token_hex(12)

    def _work():
        try:
            try:
                r = subprocess.run([*argv, prompt], capture_output=True, text=True,
                                   timeout=ONESHOT_TIMEOUT_SECS, env=_spawn_env(run_id))
            except Exception as e:  # noqa: BLE001
                print(f"[sotto] {label}: skill run failed ({type(e).__name__}: {e})", flush=True)
                _record_delivery(label, "failed", f"run: {type(e).__name__}: {e}",
                                 usage=_read_usage(usage_path), decision_ids=decision_ids)
                return
            usage = _read_usage(usage_path)
            effects = _read_delivery_effects(run_id)
            correlated_ids = list(dict.fromkeys([
                *[str(v) for v in (decision_ids or []) if str(v).strip()],
                *[str(v) for v in (effects.get("decision_ids") or []) if str(v).strip()],
            ]))
            if r.returncode != 0:
                detail = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
                print(f"[sotto] {label}: skill exited {r.returncode} — {detail[:300]}", flush=True)
                _record_delivery(label, "failed", f"exit {r.returncode}: {detail}", usage=usage,
                                 decision_ids=correlated_ids)
                return
            delivered = _deliver_text(r.stdout, label, usage=usage, decision_ids=correlated_ids)
            if delivered:
                _finalize_delivery_effects(effects.get("effects") or [])
        finally:
            if usage_path:
                try:
                    os.unlink(usage_path)
                except OSError:
                    pass
            try:
                os.unlink(_delivery_effects_path(run_id))
            except OSError:
                pass

    _record_delivery(label, "spawned", decision_ids=decision_ids)
    threading.Thread(target=_work, name=f"deliver-{label}", daemon=True).start()


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
    _spawn_and_deliver(runner, prompt, f"brief:{skill}")


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


def run_proactive_skill() -> bool:
    # Host-neutral one-shot for the sotto-proactive skill (parallels run_skill). Unlike a brief, the
    # proactive scan needs NO staged payload — it reads live Google/continuity state itself — so we
    # just hand the agent a prompt that names the skill. quiet hours + once-per-day nudge dedup are
    # deterministic in proactive_scan.py, so this prompt only has to say "run it now, and stay silent
    # if there's nothing" (the skill's SKILL.md carries the rest).
    #
    # The CHANNEL-HEALTH gate first, exactly as the valve and the meeting tap apply it: this lane
    # spends the shared daily interrupt budget now, so running it against an unlinked WhatsApp would
    # burn the day's nudges on messages that go nowhere — the one thing "undeliverable nudges are
    # never spent" promises can't happen. Returns False when the gate held it (nothing was spawned,
    # nothing was spent).
    if not _delivery_channel_ready("proactive"):
        return False
    runner = shlex.split(os.environ.get("SOTTO_RUN_SKILL", "hermes -z"))
    prompt = (
        "Run the sotto-proactive skill now, following its SKILL.md procedure EXACTLY. The Sotto Bridge "
        "just detected your Mac waking, so check for anything genuinely time-sensitive RIGHT NOW. Run "
        "proactive_scan.py and act ONLY on the nudges it returns. If it returns no nudges, say nothing "
        "and end the turn — silence is the correct, common output. Auto-draft, never auto-send; deliver "
        "as Sotto, never as 'Hermes Agent'."
    )
    _spawn_and_deliver(runner, prompt, "proactive")
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
# its verdict is the 200 body. Verdict "agent" additionally stages a bundle and spawns the
# sotto-event one-shot from a background thread (same SOTTO_RUN_SKILL pattern as run_skill).

EVENTS_SEEN_MAX = 2000            # ring size for (source,rowid) idempotency keys
TRIAGE_TIMEOUT_SECS = 30          # Tier 0 is sub-second; Tier 1 is one Flash-Lite call
_EVENTS_LOCK = threading.Lock()   # serializes seen-ring read/modify/write across handler threads


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
    return f"{src}:{rowid}"


# Where the skills tree lives, resolved ONCE per script name. The recursive glob below is not cheap
# and the answer only changes on a redeploy (which restarts this process), so a plain dict is the
# whole cache — there is nothing to invalidate. SOTTO_SKILLS_ROOT is the host-neutral knob: a
# non-Hermes host points it at wherever ITS installer copied sotto-chief-of-staff, and everything
# that shells out to the skills tree (triage, the Gmail poll, the calendar gather, dashboard fact
# edits) works there too.
_SCRIPT_CACHE: dict = {}


def _find_sotto_script(*rel):
    """Locate a sotto chief-of-staff skill script: SOTTO_SKILLS_ROOT (when set) first, then the
    Hermes install trees (same discovery as _google_setup_py), then the repo-relative source tree
    (tests / source checkouts). Memoized per script name — see _SCRIPT_CACHE."""
    import glob
    key = tuple(rel)
    if key in _SCRIPT_CACHE:
        return _SCRIPT_CACHE[key]
    bases = [b for b in (os.environ.get("SOTTO_SKILLS_ROOT") or "").strip().split(os.pathsep) if b]
    # /root/.hermes is deliberately absent: HOME is /root in the image, so expanduser already covers it.
    bases += [os.path.expanduser("~/.hermes"), "/usr/local/lib/hermes-agent"]
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


def run_event_skill(bundle_path: str) -> None:
    # Host-neutral one-shot for the sotto-event skill (parallels run_skill/run_proactive_skill —
    # same SOTTO_RUN_SKILL runner, same imperative fail-loud prompt style). The bundle path is the
    # ground truth: the agent must act only on it, never re-triage or improvise links.
    runner = shlex.split(os.environ.get("SOTTO_RUN_SKILL", "hermes -z"))
    prompt = (
        f"Run the sotto-event skill now, following its SKILL.md procedure EXACTLY. The triage funnel "
        f"flagged real-time event(s) that clear the interrupt bar; the event bundle JSON is staged at "
        f"{bundle_path}. Read THAT bundle and act only on it — do not go looking for more events and "
        f"do not re-triage. Nudge with a ready-to-send draft; auto-draft, NEVER auto-send. Use tap "
        f"links from action_links.py verbatim — never invent sms:/wa.me links and never deep-link a "
        f"group chat. If the bundle is missing or empty, say nothing and end the turn. Deliver as "
        f"Sotto, never as 'Hermes Agent'."
    )
    decision_ids = []
    try:
        with open(bundle_path, encoding="utf-8") as f:
            bundle = json.load(f) or {}
        decision_ids = [e.get("decision_id") for e in (bundle.get("events") or [])
                        if isinstance(e, dict) and e.get("decision_id")]
    except (OSError, ValueError, TypeError):
        pass
    _spawn_and_deliver(runner, prompt, "event", decision_ids=decision_ids)


def _stage_bundle(bundle: dict) -> str:
    """Write an event bundle under $SOTTO_DATA/events/ and return its path. Raises OSError on a
    failed write — callers decide whether that's a 500 (handle_events) or a logged skip (valve)."""
    os.makedirs(_events_dir(), exist_ok=True)
    bundle_path = os.path.join(_events_dir(), f"bundle-{int(time.time() * 1000)}.json")
    with open(bundle_path, "w", encoding="utf-8") as f:
        json.dump(bundle or {}, f)
    return bundle_path


def _spawn_event_agent(bundle_path: str) -> None:
    """Spawn the sotto-event one-shot from a background thread so a slow exec never delays the 200
    back to the Bridge. A spawn failure only logs — the verdict already stands, the bundle is staged,
    and the queue/brief remain the backstop for a lost nudge."""
    def _go():
        try:
            run_event_skill(bundle_path)
        except Exception as e:  # noqa: BLE001
            print(f"[sotto] event agent spawn failed: {e}", flush=True)
    threading.Thread(target=_go, daemon=True).start()


def handle_events(body: dict) -> tuple[int, dict]:
    """POST /bridge/events (also fed by the Gmail poll thread): dedupe (source,rowid) against the
    seen ring, triage synchronously, return the verdict as the 200 body. CLAIM-FREE failure
    containment (mirrors handle_trigger): events are marked seen only after the pipeline succeeded,
    so a Bridge retry after a 500 re-triages them instead of silently losing them (triage's own
    per-thread cooldown bounds double-nudges on the rare re-run)."""
    events = body.get("events")
    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        return 400, {"error": "bad events"}
    with _EVENTS_LOCK:
        seen_set = set(_load_seen())
    fresh, keys = [], []
    for e in events:
        k = _event_key(e)
        if k is not None and k in seen_set:
            continue
        fresh.append(e)
        if k is not None and k not in keys:
            keys.append(k)
    if not fresh:
        return 200, {"verdict": "drop", "reason": "duplicate events (already seen)", "bundle": {}}
    try:
        verdict = run_triage(fresh, bool(body.get("catchup")))
    except Exception as e:  # noqa: BLE001
        return 500, {"error": f"triage failed: {e}"}
    if verdict.get("verdict") == "agent":
        try:
            bundle_path = _stage_bundle(verdict.get("bundle") or {})
        except OSError as e:
            return 500, {"error": f"bundle stage failed: {e}"}
        _spawn_event_agent(bundle_path)
    with _EVENTS_LOCK:
        try:
            _save_seen(_load_seen() + keys)
        except OSError:
            pass  # dedupe is best-effort; a lost ring write only risks a re-triage, never a loss
    _touch_event_stamp()   # fresh events made it through the pipeline — /setup can say so
    return 200, verdict


# ── Gmail poll thread (Phase 2): server-side email events, no Pub/Sub ─────────────────────────────

def _email_poll_secs() -> int:
    try:
        return int((os.environ.get("SOTTO_EMAIL_POLL_SECS") or "").strip() or 90)
    except ValueError:
        return 90


def _poll_gmail_once() -> list:
    """One poll_gmail.py run → email events. RAISES on what's distinguishable at this layer (script
    missing, exec failure, non-zero exit, non-list/bad JSON) so the loop can count consecutive
    failures — a quiet mailbox and a broken poll must not both look like []. (poll_gmail.py itself
    is fail-silent for transient fetch errors; those still come back as an empty list here.)"""
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
                code, resp = handle_events({"events": events, "catchup": False})
                if code != 200:
                    print(f"[sotto] gmail poll triage error: {resp}", flush=True)
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
# budget on an undeliverable nudge; for a non-WhatsApp SOTTO_CRON_DELIVER there is nothing to probe,
# so the valve just runs). An agent verdict from the valve rides the IDENTICAL stage-bundle → spawn path
# a fresh agent verdict takes.

VALVE_INTERVAL_SECS_DEFAULT = 900   # the same */15 cadence as the proactive heartbeat

# One sentence: when Sotto delivers over WhatsApp, a nudge waits for WhatsApp to be linked; on any
# other delivery channel there is nothing to wait for. SOTTO_CRON_DELIVER (the same var start.sh
# passes to `hermes cron create --deliver`) names the channel, so a Telegram/local user is no longer
# silently denied the valve and the post-meeting tap forever.
_DELIVERY_GATE_STATE: dict = {}     # label → last logged state, so a shut gate logs once, not per tick


def _delivery_ready(whatsapp_status: str | None = None) -> bool:
    """THE delivery rule, in one sentence: when Sotto delivers over WhatsApp the link must be live;
    on any other SOTTO_CRON_DELIVER there is nothing to probe. Silent — callers that run on a timer
    use _delivery_channel_ready below; the setup wizard's completion gate calls this directly,
    passing the WhatsApp state it already rendered from rather than re-probing."""
    if (os.environ.get("SOTTO_CRON_DELIVER") or "whatsapp").strip() != "whatsapp":
        return True
    return (whatsapp_status or _whatsapp_status()) == "linked"


def _delivery_channel_ready(label: str) -> bool:
    """_delivery_ready, plus one log line per state change (never per tick) so 'why did my nudges
    stop?' is answerable from the deploy log."""
    if (os.environ.get("SOTTO_CRON_DELIVER") or "whatsapp").strip() != "whatsapp":
        return True   # nothing to probe, and nothing worth logging every tick
    ok = _delivery_ready()
    if _DELIVERY_GATE_STATE.get(label) != ok:
        _DELIVERY_GATE_STATE[label] = ok
        print(f"[sotto] {label}: {'whatsapp linked — dispatching' if ok else 'skipped, whatsapp not linked'}",
              flush=True)
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
    `--promote` (which owns every rule about what may be promoted and spends the day's budget), then
    the IDENTICAL _stage_bundle → _spawn_event_agent path a valve promotion takes. The dashboard
    calls this through HOOKS and renders whatever comes back; it never decides anything itself.

    The channel gate is checked FIRST and reported rather than swallowed: spending a nudge on an
    unlinked WhatsApp is exactly the silent loss _delivery_channel_ready exists to prevent, and a
    user who just tapped a button deserves the reason, not a shrug.
    Returns {"ok": True, "reason"} or {"ok": False, "error": <code>, "reason": <sentence>}."""
    if not _delivery_ready():
        return {"ok": False, "error": "channel",
                "reason": f"{(os.environ.get('SOTTO_CRON_DELIVER') or 'whatsapp').strip()} "
                          "isn't linked — the nudge would go nowhere"}
    try:
        out = run_promote(key)
    except Exception as e:  # noqa: BLE001
        print(f"[sotto] dashboard promote failed: {e}", flush=True)
        return {"ok": False, "error": "unavailable", "reason": "this deploy can't promote right now"}
    if not out.get("ok"):
        return out
    try:
        bundle_path = _stage_bundle(out.get("bundle") or {})
    except OSError as e:
        print(f"[sotto] dashboard promote bundle stage failed: {e}", flush=True)
        # The funnel already spent the budget and dropped the entry from the queue — re-running
        # would double-charge, so report the failure instead of retrying.
        return {"ok": False, "error": "stage", "reason": "the nudge couldn't be staged"}
    _spawn_event_agent(bundle_path)
    return {"ok": True, "reason": str(out.get("reason") or "promoted")}


# ── "Run it now" (the dashboard's brief/digest buttons) ───────────────────────────────────────────
# The cron IS the definition of these runs, and adapters/hermes/crons.json is the ONE source for the
# crons (_sotto_cron_jobs reads it for the timezone re-registration). So "run my morning brief now"
# is literally: take that job's prompt, fire it through the same SOTTO_RUN_SKILL runner run_skill
# uses. No second prompt, no second skill mapping — and a job the deploy has gated off (SOTTO_DIGEST=0)
# simply isn't in the list, so the button can't offer what the box won't run.

def _run_dashboard_job(name: str) -> dict:
    """Fire one crons.json job by name, now. {"ok": True, "skill": …} or {"ok": False, "error"}."""
    job = next((j for j in _sotto_cron_jobs() if j[0] == name), None)
    if job is None:
        return {"ok": False, "error": "unknown", "reason": "that job isn't registered on this box"}
    _, _, prompt, skill = job
    try:
        runner = shlex.split(os.environ.get("SOTTO_RUN_SKILL", "hermes -z"))
        _spawn_and_deliver(runner, prompt, f"run-now:{name}")
    except Exception as e:  # noqa: BLE001
        print(f"[sotto] dashboard run-now spawn failed ({name}): {e}", flush=True)
        return {"ok": False, "error": "spawn", "reason": "that run couldn't be started"}
    print(f"[sotto] run-now from the dashboard: {name}", flush=True)
    return {"ok": True, "skill": skill}


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

def _dispatch_synthetic(event: dict, label: str) -> bool:
    """Run one synthetic calcache event (a meeting-end tap OR a calendar change) through triage;
    stage + spawn on an agent verdict. True ⇒ dispatched, whatever verdict the funnel returned — an
    event held by quiet hours or a meeting still consumed its chance to fire, and its queue entry
    is the valve's to promote. ONE function for both producers so the channel-health gate and the
    failure containment can never drift between them."""
    if not _delivery_channel_ready(label):
        return False
    try:
        verdict = run_triage([event], False)
    except Exception as e:  # noqa: BLE001
        print(f"[sotto] {label} triage failed: {e}", flush=True)
        return False
    if verdict.get("verdict") != "agent":
        return True
    try:
        bundle_path = _stage_bundle(verdict.get("bundle") or {})
    except OSError as e:
        print(f"[sotto] {label} bundle stage failed: {e}", flush=True)
        return True     # triage already spent the budget/cooldown — re-firing would double-nudge
    _spawn_event_agent(bundle_path)
    return True


def _dispatch_meeting_tap(event: dict) -> bool:
    return _dispatch_synthetic(event, "meeting tap")


QR_FILE = os.path.join(DATA, "whatsapp-pairing.txt")

# Shared <head> for every HTML page this receiver serves. The whole web surface is ONE product:
# every page links the dashboard's stylesheet first (/static/app.css — the paper/ink/gold tokens,
# Newsreader + JetBrains Mono) and then /static/setup.css (the Connections-surface layer, built
# against the tile/nav markup below by a parallel build), and carries the SAME favicon as /app —
# so /setup and its satellite pages read as views of the same site, never a second app. No inline
# <style> blocks here: the styling contract lives entirely in those two files. (/favicon.ico still
# answers 204 for clients that ignore the link.)
_FAVICON = ("<link rel='icon' href=\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
            "viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%23221c12'/%3E%3Ctext "
            "x='16' y='23' font-family='Georgia,serif' font-style='italic' font-size='20' "
            "font-weight='600' fill='%2352b087' text-anchor='middle'%3ES%3C/text%3E%3C/svg%3E\">")


def _page_head(title: str, head_extra: str = "", body_class: str = "") -> str:
    cls = f" class='{body_class}'" if body_class else ""
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta name='color-scheme' content='light dark'>"
            f"{_FAVICON}"
            "<link rel='stylesheet' href='/static/app.css'>"
            "<link rel='stylesheet' href='/static/setup.css'>"
            f"{head_extra}<title>{title}</title></head><body{cls}>")


def _nav() -> str:
    """The shared site navigation — same destinations as the /app dashboard's own nav, with
    Integrations (this surface) marked current. Rendered on /setup; the transient satellite pages
    (QR, Google auth, connector results) use the narrow shell instead."""
    return ("<header class='sidebar'><a class='wordmark' href='/app'>Sotto</a>"
            "<nav class='nav'>"
            "<a href='/app#today'>Today</a>"
            "<a href='/app#cadence'>Cadence</a>"
            "<a href='/app#loops'>Loops</a>"
            "<a href='/app#briefs'>Briefs</a>"
            "<a href='/app#people'>People</a>"
            "<a href='/app#learned'>Learned</a>"
            "<a href='/app#record'>Record</a>"
            "<a href='/setup' class='active' aria-current='page'>Integrations</a>"
            "</nav></header>")


def _narrow_page(title: str, inner: str, head_extra: str = "", back_href: str = "/setup") -> str:
    """Minimal shared shell for the transient setup satellites (WhatsApp QR, Google auth/exchange,
    connector success/error): same fonts/palette via the two stylesheets, no sidebar, one quiet way
    back to the wizard."""
    return (_page_head(title, head_extra=head_extra)
            + "<div class='site'><main class='content narrow'>"
            + inner
            + f"<p><a class='btn-quiet' href='{back_href}'>← Integrations</a></p>"
            "</main></div></body></html>")


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


def _connect_page(heading: str, body_html: str) -> str:
    return _narrow_page(
        "Sotto — connect a service",
        f"<section class='tile'><div class='tile-head'><h2 class='tile-title'>{heading}</h2></div>"
        f"<div class='tile-body'>{body_html}</div></section>")


def _connect_error_page(step: str, detail: str) -> str:
    """Every connector failure page NAMES the failing step (discovery / registration (DCR) / state /
    exchange / …) and quotes the truncated upstream error — the first click IS the validation, so
    the page has to say exactly which leg of the flow broke."""
    import html as _html
    return _connect_page(
        "Connection failed",
        f"<p>Step that failed: <b>{_html.escape(step)}</b>"
        f"{' (DCR)' if step == 'registration' else ''}</p>"
        f"<p class='tile-hint'>{_html.escape(detail)}</p>")


def _google_setup_py():
    """Locate the Hermes google-workspace setup.py (same tool start.sh uses for the code exchange).
    Same bases as start.sh's `find` — keep the two in step. (No /root/.hermes: HOME is /root in the
    image, so expanduser already covers it. No SOTTO_SKILLS_ROOT either — google-workspace is a HOST
    skill, not part of the sotto tree that variable points at.)"""
    import glob
    for base in (os.path.expanduser("~/.hermes"), "/usr/local/lib/hermes-agent"):
        hits = glob.glob(os.path.join(base, "**", "google-workspace", "scripts", "setup.py"), recursive=True)
        if hits:
            return hits[0]
    return None


# Memoized google_connected(): every /setup GET otherwise forks a `setup.py --check` subprocess
# (30s timeout worst-case). ~20s is fresh enough for a status page; invalidated on a successful
# live exchange so "Connected ✓" shows immediately.
_GOOGLE_CHECK_TTL_SECS = 20.0
_GOOGLE_CHECK_CACHE = (0.0, None)   # (monotonic ts, (ok, msg))


def google_connected() -> tuple[bool, str]:
    """Is Google Workspace currently connected? Runs the same `setup.py --check` start.sh uses, so the
    answer matches what a cron brief sees. Google is server-side (no Bridge), so this is the single
    source of truth for 'why is Gmail/Calendar missing from my brief'. Never raises. Memoized for
    _GOOGLE_CHECK_TTL_SECS (the check forks a subprocess)."""
    global _GOOGLE_CHECK_CACHE
    ts, cached = _GOOGLE_CHECK_CACHE
    if cached is not None and (time.monotonic() - ts) < _GOOGLE_CHECK_TTL_SECS:
        return cached
    result = _google_connected_uncached()
    _GOOGLE_CHECK_CACHE = (time.monotonic(), result)
    return result


def _google_connected_uncached() -> tuple[bool, str]:
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


def _google_api_py():
    """Locate the Hermes google-workspace google_api.py — the CLI half of the same host skill
    _google_setup_py finds, same bases (the skills tree's gather_google._find_google_api pattern)."""
    import glob
    for base in (os.path.expanduser("~/.hermes"), "/usr/local/lib/hermes-agent"):
        hits = glob.glob(os.path.join(base, "**", "google-workspace", "scripts", "google_api.py"),
                         recursive=True)
        if hits:
            return hits[0]
    return None


_EMAIL_RE = re.compile(r"[^\s<>@,;\"]+@[^\s<>@,;\"]+\.[^\s<>@,;\".]+")


def _derive_google_account_email() -> str:
    """The address of the Google account the user just connected — LEARNED, not typed. The host CLI
    has no profile/whoami, but the `From` of any `in:sent` message IS the authorized account, so one
    `gmail search "in:sent" --max 1` (the exact shape gather_google.gather_sent already runs) answers
    it. Tolerates the same From variants normalize_email does ("Name <addr>", a bare addr, a
    {name,email} object). Returns "" on ANY failure and never raises — this is a nicety layered on
    SOTTO_USER_EMAIL, never a reason to fail a connect."""
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


def exchange_google_code(code: str) -> tuple[bool, str]:
    """Exchange a Google auth code for a token LIVE (no Railway redeploy). Runs the same
    `setup.py --auth-code` start.sh runs, against the PKCE verifier the /google/auth step persisted.
    Best-effort: on any miss it returns a clear reason so the user can fall back to the env+redeploy
    path. Never raises."""
    code = _extract_google_code(code)
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
        global _GOOGLE_CHECK_CACHE
        _GOOGLE_CHECK_CACHE = (0.0, None)   # drop the memo so /setup flips to Connected right away
        # The connect moment IS when Sotto learns who you are (ROADMAP: "don't we already have email
        # with Gmail auth?"). Best-effort, never blocks the success this function just earned.
        capture_google_account_email()
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
    CONNECTORS.write_json(SETTINGS_FILE, s)


_IANA_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_+./-]{0,63}\Z")


def _configured_tz_name() -> str:
    """The user's configured zone, or "" when nothing is set. CANONICAL ORDER, and the only copy of
    it in this module: SOTTO_TIMEZONE → TZ → $SOTTO_DATA/config/settings.json → (caller's fallback,
    normally server local). The same chain lives in start.sh's step 2, dashboard._local_today and the
    skills' timeutil.configured_tz — change one, change them all."""
    return (os.environ.get("SOTTO_TIMEZONE") or os.environ.get("TZ")
            or read_settings().get("timezone") or "").strip()


def _crons_file() -> str:
    """adapters/hermes/crons.json — the container copy first (/app/adapters/hermes/, where the
    Dockerfile puts it), then the repo-relative source tree (tests / source checkouts)."""
    override = (os.environ.get("SOTTO_CRONS_JSON") or "").strip()
    if override:
        return override
    for p in ("/app/adapters/hermes/crons.json",
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                           "adapters", "hermes", "crons.json")):
        if os.path.exists(p):
            return p
    return ""


USER_ROUTINE_PREFIX = "user-"   # personal routines (sotto-routines skill) — never a SYSTEM job


def _sotto_cron_jobs() -> list:
    """The sotto cron jobs start.sh registers at boot, as (name, schedule, prompt, skill). Read
    straight from adapters/hermes/crons.json — the ONE source both registrars share, so a
    re-registration here lands exactly the jobs the next boot's dedup recognizes. Honors the same
    `gate` / `schedule_env` env keys start.sh does. Empty list when the file is missing or
    unreadable (a boot registration then simply stands until the next redeploy).

    THE USER-ROUTINE FENCE, enforced at the one place every consumer reads (the timezone
    re-registration below, and /brief's run-now job list): a `user-`-prefixed name is a PERSONAL
    routine created by the `sotto-routines` skill and is never a system job — it is dropped here, so
    no consumer of this function can remove, recreate or fire one. crons.json should never contain
    such a name; this is the belt to start.sh's braces (same fence, same prefix)."""
    path = _crons_file()
    try:
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"[sotto] cron spec unreadable ({path or 'not found'}): {e}", flush=True)
        return []
    jobs = []
    for j in spec if isinstance(spec, list) else []:
        name = str(j.get("name") or "")
        if name.startswith(USER_ROUTINE_PREFIX):
            print(f"[sotto] cron spec: ignoring {name!r} — `{USER_ROUTINE_PREFIX}` names are personal "
                  "routines, not system jobs", flush=True)
            continue
        gate = j.get("gate")
        if gate and os.environ.get(gate, "1") != "1":
            continue
        sched = os.environ.get(j.get("schedule_env") or "", "") or j["schedule"]
        jobs.append((name, sched, j["prompt"], j["skill"]))
    return jobs


def _reregister_sotto_crons(tz: str) -> None:
    """Root fix for first-night UTC briefs: start.sh registers the crons at BOOT under the boot-time
    zone (UTC on a fresh deploy — the wizard hasn't run yet), and Hermes cron captures the zone at
    creation, so without this the 6:30/17:30 briefs fire in UTC until the next redeploy. Called after
    `hermes config set timezone` succeeded with a CHANGED zone: remove each job by its stable --name
    and recreate it with the exact schedule/skill/deliver start.sh uses, now under the user's zone.
    Best-effort throughout (mirrors start.sh's `|| true` posture): any failure leaves the boot
    registration standing, and the next boot's dedup+recreate self-heals. Never raises.

    SYSTEM JOBS ONLY: this walks _sotto_cron_jobs() by exact `--name`, and that function drops every
    `user-` name, so a personal routine is never removed or recreated here. Honest v1 limitation
    (stated in the sotto-routines skill): a personal routine therefore keeps the zone it was created
    under until the user recreates it — a timezone change moves Sotto's five jobs, not theirs."""
    deliver = os.environ.get("SOTTO_CRON_DELIVER", "whatsapp")
    for name, sched, prompt, skill in _sotto_cron_jobs():
        try:  # answer any "are you sure?" prompt non-interactively (same as start.sh's dedup)
            subprocess.run(["hermes", "cron", "remove", name], input="y\ny\n",
                           capture_output=True, text=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass
        try:
            subprocess.run(["hermes", "cron", "create", sched, prompt, "--skill", skill,
                            "--name", name, "--deliver", deliver],
                           capture_output=True, text=True, timeout=60)
        except Exception:  # noqa: BLE001
            pass
    print(f"[sotto] re-registered sotto crons for timezone {tz} (was registered at boot under the "
          "old zone)", flush=True)


def set_timezone(tz: str) -> tuple[bool, str]:
    """Persist the browser-detected IANA zone to the volume so compose_brief/brief_marker pick it up
    (the Railway SOTTO_TIMEZONE var becomes OPTIONAL — this kills the UTC-briefs footgun). Also nudge
    the host's cron/system-prompt zone live, and — when that lands AND the zone actually changed —
    re-register the sotto crons so the very first night's briefs fire at the local 6:30/17:30 instead
    of UTC (see _reregister_sotto_crons). Never raises."""
    tz = (tz or "").strip()
    if not tz or "/" not in tz or not _IANA_RE.match(tz):
        return False, "That doesn't look like an IANA timezone (e.g. America/Los_Angeles)."
    # The zone the boot-time cron registration ran under (start.sh: SOTTO_TIMEZONE env, else the
    # wizard zone persisted by a PREVIOUS boot, else UTC) — read BEFORE persisting the new one.
    prev = _configured_tz_name() or "UTC"
    write_setting("timezone", tz)
    # Best-effort: align the host agent's clock/cron tz too. Harmless if the CLI/flag differs.
    cfg_ok = False
    try:
        r = subprocess.run(["hermes", "config", "set", "timezone", tz],
                           capture_output=True, text=True, timeout=20)
        cfg_ok = r.returncode == 0
    except Exception:  # noqa: BLE001
        pass
    # Only re-register when the live config set SUCCEEDED (a recreate under the old config zone
    # would change nothing) and the zone differs from what boot registered under (no-op otherwise).
    if cfg_ok and tz != prev:
        _reregister_sotto_crons(tz)
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


def _humanize_ago(secs: float) -> str:
    m = int(secs // 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    if m < 24 * 60:
        return f"{m // 60} h ago"
    return f"{m // (24 * 60)} d ago"


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


def _wa_creds_paths() -> list:
    """Where the gateway's WhatsApp session lands once a phone has scanned the QR. start.sh gates
    pairing on exactly this file (`WA_CREDS="$HOME/.hermes/platforms/whatsapp/session/creds.json"`,
    written only on a successful link), and ~/.hermes is symlinked to $SOTTO_DATA/hermes — so we
    probe both spellings (volume path first: tests point DATA elsewhere, and it survives HOME
    differing from the boot shell's)."""
    return [
        os.path.join(DATA, "hermes", "platforms", "whatsapp", "session", "creds.json"),
        os.path.expanduser("~/.hermes/platforms/whatsapp/session/creds.json"),
    ]


def _whatsapp_status() -> str:
    """WhatsApp linked-state, honestly scoped: "linked" means session creds exist on disk, i.e. this
    volume was EVER linked — NOT that the gateway is up and the channel is currently deliverable
    (creds.json survives a revoked device, a dead gateway and an unpaired phone). It is a positive
    on-disk probe, which is enough for the tile and the completion gate to stop celebrating over a
    never-scanned channel; a real liveness probe is a separate piece of work (ROADMAP, batch-later).
    Else a live QR mirror file → "pairing" (mid-flight); else "unknown" (never linked on this
    volume). creds win over a lingering QR file: the mirror is removed by wa_pair.py within seconds
    of the scan, and start.sh itself treats creds-present as paired while pairing is still open."""
    for p in _wa_creds_paths():
        if os.path.exists(p):
            return "linked"
    return "pairing" if os.path.exists(QR_FILE) else "unknown"


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
HERMES_VERSION_FILENAME = "hermes-version.json"   # written by start.sh on every boot


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


def hermes_versions() -> dict:
    """{running, image} exactly as start.sh's boot line reports them — it writes the pair to the
    cache on every boot. Empty strings when the file isn't there (a run without start.sh)."""
    out = {"running": "", "image": ""}
    try:
        with open(_cache_file(HERMES_VERSION_FILENAME), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return out
    if isinstance(raw, dict):
        for k in out:
            v = raw.get(k)
            if isinstance(v, str):
                out[k] = v.strip()
    return out


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
    gok, gmsg = google_connected()
    client_present = os.path.exists(os.path.expanduser("~/.hermes/google_client_secret.json"))
    tz = _configured_tz_name()   # SOTTO_TIMEZONE → TZ → settings.json (→ server local)
    return {
        "bridge_connected": RELAY.bridge_connected(),
        "google_connected": gok,
        "google_detail": gmsg,
        "google_client_present": client_present,
        "timezone": tz,
        "whatsapp": _whatsapp_status(),
        "connectors": CONNECTORS.service_status(),
        "last_event_at": _last_event_at(),
        "update": update_status(),
        "hermes": hermes_versions(),
    }


def _tile(num: int, title: str, state: str, body: str) -> str:
    """One wizard tile in the shared markup contract (styled by /static/setup.css): data-state is
    done|todo|optional; the visible state label spells 'to do' out."""
    label = {"done": "done", "todo": "to do", "optional": "optional"}[state]
    return (f"<section class='tile' data-state='{state}'>"
            f"<div class='tile-head'><span class='tile-num'>{num}</span>"
            f"<h2 class='tile-title'>{title}</h2>"
            f"<span class='tile-state'>{label}</span></div>"
            f"<div class='tile-body'>{body}</div></section>")


def _setup_page(code: str = "") -> str:
    """The Connections view of the site — same shell/nav as the /app dashboard, so the bookmarked
    /setup entry point IS the product. Renders live status for each step (Mac · Google · WhatsApp ·
    Timezone) and only the next action you need — no jumping between four URLs or the Railway
    dashboard. Google loads LIVE (paste client → authorize → paste code), so Google needs zero
    Railway vars and zero redeploys. Timezone auto-detects from the browser. Once steps 1–4 are all
    done, a hero CTA hands you to /app (the wizard's job is over). Gated behind the setup code (the
    pairing link on this page carries the MCP bearer); internal links re-carry `?code=` so one
    authentication covers the whole wizard even if the `sotto_setup` cookie is blocked."""
    import html as _html
    qs = f"?code={urllib.parse.quote(code)}" if code else ""
    st = setup_status()
    link = pairing_link()
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
    if st["bridge_connected"]:
        mac = ("<p class='tile-status'>Your Mac is linked and reachable. "
               "(Grant Full Disk Access in the app if you haven't.)</p>" + ev_line)
    elif not RAILWAY_DOMAIN or not MCP_TOKEN:
        # An empty host or token would render a dead sotto-bridge://pair?host=&token= link — name
        # what's missing instead of handing out a pairing code that can't pair.
        missing = " and ".join(
            m for m, absent in (
                ("a bearer token — set <code>BRIDGE_TOKEN</code> in Railway", not MCP_TOKEN),
                ("a public domain — Railway → Networking → Generate Domain", not RAILWAY_DOMAIN),
            ) if absent)
        mac = (f"<p class='tile-status'>Pairing isn't ready yet — this deploy still needs "
               f"{missing}.</p>"
               "<p class='tile-hint'>See RAILWAY.md for the full setup, then reload this page.</p>")
    else:
        mac = (f"<p><a class='btn-primary' href='{el}'>Open in Sotto Bridge →</a></p>"
               "<p class='tile-hint'>Not on this Mac? Copy this and paste it into the app's "
               "“Paste pairing link” field:</p>"
               f"<p><span class='code code-block' id='c'>{el}</span> <button class='btn-quiet' "
               "onclick=\"navigator.clipboard.writeText(document.getElementById('c').innerText)\">Copy</button></p>"
               "<p class='tile-hint'>Then grant <b>Full Disk Access</b> in the app so Sotto can read Messages.</p>")

    # 2 · Google
    if st["google_connected"]:
        google = "<p class='tile-status'>Gmail + Calendar connected.</p>"
    elif not st["google_client_present"]:
        google = (
            "<p>One-time Google Cloud setup (~2 min), then paste the client JSON below. "
            "No Railway variable, no redeploy:</p>"
            "<ol class='tile-hint'>"
            "<li><a href='https://console.cloud.google.com' target='_blank'>console.cloud.google.com</a> → "
            "create (or pick) a project → enable the <b>Gmail API</b> and the <b>Google Calendar API</b>.</li>"
            "<li><b>OAuth consent screen</b> → External → publish to <b>In production</b> "
            "(left in Testing, your token expires after ~7 days; no Google review is needed for your own data).</li>"
            "<li><b>Create credentials → OAuth client ID → Desktop app → Download JSON</b>.</li>"
            "<li>Paste that JSON here:</li></ol>"
            f"<form action='/setup/google-client{qs}' method='post'>"
            "<textarea name='client_json' rows='4' class='field' placeholder='{\"installed\":{...}}'></textarea>"
            "<p><button class='btn-primary'>Save client →</button></p></form>")
    else:
        google = (f"<p><a class='btn-primary' href='/google/auth{qs}'>Authorize Gmail + Calendar →</a> "
                  "<span class='tile-hint'>(then paste the code on that page)</span></p>")

    # 3 · WhatsApp — the tile turns "done" only on the POSITIVE probe (session creds on disk, see
    # _whatsapp_status). "unknown" (never linked) keeps the QR button and stays 'to do'.
    if st["whatsapp"] == "linked":
        wa = "<p class='tile-status'>WhatsApp is linked — briefs deliver to your number.</p>"
    elif st["whatsapp"] == "pairing":
        wa = ("<p class='tile-status'>Pairing in progress — "
              f"<a href='/whatsapp/qr{qs}'>open the QR</a> and scan with your phone.</p>")
    else:
        wa = (f"<p><a class='btn-primary' href='/whatsapp/qr{qs}'>Show WhatsApp QR →</a> <span class='tile-hint'>"
              "(WhatsApp ▸ Linked Devices ▸ Link a Device — scan with your phone)</span></p>")

    # 4 · Timezone (auto-detected by the browser; posted once)
    tzv = _html.escape(st["timezone"])
    tz_block = (f"<p class='tile-status'>Timezone set to <b>{tzv}</b> — your briefs will fire at your local 6:30 am / 5:30 pm.</p>"
                if st["timezone"] else
                "<p class='tile-status' id='tzmsg'>Detecting your timezone…</p>")
    tz_js = "" if st["timezone"] else (
        "<script>(function(){try{var tz=Intl.DateTimeFormat().resolvedOptions().timeZone;"
        f"if(!tz){{return;}}fetch('/setup/timezone{qs}',{{method:'POST',headers:{{'Content-Type':'application/json'}},"
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
                  "headers:{'Content-Type':'application/json'},body:JSON.stringify({service:s})})"
                  ".then(function(){location.reload();});return false;}</script>")

    # Steps 1–4 all done (tile 5 is optional and never gates): the wizard's job is finished, so the
    # page's FIRST affordance becomes the handoff to the dashboard. The delivery step uses the SAME
    # rule the valve and the meeting tap use (_delivery_channel_ready): on WhatsApp it demands the
    # POSITIVE linked state — "unknown"/never-scanned must not celebrate over a dead delivery
    # channel — and on any other SOTTO_CRON_DELIVER there is nothing to probe, so it never blocks a
    # Telegram user's wizard from finishing.
    done = (st["bridge_connected"] and st["google_connected"] and bool(st["timezone"])
            and _delivery_ready(st["whatsapp"]))
    hero = "<a class='hero-cta' href='/app'>Open your dashboard →</a>" if done else ""
    footer = ("<p class='page-sub'>You're connected. Message yourself on WhatsApp: "
              "<b>“Sotto, give me my morning brief.”</b> Briefs also fire automatically at 6:30 am / 5:30 pm.</p>"
              if done else
              "<p class='tile-hint'>Finish the steps above, then "
              f"<a href='/setup{qs}'>recheck</a>. Briefs deliver once your Mac is linked, Google is connected, and a timezone is set.</p>")

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
    hv = st.get("hermes") or {}
    hermes_line = ""
    if hv.get("running") or hv.get("image"):
        run_v = _html.escape(str(hv.get("running") or "unknown"))
        img_v = _html.escape(str(hv.get("image") or "unknown"))
        # Same drift the boot log warns about: the volume's seeded Hermes shadowing a newer image.
        drift = ("" if run_v == img_v else
                 " Your volume is behind this image — set <code>SOTTO_REFRESH_HERMES=1</code> and "
                 f"redeploy once to adopt it (<a href='{UPDATE_DOC_URL}'>details</a>).")
        # Worded as the boot log words it — one fact, two places to read it.
        hermes_line = (f"<p class='tile-meta'>Hermes running: {run_v} · image built with: "
                       f"{img_v}.{drift}</p>")

    return (
        _page_head("Sotto — integrations", body_class="setup")
        + "<div class='site'>"
        + _nav()
        + "<main class='content'>"
        "<div class='eyebrow'>Integrations</div>"
        "<h1 class='page-title'>What Sotto connects to</h1>"
        "<p class='page-sub'>Your agent is live. Everything connects on this page — the last step is optional.</p>"
        f"{hero}"
        + _tile(1, "Link your Mac", "done" if st["bridge_connected"] else "todo", mac)
        + _tile(2, "Connect Google", "done" if st["google_connected"] else "todo", google)
        + _tile(3, "Link WhatsApp", "done" if st["whatsapp"] == "linked" else "todo", wa)
        + _tile(4, "Timezone", "done" if st["timezone"] else "todo", tz_block + tz_js)
        + _tile(5, "Connected services", "done" if svc_connected else "optional", services)
        + f"{footer}"
        f"<p class='tile-meta'>Host: <code>{_html.escape(host)}</code></p>"
        f"{update_line}{hermes_line}"
        "</main></div></body></html>"
    )


# (/pair is a legacy path: it 302-redirects to /setup in do_GET — the old standalone pair page
# is gone; the deep link + copyable pairing code live in _setup_page.)

# Setup/pairing/debug-status surface — everything here can leak the MCP bearer (pairing link), the
# live WhatsApp QR, or accept config writes, so it's gated behind the setup code (see resolve_setup_code).
SETUP_GET_PATHS = frozenset({"/setup", "/pair", "/google/auth", "/google/submit-code",
                             "/whatsapp/qr", "/debug/google"})
SETUP_POST_PATHS = frozenset({"/setup/timezone", "/setup/google-client", "/setup/disconnect"})

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

    def _security_headers(self, ctype=None):
        """The header set every response out of this handler carries — the same three the dashboard
        sends on its own responses, plus the setup CSP on HTML (see SETUP_CSP). One place, so a new
        page cannot be born unprotected."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        # Unconditional: this server has NO cacheable route. Every path is the pairing link (= the
        # bearer), an OAuth code exchange, the live WhatsApp QR, brief diagnostics, or health —
        # none of it may sit in a browser's disk cache or a shared proxy. (/static/* is the one
        # cacheable surface in the image and it belongs to dashboard.py, which sets its own.)
        self.send_header("Cache-Control", "no-store")
        if ctype and ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", SETUP_CSP)

    def _grant_header(self):
        """Emit the authenticate-once wizard cookie if this request just presented a valid ?code=."""
        granted = getattr(self, "_grant_cookie", None)
        if granted:
            self.send_header("Set-Cookie", f"sotto_setup={granted}; {SETUP_COOKIE_ATTRS}")
            self._grant_cookie = None

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
            CONNECTORS.consume_pending(state)   # burn the state; a denied flow must not linger
            desc = (q.get("error_description") or [""])[0]
            return self._html(400, _connect_error_page(
                "authorization", f"the provider returned '{err}'" + (f": {desc}" if desc else "")))
        if not state or not code_:
            return self._html(400, _connect_error_page(
                "state", "missing code/state in the callback — restart from /setup"))
        try:
            record = CONNECTORS.finish_auth(state, code_, connect_redirect_uri())
        except CONNECTORS.ConnectorError as e:
            code = 400 if e.step == "state" else 502
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
        # Healthcheck for the platform (Railway healthcheckPath=/health → restart on failure).
        if path == "/health":
            return self._send(200, {"status": "ok", "bridge_connected": RELAY.bridge_connected()})
        # A human hitting the bare service URL (Railway shows it prominently). Point at the setup
        # flow WITHOUT the code or link itself — this page is unauthenticated. Other unknown paths
        # keep their JSON 404.
        if path == "/":
            return self._html(200, _page_head("Sotto")
                + "<div class='site'><main class='content narrow'>"
                "<section class='tile'><div class='tile-head'><h2 class='tile-title'>Sotto is running</h2></div>"
                "<div class='tile-body'>"
                "<p>To finish setup, open the <code>[sotto] Setup link</code> line from your Railway "
                "deploy logs — it carries the one-time setup code this page can't show.</p>"
                "<p class='tile-hint'>Have a session already? <a href='/app'>Open your dashboard →</a></p>"
                "</div></section></main></div></body></html>")
        if path == "/favicon.ico":   # browsers request it on every page load; 204 beats a JSON 404
            return self._write(204, None, None)
        # The Window (read-only dashboard): sessions, static assets, JSON API — all in dashboard.py.
        if DASHBOARD.owns(path):
            return DASHBOARD.handle(self, "GET", path)
        # Reverse-MCP: the Bridge long-polls here for the next tool call (held open ~25s).
        if path == "/bridge/poll":
            if not self._authed(MCP_TOKEN):
                return self._send(401, {"error": "unauthorized"})
            req = RELAY.poll(timeout=25.0)
            return self._send(200 if req else 204, req or {})
        # (No /bridge/status: it was an unauthenticated leak of Mac presence with zero clients —
        # /health already carries `bridge_connected`.)
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
            return self._write(200, "text/plain; charset=utf-8", body.encode())
        # Connector OAuth callback — the IdP's browser redirect lands here, so it is NOT setup-code
        # gated: the single-use `state` minted by the gated /connect/<service>/start is the auth.
        if path == "/connect/oauth/callback":
            return self._connect_callback()
        connect_start = CONNECT_START_RE.match(path)
        # Everything below is the setup/pairing/debug-status surface: it can expose the MCP bearer
        # (pairing link) or the live WhatsApp QR, so it requires the setup code / cookie / bearer.
        # /connect/<service>/start is gated too (it spends discovery/DCR effort and mints flow state).
        if (path in SETUP_GET_PATHS or connect_start) and not self._setup_authed():
            return self._forbid_setup()
        qs = (f"?code={urllib.parse.quote(resolve_setup_code())}"
              if (path in SETUP_GET_PATHS or connect_start) else "")
        # Kick off a connector OAuth flow: discovery → DCR → pending state → 302 to the consent page.
        if connect_start:
            return self._connect_start(connect_start.group(1))
        # Google connection status — the one-glance answer to "why is Gmail/Calendar missing?" Google
        # is server-side (independent of the Bridge), so a cron brief gets it iff this says connected.
        if path == "/debug/google":
            ok, msg = google_connected()
            return self._send(200 if ok else 503, {"google_connected": ok, "detail": msg,
                                                    "authorize": "/google/auth"})
        # The unified setup wizard: live status for Mac · Google · WhatsApp · Timezone, next action inline.
        if path == "/setup":
            return self._html(200, _setup_page(resolve_setup_code()))
        # (No /setup/status: it was a JSON twin of /setup with no caller. setup_status() itself
        # stays — it is what _setup_page renders from.)
        # Legacy /pair → the wizard (keeps old links/QRs working; the deep link itself is in the page).
        if path == "/pair":
            return self._redirect(f"/setup{qs}")
        # Live Google code exchange (no Railway redeploy). The /google/auth form posts the code here.
        if path == "/google/submit-code":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            ok, msg = exchange_google_code((q.get("code") or [""])[0])
            import html as _html
            badge = "Google connected" if ok else "Not connected"
            extra = "" if ok else ("<p class='tile-hint'>Fallback: set <code>GOOGLE_AUTH_CODE</code> in Railway → Variables and "
                                   f"redeploy. <a href='/google/auth{qs}'>← back</a></p>")
            return self._html(200 if ok else 400, _narrow_page(
                "Sotto — connect Google",
                f"<section class='tile'><div class='tile-head'><h2 class='tile-title'>{badge}</h2></div>"
                f"<div class='tile-body'><p>{_html.escape(msg)}</p>{extra}</div></section>",
                back_href=f"/setup{qs}"))
        # Google Workspace authorization page: a clickable auth URL + the copy-the-code instructions.
        # The deterministic flow lives in start.sh; this just presents the one-time URL it generated.
        if path == "/google/auth":
            try:
                url = open(GAUTH_FILE).read().strip()
            except OSError:
                return self._html(200, _narrow_page(
                    "Connect Google",
                    "<section class='tile'><div class='tile-body'>"
                    "<p>No Google authorization pending — already connected, or set "
                    "<code>GOOGLE_OAUTH_CLIENT_JSON</code> in Railway to begin.</p>"
                    "</div></section>",
                    back_href=f"/setup{qs}"))
            import html as _html
            u = _html.escape(url)
            return self._html(200, _narrow_page(
                "Connect Google",
                "<section class='tile'><div class='tile-head'><h2 class='tile-title'>Connect Google to Sotto</h2></div>"
                "<div class='tile-body'>"
                f"<p><a class='btn-primary' href='{u}' target='_blank'>1 — Authorize Gmail + Calendar →</a></p>"
                "<p>You'll see an \"unverified app\" screen (it's <i>your</i> client) → <b>Advanced → Continue</b> → <b>Allow</b>.</p>"
                "<p><b>2</b> — You'll land on a <code>localhost:1/?code=…</code> page that won't load. Copy the "
                "<code>code</code> value (everything after <code>code=</code>, before <code>&</code>).</p>"
                "<p><b>3</b> — Paste it here and click <b>Connect</b> — no redeploy needed:</p>"
                "<form action='/google/submit-code' method='get'>"
                # a GET form replaces the action's query string, so the setup code rides along as a
                # hidden field (`code` itself is Google's auth code here).
                f"<input type='hidden' name='setup_code' value='{_html.escape(resolve_setup_code(), quote=True)}'>"
                # off/off/false: phone keyboards otherwise capitalize/"correct" the pasted code
                "<input name='code' class='field' placeholder='paste the code (or the whole localhost URL)' "
                "autocapitalize='off' autocorrect='off' spellcheck='false'> "
                "<button class='btn-primary'>Connect</button></form>"
                "<p class='tile-hint'>Fallback if that fails: set <code>GOOGLE_AUTH_CODE</code> "
                "in Railway → Variables and redeploy.</p>"
                "</div></section>",
                back_href=f"/setup{qs}"))
        # Serve the live WhatsApp pairing output (incl. the QR) with tight line-height so it scans in a
        # browser — Railway's log viewer distorts the terminal QR. Only available during pairing.
        if path != "/whatsapp/qr":
            return self._send(404, {"error": "not found"})
        try:
            # utf-8 explicitly: the QR is block characters, and a container with no LANG set would
            # otherwise read them through an ASCII locale and fail (or mangle) the whole page.
            with open(QR_FILE, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            return self._html(200, _narrow_page(
                "Link WhatsApp",
                "<section class='tile'><div class='tile-body'>"
                "<p>No pairing in progress (already linked, or not started yet).</p>"
                "</div></section>",
                back_href=f"/setup{qs}"))
        import html as _html
        self._html(200, _narrow_page(
            "Scan to link WhatsApp",
            "<section class='tile'><div class='tile-body'>"
            "<p>Open WhatsApp ▸ Linked Devices ▸ Link a Device, then scan. Page auto-refreshes.</p>"
            f"<pre class='qr'>{_html.escape(content)}</pre>"
            "</div></section>",
            head_extra="<meta http-equiv='refresh' content='6'>",
            back_href=f"/setup{qs}"))

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
        # /setup/disconnect — forget a connected service's token (the /setup tile's Disconnect).
        if path == "/setup/disconnect":
            try:
                service = str((json.loads(raw or b"{}") or {}).get("service", ""))
            except (json.JSONDecodeError, ValueError):
                service = ""
            if service not in CONNECTORS.SERVICES:
                return self._send(400, {"ok": False, "detail": f"unknown service '{service}' — "
                                        "known: " + ", ".join(sorted(CONNECTORS.SERVICES))})
            res = CONNECTORS.disconnect(service)
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
        qs = f"?code={urllib.parse.quote(resolve_setup_code())}"
        more = (f"<p><a class='btn-primary' href='/google/auth{qs}'>Authorize Google →</a></p>"
                if ok else "")
        return self._html(200 if ok else 400, _narrow_page(
            "Sotto — Google client",
            f"<section class='tile'><div class='tile-head'><h2 class='tile-title'>"
            f"{_html.escape(msg)}</h2></div>"
            f"<div class='tile-body'>{more}</div></section>",
            back_href=f"/setup{qs}"))

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        # Dashboard POSTs (login + the M2 write API) — dashboard.py owns auth/CSRF/lockout.
        if DASHBOARD.owns(path):
            return DASHBOARD.handle(self, "POST", path)
        if path in SETUP_POST_PATHS:
            if not self._setup_authed():
                return self._forbid_setup()
            return self._handle_setup_post(path)
        if path not in ("/sotto/trigger", "/mcp", "/bridge/respond", "/bridge/events"):
            return self._send(404, {"error": "not found"})
        token = MCP_TOKEN if path in ("/mcp", "/bridge/respond", "/bridge/events") else TOKEN
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
        # Event-driven ingestion: raw watcher events → dedupe → triage → verdict (maybe agent spawn).
        if path == "/bridge/events":
            code, resp = handle_events(body)
            return self._send(code, resp)
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
    # Email events (Phase 2): daemon poll thread, gated on google-configured inside the thread.
    start_gmail_poll_thread()
    # Deferred-queue release valve (Step 2 item 3): heartbeat thread, channel-health gated per tick.
    start_valve_thread()
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
