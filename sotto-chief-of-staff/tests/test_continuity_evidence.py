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
from render_local import _format_emails, _trim_email, message_evidence_id, call_evidence, resolve_contact_names, _format_recent_calls
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


def _unique_index(index):
    """Synthetic graph fixtures must carry the collision-aware ownership view too."""
    index['unique_identifiers'] = {cr.alias_identifier(alias): owner
        for alias, owner in index['index']['by_identifier'].items()}
    return index


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
    monkeypatch.setattr(cr, "person_identity", lambda: _unique_index(index))
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
    monkeypatch.setattr(cr, "person_identity", lambda: _unique_index(index))
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


@pytest.mark.parametrize('channel', ['imessage', 'whatsapp', 'email'])
def test_reviewed_specific_text_answer_closes_callback_across_channels(tmp_path, monkeypatch, channel):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, action_type='call_back', channel='phone', summary='Respond after Dana called')
    text = 'Saw your missed call; Thursday at two works for me.'
    msg = {'rowid': 42, 'is_from_me': True, 'handle': '+14155552222',
           'contact_jid': '14155552222@s.whatsapp.net', 'timestamp': '2026-06-24 08:00:00', 'text': text}
    source_id, field = message_evidence_id(msg, channel), channel
    local = {}
    if channel == 'email':
        msg = {'id': 'mail-callback', 'isSent': True, 'to': 'dana@example.com',
               'date': msg['timestamp'], 'body': text}
        source_id, field = msg['id'], 'emails'
        local['contacts'] = [{'name': 'Dana', 'phones': ['+14155552222'], 'emails': ['dana@example.com']}]
    local[field] = [msg]
    assert not _resolve(local)['resolved']
    assert len(_resolve(local, _update(row, 'resolved', channel, source_id, text))['resolved']) == 1


@pytest.mark.parametrize('shared', ['primary', 'secondary'])
def test_ambiguous_contact_cards_never_expand_completion_aliases(tmp_path, monkeypatch, shared):
    _env(tmp_path, monkeypatch)
    primary = 'shared@example.com' if shared == 'primary' else 'dana-a@example.com'
    row = _loop(tmp_path, channel='email', contact_identifier=primary, contact_name='Dana A')
    contacts = [{'name': 'Dana A', 'emails': [primary], 'phones': ['+14155551111']},
                {'name': 'Dana B', 'emails': ['shared@example.com'], 'phones': ['+14155552222']}]
    if shared == 'secondary':
        contacts[0]['phones'] = ['+14155552222']
    msg = {'rowid': 43, 'is_from_me': True, 'handle': '+14155552222',
           'timestamp': '2026-06-24 08:00:00', 'text': 'The pricing document is sent.'}
    update = _update(row, 'resolved', 'imessage', message_evidence_id(msg, 'imessage'), msg['text'])
    assert not _resolve({'contacts': contacts, 'imessage': [msg]}, update)['resolved']


