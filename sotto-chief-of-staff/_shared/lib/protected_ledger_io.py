"""Bounded local ledger input for protected projections; no grants or writes.

Two read shapes share one descriptor-safe file walk:

- `load_protected_entries` reads the WHOLE ledger, for a projection or an event-ID scan. Its
  bounds are sized for years of normal growth, not for a fresh install: parked rows and terminal
  rows carrying owner-tool or source-correction receipts stay on disk by design
  (HOW-SOTTO-DECIDES "Parked, never deleted"; continuity_resolve's retention rule), so a ledger
  that only grows must not turn "unavailable" after a few busy months. MAX_FILES is about a
  decade at a heavy pace of a few retained rows a day; MAX_TOTAL_BYTES is MAX_FILES rows at
  4 KiB each. A ledger past either bound is not normal growth: that read is unavailable, never
  partial.
- `load_protected_scope` serves readers that act on some rows (a reminder's loop, a nudge's
  loops). It skips a malformed file exactly as the canonical parser does (that file is no row
  anywhere in Sotto), and a row it cannot safely read - ambiguous, oversized, symlinked - is
  OMITTED and reported as `complete=False` instead of failing the read, so one bad file cannot
  hold every reminder; absence then proves nothing. With `anchors` it keeps only matching rows,
  and the bounds apply to what it keeps, never to how many other rows the ledger holds.
"""
from __future__ import annotations

import os
import stat

import ledger_io

MAX_FILE_BYTES = 128 * 1024
MAX_FILES = 16384
MAX_TOTAL_BYTES = MAX_FILES * 4 * 1024
# Room for the writer's lock file and in-flight `.loop-*.tmp` replacements beside every row.
MAX_DIRECTORY_ENTRIES = 2 * MAX_FILES


class ProtectedLedgerUnavailableError(RuntimeError):
    """The whole read is unavailable; never return an incomplete enumeration as complete."""


class _OmittedFileError(Exception):
    """One file a tolerant read leaves out; the read reports incomplete coverage instead."""


def _strict_frontmatter_has_unique_keys(content: str) -> bool:
    """Check every YAML mapping without changing the canonical frontmatter parser."""
    if ledger_io.yaml is None:
        return False

    class UniqueKeyLoader(ledger_io.yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)
            keys = set()
            for key_node, _value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in keys:
                    raise ValueError('duplicate YAML mapping key')
                keys.add(key)
            return super().construct_mapping(node, deep=deep)

    end = content.find('\n---', 3)
    if not content.startswith('---') or end == -1:
        return False
    try:
        ledger_io.yaml.load(content[4:end], Loader=UniqueKeyLoader)
    except (ValueError, TypeError, RecursionError, ledger_io.yaml.YAMLError):
        return False
    return True


