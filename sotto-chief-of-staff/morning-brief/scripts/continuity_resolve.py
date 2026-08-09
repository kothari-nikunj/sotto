#!/usr/bin/env python3
"""
continuity_resolve.py — deterministic resolution of open loops across briefs.

PORT SOURCE: app/src-tauri/src/database/continuity.rs (+ pipeline/deterministic.ts, reconciler.ts)
Runs on Hermes (execute_code) over knowledge/continuity/*.md on $SOTTO_DATA, BEFORE the LLM.
Resolves passed meetings / replied emails / expired items, dedupes by anchor_key, bumps
times_surfaced, and prunes terminal items past retention.

DIRECTION MATTERS (Step 2.7 item 1): what you OWE expires quietly after AGE_EXPIRY_DAYS, or
DEADLINE_GRACE_DAYS past its due date. What you're OWED (`waiting_on`) expires on NEITHER clock —
a due date makes a debt owed to the user more urgent, not more disposable, so a passed deadline
only makes it chase-eligible at once. It leaves by delivery (the inbound branch of
`_check_action_resolution`), by the user's hand in sotto-loops, or — when there is no
contact_identifier to chase through at all — as `unreachable` once its chases are spent. The chase
clock (`chase_pending` → `chased_count` / `last_chased_at` / `chase_after`) is written here and
nowhere else: this file is the ONLY writer of chase state, as it is of every other ledger field.

The new_actions may be the brief's raw `actions[]` (camelCase `actionItems`: type, channel,
contactName, contactIdentifier, emailThreadId, meetingTime, deadlineDate, contextSummary…) OR the
internal snake_case shape — `_normalize_action` accepts both, mirroring the Mac pipeline's
ActionItem mapping before continuity.rs sees it.

Stdin/arg JSON: { "today": "2026-06-23",
                  "signals": { "replied_thread_ids": [...],
                               "handled": [ {identifier, channel} ] },   # Already-Handled section
                  "new_actions": [ <brief actions[] or snake_case actions> ] }
Prints { "resolved":[...], "expired":[...], "active":[...] }

TWO PASSES, ONE FILE. The brief must reason about a ledger resolved as of THIS run, and only the
merge needs the brief's output — so the pass splits:
    continuity_resolve.py --resolve-only <payload>   # BEFORE compose: resolve + stamp the chase
    continuity_resolve.py --merge-only   <payload>   # AFTER compose: ingest the brief's actions[]
    continuity_resolve.py <payload>                  # both, unchanged (on-demand / back-compat)

CHASE, IN TWO PHASES — one extra field, `chase_pending`. `_stamp_chase` writes
`chase_pending: <today>` and touches nothing else: a chase counts only once it was actually
DELIVERED. proactive_scan keys its candidates on `chase_pending == today` and, on its FIRED path,
shells back to
    continuity_resolve.py --finalize-chase <anchor_key>
which is what increments `chased_count`, stamps `last_chased_at`/`chase_after` and clears the
pending stamp. This file therefore remains the ONLY writer of ledger state. An un-fired pending
stamp expires at day end (the next resolve drops it without counting it) — nothing burns
undelivered. The whole story is still one sentence: Sotto nudges them twice, then asks you.

IDENTITY IS NEVER WHAT THE MODEL TYPED. A group ask is keyed by the GROUP'S OWN ID — iMessage's
`chat_guid`, WhatsApp's `…@g.us` JID, both carried in LocalData and both now shown to the model as
`group_id:` — and a person ask by the person (canonical_id > identifier > name). A group used to be
the one counterpart with no identifier at all, so its anchor fell back to
`name:<whatever the extractor called the group that day>`; rename the group in one brief and the
same debt opened a second row. An id only counts when THIS snapshot contains it
(`canonicalize_counterpart`, source-verified — never model-asserted), and `_migrate_identity` heals
the rows a label already minted: one idempotent re-anchor per resolve, and two rows that turn out to
be one debt fold (older age and chase clock, newer words, loser terminal as `merged_duplicate`,
which renders as nothing — a dedupe is bookkeeping, not an outcome that moved).

ANCHOR MIGRATION (direction split). `waiting_on` used to share the `follow_up` anchor family, so a
reply the user owed and a debt owed TO the user on the same thread collapsed into one item whose
direction was whichever was captured first. They are two debts and now get two anchors. Pre-split
anchors keep working exactly as they are — nothing is rewritten — while new captures fork onto the
direction-correct key and the old one leaves by its own resolution/expiry route.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

import yaml

# Shared with the rest of the pipeline: compose_brief's zoneinfo-aware tz helpers (SOTTO_TIMEZONE /
# wizard settings) and ledger_io's frontmatter loader — one parser for all three ledger readers.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shared", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shared", "lib"))
import ledger_io  # noqa: E402
from textutil import unwrap_tool_result  # noqa: E402
from timeutil import _env_tz, _now_local, _resolve_tz, configured_tz  # noqa: E402

TERMINAL_RETENTION_DAYS = 30          # continuity.rs:13
AGE_EXPIRY_DAYS = 7                   # continuity.rs:973 (expiry_7d) — you-owe loops only
DEADLINE_GRACE_DAYS = 2              # continuity.rs:975 (expiry_2d_date)
CHASE_AFTER_DAYS = 3                 # default for SOTTO_CHASE_AFTER_DAYS (deadline-less waiting_ons)
CHASE_MAX = ledger_io.CHASE_MAX      # after two chases, stop nudging and hand it to sotto-loops
# The one direction that is chased instead of expired — defined once, in ledger_io, so the
# resolver, loops_query and retune_scan can never disagree about which way a debt points.
is_waiting_on = ledger_io.is_waiting_on
ACTIVE = ledger_io.ACTIVE             # continuity.rs:227 — single source in ledger_io
TERMINAL = ledger_io.TERMINAL         # continuity.rs:230 (apply_commitments uses cr.TERMINAL)
MEETING_TYPES = {"meeting_prep", "meeting_info"}        # continuity.rs:1001
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
            "mon ", "tue ", "wed ", "thu ", "fri ", "sat ", "sun "]   # continuity.rs:564-565


def _data_root() -> str:
    return os.environ.get("SOTTO_DATA", "/data")


def _dir() -> str:
    return os.path.join(_data_root(), "knowledge", "continuity")


def _normalize_action(a: dict) -> dict:
    """Map the brief's camelCase actionItems onto the snake_case shape continuity expects.
    Falls through to snake_case keys if already normalized (so both shapes work). PORT: the Mac
    pipeline builds an internal ActionItem from the FLEX actionItems before continuity.rs runs;
    without this shim every field reads None and all anchor keys collapse to "::"."""
    g = a.get
    return {
        "action_type": g("action_type") or g("type"),
        "channel": g("channel"),
        "canonical_id": g("canonical_id"),
        "contact_identifier": g("contact_identifier") or g("contactIdentifier"),
        "contact_name": g("contact_name") or g("contactName") or "",
        "group_id": g("group_id") or g("groupId"),
        "source_thread_id": g("source_thread_id") or g("emailThreadId"),
        "summary": g("summary") or g("contextSummary") or g("prose") or "",
        "ask": g("ask") or g("contextAsk"),
        "meeting_time": g("meeting_time") or g("meetingTime"),
        "deadline": g("deadline") or g("deadlineDate") or g("contextDeadline"),
        "created_at": g("created_at"),
    }


def normalize_channel(ch: str) -> str:
    ch = (ch or "").lower()
    if ch in ("gmail", "email", "apple_mail"):
        return "email"
    return ch


# The spelling normalizer lives in ledger_io so the writer and every read view classify a
# `Waiting-On ` exactly the same way (one concept, one implementation).
_normalize_action_type = ledger_io.normalize_action_type


def action_family(t: str) -> str:
    # continuity.rs:521-528 — group related types so reply≠follow_up≠call_back duplicate per person.
    # DIRECTION IS ITS OWN FAMILY: a reply you owe them and a deliverable they owe you are two
    # debts even when they live on one thread, and merging them silently inverts expiry, resolution
    # and the chase. So `waiting_on` anchors apart from the follow_up family it used to share.
    t = _normalize_action_type(t)
    if t in ledger_io.WAITING_ON_TYPES:
        return t
    if t in ("reply", "follow_up", "follow_up_stale", "call_back"):
        return "follow_up"
    if t in ("meeting_prep", "meeting_info"):
        return "meeting"
    if t in ("schedule", "reschedule", "propose_times", "rsvp"):
        return "scheduling"
    return t


def _normalize_name_for_dedup(name: str) -> str:
    # continuity.rs:234-238 — strip "<email>", keep first two words, lowercase.
    name = name or ""
    name = name.split("<", 1)[0].strip() if "<" in name else name
    return " ".join(name.split()[:2]).lower()


def _normalize_identifier_for_anchor(value: str):
    # continuity.rs:257-270 — email lowercased; phone → last 10 digits; else lowercased.
    trimmed = (value or "").strip()
    if not trimmed:
        return None
    if "@" in trimmed:
        return trimmed.lower()
    digits = re.sub(r"\D", "", trimmed)
    if len(digits) >= 10:
        return digits[-10:]
    return trimmed.lower()


def contact_anchor(canonical_id, identifier, name, group_id=None) -> str:
    # continuity.rs:272-282 — gid: > cid: > id: > name:. A group ask is keyed by the GROUP'S OWN ID
    # and a person ask by the person (canonical_id absorbs the same human's email-vs-phone drift);
    # the name is the last resort, because a name is what the extractor typed that day.
    if group_id and str(group_id).strip():
        return f"gid:{str(group_id).strip().lower()}"
    if canonical_id and str(canonical_id).strip():
        return f"cid:{str(canonical_id).strip().lower()}"
    norm = _normalize_identifier_for_anchor(identifier or "")
    if norm:
        return f"id:{norm}"
    return f"name:{_normalize_name_for_dedup(name or '')}"


def compute_anchor_key(a: dict) -> str:
    # continuity.rs:284-304 — thread id wins, else channel:family:contact_anchor.
    # A thread can carry one debt in EACH direction, so the waiting_on side takes a suffixed key
    # (every pre-split anchor keeps its exact shape; only the new direction forks).
    # EXCEPT a source-verified GROUP: the group's platform id IS the counterpart, so it anchors on
    # the counterpart shape and never on a thread id (the same group ask reached us with a thread
    # id one day and without it the next, which is how one debt became two rows).
    tid = (a.get("source_thread_id") or "").strip()
    if tid and not (a.get("group_id") or ""):
        fam = action_family(a.get("action_type", ""))
        return f"thread:{tid}:{fam}" if fam in ledger_io.WAITING_ON_TYPES else f"thread:{tid}"
    return f"{normalize_channel(a.get('channel',''))}:{action_family(a.get('action_type',''))}:" \
           f"{contact_anchor(a.get('canonical_id'), a.get('contact_identifier'), a.get('contact_name',''), a.get('group_id'))}"


# ── Group identity (the one counterpart with no per-person identifier) ────────
# A 1:1 loop anchors on an email or a phone number the data hands us. A GROUP had neither, so its
# anchor fell back to `name:<whatever the extractor called the group that day>` — an identity the
# model is free to reword, which is how one group ask became two open rows with two different
# labels. The Bridge already carries the group's stable `chat_guid` in LocalData; render_local
# exposes it to the model as `group_id:` and indexes it here, so a group's identity is a machine id
# and its label is just a label. Import-guarded: without the renderer, identity degrades to today's
# name fallback rather than costing a resolve.
try:
    import render_local as _render_local  # noqa: E402
except Exception:  # noqa: BLE001  # pragma: no cover — renderer is always importable in-tree
    _render_local = None


def group_identity(local: dict) -> dict:
    """The snapshot's group index ({"by_id", "by_label"} — see render_local.group_identity_index),
    or {} when there is no snapshot to build it from (an on-demand run without `local` simply keeps
    today's behavior)."""
    try:
        return _render_local.group_identity_index(local or {}) if _render_local else {}
    except Exception:  # noqa: BLE001
        return {}


def canonicalize_counterpart(a: dict, group_index: dict) -> dict:
    """A group ask is owed by the GROUP, whose identity is its platform id — iMessage's chat_guid,
    WhatsApp's `…@g.us` JID — and whose name is only a label. An id the action carries counts ONLY
    when this snapshot actually contains it (source-verified, never model-asserted); a group ask
    with no id at all falls back to matching the label the renderer showed. Either way the action
    comes back carrying `group_id` and the group's own name."""
    if not (group_index and _render_local):
        return a
    by_id = group_index.get("by_id") or {}
    for candidate in (a.get("group_id"), a.get("contact_identifier"), a.get("source_thread_id")):
        hit = by_id.get(_render_local.group_id_key(_s(candidate)))
        if hit:
            channel, label = hit
            return {**a, "group_id": _render_local.group_id_key(_s(candidate)),
                    "channel": a.get("channel") or channel,
                    "contact_name": label or a.get("contact_name")}
    if _s(a.get("contact_identifier")).strip():
        return a                       # a 1:1 ask already has a person identifier — not a group
    hit = (group_index.get("by_label") or {}).get(
        (normalize_channel(a.get("channel", "")), _render_local.group_label_key(a.get("contact_name"))))
    if not hit:
        return a
    guid, label = hit
    return {**a, "group_id": _render_local.group_id_key(guid),
            "contact_name": label or a.get("contact_name")}


def _identifiers_match(a: str, b: str) -> bool:
    na, nb = _normalize_identifier_for_anchor(a), _normalize_identifier_for_anchor(b)
    return bool(na and nb and na == nb)


# ── Cross-channel reply detection (continuity.rs:1089-1295) ───────────────────
# THE MOAT: an open loop is resolved when the user answered the person on ANY
# channel — outgoing iMessage/WhatsApp/call, or a calendar event now on the books —
# not just the original thread. Matches by phone last-10 / email / JID across channels.

def _digits(s: str) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def _phone_matches(a: str, b: str) -> bool:
    da, db = _digits(a), _digits(b)
    if len(da) < 7 or len(db) < 7:
        return False
    return (da[-10:] if len(da) > 10 else da) == (db[-10:] if len(db) > 10 else db)


def _handle_matches(handle: str, ident: str) -> bool:
    if handle == ident:
        return True
    if "@" in handle and "@" in ident:
        return handle.lower() == ident.lower()
    return _phone_matches(handle, ident)


def _jid_matches_phone(jid: str, phone: str) -> bool:
    return _phone_matches((jid or "").split("@")[0], phone)


def _collect_all_identifiers(primary: str, contact_name: str, local: dict) -> list:
    """Primary identifier + the contact's other emails/phones (so a reply via a different address
    still resolves). Port of collect_all_identifiers."""
    ids = [primary] if primary else []
    name_lower = (contact_name or "").lower()
    if not name_lower:
        return ids
    for c in (local.get("contacts") or []):
        if (c.get("name") or "").lower() != name_lower:
            continue
        for e in (c.get("emails") or []):
            if e and not any(i.lower() == e.lower() for i in ids):
                ids.append(e)
        for p in (c.get("phones") or []):
            if p and p not in ids:
                ids.append(p)
        break
    return ids


def _check_outgoing_message(identifiers: list, after: str, local: dict):
    for m in (local.get("imessage") or []):
        if not m.get("is_from_me"):
            continue
        if (m.get("timestamp") or "") <= (after or ""):
            continue
        handle = m.get("handle") or ""
        if any(_handle_matches(handle, i) for i in identifiers):
            return ("replied", f"Outgoing iMessage to {handle}")
    for m in (local.get("whatsapp") or []):
        if not m.get("is_from_me"):
            continue
        if (m.get("timestamp") or "") <= (after or ""):
            continue
        jid = m.get("contact_jid") or ""
        if any(_jid_matches_phone(jid, i) or _handle_matches(jid, i) for i in identifiers):
            return ("replied", f"Outgoing WhatsApp to {jid}")
    return None


# ── Inbound delivery (the mirror of the outgoing check, for `waiting_on`) ─────
# A waiting_on closes when THEY deliver, not when you speak. Same identifier machinery
# (_handle_matches / _jid_matches_phone / _collect_all_identifiers), direction flipped.
#
# THE HEURISTIC (deliberately conservative — chasing someone who already delivered is how this
# feature loses trust, but SILENTLY closing a debt they never paid is worse): an inbound message
# counts as delivery only when it carries substance — a link or a file name (the local snapshot
# drops attachment-only rows, so a real attachment reaches us as a link or not at all), or at
# least SUBSTANCE_CHARS of text — AND does not read as a promise to send it later. A bare "ok",
# a thumbs-up, and "will send it tomorrow" all leave the loop open. When unsure we do NOT resolve:
# a stale chase candidate the user dismisses beats a debt closed behind their back.
SUBSTANCE_CHARS = 40
_DELIVERY_HINT = re.compile(
    r"https?://|www\.|\.(pdf|docx?|xlsx?|pptx?|csv|zip|png|jpe?g|key|numbers)\b", re.I)
_PROMISE_HINT = re.compile(
    r"\b(i'?ll|i will|we'?ll|we will|going to|gonna)\b[^.?!]{0,40}\b"
    r"(send|share|get|shoot|forward|email|ping|upload|revert|circle back)\b"
    r"|\b(not yet|still working|working on it|haven'?t|sorry for the delay|"
    r"by (eod|eow|tomorrow|end of|the end of))\b", re.I)


def _is_delivery(text: str) -> bool:
    t = _s(text).strip()
    if not t or _PROMISE_HINT.search(t):
        return False
    return bool(_DELIVERY_HINT.search(t)) or len(t) >= SUBSTANCE_CHARS


def _inbound_cutoff(created_at) -> str:
    """The instant a delivery has to beat. New items carry a full local timestamp, which IS the
    cutoff. A LEGACY date-only `created_at` could have been recorded at any hour of that day, so
    nothing from that day counts — otherwise the 09:00 chat that preceded the 16:00 promise closes
    the debt on the day it was recorded, behind the user's back."""
    c = _s(created_at).strip()
    if not c:
        return ""
    return c if len(c) > 10 else f"{c[:10]} 23:59:59"


def _check_inbound_delivery(identifiers: list, after: str, local: dict):
    """Did THEY send something substantive since the loop was created? Mirrors
    _check_outgoing_message exactly, with `is_from_me` inverted and the substance gate applied."""
    after = _inbound_cutoff(after)
    for m in (local.get("imessage") or []):
        if m.get("is_from_me"):
            continue
        if (m.get("timestamp") or "") <= (after or ""):
            continue
        handle = m.get("handle") or ""
        if any(_handle_matches(handle, i) for i in identifiers) and _is_delivery(m.get("text")):
            return ("delivered", f"Inbound iMessage from {handle}")
    for m in (local.get("whatsapp") or []):
        if m.get("is_from_me"):
            continue
        if (m.get("timestamp") or "") <= (after or ""):
            continue
        jid = m.get("contact_jid") or ""
        if (any(_jid_matches_phone(jid, i) or _handle_matches(jid, i) for i in identifiers)
                and _is_delivery(m.get("text"))):
            return ("delivered", f"Inbound WhatsApp from {jid}")
    return None


def _check_calendar_event(identifiers: list, contact_name: str, local: dict, now: datetime):
    events = local.get("calendar_events") or local.get("events") or []
    name_lower = (contact_name or "").lower()
    now_local = _to_user_zone(now)
    lo, hi = now_local - timedelta(hours=1), now_local + timedelta(days=14)
    id_lowers = {i.lower() for i in identifiers}
    for e in events:
        st = _parse_dt(e.get("start"))
        if st is not None and not (lo <= _to_user_zone(st) <= hi):
            continue
        for a in (e.get("attendees") or []):
            email = _s(a.get("email")).lower()
            display = _s(a.get("displayName") or a.get("display_name")).lower()
            if email and email in id_lowers:
                return ("scheduled_meeting", f'Calendar event "{_s(e.get("summary")) or "a meeting"}" with {contact_name}')
            if name_lower and display and display == name_lower:
                return ("scheduled_meeting", f'Calendar event "{_s(e.get("summary")) or "a meeting"}" with {contact_name}')
    return None


def _check_action_resolution(it: dict, local: dict, now: datetime):
    """Port of check_action_resolution: did the user answer this person on any channel since the
    action was created? Returns (resolution_type, evidence) or None."""
    if not local:
        return None
    ident = it.get("contact_identifier") or ""
    if not ident:
        return None
    created = it.get("created_at") or ""
    ids = _collect_all_identifiers(ident, it.get("contact_name") or "", local)
    at = _normalize_action_type(it.get("action_type"))   # reply_message/reply_email → reply, etc.
    if at in ("reply", "follow_up", "follow_up_stale"):
        return (_check_outgoing_message(ids, created, local)
                or _check_calendar_event(ids, it.get("contact_name") or "", local, now))
    if at == "call_back":
        for c in (local.get("calls") or []):
            if c.get("is_outgoing") and (c.get("timestamp") or "") > created:
                if any(_phone_matches(c.get("phone") or "", i) for i in ids):
                    return ("called", f"Outgoing call to {c.get('phone')}")
        for c in (local.get("whatsapp_calls") or []):
            if c.get("is_outgoing") and (c.get("timestamp") or "") > created:
                if any(_jid_matches_phone(c.get("jid") or "", i) for i in ids):
                    return ("called", f"Outgoing WhatsApp call to {c.get('jid')}")
        return (_check_outgoing_message(ids, created, local)
                or _check_calendar_event(ids, it.get("contact_name") or "", local, now))
    if at in ledger_io.WAITING_ON_TYPES:
        # The mirrored branch: you're owed something, so the evidence is INBOUND. Deliberately NOT
        # the calendar check the outgoing branches use — a meeting appearing on the books proves
        # the user scheduled something, not that the other side delivered what they promised.
        return _check_inbound_delivery(ids, created, local)
    return None


def _s(v) -> str:
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.isoformat()   # unquoted YAML dates parse as date/datetime — stringify as ISO
    return str(v)


_TZ_CACHE: dict = {}


def _user_tzinfo():
    """The user's zone (SOTTO_TIMEZONE / wizard-detected settings, via compose_brief), else UTC.
    Cached per configured value: without the cache the settings file is re-read/parsed for every
    datetime conversion — O(items × events) file I/O per brief."""
    key = _env_tz() or ""
    if key not in _TZ_CACHE:
        _TZ_CACHE[key] = _resolve_tz((key or configured_tz()) or "+00:00") or timezone.utc
    return _TZ_CACHE[key]


def _parse_dt(s):
    """ISO datetime with PROPER offset handling ('Z' / '±HH:MM', 'T' or space separator, date-only).
    The old strptime(...[:19]) silently dropped the UTC offset — an off-by-one day near midnight
    across zones. Returns a (possibly naive) datetime, or None when unparseable."""
    s = _s(s).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:  # tolerate junk after the seconds (e.g. a nonstandard fraction/suffix)
            return datetime.fromisoformat(s[:19].replace(" ", "T"))
        except ValueError:
            return None


def _to_user_zone(dt: datetime) -> datetime:
    """Aware → converted to the user's zone; naive → assumed already user-local (ledger timestamps
    and Mac-side local data carry no offset)."""
    tzi = _user_tzinfo()
    return dt.replace(tzinfo=tzi) if dt.tzinfo is None else dt.astimezone(tzi)


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def chase_after_days() -> int:
    """SOTTO_CHASE_AFTER_DAYS (default 3) — how long a deadline-less `waiting_on` sits before it
    becomes chase-eligible, and the gap between chases. Read like every other knob: env, floored."""
    return max(1, _int((os.environ.get("SOTTO_CHASE_AFTER_DAYS") or "").strip(), CHASE_AFTER_DAYS))


def _days_old(created_at, ref: datetime):
    try:
        return (ref - datetime.strptime(_s(created_at)[:10], "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return None


def chase_due(it: dict, today: str, ref: datetime) -> bool:
    """Is this waiting_on due for a chase? Deadline-less ones ripen after `chase_after_days`;
    a post-deadline one escalates immediately (it's already late); `chase_after` (stamped by the
    last chase) governs afterwards; and after CHASE_MAX chases we stop and leave it to sotto-loops."""
    if not is_waiting_on(it.get("action_type")):
        return False
    if _int(it.get("chased_count")) >= CHASE_MAX:
        return False
    after = _s(it.get("chase_after"))[:10]
    if after:
        return after <= today
    deadline = _s(it.get("deadline"))[:10]
    if deadline:
        return deadline < today          # post-deadline → escalate now, no ripening wait
    age = _days_old(it.get("created_at") or today, ref)
    return age is not None and age >= chase_after_days()


def _clear_stale_pending(active: list, today: str):
    """A chase stamped yesterday but never delivered leaves NO trace: the pending stamp is dropped
    and `chased_count` was never touched. A nudge the user never saw must not be one of the two
    they get."""
    for it in active:
        pending = _s(it.get("chase_pending"))[:10]
        if pending and pending < today:
            it.pop("chase_pending", None)
            _persist(it)


def _stamp_chase(active: list, today: str, ref: datetime):
    """ONE chase stamp per local day, on ONE item — the oldest/most-overdue eligible waiting_on.
    PHASE ONE of two: this writes `chase_pending` and nothing else. `chased_count`, `last_chased_at`
    and `chase_after` all move in --finalize-chase, which proactive_scan calls after the nudge
    actually went out — a chase counts when it is delivered, not when it is proposed. This file
    stays the single writer either way."""
    if any(_s(it.get("last_chased_at"))[:10] == today or _s(it.get("chase_pending"))[:10] == today
           for it in active):
        return                            # already chased (or proposed a chase) today
    overdue = lambda it: bool(_s(it.get("deadline"))[:10] and _s(it.get("deadline"))[:10] < today)
    ranked = sorted(active, key=lambda it: (not overdue(it),
                                            -(_days_old(it.get("created_at"), ref) or 0)))
    for it in ranked:
        if chase_due(it, today, ref):
            it["chase_pending"] = today
            _persist(it)
            return


def finalize_chase(anchor_key: str, now: datetime | None = None) -> dict:
    """PHASE TWO: the nudge was DELIVERED, so the chase now counts. Called by proactive_scan's fired
    path (`continuity_resolve.py --finalize-chase <anchor_key>`) so the ledger keeps exactly one
    writer. Idempotent within the day; a no-op when nothing was pending."""
    now = now or _now_local(configured_tz() or "+00:00")
    today = _to_user_zone(now).strftime("%Y-%m-%d")
    try:
        ref = datetime.strptime(today, "%Y-%m-%d")
    except ValueError:  # pragma: no cover — strftime output always parses
        ref = _to_user_zone(now).replace(tzinfo=None)
    key = (anchor_key or "").strip()
    if not key:
        return {"ok": False, "detail": "missing anchor_key"}
    items = _load_items()
    it = items.get(key) or next((v for v in items.values() if _s(v.get("anchor_key")) == key), None)
    if it is None:
        return {"ok": False, "anchor_key": key, "detail": "no ledger item with that anchor_key"}
    if _s(it.get("last_chased_at"))[:10] == today:
        return {"ok": True, "anchor_key": key, "chased_count": _int(it.get("chased_count")),
                "detail": "already finalized today"}
    if _s(it.get("chase_pending"))[:10] != today:
        return {"ok": False, "anchor_key": key, "chased_count": _int(it.get("chased_count")),
                "detail": "no chase pending for today"}
    it["chased_count"] = _int(it.get("chased_count")) + 1
    it["last_chased_at"] = today
    it["chase_after"] = (ref + timedelta(days=chase_after_days())).strftime("%Y-%m-%d")
    it.pop("chase_pending", None)
    _persist(it)
    return {"ok": True, "anchor_key": key, "chased_count": it["chased_count"],
            "last_chased_at": today, "chase_after": it["chase_after"], "detail": "chase delivered"}


def meeting_passed(meeting_time: str, created_at: str, today: str) -> bool:
    """continuity.rs:536-573 — handles ISO timestamps AND relative times (vs created_at).
    Offset-bearing timestamps are converted to the USER'S zone before taking the date, so a meeting
    stored as e.g. 06:30Z (= 23:30 the previous day in LA) resolves on the right local day."""
    if not meeting_time:
        return False
    mt = meeting_time
    mtl = mt.lower()
    now = datetime.strptime(today, "%Y-%m-%d")
    # ISO-ish: "2026-03-12 10:00" / "2026-03-12T10:00:00-08:00" — compare the USER-LOCAL date part.
    if mt.startswith("20") and len(mt) >= 10:
        dt = _parse_dt(mt)
        if dt is None:
            return mt[:10] < today   # unparseable tail — fall back to the raw date prefix
        return _to_user_zone(dt).strftime("%Y-%m-%d") < today
    try:
        created = datetime.strptime((created_at or "")[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    if "tomorrow" in mtl and created <= now - timedelta(days=1):
        return True
    if "today" in mtl and created <= now - timedelta(days=1):
        return True
    if any(d in mtl for d in WEEKDAYS) and created <= now - timedelta(days=7):
        return True
    return False


def _load_items() -> dict:
    """All ledger entries keyed by anchor_key (falling back to filename), each carrying its
    "_path" for persist-back. Loading/parsing is ledger_io's — one parser for every reader.
    MALFORMED files are skipped entirely: they must never be surfaced as active items and never
    persisted over (that used to rewrite them as '---\\n{}\\n---', destroying the content)."""
    items = {}
    malformed = []
    for fm in ledger_io.load_entries(with_path=True, include_bare=True):
        if fm.get("_malformed"):
            malformed.append(fm["_path"])
            continue
        if not any(k != "_path" for k in fm):
            # Bare file (no frontmatter, e.g. a plain .md dropped in the dir) or an empty '{}'
            # fence: adopting it would surface a content-free open loop and _persist would rewrite
            # the file as '---\n{}\n---', destroying its contents. Never adopt, never persist over.
            continue
        items[fm.get("anchor_key") or os.path.basename(fm["_path"])] = fm
    if malformed:
        print("[continuity_resolve] skipping malformed ledger file(s) (left untouched): "
              + ", ".join(malformed), file=sys.stderr)
    return items


def _row_identity(it: dict) -> dict:
    """A ledger row's identity fields in the action shape compute_anchor_key reads."""
    return {"action_type": it.get("action_type"), "channel": it.get("channel"),
            "canonical_id": it.get("canonical_id"), "contact_identifier": it.get("contact_identifier"),
            "contact_name": it.get("contact_name"), "group_id": it.get("group_id"),
            "source_thread_id": it.get("source_thread_id")}


def _fold_duplicate(keeper: dict, loser: dict, today: str):
    """Two rows, one debt: the OLDER created_at is the debt's real age and brings its chase clock,
    the NEWER words are the live ask, and the loser closes as bookkeeping — `merged_duplicate` is
    terminal and renders as nothing anywhere, because a dedupe is not an outcome that moved."""
    older, newer = ((keeper, loser)
                    if (_s(keeper.get("created_at")) or "9999") <= (_s(loser.get("created_at")) or "9999")
                    else (loser, keeper))
    if older is not keeper:
        keeper["created_at"] = older.get("created_at")
        for k in ("chased_count", "chase_after", "last_chased_at", "chase_pending"):
            if older.get(k) is not None:
                keeper[k] = older[k]
            else:
                keeper.pop(k, None)
    for k in ("summary", "ask", "deadline"):
        if newer is not keeper and newer.get(k):
            keeper[k] = newer[k]
    keeper["times_surfaced"] = max(_int(keeper.get("times_surfaced"), 1),
                                   _int(loser.get("times_surfaced"), 1))
    _terminate(loser, "dismissed", "merged_duplicate", today)
    loser["merged_into"] = _s(keeper.get("anchor_key"))
    _persist(keeper)
    _persist(loser)


def _migrate_identity(items: dict, group_index: dict, today: str) -> list:
    """ONE idempotent migration, run before resolution: a live row whose counterpart is a group in
    today's snapshot is re-anchored onto that group's OWN id, and two rows that turn out to be the
    same counterpart become one. A group ask is keyed by the group's ID and a person ask by the
    person — never by what the extractor typed that day — so a row minted under an invented label
    ("Intro Group") heals into the real one on the next resolve instead of living out its week as a
    second open debt. Terminal rows are never touched: a loop the user closed stays closed."""
    folded = []
    for old_key in list(items):
        it = items.get(old_key)
        if it is None or it.get("status", "open") in TERMINAL:
            continue
        canon = canonicalize_counterpart(_row_identity(it), group_index)
        if not _s(canon.get("group_id")).strip():
            continue                      # not a group ask — its identity is untouched
        new_key = compute_anchor_key(canon)
        twin = items.get(new_key) if new_key != old_key else None
        if twin is not None and twin.get("status", "open") in TERMINAL:
            continue                      # the same debt already closed — never resurrect it
        it["group_id"], it["contact_name"] = canon.get("group_id"), canon.get("contact_name")
        if new_key == old_key:
            _persist(it)
        elif twin is None:
            items.pop(old_key, None)
            it["anchor_key"] = new_key     # the file keeps its path; the key it answers to changes
            items[new_key] = it
            _persist(it)
        else:
            _fold_duplicate(twin, it, today)   # the row already holding the key survives
            items.pop(old_key, None)
            folded.append(it)
    return folded


def resolve(payload: dict, now: datetime | None = None, *,
            merge: bool = True, resolve_existing: bool = True) -> dict:
    """The full pass by default. `merge=False` (--resolve-only) runs BEFORE the brief composes, so
    the brief reasons about a ledger resolved as of this morning instead of last night's;
    `resolve_existing=False` (--merge-only) runs after it, ingesting the brief's own actions[]."""
    now = now or _now_local(configured_tz() or "+00:00")   # user-zone "now", not server UTC
    today = payload.get("today") or _to_user_zone(now).strftime("%Y-%m-%d")
    # A full LOCAL timestamp, not a bare date: `created_at` is the cutoff every inbound-delivery
    # check compares against, and "2026-06-23" sorts before every message sent that day — including
    # the ones that arrived hours BEFORE the promise was made.
    created_stamp = f"{_s(today)[:10]} {_to_user_zone(now).strftime('%H:%M:%S')}"
    signals = payload.get("signals", {}) or {}
    replied = set(signals.get("replied_thread_ids", []))
    handled = signals.get("handled", []) or []   # [{identifier, channel}] from the Already-Handled section
    # The read_local snapshot (+ calendar events) drives cross-channel reply detection. The SKILL
    # tells the agent to pass the read_local JSON AS-IS, which may still be the raw MCP tool-result
    # wrapper — unwrap it the same way compose_brief does, or every cross-channel check sees {}.
    local_data = unwrap_tool_result(payload.get("local") or {})
    if payload.get("events") and "events" not in local_data:
        local_data = {**local_data, "events": payload["events"]}
    items = _load_items()
    # Group identity, straight from the snapshot the model was shown (see canonicalize_counterpart).
    group_index = group_identity(local_data)

    resolved, expired, active = [], [], []
    # Cutoffs derive from the brief's `today` (the deterministic reference the payload carries),
    # NOT the wall clock — so an offline replay / fixture with a fixed `today` resolves identically
    # regardless of when it runs.
    try:
        ref = datetime.strptime(_s(today)[:10], "%Y-%m-%d")
    except ValueError:
        ref = _to_user_zone(now).replace(tzinfo=None)
    retention_cutoff = (ref - timedelta(days=TERMINAL_RETENTION_DAYS)).strftime("%Y-%m-%d")
    age_cutoff = (ref - timedelta(days=AGE_EXPIRY_DAYS)).strftime("%Y-%m-%d")          # continuity.rs:973
    deadline_cutoff = (ref - timedelta(days=DEADLINE_GRACE_DAYS)).strftime("%Y-%m-%d")  # continuity.rs:975

    # 1) merge new actions by anchor_key (bump times_surfaced, reconciler.ts). Accept the brief's
    #    camelCase actionItems OR snake_case via _normalize_action.
    merged = []
    for raw in (payload.get("new_actions", []) if merge else []):
        a = canonicalize_counterpart(_normalize_action(raw), group_index)
        # The ledger holds COMMUNICATION DEBTS — replies owed, follow-ups, commitments,
        # waiting-ons. Meeting prep/info actions are calendar shadows: the docket is their
        # surface and the calendar already resolves them by passing. Creating loops for them
        # buried the real asks under a mirror of tomorrow's schedule (owner-reported).
        # Legacy entries on existing volumes still close via the meeting-passed resolver.
        if _normalize_action_type(a.get("action_type")) in MEETING_TYPES:
            continue
        ak = compute_anchor_key(a)
        if ak in items:
            it = items[ak]
            merged.append(it)
            it["times_surfaced"] = int(it.get("times_surfaced", 1)) + 1
            if it.get("status") not in TERMINAL:
                # A live anchor re-captured: the ASK is today's, not the day it was first seen.
                # `action_type` stays put — direction now has its own anchor family, so a genuine
                # change of direction forks a new item instead of silently inverting this one.
                for k in ("summary", "ask", "deadline"):
                    if a.get(k):
                        it[k] = a[k]
            if it.get("status") in TERMINAL:
                # A NEW action on a TERMINAL anchor = the person came back after the loop closed
                # (e.g. they replied again the day after resolution). Without a re-open the action
                # is absorbed here and step 2 `continue`s on the terminal status — the person
                # vanishes for the whole retention window. Re-open with the fresh ask; the old
                # resolution moves to prior_* so history isn't lost.
                if it.get("resolution"):
                    it["prior_resolution"] = it["resolution"]
                    it["prior_resolved_at"] = _s(it.get("resolved_at"))[:10] or None
                it.pop("resolution", None)
                it.pop("resolved_at", None)
                it["status"] = "open"
                it["reopened_at"] = today
                it["created_at"] = a.get("created_at") or created_stamp   # fresh ask — restart the clock
                for k in ("chased_count", "chase_after", "last_chased_at", "chase_pending"):
                    it.pop(k, None)      # …and a fresh chase clock: last month's chases aren't this ask's
                for k in ("summary", "ask", "channel", "meeting_time", "deadline"):
                    if a.get(k):
                        it[k] = a[k]
        else:
            items[ak] = {
                "anchor_key": ak, "action_type": a.get("action_type"), "channel": a.get("channel"),
                "contact_name": a.get("contact_name"), "contact_identifier": a.get("contact_identifier"),
                "canonical_id": a.get("canonical_id"), "status": "open",
                "created_at": a.get("created_at") or created_stamp, "times_surfaced": 1,
                "summary": a.get("summary", ""), "ask": a.get("ask"),
                "meeting_time": a.get("meeting_time"), "deadline": a.get("deadline"),
                "source_thread_id": a.get("source_thread_id"),
                "group_id": a.get("group_id"),
            }
            merged.append(items[ak])

    if not resolve_existing:
        # --merge-only: persist exactly what this pass touched (nothing else is rewritten, so a
        # dashboard/retune write landing in the same window survives) and report what's open.
        for it in merged:
            _persist(it)
        open_now = [it for it in items.values()
                    if it.get("status", "open") not in TERMINAL
                    and not (_s(it.get("snoozed_until"))[:10] > today)]
        return {"resolved": [], "expired": [], "active": _strip(open_now)}

    # 1.5) identity migration — re-anchor group rows onto the group's own id and fold the duplicates
    #      an invented label had already minted, BEFORE resolution, so one debt resolves once.
    _migrate_identity(items, group_index, today)

    # 2) deterministic resolution (continuity.rs:978-1031 + resolve_from_handled:1035-1068).
    for ak, it in items.items():
        status = it.get("status", "open")
        # _s() everywhere we slice: yaml.safe_load yields datetime.date for unquoted dates and
        # None for explicit nulls — a raw [:10] on those is a TypeError that kills the whole step.
        if status in TERMINAL:
            if (_s(it.get("resolved_at")) or "9999")[:10] < retention_cutoff:   # prune past retention
                _remove(it)
            continue
        created = (_s(it.get("created_at")) or today)[:10]
        tid = _s(it.get("source_thread_id")).strip()

        # a) replied on the tracked thread (email — most precise)
        if tid and tid in replied:
            _terminate(it, "resolved", "replied", today); resolved.append(it); _persist(it); continue
        # b) CROSS-CHANNEL: did the user answer this person on any channel (iMessage/WhatsApp/call/
        #    calendar) since the action was created? The moat — a reply via a different channel
        #    than the original still closes the loop.
        cross = _check_action_resolution(it, local_data, now)
        if cross:
            _terminate(it, "resolved", cross[0], today); it["resolution_evidence"] = cross[1]
            resolved.append(it); _persist(it); continue
        # c) contact appeared in the brief's Already-Handled section (cross-channel id match)
        if _handled_match(it, handled):
            _terminate(it, "resolved", "brief_handled", today); resolved.append(it); _persist(it); continue
        # d) aged out (open 7d+ with no resolution signal — loops must not pile up forever).
        #    DIRECTION-AWARE: what you OWE expires quietly; what you're OWED is never expired —
        #    silently dropping someone's debt to you is the opposite of a chief of staff. A
        #    `waiting_on` becomes chase-eligible instead (step h) and leaves only by delivery,
        #    by its deadline, or by the user's hand in sotto-loops.
        is_waiting = is_waiting_on(it.get("action_type"))
        if created < age_cutoff and not is_waiting:
            _terminate(it, "expired", "expired", today); expired.append(it); _persist(it); continue
        # e) deadline passed (2d grace) — for what the USER owes. A deadline never kills a
        #    `waiting_on`: a due date makes a debt owed to the user MORE protected, not less, so a
        #    passed one only makes it chase-eligible immediately and then hands off to sotto-loops.
        dl = _s(it.get("deadline"))[:10]
        if dl and dl < deadline_cutoff and not is_waiting:
            _terminate(it, "expired", "deadline_passed", today); expired.append(it); _persist(it); continue
        # e2) …with one exit: a waiting_on Sotto has chased its full quota on and has NO channel to
        #     chase through (no contact_identifier — it can neither self-resolve nor be nudged) is
        #     closed honestly rather than sat on forever.
        if (is_waiting and not _s(it.get("contact_identifier")).strip()
                and _int(it.get("chased_count")) >= CHASE_MAX):
            _terminate(it, "expired", "unreachable", today); expired.append(it); _persist(it); continue
        # f) meeting passed (meeting types only) → resolved, not expired
        is_meeting = _normalize_action_type(it.get("action_type")) in MEETING_TYPES
        if is_meeting and (meeting_passed(_s(it.get("meeting_time")), created, today)
                           or (not it.get("meeting_time") and created < deadline_cutoff)):
            _terminate(it, "resolved", "meeting_passed", today); resolved.append(it); _persist(it); continue
        # g) user-snoozed (via sotto-loops): keep the file, but don't surface until the date passes.
        if _s(it.get("snoozed_until"))[:10] > today:
            _persist(it); continue
        active.append(it); _persist(it)

    # h) the chase clock: yesterday's undelivered proposal expires, then stamp (at most) one
    #    chase-due waiting_on as PENDING for today. proactive_scan delivers it and calls
    #    --finalize-chase; nothing else writes chase state.
    _clear_stale_pending(active, today)
    _stamp_chase(active, today, ref)

    return {"resolved": _strip(resolved), "expired": _strip(expired), "active": _strip(active)}


def _strip(lst: list) -> list:
    return [{k: v for k, v in it.items() if k != "_path"} for it in lst]


def _terminate(it: dict, status: str, resolution: str, today: str):
    it["status"], it["resolution"], it["resolved_at"] = status, resolution, today


def _handled_match(it: dict, handled: list) -> bool:
    ident = it.get("contact_identifier") or ""
    if not ident:
        return False
    it_ch = normalize_channel(it.get("channel", ""))
    for h in handled:
        if normalize_channel(h.get("channel", "")) == it_ch and _identifiers_match(ident, h.get("identifier", "")):
            return True
    return False


def _persist(it: dict):
    os.makedirs(_dir(), exist_ok=True)
    path = it.get("_path") or os.path.join(_dir(), f"{_safe(it['anchor_key'])}.md")
    fm = {k: v for k, v in it.items() if k != "_path"}
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)}---\n")


def _remove(it: dict):
    p = it.get("_path")
    if p and os.path.exists(p):
        os.remove(p)


def _safe(s: str) -> str:
    # Non-alnum → '-' (no traversal) + a short hash of the full key so distinct anchor_keys
    # that normalize to the same chars (e.g. "thread:A/B" vs "thread:A-B") don't collide.
    import hashlib
    slug = "".join(c if c.isalnum() else "-" for c in s)[:72]
    return f"{slug}-{hashlib.sha256(s.encode()).hexdigest()[:8]}"


def main():
    argv = sys.argv[1:]
    if "--finalize-chase" in argv:
        i = argv.index("--finalize-chase")
        print(json.dumps(finalize_chase(argv[i + 1] if len(argv) > i + 1 else ""), default=_s))
        return
    merge = "--resolve-only" not in argv
    resolve_existing = "--merge-only" not in argv
    files = [a for a in argv if not a.startswith("--")]
    raw = open(files[0]).read() if files else sys.stdin.read()
    result = resolve(json.loads(raw), merge=merge, resolve_existing=resolve_existing)
    try:  # visibility into the continuity loop (served at /debug/brief-log)
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "lib"))
        from sotto_log import diag
        diag(f"[continuity_resolve] {len(result.get('active', []))} open loops, "
             f"{len(result.get('resolved', []))} resolved, {len(result.get('expired', []))} expired")
    except Exception:
        pass
    # default=_s: items loaded from frontmatter can carry datetime.date values (unquoted YAML
    # dates) — they must serialize as ISO strings, not kill the step at the very last print.
    print(json.dumps(result, default=_s))


if __name__ == "__main__":
    main()
