"""Private-PID/offline fixed-profile cross-process fixture; no provider or model calls."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

sys.path[:0] = ['/app/adapters/sotto']
import runtime_lock  # noqa: E402 -- packaged adapter paths are installed above.
import startup_config  # noqa: E402 -- packaged adapter paths are installed above.
import native_replacement  # noqa: E402 -- packaged adapter paths are installed above.
import fixed_five  # noqa: E402 -- packaged adapter paths are installed above.

NODE_UID = 10000
RECEIVER_UID = 10001
ROOT = Path('/data')
DATA = ROOT / 'tenant'
NODE_DIR = DATA / 'node-config'
NODE_CONFIG = NODE_DIR / 'config' / 'runtime-config.json'
NODE_PORT = 19981
RECEIVER_PORT = 19982


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def uid_call(uid, callback):
    child = os.fork()
    if child == 0:
        try:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
            callback()
            os._exit(0)
        except BaseException as error:  # noqa: BLE001 -- forked child must exit with fixture failure.
            print('fixture private operation: ' + type(error).__name__ + ': ' + str(error)[:100],
                  file=sys.stderr, flush=True)
            os._exit(1)
    _, code = os.waitpid(child, 0)
    require(os.WIFEXITED(code) and os.WEXITSTATUS(code) == 0, 'private staging failed')


def uid_cannot_read(uid, path):
    child = os.fork()
    if child == 0:
        try:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
            path.read_bytes()
            os._exit(1)
        except PermissionError:
            os._exit(0)
        except BaseException:  # noqa: BLE001 -- unexpected child failures have a distinct exit code.
            os._exit(2)
    _, code = os.waitpid(child, 0)
    require(os.WIFEXITED(code) and os.WEXITSTATUS(code) == 0,
            'Node unexpectedly accessed receiver-private gallery')


def gallery_boundary():
    artifact = 'a' * 24
    images = [DATA / 'cache' / 'visual-briefs' / artifact / f'{index:02d}.png'
              for index in range(1, 5)]
    def receiver_read():
        os.environ['SOTTO_RUNTIME_PROFILE'] = fixed_five.PROFILE
        spec = importlib.util.spec_from_file_location(
            'sotto_gallery_fixture', '/app/adapters/sotto/runtime_api.py')
        runtime_api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime_api)
        directory(DATA / 'cache', RECEIVER_UID)
        directory(DATA / 'cache' / 'visual-briefs', RECEIVER_UID)
        directory(images[0].parent, RECEIVER_UID)
        for index, path in enumerate(images):
            path.write_bytes(b'\x89PNG\r\n\x1a\nfixture-' + bytes([index]))
            path.chmod(0o600)
        found = runtime_api._read_private_gallery([str(path) for path in images], DATA)
        require(len(found) == 4 and all(item['bytes'].startswith(b'\x89PNG') for item in found),
                'receiver gallery read failed')
    uid_call(RECEIVER_UID, receiver_read)
    uid_cannot_read(NODE_UID, images[0])


def directory(path, uid, mode=0o700):
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    os.chown(path, uid, uid)
    path.chmod(mode)


def manifest():
    project = 'synthetic-project'
    return {
        'version': 1, 'deployment': 'managed', 'channel': 'photon',
        'tenantId': 'synthetic-tenant',
        'owner': {'accountId': 'synthetic-account', 'ownerId': '+15551234567'},
        'lineKey': 'physical-line-a', 'target': 'photon',
        'providerThreadId': 'imessage:iMessage;-;+15551234567~+15557654321',
        'photonLineNumber': '+15557654321',
        'stateDirectory': str(DATA / 'node-state'),
        'mediaRoot': str(DATA / 'node-media'),
        'instructions': 'Synthetic offline fixture only.',
        'runtime': {'port': NODE_PORT,
                    'bearerToken': 'runtime-api-fixture-token-0123456789'},
        'relay': {'receiverPort': RECEIVER_PORT,
                  'rootBearer': 'root-mcp-fixture-token-0123456789'},
        'modelProxy': {'origin': 'https://proxy.invalid',
                       'bearer': 'model-proxy-fixture-token-0123456789',
                       'chatModelId': 'chat-v1', 'nativeMediaModelId': 'media-v1'},
        'credentials': {'mode': 'cloud', 'photonProjectId': project,
                        'photonProjectSecret': 'photon-project-secret-fixture'},
        'activationReceipt': {
            'tenant_id': 'synthetic-tenant', 'owner': '+15551234567',
            'chat_id': 'iMessage;-;+15551234567', 'activated': True,
            'project_fingerprint': hashlib.sha256(
                ('sotto-photon-project-v1:' + project).encode()).hexdigest(),
        },
    }


def stable_identity(config):
    return {
        'channel': config['channel'], 'target': config['target'],
        'tenantId': config['tenantId'], 'accountId': config['owner']['accountId'],
        'ownerId': config['owner']['ownerId'], 'lineKey': config['lineKey'],
        'providerThreadId': config['providerThreadId'],
        'expectedProviderLine': None,
        'projectFingerprint': hashlib.sha256(
            ('sotto-photon-project-v1:' + config['credentials']['photonProjectId']).encode()).hexdigest(),
    }


def authority(config):
    return {
        'SOTTO_MCP_TOKEN': 'root-mcp-fixture-token-0123456789',
        'SOTTO_CONTROL_TOKEN': 'control-plane-fixture-token-0123456789',
        'SOTTO_MODEL_PROXY_URL': config['modelProxy']['origin'],
        'SOTTO_MODEL_PROXY_TOKEN': config['modelProxy']['bearer'],
        'SOTTO_TENANT_ID': config['tenantId'], 'SOTTO_DATA': str(DATA),
        'SOTTO_HOME': str(DATA / 'sotto-home'),
        'SOTTO_VOLUME_ID': 'synthetic-volume-id',
    }


def read_ready(process, timeout=20):
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    data = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            require(process.poll() is None, 'Node exited before ready')
            if not selector.select(0.1):
                continue
            chunk = os.read(process.stdout.fileno(), 1024)
            require(chunk, 'Node readiness pipe closed')
            data.extend(chunk)
            require(len(data) <= 4096, 'Node readiness exceeded bound')
            if b'\n' in data:
                first, _, _ = data.partition(b'\n')
                value = json.loads(first)
                require(value == {'ready': True, 'uid': NODE_UID, 'port': NODE_PORT},
                        'Node readiness identity changed')
                return
        raise RuntimeError('Node readiness timed out')
    finally:
        selector.close()


def stop(proc):
    if proc is None:
        return
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=3)


def stage_inputs():
    require(os.geteuid() == 0, 'fixture supervisor must be root')
    require(ROOT.is_dir(), 'private fixture volume missing')
    DATA.mkdir(mode=0o1771)
    os.chown(DATA, 0, RECEIVER_UID)
    DATA.chmod(0o1771)
    # Synthetic on-volume marker: it proves the supervisor compared the exact
    # private receipt, not that this disposable tmpfs is a managed mount.
    marker = DATA / '.sotto-volume.json'
    marker.write_text(json.dumps({'schema': 1, 'tenant_id': 'synthetic-tenant',
                                  'volume_id': 'synthetic-volume-id'}))
    marker.chmod(0o600)
    directory(DATA / 'sotto-home', RECEIVER_UID)
    directory(NODE_DIR, NODE_UID)
    directory(DATA / 'node-state', NODE_UID)
    directory(DATA / 'node-media', NODE_UID)
    gallery_boundary()
    data = manifest()
    config, projection = startup_config.render(data)
    uid_call(NODE_UID, lambda: startup_config.stage(NODE_DIR / 'config', config, projection))
    uid_cannot_read(RECEIVER_UID, NODE_CONFIG)
    expectation = startup_config.receiver_channel_expectation(config)
    uid_call(RECEIVER_UID, lambda: startup_config.stage_receiver_channel_expectation(
        DATA / 'runtime', expectation))
    uid_cannot_read(NODE_UID, DATA / 'runtime' / 'expected-channel.json')
    handoff = native_replacement.verified_handoff(
        data, authority(config), stable_identity=stable_identity(config),
        node_config_path=str(NODE_CONFIG), data_directory=str(DATA),
        skills_root='/app/sotto-skills', limited_profile=fixed_five.PROFILE)
    fixed_five.validate_offline(dict(handoff.receiver_env), native_uid=RECEIVER_UID)
    return data, config, handoff


def main():
    data, config, handoff = stage_inputs()
    lock = runtime_lock.acquire(str(DATA))
    node = receiver = None
    success = False
    try:
        started = time.monotonic()
        node_env = {**dict(handoff.node_env), 'MASTRA_TELEMETRY_DISABLED': '1'}
        node = subprocess.Popen(
            ['/usr/local/bin/node', '/fixture/node-harness.mjs', str(NODE_CONFIG)],
            env=node_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, user=NODE_UID,
            group=NODE_UID, extra_groups=(), pass_fds=(lock,), close_fds=True)
        receiver = subprocess.Popen(
            list(handoff.receiver_argv), env=dict(handoff.receiver_env),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(lock,), close_fds=True)
        receiver_started_after = round(time.monotonic() - started, 3)
        read_ready(node)
        node_ready_after = round(time.monotonic() - started, 3)
        if receiver.poll() is not None:
            detail = receiver.stderr.read(512).decode('utf-8', 'replace')
            raise RuntimeError('receiver exited during delayed Node startup at '
                               f'{node_ready_after}s: ' + detail[:200])
        deadline = time.monotonic() + 15
        healthy = False
        while time.monotonic() < deadline:
            if receiver.poll() is not None:
                detail = receiver.stderr.read(512).decode('utf-8', 'replace')
                raise RuntimeError('receiver exited before health at '
                                   f'{round(time.monotonic() - started, 3)}s: ' + detail[:200])
            try:
                with urlopen(f'http://127.0.0.1:{RECEIVER_PORT}/health', timeout=0.5) as response:
                    healthy = response.status == 200
                if healthy:
                    break
            except OSError:
                time.sleep(0.05)
        require(healthy, 'receiver health did not start')
        require(receiver.pid != node.pid, 'roles shared PID')
        status = Path(f'/proc/{receiver.pid}/status').read_bytes()
        uid_lines = [line for line in status.splitlines() if line.startswith(b'Uid:')]
        require(len(uid_lines) == 1 and int(uid_lines[0].split()[2]) == RECEIVER_UID,
                'managed receiver effective UID was not 10001')
        require(not os.environ.get('SOTTO_CONTROL_TOKEN'), 'fixture imported ambient control token')
        try:
            competing = runtime_lock.acquire(str(DATA))
        except RuntimeError:
            pass
        else:
            os.close(competing)
            raise RuntimeError('same tenant flock was not retained by live roles')
        success = True
    finally:
        stop(receiver)
        stop(node)
        os.close(lock)
    if success:
        reacquired = runtime_lock.acquire(str(DATA))
        os.close(reacquired)
        print(json.dumps({'ok': True, 'node_uid': NODE_UID,
                          'receiver_uid': RECEIVER_UID, 'health': 200,
                          'node_started_before_receiver': True,
                          'receiver_started_before_node_ready': True,
                          'receiver_started_after_s': receiver_started_after,
                          'node_ready_after_s': node_ready_after,
                          'lock_retained_and_reacquired_after_shutdown': True,
                          'gallery_private': True}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- top-level fixture failure is reported then exits.
        print('fixed-cross failed: ' + type(error).__name__ + ': ' + str(error)[:120],
              file=sys.stderr)
        raise SystemExit(1)
