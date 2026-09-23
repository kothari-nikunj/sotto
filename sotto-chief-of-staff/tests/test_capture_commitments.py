"""Durable Granola commitment capture: overlap, receipt checkpoint and writer truth."""
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'capture_commitments_test', ROOT / 'followup/scripts/capture_commitments.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


@pytest.fixture(autouse=True)
def _allow_granola(monkeypatch):
    monkeypatch.setattr(capture.source_context, 'allowed', lambda source: source == 'granola')


def _meeting(notes="Alex: I'll send the deck."):
    return {'meeting_id': 'meeting-1', 'title': 'Late sync', 'start': '2026-09-16T19:00:00Z',
            'end': '2026-09-16T20:00:00Z', 'attendee_emails': ['dana@example.com'],
            'your_notes': notes}


def _receipt(root, revisions, stable_revisions=None):
    path = root / 'briefs/2026-09-17.morning.learned.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'steps': {'commitments': {'status': 'ok', 'proof': {
        'coverage': 'successful_meeting_revisions', 'meeting_revisions': revisions,
        'stable_meeting_revisions': stable_revisions or []}}}}))


def test_late_notes_capture_once_and_updated_notes_get_a_new_revision(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    calls = []
    monkeypatch.setattr(capture.compose_followup, 'build_context',
                        lambda inputs, hours: ('context', inputs['granola']))
    monkeypatch.setattr(capture.compose_followup, 'compose',
                        lambda inputs, since_hours, llm=None: calls.append((inputs, since_hours)) or {
                            'commitments': [{'meeting_id': 'meeting-1'}]})
    monkeypatch.setattr(capture.apply_commitments, 'apply',
                        lambda *_args, **_kwargs: {'written': 1, 'deduped': 0,
                                                   'skipped_terminal': 0, 'anchor_keys': ['a']})
    first = capture.capture({'meetings': [_meeting()]}, 'alex@example.com')
    assert first['written'] == 1 and first['examined'] == 1
    assert calls[0][1] == 14 * 24              # bounded overlap includes after-evening/delayed notes
    _receipt(tmp_path, first['meeting_revisions'])
    assert capture.capture({'meetings': [_meeting()]}, 'alex@example.com')['examined'] == 0
    assert len(calls) == 1                      # unchanged persisted revision spends no model call

    changed = capture.capture({'meetings': [_meeting("Alex: I'll send the revised deck.")]},
                              'alex@example.com')
    assert changed['examined'] == 1 and changed['meeting_revisions'] != first['meeting_revisions']
    assert len(calls) == 2


def test_writer_failure_never_becomes_a_successful_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(capture.compose_followup, 'build_context',
                        lambda inputs, hours: ('context', inputs['granola']))
    monkeypatch.setattr(capture.compose_followup, 'compose',
                        lambda *_args, **_kwargs: {'commitments': [{'meeting_id': 'meeting-1'}]})
    monkeypatch.setattr(capture.apply_commitments, 'apply',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('fixture')))
    result = capture.capture({'meetings': [_meeting()]}, 'alex@example.com')
    assert result['failures'][0]['error'] == 'OSError'
    assert capture.successful_revisions(str(tmp_path)) == set()


def test_poisoned_oldest_does_not_starve_later_meeting_and_partial_proof_retries_only_failure(
        tmp_path, monkeypatch):
    meetings = [{**_meeting('bad'), 'meeting_id': 'bad', 'end': '2026-09-15T10:00:00Z'},
                {**_meeting('good'), 'meeting_id': 'good', 'end': '2026-09-15T11:00:00Z'}]
    calls = []
    def extract(meeting, *_args, **_kwargs):
        calls.append(meeting['meeting_id'])
        if meeting['meeting_id'] == 'bad':
            raise ValueError('poison fixture')
        return {'meeting_revision': capture.extraction_revision(meeting),
                'ledger': {'written': 1, 'anchor_keys': ['good']}}
    monkeypatch.setattr(capture, 'extract_apply_meeting', extract)
    first = capture.capture({'meetings': meetings}, data_root=str(tmp_path))
    assert first['meeting_revisions'] == [capture.extraction_revision(meetings[1])]
    assert first['failures'][0]['meeting_revision'] == capture.extraction_revision(meetings[0])
    _receipt(tmp_path, first['meeting_revisions'])
    calls.clear()
    second = capture.capture({'meetings': meetings}, data_root=str(tmp_path))
    assert calls == ['bad'] and second['meeting_revisions'] == []


