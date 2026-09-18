#!/usr/bin/env python3
"""
preferences.py — the EXPLICIT side of Sotto's preference memory.

The user states mutes, tone, VIPs and cadence here. Chat and dashboard use this one writer.
The `explicit` block retains its existing format; legacy inferred fields elsewhere in the file
are preserved but ignored. Draft usage does not change preferences.

`compose_brief` reads these to suppress muted senders / people / sections and to honor tone notes.
The `sotto-feedback` skill writes them via this CLI. Pure stdlib; never raises on read.

Cadence lives here too: `nudge_snooze_until` (a single ISO local wall-clock stamp, not a list) is
the user's "be quieter" lever — while it is in the future the event funnel (triage_event.py Tier 0)
and the proactive watcher (proactive_scan.py) hold every nudge. It is a *scalar* in the same
explicit block, written through the same path, without changing other preferences.

VIP lives here too: `vip_people` is the user's STATED list of people whose missed calls clear the
quiet-hours bar (triage_event._is_vip checks it before the two heuristics — a top-of-queue
relationship-pulse priority, or a "family" mention in their graph file). It is a plain explicit
list, and saying "Sarah is a VIP"
in chat and toggling VIP in the dashboard are the same write.

CLI:
  preferences.py show
  preferences.py mute-sender <email | @domain | phone>  # newsletters / noisy senders / a number
      # A phone may be written any way ("+12025550171", "+1 202 555 0171", "(202) 555-0171"): it is
      # stored as given and normalized when it is MATCHED, so mutes already in the file keep
      # working without a migration, and the number also matches inside a WhatsApp identifier.
  preferences.py mute-person "<display name>"        # stop flagging them in the brief
  preferences.py mute-section <section>               # e.g. birthdays, screen_time
  preferences.py tone "<short note>"                 # e.g. "keep it terse"
  preferences.py vip "<display name>"                # their missed calls reach you in quiet hours
  preferences.py unmute-sender <v> | unmute-person "<v>" | unmute-section <v> | clear-tone
  preferences.py unvip "<display name>"
  preferences.py snooze-nudges tomorrow | "+2h" | 15:00 | 3pm | 2026-08-08T06:00
  preferences.py remove-tone "keep it terse"          # remove just this note
  preferences.py unsnooze-nudges                     # "back to normal"
  preferences.py brief-audio off|morning|evening|both  # standing voice-note briefs (text always sent too)
"""
from __future__ import annotations

import json
import os
import re
import sys
from email.utils import parseaddr
from datetime import datetime, timedelta, timezone

# _shared/lib holds the shared primitives; this module is invoked as a bare script from chat, the
# dashboard, so the path is set up here rather than assumed.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jsonstore  # noqa: E402 — THE read-modify-write lock for the volume
# THE tree's identifier normaliser (→ textutil._normalize_identifier, last 10 digits), the same one
# conversation_key threads by. A mute must not have a second opinion about what a number is.
from personal_context import normalized_identifier as _normalized_identifier  # noqa: E402

LISTS = ("mute_senders", "mute_people", "mute_sections", "tone_notes", "vip_people")
# Scalar (single-value) explicit preferences. Same block, same writer, same persistence path.
SCALARS = ("nudge_snooze_until", "brief_audio", "nudge_budget")
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


