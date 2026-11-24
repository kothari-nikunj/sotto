"""tools/railway-ci.sh against a fake `railway` and `gh`: a hung poll must not lose a passed job."""
import os
import pty
import select
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(
    not (ROOT / 'tools/railway-ci.sh').is_file(),
    reason='the private Railway runner client does not ship in the public tree')

FAKE_RAILWAY = r'''#!/usr/bin/env bash
state="$FAKE_STATE"
while [ "$#" -gt 0 ] && [ "$1" != -- ]; do shift; done
shift
command="$*"
case "$command" in
  *src.bundle*|bash\ -s*) cat > /dev/null ;;
  cat\ */exit)
    count=$(( $(cat "$state/polls" 2>/dev/null || echo 0) + 1 ))
    echo "$count" > "$state/polls"
    if [ "$count" = 1 ]; then
      sleep 60 &
      echo "$!" > "$state/ssh-child"
      wait
    fi
    echo expired ;;
  cat\ */result) echo 0 ;;
  rm\ -rf*) echo removed >> "$state/removed" ;;
esac
'''

FAKE_GIT = r'''#!/usr/bin/env bash
case "$1" in
  rev-parse) echo 0123456789abcdef0123456789abcdef01234567 ;;
  bundle) echo fixture ;;
esac
'''

FAKE_GH = r'''#!/usr/bin/env bash
if [ "$1" = repo ]; then echo example/repo; exit 0; fi
echo "$*" >> "$FAKE_STATE/gh"
'''


def test_a_hung_poll_and_an_expired_grace_still_report_the_passed_job(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, body in (('railway', FAKE_RAILWAY), ('gh', FAKE_GH), ('git', FAKE_GIT)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ, PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}', FAKE_STATE=str(tmp_path),
               RAILWAY_CI_POLL_SECONDS='1', RAILWAY_CI_REMOTE_SECONDS='2')
    with subprocess.Popen(['bash', str(ROOT / 'tools/railway-ci.sh'), 'HEAD'], cwd=ROOT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=12)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    assert process.returncode == 0, stdout + stderr
    assert 'state=success' in (tmp_path / 'gh').read_text()
    assert 'the runner released the slot before this client read the result' in stdout
    assert (tmp_path / 'removed').exists()


def test_the_client_contract_skips_without_the_private_runner_script(tmp_path):
    public = tmp_path / 'public'
    tests = public / 'sotto-chief-of-staff/tests'
    tests.mkdir(parents=True)
    copied = tests / Path(__file__).name
    copied.write_text(Path(__file__).read_text())
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q',
                             f'{copied}::test_a_hung_poll_and_an_expired_grace_still_report_the_passed_job'],
                            cwd=public, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '1 skipped' in result.stdout


@pytest.mark.parametrize(('command', 'expected'), (('exit 0', 0), ('exit 7', 7), ('kill -TERM $$', 143)))
def test_short_read_preserves_the_command_exit_code(tmp_path, command, expected):
    body = (ROOT / 'tools/railway-ci.sh').read_text().split('remote_read() {', 1)[1].split('\n}', 1)[0]
    fake = tmp_path / 'railway'
    fake.write_text(f'#!/bin/sh\n{command}\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    result = subprocess.run(['bash', '-c', f'remote_seconds=2; project=x; environment=x; service=x; {body}'],
                            env=dict(os.environ, PATH=f'{tmp_path}{os.pathsep}{os.environ["PATH"]}'), timeout=10)
    assert result.returncode == expected


@pytest.mark.parametrize('interrupt', ('HUP', 'INT', 'TERM'))
def test_interrupting_the_read_wrapper_stops_its_ssh_child(tmp_path, interrupt):
    body = (ROOT / 'tools/railway-ci.sh').read_text().split('remote_read() {', 1)[1].split('\n}', 1)[0]
    fake = tmp_path / 'railway'
    fake.write_text('#!/bin/sh\nsleep 60 &\necho "$PPID $!"\nwait\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ, PATH=f'{tmp_path}{os.pathsep}{os.environ["PATH"]}')
    with subprocess.Popen(['bash', '-c', f'remote_seconds=2; project=x; environment=x; service=x; {body}'],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          start_new_session=True) as process:
        try:
            wrapper, child = map(int, process.stdout.readline().split())
            requested = getattr(signal, f'SIG{interrupt}')
            os.kill(wrapper, requested)
            process.communicate(timeout=10)
            assert process.returncode == 128 + requested
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(child, 0)
                    status = Path(f'/proc/{child}/stat')
                    if status.is_file() and status.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                        break  # The CI subreaper collects dead descendants after the job ends.
                except (ProcessLookupError, FileNotFoundError):
                    break
                time.sleep(0.02)
            else:
                pytest.fail('SSH child survived the interrupted read')
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def test_short_read_does_not_read_the_callers_controlling_terminal(tmp_path):
    body = (ROOT / 'tools/railway-ci.sh').read_text().split('remote_read() {', 1)[1].split('\n}', 1)[0]
    fake = tmp_path / 'railway'
    fake.write_text('#!/bin/sh\nread ignored\necho complete\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ, PATH=f'{tmp_path}{os.pathsep}{os.environ["PATH"]}')
    pid, terminal = pty.fork()
    if pid == 0:
        os.execvpe('bash', ['bash', '-c',
                           f'remote_seconds=2; project=x; environment=x; service=x; {body}'], env)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            exited, status = os.waitpid(pid, os.WNOHANG)
            if exited:
                assert os.waitstatus_to_exitcode(status) == 0
                break
            if select.select([terminal], [], [], 0.02)[0]:  # macOS holds the exit until the output is read
                try:
                    os.read(terminal, 1024)
                except OSError:
                    pass
        else:
            pytest.fail('terminal-attached read did not finish')
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        os.close(terminal)
