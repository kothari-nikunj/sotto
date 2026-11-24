#!/usr/bin/env python3
"""loop_check.py — what happened after an open ask: close it, or mark it for checking.

Usage: loop_check.py <continuity payload> [--granola <gather_granola output>]
       (the same continuity file continuity_resolve.py --merge-only reads)

Three signals close a task on their own (owner, Nov 2026), each through the ledger's one writer and
named in the evening brief with "tell me if that's wrong":
  - an intro ask: the owner started an email thread with the asker and the named intro target on To/Cc;
  - a scheduling ask: a small non-recurring meeting with them, organized by you or them, was booked
    after the ask and you accepted or organized it;
  - a callback ask: you answered a phone call from them after the ask, or a call you dialed lasted
    30 seconds or more (a shorter dialed call is only a hint; WhatsApp calls never count).
One signal closes at most one task: when two open tasks could claim it, neither closes. A close
the owner says was wrong reopens the same row, and neither that signal nor anything before the undo
closes it again (`signal_closes`). A meeting cancelled or declined before it starts reopens the task
it closed. Anything else only gets a check note (`loop_checks`): for a scheduling ask, a meeting
with them on the calendar; otherwise the latest thing you did after the ask — a message to them (or
the group), a connected phone call, for a callback ask a WhatsApp call that was not missed, a
message to the person the task is for (`deliver_to`), or a Granola meeting with them.
A later message from them clears the note. On a scheduling ask, readable text without a "?"
keeps your message's note with `replied_since`. A signal the owner rejected never becomes a note.
A past scheduling deadline gets `dates_passed` only when a complete current Calendar window has no upcoming meeting with them.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from email.utils import getaddresses

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "_shared", "lib"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "_shared", "scripts"))
import continuity_resolve as cr  # noqa: E402
import ledger_io  # noqa: E402
import loop_checks  # noqa: E402
import signal_closes  # noqa: E402
from calendar_context import organized_by_owner, owner_rsvp  # noqa: E402
from delivery_effects import loop_version  # noqa: E402
from loop_interactions import _exchanges, _instant, _origins_allowed, interaction_hint  # noqa: E402
from loop_snapshot import snapshot_local  # noqa: E402
from source_context import allowed  # noqa: E402
from textutil import _s  # noqa: E402
from timeutil import _calendar_day, configured_user_email, parse_observed_time  # noqa: E402

# fulfillment → the resolution an automatic close records (the brief's receipt vocabulary).
CLOSES = {"intro": "intro_sent", "schedule": "scheduled_meeting", "callback": "called"}
# Hints that outlive their source in the snapshot (a calendar meeting is always re-read).
CARRIED = {"you_messaged", "call", "sent_to_them", "met", "meeting_cancelled"}


def _attending(event: dict) -> bool:
    owner = configured_user_email()
    rsvp = owner_rsvp(event, owner)
    return rsvp in ("accepted", "tentative") if rsvp is not None else organized_by_owner(event, owner)


def fulfillment(row: dict) -> str:
    """intro | schedule | callback, from the captured field or the legacy action type; else ""."""
    kind = _s(row.get("fulfillment"))
    if kind in CLOSES:
        return kind
    if kind == "none":
        return ""
    action = ledger_io.normalize_action_type(row.get("action_type"))
    return ("schedule" if action in ledger_io.SCHEDULING_TYPES else
            "callback" if action == "call_back" else "")


def _matches(handle: str, identifiers: list) -> bool:
    return any(cr._handle_matches(handle, i) or cr._jid_matches_phone(handle, i) for i in identifiers)


def _known_target(target: str, local: dict, person_index: dict, identifiers: list) -> bool:
    """The intro target is a person Sotto knows (Contacts or the graph), not the owner or the asker."""
    if not target or target == configured_user_email().strip().lower() or _matches(target, identifiers):
        return False
    cards = {_s(email).lower() for card in local.get("contacts") or [] if isinstance(card, dict)
             for email in card.get("emails") or []}
    by_identifier = ((person_index or {}).get("index") or {}).get("by_identifier") or {}
    graph = cr._kg.normalize_identifier(target) if cr._kg else target
    return target in cards or graph in by_identifier


def _intro_sent(row: dict, local: dict, identifiers: list, cutoff: str, person_index: dict):
    """Emails the owner sent after the ask that start a thread with both the asker and the target on
    To/Cc. A reply on a thread both happen to be on is not an intro, and wording is never enough:
    a promise to intro later and a quoted ask read the same."""
    target = _s(row.get("deliver_to")).lower()
    if not _known_target(target, local, person_index, identifiers):
        return
    emails = [m for m in local.get("emails") or [] if isinstance(m, dict)]
    for message in emails:
        if not (cr._email_direction_known(message) and cr._is_sent_email(message)):
            continue
        stamp = message.get("date") or message.get("timestamp")
        thread, sent_at = _s(message.get("threadId")), _instant(stamp)
        earlier = [_instant(other.get("date") or other.get("timestamp")) for other in emails
                   if other is not message and thread and _s(other.get("threadId")) == thread]
        if (_s(message.get("subject")).strip().lower().startswith("re:") or sent_at is None
                or any(at is None or at <= sent_at for at in earlier)):
            continue
        copied = [addr.lower() for _name, addr in getaddresses(
            [_s(message.get(field)) for field in ("to", "cc") if message.get(field)]) if addr]
        source_id, when = _s(message.get("messageId") or message.get("id")), _instant(stamp)
        if (source_id and when and cr._message_is_after(stamp, cutoff) and target in copied
                and any(_matches(addr, identifiers) for addr in copied)):
            yield {"basis": "intro_sent", "source": "gmail", "kind": "email",
                   "source_id": source_id, "at": when.isoformat()}


def _going(event: dict, identifiers: list) -> bool:
    """A meeting between you and them, not an event you both got invited to: you accepted or
    organized it (a tentative reply is not going), you or they organized it, and it is small."""
    from granola_graph import MAX_ATTENDEES_FOR_GRAPH
    people = [a for a in event.get("attendees") or [] if isinstance(a, dict) and a.get("resource") is not True]
    organizer = event.get("organizer") if isinstance(event.get("organizer"), dict) else {}
    owner = configured_user_email()
    rsvp = owner_rsvp(event, owner)
    organized = organized_by_owner(event, owner)
    return (len(people) <= MAX_ATTENDEES_FOR_GRAPH
            and (rsvp == "accepted" if rsvp is not None else organized)
            and (organized or _matches(_s(organizer.get("email")).lower(), identifiers)))


def _booked(local: dict, identifiers: list, cutoff: str):
    """One-off meetings with them, created and starting after the ask, that you are going to."""
    for event in local.get("events") or []:
        if (_s(event.get("status")) == "cancelled" or event.get("recurringEventId")
                or event.get("recurrence") or not _going(event, identifiers)):
            continue
        start = event.get("start")
        stamp = start.get("dateTime") or start.get("date") if isinstance(start, dict) else start
        created = event.get("created")
        if not (created and cr._message_is_after(created, cutoff) and cr._message_is_after(stamp, cutoff)):
            continue
        people = [a for a in event.get("attendees") or [] if isinstance(a, dict)]
        if any(a.get("responseStatus") != "declined" and _matches(_s(a.get("email")).lower(), identifiers)
               for a in people) and _s(event.get("id")) and _instant(created):
            yield {"basis": "scheduled_meeting", "source": "calendar", "kind": "calendar",
                   "source_id": _s(event["id"]), "at": _instant(created).isoformat(), "starts_at": stamp}


DIALED_CLOSE_SECONDS = 30


def _called(local: dict, identifiers: list, cutoff: str):
    """Connected phone calls with them after the ask, in either direction. `answered` marks a call
    that closes a callback: one from them that you picked up, or one you dialed that lasted
    `DIALED_CLOSE_SECONDS` or more (owner, Nov 11 2026). `key` names the call itself; the evidence id
    also hashes the deploy revision and permissions, so it cannot be what an undo remembers."""
    render = cr._render_local
    for call in local.get("calls") or []:
        if not render or render.phone_call_connection(call) is not True:
            continue
        stamp = call.get("timestamp_utc") or call.get("timestamp")
        call_id, _text = render.call_evidence(call, "calls")
        when = _instant(stamp)
        if call_id and when and cr._message_is_after(stamp, cutoff) and _matches(_s(call.get("phone")), identifiers):
            direction = "out" if call.get("is_outgoing") else "in"
            yield {"basis": "called", "source": "calls", "kind": "calls", "source_id": call_id,
                   "at": when.isoformat(), "answered": call.get("is_outgoing") is False
                   or (call.get("is_outgoing") is True and call["duration_seconds"] >= DIALED_CLOSE_SECONDS),
                   "key": f"{render.normalize_phone_for_comparison(_s(call.get('phone')))}|{when.isoformat()}|{direction}"}


def _whatsapp_called(row: dict, local: dict, identifiers: list, cutoff: str):
    """(when, "whatsapp_calls") for each WhatsApp call with them that was not missed, after the ask
    and, on a callback ask, after their latest missed call (owner, Nov 11 2026: any task you owe
    them). The row has no duration and cannot prove a connection, so it is a hint and never a close."""
    since = _s(row.get("callback_since")) if fulfillment(row) == "callback" else ""
    for call in local.get("whatsapp_calls") or []:
        stamp = call.get("timestamp_utc") or call.get("timestamp")
        if (call.get("is_missed") is False and cr._message_is_after(stamp, cutoff)
                and (not since or cr._message_is_after(stamp, since)) and _matches(_s(call.get("jid")), identifiers)):
            yield _instant(stamp), "whatsapp_calls"


def closing_signals(row: dict, local: dict, person_index: dict) -> list:
    """Every owner-approved signal that could have fulfilled this ask, oldest snapshot order first."""
    kind = fulfillment(row)
    if (not kind or row.get("group_id") or row.get("resolution_mode") == "explicit"
            or ledger_io.is_waiting_on(row.get("action_type")) or not _origins_allowed(row)):
        return []
    identifiers = cr._verified_identifiers(row, local, person_index)
    cutoff = cr._inbound_cutoff(row.get("created_at"))
    if not identifiers or not cutoff:
        return []
    since = _s(row.get("callback_since"))   # a later missed call: only a call after it counts
    found = (_intro_sent(row, local, identifiers, cutoff, person_index) if kind == "intro" else
             _booked(local, identifiers, cutoff) if kind == "schedule" else
             (c for c in _called(local, identifiers, cutoff)
              if c["answered"] and (not since or cr._message_is_after(c["at"], since))))
    return [s for s in found if not signal_closes.rejected(row, s["basis"], s["source_id"], s.get("key", ""))
            and not signal_closes.before_undo(row, s["at"])]


def closing_signal(row: dict, local: dict, person_index: dict):
    """The first signal that fulfilled this ask, or None."""
    return next(iter(closing_signals(row, local, person_index)), None)


def _fell_through(row: dict, local: dict, person_index: dict, now: datetime, cancellations: dict) -> bool:
    """The meeting that closed this task was cancelled, or you or they declined it, before it started."""
    refs = row.get("resolution_source_refs") or [{}]
    source_id = _s(refs[0].get("sourceId"))
    event = next((e for e in local.get("events") or [] if isinstance(e, dict)
                  and source_id and e.get("id") == source_id), None)
    cancelled = cancellations.get(source_id) if event is None else None
    if row.get("resolution") != "scheduled_meeting" or (event is None and cancelled is None):
        return False
    start = (event or cancelled).get("start") or row["resolution_basis"].get("starts_at")
    starts = _instant(start.get("dateTime") or start.get("date") if isinstance(start, dict) else start)
    if starts is None or starts <= cr._to_user_zone(now):
        return False
    identifiers = cr._verified_identifiers(row, local, person_index)
    return (cancelled is not None or _s(event.get("status")) == "cancelled"
            or owner_rsvp(event, configured_user_email()) == "declined" or any(
        isinstance(a, dict) and a.get("responseStatus") == "declined"
        and _matches(_s(a.get("email")).lower(), identifiers) for a in event.get("attendees") or []))


def _close(row: dict, signal_found: dict, now: datetime) -> None:
    version = loop_version(row)
    cr._terminate(row, "resolved", signal_found["basis"], cr._to_user_zone(now).strftime("%Y-%m-%d"), now=now)
    row["closed_at"] = signal_found["at"]
    row["resolution_source_refs"] = [{"sourceType": signal_found["kind"], "sourceId": signal_found["source_id"]}]
    row["resolution_loop_version"] = version
    row["resolution_basis"] = {"basis": signal_found["basis"], "source": signal_found["source"],
                               "at": signal_found["at"], "recorded_at": now.isoformat(),
                               **({"starts_at": signal_found["starts_at"]} if "starts_at" in signal_found else {}),
                               **({"key": signal_found["key"]} if signal_found.get("key") else {})}
    cr._persist(row, signal_close="close")


def _owner_addresses(local: dict) -> set:
    """The owner's own addresses the sources show: the configured email, the From of sent mail,
    and the owner's attendee entry on calendar events."""
    own = {configured_user_email().strip().lower()}
    own |= {cr.parseaddr(_s(m.get("from") or m.get("sender")))[1].lower() for m in local.get("emails") or []
            if isinstance(m, dict) and cr._email_direction_known(m) and cr._is_sent_email(m)}
    own |= {_s(a.get("email")).lower() for e in local.get("events") or [] if isinstance(e, dict)
            for a in e.get("attendees") or [] if isinstance(a, dict) and a.get("self") is True}
    return own - {""}


