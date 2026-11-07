"""Current owner instructions govern context, composition and delayed delivery."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import time

import pytest

import compose_notification as notification
import delivery_effects
import jsonstore
import personal_context


@pytest.fixture
def current_owner(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'c' * 32)
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'selfhost')
    monkeypatch.setattr(notification.style_apply, 'apply', lambda _: {})
    monkeypatch.setattr(notification, '_read_json_script', lambda *a: {})
    monkeypatch.setattr(notification.loops_query, 'query', lambda: {'you_owe': [], 'waiting_on_them': []})
    monkeypatch.setattr(delivery_effects, '_loops', lambda: {})
    path = tmp_path / 'knowledge/last_local_snapshot.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'local': {'contacts': [
        {'name': 'Sarah Example', 'emails': ['sarah@example.com'], 'phones': ['+12025550111']},
        {'name': 'Bob Example', 'emails': ['bob@example.com'], 'phones': ['+12025550112']} ]}}))
    return tmp_path, datetime(2026, 11, 7, 11, tzinfo=timezone.utc)


def prefs(root, **values):
    (root / 'preferences.json').write_text(json.dumps({'explicit': values}))


def request(source='email', **fields):
    return {'source': source, 'id': 'ask', 'threadId': 'group-thread',
            'from': 'sarah@example.com', 'handle': '+12025550111',
            'date': '2026-11-07T10:58:00Z', 'text': 'Please review the proposal urgently.', **fields}


def bundle(*events):
    return {'events': [{'decision_id': str(i), 'class': 'urgent', 'sender': 'Sarah Example',
                        'event': event} for i, event in enumerate(events)]}


def staged(root):
    return json.loads((root / ('events/delivery-effects-' + 'c' * 32 + '.json')).read_text())['effects']


@pytest.mark.parametrize('source', ['email', 'GMAIL', 'imessage', 'whatsapp'])
def test_composer_resolves_muted_names_before_reading_shared_thread(current_owner, monkeypatch, source):
    root, now = current_owner
    prefs(root, mute_people=['Bob Example'])
    event = request(source)
    private = {**event, 'id': 'private', 'from': 'bob@example.com', 'handle': '+12025550112',
               'contact_jid': '12025550112@s.whatsapp.net', 'text': 'MUTED_CONTEXT_CANARY'}
    path = root / 'events/queue.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'event': private}) + '\n')
    captured = []
    def write(items, _llm):
        captured.extend(notification._writer_item(item) for item in items)
        return []
    monkeypatch.setattr(notification, '_write', write)
    assert notification.compose('event', bundle(event), now=now) == 'NO_NUDGES'
    assert captured and 'MUTED_CONTEXT_CANARY' not in json.dumps(captured)


def test_mute_after_admission_skips_writer_and_leaves_unrelated_candidate(current_owner, monkeypatch):
    root, now = current_owner
    prefs(root, mute_people=['Sarah Example'])
    admitted = bundle(request())
    admitted['events'].append({'decision_id': 'other', 'class': 'urgent', 'sender': 'Bob Example',
        'event': request(id='other', **{'from': 'bob@example.com', 'handle': '+12025550112'})})
    def write(items, _llm):
        assert [item['id'] for item in items] == ['other']
        return [{'id': 'other', 'text': 'Bob needs your decision.', 'draft': '', 'decline': ''}]
    monkeypatch.setattr(notification, '_write', write)
    assert notification.compose('event', admitted, now=now) == 'Bob needs your decision.'


@pytest.mark.parametrize('mute', [{'mute_people': ['Sarah Example']},
                                {'mute_senders': ['@example.com']},
                                {'mute_senders': ['+1 (202) 555-0111']}])
def test_new_mute_is_checked_on_serialized_delivery_retry(current_owner, monkeypatch, mute):
    root, now = current_owner
    monkeypatch.setattr(notification, '_write', lambda *_: [
        {'id': '0', 'text': 'Sarah needs your decision.', 'draft': '', 'decline': ''}])
    assert notification.compose('event', bundle(request()), now=now) == 'Sarah needs your decision.'
    proof = staged(root)
    assert delivery_effects.valid(proof, now.timestamp())
    prefs(root, **mute)
    # Re-read the serialized proof: no model recomposition or process-local decision is needed.
    assert not delivery_effects.valid(staged(root), now.timestamp())
    prefs(root)
    assert delivery_effects.valid(staged(root), now.timestamp())


def test_only_selected_sender_is_bound_and_proof_retains_no_message_body(current_owner, monkeypatch):
    root, now = current_owner
    monkeypatch.setattr(notification, '_write', lambda *_: [
        {'id': '0', 'text': 'Sarah needs your decision.', 'draft': '', 'decline': ''}])
    other = request(id='other', **{'from': 'bob@example.com', 'handle': '+12025550112'})
    notification.compose('event', bundle(request(), other), now=now)
    proof = staged(root)
    bound = [effect['notification_sender'] for effect in proof if 'notification_sender' in effect]
    assert len(bound) == 1 and 'sarah@example.com' in json.dumps(bound)
    assert 'bob@example.com' not in json.dumps(bound)
    assert 'Please review' not in json.dumps(proof)
    prefs(root, mute_people=['Bob Example'], mute_senders=['bob@example.com'])
    assert delivery_effects.valid(proof, now.timestamp())


def test_proactive_offer_obeys_mute_changed_after_composition(current_owner, monkeypatch):
    root, now = current_owner
    # A template offer has no source event and must still carry its selected person identity.
    item = {'kind': 'handoff', 'key': 'handoff', 'person': 'Sarah Example',
            'channel': 'email', 'identifier': 'sarah@example.com',
            'detail': 'Sarah needs a decision. Want a draft?'}
    assert notification.compose('proactive', [item], now=now).endswith('Want a draft?')
    prefs(root, mute_people=['Sarah Example'])
    assert not delivery_effects.valid(staged(root), now.timestamp())


@pytest.mark.parametrize('value', [None, [], 'unknown'])
def test_malformed_sender_proof_holds_delivery(current_owner, value):
    _, now = current_owner
    assert not delivery_effects.valid([{'kind': 'eligibility', 'notification_sender': value}], now.timestamp())


def test_shared_context_filter_caches_graph_names_only_with_explicit_name_mutes(current_owner, monkeypatch):
    root, _ = current_owner
    (root / 'knowledge/last_local_snapshot.json').write_text('{"local":{}}')
    reads = []
    monkeypatch.setattr(personal_context, 'graph_display_name', lambda ident: reads.append(ident) or 'Sarah Example')
    include = personal_context.conversation_filter({'mute_people': ['Sarah Example']})
    assert not include(request()) and not include(request(id='another'))
    assert reads == ['sarah@example.com']
    reads.clear()
    assert personal_context.conversation_filter({})(request())
    assert reads == []


def test_sparse_contacts_use_the_real_canonical_identity(current_owner):
    import knowledge_update
    root, _ = current_owner
    (root / 'knowledge/last_local_snapshot.json').write_text('{"local":{}}')
    knowledge_update.apply({'person_updates': [
        {'identifier': 'sarah@example.com', 'person_name': 'Sarah Example', 'facts': []}]})
    include = personal_context.conversation_filter({'mute_people': ['Sarah Example']})
    assert not include(request())
    assert include(request(**{'from': 'another@example.com', 'handle': '+12025550115'}))


def test_without_name_mutes_delivery_reads_no_contact_snapshot_or_graph(current_owner, monkeypatch):
    _, now = current_owner
    monkeypatch.setattr(Path, 'read_text', lambda *a, **k: pytest.fail('unnecessary snapshot/graph read'))
    monkeypatch.setattr(personal_context, 'graph_display_name', lambda _: pytest.fail('unnecessary graph read'))
    assert delivery_effects.valid([{'kind': 'eligibility', 'notification_sender': request()}], now.timestamp())


@pytest.mark.parametrize('acceptance', ['not_attempted', 'unknown'])
def test_restarted_outbox_obeys_mute_without_replaying_unknown_contact(current_owner, monkeypatch, acceptance):
    root, now = current_owner
    monkeypatch.setattr(notification, '_write', lambda *_: [
        {'id': '0', 'text': 'Sarah needs your decision.', 'draft': '', 'decline': ''}])
    text = notification.compose('event', bundle(request()), now=now)
    module_path = Path(notification.PACK).parent / 'runtime/trigger-receiver/outbox.py'
    def load():
        spec = importlib.util.spec_from_file_location('notification_outbox', module_path)
        box = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(box)
        box.HOOKS.update(data_root=lambda: str(root), json_transaction=jsonstore.transaction,
            local_today=lambda: now.date().isoformat(), record=lambda *a, **k: None,
            valid=lambda payload: delivery_effects.valid(payload['effects'], now.timestamp()),
            send=lambda *args: pytest.fail('muted or uncertain notification reached provider'),
            gallery_receipt=lambda _: None)
        return box
    box = load()
    box._enqueue('queued', box.KIND_NUDGE,
        {'label': 'event', 'body': text, 'target': 'invented-owner', 'effects': staged(root)},
        time.time(), now.date().isoformat())
    with jsonstore.transaction(box.path(), default={}) as doc:
        doc['rows'][0].update(acceptance=acceptance, next_at=0)
    prefs(root, mute_people=['Sarah Example'])
    restarted = load()
    assert not restarted._attempt('queued')
    row, = json.loads(Path(restarted.path()).read_text())['rows']
    assert row['status'] == (restarted.STATUS_PENDING if acceptance == 'unknown' else restarted.STATUS_SUPERSEDED)
    if acceptance == 'unknown':
        assert row['acceptance'] == 'unknown' and row['payload']['body'] == text


def test_cached_offer_respects_muted_mail_envelope_name_despite_contact_alias(current_owner):
    root, now = current_owner
    prefs(root, mute_people=['Bob Example'])
    path = root / 'knowledge/last_local_snapshot.json'
    path.write_text(json.dumps({'local': {'contacts': [
        {'name': 'Robert Example', 'emails': ['bob@example.com']}]}}))
    muted = request(**{'from': 'Bob Example <bob@example.com>', 'to': 'sarah@example.com',
                       'text': 'MUTED_ENVELOPE_CANARY'})
    unmuted = {**muted, 'id': 'another', 'from': 'Other Person <other@example.com>',
               'text': 'An unmuted detail'}
    queue = root / 'events/queue.jsonl'
    queue.parent.mkdir()
    queue.write_text(''.join(json.dumps({'event': event}) + '\n' for event in [muted, unmuted]))
    rows = notification._prep_threads({'identifier': 'sarah@example.com'}, now)
    assert [row['snippet'] for row in rows] == ['An unmuted detail']
