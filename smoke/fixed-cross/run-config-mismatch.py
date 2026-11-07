"""A configured-identity mismatch must never start receiver workers or HTTP."""
import json
import os
import subprocess
import sys
import time
from urllib.request import urlopen

from run import (DATA, NODE_CONFIG, NODE_PORT, NODE_UID, RECEIVER_PORT,
                 read_ready, require, stage_inputs, stop, runtime_lock)


def main():
    _data, _config, handoff = stage_inputs()
    lock = runtime_lock.acquire(str(DATA))
    node = receiver = None
    started = time.monotonic()
    try:
        node = subprocess.Popen(
            ['/usr/local/bin/node', '/fixture/node-harness.mjs', str(NODE_CONFIG)],
            env={**dict(handoff.node_env), 'MASTRA_TELEMETRY_DISABLED': '1'},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, user=NODE_UID, group=NODE_UID,
            extra_groups=(), pass_fds=(lock,), close_fds=True)
        receiver_env = dict(handoff.receiver_env)
        receiver_env['PHOTON_PROJECT_SECRET'] = 'synthetic-different-project-secret'
        receiver = subprocess.Popen(
            list(handoff.receiver_argv), env=receiver_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(lock,), close_fds=True)
        read_ready(node)
        deadline = time.monotonic() + 37
        while receiver.poll() is None and time.monotonic() < deadline:
            try:
                with urlopen(f'http://127.0.0.1:{RECEIVER_PORT}/health', timeout=0.2):
                    raise RuntimeError('mismatched receiver exposed health before identity check')
            except OSError:
                pass
            time.sleep(0.1)
        require(receiver.poll() is not None, 'mismatched receiver did not fail within bounded retry')
        detail = receiver.stderr.read(256).decode('utf-8', 'replace')
        require('managed Photon fixed procedure profile unavailable' in detail,
                'receiver failed for a reason other than configured identity')
        require(node.poll() is None, 'synthetic Node died during mismatch test')
        print(json.dumps({'ok': True, 'configured_mismatch_held': True,
                          'receiver_health_exposed': False,
                          'elapsed_s': round(time.monotonic() - started, 2),
                          'node_port': NODE_PORT}))
    finally:
        stop(receiver)
        stop(node)
        os.close(lock)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- fixture boundary reports bounded synthetic failures.
        print('fixed-mismatch failed: ' + type(error).__name__ + ': ' + str(error)[:120],
              file=sys.stderr)
        raise SystemExit(1)
