#!/usr/bin/env python3
"""
triage_event.py — Tier 0 + Tier 1 of the event-driven proactivity funnel (Phase 2 §3).

The receiver POSTs raw Bridge/email events here SYNCHRONOUSLY (stdin in, verdict on stdout):

  stdin:  {"events":[{source, rowid, ...}, ...], "catchup": bool}
  stdout: {"verdict":"drop|queue|agent", "reason":"...", "bundle":{...}}

Tier 0 (deterministic, free) — the verdict matrix:
  is_from_me                           → queue (class "signal"; ledger fodder, never a nudge)
  answered / outgoing call             → drop  (no action to take)
  missed call from a KNOWN person      → agent (the interrupt bar) — VIPs only during quiet hours
  missed call from an unknown number   → queue
  OTP / shortcode / system message     → drop  (silent — _is_likely_automated + render_local's filter)
  muted sender / muted person (prefs)  → drop
  quiet hours (everything else)        → queue (class "quiet")
  group without a name-mention         → queue (class "group")
  unknown non-VIP 1:1                  → queue (class "unknown"; never agent)
  survivors (known 1:1 / mentioned group / known email sender) → Tier 1

Tier 1 (one small LLM call per survivor): SOTTO_TRIAGE_MODEL (default "gemini-3.5-flash-lite") with a
tight prompt (event text + a sender one-liner from the graph/pulse, ≤ 2k tokens) → strict JSON
{"class":"urgent|actionable|scheduling_ask|ambient|ignore","why"}. urgent|actionable|scheduling_ask →
agent (scheduling_ask additionally tells the sotto-event skill to gather the calendar and propose
slots); ambient → queue; ignore → drop. ANY Tier-1 error → queue (fail toward silence, never toward
noise).

Cooldown: one agent verdict per thread key per SOTTO_EVENT_COOLDOWN_MIN (default 20 min), persisted in
$SOTTO_DATA/events/cooldowns.json; a suppressed event queues (class "cooldown").

Queue: $SOTTO_DATA/events/queue.jsonl — one {ts, verdict_class, sender, event} per line (bounded via
sotto_log.bounded_append). Entries demoted from an agent verdict (cooldown/quiet/catchup-stale) also
carry `held_class` — the Tier-0/1 class the event earned BEFORE demotion, so the release valve and
digest can tell "was interrupt-worthy, deterministically held" from born-ambient. digest_check.py
consumes it; the release valve (below) promotes from it; brief composers may later.

Surfaced ledger: $SOTTO_DATA/events/surfaced.jsonl — ONE line per triaged event at verdict time:
{ts, sender, channel, verdict: agent|queue|drop|promoted, reason, class}. Bounded like the queue.
This is the diagnostic substrate for "why didn't I get nudged?" — the dashboard's Record view
(GET /api/ledger, source "triage") renders it, and the learning loop's surfaced/outcome join reads it.

Release valve (`triage_event.py --valve`, invoked by the receiver's heartbeat thread): when no hold
is active (not quiet hours), promote up to VALVE_MAX_PER_TICK queued KNOWN-sender events that were
demoted for cooldown/quiet/catchup reasons and are younger than SOTTO_VALVE_MAX_AGE_MIN (default 4h),
respecting the per-thread cooldown and an hourly budget (SOTTO_VALVE_MAX_PER_HOUR, default 2).
Promotion returns the same {"verdict":"agent","bundle":…} shape triage() does, so the receiver routes
it through the identical sotto-event nudge path; promoted entries leave the queue and land in
surfaced.jsonl as verdict "promoted". SOTTO_VALVE=0 disables. This is the fix for the audit's worst
failure: an actionable event arriving during cooldown/quiet/catchup NEVER nudged — silently deferred
until digest (needs 8+ signals) or the evening brief.

Sender resolution reuses the brief's own machinery over the cached local snapshot
($SOTTO_DATA/knowledge/last_local_snapshot.json — compose_brief._save_local_snapshot writes it):
contacts → build_contact_lookup → resolve_*_name, plus relationship_state.json for the pulse.

VIP heuristic (deliberately simple, documented): a sender is VIP when (a) their attention-queue entry
in relationship_state.json has priority >= SOTTO_VIP_PRIORITY (default 10 — the pulse's priority is
interactions × days-waiting × type-weight, so 10+ means a top-of-queue relationship), or (b) their
knowledge-graph person file mentions "family" (family clears the quiet-hours bar for missed calls).

Quiet hours: the same SOTTO_QUIET_START/END + SOTTO_TIMEZONE semantics as proactive_scan.py (the
3-line wrap-midnight rule is ported, not imported — proactive/ isn't importable from this skill dir).

Env: SOTTO_DATA, SOTTO_TIMEZONE, SOTTO_QUIET_START/END (default 21/7), SOTTO_TRIAGE_MODEL,
     SOTTO_EVENT_COOLDOWN_MIN (default 20), SOTTO_VIP_PRIORITY (default 10),
     SOTTO_USER_NAME (group name-mention detection; unset → groups always queue), GOOGLE_AI_API_KEY,
     SOTTO_VALVE (0 disables the release valve), SOTTO_VALVE_MAX_PER_HOUR (default 2),
     SOTTO_VALVE_MAX_AGE_MIN (promotion window, default 240 — a real ask from 3h ago still deserves
     a nudge; a 2-day-old one doesn't).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

# Same cross-skill reuse pattern as proactive_scan/followup_cron: put _shared on sys.path and import
# the brief's own helpers, so triage and the brief agree on "automated", "system message", "known".
_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED_LIB = os.path.join(_HERE, "..", "..", "_shared", "lib")
_SHARED_SCRIPTS = os.path.join(_HERE, "..", "..", "_shared", "scripts")
for _p in (_SHARED_LIB, _SHARED_SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from textutil import (  # noqa: E402
    _s, _is_likely_automated, _looks_like_phone_number, _sender_addr, _extract_sender_name,
)
from render_local import (  # noqa: E402
    _is_system_message, build_contact_lookup, resolve_imessage_name, resolve_whatsapp_name,
    resolve_call_name,
)
from timeutil import _now_local, _parse_ts, configured_tz  # noqa: E402
import gemini as _gemini  # noqa: E402  (module-level so tests can stub _gemini_once)
import preferences as _prefs  # noqa: E402

# Bounds for the ambient queue file (rotate-keeping-tail, same mechanism as compose_brief.log).
QUEUE_MAX_BYTES = 4 * 1024 * 1024
QUEUE_KEEP_LINES = 4000
# The surfaced ledger mirrors the queue's bounds — same volume, same growth profile.
SURFACED_MAX_BYTES = 4 * 1024 * 1024
SURFACED_KEEP_LINES = 4000
COOLDOWN_PRUNE_SECS = 24 * 3600   # cooldown entries older than a day are dead weight
TIER1_TEXT_MAX = 1500             # chars of event text sent to Tier 1 (keeps the prompt ≤ 2k tokens)
# Release valve: ≤ this many promotions per tick, deterministically-deferred classes only.
VALVE_MAX_PER_TICK = 2
PROMOTABLE_CLASSES = frozenset({"quiet", "cooldown", "stale"})


def _data_root() -> str:
    return os.environ.get("SOTTO_DATA", "/data")


def _events_dir() -> str:
    return os.path.join(_data_root(), "events")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _in_quiet_hours(now_local) -> bool:
    """Port of proactive_scan.py's quiet-hours rule (same envs, same wrap-midnight semantics).
    Ported rather than imported — skill dirs aren't packages, and proactive/ isn't on sys.path."""
    quiet_start = _int_env("SOTTO_QUIET_START", 21)   # 9pm
    quiet_end = _int_env("SOTTO_QUIET_END", 7)        # 7am
    h = now_local.hour
    return (h >= quiet_start or h < quiet_end) if quiet_start > quiet_end else (quiet_start <= h < quiet_end)


# ── Cached-state loaders (all best-effort; a missing file means "know nothing", never a crash) ──────

def _load_snapshot_local() -> dict:
    """The brief's cached LocalData (compose_brief._save_local_snapshot → last_local_snapshot.json).
    Source of contacts (name resolution) and person_knowledge (Tier-1 one-liners)."""
    try:
        path = os.path.join(_data_root(), "knowledge", "last_local_snapshot.json")
        with open(path, encoding="utf-8") as f:
            local = (json.load(f) or {}).get("local") or {}
        return local if isinstance(local, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _load_relationship_state() -> dict:
    try:
        path = os.path.join(_data_root(), "knowledge", "relationship_state.json")
        with open(path, encoding="utf-8") as f:
            state = json.load(f) or {}
        return state if isinstance(state, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _load_prefs() -> dict:
    try:
        return _prefs.load_explicit()
    except Exception:  # noqa: BLE001
        return {"mute_senders": [], "mute_people": [], "mute_sections": [], "tone_notes": []}


# ── Tier-0 primitives ──────────────────────────────────────────────────────────────────────────────

_OTP_RE = re.compile(r"\b(verification code|one[- ]?time (?:pass)?code|security code|login code|"
                     r"2fa code|otp)\b", re.I)


def _looks_like_otp(text: str) -> bool:
    """OTP/2FA blast: the canonical phrasing plus an actual 4-8 digit code in the body."""
    t = _s(text)
    return bool(_OTP_RE.search(t)) and bool(re.search(r"\b\d{4,8}\b", t))


def _is_shortcode(handle: str) -> bool:
    """SMS shortcode sender (5-6 digit 'number') — always automated, never a person."""
    d = _s(handle).strip()
    return d.isdigit() and 3 <= len(d) <= 6


def _event_text(e: dict) -> str:
    return _s(e.get("text")) or _s(e.get("body"))


def _is_missed_call(e: dict) -> bool:
    return not e.get("is_outgoing") and not e.get("is_answered")


def _is_group(e: dict) -> bool:
    return bool(e.get("is_group_chat") or _s(e.get("chat_guid"))
                or _s(e.get("contact_jid")).endswith("@g.us"))


def _thread_key(e: dict) -> str:
    """Stable per-conversation key for the agent cooldown: group id when present, else the 1:1
    identifier, else (source,rowid) so an unkeyable event still cools down against itself."""
    guid = _s(e.get("chat_guid"))
    if guid:
        return f"group:{guid}"
    src = _s(e.get("source"))
    for k in ("handle", "contact_jid", "phone", "threadId"):
        v = _s(e.get(k))
        if v:
            return f"{src}:{v.lower()}"
    return f"{src}:{_s(e.get('rowid'))}"


def _resolve_sender(e: dict, lookup: dict) -> tuple[str, str]:
    """(display name, raw identifier) via the brief's own resolvers over the snapshot's contacts."""
    src = _s(e.get("source"))
    if src == "email":
        frm = _s(e.get("from"))
        addr = _sender_addr(frm)
        name = (lookup.get(addr) if addr else "") or _extract_sender_name(frm)
        return (name or addr or "Unknown"), addr
    if src == "whatsapp":
        jid = _s(e.get("sender_jid")) or _s(e.get("contact_jid"))
        return resolve_whatsapp_name(jid, _s(e.get("partner_name")), lookup), jid
    if src == "calls":
        phone = _s(e.get("phone"))
        return (resolve_call_name(phone, lookup) or ""), phone
    handle = _s(e.get("handle"))
    return resolve_imessage_name(handle, lookup), handle


def _is_known_name(name: str) -> bool:
    """Known = Contacts (or WhatsApp push name) resolved a real human name — same standard the
    brief's _thread_is_known_person applies to a 1:1 thread."""
    return bool(name) and name != "Unknown" and not _looks_like_phone_number(name)


