"""Only events that can reach semantic classification need retained conversation context."""
from datetime import datetime, timezone
import json

import pytest

import personal_context
import triage_event as te


NOW = datetime(2026, 8, 6, 10, 5, tzinfo=timezone.utc)
CLASSES = {'calendar_change': 'calendar_change', 'meeting_end': 'post_meeting', 'proactive': 'intention'}


def event(source):
    return {'source': source, 'rowid': source, 'timestamp': '2026-08-06T10:00:00Z',
            'start': '2026-08-06T09:00:00Z' if source == 'meeting_end' else '2026-08-06T11:00:00Z',
            'summary': 'Planning', 'text': 'Dana moved the planning call.', 'who': 'Dana Reed',
            'attendees': [{'name': 'Dana Reed', 'email': 'dana@example.test'}],
            'kind': 'intention', 'title': 'Call Dana'}


@pytest.fixture
def daytime(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_QUIET_START', '0')
    monkeypatch.setenv('SOTTO_QUIET_END', '0')
    return tmp_path


@pytest.mark.parametrize('source', sorted(te.NON_EVIDENCE_SOURCES))
def test_synthetic_source_is_classified_before_tier_one(daytime, monkeypatch, source):
    def unexpected(*args, **kwargs):
        pytest.fail('Synthetic triage attempted a Tier-1 history read or model call')

    monkeypatch.setattr(personal_context, 'conversation_snapshot', unexpected)
    monkeypatch.setattr(te, '_classify_tier1', unexpected)
    result = te.triage({'events': [event(source)]}, now_local=NOW, now_utc=NOW)
    assert result['verdict'] == 'agent'
    assert result['bundle']['events'][0]['class'] == CLASSES[source]


def test_mixed_batch_keeps_retained_context_for_inbound_message(daytime, monkeypatch):
    handle = '+12065550143'
    inbound = {'source': 'imessage', 'rowid': 'incoming', 'handle': handle,
               'text': 'Can you approve the revised budget?', 'timestamp': NOW.isoformat()}
    prior = {'id': 'previous', 'handle': handle, 'text': 'Here is the revised budget.',
             'timestamp': '2026-08-06T10:00:00Z'}
    knowledge = daytime / 'knowledge'
    knowledge.mkdir()
    (knowledge / 'last_local_snapshot.json').write_text(json.dumps({'local': {
        'contacts': [{'name': 'Dana Reed', 'phones': [handle]}], 'imessage': [prior]}}))
    monkeypatch.setattr('source_context.allowed', lambda source: True)
    judgments = []

    def classify(row, one_liner):
        judgments.append(row['rowid'])
        assert any(previous.get('id') == prior['id'] for previous, *_ in row['_sotto_conversation_rows'])
        return 'queue', 'actionable', 'budget approval'

    monkeypatch.setattr(te, '_classify_tier1', classify)
    result = te.triage({'events': [event('calendar_change'), inbound]}, now_local=NOW, now_utc=NOW)
    assert judgments == ['incoming']
    assert result['bundle']['events'][0]['class'] == 'calendar_change'
