"""Evidence-bound continuity obligations: activity is never task completion by itself."""
import importlib.util
import os
from datetime import datetime

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
spec = importlib.util.spec_from_file_location(
    "cr_contract", os.path.join(ROOT, "morning-brief", "scripts", "continuity_resolve.py"))
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)
from delivery_effects import loop_version
from render_local import _format_emails, _trim_email, message_evidence_id
import compose_brief

NOW = datetime(2026, 6, 24, 9)


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")


def _loop(tmp_path, key="k", **fields):
    directory = tmp_path / "knowledge/continuity"
    directory.mkdir(parents=True, exist_ok=True)
    row = {"anchor_key": key, "status": "open", "action_type": "reply",
           "channel": "imessage", "contact_name": "Dana",
           "contact_identifier": "+14155552222", "created_at": "2026-06-23 08:00:00",
           "summary": "Send Dana the pricing document", **fields}
    (directory / (cr._safe(key) + ".md")).write_text(
        "---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    return row


def _reviewed(update):
    """Stamp the receipt compose_brief adds after a completed critic pass reviewed this closure."""
    update = {k: v for k, v in update.items() if k != "criticReceipt"}
    if update.get("status") == "resolved":
        update["criticReceipt"] = cr.record_critic_approval(update)
    return update


def _update(row, status, source, source_id, snippet, reviewed=True):
    update = {"loopId": row["anchor_key"], "loopVersion": loop_version(row), "status": status,
              "evidence": [{"sourceType": source, "sourceId": source_id, "snippet": snippet}]}
    return _reviewed(update) if reviewed else update


def _resolve(local, update=None):
    payload = {"today": "2026-06-24", "local": local}
    if update is not None:
        payload["loop_updates"] = [update]
    return cr.resolve(payload, NOW)


def test_generic_reply_link_call_and_calendar_do_not_close_a_task(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch); _loop(tmp_path)
    local = {"imessage": [{"rowid": 1, "is_from_me": True, "handle": "4155552222",
        "timestamp": "2026-06-23 20:00:00", "text": "done https://x.test/doc"}],
        "calls": [{"is_outgoing": True, "phone": "4155552222"}],
        "calendar_events": [{"id": "m1", "attendees": [{"email": "dana@example.com"}]}]}
    out = _resolve(local)
    assert out["resolved"] == [] and [r["anchor_key"] for r in out["active"]] == ["k"]


@pytest.mark.parametrize("fields", [{"created_at": "2025-01-01"},
    {"created_at": "2025-01-01", "deadline": "2025-01-02"},
    {"action_type": "waiting_on", "contact_identifier": "", "chased_count": 2}])
def test_age_deadline_and_unreachable_never_auto_expire(tmp_path, monkeypatch, fields):
    """Age never closes a task or silently parks it without an accepted prior-day warning.
    What you're owed stays active and chased regardless of age or reachability."""
    _env(tmp_path, monkeypatch); _loop(tmp_path, **fields)
    out = _resolve({})
    retained = [r["anchor_key"] for r in out["active"] + out["parked"]]
    assert out["expired"] == [] and out["resolved"] == [] and retained == ["k"]
    assert out["parked"] == []
    assert [r["anchor_key"] for r in out["active"]] == ["k"]


def test_named_version_and_real_message_resolve_across_phone_format(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, contact_identifier="+1 (415) 555-2222")
    msg = {"rowid": 17, "is_from_me": True, "handle": "4155552222",
           "timestamp": "2026-06-23 20:00:00", "text": "The pricing document is sent."}
    update = _update(row, "resolved", "imessage", message_evidence_id(msg, "imessage"),
                     "pricing document is sent")
    out = _resolve({"imessage": [msg]}, update)
    assert [r["resolution"] for r in out["resolved"]] == ["replied"]
    assert out["resolved"][0]["resolution_source_refs"] == update["evidence"]


def test_evidence_identity_requires_matching_identifier_types():
    assert cr._handle_matches("+1 (415) 555-2222", "415-555-2222")
    assert cr._handle_matches("Dana@Example.com", "dana@example.com")
    assert cr._jid_matches_phone("14155552222@s.whatsapp.net", "+1 (415) 555-2222")
    assert cr._jid_matches_phone("14155552222@c.us", "+14155552222")
    assert not cr._handle_matches("alex4155552222@example.com", "+14155552222")
    assert not cr._jid_matches_phone("14155552222@lid", "+14155552222")
    assert not cr._jid_matches_phone("alex4155552222@example.com", "+14155552222")


def test_numeric_email_username_cannot_resolve_phone_counterpart(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path)
    msg = {"id": "mail-numeric", "isSent": True, "from": "me@example.com",
           "to": "Alex <alex4155552222@example.com>", "date": "2026-06-23T22:00:00Z",
           "body": "Attached is the final pricing document."}
    update = _update(row, "resolved", "email", "mail-numeric", "final pricing document")
    assert _resolve({"emails": [msg]}, update)["resolved"] == []


def test_direct_imessage_chat_guid_is_not_misread_as_a_group(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path)
    msg = {"rowid": 117, "chat_guid": "iMessage;-;+14155552222", "is_group_chat": False,
           "is_from_me": True, "handle": "+14155552222",
           "timestamp": "2026-06-23 20:00:00", "text": "The pricing document is sent."}
    update = _update(row, "resolved", "imessage", message_evidence_id(msg, "imessage"),
                     "pricing document is sent")
    assert [r["anchor_key"] for r in _resolve({"imessage": [msg]}, update)["resolved"]] == ["k"]


def test_disabled_source_cannot_resolve_from_leftover_snapshot_rows(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path)
    msg = {"rowid": 118, "is_from_me": True, "handle": "+14155552222",
           "timestamp": "2026-06-23 20:00:00", "text": "The pricing document is sent."}
    update = _update(row, "resolved", "imessage", message_evidence_id(msg, "imessage"),
                     "pricing document is sent")
    local = {"source_status": {"imessage": "disabled"}, "imessage": [msg]}
    assert _resolve(local, update)["resolved"] == []
    projected = compose_brief._project_loop_updates({"loop_updates": [update]},
                                                     {"local": local, "google": {}}, [row])
    assert projected == [row]


@pytest.mark.parametrize("change,reason", [
    ({"evidence": {"sourceType": "imessage", "sourceId": "x"}}, "malformed_shape"),
    ({"evidence": [{"sourceType": "imessage", "sourceId": "absent",
                    "snippet": "pricing document is sent"}]}, "missing_source"),
    ({"snippet": "an invented quotation"}, "quote_mismatch"),
])
def test_completion_diagnostics_keep_evidence_guards(tmp_path, monkeypatch, change, reason):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path)
    msg = {"rowid": 117, "is_from_me": True, "handle": "+14155552222",
           "timestamp": "2026-06-23 20:00:00", "text": "The pricing document is sent."}
    update = _update(row, "resolved", "imessage", message_evidence_id(msg, "imessage"),
                     "pricing document is sent")
    if "snippet" in change:
        update["evidence"][0]["snippet"] = change["snippet"]
    else:
        update.update(change)
    update = _reviewed(update) if isinstance(update["evidence"], list) else update
    _proof, actual = cr._completion_evidence_result(row, update, {"imessage": [msg]})
    assert actual == reason