def _graph_knows(name: str, ident: str) -> bool:
    """Does the knowledge graph have a person file for this sender? (best-effort)"""
    try:
        import knowledge  # noqa: PLC0415  (_shared/lib, already on sys.path)
        return bool(knowledge.find_person_file(name=name or "", identifier=ident or ""))
    except Exception:  # noqa: BLE001
        return False


def _is_vip(name: str, ident: str, rel_state: dict) -> bool:
    """VIP heuristic (simple, documented — see module docstring): a top-of-queue attention_queue
    priority (>= SOTTO_VIP_PRIORITY, default 10), or a 'family' mention in their graph file."""
    n = _s(name).strip().lower()
    if not n:
        return False
    try:
        vip_min = float(os.environ.get("SOTTO_VIP_PRIORITY", "").strip() or 10)
    except ValueError:
        vip_min = 10.0
    for q in (rel_state.get("attention_queue") or []):
        if _s(q.get("display_name")).strip().lower() == n:
            try:
                if float(q.get("priority") or 0) >= vip_min:
                    return True
            except (TypeError, ValueError):
                pass
    try:
        import knowledge  # noqa: PLC0415
        path = knowledge.find_person_file(name=name, identifier=ident or "")
        if path:
            with open(path, encoding="utf-8") as f:
                if re.search(r"\bfamily\b", f.read(), re.I):
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _mentions_user(text: str) -> bool:
    """Group name-mention: the user's first name (SOTTO_USER_NAME), bare or @-prefixed. Unset →
    never a mention, so groups always queue (the conservative default)."""
    uname = (os.environ.get("SOTTO_USER_NAME") or "").strip()
    if not uname or not text:
        return False
    first = uname.split()[0]
    return bool(re.search(rf"@?\b{re.escape(first)}\b", text, re.I))