def test_real_graph_collision_cannot_lend_shared_phone_to_other_person(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    from pathlib import Path
    kg = cr._kg
    folder = Path(kg.people_dir()); folder.mkdir(parents=True)
    for cid, email in [('c_aaaaaa1', 'dana-a@example.com'), ('c_bbbbbb2', 'dana-b@example.com')]:
        (folder / (cid + '.md')).write_text(kg.serialize_person_file(kg.PersonFile(
            name='Dana', canonical_id=cid, identifiers=[email, '+14155552222'])))
    row = _loop(tmp_path, channel='email', canonical_id='c_aaaaaa1', contact_identifier='dana-a@example.com')
    msg = {'rowid': 44, 'is_from_me': True, 'handle': '+14155552222',
           'timestamp': '2026-06-24 08:00:00', 'text': 'The pricing document is sent.'}
    update = _update(row, 'resolved', 'imessage', message_evidence_id(msg, 'imessage'), msg['text'])
    assert not _resolve({'imessage': [msg]}, update)['resolved']


@pytest.mark.parametrize('collision', [False, True])
def test_graph_phone_and_whatsapp_jid_share_alias_keys_without_lending_shared_identity(tmp_path, monkeypatch, collision):
    _env(tmp_path, monkeypatch)
    from pathlib import Path
    kg = cr._kg
    folder = Path(kg.people_dir()); folder.mkdir(parents=True)
    kg.write_person_file(str(folder / 'c_aaaaaa1.md'), kg.PersonFile(
        name='Dana', canonical_id='c_aaaaaa1',
        identifiers=['dana@example.com', '14155552222@s.whatsapp.net']), NOW)
    if collision:
        kg.write_person_file(str(folder / 'c_bbbbbb2.md'), kg.PersonFile(
            name='Dana', canonical_id='c_bbbbbb2',
            identifiers=['other@example.com', '+1 (415) 555-2222']), NOW)
    index = cr.person_identity()
    key = cr.alias_identifier('+14155552222')
    assert key == cr.alias_identifier('14155552222@s.whatsapp.net')
    assert (key in index['ambiguous_identifiers']) is collision
    assert (index['unique_identifiers'].get(key) == 'c_aaaaaa1') is (not collision)
    row = _loop(tmp_path, channel='email', canonical_id='c_aaaaaa1', contact_identifier='dana@example.com')
    msg = {'rowid': 45, 'is_from_me': True, 'contact_jid': '14155552222@s.whatsapp.net',
           'timestamp': '2026-06-24 08:00:00', 'text': 'The pricing document is sent.'}
    update = _update(row, 'resolved', 'whatsapp', message_evidence_id(msg, 'whatsapp'), msg['text'])
    assert bool(_resolve({'whatsapp': [msg]}, update)['resolved']) is (not collision)


def _connected_call(source):
    return ({'phone': '+14155552222', 'timestamp': '2026-06-24 08:00:00',
             'is_outgoing': True, 'is_answered': True, 'call_type': 'phone', 'duration_seconds': 125}
            if source == 'calls' else
            {'jid': '14155552222@s.whatsapp.net', 'timestamp': '2026-06-24 08:00:00',
             'is_outgoing': True, 'is_missed': False})


@pytest.mark.parametrize('source', ['calls'])
def test_rendered_connected_call_review_closes_only_named_callback(tmp_path, monkeypatch, source):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, action_type='call_back', channel='phone', summary='Return Dana call')
    _loop(tmp_path, key='separate-document', summary='Send the pricing document')
    call = _connected_call(source)
    source_id, quote = call_evidence(call, source)
    local = {source: [call], 'contacts': [{'name': 'Dana', 'phones': ['+14155552222']}]}
    rendered = _format_recent_calls(resolve_contact_names(local)['recent_calls'])
    assert source_id in rendered and quote in rendered
    assert not _resolve(local)['resolved']
    update = _update(row, 'resolved', source, source_id, quote)
    assert compose_brief._project_loop_updates({'loop_updates': [update]},
        {'local': local, 'google': {}}, [row]) == []
    result = _resolve(local, update)
    assert [item['anchor_key'] for item in result['resolved']] == ['k']
    assert [item['anchor_key'] for item in result['active']] == ['separate-document']


@pytest.mark.parametrize('instant,closes', [('2026-06-24T07:00:00Z', False), ('2026-06-24T08:30:00Z', True)])
def test_call_provenance_uses_explicit_time_and_never_bare_record_id(tmp_path, monkeypatch, instant, closes):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, action_type='call_back', channel='phone', created_at='2026-06-24 08:00:00')
    call = {**_connected_call('calls'), 'timestamp': '2026-06-24 09:00:00',
            'timestamp_utc': instant, 'record_id': '42', 'service_provider': 'FaceTime'}
    source_id, quote = call_evidence(call, 'calls')
    local = {'calls': [call]}
    # A local row number cannot bypass the full-row/source-authority evidence binding.
    assert not _resolve(local, _update(row, 'resolved', 'calls', '42', quote))['resolved']
    assert bool(_resolve(local, _update(row, 'resolved', 'calls', source_id, quote))['resolved']) is closes


