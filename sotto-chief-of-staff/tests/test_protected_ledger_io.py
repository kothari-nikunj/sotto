"""Fixtures never read real owner data or follow fixture symlinks."""
import os
import signal

import protected_ledger_io as reader
import pytest


def directory(root):
    path = root / 'knowledge' / 'continuity'
    path.mkdir(parents=True)
    return path


def test_explicit_root_sorted_frontmatter_and_no_path(tmp_path, monkeypatch):
    path = directory(tmp_path)
    monkeypatch.setenv('SOTTO_DATA', '/not-the-requested-root')
    (path / 'b.md').write_text('---\nsummary: second\n---\nbody ignored')
    (path / 'a.md').write_text('---\nsummary: first\n---\n')
    (path / 'broken.md').write_text('---\n[malformed\n---\n')
    assert reader.load_protected_entries(str(tmp_path)) == [{'summary': 'first'}, {'summary': 'second'}]


@pytest.mark.parametrize('raw', [
    '---\nsource: gmail\nsource: user_added\nsummary: ambiguous\n---\n',
    '---\nsource: user_added\ncontent_provenance:\n'
    '  version: 1\n  kind: sources\n  kind: owner\n'
    '  sources: []\n  digest: abc\n---\n',
])
def test_duplicate_source_or_nested_proof_invalidates_protected_read(tmp_path, raw):
    path = directory(tmp_path)
    (path / 'owner.md').write_text('---\nsummary: valid preceding row\n---\n')
    (path / 'z-ambiguous.md').write_text(raw)
    with pytest.raises(reader.ProtectedLedgerUnavailableError, match='ambiguous frontmatter'):
        reader.load_protected_entries(str(tmp_path))


def test_missing_ledger_is_empty_but_missing_root_unavailable(tmp_path):
    assert reader.load_protected_entries(str(tmp_path)) == []
    with pytest.raises(reader.ProtectedLedgerUnavailableError):
        reader.load_protected_entries(str(tmp_path / 'missing'))


@pytest.mark.parametrize('kind', ['file_symlink', 'directory_symlink', 'fifo'])
def test_special_files_fail_without_following_or_blocking(tmp_path, kind):
    path = directory(tmp_path)
    target = tmp_path / 'outside.md'
    target.write_text('---\nsummary: must not read\n---\n')
    if kind == 'file_symlink':
        (path / 'row.md').symlink_to(target)
    elif kind == 'fifo':
        os.mkfifo(path / 'row.md')
    else:
        path.rmdir()
        path.symlink_to(tmp_path, target_is_directory=True)
    def hung_read(_signal, _frame):
        raise AssertionError("special-file read blocked before type validation")

    previous = signal.signal(signal.SIGALRM, hung_read)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        with pytest.raises(reader.ProtectedLedgerUnavailableError):
            reader.load_protected_entries(str(tmp_path))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.parametrize('limit', ['MAX_FILE_BYTES', 'MAX_TOTAL_BYTES', 'MAX_FILES', 'MAX_DIRECTORY_ENTRIES'])
def test_limits_withhold_whole_read(tmp_path, monkeypatch, limit):
    path = directory(tmp_path)
    (path / 'a.md').write_text('---\nsummary: first\n---\n')
    (path / 'b.md').write_text('---\nsummary: second\n---\n')
    monkeypatch.setattr(reader, limit, 1)
    with pytest.raises(reader.ProtectedLedgerUnavailableError):
        reader.load_protected_entries(str(tmp_path))


def test_relative_root_is_never_interpreted_against_process_directory():
    with pytest.raises(reader.ProtectedLedgerUnavailableError):
        reader.load_protected_entries('relative')


def test_changed_file_invalidates_already_collected_rows(tmp_path, monkeypatch):
    path = directory(tmp_path)
    (path / 'a.md').write_text('---\nsummary: first\n---\n')
    target = path / 'b.md'
    target.write_text('---\nsummary: second\n---\n')
    original_read = reader.os.read
    changed = False

    def mutate(descriptor, size):
        nonlocal changed
        chunk = original_read(descriptor, size)
        if b'second' in chunk and not changed:
            changed = True
            target.write_text('---\nsummary: changed after first read\n---\n')
        return chunk

    monkeypatch.setattr(reader.os, 'read', mutate)
    with pytest.raises(reader.ProtectedLedgerUnavailableError):
        reader.load_protected_entries(str(tmp_path))
    assert changed



def test_added_file_after_enumeration_invalidates_whole_read(tmp_path, monkeypatch):
    path = directory(tmp_path)
    (path / 'initial.md').write_text('---\nsummary: first\n---\n')
    original_scandir = reader.os.scandir

    class ChangingDirectory:
        def __enter__(self):
            self.iterator = original_scandir(path)
            return self.iterator.__enter__()

        def __exit__(self, *args):
            self.iterator.__exit__(*args)
            (path / 'late.md').write_text('---\nsummary: added after enumeration\n---\n')

    monkeypatch.setattr(reader.os, 'scandir', lambda _descriptor: ChangingDirectory())
    with pytest.raises(reader.ProtectedLedgerUnavailableError, match='directory changed'):
        reader.load_protected_entries(str(tmp_path))