# ── Cooldown (one agent verdict per thread per window) ─────────────────────────────────────────────

def _cooldown_path() -> str:
    return os.path.join(_events_dir(), "cooldowns.json")


def _load_cooldowns() -> dict:
    try:
        with open(_cooldown_path(), encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _cooldown_ok(key: str, now_ts: float) -> bool:
    window = _int_env("SOTTO_EVENT_COOLDOWN_MIN", 20) * 60
    last = _load_cooldowns().get(key)
    try:
        return last is None or (now_ts - float(last)) >= window
    except (TypeError, ValueError):
        return True


def _stamp_cooldown(key: str, now_ts: float) -> None:
    try:
        cd = {k: v for k, v in _load_cooldowns().items()
              if isinstance(v, (int, float)) and (now_ts - v) < COOLDOWN_PRUNE_SECS}
        cd[key] = now_ts
        os.makedirs(_events_dir(), exist_ok=True)
        tmp = _cooldown_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cd, f)
        os.replace(tmp, _cooldown_path())
    except OSError:
        pass


# ── Queue ──────────────────────────────────────────────────────────────────────────────────────────

def _queue_path() -> str:
    return os.path.join(_events_dir(), "queue.jsonl")


def _append_queue(verdict_class: str, sender: str, event: dict, held_class: str = "") -> None:
    """One JSONL line per queued event. Bounded (rotate-keeping-tail) so the file can't grow forever
    on the /data volume. `held_class` (only on entries demoted FROM an agent verdict) preserves the
    class the event earned before cooldown/quiet/catchup demotion — the release valve and the
    sotto-event skill use it so a demoted scheduling_ask still gets the scheduling treatment when
    promoted. Best-effort — a failed append must never fail the triage."""
    try:
        from sotto_log import bounded_append  # noqa: PLC0415
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "verdict_class": verdict_class,
            "sender": sender,
            "event": event,
        }
        if held_class:
            entry["held_class"] = held_class
        bounded_append(_queue_path(), json.dumps(entry), QUEUE_MAX_BYTES, QUEUE_KEEP_LINES)
    except Exception:  # noqa: BLE001
        pass