def test_three_previously_failed_old_revisions_do_not_starve_fresh_later_notes(tmp_path, monkeypatch):
    old = [{**_meeting(f'bad-{index}'), 'meeting_id': f'bad-{index}',
            'end': f'2026-09-15T0{index}:00:00Z'} for index in range(3)]
    fresh = {**_meeting('good'), 'meeting_id': 'fresh', 'end': '2026-09-16T11:00:00Z'}
    failed_revisions = [capture.extraction_revision(row) for row in old]
    path = tmp_path / 'briefs/partial.learned.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'steps': {'commitments': {'status': 'failed', 'proof': {
        'coverage': 'partial_successful_meeting_revisions', 'meeting_revisions': [],
        'failed_meeting_revisions': failed_revisions}}}}))
    calls = []
    def extract(meeting, *_args, **_kwargs):
        calls.append(meeting['meeting_id'])
        if meeting['meeting_id'].startswith('bad'):
            raise ValueError('poison')
        return {'meeting_revision': capture.extraction_revision(meeting),
                'ledger': {'written': 1, 'anchor_keys': ['fresh']}}
    monkeypatch.setattr(capture, 'extract_apply_meeting', extract)
    result = capture.capture({'meetings': [*old, fresh]}, data_root=str(tmp_path))
    assert calls[0] == 'fresh'
    assert capture.extraction_revision(fresh) in result['meeting_revisions']


def test_failed_revisions_rotate_by_oldest_receipt_attempt(tmp_path, monkeypatch):
    meetings = [{**_meeting(f'bad-{index}'), 'meeting_id': f'bad-{index}',
                 'end': f'2026-09-15T0{index}:00:00Z'} for index in range(4)]
    revisions = [capture.extraction_revision(row) for row in meetings]
    briefs = tmp_path / 'briefs'
    briefs.mkdir(parents=True)
    (briefs / 'old.learned.json').write_text(json.dumps({'ts': '2026-09-16T01:00:00Z', 'steps': {
        'commitments': {'status': 'failed', 'proof': {
            'coverage': 'partial_successful_meeting_revisions', 'meeting_revisions': [],
            'failed_meeting_revisions': revisions}}}}))
    calls = []
    def fail(meeting, *_args, **_kwargs):
        calls.append(meeting['meeting_id'])
        raise ValueError('poison')
    monkeypatch.setattr(capture, 'extract_apply_meeting', fail)
    first = capture.capture({'meetings': meetings}, data_root=str(tmp_path))
    assert calls == ['bad-0', 'bad-1', 'bad-2']
    (briefs / 'new.learned.json').write_text(json.dumps({'ts': '2026-09-16T02:00:00Z', 'steps': {
        'commitments': {'status': 'failed', 'proof': {
            'coverage': 'partial_successful_meeting_revisions', 'meeting_revisions': [],
            'failed_meeting_revisions': first['failed_meeting_revisions']}}}}))
    calls.clear()
    capture.capture({'meetings': meetings}, data_root=str(tmp_path))
    assert calls[0] == 'bad-3'


