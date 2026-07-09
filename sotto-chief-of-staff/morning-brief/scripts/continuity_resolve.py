#!/usr/bin/env python3
"""
continuity_resolve.py — deterministic resolution of open loops across briefs.

PORT SOURCE: app/src-tauri/src/database/continuity.rs (+ pipeline/deterministic.ts, reconciler.ts)
Runs on Hermes (execute_code) over knowledge/continuity/*.md on $SOTTO_DATA, BEFORE the LLM.
Resolves passed meetings / replied emails / expired items, dedupes by anchor_key, bumps
times_surfaced, and prunes terminal items past retention.

The new_actions may be the brief's raw `actions[]` (camelCase `actionItems`: type, channel,
contactName, contactIdentifier, emailThreadId, meetingTime, deadlineDate, contextSummary…) OR the
internal snake_case shape — `_normalize_action` accepts both, mirroring the Mac pipeline's
ActionItem mapping before continuity.rs sees it.

Stdin/arg JSON: { "today": "2026-06-23",
                  "signals": { "replied_thread_ids": [...],
                               "handled": [ {identifier, channel} ] },   # Already-Handled section
                  "new_actions": [ <brief actions[] or snake_case actions> ] }
Prints { "resolved":[...], "expired":[...], "active":[...] }
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
import compose_brief as cb  # noqa: E402
import ledger_io  # noqa: E402

TERMINAL_RETENTION_DAYS = 30          # continuity.rs:13
AGE_EXPIRY_DAYS = 7                   # continuity.rs:973 (expiry_7d)
DEADLINE_GRACE_DAYS = 2              # continuity.rs:975 (expiry_2d_date)
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


def action_family(t: str) -> str:
    # continuity.rs:521-528 — group related types so reply≠follow_up≠call_back duplicate per person.
    t = (t or "").lower()
    if t in ("reply", "follow_up", "follow_up_stale", "call_back", "waiting_on"):
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


def contact_anchor(canonical_id, identifier, name) -> str:
    # continuity.rs:272-282 — cid: > id: > name: (so the same person via phone vs email matches).
    if canonical_id and str(canonical_id).strip():
        return f"cid:{str(canonical_id).strip().lower()}"
    norm = _normalize_identifier_for_anchor(identifier or "")
    if norm:
        return f"id:{norm}"
    return f"name:{_normalize_name_for_dedup(name or '')}"


def compute_anchor_key(a: dict) -> str:
    # continuity.rs:284-304 — thread id wins, else channel:family:contact_anchor.
    tid = (a.get("source_thread_id") or "").strip()
    if tid:
        return f"thread:{tid}"
    return f"{normalize_channel(a.get('channel',''))}:{action_family(a.get('action_type',''))}:" \
           f"{contact_anchor(a.get('canonical_id'), a.get('contact_identifier'), a.get('contact_name',''))}"


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
    at = (it.get("action_type") or "").lower()
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
    key = cb._env_tz() or ""
    if key not in _TZ_CACHE:
        _TZ_CACHE[key] = cb._resolve_tz((key or cb.configured_tz()) or "+00:00") or timezone.utc
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
        items[fm.get("anchor_key") or os.path.basename(fm["_path"])] = fm
    if malformed:
        print("[continuity_resolve] skipping malformed ledger file(s) (left untouched): "
              + ", ".join(malformed), file=sys.stderr)
    return items


def resolve(payload: dict, now: datetime | None = None) -> dict:
    now = now or cb._now_local(cb.configured_tz() or "+00:00")   # user-zone "now", not server UTC
    today = payload.get("today") or _to_user_zone(now).strftime("%Y-%m-%d")
    signals = payload.get("signals", {}) or {}
    replied = set(signals.get("replied_thread_ids", []))
    handled = signals.get("handled", []) or []   # [{identifier, channel}] from the Already-Handled section
    # The read_local snapshot (+ calendar events) drives cross-channel reply detection.
    local_data = payload.get("local") or {}
    if payload.get("events") and "events" not in local_data:
        local_data = {**local_data, "events": payload["events"]}
    items = _load_items()

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
    for raw in payload.get("new_actions", []):
        a = _normalize_action(raw)
        ak = compute_anchor_key(a)
        if ak in items:
            items[ak]["times_surfaced"] = int(items[ak].get("times_surfaced", 1)) + 1
        else:
            items[ak] = {
                "anchor_key": ak, "action_type": a.get("action_type"), "channel": a.get("channel"),
                "contact_name": a.get("contact_name"), "contact_identifier": a.get("contact_identifier"),
                "canonical_id": a.get("canonical_id"), "status": "open",
                "created_at": a.get("created_at") or today, "times_surfaced": 1,
                "summary": a.get("summary", ""), "ask": a.get("ask"),
                "meeting_time": a.get("meeting_time"), "deadline": a.get("deadline"),
                "source_thread_id": a.get("source_thread_id"),
            }

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
        # d) aged out (open 7d+ with no resolution signal — loops must not pile up forever)
        if created < age_cutoff:
            _terminate(it, "expired", "expired", today); expired.append(it); _persist(it); continue
        # e) deadline passed (2d grace)
        dl = _s(it.get("deadline"))[:10]
        if dl and dl < deadline_cutoff:
            _terminate(it, "expired", "deadline_passed", today); expired.append(it); _persist(it); continue
        # f) meeting passed (meeting types only) → resolved, not expired
        is_meeting = (it.get("action_type") or "") in MEETING_TYPES
        if is_meeting and (meeting_passed(_s(it.get("meeting_time")), created, today)
                           or (not it.get("meeting_time") and created < deadline_cutoff)):
            _terminate(it, "resolved", "meeting_passed", today); resolved.append(it); _persist(it); continue
        # g) user-snoozed (via sotto-retune): keep the file, but don't surface until the date passes.
        if _s(it.get("snoozed_until"))[:10] > today:
            _persist(it); continue
        active.append(it); _persist(it)

    strip = lambda lst: [{k: v for k, v in it.items() if k != "_path"} for it in lst]
    return {"resolved": strip(resolved), "expired": strip(expired), "active": strip(active)}


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
    raw = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
    result = resolve(json.loads(raw))
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