def test_call_evidence_preserves_repeated_hour_offset_when_captured(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/New_York')
    call = {**_connected_call('calls'), 'is_outgoing': False, 'is_answered': False,
            'timestamp': '2024-11-03 01:30:00', 'timestamp_utc': '2024-11-03T06:30:00Z', 'record_id': '42'}
    source_id, quote = call_evidence(call, 'calls')
    action = {'type': 'call_back', 'channel': 'phone', 'contactName': 'Dana',
              'contactIdentifier': '+14155552222', 'contextSummary': 'Respond to missed call',
              'evidence': [{'sourceType': 'calls', 'sourceId': source_id, 'snippet': quote}]}
    result = cr.resolve({'today': '2024-11-03', 'local': {'calls': [call]}, 'new_actions': [action]},
                        datetime(2024, 11, 3, 3), resolve_existing=False)
    assert result['active'][0]['created_at'] == '2024-11-03T01:30:00-05:00'
    # The later instant can have an earlier-looking wall time during the fold.
    assert cr._message_is_after('2024-11-03T06:15:00Z', '2024-11-03T01:45:00-04:00')
    assert not cr._message_is_after('2024-11-03T05:45:00Z', '2024-11-03T01:15:00-05:00')


def test_native_whatsapp_not_missed_attempt_cannot_prove_connected_or_hide_missed_call(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, action_type='call_back', channel='phone')
    attempted = _connected_call('whatsapp_calls')  # native schema has no answered/duration field
    missed = {**attempted, 'is_outgoing': False, 'is_missed': True,
              'timestamp': '2026-06-24 07:00:00'}
    local = {'whatsapp_calls': [missed, attempted],
             'contacts': [{'name': 'Dana', 'phones': ['+14155552222']}]}
    assert call_evidence(attempted, 'whatsapp_calls') == ('', '')
    resolved = resolve_contact_names(local)
    assert len(resolved['missed_calls']) == 1 and resolved['recent_calls'] == []
    update = _update(row, 'resolved', 'whatsapp_calls', 'invented-call-proof',
                     'Outgoing connected WhatsApp call to Dana')
    assert not _resolve(local, update)['resolved']
    assert cr._load_items()['k']['status'] == 'open'


@pytest.mark.parametrize('change', ['unanswered', 'unknown_connection', 'unknown_direction', 'earlier',
    'wrong_person', 'stale_version', 'unreviewed', 'fragment', 'revoked', 'revision', 'row_changed'])
@pytest.mark.parametrize('source', ['calls', 'whatsapp_calls'])
def test_callback_call_evidence_holds_on_missing_or_changed_proof(tmp_path, monkeypatch, source, change):
    import json
    _env(tmp_path, monkeypatch)
    monkeypatch.setenv('SOTTO_SKILL_COMPOSITION_PROOF', json.dumps({'source_revision': 'a' * 64}))
    row = _loop(tmp_path, action_type='call_back', channel='phone')
    call = _connected_call(source)
    key = 'is_answered' if source == 'calls' else 'is_missed'
    if change == 'unanswered':
        call[key] = source != 'calls'
    if change == 'unknown_connection':
        call.pop(key)
    if change == 'unknown_direction':
        call.pop('is_outgoing')
    if change == 'earlier':
        call['timestamp'] = '2026-06-23 07:00:00'
    if change == 'wrong_person':
        call['phone' if source == 'calls' else 'jid'] = '+14155559999'
    source_id, quote = call_evidence(call, source)
    update = _update(row, 'resolved', source, source_id or 'unproved', quote or 'Unproved call')
    if change == 'stale_version':
        update = _reviewed({**update, 'loopVersion': 'stale'})
    if change == 'unreviewed':
        update.pop('criticReceipt')
    if change == 'fragment':
        update['evidence'][0]['snippet'] = 'connected phone call'
        update = _reviewed(update)
    if change == 'revision':
        monkeypatch.setenv('SOTTO_SKILL_COMPOSITION_PROOF', json.dumps({'source_revision': 'b' * 64}))
    if change == 'row_changed':
        call['duration_seconds'] = 999
    local = {source: [call]}
    if change == 'revoked':
        local['source_status'] = {source: 'disabled'}
    assert not _resolve(local, update)['resolved']


@pytest.mark.parametrize('fields', [{'action_type': 'reply'}, {'action_type': 'waiting_on'},
    {'action_type': 'call_back', 'group_id': 'iMessage;+;group'},
    {'action_type': 'call_back', 'resolution_mode': 'explicit'}])
def test_connected_call_cannot_pay_other_obligations(tmp_path, monkeypatch, fields):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, **fields)
    call = _connected_call('calls'); source_id, quote = call_evidence(call, 'calls')
    assert not _resolve({'calls': [call]}, _update(row, 'resolved', 'calls', source_id, quote))['resolved']


@pytest.mark.parametrize('text', ['yes', '2pm works', 'Sent.'])
def test_short_entire_answer_requires_real_message_and_review(tmp_path, monkeypatch, text):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, summary='Confirm the time or delivery with Dana')
    msg = {'rowid': 45, 'is_from_me': True, 'handle': '+14155552222',
           'timestamp': '2026-06-24 08:00:00', 'text': text}
    source_id = message_evidence_id(msg, 'imessage')
    assert not _resolve({'imessage': [msg]}, _update(row, 'resolved', 'imessage', source_id, text, reviewed=False))['resolved']
    assert len(_resolve({'imessage': [msg]}, _update(row, 'resolved', 'imessage', source_id, text))['resolved']) == 1


