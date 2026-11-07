#!/usr/local/bin/python3
"""External, offline composed-runtime failure-path fixture (not a production supervisor)."""
from __future__ import annotations

import json
import ctypes
import os
from pathlib import Path
import pwd
import re
import selectors
import signal
import subprocess
import sys
import threading
import time

SOTTO_ADAPTER = Path('/app/adapters/sotto')
sys.path.insert(0, str(SOTTO_ADAPTER))
import runtime_lock  # noqa: E402



class BoundedCapture:
    """Drain child diagnostics without retaining more than a fixed prefix."""

    def __init__(self, pipe, limit: int = 8192) -> None:
        self.pipe = pipe
        self.limit = limit
        self.data = bytearray()
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        while True:
            chunk = self.pipe.read(4096)
            if not chunk:
                return
            remaining = self.limit - len(self.data)
            if remaining > 0:
                self.data.extend(chunk[:remaining])

    def text(self) -> str:
        self.thread.join(timeout=5)
        require(not self.thread.is_alive(), 'child diagnostic pipe did not close')
        return self.data.decode('utf-8', 'replace')


NODE_UID = 10000
RECEIVER_UID = 10001
NODE_HARNESS = '/app/runtime/sotto-chat/test/fixtures/composed-runtime/node_runtime_harness.mjs'
NODE_BIN = '/usr/local/bin/node'
PYTHON_BIN = '/usr/local/bin/python3'
MANAGED_EXEC = '/app/adapters/sotto/managed_exec.py'
RECEIVER_MAIN = '/app/trigger-receiver/receiver.py'
READY_TIMEOUT = 30
STOP_TIMEOUT = 20


def require(value: bool, safe_reason: str) -> None:
    if not value:
        raise RuntimeError(safe_reason)


def child_status(pid: int) -> dict[str, int] | None:
    try:
        text = Path(f'/proc/{pid}/status').read_text(encoding='ascii')
    except (OSError, UnicodeError):
        return None
    result = {}
    for line in text.splitlines():
        if line.startswith('Uid:'):
            values = line.split()[1:]
            if len(values) >= 4:
                result = {'real': int(values[0]), 'effective': int(values[1]),
                          'saved': int(values[2]), 'filesystem': int(values[3])}
            break
    return result or None


def stop_node(process: subprocess.Popen) -> int:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        return process.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.wait(timeout=5)


def make_config() -> dict:
    config = {
        'version': 1,
        'channel': 'telegram',
        'stateDirectory': '/tmp/unused-state',
        'mediaRoot': '/tmp/unused-media',
        'bearerToken': 'node-fixture-bearer-token-0123456789abcdef',
        'port': 0,
        'owner': {'accountId': 'synthetic-owner-account', 'ownerId': '8675309'},
        'lineKey': 'fixture-primary',
        'target': 'telegram-owner',
        'providerThreadId': 'telegram:8675309',
        'credentials': {
            'telegramToken': '123456:fixture_bot_token_with_at_least_twenty_chars',
            'telegramLinkFile': {'bot_token': '123456:fixture_bot_token_with_at_least_twenty_chars',
                                 'allowed_user': 8675309},
        },
        'instructions': 'Offline fixture only.',
        'modelProxy': {'origin': 'https://proxy.invalid', 'bearer': 'fixture-model-token',
                       'chatModelId': 'fixture-chat', 'nativeMediaModelId': 'fixture-media'},
    }
    return config