def test_rejected_proposal_receipt_identifies_item_without_raw_evidence(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path)
    update = _update(row, "resolved", "imessage", "missing-private-id", "private quotation")
    _resolve({"imessage": []}, update)
    stderr = capsys.readouterr().err
    line = next(line for line in stderr.splitlines() if 'loop update items:' in line)
    items = yaml.safe_load(line.partition('loop update items: ')[2])
    assert items['accepted'] == []
    assert items['rejected'][0]['reason'] == 'missing_source'
    assert len(items['rejected'][0]['digest']) == 24
    assert 'private quotation' not in line and 'missing-private-id' not in line


def test_malformed_top_level_update_is_counted_as_rejected(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path)
    cr.resolve({'today': '2026-06-24', 'local': {}, 'loop_updates': 'bad shape'}, NOW)
    stderr = capsys.readouterr().err
    summary = next(line for line in stderr.splitlines() if 'loop update outcomes:' in line)
    assert '"malformed_update": 1' in summary
    assert '"total": 1' in summary


def test_cross_channel_completion_uses_snapshot_contact_identity(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, channel="email", contact_identifier="dana@example.com")
    msg = {"rowid": 18, "is_from_me": True, "contact_jid": "14155552222@s.whatsapp.net",
           "timestamp": "2026-06-23 21:00:00", "text": "The pricing document is sent."}
    update = _update(row, "resolved", "whatsapp", message_evidence_id(msg, "whatsapp"),
                     "pricing document is sent")
    local = {"contacts": [{"name": "Dana", "emails": ["dana@example.com"],
              "phones": ["+1 415 555 2222"]}], "whatsapp": [msg]}
    assert len(_resolve(local, update)["resolved"]) == 1


