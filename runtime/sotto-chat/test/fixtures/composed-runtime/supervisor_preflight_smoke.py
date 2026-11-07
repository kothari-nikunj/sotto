"""Local-only process-boundary fixture; deliberately not a runtime launcher."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

SOURCE = Path(os.environ.get('SUPERVISOR_SOURCE_ROOT', '/app'))
PROTOTYPE = Path(__file__).resolve().parent
ADAPTERS = SOURCE / 'adapters'
sys.path[:0] = [str(ADAPTERS / 'sotto')]

import runtime_lock  # noqa: E402


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError('fixture module unavailable')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def require(value: bool, reason: str) -> None:
    if not value:
        raise RuntimeError(reason)


def wait_file(path: Path, timeout: float = 5.0) -> dict:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.02)
    raise RuntimeError('synthetic child did not become ready')


def lock_is_busy(data: Path) -> bool:
    try:
        fd = runtime_lock.acquire(data)
    except RuntimeError:
        return True
    os.close(fd)
    return False


def group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def drop_node_uid() -> None:
    os.setgroups([])
    os.setgid(10000)
    os.setuid(10000)


def wait_group_gone(pgid: int, timeout: float = 5.0) -> None:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if not group_exists(pgid):
            return
        time.sleep(0.025)
    raise RuntimeError('child process group did not disappear')


def terminate_group(proc: subprocess.Popen, pgid: int, grace: float = 0.25) -> None:
    if group_exists(pgid):
        os.killpg(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    if group_exists(pgid):
        os.killpg(pgid, signal.SIGKILL)
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('child leader did not exit after KILL') from error
    wait_group_gone(pgid)


def force_cleanup_group(proc: subprocess.Popen, pgid: int) -> None:
    """Failure-path cleanup never returns while a child group can retain the lock."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        # The group kill above should include its leader; repeat once in case of a race.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=2.0)
    wait_group_gone(pgid)


def readiness_preflight() -> str:
    runtime_api = load_module('sotto_runtime_api_preflight', ADAPTERS / 'sotto' / 'runtime_api.py')
    os.environ['SOTTO_CHAT_RUNTIME_URL'] = 'http://127.0.0.1:19981'
    os.environ['SOTTO_CHAT_RUNTIME_TOKEN'] = 'synthetic-local-runtime-token-0123456789'
    ready, reason = runtime_api.runtime_readiness()
    require(ready is False, 'frozen runtime readiness unexpectedly passed')
    require('unattended skill run tool and approval parity' in reason
            and 'personal routine scheduler and migration contract' in reason
            and 'credential-discovery contract' in reason,
            f'readiness refusal was not the expected static contract gate: {reason}')
    return reason


def build_projection(data: Path):
    fixture = load_module('handoff_fixture', ADAPTERS / 'sotto' / 'test_process_handoff.py')
    startup = load_module('handoff_startup', ADAPTERS / 'sotto' / 'startup_config.py')
    handoff_module = load_module('handoff_builder', ADAPTERS / 'sotto' / 'process_handoff.py')
    manifest = fixture.photon_manifest()
    config, _projection = startup.render(manifest)
    authority = fixture.authorities(config)
    authority['SOTTO_DATA'] = str(data)
    authority['SOTTO_HOME'] = str(data / 'sotto-home')
    return handoff_module.build_child_handoff(
        manifest,
        authority,
        stable_identity=fixture.stable_identity(config),
        node_config_path='/run/sotto-node/runtime-config.json',
        data_directory=str(data),
        skills_root='/app/sotto-skills',
    )


