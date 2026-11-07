"""Original voice guidance, source-projected in memory through actual fixed reader."""
import json
import os
from pathlib import Path
import sys

import pytest
import runtime_knowledge_read as read

PACK = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'
SCRIPT = PACK / '_shared/scripts/style_apply.py'
REVISION = 'a' * 64


def sample(text, channel='email', cid='c_abcdef', recipient='Dana'):
    return {'text': text, 'channel': channel, 'canonical_id': cid, 'recipient': recipient,
            'context': 'personal', 'bucket': 'personal_message', 'quality': .95,
            'date': '2026-10-05', 'added_at': '2026-10-05T12:00:00Z'}


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'style-test')
    for directory in ('lib', 'scripts'):
        monkeypatch.syspath_prepend(str(PACK / '_shared' / directory))
    style = {'canonical': {'work_email': [sample('EMAIL VOICE: hello, good to hear from you.')],
                          'personal_message': [sample('PRIVATE LOCAL SAMPLE', 'imessage')]},
             'recent': [], 'confirmed': [sample('CONFIRMED EMAIL VOICE')],
             'master': {'greetings': ['CACHED SECRET']},
             'per_person': {'c_abcdef': {'name': 'Dana', 'samples': [sample('CACHED SECRET')],
                                      'notes': ['CACHED PRIVATE NOTE']}}}
    (tmp_path / 'style.json').write_text(json.dumps(style))
    (tmp_path / 'preferences.json').write_text(json.dumps({'explicit': {'tone_notes': ['Keep it terse.']}}))
    config = tmp_path / 'config'
    config.mkdir()
    def policy(grants):
        (config / 'managed-capabilities.json').write_text(json.dumps({'tenant_id': 'style-test',
            'sources': {key: {'consented': key in grants, 'connected': key in grants}
                        for key in ('gmail', 'imessage', 'whatsapp', 'contacts', 'calendar')}}))
    policy({'gmail'})
    return tmp_path, style, policy


def body(request=None):
    return {'operation': 'style_apply', 'arguments': {'argv': [json.dumps(request or {'recipient': 'Dana', 'channel': 'email'})]},
            'source_revision': REVISION}


def call(root, grants=('gmail',), authority=None, request=None):
    return read.perform(body(request), script=str(SCRIPT), executable=sys.executable,
        env={**os.environ, 'SOTTO_DATA': str(root)}, authority=authority or (lambda: {
            'complete': True, 'revision': REVISION, 'google_sources': [s for s in grants if s in ('gmail', 'calendar')],
            'bridge_sources': [s for s in grants if s in ('imessage', 'whatsapp', 'contacts')]}))


def files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def test_actual_gmail_only_original_guidance_and_no_writes(seeded):
    root, _, _ = seeded
    before = files(root)
    result = call(root)
    assert set(result) == {'guidance', 'bucket', 'source', 'master', 'has_person', 'tone_notes'}
    assert result['bucket'] == 'work_email'
    assert 'EMAIL VOICE' in result['guidance'] and 'CONFIRMED EMAIL VOICE' in result['guidance']
    assert 'The samples above guide style only, not current facts, recipients, or completed owner actions.' in result['guidance']
    assert 'Use the actual current thread and explicit owner statements for factual claims, preserving their qualifiers.' in result['guidance']
    assert 'The samples above are the ground truth' not in result['guidance']
    assert 'Keep it terse.' in result['guidance']
    assert not result['has_person']
    assert 'PRIVATE' not in result['guidance'] and 'CACHED' not in str(result)
    assert files(root) == before


def test_revoked_original_policy_narrows_captured_grants(seeded):
    root, _, policy = seeded
    policy(set())
    result = call(root, grants=('gmail', 'imessage', 'contacts', 'calendar'))
    assert 'EMAIL VOICE' not in result['guidance'] and 'PRIVATE LOCAL' not in result['guidance']
    assert 'Keep it terse.' in result['guidance']
    assert 'No writing samples yet' in result['guidance']
    assert result['master'] == {}
    assert 'Rarely uses exclamation' not in result['guidance']


