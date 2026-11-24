"""What a captured task owes, and who owes it (`morning-brief/scripts/continuity_resolve.py`).

Extraction may name the `deliverable` (one imperative clause) and `owedBy` (`owner` | `them`) on
each action. They are stored on a new ledger row; readers show the deliverable first
(`ledger_io.deliverable`). Direction itself stays defined once, by the type (`ledger_io.is_waiting_on`).

Two direction rules run on a new obligation after its evidence is verified, before it is keyed:
  - A `reply` with `owedBy: them` whose earliest cited message the owner sent is the owner's
    request to them (a later cited message may be their acknowledgement), so it is captured as
    `waiting_on`. Nobody owes a reply to their own message.
  - Otherwise a capture whose `owedBy` disagrees with its type is withheld.
A first capture not marked newObligation that is a `reply` with `owedBy: them` gets the same rules
after its evidence binds; with nothing bound it is withheld (`first_reply_held`).
Without `owedBy` an owner-sent message is ambiguous (a request or a promise), so the type stands.
Only message direction and the model's own enum decide; meaning stays the extraction prompt's job.
"""
from __future__ import annotations

import re

from ledger_provenance import ref_fields, source_refs

OWED_BY = ("owner", "them")
_WORD_SEPARATORS = re.compile(r"\W+")
# The bound evidence messages as (observed time, direction) pairs (True = the owner sent it), set
# by the resolver's evidence binder on the in-memory action only; never stored.
SENT_KEY = "_sent"


def fields(get) -> dict:
    """The capture fields of one action: what fulfills it, what is owed, and who owes it."""
    from signal_closes import fulfillment_fields
    text = get("deliverable")
    owed = get("owed_by") or get("owedBy")
    return {**fulfillment_fields(get),
            "deliverable": (" ".join(text.split()) or None) if isinstance(text, str) else None,
            "owed_by": owed if owed in OWED_BY else None}


def _type_owed_by(action: dict) -> str:
    from ledger_io import is_waiting_on
    return "them" if is_waiting_on(action.get("action_type")) else "owner"


def stored(action: dict) -> dict:
    """The capture fields a new row keeps; an `owed_by` that contradicts the type is not kept, and
    neither is the deliverable written for that other direction."""
    out = {k: action[k] for k in ("fulfillment", "deliver_to") if action.get(k)}
    if not action.get("owed_by") or action["owed_by"] == _type_owed_by(action):
        out.update({k: action[k] for k in ("deliverable", "owed_by") if action.get(k)})
    return out


def retext(row: dict, source: dict, keys: tuple) -> None:
    """Copy `source`'s non-empty `keys` onto `row`. New task text takes its own deliverable with it:
    the one written for the old text is never shown over a reopened or folded row's new ask."""
    for key in keys:
        if source.get(key):
            row[key] = source[key]
    if source.get("summary") or source.get("ask"):
        row.pop("deliverable", None), row.pop("owed_by", None)
        kept = stored({**source, "action_type": row.get("action_type")})
        row.update({k: kept[k] for k in ("deliverable", "owed_by") if k in kept})


def first_reply_held(action: dict, bind, outcomes: dict, rejected: list) -> bool | None:
    """A first capture not marked newObligation that is a `reply` with `owedBy: them`: `bind()`
    binds its evidence, then the same two rules run; with nothing bound it is withheld, never saved
    as a task the owner owes. None means the rules do not apply (the caller keeps its key)."""
    from ledger_io import normalize_action_type
    if (action.get("new_obligation") or action.get("owed_by") != "them"
            or normalize_action_type(action.get("action_type")) != "reply"):
        return None
    if bind() is None:
        action.pop(SENT_KEY, None)
    return held(action, outcomes, rejected)


