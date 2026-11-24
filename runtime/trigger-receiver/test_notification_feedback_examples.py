"""Current, receipt-bound Gmail examples in a fresh brief."""

import json
from datetime import timedelta

import pytest
from test_owner_usefulness import notification_rating, context

__all__ = ["context", "notification_rating"]


def test_next_brief_reuses_rating_only_for_its_current_original(notification_rating, context, monkeypatch):
    from test_runtime_owner_loop_close import WHEN
    rate, _, _, outbox, route, _, notification, source_id = notification_rating
    assert rate('8' * 64)['status'] == 'committed'
    import compose_brief as cb
    monkeypatch.setenv('SOTTO_BRIEF_FEEDBACK_PROOF', json.dumps({'route_digest': route, 'source_revision': 'c' * 64}))
    log = context[0] / 'outcomes.jsonl'
    rated = json.loads(log.read_text())
    rated['ts'] = WHEN.isoformat()
    log.write_text(json.dumps(rated) + '\n')
    original = {'id': source_id, 'from': 'Cedar <cedar@example.test>', 'subject': 'Weekly update',
                'labelIds': ['INBOX'], 'snippet': 'New garden photos.'}
    sibling = {**original, 'id': 'other-message', 'subject': 'Please sign the lease', 'snippet': 'Due today.'}
    def prompt(emails, days=1):
        return cb.build_prompt('{{master_context}}\n{{gmail}}', {'type': 'morning', 'local': {},
            'now': (WHEN + timedelta(days=days)).isoformat(), 'google': {'emails': emails}})
    def rated_section(rendered):
        return rendered.split('USER FEEDBACK ON SPECIFIC PAST OUTPUTS')[-1].split('END USER FEEDBACK')[0]
    rendered = prompt([original, sibling])
    assert 'not_useful' in rendered
    assert source_id in rated_section(rendered) and sibling['id'] not in rated_section(rendered)
    assert sibling['subject'] in rendered and notification not in rendered
    assert 'not_useful' not in prompt([sibling])
    assert 'not_useful' not in prompt([original], days=44)
    assert 'not_useful' not in prompt([original], days=-1)
    with monkeypatch.context() as changed:
        saved_prefs = cb.explicit_prefs()
        changed.setattr(cb, 'explicit_prefs', lambda: {**saved_prefs, 'mute_people': ['Cedar']})
        assert 'not_useful' not in prompt([original])
    from personal_context import feedback as examples
    assert not examples(WHEN, gmail_ids=[source_id], permitted=lambda _source: False)
    log.write_text(json.dumps({k: v for k, v in rated.items() if k != 'archive_version'}) + '\n')
    assert 'not_useful' not in prompt([original])
    log.write_text(json.dumps(rated) + '\n')
    monkeypatch.delenv('SOTTO_BRIEF_FEEDBACK_PROOF')
    assert 'not_useful' not in prompt([original])
    monkeypatch.setenv('SOTTO_BRIEF_FEEDBACK_PROOF', json.dumps({'route_digest': route, 'source_revision': 'f' * 64}))
    assert 'not_useful' not in prompt([original])
    monkeypatch.setenv('SOTTO_BRIEF_FEEDBACK_PROOF', json.dumps({'route_digest': 'f' * 64, 'source_revision': 'c' * 64}))
    assert 'not_useful' not in prompt([original])
    monkeypatch.setenv('SOTTO_BRIEF_FEEDBACK_PROOF', json.dumps({'route_digest': route, 'source_revision': 'c' * 64}))
    saved = json.loads(outbox.read_text())
    outbox.write_text(json.dumps({'rows': saved['rows'] * 2}))
    assert 'not_useful' not in prompt([original])
    old_hash = saved['rows'][0]['receipt']['payload_hash']
    saved['rows'][0]['receipt']['payload_hash'] = '1' * 64
    outbox.write_text(json.dumps(saved))
    assert 'not_useful' not in prompt([original])
    saved['rows'][0]['receipt']['payload_hash'] = old_hash
    saved['rows'][0]['status'] = 'superseded'
    outbox.write_text(json.dumps(saved))
    assert 'not_useful' not in prompt([original])


def test_explicit_rerating_can_bind_a_legacy_rating(notification_rating, context):
    rate, *_ = notification_rating
    assert rate('8' * 64)['status'] == 'committed'
    log = context[0] / 'outcomes.jsonl'
    old = json.loads(log.read_text())
    version = old.pop('archive_version')
    log.write_text(json.dumps(old) + '\n')
    assert rate('9' * 64)['status'] == 'committed'
    assert json.loads(log.read_text().splitlines()[-1]).get('archive_version') == version


