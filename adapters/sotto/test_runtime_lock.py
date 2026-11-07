"""A present replacement epoch, even a broken one, refuses a fresh runtime acquisition."""
import pytest

import tenant_lifecycle
import runtime_lock


def test_dangling_epoch_symlink_refuses_runtime(tmp_path):
    (tmp_path / runtime_lock.EPOCH_FILE).symlink_to(tmp_path / 'missing')
    with pytest.raises(RuntimeError, match='replacement epoch'):
        runtime_lock.acquire(tmp_path)
    with pytest.raises(tenant_lifecycle.LifecycleUnavailableError, match='epoch'):
        tenant_lifecycle._acquire_tenant_lock(tmp_path, 10001)


def test_malformed_epoch_refuses_both_acquisition_paths(tmp_path):
    (tmp_path / runtime_lock.EPOCH_FILE).write_text('not-json')
    with pytest.raises(RuntimeError, match='replacement epoch'):
        runtime_lock.acquire(tmp_path)
    with pytest.raises(tenant_lifecycle.LifecycleUnavailableError, match='epoch'):
        tenant_lifecycle._acquire_tenant_lock(tmp_path, 10001)


def test_runtime_rechecks_the_epoch_after_acquiring_volume_lock(tmp_path, monkeypatch):
    """An epoch published between the fast check and flock still refuses the writer."""
    original = runtime_lock.fcntl.flock

    def hold_then_lock(fd, flags):
        original(fd, flags)
        (tmp_path / runtime_lock.EPOCH_FILE).write_text('{}')

    monkeypatch.setattr(runtime_lock.fcntl, 'flock', hold_then_lock)
    with pytest.raises(RuntimeError, match='replacement epoch'):
        runtime_lock.acquire(tmp_path)