# ── Surfaced ledger (one line per verdict — the "why didn't I get nudged?" substrate) ─────────────

def _surfaced_path() -> str:
    return os.path.join(_events_dir(), "surfaced.jsonl")


def _record_surfaced(verdict: str, cls: str, reason: str, sender: str, event: dict) -> None:
    """Append one {ts, sender, channel, verdict, reason, class} line to surfaced.jsonl at verdict
    time. ts uses the same ISO-Z format the dashboard's /api/ledger parses, so The Record renders
    these rows as-is. Best-effort — recording must never fail (or slow) the triage."""
    try:
        from sotto_log import bounded_append  # noqa: PLC0415
        ev = event if isinstance(event, dict) else {}
        line = json.dumps({
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sender": _s(sender) or _s(ev.get("from")) or _s(ev.get("handle"))
                      or _s(ev.get("contact_jid")) or _s(ev.get("phone")) or "",
            "channel": _s(ev.get("source")),
            "verdict": verdict,
            "reason": _s(reason)[:300],
            "class": cls,
        })
        bounded_append(_surfaced_path(), line, SURFACED_MAX_BYTES, SURFACED_KEEP_LINES)
    except Exception:  # noqa: BLE001
        pass


# ── Tier 1 ─────────────────────────────────────────────────────────────────────────────────────────