def test_checkpoint_identity_changes_with_account_name_contract(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_USER_NAME', 'Alex Smith')
    monkeypatch.setattr(capture.compose_followup, 'build_context',
                        lambda inputs, hours: ('context', inputs['granola']))
    calls = []
    monkeypatch.setattr(capture.compose_followup, 'compose',
                        lambda *_args, **_kwargs: calls.append(1) or {'commitments': []})
    monkeypatch.setattr(capture.apply_commitments, 'apply', lambda *_args, **_kwargs: {
        'written': 0, 'deduped': 0, 'skipped_terminal': 0, 'anchor_keys': []})
    first = capture.capture({'meetings': [_meeting()]}, 'alex@example.com', data_root=str(tmp_path))
    _receipt(tmp_path, first['meeting_revisions'])
    assert capture.capture({'meetings': [_meeting()]}, 'alex@example.com',
                           data_root=str(tmp_path))['examined'] == 0
    monkeypatch.setenv('SOTTO_USER_NAME', 'Alexandra Smith')
    second = capture.capture({'meetings': [_meeting()]}, 'alex@example.com', data_root=str(tmp_path))
    assert second['examined'] == 1 and len(calls) == 2
    assert second['meeting_revisions'] != first['meeting_revisions']


def test_transcript_expiry_uses_stable_checkpoint_but_transcript_edits_are_new(tmp_path):
    with_transcript = _meeting("Alex: I'll send the deck.")
    with_transcript['transcript'] = "Alex: I'll send the deck."
    without_transcript = dict(with_transcript)
    without_transcript.pop('transcript')
    full = capture.extraction_revision(with_transcript)
    stable = capture.extraction_revision(with_transcript, stable=True)
    assert full != stable == capture.extraction_revision(without_transcript)
    _receipt(tmp_path, [full], [stable])
    assert capture.capture({'meetings': [without_transcript]}, data_root=str(tmp_path))['examined'] == 0
    edited = dict(with_transcript, transcript="Alex: I'll send the deck and contract.")
    assert capture.extraction_revision(edited) != full


def test_capture_replay_handles_transcript_expiry_enrichment_and_reworded_notes(
        tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_USER_NAME', 'Alex Smith')
    base = {**_meeting("Alex: I'll send the deck."),
            'attendees': [{'email': 'alex@example.com', 'name': 'Alex Smith'},
                          {'email': 'dana@example.com', 'name': 'Dana'}],
            'attendee_emails': ['alex@example.com', 'dana@example.com'],
            'transcript': "Alex: I'll send the deck."}
    monkeypatch.setattr(capture.compose_followup, 'build_context',
                        lambda inputs, hours: ('context', inputs['granola']))
    calls = []
    def compose(inputs, **_kwargs):
        meeting = inputs['granola'][0]
        calls.append(meeting)
        if 'contract' in meeting.get('transcript', ''):
            what, snippet = 'send the contract', "Alex: I'll send the contract."
        elif 'share the deck' in meeting.get('your_notes', ''):
            what, snippet = 'share the deck', 'Alex: I will share the deck.'
        else:
            what, snippet = 'send the deck', "Alex: I'll send the deck."
        return {'commitments': [{'meeting_id': 'meeting-1', 'owner': 'Alex',
            'owner_is_user': True, 'what': what, 'to_email': 'dana@example.com',
            'source_snippet': snippet}], 'drafts': [], 'followup_markdown': ''}
    monkeypatch.setattr(capture.compose_followup, 'compose', compose)

    first = capture.capture({'meetings': [base]}, 'alex@example.com', data_root=str(tmp_path))
    assert first['written'] == 1
    _receipt(tmp_path, first['meeting_revisions'], first['stable_meeting_revisions'])
    expired = dict(base)
    expired.pop('transcript')
    assert capture.capture({'meetings': [expired]}, 'alex@example.com',
                           data_root=str(tmp_path))['examined'] == 0

    enriched = {**base, 'transcript': ("Alex: I'll send the deck.\n"
                                       "Alex: I'll send the contract.")}
    second = capture.capture({'meetings': [enriched]}, 'alex@example.com', data_root=str(tmp_path))
    assert second['written'] == 1
    edited_notes = {**expired, 'your_notes': 'Alex: I will share the deck.'}
    third = capture.capture({'meetings': [edited_notes]}, 'alex@example.com', data_root=str(tmp_path))
    assert third['deduped'] == 1 and third['written'] == 0
    assert len(calls) == 3


def test_canonical_configured_email_wins_over_stale_caller_value(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'settings.json').write_text(json.dumps({'google_account_email': 'owner@example.com'}))
    monkeypatch.delenv('SOTTO_USER_EMAIL', raising=False)
    monkeypatch.setattr(capture.compose_followup, 'build_context',
                        lambda inputs, hours: ('context', inputs['granola']))
    monkeypatch.setattr(capture.compose_followup, 'compose',
                        lambda *_args, **_kwargs: {'commitments': []})
    seen = []
    monkeypatch.setattr(capture.apply_commitments, 'apply',
                        lambda _out, email, **_kwargs: seen.append(email) or {'written': 0})
    capture.extract_apply_meeting(_meeting(), 'stale@example.com', data_root=str(tmp_path))
    assert seen == ['owner@example.com']
    assert capture.extraction_revision(_meeting(), 'stale@example.com') == \
        capture.extraction_revision(_meeting(), 'another-stale@example.com')


def test_revoke_after_model_prevents_apply_and_success_cache(tmp_path, monkeypatch):
    states = iter((True, False))
    monkeypatch.setattr(capture.source_context, 'allowed', lambda _source: next(states))
    monkeypatch.setattr(capture.compose_followup, 'build_context',
                        lambda inputs, hours: ('context', inputs['granola']))
    monkeypatch.setattr(capture.compose_followup, 'compose',
                        lambda *_args, **_kwargs: {'commitments': []})
    applied = []
    monkeypatch.setattr(capture.apply_commitments, 'apply', lambda *_args, **_kwargs: applied.append(1))
    with pytest.raises(PermissionError):
        capture.extract_apply_meeting(_meeting(), 'alex@example.com', data_root=str(tmp_path))
    assert applied == []
    assert not list((tmp_path / 'events/notification-artifacts').glob('followup-*.json'))


def test_failed_receipt_does_not_suppress_retry(tmp_path):
    revision = capture.meeting_revision(_meeting())
    path = tmp_path / 'briefs/failed.learned.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'steps': {'commitments': {'status': 'failed', 'proof': {
        'coverage': 'successful_meeting_revisions', 'meeting_revisions': [revision]}}}}))
    assert revision not in capture.successful_revisions(str(tmp_path))


def test_capture_batches_three_and_leaves_the_rest_for_next_job(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    meetings = [{**_meeting(f'Alex: I will send deck {index}.'), 'meeting_id': f'm-{index}',
                 'start': f'2026-09-{10 + index:02d}T10:00:00Z'} for index in range(5)]
    monkeypatch.setattr(capture.compose_followup, 'build_context',
                        lambda inputs, hours: ('context', inputs['granola']))
    seen = []
    monkeypatch.setattr(capture.compose_followup, 'compose',
                        lambda inputs, **kwargs: seen.extend(inputs['granola']) or {'commitments': []})
    monkeypatch.setattr(capture.apply_commitments, 'apply', lambda *_args, **_kwargs: {
        'written': 0, 'deduped': 0, 'skipped_terminal': 0, 'anchor_keys': []})
    result = capture.capture({'meetings': meetings}, 'alex@example.com')
    assert result['examined'] == 3 and len(result['meeting_revisions']) == 3 and len(seen) == 3


def test_cli_rejects_malformed_input(tmp_path, capsys):
    path = tmp_path / 'granola.json'
    path.write_text('{bad')
    assert capture.main(['--granola', str(path)]) == 1
    assert 'unreadable Granola input' in capsys.readouterr().err
