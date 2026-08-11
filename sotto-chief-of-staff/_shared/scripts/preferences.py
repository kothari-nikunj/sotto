#!/usr/bin/env python3
"""
preferences.py — the EXPLICIT side of Sotto's preference memory.

The repo already learns preferences from BEHAVIOR (approval-tiers/learn_preferences.py tallies
outcomes.jsonl → preferences.json). What was missing is the explicit channel: the user saying "stop
surfacing newsletters", "don't flag Bob", "keep it terse". Those are precious — they must never be
wiped by the behavioral learner, which rewrites preferences.json wholesale. So we keep them in the
SAME file under a reserved `explicit` block, and learn_preferences.py carries that block forward
untouched on every run.

`compose_brief` reads these to suppress muted senders / people / sections and to honor tone notes.
The `sotto-feedback` skill writes them via this CLI. Pure stdlib; never raises on read.

Cadence lives here too: `nudge_snooze_until` (a single ISO local wall-clock stamp, not a list) is
the user's "be quieter" lever — while it is in the future the event funnel (triage_event.py Tier 0)
and the proactive watcher (proactive_scan.py) hold every nudge. It is a *scalar* in the same
explicit block, written through the same path, and preserved by the behavioral learner identically.

VIP lives here too: `vip_people` is the user's STATED list of people whose missed calls clear the
quiet-hours bar (triage_event._is_vip checks it before the two heuristics — a top-of-queue
relationship-pulse priority, or a "family" mention in their graph file). It is a plain explicit
list, so the behavioral learner carries it forward like every other one, and saying "Sarah is a VIP"
in chat and toggling VIP in the dashboard are the same write.

CLI:
  preferences.py show
  preferences.py mute-sender <email-or-@domain>      # newsletters / noisy senders
  preferences.py mute-person "<display name>"        # stop flagging them in the brief
  preferences.py mute-section <section>               # e.g. birthdays, screen_time
  preferences.py tone "<short note>"                 # e.g. "keep it terse"
  preferences.py vip "<display name>"                # their missed calls reach you in quiet hours
  preferences.py unmute-sender <v> | unmute-person "<v>" | unmute-section <v> | clear-tone
  preferences.py unvip "<display name>"
  preferences.py snooze-nudges tomorrow | "+2h" | 15:00 | 3pm | 2026-08-08T06:00
  preferences.py unsnooze-nudges                     # "back to normal"
  preferences.py brief-audio off|morning|evening|both  # standing voice-note briefs (text always sent too)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

LISTS = ("mute_senders", "mute_people", "mute_sections", "tone_notes", "vip_people")
# Scalar (single-value) explicit preferences. Same block, same writer, same learner protection.
SCALARS = ("nudge_snooze_until", "brief_audio")
BRIEF_AUDIO_VALUES = ("off", "morning", "evening", "both")   # standing voice-note preference for the cron briefs
SNOOZE_FMT = "%Y-%m-%dT%H:%M"    # minute precision, local wall clock (no offset — see snooze_active)
# A snooze lifts when quiet hours do. "Quieter today" used to resolve to a hardcoded 6am while
# SOTTO_QUIET_END defaults to 7 — so the stamp said 6 and nudges actually resumed at 7. Read the
# SAME env var the funnel reads, with the SAME default: the coupling is the variable, not an import
# (event-triage/ isn't importable from here, and triage_event.py imports THIS module).
SNOOZE_MORNING_HOUR_DEFAULT = 7


def _root() -> str:
    return os.environ.get("SOTTO_DATA", "/data")


def _path() -> str:
    return os.path.join(_root(), "preferences.json")


def _load_all() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def empty_explicit() -> dict:
    out = {k: [] for k in LISTS}
    out.update({k: "" for k in SCALARS})
    return out


def load_explicit() -> dict:
    """The user's explicit preferences, always shaped (missing lists default to empty, missing
    scalars to "")."""
    ex = (_load_all().get("explicit") or {})
    out = empty_explicit()
    for k in LISTS:
        v = ex.get(k)
        if isinstance(v, list):
            out[k] = [str(x) for x in v if str(x).strip()]
    for k in SCALARS:
        v = ex.get(k)
        if isinstance(v, str):
            out[k] = v.strip()
    return out


def _norm(kind: str, value: str) -> str:
    value = (value or "").strip()
    if kind == "mute_senders":
        return value.lower()          # emails/domains are case-insensitive
    return value


def _save(explicit: dict) -> None:
    data = _load_all()
    explicit["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["explicit"] = explicit
    os.makedirs(_root(), exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)   # user data, same posture as every other writer on the volume
    os.replace(tmp, _path())


def add(kind: str, value: str) -> dict:
    if kind not in LISTS:
        raise ValueError(f"unknown preference list: {kind}")
    value = _norm(kind, value)
    ex = load_explicit()
    if value and value not in ex[kind]:
        ex[kind].append(value)
    _save(ex)
    return ex


def remove(kind: str, value: str) -> dict:
    if kind not in LISTS:
        raise ValueError(f"unknown preference list: {kind}")
    value = _norm(kind, value)
    ex = load_explicit()
    ex[kind] = [x for x in ex[kind] if x != value]
    _save(ex)
    return ex


def set_scalar(kind: str, value: str) -> dict:
    """Write one scalar explicit preference (`nudge_snooze_until`, `brief_audio`). "" clears it."""
    if kind not in SCALARS:
        raise ValueError(f"unknown preference scalar: {kind}")
    ex = load_explicit()
    ex[kind] = (value or "").strip()
    _save(ex)
    return ex


# ── Cadence: the nudge snooze ("quieter today" / "quiet until 3" / "back to normal") ───────────────

def _now_local_best_effort() -> datetime:
    """The user's wall clock (SOTTO_TIMEZONE / settings.json) via the brief's own tz resolution;
    falls back to naive system-local time when _shared/lib isn't importable (bare CLI use)."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
        from timeutil import _now_local, configured_tz  # noqa: PLC0415
        return _now_local(configured_tz() or "+00:00")
    except Exception:  # noqa: BLE001
        return datetime.now()