def test_short_fragment_cannot_cherry_pick_incomplete_response(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch); row = _loop(tmp_path)
    msg = {'rowid': 46, 'is_from_me': True, 'handle': '+14155552222',
           'timestamp': '2026-06-24 08:00:00', 'text': 'I have not sent the pricing document yet.'}
    update = _update(row, 'resolved', 'imessage', message_evidence_id(msg, 'imessage'), 'sent')
    assert not _resolve({'imessage': [msg]}, update)['resolved']


@pytest.mark.parametrize('text,closes', [('The pricing document is sent.', False), ('Happy birthday!', True)])
def test_optional_birthday_closure_requires_actual_greeting(tmp_path, monkeypatch, text, closes):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, resolution_mode='birthday_greeting', summary='Optional birthday greeting')
    msg = {'rowid': 47, 'is_from_me': True, 'handle': '+14155552222',
           'timestamp': '2026-06-24 08:00:00', 'text': text}
    update = _update(row, 'resolved', 'imessage', message_evidence_id(msg, 'imessage'), text)
    assert bool(_resolve({'imessage': [msg]}, update)['resolved']) is closes


@pytest.mark.parametrize('source', ['calls', 'whatsapp_calls'])
def test_callback_capture_uses_missed_call_time_before_brief_and_later_answer(tmp_path, monkeypatch, source):
    from render_local import _format_missed_calls
    _env(tmp_path, monkeypatch)
    call = _connected_call(source)
    call['is_outgoing'] = False
    call['is_answered' if source == 'calls' else 'is_missed'] = source != 'calls'
    source_id, quote = call_evidence(call, source)
    local = {source: [call], 'contacts': [{'name': 'Dana', 'phones': ['+14155552222']}]}
    assert source_id in _format_missed_calls(resolve_contact_names(local)['missed_calls'])
    action = {'type': 'call_back', 'channel': 'phone', 'contactName': 'Dana',
              'contactIdentifier': '+14155552222', 'contextSummary': 'Respond to missed call',
              'evidence': [{'sourceType': source, 'sourceId': source_id, 'snippet': quote}]}
    captured = cr.resolve({'today': '2026-06-24', 'local': local, 'new_actions': [action]},
                          NOW, resolve_existing=False)['active']
    assert len(captured) == 1 and captured[0]['created_at'] == '2026-06-24 08:00:00'
    row = captured[0]
    msg = {'rowid': 48, 'is_from_me': True, 'handle': '+14155552222',
           'timestamp': '2026-06-24 08:30:00', 'text': 'Saw your call; Thursday at two works for me.'}
    local['imessage'] = [msg]
    update = _update(row, 'resolved', 'imessage', message_evidence_id(msg, 'imessage'), msg['text'])
    assert compose_brief._project_loop_updates({'loop_updates': [update]},
        {'local': local, 'google': {}}, [row]) == []
    assert [item['anchor_key'] for item in _resolve(local, update)['resolved']] == [row['anchor_key']]


@pytest.mark.parametrize('change', ['wrong_id', 'wrong_person', 'outgoing', 'future', 'revoked'])
def test_callback_capture_refuses_unobserved_or_wrong_missed_call(tmp_path, monkeypatch, change):
    _env(tmp_path, monkeypatch)
    call = _connected_call('calls'); call.update(is_outgoing=False, is_answered=False)
    if change == 'outgoing':
        call['is_outgoing'] = True
    if change == 'future':
        call['timestamp'] = '2026-06-24 10:00:00'
    source_id, quote = call_evidence(call, 'calls')
    if change == 'wrong_id':
        source_id = 'call:v1:invented'
    local = {'calls': [call]}
    if change == 'revoked':
        local['source_status'] = {'calls': 'disabled'}
    action = {'type': 'call_back', 'channel': 'phone', 'contactName': 'Dana',
              'contactIdentifier': '+14155559999' if change == 'wrong_person' else '+14155552222',
              'contextSummary': 'Respond to missed call',
              'evidence': [{'sourceType': 'calls', 'sourceId': source_id, 'snippet': quote}]}
    assert cr.resolve({'today': '2026-06-24', 'local': local, 'new_actions': [action]},
                      NOW, resolve_existing=False)['active'] == []


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