def _sent_to_them(row: dict, local: dict, identifiers: list, cutoff: str):
    """(when, source) for each direct message or email you sent the person the task is for.
    An email the owner rejected as this task's intro close is not a hint either."""
    target = _s(row.get("deliver_to")).lower()
    if not target or _matches(target, identifiers) or _matches(target, sorted(_owner_addresses(local))):
        return
    for source in ("imessage", "whatsapp"):
        for message in local.get(source) or []:
            stamp = message.get("timestamp_utc") or message.get("timestamp")
            if (message.get("is_from_me") is True and not cr._observed_group_id(message, source)
                    and not message.get("is_group_chat") and cr._message_is_after(stamp, cutoff)
                    and _matches(_s(message.get("handle") or message.get("contact_jid")), [target])):
                yield _instant(stamp), source
    for message in local.get("emails") or []:
        stamp = message.get("date") or message.get("timestamp")
        if (cr._email_direction_known(message) and cr._is_sent_email(message) and cr._message_is_after(stamp, cutoff)
                and any(_matches(h, [target]) for h in cr._email_recipients(message))
                and not signal_closes.rejected(row, "intro_sent", _s(message.get("messageId") or message.get("id")))):
            yield _instant(stamp), "gmail"


def _met(meetings: list, identifiers: list, cutoff: str):
    """(when, "granola") for each Granola meeting with them after the ask, from the gather's own
    output (attendees and exact start only; a day-only date cannot be placed against the ask)."""
    import source_context
    if not source_context.allowed("granola"):
        return
    for meeting in meetings:
        stamp = meeting.get("start") if isinstance(meeting, dict) else None
        if stamp and cr._message_is_after(stamp, cutoff) and any(
                _matches(_s(email).lower(), identifiers) for email in meeting.get("attendee_emails") or []):
            yield _instant(stamp), "granola"


