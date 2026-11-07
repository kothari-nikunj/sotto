"""Bounded disk-to-view fixtures; no runtime tool or authenticated route is exposed."""
from datetime import datetime, timezone

import pytest
import yaml

import knowledge_edit
import ledger_io
from ledger_provenance import digest_frontmatter
import owner_continuity as reads
from protected_ledger_io import ProtectedLedgerUnavailableError

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def fixture_row(root, name, *, status='open', snoozed_until=None, source=False):
    row = {'anchor_key': name, 'summary': name, 'source': 'gmail' if source else 'user_added',
           'channel': 'email' if source else 'manual', 'action_type': 'follow_up',
           'source_thread_id': 'gmail-thread' if source else 'manual:' + name,
           'status': status, 'created_at': '2026-09-01', 'snoozed_until': snoozed_until}
    if source:
        row['source_refs'] = [{'sourceType': 'gmail', 'sourceId': 'message-1'}]
    row['content_provenance'] = {'version': 1, 'kind': 'sources' if source else 'owner',
                                 'sources': ['gmail'] if source else [],
                                 'digest': digest_frontmatter(row)}
    directory = root / 'knowledge' / 'continuity'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (name + '.md')).write_text('---\n' + yaml.safe_dump(row) + '---\n')


def test_canonical_selection_and_counts_follow_provenance_under_explicit_root(tmp_path, monkeypatch):
    fixture_row(tmp_path, 'active-owner')
    fixture_row(tmp_path, 'parked-owner', status='parked')
    fixture_row(tmp_path, 'closed-owner', status='resolved')
    fixture_row(tmp_path, 'snoozed-owner', snoozed_until='2099-01-01')
    fixture_row(tmp_path, 'source-must-be-withheld', source=True)
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path / 'unrelated-data'))
    result = reads.read_owner_continuity(str(tmp_path), now=NOW, include_parked=True)
    assert result['coverage'] == 'owner_authored_only'
    assert [row['what'] for row in result['you_owe']] == ['active-owner']
    assert [row['what'] for row in result['parked']] == ['parked-owner']
    assert result['counts'] == {'you_owe': 1, 'waiting_on_them': 0, 'parked': 1}
    assert 'source-must-be-withheld' not in str(result)
    result = reads.read_owner_continuity(str(tmp_path), now=NOW, view='retune')
    assert result['current'] == {}
    assert result['counts'] == {'stale': 1, 'mute_suggestions': 0}
    assert result['stale_loops'][0]['what'] == 'active-owner'


def test_actual_admitted_owner_writer_visible_but_legacy_cli_content_unproved(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    text = 'Send my signed deck'
    knowledge_edit.op_loop_add_admitted_owner_text(text, owner_text_span=text,
                                                   owner_event_id='fixture-owner-event', now=NOW)
    knowledge_edit.op_loop_add('Copied email request stays unproved', now=NOW)
    result = reads.read_owner_continuity(str(tmp_path), now=NOW)
    assert [row['what'] for row in result['you_owe']] == [text]
    assert len(ledger_io.load_entries()) == 2


def test_response_size_failure_returns_no_partial_projection(tmp_path, monkeypatch):
    fixture_row(tmp_path, 'owner')
    monkeypatch.setattr(reads, 'MAX_RESPONSE_BYTES', 1)
    with pytest.raises(ProtectedLedgerUnavailableError, match='response bound'):
        reads.read_owner_continuity(str(tmp_path), now=NOW)


@pytest.mark.parametrize('options', [dict(now=datetime(2026, 9, 30)),
                                     dict(now=NOW, view='unknown'),
                                     dict(now=NOW, include_parked='yes'),
                                     dict(now=NOW, view='retune', include_parked=True)])
def test_invalid_request_never_reads_disk(tmp_path, monkeypatch, options):
    def forbidden(_root):
        raise AssertionError('disk read before request validation')
    monkeypatch.setattr(reads, 'load_protected_entries', forbidden)
    with pytest.raises(ProtectedLedgerUnavailableError, match='invalid protected read'):
        reads.read_owner_continuity(str(tmp_path), **options)