def test_cross_channel_completion_uses_only_verified_graph_aliases(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, channel="email", contact_identifier="dana@example.com",
                canonical_id="person-dana")
    msg = {"rowid": 118, "is_from_me": True, "handle": "+14155552222",
           "timestamp": "2026-06-23 21:00:00", "text": "The pricing document is sent."}
    update = _update(row, "resolved", "imessage", message_evidence_id(msg, "imessage"),
                     "pricing document is sent")
    index = {"index": {"by_identifier": {"dana@example.com": "/people/dana.md",
                                          "4155552222": "/people/dana.md"},
                       "by_cid": {"person-dana": "/people/dana.md"}}}
    monkeypatch.setattr(cr, "person_identity", lambda: index)
    _loop(tmp_path, key="separate-memo", channel="email",
          contact_identifier="dana@example.com", canonical_id="person-dana",
          summary="Review Dana's separate memo")
    result = _resolve({"imessage": [msg]}, update)
    assert [item["anchor_key"] for item in result["resolved"]] == [row["anchor_key"]]
    assert [item["anchor_key"] for item in result["active"]] == ["separate-memo"]


def test_shared_name_or_unverified_graph_alias_cannot_bind_completion(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, channel="email", contact_identifier="dana@example.com",
                canonical_id="person-dana")
    msg = {"rowid": 119, "is_from_me": True, "handle": "+14155552222",
           "timestamp": "2026-06-23 21:00:00", "text": "The pricing document is sent."}
    update = _update(row, "resolved", "imessage", message_evidence_id(msg, "imessage"),
                     "pricing document is sent")
    index = {"index": {"by_identifier": {"dana@example.com": "/people/dana.md",
                                          "4155552222": "/people/other-dana.md"},
                       "by_cid": {"person-dana": "/people/dana.md"}}}
    monkeypatch.setattr(cr, "person_identity", lambda: index)
    local = {"imessage": [msg], "contacts": [{"name": "Dana", "emails": ["other@example.com"],
                                              "phones": ["+14155552222"]}]}
    assert _resolve(local, update)["resolved"] == []


def test_evidence_requires_current_version_native_id_quote_direction_and_counterpart(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, channel="email", contact_identifier="dana@example.com")
    msg = {"id": "mail-1", "isSent": True, "from": "me@example.com",
           "to": "Dana <dana@example.com>", "date": "2026-06-23T22:00:00Z",
           "body": "Attached is the final pricing document."}
    good = _update(row, "resolved", "email", "mail-1", "final pricing document")
    bad = [{**good, "loopVersion": "stale"},
           {**good, "evidence": [{**good["evidence"][0], "sourceId": "mail-2"}]},
           {**good, "evidence": [{**good["evidence"][0], "snippet": "invented quote"}]}]
    for update in bad:
        assert _resolve({"emails": [msg]}, update)["resolved"] == []
    assert len(_resolve({"emails": [msg]}, good)["resolved"]) == 1