def test_paraphrase_recaptures_stable_base_without_redefining_the_task(tmp_path, monkeypatch):
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
    assert match["summary"] == original["summary"]


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
    monkeypatch.setattr(cr, "person_identity", lambda: _unique_index(index))
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
    monkeypatch.setattr(cr, "person_identity", lambda: _unique_index(index))
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
    monkeypatch.setattr(cr, "person_identity", lambda: _unique_index(index))
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
    monkeypatch.setattr(cr, "person_identity", lambda: _unique_index(index))
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


@pytest.mark.parametrize('cite_assistant', [True, False])
def test_assistant_source_cannot_mint_debt_but_original_human_email_can(tmp_path, monkeypatch, cite_assistant):
    _env(tmp_path, monkeypatch)
    # Display names are deliberately the same: only exact native source metadata is authority.
    human = {'id': 'human-request', 'from': 'Poke <dana@example.com>',
             'to': 'owner@example.com', 'labelIds': ['INBOX'],
             'date': '2026-06-24 08:00:00', 'snippet': 'Please send the signed contract.'}
    assistant = {**human, 'id': 'assistant-summary', 'is_assistant': True}
    action = {'type': 'reply', 'channel': 'email', 'contactName': 'Poke',
              'contactIdentifier': 'dana@example.com', 'contextSummary': 'Send the signed contract',
              'emailMessageId': assistant['id'] if cite_assistant else human['id']}
    result = cr.resolve({'today': '2026-06-24', 'local': {'emails': [human, assistant]},
                         'new_actions': [action]}, NOW, resolve_existing=False)
    assert len(result['active']) == (0 if cite_assistant else 1)
    assert len(cr._load_items()) == (0 if cite_assistant else 1)


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


@pytest.mark.parametrize('source', ['imessage', 'email'])
@pytest.mark.parametrize('after_request', [True, False])
def test_ordinary_capture_uses_request_time_so_later_answer_before_capture_can_close(tmp_path, monkeypatch, source, after_request):
    _env(tmp_path, monkeypatch)
    ask = 'Please send the pricing document.'
    answer = 'Here is the pricing document you asked for.'
    if source == 'email':
        ident = 'dana@example.com'
        request = {'messageId': 'request-1', 'from': ident, 'date': '2026-06-24 08:00:00', 'body': ask}
        reply = {'messageId': 'reply-2', 'to': ident, 'isSent': True,
                 'date': '2026-06-24 08:30:00', 'body': answer}
        field = 'emails'
    else:
        ident = '+14155552222'
        request = {'id': 'request-1', 'handle': ident, 'timestamp': '2026-06-24 08:00:00',
                   'is_from_me': False, 'text': ask}
        reply = {'id': 'reply-2', 'handle': ident, 'timestamp': '2026-06-24 08:30:00',
                 'is_from_me': True, 'text': answer}
        field = 'imessage'
    action = {'type': 'reply', 'channel': source, 'contactName': 'Dana', 'contactIdentifier': ident,
              'contextSummary': 'Send the pricing document',
              'evidence': [{'sourceType': source, 'sourceId': 'request-1', 'snippet': ask}]}
    if not after_request:
        reply['date' if source == 'email' else 'timestamp'] = '2026-06-24 07:30:00'
    local = {field: [request, reply]}
    row = cr.resolve({'today': '2026-06-24', 'local': local, 'new_actions': [action]},
                     NOW, resolve_existing=False)['active'][0]
    assert row['created_at'] == '2026-06-24 08:00:00'
    update = _update(row, 'resolved', source, 'reply-2', answer)
    result = _resolve(local, update)
    assert [item['anchor_key'] for item in result['resolved']] == ([row['anchor_key']] if after_request else [])
    assert bool(result['active']) is not after_request


@pytest.mark.parametrize('change', ['wrong_id', 'wrong_person', 'future', 'wrong_quote'])
def test_ordinary_capture_never_backdates_from_unproved_source(tmp_path, monkeypatch, change):
    _env(tmp_path, monkeypatch)
    request = {'id': 'request-1', 'handle': '+14155552222', 'timestamp': '2026-06-24 08:00:00',
               'is_from_me': False, 'text': 'Please send the pricing document.'}
    if change == 'wrong_person':
        request['handle'] = '+14155559999'
    if change == 'future':
        request['timestamp'] = '2026-06-24 10:00:00'
    action = {'type': 'reply', 'channel': 'imessage', 'contactName': 'Dana',
              'contactIdentifier': '+14155552222', 'contextSummary': 'Send the pricing document',
              'evidence': [{'sourceType': 'imessage', 'sourceId': 'wrong' if change == 'wrong_id' else 'request-1',
                           'snippet': 'Unobserved words' if change == 'wrong_quote' else request['text']}]}
    row = cr.resolve({'today': '2026-06-24', 'local': {'imessage': [request]}, 'new_actions': [action]},
                     NOW, resolve_existing=False)['active'][0]
    assert row['created_at'] == '2026-06-24 09:00:00'