def test_bound_rating_and_brief_examples_share_the_existing_cap(notification_rating, context, monkeypatch):
    from test_runtime_owner_loop_close import WHEN
    rate, _, _, _, route, _, _, source_id = notification_rating
    assert rate('8' * 64)['status'] == 'committed'
    import personal_context as pc
    monkeypatch.setenv('SOTTO_BRIEF_FEEDBACK_PROOF', json.dumps({'route_digest': route, 'source_revision': 'c' * 64}))
    log = context[0] / 'outcomes.jsonl'
    notification = {**json.loads(log.read_text()), 'ts': WHEN.isoformat()}
    briefs = [{'outcome': 'useful', 'source': 'user_feedback', 'ts': WHEN.isoformat(), 'reference': f'brief-{i}',
               'archive_version': 'same', 'item_locator': 'output'} for i in range(pc.FEEDBACK_LIMIT)]
    monkeypatch.setattr(pc, 'accepted_outputs', lambda **_: {row['reference']: {'archive_version': 'same', 'text': 'x'}
                                                             for row in briefs})
    for ordered, should_include in (([notification, *briefs], False), ([*briefs, notification], True)):
        log.write_text(''.join(json.dumps(row) + '\n' for row in ordered))
        examples = pc.feedback(WHEN, gmail_ids=[source_id])
        assert len(examples) == pc.FEEDBACK_LIMIT
        assert any(row.get('previous_notification_about_gmail_message_id') == source_id for row in examples) == should_include


@pytest.mark.parametrize('revision,expected', [('c' * 64, True), (None, False)])
def test_morning_worker_passes_only_the_current_proof(tmp_path, monkeypatch, revision, expected):
    import sys
    import receiver as receiver_module
    from pathlib import Path
    monkeypatch.setattr(receiver_module, 'DATA', str(tmp_path))
    monkeypatch.setattr(receiver_module, '_validate_fixed_work', lambda *_: None)
    monkeypatch.setattr(receiver_module, '_record_delivery', lambda *_a, **_kw: None)
    monkeypatch.setattr(receiver_module, '_deliver_target', lambda: 'photon')
    route = 'a' * 64
    monkeypatch.setattr(receiver_module, '_candidate_retained_route_digest', lambda: route)
    monkeypatch.setattr(receiver_module, '_runtime_source_authority', lambda: {'complete': revision is not None, 'revision': revision})
    monkeypatch.setenv('SOTTO_BRIEF_FEEDBACK_PROOF', json.dumps({'source_revision': 'f' * 64, 'route_digest': 'f' * 64}))
    monkeypatch.setenv('SOTTO_SKILL_COMPOSITION_PROOF', json.dumps({'source_revision': 'f' * 64, 'route_digest': 'f' * 64}))
    render = receiver_module._load_shared_lib('render_local')
    import source_context
    import os
    from unittest.mock import patch
    monkeypatch.setattr(source_context, 'allowed', lambda _: True)
    call = {'phone': '+14155550109', 'timestamp': '2026-06-24 08:00:00',
            'is_outgoing': True, 'is_answered': False, 'call_type': 'phone', 'duration_seconds': 125}
    class ChildStartedError(Exception):
        pass
    def spawn(_argv, **kw):
        with patch.dict(os.environ, {**kw['env'], 'SOTTO_DEPLOYMENT_MODE': 'managed'}, clear=True):
            assert render.call_evidence(call, 'calls', permission_scope='d' * 24) == ('', '')
        assert 'SOTTO_SKILL_COMPOSITION_PROOF' not in kw['env']
        assert json.loads(kw['env'].get('SOTTO_BRIEF_FEEDBACK_PROOF', '{}')) == (
            {'source_revision': revision, 'route_digest': route} if expected else {})
        raise ChildStartedError
    monkeypatch.setattr(receiver_module.subprocess, 'Popen', spawn)
    here = Path(receiver_module.__file__).parent
    with pytest.raises(ChildStartedError):
        receiver_module._execute_work({'id': 'b' * 32, 'kind': 'run', 'payload': {
            'runner': [sys.executable, str(here / 'brief_runner.py')], 'label': 'cron:sotto-morning-brief',
            'prompt': json.dumps({'kind': 'morning', 'pack': str(here.parents[1] / 'sotto-chief-of-staff')})}})
