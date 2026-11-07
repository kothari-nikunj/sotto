"""Host-safe negative tests for the lifecycle-only tenant lock reader."""
import os
from pathlib import Path
import signal

import pytest

import tenant_lifecycle
import runtime_lock


def _native_uid() -> int:
    return os.geteuid() or 10001


def test_lifecycle_lock_refuses_symlink_and_fifo_without_opening_target(tmp_path):
    root = tmp_path / 'tenant'
    root.mkdir(mode=0o700)
    target = tmp_path / 'unrelated-private'
    target.write_text('untouched')
    target.chmod(0o600)
    lock = root / runtime_lock.LOCK
    lock.symlink_to(target)
    with pytest.raises(tenant_lifecycle.LifecycleUnavailableError):
        tenant_lifecycle._acquire_tenant_lock(root, _native_uid())
    assert target.read_text() == 'untouched'
    lock.unlink()
    os.mkfifo(lock, 0o600)
    with pytest.raises(tenant_lifecycle.LifecycleUnavailableError):
        tenant_lifecycle._acquire_tenant_lock(root, _native_uid())
    lock.unlink()
    lock.write_bytes(b'')
    lock.chmod(0o600)
    os.link(lock, tmp_path / 'lock-alias')
    with pytest.raises(tenant_lifecycle.LifecycleUnavailableError):
        tenant_lifecycle._acquire_tenant_lock(root, _native_uid())


def test_lifecycle_lock_requires_exact_protected_root(tmp_path):
    root = tmp_path / 'tenant'
    root.mkdir(mode=0o700)
    claimed = tenant_lifecycle._acquire_tenant_lock(root, _native_uid())
    try:
        assert Path(root / runtime_lock.LOCK).is_file()
    finally:
        os.close(claimed.fd)
    link = tmp_path / 'alias'
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(tenant_lifecycle.LifecycleUnavailableError):
        tenant_lifecycle._acquire_tenant_lock(link, _native_uid())
    root.chmod(0o777)
    with pytest.raises(tenant_lifecycle.LifecycleUnavailableError):
        tenant_lifecycle._acquire_tenant_lock(root, _native_uid())


def test_replacement_quiesce_signal_only_reaches_exact_node_leader(monkeypatch):
    node = tenant_lifecycle._Process(10, 2, 10000, 101, b'S')
    child = tenant_lifecycle._Process(11, 10, 10000, 102, b'S')
    receiver = tenant_lifecycle._Process(12, 2, 10001, 103, b'S')
    owner = object.__new__(tenant_lifecycle.Lifecycle)
    owner._replacement_leaders = {10: (101, 10000), 12: (103, 0)}
    owner._state = 'replacement'
    monkeypatch.setattr(owner, '_assert_lock', lambda: None)
    monkeypatch.setattr(owner, '_assert_empty', lambda: None)
    monkeypatch.setattr(owner, '_reap', lambda: None)
    signalled = []
    observations = iter(([node, child, receiver], [node, child, receiver],
                         [child, receiver], []))
    scans = [0]
    def tree():
        scans[0] += 1
        if scans[0] == 2:
            assert signalled == [(10, signal.SIGUSR2)]
        return next(observations)
    monkeypatch.setattr(owner, '_replacement_tree', tree)
    monkeypatch.setattr(tenant_lifecycle, '_signal_exact',
                        lambda entry, sig: signalled.append((entry.pid, sig)))

    owner.stop_replacement(timeout=2, grace=1, node_quiesce_signal=signal.SIGUSR2)

    assert signalled == [(10, signal.SIGUSR2), (11, signal.SIGTERM),
                         (12, signal.SIGTERM)]


