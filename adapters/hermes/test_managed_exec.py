import importlib.util
import os
from pathlib import Path
import pwd
import subprocess
import sys
import time

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('managed_exec_test', HERE / 'managed_exec.py')
managed_exec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(managed_exec)


def test_managed_image_uses_public_runtime_and_hands_root_artifacts_to_workload_uid():
    docker = (HERE.parents[1] / 'Dockerfile').read_text()
    start = (HERE / 'start.sh').read_text()
    assert 'UV_PYTHON_INSTALL_DIR=/usr/local/share/uv/python' in docker
    assert 'install -m 0755 /root/.local/bin/hermes /usr/local/bin/hermes' in docker
    assert 'runuser -u sotto -- env HOME=/home/sotto PATH=/usr/local/bin:/usr/bin:/bin hermes' in docker
    assert 'ENV PATH="/usr/local/bin:/usr/local/share/uv/bin:${PATH}"' in docker
    assert '/root/.local/bin:/root/.hermes/bin' not in docker
    final_handoff = 'find "$HSTATE" -xdev -user root -exec chown sotto:sotto {} +'
    assert start.index('python3 /app/adapters/hermes/web_config.py') < start.index(final_handoff)
    assert start.index(final_handoff) < start.index('managed_exec.py gateway hermes gateway')


def test_receiver_orders_vault_drop_privilege_and_nondumpable_before_runtime(monkeypatch):
    order = []
    monkeypatch.setattr(managed_exec, 'drop_user', lambda: order.append('uid'))
    monkeypatch.setattr(managed_exec, 'nondumpable', lambda: order.append('nondumpable'))
    monkeypatch.setattr(managed_exec.os, 'setsid', lambda: order.append('setsid'))
    monkeypatch.setattr(managed_exec.runpy, 'run_path', lambda *a, **k: order.append('runtime'))
    monkeypatch.setenv('SOTTO_CONTROL_TOKEN', 'secret')
    monkeypatch.setattr(sys, 'argv', list(sys.argv))
    original = sys.modules.pop('control_vault', None)
    try:
        managed_exec.main(['receiver', '/app/receiver.py'])
        assert order == ['setsid', 'uid', 'nondumpable', 'runtime']
        assert 'SOTTO_CONTROL_TOKEN' not in os.environ
        assert '/app' in sys.path
    finally:
        sys.modules.pop('control_vault', None)
        if original is not None:
            sys.modules['control_vault'] = original


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='release gate runs in the Linux image')
def test_linux_nondumpable_receiver_boundary_requires_container_identity(tmp_path):
    """Built-image gate: peer can share 0600 state but cannot inspect receiver or mutate code."""
    if os.geteuid() != 0 or not Path('/app/adapters/hermes/managed_exec.py').exists():
        pytest.skip('requires built container image as root')
    account = pwd.getpwnam('sotto')
    # pytest may create 0700 ancestors; the dropped child must be able to traverse its isolated
    # fixture directory so a path-permission failure cannot masquerade as a process boundary.
    for parent in (tmp_path.parent.parent, tmp_path.parent, tmp_path):
        parent.chmod(parent.stat().st_mode | 0o111)
    os.chown(tmp_path, account.pw_uid, account.pw_gid)
    shared = tmp_path / 'shared'
    shared.write_text('state')
    os.chown(shared, account.pw_uid, account.pw_gid)
    shared.chmod(0o600)
    pidfile = tmp_path / 'pid'
    receiver = tmp_path / 'receiver.py'
    receiver.write_text('import os,time\nopen(os.environ["PIDFILE"],"w").write(str(os.getpid()))\ntime.sleep(10)\n')
    receiver.chmod(0o644)
    env = {**os.environ, 'SOTTO_CONTROL_TOKEN': 'boundary-secret', 'PIDFILE': str(pidfile)}
    process = subprocess.Popen([sys.executable, '/app/adapters/hermes/managed_exec.py',
                                'receiver', str(receiver)], env=env)
    try:
        for _ in range(100):
            if pidfile.exists():
                break
            time.sleep(.02)
        pid = pidfile.read_text()
        probe = ('import os,sys; pid=sys.argv[1]; shared=sys.argv[2]; '
                 'ok=[]; '
                 '\ntry: open("/proc/"+pid+"/environ","rb").read(); ok.append("proc-readable")\nexcept OSError: pass; '
                 '\ntry: open("/app/runtime-boundary-probe","w").write("x"); ok.append("app-writable")\nexcept OSError: pass; '
                 '\nopen(shared,"a").write("-updated"); print(",".join(ok))')
        result = subprocess.run([sys.executable, '/app/adapters/hermes/managed_exec.py', 'gateway',
                                 sys.executable, '-c', probe, pid, str(shared)],
                                env=env, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0 and result.stdout.strip() == ''
        assert shared.read_text() == 'state-updated' and shared.stat().st_mode & 0o777 == 0o600
    finally:
        process.terminate()
        process.wait(timeout=5)