def main() -> None:
    data = Path('/tmp/sotto-prototype-data')
    marker_dir = Path('/tmp/sotto-prototype-markers') / uuid.uuid4().hex
    marker_dir.mkdir(parents=True, mode=0o700)
    marker_dir.chmod(0o777)  # synthetic UID-separated workers need to publish nonsecret readiness markers
    handoff = build_projection(data)
    require(handoff.node_launch_uid == 10000 and handoff.receiver_workload_uid == 10001,
            'closed handoff UID contract changed')
    require('SOTTO_MCP_TOKEN' not in handoff.node_env and 'PHOTON_PROJECT_SECRET' not in handoff.node_env,
            'receiver/provider authority leaked into the Node environment')
    require('/app/adapters/sotto/managed_exec.py' in handoff.receiver_argv,
            'handoff no longer uses managed_exec')

    # The real static preflight is evaluated while ownership is held, before any
    # child is launched. Current code must refuse, so no provider adapter can start.
    owner_fd = runtime_lock.acquire(data)
    launches = 0
    try:
        reason = readiness_preflight()
        require(launches == 0, 'a child was launched before readiness passed')
        require(lock_is_busy(data), 'runtime lock was not held during readiness preflight')
    finally:
        os.close(owner_fd)

    # Exercise the reusable low-level process boundary only with inert fixture
    # children; this is not an override of runtime_readiness and starts no providers.
    owner_fd = runtime_lock.acquire(data)
    node_marker = marker_dir / 'node.json'
    receiver_marker = marker_dir / 'receiver.json'
    env_node = dict(handoff.node_env)
    env_receiver = dict(handoff.receiver_env)
    env_node['PATH'] = '/usr/local/bin:/usr/bin:/bin'
    env_receiver['PATH'] = '/usr/local/bin:/usr/bin:/bin'
    node_argv = [handoff.node_argv[0], str(PROTOTYPE / 'node_child.mjs'), str(node_marker),
                 str(PROTOTYPE / 'stubborn_descendant.py'), str(owner_fd)]
    node = subprocess.Popen(
        node_argv,
        cwd='/tmp', env=env_node, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, pass_fds=(owner_fd,), start_new_session=True,
        close_fds=True, preexec_fn=drop_node_uid,
    )
    launches += 1
    receiver = None
    receiver_pgid = None
    node_pgid = node.pid
    try:
        node_ready = wait_file(node_marker)
        desc_ready = wait_file(Path(str(node_marker) + '.descendant'))
        require(node_ready['uid'] == 10000 and desc_ready['uid'] == 10000,
                'synthetic Node process tree did not run as UID 10000')
        receiver_argv = [sys.executable, str(ADAPTERS / 'sotto' / 'managed_exec.py'), 'gateway',
                         sys.executable, str(PROTOTYPE / 'managed_worker.py'), str(receiver_marker)]
        receiver = subprocess.Popen(
            receiver_argv, cwd='/tmp', env=env_receiver, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(owner_fd,),
            close_fds=True,
        )
        launches += 1
        receiver_pgid = receiver.pid
        receiver_ready = wait_file(receiver_marker)
        require(receiver_ready['uid'] == 10001, 'managed_exec did not drop the worker to UID 10001')

        os.close(owner_fd)
        owner_fd = -1
        require(lock_is_busy(data), 'runtime lock released while children held inherited descriptors')

        terminate_group(receiver, receiver_pgid, grace=2.0)
        receiver = None
        receiver_pgid = None
        # The Node leader exits on TERM but its stubborn descendant keeps the flock.
        os.killpg(node_pgid, signal.SIGTERM)
        try:
            node.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        require(group_exists(node_pgid), 'stubborn descendant did not survive leader TERM')
        require(lock_is_busy(data), 'runtime lock released before the remaining descendant stopped')
        os.killpg(node_pgid, signal.SIGKILL)
        node.wait(timeout=2.0)
        wait_group_gone(node_pgid)
        require(not lock_is_busy(data), 'runtime lock remained busy after all process groups stopped')
        reacquired = runtime_lock.acquire(data)
        os.close(reacquired)
        print(json.dumps({
            'ok': True,
            'preflight_refused_before_child_launch': True,
            'preflight_reason': reason,
            'projection_node_uid': handoff.node_launch_uid,
            'projection_receiver_uid': handoff.receiver_workload_uid,
            'observed_node_uid': node_ready['uid'],
            'observed_receiver_uid': receiver_ready['uid'],
            'descendant_survived_leader_term': True,
            'kill_reaped_group_before_lock_release': True,
            'lock_reacquired_after_all_children_stopped': True,
        }, sort_keys=True), flush=True)
    finally:
        try:
            if receiver is not None and receiver_pgid is not None and group_exists(receiver_pgid):
                force_cleanup_group(receiver, receiver_pgid)
        finally:
            try:
                if group_exists(node_pgid):
                    force_cleanup_group(node, node_pgid)
            finally:
                if owner_fd >= 0:
                    os.close(owner_fd)


if __name__ == '__main__':
    main()