def _sender_one_liner(name: str, ident: str, snapshot_local: dict, rel_state: dict) -> str:
    """One line of who-this-is for the Tier-1 prompt: name + their pulse status + their packed graph
    head line, whatever exists. Hard-capped so the prompt stays tiny."""
    bits = [name or ident or "unknown sender"]
    n = _s(name).strip().lower()
    for q in (rel_state.get("attention_queue") or []):
        if n and _s(q.get("display_name")).strip().lower() == n:
            bits.append(f"{_s(q.get('queue_type'))}: {_s(q.get('reason'))}")
            break
    pk = snapshot_local.get("person_knowledge")
    if n and isinstance(pk, dict):
        for packed in pk.values():
            head = _s(packed).split("\n", 1)[0]
            if head.lower().startswith(n):
                bits.append(head)
                break
    return " | ".join(b for b in bits if b)[:300]


def _classify_tier1(e: dict, one_liner: str) -> tuple[str, str, str]:
    """One Flash-Lite call → (verdict, class, reason). Raises on ANY problem; the caller maps every
    raise to queue (fail toward silence)."""
    model = os.environ.get("SOTTO_TRIAGE_MODEL", "gemini-3.5-flash-lite")
    key = os.environ.get("GOOGLE_AI_API_KEY") or ""
    if not key:
        raise RuntimeError("GOOGLE_AI_API_KEY not set")
    group_note = " (group chat — the user was mentioned by name)" if _is_group(e) else ""
    prompt = (
        'You are the triage layer of a personal chief-of-staff. Classify ONE inbound event.\n'
        'Respond with STRICT JSON only: {"class":"urgent|actionable|scheduling_ask|ambient|ignore","why":"<one short sentence>"}\n'
        "Definitions:\n"
        "- urgent: time-sensitive or a direct ask from someone who matters — worth interrupting the user now\n"
        "- actionable: a real ask/commitment, but it can wait for a nudge\n"
        '- scheduling_ask: a direct request to find time to meet or talk ("can we do coffee Thursday?",'
        ' "got 30 min next week?") — nudge-worthy; the agent will propose real slots from the calendar\n'
        "- ambient: FYI, social chatter, scheduling noise that asks nothing — batch it into a digest\n"
        "- ignore: automated or no-signal noise\n"
        f"Sender: {one_liner}\n"
        f"Channel: {_s(e.get('source'))}{group_note}\n"
        f"Subject: {_s(e.get('subject'))[:200]}\n"
        f"Event text:\n{_event_text(e)[:TIER1_TEXT_MAX]}\n"
    )
    raw = _gemini._gemini_once(model, key, prompt, label=" [triage]")
    m = re.search(r"\{.*\}", raw, re.S)   # peel any accidental fencing/prose
    obj = json.loads(m.group(0) if m else raw)
    cls = _s(obj.get("class")).strip().lower()
    why = _s(obj.get("why")).strip()[:200]
    if cls in ("urgent", "actionable", "scheduling_ask"):
        return "agent", cls, why or f"tier1 {cls}"
    if cls == "ambient":
        return "queue", "ambient", why or "tier1 ambient"
    if cls == "ignore":
        return "drop", "ignore", why or "tier1 ignore"
    raise RuntimeError(f"unexpected tier1 class {cls!r}")


# ── The per-event decision ─────────────────────────────────────────────────────────────────────────

