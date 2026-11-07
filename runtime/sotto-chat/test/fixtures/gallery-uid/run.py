"""Root-only two-UID loopback fixture; no provider sockets or credentials."""
import hashlib
import json
import os
import pwd
import shutil
import signal
import selectors
import stat
import subprocess
import tempfile
from pathlib import Path

NODE_UID = 10000
RECEIVER_UID = 10001
BEARER = 'offline-gallery-fixture-token-not-a-credential-0001'
SUMMARY = 'synthetic offline gallery'
DISPATCH = 'gallery-uid-smoke'


def drop(uid):
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)


def expect(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    expect(os.geteuid() == 0, 'fixture requires a root test container for UID transitions')
    pwd.getpwuid(NODE_UID)
    pwd.getpwuid(RECEIVER_UID)
    fixture_root = Path(tempfile.mkdtemp(prefix='gallery-uid-'))
    fixture_root.chmod(0o755)
    node_proc = None
    try:
        receiver_root = fixture_root / 'receiver'
        data_root = receiver_root / 'data'
        artifact = data_root / 'cache' / 'visual-briefs' / ('a' * 24)
        artifact.mkdir(parents=True, mode=0o700)
        directories = (receiver_root, data_root, data_root / 'cache', data_root / 'cache' / 'visual-briefs', artifact)
        for directory in directories:
            directory.chmod(0o711)
        source_bytes = []
        paths = []
        for index in range(1, 5):
            body = b'\x89PNG\r\n\x1a\n' + b'uid-handoff-fixture-' + str(index).encode('ascii')
            path = artifact / f'{index:02d}.png'
            path.write_bytes(body)
            path.chmod(0o600)
            source_bytes.append(body)
            paths.append(path)
        for path in paths:
            os.chown(path, RECEIVER_UID, RECEIVER_UID)
        for directory in reversed(directories):
            os.chown(directory, RECEIVER_UID, RECEIVER_UID)
        for directory in reversed(directories):
            directory.chmod(0o700)
        hashes = [hashlib.sha256(body).hexdigest() for body in source_bytes]
        expected_payload_hash = hashlib.sha256(json.dumps(
            {'summary': SUMMARY, 'images': hashes}, separators=(',', ':'), ensure_ascii=False
        ).encode('utf-8')).hexdigest()

        node_state = fixture_root / 'node-state'
        node_state.mkdir(mode=0o700)
        os.chown(node_state, NODE_UID, NODE_UID)
        expected_dir = fixture_root / 'public'
        expected_dir.mkdir(mode=0o755)
        expected_file = expected_dir / 'expected.json'
        expected_file.write_text(json.dumps({'dispatch_id': DISPATCH, 'bearer': BEARER}, separators=(',', ':')))
        expected_file.chmod(0o444)
        node_script = '/fixture/node-server.mjs'
        forbidden_path = str(paths[0])
        node_env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/nonexistent', 'NODE_ENV': 'production', 'MASTRA_TELEMETRY_DISABLED': '1'}
        node_proc = subprocess.Popen(
            ['/usr/local/bin/node', node_script, str(node_state), forbidden_path, str(expected_file)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=node_env, preexec_fn=lambda: drop(NODE_UID),
        )
        ready = None
        with selectors.DefaultSelector() as selector:
            selector.register(node_proc.stdout, selectors.EVENT_READ)
            events = selector.select(timeout=20)
            if events:
                line = node_proc.stdout.readline()
                ready = json.loads(line) if line else None
        if node_proc.poll() is not None and ready is None:
            stderr = node_proc.stderr.read(4096) if node_proc.stderr else ''
            raise RuntimeError(f'Node fixture exited before ready ({node_proc.returncode}): {stderr[:1000]}')
        expect(isinstance(ready, dict) and ready.get('ready') is True and type(ready.get('port')) is int,
               'Node fixture did not publish readiness')

        sender_env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/nonexistent', 'PYTHONDONTWRITEBYTECODE': '1'}
        sender = subprocess.run(
            ['/usr/local/bin/python3', '-I', '-B', '/fixture/sender.py',
             '--adapter-root', '/app/adapters/sotto', '--port', str(ready['port']),
             '--data-root', str(data_root), '--bearer', BEARER],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=30, env=sender_env, preexec_fn=lambda: drop(RECEIVER_UID),
        )
        expect(sender.returncode == 0, f'receiver sender failed: {sender.stderr[:1000]}')
        response = json.loads(sender.stdout)
        expect(response['first'][0] is True and response['second'][0] is True,
               'receiver did not get both durable accepted responses')
        health = response['health']
        expect(health['directReadDenied'] is True, 'UID10000 could read receiver-owned file paths')
        expect(len(health['calls']) == 1, 'same dispatch replay contacted transport more than once')
        call = health['calls'][0]
        expect(call['text'] == SUMMARY, 'provider text did not preserve exact summary')
        media = call['media']
        expect([entry['filename'] for entry in media] == [f'{index:02d}.png' for index in range(1, 5)],
               'transport attachment order/names changed')
        expect([entry['sha256'] for entry in media] == hashes, 'transport bytes differ from receiver-owned sources')
        expect([entry['size'] for entry in media] == [len(body) for body in source_bytes],
               'transport attachment sizes differ from source')
        expect(health['row']['status'] == 'accepted' and
               health['row']['payload_hash'] == expected_payload_hash and
               health['row']['provider_message_id'] == 'fake-provider-gallery-id',
               'durable accepted dispatch/hash did not match transferred content')
        expect(response['first'][2]['acceptance'] == 'accepted' and
               response['second'][2]['acceptance'] == 'accepted', 'receipt acceptance was not preserved')
        for path in paths:
            mode = stat.S_IMODE(path.stat().st_mode)
            expect(mode == 0o600 and path.stat().st_uid == RECEIVER_UID,
                   'fixture changed receiver file ownership or mode')

        node_proc.send_signal(signal.SIGTERM)
        node_proc.wait(timeout=15)
        expect(node_proc.returncode == 0, 'Node fixture did not shut down cleanly')
        print(json.dumps({
            'result': 'pass', 'node_uid': NODE_UID, 'receiver_uid': RECEIVER_UID,
            'receiver_file_mode': '0600', 'node_direct_path_read': 'denied',
            'transferred_images': len(media), 'transport_calls_for_duplicate_dispatch': len(health['calls']),
            'hashes': hashes, 'payload_hash': expected_payload_hash,
            'receipt': 'accepted', 'network': 'loopback only',
        }, separators=(',', ':')))
    finally:
        if node_proc is not None and node_proc.poll() is None:
            node_proc.terminate()
            try:
                node_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                node_proc.kill()
                node_proc.wait(timeout=5)
        shutil.rmtree(fixture_root, ignore_errors=True)


if __name__ == '__main__':
    main()
