"""Real child-process contracts; no Hermes, network or production state is used."""
from pathlib import Path
import os
import shlex
import subprocess
import sys
import time

import pytest

HERE = Path(__file__).parent


def test_gateway_does_not_inherit_workload_renewal_credential(tmp_path):
    """Execute the shipped launch line against a fake gateway, with a control credential set."""
    launcher = (HERE / 'start.sh').read_text()
    command = next(line for line in launcher.splitlines()
                   if 'process_group.py hermes gateway &' in line).strip().removesuffix('&').strip()
    command = command.replace('/app/adapters/hermes/process_group.py', shlex.quote(str(HERE / 'process_group.py')))
    gateway = tmp_path / 'hermes'
    gateway.write_text('#!/usr/bin/env python3\nimport os\nprint("SOTTO_CONTROL_TOKEN" in os.environ)\n')
    gateway.chmod(0o700)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'],
               SOTTO_CONTROL_TOKEN='<test-control-token>')
    result = subprocess.run(['bash', '-c', command], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == 'False'
    assert 'RECEIVER_PID=$!' in launcher
    assert 'sotto_install_traps' in launcher


def launch(tmp_path, failing='receiver', code=7, gateway=True):
    child = tmp_path / 'child.py'
    child.write_text('''import signal, sys, time
from pathlib import Path
import os
name, root, failing, code = sys.argv[1:]
def stop(*_):
    Path(root, name + '.stopped').write_text('term')
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
Path(root, name + '.ready').write_text('ready')
if name == failing:
    time.sleep(0.6)
    sys.exit(int(code))
while True: time.sleep(0.1)
''')
    def command(name):
        return shlex.join([sys.executable, str(HERE / 'process_group.py'), sys.executable,
                           str(child), name, str(tmp_path), failing, str(code)])
    script = ('set -euo pipefail\nsource ' + shlex.quote(str(HERE / 'supervise.sh'))
              + '\nsotto_install_traps\n' + command('receiver') + ' &\nRECEIVER_PID=$!\n')
    if gateway:
        script += command('gateway') + ' &\nGW_PID=$!\n'
    script += 'sotto_supervise\n'
    return subprocess.Popen(['bash', '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.parametrize('failing,code,gateway', [('receiver', 7, True), ('gateway', 8, True),
                                               ('receiver', 7, False), ('receiver', 0, False)])
def test_failure_status_and_sibling_shutdown(tmp_path, failing, code, gateway):
    process = launch(tmp_path, failing, code, gateway)
    try:
        out, err = process.communicate(timeout=6)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == (code or 1), (out, err)
    if gateway:
        sibling = 'gateway' if failing == 'receiver' else 'receiver'
        assert (tmp_path / (sibling + '.stopped')).exists()


def test_intentional_shutdown_stops_both_groups(tmp_path):
    process = launch(tmp_path, failing='none')
    try:
        deadline = time.monotonic() + 5
        while len(list(tmp_path.glob('*.ready'))) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(list(tmp_path.glob('*.ready'))) == 2
        process.terminate()
        out, err = process.communicate(timeout=6)
        assert process.returncode == 0, (out, err)
        assert {p.stem for p in tmp_path.glob('*.stopped')} == {'receiver', 'gateway'}
    finally:
        if process.poll() is None:
            process.kill()
