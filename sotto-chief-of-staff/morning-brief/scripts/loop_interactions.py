"""Evidence for a direct exchange and informational iMessage task hints."""
from datetime import datetime

import continuity_resolve as cr
import loop_checks
from ledger_provenance import ALIASES, source_loop_sources
from textutil import _s
from timeutil import source_observed_time


def _instant(value):
    parsed = cr._parse_dt(value)
    if parsed is None:
        try:  # Gmail dates are RFC 2822
            parsed = cr.parsedate_to_datetime(_s(value))
        except (TypeError, ValueError, OverflowError):
            return None
    return cr._to_user_zone(parsed)


def _origins_allowed(row: dict) -> bool:
    """Only a source task whose own sources are still permitted; owner-authored tasks are never touched."""
    try:
        origins = source_loop_sources(row)
    except ValueError:
        if row.get("content_provenance") is not None or row.get("source") or row.get("source_refs"):
            return False
        origin = ALIASES.get(cr.normalize_channel(_s(row.get("channel"))))
        origins = {origin} if origin else set()
    import source_context
    return bool(origins) and all(source_context.allowed(source) for source in origins)



def _message_party(row: dict, message: dict, source: str, identifiers: list) -> bool:
    group = cr._observed_group_id(message, source)
    if row.get("group_id"):
        render = cr._render_local
        return bool(render and group and render.group_id_key(group) == render.group_id_key(row["group_id"]))
    handle = _s(message.get("handle") or message.get("contact_jid"))
    return not (group or message.get("is_group_chat")) and any(
        cr._handle_matches(handle, ident) for ident in identifiers)


def _exchanges(row: dict, local: dict, identifiers: list, cutoff: str):
    """(when, outgoing, consent source, text) for each message with this counterpart after the ask."""
    for source in ("imessage", "whatsapp"):
        for message in local.get(source) or []:
            if type(message.get("is_from_me")) is not bool:
                continue
            stamp = message.get("timestamp_utc") or message.get("timestamp")
            if cr._message_is_after(stamp, cutoff) and _message_party(row, message, source, identifiers):
                yield _instant(stamp), message["is_from_me"], source, _s(message.get("text") or message.get("body"))
    if row.get("group_id"):
        return
    for message in local.get("emails") or []:
        if not cr._email_direction_known(message):
            continue
        outgoing = cr._is_sent_email(message)
        stamp = message.get("date") or message.get("timestamp")
        handles = cr._email_recipients(message) if outgoing else [
            cr.parseaddr(_s(message.get("from") or message.get("sender")))[1]]
        if cr._message_is_after(stamp, cutoff) and any(
                cr._handle_matches(h, i) for h in handles for i in identifiers):
            yield _instant(stamp), outgoing, "gmail", _s(message.get("body") or message.get("snippet"))


def interaction_superseded(row: dict, local: dict, identifiers: list, sent: datetime) -> bool:
    cutoff = cr._inbound_cutoff(row.get("created_at"))
    return any(when is not None and (when > sent or (when == sent and not outgoing))
               for when, outgoing, _source, _text in _exchanges(row, local, identifiers, cutoff))


def interaction_hint(row: dict, local: dict, person_index: dict, now: datetime, note: dict | None = None):
    """Only describe a positive observation on the latest direct send after this task's ask."""
    messages = local.get("imessage")
    if not messages or row.get("group_id") or (note and (note.get("basis") != "you_messaged"
            or note.get("source") != "imessage" or note.get("replied_since"))):
        return None
    identifiers = cr._verified_identifiers(row, local, person_index)
    cutoff = cr._inbound_cutoff(row.get("created_at"))
    if not identifiers or not cutoff or not _origins_allowed(row):
        return None
    latest = None
    sent = None
    for message in messages:
        if message.get("is_from_me") is not True or not _message_party(row, message, "imessage", identifiers):
            continue
        when = source_observed_time({**message, "source": "imessage"})
        if when is None or when > now or not cr._message_is_after(when.isoformat(), cutoff):
            continue
        if sent is None or when > sent:
            latest, sent = message, when
        elif when == sent and message != latest:
            latest = None
    if (latest is None or latest.get("text") == "[message withdrawn]"
            or (note and _instant(note["at"]) != sent) or interaction_superseded(row, local, identifiers, sent)):
        return None
    receipt = source_observed_time({"timestamp_utc": latest.get("date_read")})
    seen = [(receipt, "read_no_reply", "")] if receipt is not None and sent <= receipt <= now else []
    for reaction in latest.get("reactions") or []:
        if not isinstance(reaction, dict) or reaction.get("is_from_me") is not False:
            continue
        kind = _s(reaction.get("kind"))
        when = source_observed_time({"timestamp_utc": reaction.get("timestamp_utc")})
        if kind in loop_checks.REACTION_KINDS and when is not None and sent <= when <= now:
            seen.append((when, "reacted", kind))
    if not seen:
        return None
    when, basis, kind = max(seen, key=lambda item: (item[0], bool(item[2])))
    return {"basis": basis, "at": when.isoformat(), "source": "imessage", "message_at": sent.isoformat(),
            **({"kind": kind} if kind else {})}