def _mutate(apply) -> dict:
    """THE way the explicit block changes: read, mutate and write inside ONE lock.

    The mutation has to happen in here. An earlier fix moved only the FILE read inside the lock and
    left callers computing their new block from `load_explicit()` outside it — which made things
    worse, not better: serialising the writes turned a 7% racy overwrite into a 23% deterministic
    one, because the losing writer faithfully wrote back a block it had read before the winner's
    change existed. Measured both times; that is the only reason it was caught.

    `apply` receives the current explicit block and mutates it in place."""
    with jsonstore.transaction(_path(), default={}, mode=0o600, indent=2) as data:
        if not isinstance(data, dict):
            data.clear()
        ex = data.get("explicit")
        if not isinstance(ex, dict):
            ex = empty_explicit()
        for k in LISTS:
            if not isinstance(ex.get(k), list):
                ex[k] = []
        for k in SCALARS:
            if not isinstance(ex.get(k), str):
                ex[k] = ""
        apply(ex)
        ex["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data["explicit"] = ex
        return dict(ex)


def _save(explicit: dict) -> None:
    """Replace the whole explicit block. Kept for callers that legitimately own the entire block
    (clear-tone); everything incremental goes through _mutate so it can't clobber a concurrent
    change it never saw."""
    _mutate(lambda ex: (ex.clear(), ex.update(explicit)))


def add(kind: str, value: str) -> dict:
    if kind not in LISTS:
        raise ValueError(f"unknown preference list: {kind}")
    value = _norm(kind, value)

    def _apply(ex):
        if value and value not in ex[kind]:
            ex[kind].append(value)
    return _mutate(_apply)


def remove(kind: str, value: str) -> dict:
    if kind not in LISTS:
        raise ValueError(f"unknown preference list: {kind}")
    value = _norm(kind, value)
    return _mutate(lambda ex: ex.__setitem__(kind, [x for x in ex[kind] if x != value]))


def set_scalar(kind: str, value: str) -> dict:
    """Write one scalar explicit preference (`nudge_snooze_until`, `brief_audio`). "" clears it."""
    if kind not in SCALARS:
        raise ValueError(f"unknown preference scalar: {kind}")
    return _mutate(lambda ex: ex.__setitem__(kind, (value or "").strip()))


def configured_nudge_budget() -> int:
    """The installation's ceiling. An explicit preference may reduce it, never raise it."""
    try:
        return max(0, int((os.environ.get("SOTTO_NUDGE_BUDGET") or "").strip() or 4))
    except (TypeError, ValueError):
        return 4


def effective_nudge_budget(explicit: dict | None = None, configured: int | None = None) -> int:
    """Current unsolicited-nudge allowance, clamped to the configured installation maximum."""
    ceiling = configured_nudge_budget() if configured is None else max(0, int(configured))
    ex = explicit if isinstance(explicit, dict) else load_explicit()
    raw = str(ex.get("nudge_budget") or "").strip()
    if not raw:
        return ceiling
    try:
        return min(ceiling, max(0, int(raw)))
    except (TypeError, ValueError):
        return ceiling


def change_nudge_budget(direction: str) -> dict:
    """Apply the user's plain volume controls: fewer halves, no sets zero, more restores default."""
    direction = (direction or "").strip().lower()
    if direction not in ("fewer", "no", "more"):
        raise ValueError("nudge-budget takes fewer|no|more")
    ceiling = configured_nudge_budget()

    def _apply(ex):
        current = effective_nudge_budget(ex, ceiling)
        if direction == "fewer":
            value = current if current == 0 else current // 2
        elif direction == "no":
            value = 0
        else:
            value = ceiling
        ex["nudge_budget"] = str(value)

    return _mutate(_apply)


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


# A phone-shaped local part, by the same rule textutil._normalize_identifier applies.
_PHONE_LOCAL = re.compile(r"[\d\s\-\+\(\)]+")
# Chat transports that carry a real phone number in front of the '@'. Everything else with an '@'
# is an email (matched as an email), a '@lid' privacy handle, or a '@g.us' room — never a person's
# number, and never something a phone mute may catch.
_PHONE_JID_DOMAINS = ("s.whatsapp.net", "whatsapp.net", "c.us")
PHONE_KEY_MIN_DIGITS = 7                 # shorter than this is a shortcode, not a phone number


def _phone_key(value: str) -> str:
    """The comparison key for a phone number written any way — "+1 202 555 0171", "(202) 555-0171",
    "12025550171", or the phone in front of a WhatsApp JID — and "" for anything that is not one.

    One normaliser for the whole tree: personal_context.normalized_identifier (→
    textutil._normalize_identifier, last 10 digits), so a mute and a conversation agree on what a
    number is. Keys are compared whole, so a mute on +12025550171 never catches ...0170."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    local, sep, domain = v.partition("@")
    if sep and domain not in _PHONE_JID_DOMAINS:
        return ""
    if not _PHONE_LOCAL.fullmatch(local):
        return ""
    key = _normalized_identifier(local)
    return key if len(key) >= PHONE_KEY_MIN_DIGITS else ""


def sender_is_muted(identifier: str, muted: list) -> bool:
    """True if a sender identifier matches a muted sender.

    An email matches exactly, or by an '@domain' / 'domain' suffix rule (so '@news.acme.com' or
    'news.acme.com' mutes the whole sending domain). A phone number matches however either side is
    written — the mute and the identifier are compared through the tree's one identifier normaliser
    (_phone_key), which also reads the number out of a WhatsApp JID. Email matching is unchanged:
    a phone key only ever compares against another phone key."""
    e = (identifier or "").strip().lower()
    if not e:
        return False
    key = _phone_key(e)
    dom = e.split("@", 1)[1] if "@" in e else e
    for m in muted:
        m = (m or "").strip().lower()
        if not m:
            continue
        if key and _phone_key(m) == key:      # a number, in whatever format either side stored it
            return True
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


def proactively_muted(sender: str, event: dict, explicit: dict) -> bool:
    """THE current-mute predicate for every communication surface: the event funnel's ingress gate,
    the release valve, a tapped promotion and the midday digest all ask this one question, so
    "stop surfacing X" cannot drop yesterday's queued message and still let today's ring through.

    `sender` is triage's resolved display name; event fields retain transport identifiers. Names
    are exact and case-insensitive. Sender rules reuse sender_is_muted, including domain rules and
    phone numbers in any format (and the phone inside a WhatsApp JID).
    """
    explicit = explicit if isinstance(explicit, dict) else {}
    name = (sender or "").strip().lower()
    if name and any(name == str(person or "").strip().lower()
                    for person in (explicit.get("mute_people") or [])):
        return True
    if not isinstance(event, dict):
        return False
    identifiers = []
    for key in ("handle", "contact_jid", "sender_jid", "phone", "email", "address"):
        value = str(event.get(key) or "").strip()
        if value:
            identifiers.append(value)
    from_value = str(event.get("from") or "").strip()
    if from_value:
        identifiers.extend(v for v in (from_value, parseaddr(from_value)[1]) if v)
    return any(sender_is_muted(value, explicit.get("mute_senders") or [])
               for value in identifiers)


_CLI = {
    "mute-sender": ("mute_senders", add), "unmute-sender": ("mute_senders", remove),
    "mute-person": ("mute_people", add), "unmute-person": ("mute_people", remove),
    "mute-section": ("mute_sections", add), "unmute-section": ("mute_sections", remove),
    "tone": ("tone_notes", add), "remove-tone": ("tone_notes", remove),
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
        print(json.dumps(_mutate(lambda ex: ex.__setitem__("tone_notes", [])))); return
    if cmd == "unsnooze-nudges":
        print(json.dumps(set_scalar("nudge_snooze_until", ""))); return
    if cmd == "nudge-budget":
        value = (sys.argv[2] if len(sys.argv) > 2 else "").strip().lower()
        try:
            print(json.dumps(change_nudge_budget(value)))
        except ValueError as e:
            print(json.dumps({"error": str(e)})); sys.exit(2)
        return
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
