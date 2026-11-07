"""Run only in a disposable root/tini private-PID Docker container, with no network."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time

import native_replacement
import runtime_lock
import startup_config
import tenant_lifecycle
from test_process_handoff import authorities, photon_manifest, stable_identity


ROLE = Path(__file__).parent / 'test_fixtures/replacement_role.py'
WRAPPER = Path(__file__).parent / 'managed_exec.py'


def require(value, reason):
    if not value:
        raise AssertionError(reason)


def marker(path):
    until = time.monotonic() + 4
    while time.monotonic() < until:
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.02)
    raise AssertionError('replacement role did not start')


def new_lifecycle(root):
    root.mkdir(mode=0o1777)
    root.chmod(0o1777)
    lifecycle = tenant_lifecycle.Lifecycle(root, native_uid=10001,
                                                 private_pid_namespace=True)
    lifecycle.prove_empty_start()
    return lifecycle


def roles(root, *, node_mode='wait', receiver_mode='wait', receiver_argv=None):
    node_marker = root / 'node.json'
    receiver_marker = root / 'receiver.json'
    node_argv = (sys.executable, str(ROLE), str(node_marker), node_mode)
    receiver_argv = receiver_argv or (
        sys.executable, str(WRAPPER), 'gateway', sys.executable,
        str(ROLE), str(receiver_marker), receiver_mode)
    node_env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'}
    receiver_env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
                    'SOTTO_DATA': str(root)}
    return node_argv, receiver_argv, node_env, receiver_env, node_marker, receiver_marker


def finish_fixture(lifecycle):
    """Private test teardown only: production failed state cannot call close()."""
    try:
        lifecycle.stop_replacement(timeout=4, grace=0.1)
    except tenant_lifecycle.LifecycleUnavailableError:
        pass
    lifecycle._assert_empty()
    os.close(lifecycle._guard_fd)
    os.close(lifecycle._lock_fd)
    lifecycle._lock_fd = None


def exercise_inventory_rescan():
    """A torn /proc sample may recover, but a second unsafe sample never can."""
    lifecycle = object.__new__(tenant_lifecycle.Lifecycle)
    lifecycle._pid = 2
    lifecycle._native_uid = 10001
    lifecycle._replacement_leaders = {10: (100, 10000), 11: (200, 0)}
    process = tenant_lifecycle._Process
    init = process(1, 0, 0, 1, b'S')
    supervisor = process(2, 1, 0, 50, b'S')
    node = process(10, 2, 10000, 100, b'S')
    receiver = process(11, 2, 0, 200, b'S')
    child = process(20, 10, 10000, 300, b'S')
    torn = {1: init, 2: supervisor, 10: node, 11: receiver,
            20: process(20, 99, 10000, 300, b'S')}
    closed = {1: init, 2: supervisor, 10: node, 11: receiver, 20: child}
    original_inventory = tenant_lifecycle._processes

    def scan(*samples):
        calls = iter(samples)
        count = []

        def inventory():
            count.append(1)
            return next(calls)

        tenant_lifecycle._processes = inventory
        try:
            result = lifecycle._replacement_tree()
            return result, len(count)
        finally:
            tenant_lifecycle._processes = original_inventory

    selected, calls = scan(torn, closed)
    require({entry.pid for entry in selected} == {10, 11, 20} and calls == 2,
            'transient missing parent did not get one fresh, fully checked inventory')

    for first, second, reason in (
        (torn, torn, 'persistent missing parent'),
        (torn, {**closed, 20: process(20, 1, 10000, 300, b'S')}, 'outside tree'),
        (torn, {**closed, 20: process(20, 10, 12345, 300, b'S')}, 'foreign UID'),
        (torn, {**closed, 10: process(10, 2, 10000, 101, b'S')}, 'leader reuse'),
    ):
        try:
            scan(first, second)
        except tenant_lifecycle.LifecycleUnavailableError:
            pass
        else:
            raise AssertionError(reason + ' passed fresh replacement validation')

    # Errors that are not an incomplete ancestry sample must never trigger a rescan.
    for unsafe, reason in (
        ({**closed, 20: process(20, 1, 10000, 300, b'S')}, 'outside tree'),
        ({**closed, 20: process(20, 10, 12345, 300, b'S')}, 'foreign UID'),
        ({**closed, 10: process(10, 2, 10000, 101, b'S')}, 'leader reuse'),
    ):
        attempts = []
        tenant_lifecycle._processes = lambda: (attempts.append(1), unsafe)[1]
        try:
            try:
                lifecycle._replacement_tree()
            except tenant_lifecycle.LifecycleUnavailableError:
                pass
            else:
                raise AssertionError(reason + ' was accepted')
            require(len(attempts) == 1, reason + ' caused an unsafe retry')
        finally:
            tenant_lifecycle._processes = original_inventory

    # A recovered scan cannot make a missing role leader healthy.
    lifecycle._state = 'replacement'
    lifecycle._assert_lock = lambda: None
    lifecycle._reap = lambda: None
    original_read = tenant_lifecycle._read_process
    tenant_lifecycle._processes = iter((torn, closed)).__next__
    tenant_lifecycle._read_process = lambda pid: None if pid == 10 else closed.get(pid)
    try:
        try:
            lifecycle.check_replacement()
        except tenant_lifecycle.LifecycleUnavailableError:
            pass
        else:
            raise AssertionError('missing role leader passed after inventory rescan')
        require(lifecycle._state == 'failed', 'missing role leader did not hold lifecycle')
    finally:
        tenant_lifecycle._processes = original_inventory
        tenant_lifecycle._read_process = original_read


def test_inventory_rescan():
    """Pure process-inventory logic: runs under ordinary pytest, not only the root/tini fixture."""
    exercise_inventory_rescan()


def exercise_success(root):
    lifecycle = new_lifecycle(root)
    node, receiver, node_env, receiver_env, node_path, receiver_path = roles(root)
    try:
        lifecycle._start_replacement_roles(node, receiver, node_env, receiver_env)
        require(marker(node_path)['uid'] == 10000, 'Node role UID changed')
        require(marker(receiver_path)['uid'] == 10001, 'receiver wrapper did not drop UID')
        lifecycle.check_replacement()
        require(lifecycle._lock_fd is not None, 'supervisor dropped tenant flock')
        try:
            runtime_lock.acquire(root)
        except RuntimeError:
            pass
        else:
            raise AssertionError('second runtime acquired during replacement')
        for denied in (lambda: lifecycle.close(),
                       lifecycle.prove_empty_start,
                       lambda: lifecycle._start_replacement_roles(node, receiver, node_env, receiver_env)):
            try:
                denied()
            except tenant_lifecycle.LifecycleUnavailableError:
                pass
            else:
                raise AssertionError('replacement permitted wrong lifecycle transition')
    finally:
        finish_fixture(lifecycle)
    require((root / runtime_lock.EPOCH_FILE).is_file(), 'durable crash hold missing')
    try:
        tenant_lifecycle.Lifecycle(root, native_uid=10001, private_pid_namespace=True)
    except tenant_lifecycle.LifecycleUnavailableError:
        pass
    else:
        raise AssertionError('restart bypassed unresolved replacement hold')


def exercise_configured_projection(root):
    lifecycle = new_lifecycle(root)
    manifest = photon_manifest()
    config, _receiver = startup_config.render(manifest)
    authority = authorities(config)
    authority['SOTTO_DATA'] = str(root)
    authority['SOTTO_HOME'] = str(root / 'sotto-home')
    node_dir = root / 'node-stage'
    node_dir.mkdir(mode=0o700)
    os.chown(node_dir, 10000, 10000)
    runtime_dir = root / 'runtime'
    runtime_dir.mkdir(mode=0o700)
    os.chown(runtime_dir, 10001, 10001)
    node_config = node_dir / 'runtime-config.json'
    node_config.write_bytes(startup_config._payload_bytes(config))
    node_config.chmod(0o600)
    os.chown(node_config, 10000, 10000)
    expectation = runtime_dir / 'expected-channel.json'
    expectation.write_bytes(startup_config._payload_bytes(
        startup_config.receiver_channel_expectation(config)))
    expectation.chmod(0o600)
    os.chown(expectation, 10001, 10001)
    options = dict(stable_identity=stable_identity(config),
                   node_config_path=str(node_config), data_directory=str(root),
                   skills_root='/app/sotto-skills')
    handoff = native_replacement.verified_handoff(manifest, authority, **options)
    require(handoff.node_launch_uid == 10000 and handoff.receiver_workload_uid == 10001,
            'closed projection UIDs changed')
    try:
        native_replacement.start_configured_replacement(
            lifecycle, manifest, authority, **options)
    except native_replacement.ReplacementUnavailableError:
        pass  # current hard receiver-readiness gate must stop before process launch
    else:
        raise AssertionError('unready receiver launched')
    require(lifecycle._state == 'quiescent' and not lifecycle._replacement_children,
            'readiness refusal contacted a child')
    wrong = root.parent / 'other-tenant'
    wrong.mkdir(mode=0o700)
    try:
        lifecycle.assert_replacement_root(wrong)
    except tenant_lifecycle.LifecycleUnavailableError:
        pass
    else:
        raise AssertionError('other tenant root inherited flock authority')
    node_config.write_bytes(b'changed')
    try:
        native_replacement.verified_handoff(manifest, authority, **options)
    except native_replacement.ReplacementUnavailableError:
        pass
    else:
        raise AssertionError('changed staged Node config was accepted')
    lifecycle.close()


def exercise_failed_second_launch(root):
    lifecycle = new_lifecycle(root)
    node, _, node_env, receiver_env, node_path, _ = roles(root)
    try:
        try:
            lifecycle._start_replacement_roles(node, ('/nonexistent/receiver',),
                                               node_env, receiver_env)
        except tenant_lifecycle.LifecycleUnavailableError:
            pass
        else:
            raise AssertionError('bad second child was accepted')
        require(lifecycle._state == 'failed', 'partial launch did not hold')
        try:
            lifecycle.close()
        except tenant_lifecycle.LifecycleUnavailableError:
            pass
        else:
            raise AssertionError('partial launch released lock')
    finally:
        finish_fixture(lifecycle)


def exercise_child_exit(root):
    lifecycle = new_lifecycle(root)
    node, receiver, node_env, receiver_env, node_path, receiver_path = roles(
        root, node_mode='exit')
    try:
        lifecycle._start_replacement_roles(node, receiver, node_env, receiver_env)
        marker(node_path)
        marker(receiver_path)
        time.sleep(0.2)
        try:
            lifecycle.check_replacement()
        except tenant_lifecycle.LifecycleUnavailableError:
            pass
        else:
            raise AssertionError('exited Node role was treated as healthy')
        require(lifecycle._state == 'failed', 'child exit did not hold')
    finally:
        finish_fixture(lifecycle)


def exercise_detached_and_timeout(root):
    lifecycle = new_lifecycle(root)
    node, receiver, node_env, receiver_env, node_path, receiver_path = roles(
        root, node_mode='detached')
    original_signal = tenant_lifecycle._signal_exact
    try:
        lifecycle._start_replacement_roles(node, receiver, node_env, receiver_env)
        marker(node_path)
        marker(receiver_path)
        detached = int(node_path.with_suffix('.detached').read_text())
        require(tenant_lifecycle._read_process(detached) is not None,
                'detached Node worker missing')
        tenant_lifecycle._signal_exact = lambda *_: None
        try:
            lifecycle.stop_replacement(timeout=0.15, grace=0.05)
        except tenant_lifecycle.LifecycleUnavailableError:
            pass
        else:
            raise AssertionError('uncertain shutdown returned success')
        require(lifecycle._state == 'failed', 'timeout did not hold')
        try:
            lifecycle.close()
        except tenant_lifecycle.LifecycleUnavailableError:
            pass
        else:
            raise AssertionError('timeout released lock')
    finally:
        tenant_lifecycle._signal_exact = original_signal
        finish_fixture(lifecycle)
    require(tenant_lifecycle._read_process(detached) is None,
            'detached Node worker survived owned cleanup')


def main():
    require(sys.platform == 'linux' and os.geteuid() == 0, 'isolated root Linux required')
    require(Path('/proc/1/comm').read_text().strip() == 'tini' and os.getppid() == 1,
            'fixture must run directly under tini')
    exercise_inventory_rescan()
    with tempfile.TemporaryDirectory(prefix='sotto-replacement-') as directory:
        root = Path(directory)
        root.chmod(0o1777)
        exercise_success(root / 'success')
        exercise_configured_projection(root / 'projection')
        exercise_failed_second_launch(root / 'partial')
        exercise_child_exit(root / 'child-exit')
        exercise_detached_and_timeout(root / 'detached')
    print(json.dumps({'same_flock': True, 'node_uid': 10000, 'receiver_uid': 10001,
                      'partial_launch_held': True, 'child_exit_held': True,
                      'detached_reaped': True, 'timeout_held': True,
                      'restart_held': True}, sort_keys=True))


if __name__ == '__main__':
    main()