def _snooze_morning_hour() -> int:
    """The hour a "quieter today" snooze lifts: SOTTO_QUIET_END (the funnel's own quiet-hours knob),
    default 7. Anything unparseable or outside 0..23 falls back to the default."""
    try:
        h = int((os.environ.get("SOTTO_QUIET_END") or "").strip() or SNOOZE_MORNING_HOUR_DEFAULT)
    except ValueError:
        return SNOOZE_MORNING_HOUR_DEFAULT
    return h if 0 <= h <= 23 else SNOOZE_MORNING_HOUR_DEFAULT


def _naive(dt: datetime) -> datetime:
    """Wall-clock view of an instant. The snooze is a LOCAL wall-clock stamp — comparing naive
    both sides is what makes 'quiet until 3' mean 3pm where the user is, whatever tzinfo the
    resolved clock happens to carry."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def parse_snooze(value: str):
    """An `nudge_snooze_until` value → naive local datetime, or None when absent/unparseable.
    A bare date means midnight AT THE START of that date (the moment the snooze lifts)."""
    v = (value or "").strip()
    if not v:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        v += "T00:00:00"
    try:
        return _naive(datetime.fromisoformat(v.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def resolve_snooze_spec(spec: str, now_local: datetime | None = None) -> str:
    """A user-facing snooze spec → the stored ISO local stamp. Deterministic clock math lives here
    (never in the agent's head). Accepts:
      "tomorrow" / "today"        → tomorrow at quiet-end (SOTTO_QUIET_END, default 7am), which is
                                    also what "rest of the day" means — a snooze lifts when quiet
                                    hours do
      "+2h" / "2h" / "90m"        → now + delta        ("quiet for 2 hours")
      "15:00" / "3pm" / "3"       → that time today, or tomorrow if it already passed
      "2026-08-08" / ISO datetime → verbatim
      "" / off / clear / normal   → "" (clears the snooze)
    Raises ValueError on anything else — the caller reports it rather than guessing."""
    s = (spec or "").strip().lower()
    now = _naive(now_local or _now_local_best_effort())
    if s in ("", "off", "clear", "none", "normal", "back to normal"):
        return ""
    if s in ("today", "tomorrow", "rest of the day", "quieter today"):
        nxt = (now + timedelta(days=1)).replace(hour=_snooze_morning_hour(), minute=0,
                                                second=0, microsecond=0)
        return nxt.strftime(SNOOZE_FMT)
    m = re.fullmatch(r"\+?(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)", s)
    if m:
        n = float(m.group(1))
        delta = timedelta(hours=n) if m.group(2).startswith(("h",)) else timedelta(minutes=n)
        return (now + delta).strftime(SNOOZE_FMT)
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        elif not ampm and hour < 7:
            hour += 12               # bare "3" from someone awake means 3pm, not 3am
        if hour > 23 or minute > 59:
            raise ValueError(f"unparseable time: {spec}")
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.strftime(SNOOZE_FMT)
    parsed = parse_snooze(spec)
    if parsed is not None:
        return parsed.strftime(SNOOZE_FMT)
    raise ValueError(f"unparseable snooze spec: {spec}")


def snooze_active(now_local: datetime | None = None, explicit: dict | None = None) -> bool:
    """True while `nudge_snooze_until` is in the future. Missing/unparseable → False (a broken
    stamp must never silence Sotto forever)."""
    ex = explicit if isinstance(explicit, dict) else load_explicit()
    until = parse_snooze(str(ex.get("nudge_snooze_until") or ""))
    if until is None:
        return False
    return _naive(now_local or _now_local_best_effort()) < until


def sender_is_muted(email: str, muted: list) -> bool:
    """True if an email address matches a muted sender — exact address, or an '@domain' / 'domain'
    suffix rule (so '@news.acme.com' or 'news.acme.com' mutes the whole sending domain)."""
    e = (email or "").strip().lower()
    if not e:
        return False
    dom = e.split("@", 1)[1] if "@" in e else e
    for m in muted:
        m = (m or "").strip().lower()
        if not m:
            continue
        if m.startswith("@"):                 # "@domain" → whole-domain rule
            rule = m[1:]
            if rule and (dom == rule or dom.endswith("." + rule)):
                return True
        elif "@" in m:                        # full address → exact match
            if e == m:
                return True
        else:                                 # bare "domain" → whole-domain rule
            if dom == m or dom.endswith("." + m):
                return True
    return False


_CLI = {
    "mute-sender": ("mute_senders", add), "unmute-sender": ("mute_senders", remove),
    "mute-person": ("mute_people", add), "unmute-person": ("mute_people", remove),
    "mute-section": ("mute_sections", add), "unmute-section": ("mute_sections", remove),
    "tone": ("tone_notes", add),
    "vip": ("vip_people", add), "unvip": ("vip_people", remove),
}


def is_vip(name: str, vip_people: list) -> bool:
    """True when a resolved display name is on the user's stated VIP list (exact, case-insensitive
    — the same standard mute_people uses; nothing fuzzy, so 'Sam' never VIPs 'Samantha')."""
    n = (name or "").strip().lower()
    if not n:
        return False
    return any(n == (v or "").strip().lower() for v in (vip_people or []))


def main():
    if len(sys.argv) < 2:
        print(json.dumps(load_explicit())); return
    cmd = sys.argv[1]
    if cmd == "show":
        print(json.dumps(load_explicit())); return
    if cmd == "clear-tone":
        ex = load_explicit(); ex["tone_notes"] = []; _save(ex)
        print(json.dumps(ex)); return
    if cmd == "unsnooze-nudges":
        print(json.dumps(set_scalar("nudge_snooze_until", ""))); return
    if cmd == "brief-audio":
        # One sentence: your briefs arrive as voice notes too, whenever you say so — off | morning
        # | evening | both. The text brief is always delivered regardless; voice is in addition.
        value = (sys.argv[2] if len(sys.argv) > 2 else "").strip().lower()
        if value not in BRIEF_AUDIO_VALUES:
            print(json.dumps({"error": f"brief-audio takes one of {'|'.join(BRIEF_AUDIO_VALUES)}"})); sys.exit(2)
        print(json.dumps(set_scalar("brief_audio", "" if value == "off" else value))); return
    if cmd == "snooze-nudges":
        spec = " ".join(sys.argv[2:]).strip()
        if not spec:
            print(json.dumps({"error": "missing value"})); sys.exit(2)
        try:
            value = resolve_snooze_spec(spec)
        except ValueError as e:
            print(json.dumps({"error": str(e)})); sys.exit(2)
        print(json.dumps(set_scalar("nudge_snooze_until", value))); return
    if cmd not in _CLI:
        print(json.dumps({"error": f"unknown command: {cmd}"})); sys.exit(2)
    kind, fn = _CLI[cmd]
    value = sys.argv[2] if len(sys.argv) > 2 else ""
    if not value.strip():
        print(json.dumps({"error": "missing value"})); sys.exit(2)
    print(json.dumps(fn(kind, value)))


if __name__ == "__main__":
    main()