def _meeting(row: dict, local: dict, identifiers: list, cutoff: str):
    """Always from the current calendar: a cancelled or removed meeting drops its note."""
    for event in local.get("events") or []:
        if (_s(event.get("status")) == "cancelled" or not _attending(event)
                or signal_closes.rejected(row, "scheduled_meeting", _s(event.get("id")))):
            continue
        start = event.get("start")
        stamp = start.get("dateTime") or start.get("date") if isinstance(start, dict) else start
        if not cr._message_is_after(stamp, cutoff):
            continue
        for attendee in event.get("attendees") or []:
            if not isinstance(attendee, dict) or attendee.get("responseStatus") == "declined":
                continue
            email = _s(attendee.get("email")).lower()
            if email and any(cr._handle_matches(email, i) for i in identifiers):
                return {"basis": "meeting", "at": _s(stamp), "source": "calendar"}
    return None


def _replied_since(row: dict, note: dict, seen, inbound: list):
    later = [(when, text, source) for when, text, source in inbound if when > seen]
    if (fulfillment(row) != "schedule" or note.get("basis") != "you_messaged"
            or any(not text.strip() or "?" in text for _when, text, _source in later)):
        return None
    prior = _instant(note.get("replied_since"))
    if prior is not None and prior > seen:
        later.append((prior, "", note.get("replied_source")))
    if not later:
        return None
    latest, _, source = max(later, key=lambda message: message[0])
    return {**note, "replied_since": latest.isoformat(), "replied_source": source}