@pytest.mark.parametrize("payload_kind", ["emails", "messages", "items", "local_emails"])
def test_resolver_rechecks_supplemental_gmail_dependency_grants(tmp_path, monkeypatch, payload_kind):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, channel="email", contact_identifier="dana@example.com")
    msg = {"id": "mail-context", "isSent": True, "from": "me@example.com",
           "to": "Dana <dana@example.com>", "date": "2026-06-23T22:00:00Z",
           "body": "Attached is the final pricing document.",
           "_loop_context_sources": ["gmail", "imessage"]}
    update = _update(row, "resolved", "email", "mail-context", "final pricing document")
    payload = {"today": "2026-06-24", "local": {}, "loop_updates": [update]}
    if payload_kind == "local_emails":
        payload["local"] = {"emails": [msg]}
    elif payload_kind == "emails":
        payload["emails"] = [msg]
    else:
        payload["emails"] = {payload_kind: [msg]}
    import source_context
    original_allowed = source_context.allowed
    monkeypatch.setattr(source_context, "allowed",
                        lambda source: source != "imessage" and original_allowed(source))
    out = cr.resolve(payload, NOW)
    assert out["resolved"] == []
    assert [item["anchor_key"] for item in out["active"]] == [row["anchor_key"]]


def test_rendered_sent_email_id_survives_normalization_and_resolves_matching_loop(tmp_path, monkeypatch):
    """The model can only cite evidence the rendered input exposes; keep the Gmail native ID from
    gather through prompt rendering, normalized output, and the evidence-bound resolver."""
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, channel="gmail", contact_identifier="dana@example.com")
    msg = {"id": "gmail-native-17", "threadId": "thread-4", "labelIds": ["SENT"],
           "isSent": True, "from": "Me <me@example.com>", "to": "Dana <dana@example.com>",
           "subject": "Pricing", "date": "2026-06-23T22:00:00Z",
           "body": "Attached is the final pricing document."}
    inbox = {"id": "gmail-inbox-18", "threadId": "thread-5", "labelIds": ["INBOX"],
             "from": "Dana <dana@example.com>", "to": "Me <me@example.com>",
             "subject": "Re: Pricing", "date": "2026-06-23T21:00:00Z", "body": "Thanks."}
    rendered = _format_emails([_trim_email(msg), _trim_email(inbox)])
    assert "MessageId: gmail-native-17" in rendered
    assert "MessageId: gmail-inbox-18" in rendered
    assert "loop is CLOSED for these threads" not in rendered

    proposed = _update(row, "resolved", "email", "gmail-native-17", "final pricing document")
    normalized = compose_brief._normalize_output({"loopUpdates": [proposed]})
    # A model-written review receipt never survives normalization; the critic pass restamps it.
    assert "criticReceipt" not in normalized["loop_updates"][0]
    out = cr.resolve({"today": "2026-06-24", "local": {"emails": [msg]},
                      "loop_updates": [_reviewed(u) for u in normalized["loop_updates"]]}, NOW)
    assert [resolved["anchor_key"] for resolved in out["resolved"]] == [row["anchor_key"]]


def test_inbound_message_cannot_resolve_what_user_owes(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch); row = _loop(tmp_path)
    msg = {"rowid": 19, "is_from_me": False, "handle": "+14155552222",
           "timestamp": "2026-06-23 22:00:00", "text": "I sent the pricing document."}
    update = _update(row, "resolved", "imessage", message_evidence_id(msg, "imessage"),
                     "sent the pricing document")
    assert _resolve({"imessage": [msg]}, update)["resolved"] == []