def _read_file(parent: int, name: str, remaining_total: int, *, tolerant: bool) -> bytes:
    """One regular, non-symlinked file within the per-file and remaining total bounds."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except OSError:
        if tolerant:          # a symlink is refused, never followed; the file is left out
            raise _OmittedFileError() from None
        raise
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            if tolerant:
                raise _OmittedFileError()
            raise ProtectedLedgerUnavailableError('ledger input is unavailable')
        remaining = min(MAX_FILE_BYTES, remaining_total)
        content = bytearray()
        while True:
            chunk = os.read(descriptor, min(8192, remaining - len(content) + 1))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > remaining:
                raise ProtectedLedgerUnavailableError('ledger input exceeds its bound')
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ProtectedLedgerUnavailableError('ledger changed during read')
        return bytes(content)
    finally:
        os.close(descriptor)


def _walk(data_root: str, *, mode: str, anchors: frozenset | None) -> tuple[list[dict], bool]:
    """The one safe walk; `mode` is 'strict', 'ordinary' or 'tolerant' (see the two readers)."""
    descriptors = []
    if not isinstance(data_root, str) or not os.path.isabs(data_root) or len(data_root) > 4096:
        raise ProtectedLedgerUnavailableError("ledger root must be explicit and absolute")
    tolerant = mode == 'tolerant'
    whole = anchors is None
    complete = True
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(data_root, directory_flags)
        descriptors.append(parent)
        for component in ('knowledge', 'continuity'):
            try:
                parent = os.open(component, directory_flags, dir_fd=parent)
            except FileNotFoundError:
                return [], True
            descriptors.append(parent)
        before_directories = [os.fstat(fd) for fd in descriptors]
        names = []
        entries = 0
        with os.scandir(parent) as directory:
            for entry in directory:
                entries += 1
                if whole and entries > MAX_DIRECTORY_ENTRIES:
                    raise ProtectedLedgerUnavailableError('ledger input exceeds its bound')
                if entry.name.endswith('.md'):
                    names.append(entry.name)
                    if whole and len(names) > MAX_FILES:
                        raise ProtectedLedgerUnavailableError('ledger input exceeds its bound')
        rows = []
        total = 0
        for name in sorted(names):
            try:
                # A scoped read charges a file to the total only once its row is kept.
                content = _read_file(parent, name, MAX_TOTAL_BYTES - total if whole else MAX_FILE_BYTES,
                                     tolerant=tolerant)
                try:
                    text = content.decode('utf-8')
                    row = ledger_io.parse_frontmatter(text)
                except (UnicodeError, RecursionError):
                    text, row = '', None
                if type(row) is not dict:
                    if mode == 'strict':
                        raise ProtectedLedgerUnavailableError(
                            'ledger contains an omitted or ambiguous file')
                    continue      # not a row: the canonical parser skips it the same way
                if not whole and row.get('anchor_key') not in anchors:
                    continue
                # The ordinary parser accepts duplicate YAML keys by taking the last value. Never
                # let an ambiguous source or nested proof become an apparently owner-authored row.
                if not _strict_frontmatter_has_unique_keys(text):
                    if tolerant:
                        raise _OmittedFileError()
                    raise ProtectedLedgerUnavailableError(
                        'ledger contains an omitted or ambiguous file' if mode == 'strict'
                        else 'ledger contains ambiguous frontmatter')
            except _OmittedFileError:
                complete = False
                continue
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise ProtectedLedgerUnavailableError('ledger input exceeds its bound')
            rows.append(row)
        for descriptor, before in zip(descriptors, before_directories, strict=True):
            after = os.fstat(descriptor)
            if (before.st_mtime_ns, before.st_ctime_ns, before.st_nlink) != (
                    after.st_mtime_ns, after.st_ctime_ns, after.st_nlink):
                raise ProtectedLedgerUnavailableError('ledger directory changed during read')
        return rows, complete
    except (OSError, ValueError, TypeError, OverflowError):
        raise ProtectedLedgerUnavailableError('ledger input is unavailable') from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def load_protected_entries(data_root: str, *, strict: bool = False) -> list[dict]:
    """Read raw frontmatter; caller must still project provenance before rendering.

    Every directory below the explicit root is opened relative to its parent descriptor,
    with symlinks refused. Missing knowledge/continuity directories mean an empty ledger;
    inaccessible, detectably changing or oversized input invalidates the whole read. In ordinary
    mode malformed rows are omitted, as by the canonical parser. Strict mode fails the whole read
    instead of omitting a bare/malformed/non-mapping .md file; use it when absence would authorize
    a new event intent. No filename/path metadata or omitted count is added.
    Metadata checks detect observed races; they do not establish a transactional snapshot.
    """
    return _walk(data_root, mode='strict' if strict else 'ordinary', anchors=None)[0]


def load_protected_scope(data_root: str, anchors=None) -> tuple[list[dict], bool]:
    """The rows a reader can act on, and whether every canonical row was read (`complete`).

    A malformed or bare .md file is skipped as the canonical parser skips it. A file the canonical
    parser WOULD read but this reader will not trust - ambiguous (duplicate YAML keys), oversized,
    symlinked or special - is left out and makes `complete` False rather than failing the read.
    `anchors`, an iterable of anchor keys, keeps only those rows. Absence proves nothing unless
    `complete` is True. A missing root, an unreadable directory, a whole read past its bounds or a
    concurrent change still raises ProtectedLedgerUnavailableError: retry later, never guess.
    """
    if anchors is not None:
        anchors = frozenset(anchors)
        if not anchors or any(type(anchor) is not str or not anchor for anchor in anchors):
            raise ProtectedLedgerUnavailableError('invalid ledger scope')
    return _walk(data_root, mode='tolerant', anchors=anchors)