def classify_event(e: dict, ctx: dict) -> tuple[str, str, str, str]:
    """(verdict, class, reason, sender_name) for ONE event, per the Tier-0 matrix + Tier 1.
    Cooldown is applied by the caller (it needs disk + a shared stamp across the batch)."""
    src = _s(e.get("source"))
    name, ident = _resolve_sender(e, ctx["lookup"])

    # 1) The user's own outbound — a loop-closing signal for deterministic resolution, never a nudge.
    if e.get("is_from_me"):
        return "queue", "signal", "own outbound message (ledger signal)", name

    # 2) Calls carry no text — decide entirely on direction + who.
    if src == "calls":
        if not _is_missed_call(e):
            return "drop", "call", "answered/outgoing call — nothing to do", name
        if _is_known_name(name):
            if not ctx["quiet"] or _is_vip(name, ident, ctx["rel_state"]):
                return "agent", "missed_call", f"missed call from {name}", name
            return "queue", "quiet", f"missed call from {name} in quiet hours (non-VIP)", name
        return "queue", "missed_call", "missed call from unknown number", name

    text = _event_text(e)

    # 3) Automated noise — the same filters the brief applies before the LLM ever sees a thread.
    if src == "email":
        if _is_likely_automated(ident):
            return "drop", "automated", f"automated sender {ident}", name
    else:
        if _is_system_message(text):
            return "drop", "system", "system/status message", name
        if _looks_like_otp(text) or _is_shortcode(ident.split("@")[0]):
            return "drop", "automated", "OTP/shortcode blast", name

    # 4) Explicit mutes (the sotto-feedback channel) — "stop surfacing X" must stick here too.
    if src == "email" and ident:
        try:
            if _prefs.sender_is_muted(ident, ctx["prefs"]["mute_senders"]):
                return "drop", "muted", f"muted sender {ident}", name
        except Exception:  # noqa: BLE001
            pass
    if name and any(_s(name).strip().lower() == _s(m).strip().lower()
                    for m in ctx["prefs"]["mute_people"]):
        return "drop", "muted", f"muted person {name}", name

    # 5) Quiet hours queue everything that survives the drops (VIP missed calls already handled).
    if ctx["quiet"]:
        return "queue", "quiet", "quiet hours", name

    # 6) Groups only clear the bar when the user is called out by name.
    group = _is_group(e)
    if group and not _mentions_user(text):
        return "queue", "group", "group message without a name-mention", name

    # 7) Unknown non-VIP 1:1 never reaches the agent (VIP requires being known, by construction).
    known = (bool(ctx["lookup"].get(ident)) or _graph_knows(name, ident)) if src == "email" \
        else _is_known_name(name)
    if not known and not group:
        return "queue", "unknown", "unknown sender 1:1", name

    # 8) Nothing to judge (attachment-only etc.) — don't burn a Tier-1 call on empty text.
    if not text.strip():
        return "queue", "ambient", "no text content", name

    # 9) Survivor → Tier 1. ANY error → queue (fail toward silence, never toward noise).
    try:
        one_liner = _sender_one_liner(name, ident, ctx["snapshot"], ctx["rel_state"])
        verdict, cls, reason = _classify_tier1(e, one_liner)
        return verdict, cls, reason, name
    except Exception as err:  # noqa: BLE001
        return "queue", "ambient", f"tier1 error → queue: {err}", name


def _event_age_min(e: dict, now_utc: datetime):
    """Event age in minutes, or None when the timestamp is missing/unparseable (never gate on a
    guess). Naive timestamps are treated as UTC — the Bridge readers emit UTC wall-clock strings."""
    ts = _parse_ts(_s(e.get("timestamp")) or _s(e.get("date")))
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now_utc - ts).total_seconds() / 60.0)