def _calendar_covers_now(observation, now: datetime) -> bool:
    if (not allowed("calendar") or not isinstance(observation, dict)
            or observation.get("status") != "ok" or observation.get("complete") is not True):
        return False
    coverage = observation.get("coverage")
    if not isinstance(coverage, dict):
        return False
    since = parse_observed_time(coverage.get("since"))
    until = parse_observed_time(coverage.get("until"))
    return since is not None and until is not None and since <= now < until


def _dates_passed(row: dict, local: dict, person_index: dict, now: datetime):
    deadline = _calendar_day(row.get("deadline"))
    if (row.get("group_id") or fulfillment(row) != "schedule" or not deadline
            or deadline >= cr._to_user_zone(now).date().isoformat()
            or not cr._verified_identifiers(row, local, person_index)):
        return None
    return {"basis": "dates_passed", "at": deadline, "source": "calendar"}


def signal(row: dict, local: dict, person_index: dict, previous: dict | None = None, meetings: list = ()):
    identifiers = cr._verified_identifiers(row, local, person_index)
    cutoff = cr._inbound_cutoff(row.get("created_at"))
    if not identifiers and not row.get("group_id"):
        return None
    # A meeting that includes them says nothing about a task that is not a scheduling ask.
    meeting = (_meeting(row, local, identifiers, cutoff)
               if not row.get("group_id") and fulfillment(row) == "schedule" else None)
    if meeting:
        return meeting
    steps, inbound = [], []   # (when, basis, source) for what you did; (when, text, source) for their messages
    for when, outgoing, source, text in _exchanges(row, local, identifiers, cutoff):
        if when is not None and outgoing:
            steps.append((when, "you_messaged", source))
        elif when is not None:
            inbound.append((when, text, source))
    last_in = max((when for when, _text, _source in inbound), default=None)
    since = _s(row.get("callback_since")) if fulfillment(row) == "callback" else ""
    if not row.get("group_id"):
        steps += [(_instant(call["at"]), "call", "calls") for call in _called(local, identifiers, cutoff)
                  if not signal_closes.rejected(row, "called", call["source_id"], call["key"])
                  and (not since or cr._message_is_after(call["at"], since))]
        steps += [(when, "call", source) for when, source in _whatsapp_called(row, local, identifiers, cutoff)]
        steps += [(when, "sent_to_them", source) for when, source in _sent_to_them(row, local, identifiers, cutoff)]
        steps += [(when, "met", source) for when, source in _met(meetings, identifiers, cutoff)]
    steps = [step for step in steps if step[0] is not None]
    if steps:
        when, basis, source = max(steps, key=lambda step: step[0])
        prior = _instant(previous.get("at")) if previous and previous.get("basis") in CARRIED else None
        if prior is not None and prior >= when:
            return _carried(row, previous, since, last_in, inbound)
        note = {"basis": basis, "at": when.isoformat(), "source": source}
        reply = _instant(previous.get("replied_since")) if previous else None
        if reply is not None and when < reply:
            note.update(replied_since=previous["replied_since"], replied_source=previous.get("replied_source"))
        replied = _replied_since(row, note, when, inbound)
        if replied:
            return replied
        if (last_in is None or when > last_in) and not note.get("replied_since"):
            return note
    return _carried(row, previous, since, last_in, inbound)


