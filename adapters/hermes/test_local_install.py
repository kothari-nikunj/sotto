"""Fresh-install contracts without downloads, credentials or changing the user's Hermes."""
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('local_preflight', HERE / 'local_preflight.py')
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


@pytest.fixture
def bootstrap(tmp_path):
    """A tiny real Git runtime with reviewed gateway fixtures; only the download is replaced."""
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'gateway').mkdir()
    compat = (HERE / 'provider_error_compat.py').read_text()
    for name, fixture, constant in (
        ('run.py', 'provider_error_pinned_excerpt.py.txt', 'PINNED_SOURCE_SHA256'),
        ('run_turn_runner.py', 'provider_error_turn_runner_excerpt.py.txt', 'PINNED_TURN_RUNNER_SHA256'),
    ):
        data = (HERE / fixture).read_bytes()
        (source / 'gateway' / name).write_bytes(data)
        compat = re.sub(rf'({constant}\s*=\s*)[\'"][a-f0-9]+[\'"]',
                        rf'\g<1>"{hashlib.sha256(data).hexdigest()}"', compat)
    git(source, 'init', '-q')
    git(source, 'add', '.')
    git(source, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        '-c', 'commit.gpgsign=false', 'commit', '-qm', 'runtime fixture')
    adapter = tmp_path / 'adapter'
    adapter.mkdir()
    shutil.copy(HERE / 'install-runtime.sh', adapter)
    (adapter / 'provider_error_compat.py').write_text(compat)
    (adapter / 'hermes.commit').write_text(git(source, 'rev-parse', 'HEAD') + '\n')
    (adapter / 'hermes-install.sh').write_text('''set -eu
printf '%s\\n' "$@" > "$TEST_ARGS"
while [ "$#" -gt 0 ]; do
  case "$1" in --dir) dest="$2"; shift;; esac
  shift
done
git clone -q "$TEST_SOURCE" "$dest"
''')
    bins = tmp_path / 'bin'
    bins.mkdir()
    # Controlled PATH cannot find the operator's own Hermes CLI.
    for name in ('bash', 'git', 'dirname', 'tr', 'grep'):
        (bins / name).symlink_to(shutil.which(name))
    python = bins / 'python3'
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    env = {**os.environ, 'HOME': str(tmp_path / 'home'), 'PATH': str(bins),
           'HERMES_HOME': str(tmp_path / 'home' / '.hermes'),
           'HERMES_INSTALL_DIR': str(tmp_path / 'runtime'),
           'TEST_ARGS': str(tmp_path / 'args'), 'TEST_SOURCE': str(source)}
    return adapter, env, bins


def run_bootstrap(fixture):
    adapter, env, _ = fixture
    return subprocess.run(['/bin/bash', str(adapter / 'install-runtime.sh')],
                          env=env, text=True, capture_output=True)


def test_fresh_install_pins_and_checks_runtime(bootstrap):
    adapter, env, _ = bootstrap
    result = run_bootstrap(bootstrap)
    assert result.returncode == 0, result.stderr
    args = Path(env['TEST_ARGS']).read_text().splitlines()
    assert args[args.index('--commit') + 1] == (adapter / 'hermes.commit').read_text().strip()
    assert '--force-commit' in args
    assert '--skip-setup' in args
    # Rerunning verifies the same source without invoking the upstream updater.
    Path(env['TEST_ARGS']).unlink()
    assert run_bootstrap(bootstrap).returncode == 0
    assert not Path(env['TEST_ARGS']).exists()


def test_existing_unrelated_cli_is_not_replaced(bootstrap):
    _, env, bins = bootstrap
    cli = bins / 'hermes'
    cli.write_text('#!/bin/sh\nexit 0\n')
    cli.chmod(0o755)
    result = run_bootstrap(bootstrap)
    assert result.returncode != 0
    assert 'Nothing was changed' in result.stderr
    assert cli.read_text() == '#!/bin/sh\nexit 0\n'
    assert not Path(env['TEST_ARGS']).exists()


def test_installer_cannot_report_success_at_wrong_commit(bootstrap):
    adapter, _, _ = bootstrap
    (adapter / 'hermes.commit').write_text('0' * 40 + '\n')
    result = run_bootstrap(bootstrap)
    assert result.returncode != 0
    assert 'did not reach' in result.stderr


def test_launcher_outside_path_is_not_overwritten(bootstrap):
    _, env, _ = bootstrap
    launcher = Path(env['HOME']) / '.local/bin/hermes'
    launcher.parent.mkdir(parents=True)
    launcher.write_text('existing unrelated launcher')
    assert run_bootstrap(bootstrap).returncode != 0
    assert launcher.read_text() == 'existing unrelated launcher'
    assert not Path(env['TEST_ARGS']).exists()


def test_matching_checkout_does_not_authorize_an_unrelated_active_launcher(bootstrap):
    assert run_bootstrap(bootstrap).returncode == 0
    _, env, bins = bootstrap
    args = Path(env['TEST_ARGS'])
    args.unlink()
    launcher = bins / 'hermes'
    launcher.write_text('#!/bin/sh\nexit 0\n')
    launcher.chmod(0o755)
    result = run_bootstrap(bootstrap)
    assert result.returncode != 0
    assert 'Cannot verify' in result.stderr
    assert not args.exists()


