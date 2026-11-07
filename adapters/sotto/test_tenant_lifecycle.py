"""Offline kernel start contract; run only as root in a dedicated private Docker PID namespace."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import time

import tenant_lifecycle
import runtime_lock

WORKER = Path(__file__).parent / 'test_fixtures/lifecycle_worker.py'


def require(value, reason):
    if not value:
        raise AssertionError(reason)


def refused(action, reason):
    try:
        action()
    except tenant_lifecycle.LifecycleUnavailableError:
        return
    raise AssertionError(reason)


def await_marker(path: Path) -> dict:
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.02)
    raise AssertionError('detached worker did not start')


def lock_busy(root):
    try:
        descriptor = runtime_lock.acquire(root)
    except RuntimeError:
        return True
    os.close(descriptor)
    return False


def flock_held(root):
    """Probe the kernel lock directly; runtime_lock.acquire also refuses on a hold file."""
    try:
        descriptor = os.open(root / runtime_lock.LOCK, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)
    return False


def discard(lifecycle):
    """Test-only teardown of an owner that production must leave to process exit."""
    os.close(lifecycle._guard_fd)
    try:
        os.close(lifecycle._lock_fd)
    except OSError:
        pass
    lifecycle._lock_fd = None


def new_root(root: Path) -> Path:
    root.mkdir(mode=0o700)
    return root


def inert_launch(lifecycle, root):
    """A launch attempt with argv that would fail loudly if a child were ever started."""
    lifecycle._start_replacement_roles(
        ('/nonexistent/node',), ('/nonexistent/receiver',),
        {'PATH': '/usr/bin:/bin'}, {'PATH': '/usr/bin:/bin', 'SOTTO_DATA': str(root)})


def exercise_empty_start(root: Path):
    lifecycle = tenant_lifecycle.Lifecycle(new_root(root), native_uid=10001,
                                                 private_pid_namespace=True)
    require(lock_busy(root), 'tenant lock not held after construction')
    refused(lifecycle.close, 'unproved owner released the tenant lock')
    lifecycle.prove_empty_start()
    refused(lifecycle.prove_empty_start, 'empty start was proved twice')
    lifecycle.assert_replacement_root(root)
    require(lock_busy(root), 'tenant lock not retained after empty proof')
    lifecycle.close()
    require(not lock_busy(root), 'tenant lock did not release after empty proof')


def exercise_existing_epoch(root: Path):
    new_root(root)
    (root / runtime_lock.EPOCH_FILE).write_text('{}')
    refused(lambda: tenant_lifecycle.Lifecycle(root, native_uid=10001,
                                                     private_pid_namespace=True),
            'fresh start ignored an existing replacement epoch')
    require(not flock_held(root), 'refused fresh start kept the tenant lock')
    lifecycle = tenant_lifecycle.Lifecycle(root, native_uid=10001,
                                                 private_pid_namespace=True,
                                                 reentry_epoch=True)
    require(flock_held(root), 'reentry did not hold the tenant lock')
    lifecycle.prove_empty_start()
    refused(lambda: inert_launch(lifecycle, root), 'reentry minted a fresh epoch')
    require(lifecycle._state == 'quiescent' and not lifecycle._replacement_children,
            'refused fresh launch changed state or started a child')
    lifecycle.close()


def exercise_prior_consumer(root: Path):
    """A process already present is a refusal, never a signal target."""
    new_root(root)
    root.chmod(0o1777)  # native UID writes markers under the root-owned fixture dir
    marker = root / 'prior.json'
    starter = subprocess.Popen((sys.executable, str(WORKER), str(marker)),
                               env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C'},
                               start_new_session=True, user=10001, group=10001,
                               extra_groups=())
    observed = await_marker(marker)
    starter.wait(timeout=4)
    lifecycle = tenant_lifecycle.Lifecycle(root, native_uid=10001,
                                                 private_pid_namespace=True)
    try:
        def identity():
            entry = tenant_lifecycle._read_process(observed['pid'])
            return entry and (entry.pid, entry.starttime, entry.uid, entry.state != b'Z')

        prior = identity()
        require(prior, 'prior consumer fixture did not stay alive')
        refused(lifecycle.prove_empty_start, 'existing consumer passed the empty proof')
        time.sleep(0.1)
        require(identity() == prior, 'empty proof signalled or replaced an existing consumer')
        refused(lambda: inert_launch(lifecycle, root), 'unproved owner launched roles')
        refused(lifecycle.close, 'unproved owner released the tenant lock')
        require(lock_busy(root), 'refusal dropped the tenant lock')
    finally:
        if tenant_lifecycle._read_process(observed['pid']) is not None:
            os.kill(observed['pid'], signal.SIGKILL)
        deadline = time.monotonic() + 4
        while tenant_lifecycle._read_process(observed['pid']) is not None:
            require(time.monotonic() < deadline, 'init did not reap the prior consumer')
            time.sleep(0.02)
        discard(lifecycle)


def exercise_broken_lock(root: Path, *, close_fd: bool):
    lifecycle = tenant_lifecycle.Lifecycle(new_root(root), native_uid=10001,
                                                 private_pid_namespace=True)
    lifecycle.prove_empty_start()
    if close_fd:
        os.close(lifecycle._lock_fd)
    else:
        fcntl.flock(lifecycle._lock_fd, fcntl.LOCK_UN)
    try:
        refused(lambda: lifecycle.assert_replacement_root(root), 'lost flock kept root authority')
        refused(lambda: inert_launch(lifecycle, root), 'lost flock launched roles')
        require(not lifecycle._replacement_children, 'lost flock started a child')
        refused(lifecycle.close, 'lost flock permitted ordinary lock release')
    finally:
        discard(lifecycle)


def exercise_replaced_lock_path(root: Path):
    lifecycle = tenant_lifecycle.Lifecycle(new_root(root), native_uid=10001,
                                                 private_pid_namespace=True)
    lifecycle.prove_empty_start()
    original = root / runtime_lock.LOCK
    original.rename(root / 'old-runtime-lock')
    original.write_bytes(b'')
    original.chmod(0o600)
    try:
        refused(lambda: lifecycle.assert_replacement_root(root), 'replaced lock kept root authority')
        refused(lambda: inert_launch(lifecycle, root), 'replaced lock launched roles')
        refused(lifecycle.close, 'replaced lock path allowed ordinary close')
    finally:
        discard(lifecycle)


def exercise_forked_copy(root: Path):
    lifecycle = tenant_lifecycle.Lifecycle(new_root(root), native_uid=10001,
                                                 private_pid_namespace=True)
    lifecycle.prove_empty_start()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            lifecycle._assert_lock()
        except tenant_lifecycle.LifecycleUnavailableError:
            os.write(write_fd, b'refused')
        else:
            os.write(write_fd, b'accepted')
        os._exit(0)
    os.close(write_fd)
    verdict = os.read(read_fd, 16)
    os.close(read_fd)
    os.waitpid(pid, 0)
    require(verdict == b'refused', 'forked copy inherited lock ownership')
    lifecycle.close()


def main():
    require(sys.platform == 'linux' and os.geteuid() == 0, 'Linux root fixture required')
    require(os.getpid() != 1 and Path('/proc/1/comm').read_text().strip() == 'tini',
            'run only under dedicated Docker --pid=private with tini PID1')
    with tempfile.TemporaryDirectory(prefix='sotto-native-lifecycle-') as directory:
        root = Path(directory)
        root.chmod(0o1777)  # the native-UID fixture worker traverses the temporary directory
        exercise_prior_consumer(root / 'prior-consumer')
        exercise_empty_start(root / 'empty')
        exercise_existing_epoch(root / 'existing-epoch')
        exercise_broken_lock(root / 'closed-fd', close_fd=True)
        exercise_broken_lock(root / 'unlocked-fd', close_fd=False)
        exercise_replaced_lock_path(root / 'replaced-lock-path')
        exercise_forked_copy(root / 'forked-copy')
    print(json.dumps({'subreaper': True, 'empty_start_proved': True,
                      'existing_epoch_refuses_fresh_start': True,
                      'prior_consumer_refused_not_signalled': True,
                      'broken_lock_no_launch': True, 'replaced_lock_no_launch': True,
                      'forked_copy_not_owner': True}, sort_keys=True))


if __name__ == '__main__':
    main()