def test_recapture_does_not_rebase_an_existing_task_cutoff(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, created_at='2026-06-24 08:45:00')
    request = {'id': 'request-1', 'handle': '+14155552222', 'timestamp': '2026-06-24 08:00:00',
               'is_from_me': False, 'text': 'Please send the pricing document.'}
    action = {'loopId': row['anchor_key'], 'type': 'reply', 'channel': 'imessage',
              'contactName': 'Dana', 'contactIdentifier': '+14155552222',
              'contextSummary': row['summary'],
              'evidence': [{'sourceType': 'imessage', 'sourceId': 'request-1', 'snippet': request['text']}]}
    rows = cr.resolve({'today': '2026-06-24', 'local': {'imessage': [request]}, 'new_actions': [action]},
                      NOW, resolve_existing=False)['active']
    assert len(rows) == 1 and rows[0]['created_at'] == row['created_at']


@pytest.mark.parametrize('outgoing', [False, True])
def test_new_same_day_commitment_survives_automatic_closure(tmp_path, monkeypatch, outgoing):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path)
    delivered = {'rowid': 801, 'handle': '+14155552222', 'is_from_me': True,
                 'timestamp': '2026-06-24 08:30:00', 'text': 'The pricing document is attached.'}
    update = _update(row, 'resolved', 'imessage', '801', delivered['text'])
    _resolve({'imessage': [delivered]}, update)
    closed = cr._load_items()['k']
    assert closed['closed_at'].startswith('2026-06-24T08:30:00')
    fresh = {'rowid': 802, 'handle': '+14155552222', 'is_from_me': outgoing,
             'timestamp': '2026-06-24 09:30:00', 'text': 'I will send the revised contract today.'}
    action = {'type': 'reply', 'channel': 'imessage', 'contactIdentifier': '+14155552222',
              'contactName': 'Dana', 'contextSummary': 'Send the revised contract',
              'loopId': 'k', 'newObligation': outgoing,
              'evidence': [{'sourceType': 'imessage', 'sourceId': '802', 'snippet': fresh['text']}]}
    out = cr.resolve({'today': '2026-06-24', 'new_actions': [action], 'local': {'imessage': [fresh]}},
                     datetime(2026, 6, 24, 10))
    assert len(out['active']) == 1 and out['active'][0]['summary'] == 'Send the revised contract'


def test_country_codes_and_numeric_email_do_not_share_person_keys(tmp_path, monkeypatch):
    import knowledge
    from textutil import _normalize_identifier
    _env(tmp_path, monkeypatch)
    for normalize in (knowledge.normalize_identifier, _normalize_identifier):
        assert normalize('4155552222@example.com') != normalize('+14155552222')
        assert normalize('+44 4155552222') != normalize('+91 4155552222')
        assert normalize('14155552222@s.whatsapp.net') == normalize('+1 (415) 555-2222')
    assert not cr._handle_matches('+44 4155552222', '+91 4155552222')
    ids = cr._collect_all_identifiers('14155552222@s.whatsapp.net', '', {
        'contacts': [{'phones': ['+1 (415) 555-2222'], 'emails': ['dana@example.com']}]})
    assert 'dana@example.com' in ids


def test_waiting_date_comes_from_the_cited_promise_not_other_message_dates(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, action_type='waiting_on')
    msg = {'rowid': 803, 'handle': '+14155552222', 'is_from_me': False,
           'timestamp': '2026-06-24 08:00:00',
           'text': 'I am away tomorrow. I will send the pricing document by Friday.'}
    update = _update(row, 'waiting', 'imessage', '803', 'I will send the pricing document by Friday.')
    _resolve({'imessage': [msg]}, update)
    assert cr._load_items()['k']['chase_after'] == '2026-06-26'


