"""Delivered-message windows and reply-context proof for every Photon delivery kind."""
import hashlib
import json

import pytest

from test_delivered_task_completion import conversation
from test_runtime_owner_loop_tool import call, context, request, rows
from test_runtime_source_loop_tool import actual_writer_module

__all__ = ['context', 'actual_writer_module']
HOUR = 3600


def _aged(path, seconds):
    doc = json.loads(path.read_text())
    doc['rows'][0]['receipt']['accepted_at'] = request()['admitted_at_ms'] / 1000 - seconds
    path.write_text(json.dumps(doc))


def _quoted(body):
    body['args']['delivery'] = dict(body['args']['delivery'], quoted=True)
    return request(body['args'], text=body['text'])


def test_unthreaded_reply_seventy_minutes_later_closes_the_named_task(context):
    reply, path = conversation(context, 'nudge')
    _aged(path, 70 * 60)
    assert call(reply('Done', 'Done'), context)['status'] == 'committed'
    assert {r['anchor_key'] for r in rows() if r['status'] == 'resolved'} == {'cleanup-3'}


def test_unthreaded_reply_past_twelve_hours_selects_nothing(context):
    reply, path = conversation(context, 'nudge')
    _aged(path, 12 * HOUR + 60)
    call(reply('Done', 'Done'), context)
    assert all(r['status'] == 'open' for r in rows())


def test_quoted_reply_two_days_later_closes_its_task_while_the_version_matches(context):
    reply, path = conversation(context, 'nudge')
    _aged(path, 2 * 24 * HOUR)
    assert call(_quoted(reply('Done', 'Done')), context)['status'] == 'committed'
    assert {r['anchor_key'] for r in rows() if r['status'] == 'resolved'} == {'cleanup-3'}


def test_quoted_reply_past_the_receipt_window_or_after_authority_changed_selects_nothing(context):
    reply, path = conversation(context, 'nudge')
    _aged(path, 7 * 24 * HOUR + 60)
    call(_quoted(reply('Done', 'Done')), context)
    _aged(path, 2 * 24 * HOUR)
    changed = _quoted(reply('Done', 'Done'))
    changed['args']['source_revision'] = 'c' * 64
    call(request(changed['args'], text='Done'), context)
    assert all(r['status'] == 'open' for r in rows())


@pytest.mark.parametrize('quoted', [False, 'yes', None])
def test_only_a_literal_quote_marker_is_accepted(quoted):
    from cleanup_selection import validate_resolve_many_args
    args = dict(operation='resolve_many', selection_mode='delivered', allow_partial=True, source_revision='a' * 64,
                items=[dict(name='Done', correction='Done')],
                delivery=dict(dispatch_id='f' * 16, message_id='m', route_digest='b' * 64, payload_hash='c' * 64))
    validate_resolve_many_args(dict(args, delivery=dict(args['delivery'], quoted=True)))
    with pytest.raises(ValueError):
        validate_resolve_many_args(dict(args, delivery=dict(args['delivery'], quoted=quoted)))


def _sent(monkeypatch):
    import receiver
    sent = []
    def send(body, _target, _key, _attempt, **context):
        sent.append((body, context))
        return True, '', {'acceptance': 'accepted', 'message_id': 'provider'}
    monkeypatch.setitem(receiver.OUTBOX.HOOKS, 'send', send)
    return sent


@pytest.mark.parametrize('label', ['event', 'cron:sotto-midday-digest', 'cron:sotto-proactive', 'cron:sotto-morning-brief'])
def test_composed_photon_text_carries_its_composing_revision(tmp_path, monkeypatch, label):
    import receiver
    from test_protected_brief_detail import BRIEF, _fixture
    _, route, state = _fixture(tmp_path, monkeypatch)
    monkeypatch.setenv('SOTTO_VISUAL_BRIEFS', '0')
    sent = _sent(monkeypatch)
    body, effects = 'Person 3 wrote about Obligation 3.', []
    if label.endswith('morning-brief'):
        # A text brief with an offer and no task bindings still carries reply context.
        body = receiver._delivery_body(BRIEF) + '\n\nWant a draft reply?'
        effects = [{'kind': 'pending_offer', 'offer': {'kind': 'handoff', 'question': 'Want a draft reply?'}}]
        monkeypatch.setattr(receiver, '_candidate_retained_route_proof', lambda: {
            'route_digest': route, 'owner_route': {'chat_id': 'owner-chat'}})
    assert receiver._deliver_text(body, label, run_id='e' * 16, effects=effects,
        source_revision_at_composition=state['revision'], route_digest_at_composition=route,
        source_text_sha256=hashlib.sha256(body.encode()).hexdigest())
    assert sent == [(body, {'source_revision': state['revision']})]


@pytest.mark.parametrize('change', ['source', 'route', 'none'])
def test_receiver_written_notice_records_the_revision_current_at_handoff(tmp_path, monkeypatch, change):
    import receiver
    from test_protected_brief_detail import _fixture
    _, route, state = _fixture(tmp_path, monkeypatch)
    sent = _sent(monkeypatch)
    if change == 'source':
        state['revision'] = 'b' * 64  # the notice was composed earlier; only the handoff revision counts
    if change == 'route':
        monkeypatch.setattr(receiver, '_candidate_retained_route_digest', iter(['c' * 64, route]).__next__)
    text = 'Connect a source.'
    assert receiver._deliver_text(text, 'status:no-sources', run_id='9' * 16)
    expected = {} if change == 'route' else {'source_revision': state['revision']}
    assert sent == [(text, expected)]


def test_telegram_notice_carries_no_reply_context(tmp_path, monkeypatch):
    import receiver
    from test_protected_brief_detail import _fixture
    _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(receiver, '_deliver_target', lambda: 'telegram')
    sent = _sent(monkeypatch)
    assert receiver._deliver_text('Connect a source.', 'status:no-sources', run_id='9' * 16)
    assert sent == [('Connect a source.', {})]


def test_offer_route_capture_uses_protected_owner_route(monkeypatch):
    import receiver as rec
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'photon')
    payload = {'effects': [{'kind': 'pending_offer'}]}
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', lambda: {
        'route_digest': 'a' * 64, 'owner_route': {'chat_id': 'iMessage;-;+14155550100'}})
    assert rec._capture_offer_route(payload) == {
        'route_digest': 'a' * 64, 'chat_id': 'iMessage;-;+14155550100'}
    for field in ('prep_route_digest', 'routine_route_digest'):
        assert rec._capture_offer_route({**payload, field: 'b' * 64}) is None
        assert rec._capture_offer_route({**payload, field: 'a' * 64}) is not None
    for label in ('cron:sotto-morning-brief', 'event', 'cron:sotto-proactive'):
        row = {**payload, 'label': label}
        assert rec._delivery_valid({**row, 'prep_route_digest': 'a' * 64})
        assert not rec._delivery_valid({**row, 'prep_route_digest': 'b' * 64}), label
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', lambda: None)
    assert rec._delivery_valid({**row, 'prep_route_digest': 'a' * 64})
    assert rec._capture_offer_route(payload) is None
    assert rec._capture_offer_route({'effects': []}) == {}