def await_node_ready(process: subprocess.Popen) -> dict:
    require(process.stdout is not None, 'Node stdout pipe missing')
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + READY_TIMEOUT
    buffer = bytearray()
    try:
        while time.monotonic() < deadline:
            require(process.poll() is None, 'Node runtime exited before ready')
            events = selector.select(timeout=0.1)
            if not events:
                continue
            chunk = os.read(process.stdout.fileno(), 512)
            require(bool(chunk), 'Node runtime closed readiness pipe')
            buffer.extend(chunk)
            require(len(buffer) <= 4096, 'Node readiness output exceeded bound')
            if b'\n' in buffer:
                line, _, remainder = buffer.partition(b'\n')
                require(not remainder, 'Node emitted unexpected readiness output')
                try:
                    value = json.loads(line.decode('ascii'))
                except (UnicodeError, ValueError):
                    raise RuntimeError('Node readiness record was invalid') from None
                require(isinstance(value, dict) and value.get('kind') == 'node-ready',
                        'Node did not report fixture readiness')
                require(type(value.get('uid')) is int and value['uid'] == NODE_UID,
                        'Node process UID did not match 10000')
                require(type(value.get('port')) is int and 1 <= value['port'] <= 65535,
                        'Node loopback listener port was invalid')
                require(value.get('gateway') == 'active', 'Node gateway lifecycle was not active')
                return value
        raise RuntimeError('Node runtime readiness timed out')
    finally:
        selector.close()


def await_stdout_line(process: subprocess.Popen, prefix: bytes, timeout: float = 10) -> bytes:
    require(process.stdout is not None, 'synthetic Node stdout pipe missing')
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    try:
        while time.monotonic() < deadline:
            require(process.poll() is None, 'synthetic Node exited before descendant was ready')
            events = selector.select(timeout=0.1)
            if not events:
                continue
            chunk = os.read(process.stdout.fileno(), 512)
            require(bool(chunk), 'synthetic Node closed its readiness pipe')
            buffer.extend(chunk)
            require(len(buffer) <= 2048, 'synthetic Node readiness output exceeded bound')
            if b'\n' in buffer:
                line, _, remainder = buffer.partition(b'\n')
                require(not remainder, 'synthetic Node emitted unexpected readiness output')
                require(line.startswith(prefix), 'synthetic Node descendant marker was invalid')
                return line
        raise RuntimeError('synthetic Node descendant readiness timed out')
    finally:
        selector.close()


def proc_state(pid: int) -> str | None:
    try:
        fields = Path(f'/proc/{pid}/stat').read_text(encoding='ascii').rsplit(')', 1)[1].split()
    except (OSError, UnicodeError, IndexError):
        return None
    return fields[0] if fields else None