def triage(payload: dict, now_local=None, now_utc=None) -> dict:
    """The whole funnel for one batch: Tier 0 → Tier 1 → cooldown → queue writes → verdict.
    `now_local`/`now_utc` are injectable for tests; production uses the configured timezone."""
    events = [e for e in (payload.get("events") or []) if isinstance(e, dict)]
    if now_local is None:
        now_local = _now_local(configured_tz() or "+00:00")
    ctx = {
        "quiet": _in_quiet_hours(now_local),
        "snapshot": _load_snapshot_local(),
        "rel_state": _load_relationship_state(),
        "prefs": _load_prefs(),
    }
    ctx["lookup"] = build_contact_lookup(ctx["snapshot"].get("contacts")
                                         if isinstance(ctx["snapshot"].get("contacts"), list) else [])
    now_ts = time.time()
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    catchup = bool(payload.get("catchup"))
    max_age = _int_env("SOTTO_EVENT_MAX_AGE_MIN", 30)
    agent_events, reasons, queued = [], [], False
    for e in events:
        verdict, cls, reason, name = classify_event(e, ctx)
        held_class = ""   # set when an agent verdict is demoted — preserved into the queue entry
        # Reconnect grace: after a Bridge gap (Mac asleep/off for hours) the backlog arrives in
        # bursts. A real-time nudge only makes sense for a real-time event — a stale message was
        # probably already handled on another device, so anything older than max_age (or any
        # message in an explicit catchup batch) goes to the queue and surfaces in the digest/next
        # brief instead of a barrage of nudges. Missed calls stay exempt: rare, high-signal, and
        # still worth surfacing hours later.
        if verdict == "agent" and cls != "missed_call":
            age = _event_age_min(e, now_utc)
            if catchup:
                held_class = cls
                verdict, cls, reason = "queue", "stale", f"catchup batch → queued ({name})"
            elif age is not None and age > max_age:
                held_class = cls
                verdict, cls, reason = "queue", "stale", f"{int(age)}m old → queued ({name})"
        if verdict == "agent":
            key = _thread_key(e)
            if not _cooldown_ok(key, now_ts):
                held_class = cls
                verdict, cls, reason = "queue", "cooldown", f"agent suppressed by cooldown ({key})"
            else:
                _stamp_cooldown(key, now_ts)
        if verdict == "queue":
            queued = True
            _append_queue(cls, name, e, held_class=held_class)
        elif verdict == "agent":
            agent_events.append({"event": e, "sender": name, "class": cls, "why": reason})
        _record_surfaced(verdict, cls, reason, name, e)   # the diagnostic ledger, every verdict
        reasons.append(reason)
    overall = "agent" if agent_events else ("queue" if queued else "drop")
    bundle = {}
    if agent_events:
        bundle = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "catchup": bool(payload.get("catchup")),
            "events": agent_events,
        }
    return {"verdict": overall,
            "reason": "; ".join(reasons[:5]) if reasons else "no events",
            "bundle": bundle}


# ── Release valve (the deferred queue's way back to a nudge) ──────────────────────────────────────

def _valve_state_path() -> str:
    return os.path.join(_events_dir(), "valve_state.json")


def _valve_recent(now_ts: float) -> list:
    """Promotion timestamps within the last hour (the budget window). Unreadable → empty."""
    try:
        with open(_valve_state_path(), encoding="utf-8") as f:
            ts_list = (json.load(f) or {}).get("promotions") or []
        return [t for t in ts_list if isinstance(t, (int, float)) and (now_ts - t) < 3600]
    except Exception:  # noqa: BLE001
        return []


def _valve_record(recent: list, n: int, now_ts: float) -> None:
    try:
        os.makedirs(_events_dir(), exist_ok=True)
        tmp = _valve_state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"promotions": recent + [now_ts] * n}, f)
        os.replace(tmp, _valve_state_path())
    except OSError:
        pass