def test_existing_runtime_with_modified_gateway_fails_without_updating(bootstrap):
    assert run_bootstrap(bootstrap).returncode == 0
    _, env, _ = bootstrap
    args = Path(env['TEST_ARGS'])
    args.unlink()
    gateway = Path(env['HERMES_INSTALL_DIR']) / 'gateway/run.py'
    gateway.write_text('unreviewed gateway\n')
    result = run_bootstrap(bootstrap)
    assert result.returncode != 0
    assert not args.exists()
    assert gateway.read_text() == 'unreviewed gateway\n'


def test_preflight_checks_all_direct_runtime_pins(tmp_path, monkeypatch):
    (tmp_path / 'requirements.in').write_text('# Runtime\npyyaml==6.0.1\nPillow==12.3.0\n')
    monkeypatch.setattr(preflight, 'version', lambda name: {'pyyaml': '6.0.1', 'Pillow': '12.3.0'}[name])
    assert preflight.check(tmp_path) == []
    monkeypatch.setattr(preflight, 'version', lambda name: '0.0')
    assert len(preflight.check(tmp_path)) == 2


def test_missing_dependencies_fail_before_hermes_or_config_writes(tmp_path):
    bins = tmp_path / 'bin'
    bins.mkdir()
    python = bins / 'python3'
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
    python.chmod(0o755)
    for name in ('dirname',):
        (bins / name).symlink_to(shutil.which(name))
    home = tmp_path / 'home'
    home.mkdir()
    result = subprocess.run(['/bin/bash', str(HERE / 'install.sh')], text=True,
                            capture_output=True, env={**os.environ, 'PATH': str(bins),
                            'HOME': str(home), 'HERMES_HOME': str(home / '.hermes')})
    assert result.returncode != 0
    assert 'Python environment is not ready' in result.stderr
    assert '--require-hashes' in result.stderr
    assert not (home / '.hermes').exists()


def test_local_wiring_installs_bridge_and_schedules_without_a_receiver(bootstrap, tmp_path):
    """Exercise the real adapter with a receipt-capable fake CLI and isolated HOME."""
    import json  # noqa: PLC0415
    import yaml  # noqa: PLC0415

    assert run_bootstrap(bootstrap).returncode == 0
    fixture_adapter, env, bins = bootstrap
    root = tmp_path / 'sotto'
    adapter = root / 'adapters/hermes'
    adapter.mkdir(parents=True)
    for name in ('install.sh', 'local_preflight.py', 'notification_config.py', 'web_config.py',
                 'web_provider.py', 'configure_mcp.py', 'sotto.bundle.yaml', 'sotto-persona.md',
                 'crons.json', 'hermes.commit'):
        shutil.copy(HERE / name, adapter)
    shutil.copy(fixture_adapter / 'provider_error_compat.py', adapter)
    shutil.copy(HERE.parents[1] / 'requirements.in', root)
    (root / 'sotto-chief-of-staff').mkdir()
    (root / 'sotto-chief-of-staff/SKILL.md').write_text('fixture skill')
    calls = tmp_path / 'calls.jsonl'
    cli = bins / 'hermes'
    cli.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
log = Path({str(calls)!r})
args = sys.argv[1:]
if args == ['send', '--help']:
    print('--json')
elif args == ['cron', 'list']:
    if log.exists():
        print(log.read_text())
else:
    with log.open('a') as f:
        f.write(json.dumps(args) + '\\n')
''')
    cli.chmod(0o755)
    bridge = tmp_path / 'sotto-bridged'
    bridge.write_text('#!/bin/sh\nexit 0\n')
    bridge.chmod(0o755)
    env.update(PATH=f"{bins}:/usr/bin:/bin", SOTTO_BRIDGE_BIN=str(bridge),
               BRIDGE_TOKEN='', SOTTO_BRIDGE_TOKEN='', SOTTO_CRON_DELIVER='telegram',
               SOTTO_PROACTIVE='1', SOTTO_DIGEST='1')
    result = subprocess.run(['/bin/bash', str(adapter / 'install.sh')],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    home = Path(env['HERMES_HOME'])
    config = yaml.safe_load((home / 'config.yaml').read_text())
    assert config['mcp_servers']['sotto-local']['command'] == str(bridge)
    assert '--allow-send' not in config['mcp_servers']['sotto-local'].get('args', [])
    assert (home / 'skills/sotto/SKILL.md').read_text() == 'fixture skill'
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    crons = [c for c in commands if c[:2] == ['cron', 'create']]
    expected = json.loads((adapter / 'crons.json').read_text())
    assert len(crons) == len(expected)
    assert {c[c.index('--name') + 1] for c in crons} == {j['name'] for j in expected}
    assert all(c[c.index('--deliver') + 1] == 'telegram' for c in crons)
    assert not any(c[:3] == ['config', 'set', 'model'] for c in commands)
    # Existing channel and schedules survive rerunning the actual installer.
    env.pop('SOTTO_CRON_DELIVER')
    again = subprocess.run(['/bin/bash', str(adapter / 'install.sh')],
                           env=env, text=True, capture_output=True)
    assert again.returncode == 0, again.stderr
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len([c for c in commands if c[:2] == ['cron', 'create']]) == len(crons)
    assert 'SOTTO_CRON_DELIVER=telegram' in (home / '.env').read_text()