def test_whole_read_holds_a_ledger_grown_past_the_old_bound(tmp_path):
    """PR #817 review: parked rows and receipt-bearing terminal rows are kept by design, so the
    former 1024-file bound was reachable by ordinary use and failed every strict reader."""
    path = directory(tmp_path)
    for i in range(1100):
        (path / f'row-{i:04}.md').write_text(f'---\nanchor_key: k{i}\nstatus: parked\n---\n')
    assert len(reader.load_protected_entries(str(tmp_path), strict=True)) == 1100
    assert reader.MAX_FILES >= 16384 and reader.MAX_TOTAL_BYTES >= reader.MAX_FILES * 4096


def test_scope_skips_malformed_like_the_canonical_parser_and_reports_untrusted_rows(tmp_path):
    path = directory(tmp_path)
    (path / 'a.md').write_text('---\nanchor_key: a\n---\n')
    (path / 'bare.md').write_text('no frontmatter at all')
    (path / 'broken.md').write_text('---\n[malformed\n---\n')
    assert reader.load_protected_scope(str(tmp_path)) == ([{'anchor_key': 'a'}], True)
    (path / 'dup.md').write_text('---\nanchor_key: b\nanchor_key: c\n---\n')
    assert reader.load_protected_scope(str(tmp_path)) == ([{'anchor_key': 'a'}], False)


@pytest.mark.parametrize('kind', ['file_symlink', 'fifo', 'oversized'])
def test_scope_leaves_out_a_file_it_will_not_read_instead_of_failing(tmp_path, kind):
    path = directory(tmp_path)
    (path / 'a.md').write_text('---\nanchor_key: a\n---\n')
    target = tmp_path / 'outside.md'
    target.write_text('---\nanchor_key: a\n---\n')
    if kind == 'file_symlink':
        (path / 'row.md').symlink_to(target)
    elif kind == 'fifo':
        os.mkfifo(path / 'row.md')
    else:
        (path / 'row.md').write_text('---\nanchor_key: a\nsummary: ' + 'x' * reader.MAX_FILE_BYTES + '\n---\n')
    assert reader.load_protected_scope(str(tmp_path), ['a']) == ([{'anchor_key': 'a'}], False)
    with pytest.raises(reader.ProtectedLedgerUnavailableError):
        reader.load_protected_entries(str(tmp_path))


def test_scope_by_anchor_bounds_what_it_keeps_not_the_ledger_size(tmp_path, monkeypatch):
    path = directory(tmp_path)
    for name in ('a', 'b', 'c'):
        (path / f'{name}.md').write_text(f'---\nanchor_key: {name}\nsummary: {name * 40}\n---\n')
    for limit, value in (('MAX_FILES', 1), ('MAX_DIRECTORY_ENTRIES', 1), ('MAX_TOTAL_BYTES', 80)):
        monkeypatch.setattr(reader, limit, value)
    rows, complete = reader.load_protected_scope(str(tmp_path), ['b'])
    assert [row['anchor_key'] for row in rows] == ['b'] and complete
    with pytest.raises(reader.ProtectedLedgerUnavailableError):
        reader.load_protected_scope(str(tmp_path))
    with pytest.raises(reader.ProtectedLedgerUnavailableError):
        reader.load_protected_scope(str(tmp_path), ['a', 'b'])     # what it KEEPS is still bounded
    for invalid in ([], [''], [None]):
        with pytest.raises(reader.ProtectedLedgerUnavailableError):
            reader.load_protected_scope(str(tmp_path), invalid)


def test_scope_still_refuses_a_concurrent_change_and_a_missing_root(tmp_path, monkeypatch):
    path = directory(tmp_path)
    target = path / 'a.md'
    target.write_text('---\nanchor_key: a\n---\n')
    original_read = reader.os.read

    def mutate(descriptor, size):
        chunk = original_read(descriptor, size)
        if b'anchor_key: a' in chunk:
            target.write_text('---\nanchor_key: a\nstatus: resolved\n---\n')
        return chunk

    monkeypatch.setattr(reader.os, 'read', mutate)
    with pytest.raises(reader.ProtectedLedgerUnavailableError):
        reader.load_protected_scope(str(tmp_path), ['a'])
    monkeypatch.setattr(reader.os, 'read', original_read)
    with pytest.raises(reader.ProtectedLedgerUnavailableError):
        reader.load_protected_scope(str(tmp_path / 'missing'), ['a'])