def test_waiting_update_uses_inbound_promise_and_promised_date(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, action_type="waiting_on", summary="Dana owes the signed waiver",
                chase_after="2026-06-24")
    msg = {"rowid": 20, "is_from_me": False, "handle": "+14155552222",
           "timestamp": "2026-06-24 08:00:00", "text": "Legal will send it on Friday."}
    update = _update(row, "waiting", "imessage", message_evidence_id(msg, "imessage"),
                     "will send it on Friday")
    active, = _resolve({"imessage": [msg]}, update)["active"]
    assert active["last_heard_at"] == "2026-06-24 08:00:00"
    assert active["chase_after"] == "2026-06-26"


def test_waiting_on_resolves_as_delivered_only_with_inbound_proof(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, action_type="waiting_on", summary="Dana owes the signed waiver")
    msg = {"rowid": 21, "is_from_me": False, "handle": "+14155552222",
           "timestamp": "2026-06-24 08:00:00", "text": "Here is the signed waiver."}
    update = _update(row, "resolved", "imessage", message_evidence_id(msg, "imessage"), "signed waiver")
    assert [r["resolution"] for r in _resolve({"imessage": [msg]}, update)["resolved"]] == ["delivered"]


def test_explicit_manual_commitment_ignores_model_update(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch); row = _loop(tmp_path, resolution_mode="explicit")
    msg = {"rowid": 22, "is_from_me": True, "handle": "+14155552222",
           "timestamp": "2026-06-24 08:00:00", "text": "The pricing document is sent."}
    update = _update(row, "resolved", "imessage", message_evidence_id(msg, "imessage"),
                     "pricing document is sent")
    assert _resolve({"imessage": [msg]}, update)["resolved"] == []


def test_callback_activity_requires_explicit_completion(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch); _loop(tmp_path, action_type="call_back", channel="phone")
    out = _resolve({"whatsapp_calls": [{"is_outgoing": True,
        "jid": "14155552222@s.whatsapp.net", "timestamp": "2026-06-24 08:00:00"}]})
    assert out["resolved"] == []