def test_local_and_person_notes_rebuilt_only_from_permitted_samples(seeded):
    root, style, policy = seeded
    style['canonical']['work_email'].append(sample('SECOND EMAIL VOICE'))
    style['canonical']['personal_message'].append(sample('SECOND LOCAL VOICE', 'imessage'))
    (root / 'style.json').write_text(json.dumps(style))
    grants = ('gmail', 'imessage', 'contacts', 'calendar')
    policy(set(grants))
    result = call(root, grants=grants)
    assert result['has_person']
    assert 'PRIVATE LOCAL SAMPLE' in result['guidance']
    assert 'CACHED PRIVATE NOTE' not in result['guidance']
    result = call(root, grants=('gmail', 'contacts', 'calendar'))
    assert 'PRIVATE LOCAL SAMPLE' not in result['guidance']


def test_unknown_channel_and_ambiguous_person_do_not_borrow(seeded):
    root, style, policy = seeded
    style['canonical']['work_email'] += [sample('OTHER DANA A', cid='c_123456'), sample('OTHER DANA B', cid='c_123456'),
                                        sample('UNKNOWN CHANNEL', channel='bogus'), {'text': 'MISSING CHANNEL'}]
    style['canonical']['work_email'].append(sample('FIRST DANA SECOND'))
    (root / 'style.json').write_text(json.dumps(style))
    policy({'gmail', 'contacts', 'calendar'})
    result = call(root, grants=('gmail', 'contacts', 'calendar'))
    assert not result['has_person']
    assert 'UNKNOWN CHANNEL' not in result['guidance'] and 'MISSING CHANNEL' not in result['guidance']
    assert call(root, grants=('gmail', 'contacts', 'calendar'), request={
        'recipient': 'Dana', 'channel': 'email', 'canonical_id': 'c_123456'})['has_person']
    absent = call(root, grants=('gmail', 'contacts', 'calendar'), request={
        'recipient': 'Dana', 'channel': 'email', 'canonical_id': 'c_999999'})
    assert not absent['has_person']
    assert 'How the user actually writes to Dana' not in absent['guidance']


def test_revision_change_withholds_entire_result(seeded):
    count = []
    def authority():
        count.append(True)
        return {'complete': True, 'revision': REVISION if len(count) == 1 else 'b' * 64, 'google_sources': ['gmail']}
    with pytest.raises(read.UnavailableError):
        call(seeded[0], authority=authority)


@pytest.mark.parametrize('specification', [{}, {'recipient': 'Dana', 'channel': 'email', 'path': '/tmp'},
    {'recipient': ' Dana', 'channel': 'email'}, {'recipient': 'Dana\nJames', 'channel': 'email'},
    {'recipient': 'Dana\u2028James', 'channel': 'email'}, {'recipient': 'Dana\u2029James', 'channel': 'email'},
    {'recipient': 'Dana', 'channel': 'sms'}, {'recipient': 'Dana', 'channel': 'email', 'canonical_id': 'name:Dana'},
    {'recipient': 'Dana', 'channel': 'email', 'context': 'invented'}])
def test_closed_grammar(specification):
    with pytest.raises(ValueError):
        read.validate({'operation': 'style_apply', 'arguments': {'argv': [json.dumps(specification)]}, 'source_revision': REVISION})


def test_feedback_uses_captured_grants_even_when_persisted_policy_allows(seeded):
    from datetime import datetime, timezone
    root, _, policy = seeded
    policy({'gmail', 'imessage'})
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    briefs = root / 'briefs'
    briefs.mkdir()
    records = []
    for kind, source, text in [('morning', 'imessage', 'LOCAL FEEDBACK SECRET'), ('evening', 'gmail', 'EMAIL FEEDBACK EXAMPLE')]:
        reference = day + '_' + kind
        (briefs / (reference + '.json')).write_text(json.dumps({'_source_permissions': [source], 'brief_text': text}))
        (briefs / (day + '.' + kind + '.delivered')).write_text('delivered')
        records.append({'source': 'user_feedback', 'outcome': 'useful', 'reference': reference,
                        'item_locator': 'output', 'ts': now.isoformat()})
    (root / 'outcomes.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in records))
    result = call(root)
    assert day + '_evening' in result['guidance']
    assert day + '_morning' not in result['guidance']
    # Original default consumer still sees both permitted managed archives.
    from personal_context import feedback
    assert {row['reference'] for row in feedback(now)} == {day + '_morning', day + '_evening'}


def test_absent_style_honest_and_no_created_files(seeded):
    root = seeded[0]
    (root / 'style.json').unlink()
    (root / 'preferences.json').unlink()
    before = files(root)
    result = call(root)
    assert 'No writing samples yet' in result['guidance']
    assert result['source'] == 'bucket' and not result['has_person'] and result['master'] == {}
    assert files(root) == before