def test_read_attachment_content_can_prove_delivery_but_filename_cannot(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, action_type='waiting_on', channel='email', contact_identifier='dana@example.com')
    text = 'The agreed pricing is $500 per month.'
    mail = {'id': 'att-mail', 'from': 'dana@example.com', 'date': '2026-06-24T08:00:00Z',
            'body': 'See attached.', 'attachments': [{'filename': 'pricing.txt', 'text': text}]}
    update = _update(row, 'resolved', 'email', 'att-mail', text)
    assert _resolve({'emails': [mail]}, update)['resolved']
    row = _loop(tmp_path, key='other', action_type='waiting_on', channel='email', contact_identifier='dana@example.com')
    mail['attachments'] = [{'filename': text, 'unreadable': 'no local text'}]
    assert not _resolve({'emails': [mail]}, _update(row, 'resolved', 'email', 'att-mail', text))['resolved']


def test_callback_original_evidence_reaches_critic(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, action_type='call_back', channel='phone')
    call = _connected_call('calls')
    source_id, quote = call_evidence(call, 'calls')
    update = _update(row, 'resolved', 'calls', source_id, quote, reviewed=False)
    reviewed = set()
    inputs = {'local': {'calls': [call], 'action_ledger': [row]}, 'google': {}}
    monkeypatch.setattr(compose_brief, '_protected_action_ledger', lambda _: [row])
    context = compose_brief._loop_update_review_context({'loop_updates': [update]}, inputs, reviewed)
    assert context[0]['evidence'][0]['original_context'] == quote
    assert context[0]['evidence'][0]['direction'] == 'sent_by_user'
    assert cr.critic_review_receipt(update) in reviewed


@pytest.mark.parametrize('truncated', [False, True])
def test_reply_all_from_group_member_needs_complete_roster(tmp_path, monkeypatch, truncated):
    _env(tmp_path, monkeypatch)
    group = 'iMessage;+;work-group'
    row = _loop(tmp_path, action_type='waiting_on', group_id=group, contact_identifier='')
    roster = ['dana@example.com', 'alex@example.com']
    if truncated:
        roster += [f'person{i}@example.com' for i in range(98)]
    local = {'imessage': [{'chat_guid': group, 'is_group_chat': True, 'group_participants': roster}],
             'emails': [{'id': 'group-reply', 'from': 'dana@example.com',
                         'to': ['owner@example.com', *roster[1:]], 'date': '2026-06-24T08:00:00Z',
                         'body': 'The pricing document has been delivered to everyone.'}]}
    update = _update(row, 'resolved', 'email', 'group-reply', local['emails'][0]['body'])
    assert bool(_resolve(local, update)['resolved']) is (not truncated)


def test_future_completion_evidence_cannot_close_a_task(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path)
    msg = {'rowid': 804, 'handle': '+14155552222', 'is_from_me': True,
           'timestamp': '2026-06-25 08:00:00', 'text': 'The pricing document is attached.'}
    update = _update(row, 'resolved', 'imessage', '804', msg['text'])
    assert not _resolve({'imessage': [msg]}, update)['resolved']


def test_fresh_request_after_terminal_peers_uses_latest_real_instant_across_fold(tmp_path,monkeypatch):
    _env(tmp_path,monkeypatch)
    monkeypatch.setenv('SOTTO_TIMEZONE','America/Los_Angeles')
    for key,closed_at in [('prior-one','2026-11-01T00:00:00-07:00'),
                          ('prior-two','2026-11-01T01:50:00-07:00')]:
        _loop(tmp_path,key,status='resolved',resolution='user_resolved',closed_at=closed_at,
              created_at='2026-10-31T12:00:00-07:00',channel='email',source='gmail',
              contact_identifier='dana@example.test',source_thread_id=key)
    messages=[{'id':ident,'threadId':'fresh-thread','from':'dana@example.test',
               'body':'Can you confirm the lunch address?','date':stamp}
              for ident,stamp in [('before-fold','2026-11-01T01:45:00-07:00'),
                                 ('after-fold','2026-11-01T01:15:00-08:00')]]
    action={'type':'reply','channel':'email','contactName':'Dana',
            'contactIdentifier':'dana@example.test','emailThreadId':'fresh-thread',
            'contextSummary':'Confirm the lunch address'}
    result=cr.resolve({'today':'2026-11-01','emails':messages,'new_actions':[action]},
                      datetime.fromisoformat('2026-11-01T11:00:00+00:00'))
    opened,=result['active']
    assert opened['created_at']=='2026-11-01T01:15:00-08:00'
    monkeypatch.setenv('SOTTO_TIMEZONE','UTC')
    assert not cr._message_is_after('2026-11-01T09:00:00Z',opened['created_at'])
    assert cr._message_is_after('2026-11-01T09:30:00Z',opened['created_at'])