def release_valve(now_local=None, now_utc=None, now_ts=None) -> dict:
    """Promote up to VALVE_MAX_PER_TICK deferred queue entries back into the agent path (module
    docstring has the full contract). Returns the same {"verdict","reason","bundle"} shape triage()
    does so the receiver stages + spawns identically. Eligibility, in order:
      class ∈ PROMOTABLE_CLASSES (demoted-agent verdicts only — never drop-class/born-ambient) →
      KNOWN sender (the resolved name triage wrote) → younger than SOTTO_VALVE_MAX_AGE_MIN →
      per-thread cooldown clear at promotion time (stamped on promotion, so two queue entries from
      one thread can't both promote in a tick).
    Holds: quiet hours (in-meeting joins here once the calendar cache exists) → no promotion.
    Budget: SOTTO_VALVE_MAX_PER_HOUR (default 2) across ticks; SOTTO_VALVE=0 disables entirely.
    Channel health is the CALLER's gate (the receiver's positive WhatsApp probe) — this function
    only decides what deserves promotion."""
    if (os.environ.get("SOTTO_VALVE", "").strip() or "1") == "0":
        return {"verdict": "drop", "reason": "valve disabled (SOTTO_VALVE=0)", "bundle": {}}
    if now_local is None:
        now_local = _now_local(configured_tz() or "+00:00")
    if _in_quiet_hours(now_local):
        return {"verdict": "drop", "reason": "quiet hours hold — nothing promoted", "bundle": {}}
    now_ts = time.time() if now_ts is None else now_ts
    now_utc = datetime.now(timezone.utc) if now_utc is None else now_utc
    recent = _valve_recent(now_ts)
    budget = max(0, _int_env("SOTTO_VALVE_MAX_PER_HOUR", 2) - len(recent))
    cap = min(VALVE_MAX_PER_TICK, budget)
    if cap <= 0:
        return {"verdict": "drop", "reason": "valve budget spent this hour", "bundle": {}}
    try:
        with open(_queue_path(), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    max_age = _int_env("SOTTO_VALVE_MAX_AGE_MIN", 240)
    promoted, promoted_idx = [], set()
    for i, line in enumerate(lines):     # oldest first — FIFO fairness for the longest-held ask
        if len(promoted) >= cap:
            break
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        cls = _s(entry.get("verdict_class"))
        if cls not in PROMOTABLE_CLASSES:
            continue
        sender = _s(entry.get("sender"))
        if not _is_known_name(sender):
            continue
        ev = entry.get("event") if isinstance(entry.get("event"), dict) else None
        if not ev:
            continue
        age = _event_age_min(ev, now_utc)
        if age is None:                  # no event ts → fall back to when it was queued
            qts = _parse_ts(_s(entry.get("ts")))
            if qts is None:
                continue                 # unparseable both ways — never promote on a guess
            if qts.tzinfo is None:
                qts = qts.replace(tzinfo=timezone.utc)
            age = max(0.0, (now_utc - qts).total_seconds() / 60.0)
        if age > max_age:
            continue
        key = _thread_key(ev)
        if not _cooldown_ok(key, now_ts):
            continue
        _stamp_cooldown(key, now_ts)
        held = _s(entry.get("held_class")) or cls
        reason = f"held: {cls}, {int(age)}m old"
        promoted.append({"event": ev, "sender": sender, "class": held,
                         "why": f"promoted from the deferred queue ({reason})"})
        promoted_idx.add(i)
        _record_surfaced("promoted", held, reason, sender, ev)
    if not promoted:
        return {"verdict": "drop", "reason": "nothing promotable in the queue", "bundle": {}}
    try:                                 # promoted entries leave the queue (atomic rewrite)
        rest = [ln for i, ln in enumerate(lines) if i not in promoted_idx]
        tmp = _queue_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(rest)
        os.replace(tmp, _queue_path())
    except OSError:
        pass
    _valve_record(recent, len(promoted), now_ts)
    bundle = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "catchup": False,
        "promoted": True,
        "events": promoted,
    }
    return {"verdict": "agent",
            "reason": "; ".join(p["why"] for p in promoted),
            "bundle": bundle}


def main():
    if "--valve" in sys.argv[1:]:
        try:
            out = release_valve()
        except Exception as e:  # noqa: BLE001 — fail toward silence, same posture as triage
            out = {"verdict": "drop", "reason": f"valve error → no promotion: {e}", "bundle": {}}
        try:
            from sotto_log import diag  # noqa: PLC0415
            diag(f"[triage_event --valve] {out['verdict']}: {_s(out.get('reason'))[:200]}")
        except Exception:  # noqa: BLE001
            pass
        print(json.dumps(out))
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
    except Exception:  # noqa: BLE001
        print(json.dumps({"verdict": "drop", "reason": "unparseable input", "bundle": {}}))
        return
    try:
        out = triage(payload)
    except Exception as e:  # noqa: BLE001
        # Fail toward silence: preserve the raw events in the queue and answer "queue", never crash
        # the receiver's synchronous call or nudge the user off a broken pipeline.
        for ev in (payload.get("events") or []):
            if isinstance(ev, dict):
                _append_queue("error", "", ev)
        out = {"verdict": "queue", "reason": f"triage error → queue: {e}", "bundle": {}}
    try:
        from sotto_log import diag  # noqa: PLC0415
        diag(f"[triage_event] {len(payload.get('events') or [])} event(s) → "
             f"{out['verdict']}: {_s(out.get('reason'))[:200]}")
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps(out))


if __name__ == "__main__":
    main()