def test_stuck_node_gets_bounded_quiesce_before_receiver_term(monkeypatch):
    node = tenant_lifecycle._Process(10, 2, 10000, 101, b'S')
    receiver = tenant_lifecycle._Process(12, 2, 10001, 103, b'S')
    owner = object.__new__(tenant_lifecycle.Lifecycle)
    owner._replacement_leaders = {10: (101, 10000), 12: (103, 0)}
    owner._state = 'replacement'
    checks = []
    monkeypatch.setattr(owner, '_assert_lock', lambda: checks.append(True))
    monkeypatch.setattr(owner, '_assert_empty', lambda: None)
    monkeypatch.setattr(owner, '_reap', lambda: None)
    now = [0.0]
    monkeypatch.setattr(tenant_lifecycle.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(tenant_lifecycle.time, 'sleep',
                        lambda seconds: now.__setitem__(0, round(now[0] + 0.1, 1)))
    monkeypatch.setattr(owner, '_replacement_tree',
                        lambda: [node, receiver] if now[0] <= 1 else [receiver] if now[0] < 1.6 else [])
    signalled = []
    monkeypatch.setattr(tenant_lifecycle, '_signal_exact',
                        lambda entry, sig: signalled.append((now[0], entry.pid, sig)))

    owner.stop_replacement(timeout=2, grace=1, node_quiesce_signal=signal.SIGUSR2)

    assert signalled == [(0.0, 10, signal.SIGUSR2), (1.0, 10, signal.SIGKILL),
                         (1.0, 12, signal.SIGTERM), (1.5, 12, signal.SIGKILL)]
    assert len(checks) >= len(signalled)


def test_supervised_stop_keeps_receiver_until_node_budget_expires(monkeypatch):
    node = tenant_lifecycle._Process(10, 2, 10000, 101, b'S')
    receiver = tenant_lifecycle._Process(12, 2, 10001, 103, b'S')
    owner = object.__new__(tenant_lifecycle.Lifecycle)
    owner._replacement_leaders = {10: (101, 10000), 12: (103, 0)}
    owner._state = 'replacement'
    monkeypatch.setattr(owner, '_assert_lock', lambda: None)
    monkeypatch.setattr(owner, '_assert_empty', lambda: None)
    monkeypatch.setattr(owner, '_reap', lambda: None)
    now = [0.0]
    monkeypatch.setattr(tenant_lifecycle.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(tenant_lifecycle.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + 1))
    monkeypatch.setattr(owner, '_replacement_tree',
                        lambda: [node, receiver] if now[0] <= 35 else [receiver] if now[0] < 41 else [])
    signals = []
    monkeypatch.setattr(tenant_lifecycle, '_signal_exact',
                        lambda entry, sig: signals.append((now[0], entry.pid, sig)))

    owner.stop_replacement(timeout=45, grace=35, node_quiesce_signal=signal.SIGUSR2)

    assert signals == [(0.0, 10, signal.SIGUSR2), (35.0, 10, signal.SIGKILL),
                       (35.0, 12, signal.SIGTERM), (40.0, 12, signal.SIGKILL)]


def test_supervised_stop_holds_changed_node_uid_without_signalling(monkeypatch):
    changed = tenant_lifecycle._Process(10, 2, 10001, 101, b'S')
    owner = object.__new__(tenant_lifecycle.Lifecycle)
    owner._replacement_leaders = {10: (101, 10000)}
    owner._state = 'replacement'
    locks = []
    signals = []
    monkeypatch.setattr(owner, '_assert_lock', lambda: locks.append(True))
    monkeypatch.setattr(owner, '_reap', lambda: None)
    monkeypatch.setattr(owner, '_replacement_tree', lambda: [changed])
    monkeypatch.setattr(tenant_lifecycle, '_signal_exact',
                        lambda entry, sig: signals.append((entry.pid, sig)))

    with pytest.raises(tenant_lifecycle.LifecycleUnavailableError,
                       match='Node identity changed'):
        owner.stop_replacement(timeout=2, grace=1, node_quiesce_signal=signal.SIGUSR2)
    assert locks and not signals
    assert owner._state == 'failed'


def test_replacement_default_keeps_term_and_refuses_other_override(monkeypatch):
    node = tenant_lifecycle._Process(10, 2, 10000, 101, b'S')
    owner = object.__new__(tenant_lifecycle.Lifecycle)
    owner._replacement_leaders = {10: (101, 10000)}
    owner._state = 'replacement'
    monkeypatch.setattr(owner, '_assert_lock', lambda: None)
    monkeypatch.setattr(owner, '_assert_empty', lambda: None)
    monkeypatch.setattr(owner, '_reap', lambda: None)
    observations = iter(([node], []))
    monkeypatch.setattr(owner, '_replacement_tree', lambda: next(observations))
    signalled = []
    monkeypatch.setattr(tenant_lifecycle, '_signal_exact',
                        lambda entry, sig: signalled.append((entry.pid, sig)))

    owner.stop_replacement(timeout=2, grace=1)
    assert signalled == [(10, signal.SIGTERM)]
    with pytest.raises(tenant_lifecycle.LifecycleUnavailableError):
        owner.stop_replacement(timeout=2, grace=1, node_quiesce_signal=signal.SIGINT)
