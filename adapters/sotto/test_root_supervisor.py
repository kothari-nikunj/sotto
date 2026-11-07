"""Offline kernel-mount observation contract for the root supervisor."""
from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys

import pytest

try:
    from . import root_supervisor
except ImportError:
    import root_supervisor


class _ShortRead(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 7))


def test_mountinfo_reads_all_short_proc_chunks_before_parsing():
    raw = b'1 0 8:2 / / rw - ext4 /dev/vda rw\n42 1 8:3 / /data rw - ext4 /dev/vdb rw\n'
    assert root_supervisor._read_mountinfo(_ShortRead(raw)) == raw


def test_mount_record_finds_later_entry_after_short_initial_read(monkeypatch):
    raw = b'1 0 8:2 / / rw - ext4 /dev/vda rw\n42 1 8:3 / /data rw - ext4 /dev/vdb rw\n'
    monkeypatch.setattr(root_supervisor, 'open',
                        lambda *_args, **_kwargs: _ShortRead(raw), raising=False)
    assert root_supervisor._mount_record(Path('/data')) == ('ext4', '/dev/vdb', 8, 3, 42)


def test_mountinfo_refuses_content_above_bound(monkeypatch):
    monkeypatch.setattr(root_supervisor, '_MAX_MOUNTINFO_BYTES', 20)
    with pytest.raises(root_supervisor.SupervisorUnavailableError):
        root_supervisor._read_mountinfo(_ShortRead(b'x' * 21))


def test_direct_isolated_cli_refuses_invalid_arguments_from_foreign_cwd(tmp_path):
    (tmp_path / 'startup_config.py').write_text('raise RuntimeError("foreign cwd imported")\n')
    result = subprocess.run(
        [sys.executable, '-I', '-B', str(Path(root_supervisor.__file__).resolve()),
         '--definitely-invalid'],
        cwd=tmp_path, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 2
    assert result.stdout == ''
    assert result.stderr.strip() == '[sotto] root supervisor arguments unavailable'