def _carried(row: dict, previous: dict | None, since: str, last_in, inbound: list):
    """A note survives a day its source left the snapshot, unless they wrote back since (on a
    scheduling ask, with a question) or its source is no longer allowed. A calendar note always
    comes from the current calendar."""
    if not previous or previous.get("basis") not in CARRIED or not allowed(_s(previous.get("source"))):
        return None
    if previous.get("source") in ("whatsapp_calls", "calls") and since and not cr._message_is_after(previous.get("at"), since):
        return None   # a saved call hint must still come after their latest missed call
    seen = _instant(previous.get("at"))
    if not seen or last_in is None or last_in < seen:
        return previous if seen else None
    return _replied_since(row, previous, seen, inbound)


def _granola_meetings(granola) -> list:
    """gather_granola output ({"meetings": [...]} or a bare list), or [] when absent."""
    meetings = granola.get("meetings") if isinstance(granola, dict) else granola
    return [m for m in meetings or [] if isinstance(m, dict)]


def run(payload: dict, now: datetime | None = None, granola=None) -> dict:
    local = snapshot_local(payload)
    meetings = _granola_meetings(granola)
    person_index = cr.person_identity()
    previous = loop_checks.read(include_hidden_replies=True)  # keep a hidden reply from becoming an unanswered note
    now = now or datetime.now(timezone.utc)
    calendar_complete = _calendar_covers_now(payload.get("calendar_observation"), now)
    checks, closed = {}, 0
    cancellations = {e['id']: e for e in payload.get("calendar_cancellations", [])
                     if isinstance(e, dict) and isinstance(e.get('id'), str)} if allowed("calendar") else {}
    with cr._ledger_lock():
        ledger = list(cr._load_items().values())
        for row in ledger:
            if signal_closes.is_signal_close(row) and _fell_through(row, local, person_index, now, cancellations):
                signal_closes.reopen(row, cr._to_user_zone(now).strftime("%Y-%m-%d"), reject=False)
                cr._persist(row, signal_close="reopen")
                checks[loop_version(row)] = {"basis": "meeting_cancelled", "at": now.isoformat(), "source": "calendar"}
        rows = [row for row in ledger if row.get("status", "open") in ledger_io.ACTIVE | ledger_io.PARKED
                and not ledger_io.is_waiting_on(row.get("action_type"))
                and ledger_io.normalize_action_type(row.get("action_type")) not in ledger_io.MEETING_TYPES]
        for row in ledger:
            if row.get("status", "open") in ledger_io.ACTIVE and ledger_io.is_waiting_on(row.get("action_type")):
                hint = interaction_hint(row, local, person_index, now)
                if hint:
                    checks[loop_version(row)] = hint
        # One signal proves at most one task: one that already closed a task is spent, and one
        # that two open tasks could claim closes neither.
        name = lambda basis, s: (basis, s.get("key") or s.get("source_id") or s.get("sourceId"))  # noqa: E731
        spent = {name(r["resolution"], {**(r.get("resolution_source_refs") or [{}])[0], **r["resolution_basis"]})
                 for r in ledger if signal_closes.is_signal_close(r)}
        candidates = [[s for s in closing_signals(row, local, person_index) if name(s["basis"], s) not in spent]
                      for row in rows]
        claims = [name(s["basis"], s) for found in candidates for s in found]
        for row, found in zip(rows, candidates):
            if found and claims.count(name(found[0]["basis"], found[0])) == 1:
                _close(row, found[0], now)
                closed += 1
                continue
            version = loop_version(row)
            if row.get("status", "open") not in ledger_io.ACTIVE or version in checks:
                continue
            prior = previous.get(version)
            if prior and prior.get("basis") in loop_checks.INTERACTION_BASES:
                prior = {"basis": "you_messaged", "at": prior.get("message_at"), "source": "imessage"}
            note = (signal(row, local, person_index, prior, meetings)
                    or (calendar_complete and _dates_passed(row, local, person_index, now)))
            if note:
                checks[version] = interaction_hint(row, local, person_index, now, note) or note
    loop_checks.record(checks, previous=previous)
    return {"checked": len(checks), "closed": closed}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("continuity")
    parser.add_argument("--granola", default="")
    args = parser.parse_args()
    with open(args.continuity) as handle:
        payload = json.load(handle)
    granola = None
    if args.granola and os.path.exists(args.granola):
        with open(args.granola) as handle:
            granola = json.load(handle)
    print(json.dumps(run(payload, granola=granola)))


if __name__ == "__main__":
    main()
