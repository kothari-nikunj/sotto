"""closure_pass.py — judge the owner's own message against that person's open tasks when it is sent.

One rule: when you send someone a direct message, each open task you owe that person is checked
against that message alone. "Delivered" with a quote becomes one ordinary `loop_updates` proposal,
and it closes the task only when the critic approves it and the resolver's own identity, direction,
time and quote checks pass (the brief's strict path, the same writer). Anything less writes at most
the "maybe done" hint (`loop_checks`), at send time, unless their own message in the same batch is
newer (the brief's rule: a later message from them clears the note). Any batch with their direct
message, even one without yours, refreshes a saved note. A group thread, a task they owe you, an
owner-authored task and an explicitly protected task are never touched.

`judge(task, messages)` sees only {ask, deliverable, source_quote} and [{id, sent_at, text}] and
returns {verdict: delivered|partial|not, message_id, quote, reason}. `critic(review)` returns True
to approve. Neither has a default: without both, the pass writes the hint only.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "_shared", "lib"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "_shared", "scripts"))
import continuity_resolve as cr  # noqa: E402
import ledger_io  # noqa: E402
import loop_check  # noqa: E402
import loop_checks  # noqa: E402
from delivery_effects import loop_version  # noqa: E402
from loop_interactions import _instant, _message_party, _origins_allowed, interaction_superseded  # noqa: E402
from loop_snapshot import snapshot_local  # noqa: E402
from personal_context import event_source  # noqa: E402
from textutil import _s  # noqa: E402
from timeutil import source_observed_time  # noqa: E402

FIELDS = {"imessage": "imessage", "whatsapp": "whatsapp", "gmail": "emails"}


def _direct_outbound(events: list, snapshot: dict, own: bool = True) -> dict:
    """The permitted snapshot holding the direct messages from these events: the owner's own only,
    or (`own=False`) theirs too."""
    local = {"contacts": snapshot.get("contacts") or []}
    for event in events:
        field = FIELDS.get(event_source(event))
        if (field and (not own or event.get("is_from_me") is True) and not event.get("is_group_chat")
                and not _s(event.get("chat_guid")) and not _s(event.get("contact_jid")).endswith("@g.us")):
            local.setdefault(field, []).append(event)
    return snapshot_local({"local": local})


def _eligible(row: dict) -> bool:
    return (row.get("status", "open") in ledger_io.ACTIVE and not row.get("group_id")
            and not ledger_io.is_waiting_on(row.get("action_type"))
            and ledger_io.normalize_action_type(row.get("action_type")) not in ledger_io.MEETING_TYPES
            and row.get("resolution_mode") != "explicit" and _origins_allowed(row))


def _sent_after_ask(row: dict, local: dict, identifiers: list) -> list:
    """{id, sent_at, text, source} for each message in `local` the owner sent this person after the ask."""
    from source_context import email_evidence_text
    cutoff = cr._inbound_cutoff(row.get("created_at"))
    found = []
    for source in ("imessage", "whatsapp"):
        for message in local.get(source) or []:
            stamp = message.get("timestamp")
            if message.get("timestamp_utc") is not None:
                stamp = source_observed_time(message)
                if stamp is None:
                    continue
                stamp = stamp.isoformat()
            if cr._message_is_after(stamp, cutoff) and _message_party(row, message, source, identifiers):
                found.append({"id": cr._render_local.message_evidence_id(message, source), "sent_at": _s(stamp),
                              "text": _s(message.get("body") or message.get("text")), "source": source})
    for message in local.get("emails") or []:
        stamp = message.get("date") or message.get("timestamp")
        if (cr._email_direction_known(message) and cr._is_sent_email(message) and cr._message_is_after(stamp, cutoff)
                and any(cr._handle_matches(h, i) for h in cr._email_recipients(message) for i in identifiers)):
            found.append({"id": _s(message.get("messageId") or message.get("id") or message.get("rowid")),
                          "sent_at": _s(stamp),
                          "text": email_evidence_text(message, include_attachments=True), "source": "email"})
    return [message for message in found if message["id"] and message["text"]]


def _task(row: dict) -> dict:
    refs = [ref for ref in row.get("source_refs") or [] if isinstance(ref, dict)]
    return {"ask": _s(row.get("ask") or row.get("summary")), "deliverable": ledger_io.deliverable(row),
            "source_quote": _s(refs[0].get("snippet")) if refs else ""}


def _proposal(row: dict, messages: list, verdict: dict, critic):
    """The critic-approved `loop_updates` entry for a quoted "delivered" verdict, or None."""
    quote = " ".join(_s(verdict.get("quote")).split())
    message = next((m for m in messages if m["id"] == _s(verdict.get("message_id"))), None)
    if verdict.get("verdict") != "delivered" or not quote or not message or quote not in " ".join(message["text"].split()):
        return None
    update = {"loopId": row["anchor_key"], "loopVersion": loop_version(row), "status": "resolved",
              "evidence": [{"sourceType": message["source"], "sourceId": message["id"], "snippet": quote}],
              "reason": " ".join(_s(verdict.get("reason")).split())[:240]}
    review = {"proposal_id": cr.critic_review_receipt(update), "loopId": update["loopId"], "status": "resolved",
              "task": ledger_io.task_label(row)[:300], "model_reason": update["reason"],
              "evidence": [{"sourceType": message["source"], "sourceId": message["id"], "proposed_quote": quote,
                            "original_context": message["text"][:2000], "date": message["sent_at"],
                            "message_sender_role": "owner"}]}
    if critic(review) is not True:
        return None
    return {**update, "criticReceipt": cr.record_critic_approval(update)}


def _refresh_notes(batch: dict) -> None:
    """Apply the same carry rule in triage and Learn, including inbound-only batches."""
    saved = loop_checks.read(include_hidden_replies=True)
    if not saved or not any(batch.get(field) for field in FIELDS.values()):
        return
    person_index = cr.person_identity()
    for row in cr._load_items().values():
        version = loop_version(row)
        note = saved.get(version)
        if not note or note.get("basis") not in loop_check.CARRIED | loop_checks.INTERACTION_BASES:
            continue
        identifiers = cr._verified_identifiers(row, batch, person_index)
        if not identifiers:
            continue
        if note.get("basis") in loop_checks.INTERACTION_BASES:
            sent = _instant(note.get("message_at"))
            if sent and interaction_superseded(row, batch, identifiers, sent):
                loop_checks.refresh(version, note, None)
        else:
            updated = loop_check.signal(row, batch, person_index, note)
            if updated != note:
                loop_checks.refresh(version, note, updated)


def run(events: list, snapshot: dict, *, judge=None, critic=None, now=None) -> dict:
    batch = _direct_outbound(events, snapshot, own=False)
    _refresh_notes(batch)
    local = _direct_outbound(events, snapshot)
    if not any(local.get(field) for field in FIELDS.values()):
        return {"judged": 0, "closed": 0, "hinted": 0}
    person_index, candidates = None, []
    for row in cr._load_items().values():
        # Contacts first: the people graph is read only when a card already links this message to the task.
        if not _eligible(row) or not _sent_after_ask(row, local, cr._verified_identifiers(row, local, {})):
            continue
        person_index = person_index or cr.person_identity()
        messages = _sent_after_ask(row, local, cr._verified_identifiers(row, local, person_index))
        if messages:
            candidates.append((row, messages))
    judged, proposals = 0, []
    if judge and critic:
        for row, messages in candidates:
            judged += 1
            verdict = judge(_task(row), [{k: m[k] for k in ("id", "sent_at", "text")} for m in messages])
            proposal = _proposal(row, messages, verdict, critic) if isinstance(verdict, dict) else None
            if proposal:
                proposals.append(proposal)
    closed = set()
    if proposals:
        result = cr.resolve({"local": local, "loop_updates": proposals}, now, resolve_existing=False)
        closed = {row["anchor_key"] for row in result["resolved"]}
    hinted = 0
    for row, _messages in candidates:
        note = None if row["anchor_key"] in closed else loop_check.signal(row, batch, person_index)
        if note:
            loop_checks.add(loop_version(row), note)
            hinted += 1
    return {"judged": judged, "closed": len(closed), "hinted": hinted}