def test_same_person_prose_folds_unless_second_obligation_is_source_bound(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    base = {"action_type": "reply", "channel": "imessage", "contactName": "Dana",
            "contactIdentifier": "+14155552222", "contextSummary": "Send the pricing document"}
    first = cr.resolve({"today": "2026-06-24", "new_actions": [base, dict(base)]}, NOW,
                       resolve_existing=False)
    assert len(first["active"]) == 1
    second = cr.resolve({"today": "2026-06-24", "new_actions": [
        {**base, "contextSummary": "Confirm the lunch address"}]}, NOW, resolve_existing=False)
    assert len(second["active"]) == 1
    msg = {"rowid": 91, "is_from_me": False, "handle": "+14155552222",
           "timestamp": "2026-06-24 08:00:00", "text": "Send it and confirm lunch."}
    distinct = {**base, "contextSummary": "Confirm the lunch address", "newObligation": True,
                "evidence": [{"sourceType": "imessage", "sourceId": "91",
                              "snippet": "confirm lunch"}]}
    third = cr.resolve({"today": "2026-06-24", "new_actions": [distinct],
                        "local": {"imessage": [msg]}}, NOW, resolve_existing=False)
    assert len(third["active"]) == 2


def test_paraphrase_updates_stable_base_and_loop_id_still_targets_it(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    base = {"action_type": "reply", "channel": "imessage", "contactName": "Dana",
            "contactIdentifier": "+14155552222", "contextSummary": "Send the pricing document"}
    original, = cr.resolve({"today": "2026-06-24", "new_actions": [base]}, NOW,
                           resolve_existing=False)["active"]
    paraphrase = {**base, "contextSummary": "Get Dana the price sheet"}
    assert len(cr.resolve({"today": "2026-06-24", "new_actions": [paraphrase]}, NOW,
                          resolve_existing=False)["active"]) == 1
    named = {**paraphrase, "loopId": original["anchor_key"]}
    out = cr.resolve({"today": "2026-06-24", "new_actions": [named]}, NOW,
                     resolve_existing=False)
    match = next(r for r in out["active"] if r["anchor_key"] == original["anchor_key"])
    assert match["summary"] == "Get Dana the price sheet"


def test_group_evidence_must_bind_tracked_group(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    group = "iMessage;+;group-one"
    row = _loop(tmp_path, group_id=group, contact_identifier="", contact_name="Dinner group")
    msg = {"rowid": 23, "is_from_me": True, "handle": "+14155552222",
           "chat_guid": "iMessage;+;group-two", "is_group_chat": True,
           "timestamp": "2026-06-24 08:00:00", "text": "The dinner address is confirmed."}
    update = _update(row, "resolved", "imessage", message_evidence_id(msg, "imessage"),
                     "dinner address is confirmed")
    assert _resolve({"imessage": [msg]}, update)["resolved"] == []
    msg["chat_guid"] = group
    update = _update(row, "resolved", "imessage", message_evidence_id(msg, "imessage"),
                     "dinner address is confirmed")
    assert len(_resolve({"imessage": [msg]}, update)["resolved"]) == 1


def test_group_task_accepts_email_to_verified_participant_alias(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    group = "iMessage;+;group-one"
    row = _loop(tmp_path, group_id=group, contact_identifier="",
                contact_name="Introduction group", summary="Introduce Alex to ExampleCo")
    group_message = {"rowid": 30, "is_from_me": False, "is_group_chat": True,
                     "chat_guid": group, "handle": "+14155552222",
                     "group_participants": ["+14155552222"],
                     "timestamp": "2026-06-23 09:00:00", "text": "Please introduce us."}
    email = {"id": "mail-intro", "isSent": True, "from": "me@example.com",
             "to": "Alex <alex@example.com>", "date": "2026-06-23T22:00:00Z",
             "body": "Introducing Alex to ExampleCo."}
    update = _update(row, "resolved", "email", "mail-intro", "Introducing Alex to ExampleCo")
    index = {"index": {"by_identifier": {"4155552222": "/people/alex.md",
                                          "alex@example.com": "/people/alex.md"},
                       "by_cid": {"person-alex": "/people/alex.md"}}}
    monkeypatch.setattr(cr, "person_identity", lambda: index)
    local = {"imessage": [group_message], "emails": [email]}
    assert [item["anchor_key"] for item in _resolve(local, update)["resolved"]] == [row["anchor_key"]]


@pytest.mark.parametrize("recipients,closes", [
    ("Mom <mom@example.com>", False),                              # one member of the group
    ("Mom <mom@example.com>, Dad <dad@example.com>", True),        # the whole roster
])
def test_group_task_closes_by_email_only_when_addressed_to_whole_roster(
        tmp_path, monkeypatch, recipients, closes):
    """A 1:1 email to one member is that member's conversation, not the group's: "Happy birthday
    Mom!" cannot pay "send the family the dinner address", even with a reviewed proposal."""
    _env(tmp_path, monkeypatch)
    group = "iMessage;+;family-chat"
    row = _loop(tmp_path, group_id=group, contact_identifier="", contact_name="Family",
                summary="Send the family the dinner address")
    group_message = {"rowid": 31, "is_from_me": False, "is_group_chat": True,
                     "chat_guid": group, "handle": "+14155552222",
                     "group_participants": ["+14155552222", "+14155553333"],
                     "timestamp": "2026-06-23 07:00:00", "text": "What's the dinner address?"}
    email = {"id": "mail-family", "isSent": True, "from": "me@example.com", "to": recipients,
             "date": "2026-06-23T22:00:00Z", "body": "Happy birthday Mom! Dinner is at 12 Elm St."}
    index = {"index": {"by_identifier": {"4155552222": "/people/mom.md",
                                          "mom@example.com": "/people/mom.md",
                                          "4155553333": "/people/dad.md",
                                          "dad@example.com": "/people/dad.md"},
                       "by_cid": {}}}
    monkeypatch.setattr(cr, "person_identity", lambda: index)
    update = _update(row, "resolved", "email", "mail-family", "Dinner is at 12 Elm St.")
    resolved = _resolve({"imessage": [group_message], "emails": [email]}, update)["resolved"]
    assert [item["anchor_key"] for item in resolved] == ([row["anchor_key"]] if closes else [])


def test_group_roster_is_the_sources_participant_list_not_senders_seen(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    group = "iMessage;+;family-chat"
    row = _loop(tmp_path, group_id=group, contact_identifier="", contact_name="Family",
                summary="Send the family the dinner address")
    sender_only = {"rowid": 32, "is_from_me": False, "is_group_chat": True, "chat_guid": group,
                   "handle": "+14155552222", "timestamp": "2026-06-23 07:00:00",
                   "text": "What's the dinner address?"}
    email = {"id": "mail-mom", "isSent": True, "from": "me@example.com",
             "to": "Mom <mom@example.com>", "date": "2026-06-23T22:00:00Z",
             "body": "Dinner is at 12 Elm St."}
    index = {"index": {"by_identifier": {"4155552222": "/people/mom.md",
                                          "mom@example.com": "/people/mom.md"}, "by_cid": {}}}
    monkeypatch.setattr(cr, "person_identity", lambda: index)
    update = _update(row, "resolved", "email", "mail-mom", "Dinner is at 12 Elm St.")
    assert _resolve({"imessage": [sender_only], "emails": [email]}, update)["resolved"] == []


@pytest.mark.parametrize("change", ["wrong_recipient", "missing_roster", "unverified_alias",
                                     "email_with_phone_digits"])
def test_group_email_needs_observed_roster_and_verified_recipient(tmp_path, monkeypatch, change):
    _env(tmp_path, monkeypatch)
    group = "iMessage;+;group-one"
    row = _loop(tmp_path, group_id=group, contact_identifier="",
                contact_name="Introduction group", summary="Introduce Alex to ExampleCo")
    group_message = {"rowid": 30, "is_from_me": False, "is_group_chat": True,
                     "chat_guid": group, "handle": "+14155552222",
                     "group_participants": ["+14155552222"],
                     "timestamp": "2026-06-23 09:00:00", "text": "Please introduce us."}
    email = {"id": "mail-intro", "isSent": True, "from": "me@example.com",
             "to": "Alex <alex@example.com>", "date": "2026-06-23T22:00:00Z",
             "body": "Introducing Alex to ExampleCo."}
    index = {"index": {"by_identifier": {"4155552222": "/people/alex.md",
                                          "alex@example.com": "/people/alex.md"},
                       "by_cid": {"person-alex": "/people/alex.md"}}}
    if change == "wrong_recipient":
        email["to"] = "Stranger <stranger@example.com>"
    elif change == "missing_roster":
        group_message["chat_guid"] = "iMessage;+;another-group"
    elif change == "email_with_phone_digits":
        email["to"] = "Stranger <x4155552222@example.com>"
    else:
        index["index"]["by_identifier"]["alex@example.com"] = "/people/other.md"
    monkeypatch.setattr(cr, "person_identity", lambda: index)
    update = _update(row, "resolved", "email", "mail-intro", "Introducing Alex to ExampleCo")
    assert _resolve({"imessage": [group_message], "emails": [email]}, update)["resolved"] == []


def _excerpt_email(**fields):
    """A sent email whose loop-lane excerpt is a different read (HTML) of the same message."""
    return {"id": "mail-x", "isSent": True, "labelIds": ["SENT"], "from": "me@example.com",
            "to": "Dana <dana@example.com>", "date": "2026-06-23T22:00:00Z",
            "body": "Plain text: will do next week.", "_loop_context": True,
            "_loop_context_body": "<div>Attached is the final pricing document.</div>",
            "_loop_context_body_sources": ["gmail"], **fields}


SPANNING_QUOTE = "next week. [Additional excerpt from the same email] <div>Attached"


def test_completion_quote_spanning_the_excerpt_label_is_not_verbatim(tmp_path, monkeypatch):
    """The label joining body and excerpt is code-made; a quote across it is not in the email."""
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, channel="email", contact_identifier="dana@example.com")
    msg = _excerpt_email()
    spanning = _update(row, "resolved", "email", "mail-x", SPANNING_QUOTE)
    proof, reason = cr._completion_evidence_result(row, spanning, {"emails": [msg]})
    assert proof is None and reason == "quote_mismatch"
    assert _resolve({"emails": [msg]}, spanning)["resolved"] == []
    # A quote wholly inside either verbatim read of the message still binds.
    inside = _update(row, "resolved", "email", "mail-x", "Attached is the final pricing document.")
    assert len(_resolve({"emails": [msg]}, inside)["resolved"]) == 1


def test_observed_source_quote_spanning_the_excerpt_label_cannot_bind(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    msg = _excerpt_email(isSent=False, labelIds=["INBOX"], **{"from": "Dana <dana@example.com>",
                         "to": "me@example.com"})
    action = {"contact_identifier": "dana@example.com",
              "source_refs": [{"sourceType": "email", "sourceId": "mail-x", "snippet": SPANNING_QUOTE}]}
    assert cr._observed_action_source(action, {"emails": [msg]}, "2026-06-24 09:00:00") is None
    action["source_refs"][0]["snippet"] = "Attached is the final pricing document."
    assert cr._observed_action_source(action, {"emails": [msg]}, "2026-06-24 09:00:00") is not None


def test_public_proposal_hash_cannot_claim_a_review(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path)
    msg = {'rowid': 90, 'is_from_me': True, 'handle': '+14155552222',
           'timestamp': '2026-06-23 20:00:00', 'text': 'Here is the pricing document you asked for.'}
    update = _update(row, 'resolved', 'imessage', message_evidence_id(msg, 'imessage'),
                     msg['text'], reviewed=False)
    update['criticReceipt'] = cr.critic_review_receipt(update)
    proof, reason = cr._completion_evidence_result(row, update, {'imessage': [msg]}, {})
    assert proof is None and reason == 'unreviewed_resolution'
    cr.record_critic_approval(update)
    proof, reason = cr._completion_evidence_result(row, update, {'imessage': [msg]}, {})
    assert proof is not None, reason
    changed = {**update, 'loopVersion': 'other-revision'}
    changed['criticReceipt'] = cr.critic_review_receipt(changed)
    assert not cr._has_critic_approval(changed)


def test_recorded_reviews_are_bounded_expire_and_keep_no_source_text(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    import jsonstore
    now = cr.datetime.now(cr.timezone.utc).timestamp()
    jsonstore.write_atomic(cr._review_path(), {'expired': now - cr.REVIEW_RECEIPTS_TTL - 1,
        **{str(i): now - 1 for i in range(cr.REVIEW_RECEIPTS_MAX)}})
    proposal = {'loopId': 'task', 'status': 'resolved', 'evidence': [{'snippet': 'private text'}]}
    ident = cr.record_critic_approval(proposal)
    saved = jsonstore.read(cr._review_path())
    assert len(saved) == cr.REVIEW_RECEIPTS_MAX and ident in saved and 'expired' not in saved
    assert 'private text' not in str(saved)
    proposal['criticReceipt'] = ident
    saved[ident] = now - cr.REVIEW_RECEIPTS_TTL - 1
    jsonstore.write_atomic(cr._review_path(), saved)
    assert not cr._has_critic_approval(proposal)