@pytest.mark.parametrize('reopen', [False, True])
@pytest.mark.parametrize('after_request', [False, True])
def test_naive_source_keeps_wall_clock_order_after_timezone_setting_correction(
        tmp_path, monkeypatch, reopen, after_request):
    _env(tmp_path, monkeypatch)
    if reopen:
        _loop(tmp_path, status='resolved', resolution='user_resolved',
              closed_at='2026-06-23 10:00:00', source='imessage',
              source_refs=[{'sourceType': 'imessage', 'sourceId': 'old-request',
                            'snippet': 'Please send the previous document.'}])
    ask = 'Please send the pricing document.'
    answer = 'Here is the pricing document you asked for.'
    request = {'id': 'new-request', 'handle': '+14155552222',
               'timestamp': '2026-06-24 08:00:00.500000',
               'is_from_me': False, 'text': ask}
    reply = {'id': 'answer', 'handle': '+14155552222',
             'timestamp': '2026-06-24 08:30:00.500000' if after_request else
                          '2026-06-24 07:30:00.500000',
             'is_from_me': True, 'text': answer}
    action = {'type': 'reply', 'channel': 'imessage', 'contactName': 'Dana',
              'contactIdentifier': '+14155552222', 'contextSummary': 'Send the pricing document',
              'evidence': [{'sourceType': 'imessage', 'sourceId': 'new-request', 'snippet': ask}]}
    if not reopen:
        action['newObligation'] = True
    local = {'imessage': [request, reply]}
    captured = cr.resolve({'today': '2026-06-24', 'local': local, 'new_actions': [action]},
                          NOW, resolve_existing=False)
    row, = captured['active']
    # The Mac clock has not moved. Correcting only Sotto's configured zone must not
    # turn its earlier 07:30 message into a reply after the original 08:00 request.
    monkeypatch.setenv('SOTTO_TIMEZONE', '-07:00')
    update = _update(row, 'resolved', 'imessage', 'answer', answer)
    projected = compose_brief._project_loop_updates({'loop_updates': [update]},
        {'now': '2026-06-24T09:00:00-07:00', 'local': local, 'google': {}}, [row])
    applied = _resolve(local, update)
    assert (not projected, bool(applied['resolved'])) == (after_request, after_request)
    assert row['created_at'] == '2026-06-24 08:00:00.500000'
    assert bool(applied['active']) is not after_request


@pytest.mark.parametrize('recipient_field', ['to', 'cc', 'bcc'])
def test_owner_sent_introduction_preserves_native_recipient_identity(tmp_path, monkeypatch, recipient_field):
    import gather_google
    from gmail_read import message_fields
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, channel='gmail', contact_identifier='introducer@example.com',
                summary='Reply to the introduction')
    native = {'id': 'intro-reply', 'labelIds': ['SENT'], 'payload': {'headers': [
        {'name': 'From', 'value': 'owner@example.com'},
        {'name': recipient_field, 'value': 'Introducer <introducer@example.com>'},
        {'name': 'Date', 'value': '2026-06-24T08:00:00Z'}]}}
    full = message_fields(native)
    full['body'] = 'Thanks for the introduction; I sent my availability.'
    mail = gather_google.normalize_email({'id': 'intro-reply'}, full)
    update = _update(row, 'resolved', 'gmail', mail['id'], mail['body'])
    assert _resolve({'emails': [mail]}, update)['resolved']
    # Incoming Bcc and body claims are not recipient proof for an owner reply.
    assert 'introducer@example.com' not in cr._email_recipients({
        'isSent': False, 'bcc': 'introducer@example.com', 'body': 'to bcc'})
    assert 'introducer@example.com' not in cr._email_recipients({
        'isSent': True, 'to': 'other@example.com', 'body': 'introducer@example.com to bcc'})


@pytest.mark.parametrize('outgoing', [False, True])
def test_reviewed_answered_call_in_either_direction_fulfills_only_callback(tmp_path, monkeypatch, outgoing):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, action_type='call_back', summary='Reconnect with Dana by phone')
    _loop(tmp_path, key='separate-ask', summary='Send the proposal')
    call = {**_connected_call('calls'), 'is_outgoing': outgoing}
    source_id, quote = call_evidence(call, 'calls')
    update = _update(row, 'resolved', 'calls', source_id, quote)
    result = _resolve({'calls': [call]}, update)
    assert [r['anchor_key'] for r in result['resolved']] == ['k']
    assert [r['anchor_key'] for r in result['active']] == ['separate-ask']
    assert result['resolved'][0]['resolution_evidence'].startswith('Outgoing' if outgoing else 'Inbound')
