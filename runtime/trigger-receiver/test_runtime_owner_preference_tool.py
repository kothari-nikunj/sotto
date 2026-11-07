"""Explicit private deltas retain original outcomes in the existing preference store."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json

import pytest

import runtime_owner_preference_tool as tool
from test_runtime_owner_loop_close import context, IDENTITY, ROOT, WHEN, THREAD, capability

__all__ = ['context']


def request(args=None, *, operation='apply', event='a' * 64, text='Mute Alice@example.com please'):
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    action = hashlib.sha256(json.dumps([event, 'owner_preference_tool', canonical],
                                      ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return dict(version=1, operation=operation, identity=IDENTITY, action_id=action,
                event_id=event, payload_hash='b' * 64, text_hash=hashlib.sha256(text.encode()).hexdigest(),
                text=text, admitted_at_ms=int(WHEN.timestamp() * 1000), provider_thread_id=THREAD, args=args)


def call(body, context):
    data, policy, activation = context
    policy.update(SOTTO_DATA=str(data), SOTTO_RUNTIME_PROFILE='managed-photon-fixed-five-v1')
    return tool.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT, policy=policy,
                        data_root=data, capability=capability, activation=activation)


def test_exact_original_receipt_survives_changes_settings_and_quiet(context, monkeypatch):
    writer = tool._preferences()
    writer.set_scalar('nudge_snooze_until', '2099-01-01')
    path = context[0] / 'preferences.json'
    data = json.loads(path.read_text())
    data.update(learned={'private': 'do not expose'}, owner_quiet_receipts={'saved': {'proof': 'keep'}})
    path.write_text(json.dumps(data))
    body = request({'operation': 'add', 'field': 'mute_senders', 'value': 'Alice@example.com'})
    assert call(dict(body, operation='inspect'), context)['status'] == 'not_found'
    committed = call(body, context)
    assert committed['outcome']['value'] == 'alice@example.com'
    assert writer.sender_is_muted('ALICE@example.com', writer.load_explicit()['mute_senders'])
    removed = call(request({'operation': 'remove', 'field': 'mute_senders', 'value': 'alice@example.com'},
                          event='c' * 64), context)
    assert removed['outcome']['present'] is False
    assert call(dict(body, operation='inspect'), context) == committed
    assert call(body, context) == committed
    monkeypatch.setenv('SOTTO_NUDGE_BUDGET', '9')
    budget = request({'operation': 'nudge-budget', 'direction': 'fewer'}, event='d' * 64)
    receipt = call(budget, context)
    assert receipt['outcome'] == {'operation': 'nudge-budget', 'direction': 'fewer', 'effective': 4, 'ceiling': 9}
    writer.change_nudge_budget('no')
    monkeypatch.setenv('SOTTO_NUDGE_BUDGET', '2')
    assert call(dict(budget, operation='inspect'), context) == receipt
    saved = json.loads(path.read_text())
    assert saved['learned'] == data['learned'] and saved['owner_quiet_receipts'] == data['owner_quiet_receipts']
    assert saved['explicit']['nudge_snooze_until'] == '2099-01-01'


def test_concurrent_delta_and_duplicate_no_recompute(context, monkeypatch):
    monkeypatch.setenv('SOTTO_NUDGE_BUDGET', '8')
    writer = tool._preferences()
    proof = dict(domain='sotto.owner-preference-tool.v1', event_id='event', action_id='action',
                 payload_hash='payload', text_hash='text', identity_digest='identity', args_hash='args',
                 args={'operation': 'nudge-budget', 'direction': 'fewer'}, admitted_at_ms=1, provider_thread_id=THREAD)
    with ThreadPoolExecutor(4) as executor:
        outcomes = list(executor.map(lambda _: writer.admitted_owner_preference(proof), range(8)))
    assert all(row['effective'] == 4 for row in outcomes)
    assert writer.effective_nudge_budget() == 4
    changed = dict(proof, args={'operation': 'clear-tone'})
    with pytest.raises(writer.OwnerPreferenceRefusalError, match='event_conflict'):
        writer.admitted_owner_preference(changed)


def test_read_bound_explicit_only_and_legacy_sender_removal(context):
    data = context[0]
    fields = tool._preferences().LISTS
    explicit = {field: ['😀' * 500] * 100 for field in fields}
    explicit['mute_senders'] = ['ALICE@example.com', 'alice@example.com']
    (data / 'preferences.json').write_text(json.dumps({'explicit': explicit, 'learned': {'secret': True}}))
    projection = call(request(operation='read'), context)
    assert projection['coverage'] == 'explicit_only' and 'learned' not in projection
    assert len(json.dumps(projection).encode()) <= 512 * 1024 and projection['truncated_fields']
    result = call(request({'operation': 'remove', 'field': 'mute_senders', 'value': 'ALICE@example.com'}), context)
    assert result['outcome']['value'] == 'alice@example.com'
    assert tool._preferences().load_explicit()['mute_senders'] == []


def test_bad_span_arguments_route_and_changed_event_refuse_before_effect(context):
    args = {'operation': 'add', 'field': 'tone_notes', 'value': 'invented wording'}
    assert call(request(args), context)['code'] == 'owner_span_unavailable'
    assert call(request({'operation': 'remove', 'field': 'tone_notes', 'value': 'missing'}), context)['code'] == 'target_unavailable'
    for bad in ({'operation': 'add', 'field': 'brief_audio', 'value': 'both'},
                {'operation': 'clear-tone', 'extra': True}, {'operation': 'nudge-budget', 'direction': 'max'}):
        with pytest.raises(ValueError):
            call(request(bad), context)
    with pytest.raises(ValueError, match='hash'):
        call(dict(request(operation='read'), text_hash='c' * 64), context)
    with pytest.raises(tool.UnavailableError):
        call(dict(request(operation='read'), provider_thread_id='imessage:iMessage;-;other~+15555550123'), context)
    first = request({'operation': 'clear-tone'})
    assert call(first, context)['status'] == 'committed'
    assert call(request({'operation': 'nudge-budget', 'direction': 'no'}), context)['code'] == 'event_conflict'


@pytest.mark.parametrize('field', ['mute_senders', 'mute_people', 'mute_sections', 'tone_notes', 'vip_people'])
def test_all_list_deltas_and_clear(context, field):
    body = request({'operation': 'add', 'field': field, 'value': 'Cedar'}, text='Use Cedar please')
    assert call(body, context)['outcome']['present'] is True
    projection = call(request(operation='read'), context)
    assert projection['preferences'][field] == ['cedar' if field == 'mute_senders' else 'Cedar']
    assert call(request({'operation': 'clear-tone'}, event='c' * 64), context)['outcome']['count'] == 0


def test_uncertain_atomic_write_can_only_be_inspected(context, monkeypatch):
    writer = tool._preferences()
    monkeypatch.setattr(tool, '_preferences', lambda: writer)
    durable = writer._durable_preferences_replace
    def lose_response(path, data):
        durable(path, data)
        raise OSError('synthetic response lost after durable write')
    monkeypatch.setattr(writer, '_durable_preferences_replace', lose_response)
    body = request({'operation': 'nudge-budget', 'direction': 'no'})
    with pytest.raises(OSError):
        call(body, context)
    assert call(dict(body, operation='inspect'), context)['outcome']['effective'] == 0
    assert writer.effective_nudge_budget() == 0
    path = context[0] / 'preferences.json'
    data = json.loads(path.read_text())
    data['owner_preference_receipts'][body['event_id']]['outcome']['effective'] = True
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='receipt'):
        call(dict(body, operation='inspect'), context)


@pytest.mark.parametrize('direction', ['no', 'more'])
def test_inspection_rejects_inconsistent_integer_budget_receipt(context, direction, monkeypatch):
    monkeypatch.setenv('SOTTO_NUDGE_BUDGET', '4')
    body = request({'operation': 'nudge-budget', 'direction': direction})
    call(body, context)
    path = context[0] / 'preferences.json'
    data = json.loads(path.read_text())
    data['owner_preference_receipts'][body['event_id']]['outcome']['effective'] = 2
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='receipt'):
        call(dict(body, operation='inspect'), context)
    assert path.read_bytes() == before


def procedure_offer(context, monkeypatch, rule='Never book me Fridays.'):
    from runtime_owner_loop import _load_writer
    _load_writer()
    import pending_offer
    import compose_brief
    import delivery_effects
    data, policy, _ = context
    policy['_source_authority'] = lambda: {'complete': True, 'revision': 'c' * 64}
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'e' * 32)
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', IDENTITY['owner_id'])
    monkeypatch.setattr(pending_offer, '_now', lambda: WHEN)
    rule = compose_brief._pick_procedural_candidate({'procedural_candidates': [{'rule': rule}]})
    assert rule
    output = compose_brief._append_procedure_offer({'brief_markdown': '## Evening\nToday.'}, {'_procedure_offer': rule})
    assert rule in output['brief_markdown']
    effects = json.loads((data / 'events' / ('delivery-effects-' + 'e'*32 + '.json')).read_text())['effects']
    delivery_effects.finalize(effects, {'message_id': 'accepted-procedure', 'target': 'photon',
        'chat_id': 'iMessage;-;owner', 'accepted_at': WHEN.timestamp()})
    return request({'operation': 'accept-procedure-offer', 'selection': {
        'reply_to_message_id': 'accepted-procedure', 'provider_sent_at_ms': None,
        'provider_message_id': 'owner-procedure-yes'}}, text='Yes.')


def test_actual_composed_procedure_offer_append_and_original_recovery(context, monkeypatch):
    import master_file
    body = procedure_offer(context, monkeypatch, 'Never book Fridays.\nUse mornings instead.')
    before = tool._preferences().load_explicit()
    receipt = call(body, context)
    assert receipt['outcome']['rule'] == 'Never book Fridays.\nUse mornings instead.'
    assert receipt['outcome']['section'] == 'Procedures'
    stored = master_file.read()
    assert stored == '## Procedures\nNever book Fridays.\nUse mornings instead.\n'
    import compose_brief
    import importlib.util
    from pathlib import Path
    prep_path = Path(tool.__file__).resolve().parents[2] / 'sotto-chief-of-staff/meeting-prep/scripts/compose_meeting_prep.py'
    spec = importlib.util.spec_from_file_location('procedure_test_prep', prep_path)
    prep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prep)
    assert receipt['outcome']['rule'] in compose_brief._format_master_context()
    assert receipt['outcome']['rule'] in prep._master_context()
    context[1]['_source_authority'] = lambda: {'complete': False}
    assert call(dict(body, operation='inspect'), context) == receipt
    assert call(body, context) == receipt and master_file.read() == stored
    assert tool._preferences().load_explicit() == before
    assert call(request({'operation': 'clear-tone'}, event=body['event_id']), context)['code'] == 'event_conflict'


def test_procedure_append_uncertain_is_held_without_repeat(context, monkeypatch):
    body = procedure_offer(context, monkeypatch)
    import master_file
    append = master_file.append_section
    calls = []
    def uncertain(section, rule):
        calls.append(rule)
        append(section, rule)
        raise OSError('response lost after atomic markdown effect')
    monkeypatch.setattr(master_file, 'append_section', uncertain)
    assert call(body, context) == {'version': 1, 'status': 'unknown'}
    assert call(dict(body, operation='inspect'), context) == {'version': 1, 'status': 'unknown'}
    assert call(body, context) == {'version': 1, 'status': 'unknown'}
    assert len(calls) == 1 and master_file.read().count('Never book me Fridays.') == 1


def test_procedure_capacity_and_section_injection_refuse(context, monkeypatch):
    body = procedure_offer(context, monkeypatch, 'Use mornings.\n## About\nUnrelated section')
    assert call(body, context)['code'] == 'target_unavailable'
    import master_file
    assert master_file.read() == ''


def test_procedure_original_master_capacity_refusal(context, monkeypatch):
    import master_file
    body = procedure_offer(context, monkeypatch)
    master_file.set_section('About', 'x' * 7975)
    before = master_file.read()
    assert call(body, context)['code'] == 'master_capacity'
    assert master_file.read() == before
    assert call(dict(body, operation='inspect'), context)['code'] == 'master_capacity'


def test_procedure_preview_does_not_consume_or_write(context, monkeypatch):
    body = procedure_offer(context, monkeypatch)
    before = (context[0] / 'preferences.json').read_bytes() if (context[0] / 'preferences.json').exists() else None
    result = call(dict(body, operation='preview'), context)
    assert result['status'] == 'match' and result['kind'] == 'procedure'
    path = context[0] / 'preferences.json'
    assert (path.read_bytes() if path.exists() else None) == before
    import master_file
    assert master_file.read() == ''
    bad = request({'operation': 'accept-procedure-offer', 'selection': {
        **body['args']['selection'], 'reply_to_message_id': 'other-question'}}, operation='preview', text='yes')
    assert call(bad, context) == {'version': 1, 'status': 'no_match'}


def test_procedure_exact_clear_recovery_preserves_newer_and_second_event_refuses(context, monkeypatch):
    body = procedure_offer(context, monkeypatch)
    import pending_offer
    import master_file
    clear = pending_offer.clear_offer
    monkeypatch.setattr(pending_offer, 'clear_offer', lambda **kwargs: (_ for _ in ()).throw(OSError('cleanup unavailable')))
    saved = call(body, context)
    assert saved['status'] == 'committed'
    second = request(body['args'], event='f'*64, text='Yes.')
    assert call(second, context)['code'] == 'target_unavailable'
    text = master_file.read()
    monkeypatch.setattr(pending_offer, 'clear_offer', clear)
    newer = pending_offer.set_offer('meeting_prep', 'Prepare Dana?', 'Dana')
    pending_offer.activate_offer(newer, {'message_id':'newer-rule-question','target':'photon',
        'chat_id':'iMessage;-;owner','accepted_at':WHEN.timestamp()})
    context[1]['_source_authority'] = lambda: {'complete':False}
    assert call(dict(body, operation='inspect'), context) == saved
    assert pending_offer.get_offer(message_id='newer-rule-question')['offer_id'] == newer['offer_id']
    assert master_file.read() == text


def test_procedure_is_confirmed_owner_memory_not_fresh_source_read(context, monkeypatch):
    body = procedure_offer(context, monkeypatch)
    context[1]['_source_authority'] = lambda: {'complete': False}
    assert call(body, context)['status'] == 'committed'


def test_concurrent_same_procedure_event_returns_original_once(context, monkeypatch):
    body = procedure_offer(context, monkeypatch)
    with ThreadPoolExecutor(4) as executor:
        results = list(executor.map(lambda _: call(body, context), range(4)))
    assert all(result == results[0] for result in results)
    import master_file
    assert master_file.read().count('Never book me Fridays.') == 1


@pytest.mark.parametrize('field,value', [('rule','Unrecorded rule'), ('offer_id','0'*64)])
def test_mutated_procedure_original_receipt_refuses(context, monkeypatch, field, value):
    body = procedure_offer(context, monkeypatch)
    assert call(body, context)['status'] == 'committed'
    path = context[0] / 'preferences.json'
    data = json.loads(path.read_text())
    data['owner_preference_receipts'][body['event_id']]['outcome'][field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='procedure receipt outcome invalid'):
        call(dict(body, operation='inspect'), context)


@pytest.mark.parametrize('text', ['shorter pls', 'Shorter!', 'please make it shorter',
    'make this more concise, please', 'more terse', 'rewrite that', 'rephrase this pls',
    'can you make that shorter?', 'make it a little shorter', 'too long', 'tighten it up',
    'be more concise'])
def test_one_off_rewrite_cannot_be_saved_as_tone(context, text):
    body = request({'operation': 'add', 'field': 'tone_notes', 'value': text}, text=text)
    path = context[0] / 'preferences.json'
    before = path.read_bytes() if path.exists() else None
    assert call(body, context) == {'version': 1, 'status': 'rejected', 'code': 'owner_span_unavailable'}
    assert call(dict(body, operation='inspect'), context)['status'] == 'not_found'
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize('text', ['Always make replies shorter', 'From now on be more terse',
    'Save this tone preference: shorter pls', 'Tone: Keep it terse'])
def test_explicit_ongoing_tone_still_saves(context, text):
    body = request({'operation': 'add', 'field': 'tone_notes', 'value': text}, text=text)
    assert call(body, context)['status'] == 'committed'
    assert text in tool._preferences().load_explicit()['tone_notes']


def test_old_one_off_tone_receipt_remains_inspectable_without_reapplying(context):
    body = request({'operation': 'add', 'field': 'tone_notes', 'value': 'shorter pls'}, text='shorter pls')
    data, policy, activation = context
    policy.update(SOTTO_DATA=str(data), SOTTO_RUNTIME_PROFILE='managed-photon-fixed-five-v1')
    proof = tool.validate_tool_admission(body, operations=('apply',), kind='owner_preference_tool',
        domain='sotto.owner-preference-tool.v1', bearer=capability.derive(ROOT, IDENTITY), root=ROOT,
        policy=policy, data_root=data, capability=capability, activation=activation)
    writer = tool._preferences()
    original = writer.admitted_owner_preference(proof)  # Receipt made by the previous version.
    path = data / 'preferences.json'
    saved = json.loads(path.read_text())
    saved['explicit']['tone_notes'] = []  # Owner subsequently removed the note.
    path.write_text(json.dumps(saved))
    for operation in ('inspect', 'apply'):
        result = call(dict(body, operation=operation), context)
        assert result['status'] == 'committed' and result['outcome'] == original
        assert writer.load_explicit()['tone_notes'] == []
