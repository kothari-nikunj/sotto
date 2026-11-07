"""Canonical owner-only close, retry, and protected provenance checks."""
from datetime import datetime, timezone

import pytest
import yaml

import continuity_resolve as cr
import knowledge_edit as ke
import ledger_io
from ledger_provenance import classify_ledger_sources, digest_frontmatter

NOW = datetime(2026, 10, 5, 17, 35, tzinfo=timezone.utc)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    return tmp_path


def _created():
    return ke.op_loop_add_admitted_owner_text(
        'Send the signed deck', owner_text_span='Send the signed deck',
        owner_event_id='add-event', now=NOW)['anchor_key']


def _command(anchor):
    return '/loop close ' + anchor


def _prepare(anchor, event='close-event'):
    return ke.op_loop_close_prepare_admitted_owner_anchor(
        anchor, command=_command(anchor), owner_event_id=event, now=NOW)['expected_row_digest']


def _write(anchor, pre, event='close-event'):
    return ke.op_loop_close_admitted_owner_anchor(
        anchor, command=_command(anchor), owner_event_id=event,
        expected_row_digest=pre, now=NOW)


def _inspect(anchor, pre, event='close-event'):
    return ke.op_loop_close_inspect_admitted_owner_anchor(
        anchor, command=_command(anchor), owner_event_id=event,
        expected_row_digest=pre, now=NOW)


def test_add_prepare_close_retry_and_inspect_preserve_creation(root):
    anchor = _created()
    pre = _prepare(anchor)
    result = _write(anchor, pre)
    assert result['created'] is True and result['anchor_key'] == anchor
    assert _write(anchor, pre)['created'] is False
    assert _inspect(anchor, pre)['post_digest'] == result['post_digest']
    rows = [row for row in ledger_io.load_entries() if row.get('anchor_key') == anchor]
    assert len(rows) == 1
    row = rows[0]
    assert row['owner_event_id'] == 'add-event'
    assert row['summary'] == 'Send the signed deck'
    assert row['status'] == 'resolved' and row['resolution'] == 'user_resolved'
    assert row['owner_close_event_id'] == 'close-event'
    assert row['owner_close_previous_digest'] == pre
    assert row['content_provenance']['digest'] == result['post_digest']
    assert classify_ledger_sources(row) == ('owner', frozenset())


def test_changed_valid_owner_row_refuses_before_effect(root):
    anchor = _created()
    pre = _prepare(anchor)
    row = cr._load_items()[anchor]
    row['deadline'] = '2026-10-06'
    row['content_provenance']['digest'] = digest_frontmatter(row)
    cr._persist(row)
    with pytest.raises(ke.OwnerLoopClosePreEffectRefusalError) as error:
        _write(anchor, pre)
    assert error.value.code == 'owner_close_target_changed'
    assert cr._load_items()[anchor]['status'] == 'open'


def test_foreign_and_legacy_targets_never_gain_owner_stamp(root):
    directory = root / 'knowledge' / 'continuity'
    directory.mkdir(parents=True)
    (directory / 'legacy.md').write_text('---\nanchor_key: legacy\nstatus: open\nsummary: Old\n---\n')
    for anchor in ('legacy', 'missing'):
        with pytest.raises(ke.OwnerLoopClosePreEffectRefusalError) as error:
            _prepare(anchor)
        assert error.value.code == 'owner_close_target_unavailable'
    assert 'content_provenance' not in cr._load_items()['legacy']


def test_source_backed_row_and_forged_refresh_flag_refuse(root):
    directory = root / 'knowledge' / 'continuity'
    directory.mkdir(parents=True)
    source = {'anchor_key': 'source-anchor', 'status': 'open', 'summary': 'Source ask',
              'channel': 'gmail', 'source': 'gmail', 'source_thread_id': 'opaque-gmail-thread'}
    source['content_provenance'] = {'version': 1, 'kind': 'sources', 'sources': ['gmail'],
                                    'digest': digest_frontmatter(source)}
    (directory / 'source.md').write_text('---\n' + yaml.safe_dump(source) + '---\n')
    with pytest.raises(ke.OwnerLoopClosePreEffectRefusalError) as error:
        _prepare('source-anchor')
    assert error.value.code == 'owner_close_target_unavailable'
    row = cr._load_items()['source-anchor']
    row['status'], row['resolution'] = 'resolved', 'user_resolved'
    row['owner_close_event_id'] = 'close-event'
    row['owner_close_text_hash'] = 'c' * 64
    row['owner_close_previous_digest'] = source['content_provenance']['digest']
    with pytest.raises(ValueError, match='owner close transition'):
        cr._persist(row, owner_close_previous_digest=source['content_provenance']['digest'])
    assert cr._load_items()['source-anchor']['status'] == 'open'


def test_ambiguous_anchor_and_changed_command_fail_closed(root):
    anchor = _created()
    pre = _prepare(anchor)
    with pytest.raises(ke.EditError, match='invalid admitted'):
        ke.op_loop_close_admitted_owner_anchor(
            anchor, command='/loop close forged', owner_event_id='close-event',
            expected_row_digest=pre, now=NOW)
    for padded in (' ' + anchor, anchor + ' '):
        with pytest.raises(ke.EditError, match='invalid admitted'):
            ke.op_loop_close_prepare_admitted_owner_anchor(
                padded, command=_command(padded), owner_event_id='close-event', now=NOW)
    source = (root / 'knowledge' / 'continuity' / 'duplicate.md')
    source.write_text('---\nanchor_key: ' + anchor + '\nstatus: open\n---\n')
    with pytest.raises(ke.EditError, match='ambiguous'):
        _write(anchor, pre)


def test_second_event_cannot_close_same_terminal_owner_row(root):
    anchor = _created()
    pre = _prepare(anchor)
    _write(anchor, pre)
    with pytest.raises(ke.OwnerLoopClosePreEffectRefusalError) as error:
        _write(anchor, pre, event='another-close-event')
    assert error.value.code == 'owner_close_target_unavailable'
    with pytest.raises(ke.EditError, match='receipt is unavailable'):
        _inspect(anchor, pre, event='another-close-event')


def test_unknown_after_persist_inspects_without_second_transition(root, monkeypatch):
    anchor = _created()
    pre = _prepare(anchor)
    original = cr._fsync_ledger_directory_tree
    calls = 0
    monkeypatch.setattr(ke, '_load_continuity', lambda: cr)

    def interrupted():
        nonlocal calls
        calls += 1
        raise OSError('synthetic directory fsync failure')

    monkeypatch.setattr(cr, '_fsync_ledger_directory_tree', interrupted)
    with pytest.raises(OSError):
        _write(anchor, pre)
    assert calls == 1
    monkeypatch.setattr(cr, '_fsync_ledger_directory_tree', original)
    proof = _inspect(anchor, pre)
    assert proof['post_digest'] == cr._load_items()[anchor]['content_provenance']['digest']
    assert _write(anchor, pre)['created'] is False
    (root / 'knowledge' / 'continuity' / next(
        p.name for p in (root / 'knowledge' / 'continuity').glob('*.md'))).unlink()
    with pytest.raises(ke.EditError, match='receipt is unavailable'):
        _inspect(anchor, pre)
