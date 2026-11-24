"""Proved composition revisions reach the accepted scheduled-text dispatch."""

import pytest

import receiver as rec
from test_protected_brief_detail import FakeProcess, _fixture


@pytest.mark.parametrize('edition', ['morning', 'evening'])
@pytest.mark.parametrize('change', ['none', 'legacy', 'source', 'route', 'body', 'queued_source', 'queued_route'])
def test_scheduled_text_brief_forwards_only_proven_composition_revision(tmp_path, monkeypatch, edition, change):
    _, route, state = _fixture(tmp_path, monkeypatch)
    monkeypatch.setenv('SOTTO_VISUAL_BRIEFS', '0')
    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *_args, **_kwargs: FakeProcess())
    label = f'cron:sotto-{edition}-brief'
    job = {'id': 'text-brief', 'kind': 'run', 'owner': 'worker', 'payload': {
        'runner': ['/fixture-runner'], 'prompt': '{}', 'label': label}}
    result = rec._execute_work(job)
    assert result['source_revision_at_composition'] == 'a' * 64
    assert result['route_digest_at_composition'] == route

    sent = []
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda body, target, *_args, **context: (
        sent.append((body, target, context)) is None, '', {'acceptance': 'accepted', 'message_id': 'text-provider'}))
    proof = {key: result[key] for key in (
        'source_revision_at_composition', 'route_digest_at_composition', 'source_text_sha256')}
    if change == 'legacy':
        proof = {}
    if change == 'source':
        state['revision'] = 'b' * 64
    if change == 'route':
        monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: 'c' * 64)
    if change == 'body':
        result['text'] += '\nChanged after composition.'
    if change in ('queued_source', 'queued_route'):
        ready = [False]
        monkeypatch.setitem(rec.OUTBOX.HOOKS, 'delivery_ready', lambda _payload: ready[0])
        assert not rec._deliver_text(result['text'], label, run_id=job['id'], **proof)
        assert not sent
        if change == 'queued_source':
            state['revision'] = 'b' * 64
        else:
            monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: 'c' * 64)
        ready[0] = True
        rec.OUTBOX.drain()
    else:
        assert rec._deliver_text(result['text'], label, run_id=job['id'], **proof)
    assert sent == [(rec._delivery_body(result['text']), 'photon',
                     {'source_revision': 'a' * 64} if change == 'none' else {})]
