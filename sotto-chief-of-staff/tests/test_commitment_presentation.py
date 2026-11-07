"""Exact reviewed prose and bounded text presentation of actual saved obligations."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

import compose_brief as cb
from delivery_effects import loop_version
import visual_brief

NOW = datetime(2026, 10, 21, 9, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-hosted')
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')


def test_muted_historical_person_is_withheld_from_prompt_and_critic(monkeypatch):
    monkeypatch.setattr(cb, 'explicit_prefs', lambda: {'mute_people': ['Sam Synthetic'],
        'mute_senders': [], 'mute_sections': [], 'tone_notes': []})
    def historical(handle, text, mid):
        return {'handle': handle, 'text': text, 'source_id': mid, 'timestamp': '2026-10-19 08:00:00',
                'is_from_me': False, 'is_group_chat': False, 'chat_guid': None,
                '_loop_context': True, '_loop_context_sources': ['gmail', 'imessage']}
    private = historical('+14155550193', 'MUTED-HISTORICAL-BODY', 'muted-old')
    visible = historical('+14155550194', 'VISIBLE-HISTORICAL-BODY', 'visible-old')
    inputs = {'now': NOW.isoformat(), 'local': {'contacts': [
        {'name': 'Sam Synthetic', 'phones': [private['handle']]},
        {'name': 'Robin Green', 'phones': [visible['handle']]}], 'imessage': [private, visible]}}
    before = deepcopy(inputs)
    prompt = cb.build_prompt('{{imessage_needs_response}}', inputs)
    manifest = cb.build_data_manifest(inputs)
    for text in (prompt, json.dumps(manifest)):
        assert private['text'] not in text and private['source_id'] not in text
        assert visible['text'] in text and visible['source_id'] in text
    assert inputs == before


@pytest.mark.parametrize('other_issue', [None, 'semantic', 'unknown_validator'])
def test_reorder_rewrite_keeps_reviewed_draft_only_for_pure_reorder(monkeypatch, other_issue):
    row = {'anchor_key': 'checklist', 'status': 'open', 'contact_name': 'Maya Journey',
        'contact_identifier': 'maya.journey@example.test', 'ask': 'Send the launch checklist tomorrow.',
        'created_at': '2026-10-18T09:00:00Z', 'deadline': '2026-10-19'}
    line = ('- **Maya Journey**: Send her the rollout checklist, due October 19. '
            '<!--id:maya.journey@example.test|ch:email--><!--loop:checklist-->')
    changed = line.replace('Send her', 'Share her')
    draft = {'brief_markdown': '## Should Handle Today\n' + line, 'actions': [], 'loop_updates': []}
    monkeypatch.setattr(cb, 'build_data_manifest', lambda inputs: {})
    monkeypatch.setattr(cb, '_task_mention_review', lambda out, inputs: {'approved': {
        'proposal_id': 'approved', 'loop_id': 'checklist', 'loop_version': loop_version(row),
        'identity_proven': True, 'displayed_line': line}})
    calls = []
    def model(prompt, inputs, **kwargs):
        calls.append(inputs)
        if inputs.get('_critic'):
            return json.dumps({'patches': [{'type': 'fix_attribution' if other_issue == 'semantic' else 'reorder',
                'detail': 'Correct the requested issue.', 'severity': 'moderate'}], 'score': 85,
                'summary': 'Review complete.', 'mention_verdicts': [{'proposal_id': 'approved', 'verdict': 'approve'}],
                'loop_verdicts': []})
        return json.dumps({'brief_markdown': '## Should Handle Today\n' + changed,
                          'actions': [], 'loop_updates': []})
    violations = ['unknown-validator: another required change'] if other_issue == 'unknown_validator' else []
    out = cb.critique_and_revise(draft, {}, llm=model, violations=violations, source_prompt='source fixture')
    assert len(calls) == 2
    if other_issue is None:
        assert out['brief_markdown'].splitlines()[-1] == line
        assert out['_critic']['revision_rejected'] == 'reviewed_line_changed'
        assert out['_critic']['revision_preserved_draft'] is True
        assert cb._reviewed_mention_keys(out, [row]) == {'checklist'}
        cb._append_still_open(out, [row], NOW, '2026-10-21')
        assert '## Still open' not in out['brief_markdown']
    else:
        assert changed in out['brief_markdown']
        assert not out['_critic'].get('revision_preserved_draft')
        assert cb._reviewed_mention_keys(out, [row]) == set()


def quiet_rows(count=2):
    return [{'anchor_key': f'quiet-{i}', 'contact_name': f'Robin Person{chr(65 + i)}',
        'status': 'open', 'created_at': '2026-10-18T09:00:00Z', 'deadline': None,
        'summary': 'Review the planning notes and decide whether to attend the workshop next month.'}
        for i in range(count)]


def test_short_text_brief_shows_two_quiet_asks_without_inventing_urgency():
    rows = quiet_rows()
    before = deepcopy(rows)
    out = cb._append_still_open({'brief_markdown': 'Morning update.\n\nNo new communication needs a reply.'},
                               rows, NOW, '2026-10-21')
    assert visual_brief.build(out['brief_markdown']) is None
    cb._expand_quiet_loops(out, rows, NOW, '2026-10-21')
    assert 'other open loop' not in out['brief_markdown']
    for row in rows:
        assert row['contact_name'] in out['brief_markdown'] and row['summary'] in out['brief_markdown']
    assert 'Needs Attention' not in out['brief_markdown'] and 'overdue' not in out['brief_markdown']
    assert cb._represented_loop_rows(rows, out['brief_markdown']) == rows
    assert rows == before


def test_short_text_expansion_stays_bounded_and_card_overflow_stays_held(monkeypatch):
    from chatfmt import to_imessage
    rows = quiet_rows(20)
    out = cb._append_still_open({'brief_markdown': 'Morning update.\n\nNo new communication needs a reply.'},
                               rows, NOW, '2026-10-21')
    cb._expand_quiet_loops(out, rows, NOW, '2026-10-21')
    shown = cb._represented_loop_rows(rows, out['brief_markdown'])
    assert 0 < len(shown) < len(rows)
    assert f'{len(rows) - len(shown)} other open loops' in out['brief_markdown']
    assert len(to_imessage(out['brief_markdown'], normalize_style=False).split()) <= visual_brief.MAX_WORDS
    from test_visual_brief import BRIEF
    card_out = cb._append_still_open({'brief_markdown': BRIEF}, rows[:2], NOW, '2026-10-21')
    original = card_out['brief_markdown']
    assert visual_brief.build(original) is not None
    monkeypatch.setattr(visual_brief, 'fits', lambda *args, **kwargs: False)
    cb._expand_quiet_loops(card_out, rows[:2], NOW, '2026-10-21')
    assert card_out['brief_markdown'] == original
