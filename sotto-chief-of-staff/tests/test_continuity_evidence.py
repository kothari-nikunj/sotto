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


def _update(row, status, source, source_id, snippet):
    return {"loopId": row["anchor_key"], "loopVersion": loop_version(row), "status": status,
            "evidence": [{"sourceType": source, "sourceId": source_id, "snippet": snippet}]}


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
    out = cr.resolve({"today": "2026-06-24", "local": {"emails": [msg]},
                      "loop_updates": normalized["loop_updates"]}, NOW)
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
