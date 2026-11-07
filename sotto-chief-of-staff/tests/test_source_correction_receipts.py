"""Bounded dormant receipts never infer a withdrawal from absent evidence."""
import json
import pytest
import source_corrections as corrections


def change(key='a'):
    return {'source_ref': 'msg:v2:imessage:' + key * 64,
            'previous_content_version': 'b' * 64, 'content_version': 'c' * 64,
            'source_revision_evidence': {'kind': 'edit', 'marker': 'date_edited', 'value': '800000000000000001'},
            'store_generation': 'd' * 64}


@pytest.fixture
def active(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(corrections, 'ENABLED', True)
    return tmp_path


def test_retained_receipts_are_read_only_and_keep_exact_version_authority(active):
    candidate = change()
    key = corrections._identity(candidate)
    path = corrections._path()
    path.parent.mkdir()
    path.write_text(json.dumps({'schema_version': 2, 'receipts': {key: {
        'evidence': candidate, 'status': 'held', 'after': 'c_abc.md',
        'accepted_at': '2026-10-20T00:00:00+00:00', 'unreadable': 1}}}))
    before = path.read_bytes()
    assert corrections.invalidated_refs() == {corrections.evidence_ref(candidate)}
    assert corrections.revision()
    assert corrections._load()['receipts'][key]['status'] == 'held'
    assert path.read_bytes() == before


@pytest.mark.parametrize('value', [
    {'receipts': {}}, {'schema_version': 2, 'receipts': []},
    {'schema_version': 2, 'receipts': {}, 'private': 'unexpected'},
])
def test_active_unknown_or_malformed_state_fails_closed(active, value):
    path = corrections._path()
    path.parent.mkdir()
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        corrections.invalidated_refs()


def test_legacy_state_never_authorizes_withdrawal_and_off_never_reads_it(active, monkeypatch):
    path = corrections._path()
    path.parent.mkdir()
    path.write_text(json.dumps(['imessage:msg:v1:' + 'a' * 24]))
    assert corrections.invalidated_refs() == set()
    path.write_text('broken legacy')
    monkeypatch.setattr(corrections, 'ENABLED', False)
    assert corrections.invalidated_refs() == set()
    assert corrections.revision() == ''
    assert path.read_text() == 'broken legacy'