def reap_adopted_child(pid: int, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return proc_state(pid) is None
        if waited == pid:
            return proc_state(pid) is None
        time.sleep(0.02)
    return False


def exercise_descendant_lock(data_root: Path) -> dict[str, int | bool]:
    """Prove a grandchild's inherited lock outlives a dead group leader and supervisor fd."""
    # PID 1 already adopts namespace orphans. Outside a PID namespace init process,
    # explicitly become a child subreaper so the synthetic descendant can be waited.
    if os.getpid() != 1:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.restype = ctypes.c_int
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                               ctypes.c_ulong, ctypes.c_ulong]
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), 'could not enable child reaping for fixture')

    lock_fd = runtime_lock.acquire(str(data_root))
    stubborn = None
    capture = None
    descendant_pid = None
    try:
        fd = lock_fd
        child_source = (
            "require('node:fs').fstatSync(3);"
            "process.on('SIGTERM',()=>{});"
            "process.stdout.write('DESCENDANT_READY '+process.pid+'\\n');"
            "setInterval(()=>{},1000);"
        )
        parent_source = (
            f"require('node:fs').fstatSync({fd});"
            "process.on('SIGTERM',()=>{});"
            "const {spawn}=require('node:child_process');"
            f"const c=spawn(process.execPath,['-e',{json.dumps(child_source)}],"
            f"{{stdio:['ignore','pipe','ignore',{fd}]}});"
            "c.stdout.on('data',d=>process.stdout.write(d));"
            "setInterval(()=>{},1000);"
        )
        environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp',
                       'LANG': 'C.UTF-8', 'NODE_ENV': 'production'}
        stubborn = subprocess.Popen(
            [NODE_BIN, '-e', parent_source], env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, user=NODE_UID, group=NODE_UID, extra_groups=(),
            pass_fds=(lock_fd,), close_fds=True,
        )
        capture = BoundedCapture(stubborn.stderr)
        marker = await_stdout_line(stubborn, b'DESCENDANT_READY ')
        descendant_pid = int(marker.partition(b' ')[2])
        require(os.getpgid(descendant_pid) == stubborn.pid,
                'synthetic descendant escaped the supervised process group')
        require(proc_state(descendant_pid) not in (None, 'Z'),
                'synthetic descendant was not running after readiness')

        # Close both the fixture supervisor's descriptor and the Node group leader. The
        # child Node still owns fd 3, so flock must remain unavailable until group cleanup.
        os.close(lock_fd)
        lock_fd = -1
        os.kill(stubborn.pid, signal.SIGKILL)
        leader_code = stubborn.wait(timeout=5)
        require(leader_code == -signal.SIGKILL, 'synthetic Node leader was not killed as expected')
        require(proc_state(descendant_pid) not in (None, 'Z'),
                'synthetic descendant did not outlive its Node group leader')
        try:
            competing_fd = runtime_lock.acquire(str(data_root))
        except RuntimeError:
            descendant_retained_lock = True
        else:
            os.close(competing_fd)
            descendant_retained_lock = False
        require(descendant_retained_lock,
                'runtime lock became available while a descendant still held the inherited fd')

        os.killpg(stubborn.pid, signal.SIGTERM)
        term_deadline = time.monotonic() + 2
        while time.monotonic() < term_deadline and proc_state(descendant_pid) == 'Z':
            time.sleep(0.02)
        require(proc_state(descendant_pid) not in (None, 'Z'),
                'synthetic descendant did not hold through graceful TERM')
        os.killpg(stubborn.pid, signal.SIGKILL)
        require(reap_adopted_child(descendant_pid),
                'synthetic descendant was not reaped after process-group KILL')
        capture.text()
        reacquired_fd = runtime_lock.acquire(str(data_root))
        os.close(reacquired_fd)
        return {'leader_killed': True, 'descendant_pid': descendant_pid,
                'descendant_retained_lock': descendant_retained_lock,
                'term_required_kill': True, 'descendant_reaped': True,
                'lock_reacquired_after_group_reap': True}
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        if stubborn is not None and stubborn.poll() is None:
            try:
                os.killpg(stubborn.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                stubborn.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if descendant_pid is not None:
            reap_adopted_child(descendant_pid)
        if capture is not None:
            capture.pipe.close()


FIXTURE_STAGE = 'initializing'

def main() -> int:
    global FIXTURE_STAGE
    FIXTURE_STAGE = 'identity-check'
    require(os.geteuid() == 0, 'fixture supervisor must run as root')
    node_account = pwd.getpwuid(NODE_UID)
    receiver_account = pwd.getpwnam('sotto')
    require(node_account.pw_name == 'sotto-runtime', 'Node account name mismatch')
    require(receiver_account.pw_uid == RECEIVER_UID, 'receiver workload UID mismatch')

    FIXTURE_STAGE = 'claim-runtime-lock'
    data_root = Path('/data')
    lock_fd = runtime_lock.acquire(str(data_root))
    FIXTURE_STAGE = 'lock-acquired'
    lock_path = data_root / runtime_lock.LOCK
    lock_stat = lock_path.stat()
    require(lock_stat.st_uid == 0 and lock_stat.st_mode & 0o777 == 0o600,
            'runtime lock file was not root-owned private state')
    sotto_home = data_root / 'sotto-home'
    FIXTURE_STAGE = 'prepare-home'
    sotto_home.mkdir(mode=0o700, exist_ok=True)
    os.chown(sotto_home, RECEIVER_UID, receiver_account.pw_gid)
    FIXTURE_STAGE = 'prepare-root-owned-data'

    node_process = None
    receiver_process = None
    receiver_capture = None
    node_capture = None
    receiver_uid_seen = False
    expected_failure = False
    clean_shutdown = False
    release_fd = lock_fd
    try:
        FIXTURE_STAGE = 'check-lock-contention'
        # A second open file description in this process must not steal the live tenant lock.
        try:
            competing_fd = runtime_lock.acquire(str(data_root))
        except RuntimeError:
            pass
        else:
            os.close(competing_fd)
            raise RuntimeError('runtime lock contention was not enforced')

        FIXTURE_STAGE = 'prepare-node-config'
        config_json = json.dumps(make_config(), separators=(',', ':'))
        node_environment = {
            'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp',
            'USER': 'sotto-runtime', 'LOGNAME': 'sotto-runtime', 'LANG': 'C.UTF-8',
            'NODE_ENV': 'production', 'MASTRA_TELEMETRY_DISABLED': '1',
        }
        FIXTURE_STAGE = 'start-node'
        node_process = subprocess.Popen(
            [NODE_BIN, NODE_HARNESS, config_json], env=node_environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, user=NODE_UID, group=NODE_UID, extra_groups=(),
            pass_fds=(lock_fd,), close_fds=True,
        )
        node_capture = BoundedCapture(node_process.stderr)
        FIXTURE_STAGE = 'await-node-ready'
        try:
            node_ready = await_node_ready(node_process)
        except Exception:
            if node_process.poll() is None:
                stop_node(node_process)
            node_diagnostic = node_capture.text()
            print(json.dumps({
                'diagnostic': 'node-not-ready',
                'exit_code': node_process.poll(),
                'harness_start_failure': 'NODE_FIXTURE_START_FAILED' in node_diagnostic,
            }, separators=(',', ':')), file=sys.stderr)
            raise

        receiver_environment = {
            'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/home/sotto',
            'USER': 'sotto', 'LOGNAME': 'sotto', 'LANG': 'C.UTF-8',
            'SOTTO_DEPLOYMENT_MODE': 'managed',
            'SOTTO_TENANT_ID': 'synthetic-tenant', 'SOTTO_DATA': str(data_root),
            'SOTTO_HOME': str(sotto_home), 'SOTTO_VOLUME_ID': 'synthetic-volume',
            'SOTTO_CRON_DELIVER': 'photon', 'SOTTO_IMESSAGE_NUMBER': '+15555550123',
            'SOTTO_TRIGGER_PORT': '18789',
            'SOTTO_CHAT_RUNTIME_URL': f"http://127.0.0.1:{node_ready['port']}",
            'SOTTO_CHAT_RUNTIME_TOKEN': 'node-fixture-bearer-token-0123456789abcdef',
            'SOTTO_MCP_TOKEN': 'fixture-root-mcp-token-0123456789abcdef',
            'SOTTO_CONTROL_TOKEN': 'fixture-control-token-0123456789abcdef',
            'SOTTO_MODEL_PROXY_URL': 'https://proxy.invalid',
            'SOTTO_MODEL_PROXY_TOKEN': 'fixture-model-token-0123456789abcdef',
            'PHOTON_HOME_CHANNEL': '+15555550123', 'PHOTON_ALLOWED_USERS': '+15555550123',
            'PHOTON_PROJECT_ID': 'synthetic-photon-project',
            'PHOTON_PROJECT_SECRET': 'synthetic-photon-secret',
        }
        FIXTURE_STAGE = 'start-receiver'
        receiver_process = subprocess.Popen(
            [PYTHON_BIN, MANAGED_EXEC, 'receiver', RECEIVER_MAIN], env=receiver_environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=False, pass_fds=(lock_fd,), close_fds=True,
        )
        receiver_capture = BoundedCapture(receiver_process.stderr)
        FIXTURE_STAGE = 'await-receiver-readiness-failure'
        deadline = time.monotonic() + READY_TIMEOUT
        receiver_code = None
        while time.monotonic() < deadline:
            status = child_status(receiver_process.pid)
            if status and status['effective'] == RECEIVER_UID:
                receiver_uid_seen = True
            receiver_code = receiver_process.poll()
            if receiver_code is not None:
                break
            time.sleep(0.005)
        FIXTURE_STAGE = 'assert-receiver-exit'
        require(receiver_code is not None, 'receiver did not exit at the readiness gate')
        FIXTURE_STAGE = 'assert-receiver-uid'
        require(receiver_uid_seen, 'receiver workload UID 10001 was not observed')
        receiver_failure = receiver_capture.text()
        FIXTURE_STAGE = 'assert-receiver-reason'
        expected_failure = (receiver_code != 0 and
                            'Sotto receiver selection is not ready:' in receiver_failure)
        if not expected_failure:
            print(json.dumps({'diagnostic': 'receiver-not-readiness', 'exit_code': receiver_code,
                              'readiness_phrase': 'Sotto receiver selection is not ready:' in receiver_failure,
                              'traceback': 'Traceback' in receiver_failure,
                              'missing_module': 'ModuleNotFoundError' in receiver_failure or 'No module named' in receiver_failure,
                              'module_name': next(iter(re.findall(r"No module named ['\"]([A-Za-z0-9_.]+)", receiver_failure)), None),
                              'permission_error': 'Permission denied' in receiver_failure}, separators=(',', ':')), file=sys.stderr)
        require(expected_failure, 'receiver failed for a reason other than the expected readiness gate')

        # Drop the supervisor's own fd while the Node child is still alive. A
        # second open must fail now because the running runtime inherited the lock.
        FIXTURE_STAGE = 'close-supervisor-lock-fd'
        os.close(release_fd)
        release_fd = -1
        FIXTURE_STAGE = 'assert-node-still-owns-lock'
        try:
            competing_fd = runtime_lock.acquire(str(data_root))
        except RuntimeError:
            node_owns_lock = True
        else:
            os.close(competing_fd)
            node_owns_lock = False
        require(node_owns_lock, 'Node did not retain runtime lock after supervisor closed its fd')

        FIXTURE_STAGE = 'stop-node'
        node_stop_code = stop_node(node_process)
        node_capture.text()
        clean_shutdown = node_stop_code == 0
        require(clean_shutdown, 'Node runtime did not stop cleanly')
        node_process = None
        FIXTURE_STAGE = 'reacquire-runtime-lock'
        released_fd = runtime_lock.acquire(str(data_root))
        FIXTURE_STAGE = 'close-reacquired-lock'
        os.close(released_fd)
        FIXTURE_STAGE = 'exercise-inherited-descendant-lock'
        descendant_result = exercise_descendant_lock(data_root)
        FIXTURE_STAGE = 'emit-result'
        print(json.dumps({
            'ok': True,
            'expected_receiver_gate_failure': True,
            'receiver_exit_code': receiver_code,
            'node_shutdown_exit_code': node_stop_code,
            'node_uid': NODE_UID,
            'receiver_uid': RECEIVER_UID,
            'lock_file_uid': lock_stat.st_uid,
            'lock_file_mode': oct(lock_stat.st_mode & 0o777),
            'node_retained_lock_after_supervisor_close': node_owns_lock,
            'lock_reacquired_after_shutdown': True,
            'stubborn_descendant': descendant_result,
            'receiver_reason': 'unported_runtime_readiness',
        }, separators=(',', ':')))
        return 0
    finally:
        if receiver_process is not None and receiver_process.poll() is None:
            try:
                group = os.getpgid(receiver_process.pid)
                if group == receiver_process.pid:
                    os.killpg(group, signal.SIGTERM)
                else:
                    receiver_process.terminate()
            except ProcessLookupError:
                pass
            try:
                receiver_process.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    group = os.getpgid(receiver_process.pid)
                    if group == receiver_process.pid:
                        os.killpg(group, signal.SIGKILL)
                    else:
                        receiver_process.kill()
                except ProcessLookupError:
                    pass
                receiver_process.wait(timeout=5)
        if node_process is not None:
            try:
                stop_node(node_process)
            except (OSError, subprocess.SubprocessError):
                pass
        if release_fd >= 0:
            os.close(release_fd)
        if receiver_capture is not None:
            receiver_capture.pipe.close()
        if node_capture is not None:
            node_capture.pipe.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - sanitized fixture failure summary
        # Emit only exception class and safe fixture stage, never provider/config data.
        error_number = getattr(error, 'errno', None)
        print(
            f'COMPOSED_FAILURE_FIXTURE_FAILED stage={FIXTURE_STAGE} '
            f'type={type(error).__name__} errno={error_number!r}',
            file=sys.stderr,
        )
        raise SystemExit(1)