def held(action: dict, outcomes: dict, rejected: list) -> bool:
    """Apply the two direction rules; True means withhold this capture (counted and logged)."""
    from ledger_io import normalize_action_type
    sent = action.get(SENT_KEY) or []
    owed = action.get("owed_by")
    # Every direction must be known, and a tie for earliest must be all the owner's.
    first = min((when for when, _ in sent), default=None) if all(
        when is not None and type(mine) is bool for when, mine in sent) else None
    if (owed == "them" and normalize_action_type(action.get("action_type")) == "reply"
            and first is not None and all(mine for when, mine in sent if when == first)):
        action["action_type"] = "waiting_on"
        outcomes["direction_corrected"] = outcomes.get("direction_corrected", 0) + 1
        return False
    if not owed or owed == _type_owed_by(action):
        return False
    rejected.append(f"{action.get('contact_name') or '(unnamed)'}: direction withheld, owedBy disagrees with the type")
    outcomes["direction_held"] = outcomes.get("direction_held", 0) + 1
    return True


def _words(value) -> str:
    return " ".join(_WORD_SEPARATORS.sub(" ", str(value or "")).casefold().split())


def _quoted_clauses(row: dict) -> set[str]:
    refs = row.get("source_refs") or []
    refs = [refs] if isinstance(refs, dict) else refs
    return {quote for ref in refs if isinstance(ref, dict)
            if (quote := " ".join(str(ref.get("snippet") or "").split()))}


def _same_work(action: dict, row: dict, ask: str) -> bool:
    other_ask = _words(row.get("ask"))
    if not other_ask:
        return False
    mine, theirs = _words(action.get("deliverable")), _words(row.get("deliverable"))
    work_matches = mine == theirs if mine and theirs else ask == other_ask
    if not work_matches:
        return False
    quotes, other_quotes = _quoted_clauses(action), _quoted_clauses(row)
    return not quotes or not other_quotes or quotes == other_quotes


def same_ask(action: dict, items: dict, key: str, evidence, anchor) -> str:
    """A new capture keeps the one row, of any type in the same direction, holding this verified
    person's same request: shared source message, matching work, and compatible quoted clauses."""
    from ledger_io import is_waiting_on
    who = anchor(action.get("canonical_id"), action.get("contact_identifier"), "", action.get("group_id"))
    ask = _words(action.get("ask"))
    if key in items or not ask or not who.startswith(("gid:", "cid:", "id:")):
        return key
    refs = evidence(action)
    if not refs:
        return key
    found = [k for k, row in items.items()
             if anchor(row.get("canonical_id"), row.get("contact_identifier"), "", row.get("group_id")) == who
             and is_waiting_on(row.get("action_type")) == is_waiting_on(action.get("action_type"))
             and refs & evidence(row) and _same_work(action, row, ask)]
    return found[0] if len(found) == 1 else key


def stale_capture_target(action: dict) -> str:
    """Choose the stale-evidence recipient with the resolver's action field precedence."""
    from ledger_io import is_waiting_on, normalize_action_type
    if action.get("new_obligation") is not True and action.get("newObligation") is not True:
        return ""
    kind = action.get("action_type") or action.get("type")
    owed_by = action.get("owed_by") or action.get("owedBy")
    if is_waiting_on(kind) or (normalize_action_type(kind) == "reply" and owed_by == "them"):
        return str(action.get("contact_identifier") or action.get("contactIdentifier") or "").lower().strip()
    return ""


def capture_emails(action, local):
    """Ordinary emails plus any stale source that can prove this new waiting-on."""
    emails = local.get("emails") or []
    target = stale_capture_target(action)
    if target:
        return emails + [row for row in local.get("stale_capture", []) if target in row.get("_stale_targets", [])]
    return emails


def cites_checkable_messages(action: dict, local: dict, channel) -> bool:
    """True when every cited ref names a Gmail, iMessage or WhatsApp message the snapshot could hold.

    Only such a citation can be disproved by a failed bind. A meeting note or calendar ref is not a
    message, and a stale-thread message is shown to the model but kept out of the bindable rows
    unless it can prove a waiting-on (`capture_emails`); neither failure says the model invented it."""
    refs = source_refs(action)
    stale = {str(row.get(key)) for row in local.get("stale_capture") or [] if isinstance(row, dict)
             for key in ("source_id", "id", "messageId", "message_id") if row.get(key)}
    for ref in refs:
        source, ident, _ = ref_fields(ref)
        kind = channel(source)
        if kind not in ("email", "imessage", "whatsapp") or (kind == "email" and ident in stale):
            return False
    return bool(refs)
