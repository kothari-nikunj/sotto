#!/usr/bin/env python3
"""Offline Linux image gate: immutable product identity, writable tenant state.

Runs as root in the built image against a disposable fixture, never /data. The
probe drops to the real managed UID and loads the pinned Hermes prompt reader.
"""
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile

from managed_config import reconcile
from managed_identity import protect, verify_home

HERE = Path(__file__).resolve().parent
HERMES = Path('/usr/local/lib/hermes-agent')

PROBE = r'''
import os
from pathlib import Path
import sys

data, home, alias = map(Path, sys.argv[1:4])
expected = Path(sys.argv[4]).read_text()
sys.path.insert(0, '/usr/local/lib/hermes-agent')
from hermes_cli.config import ensure_hermes_home, load_config
from agent.prompt_builder import load_soul_md

# Normal Hermes config/auth initialization must not remove the sticky boundary
# or make the home inaccessible to later worker processes.
ensure_hermes_home()
load_config()
from hermes_constants import secure_parent_dir
secure_parent_dir(home / 'auth.json')
assert load_soul_md() == expected.strip(), 'pinned Hermes did not load the product identity'

def denied(label, operation):
    try:
        operation()
    except PermissionError:
        return
    raise AssertionError(label + ' unexpectedly allowed')

soul = home / 'SOUL.md'
replacement = home / 'replacement.md'
replacement.write_text('replacement')
denied('rewrite', lambda: soul.write_text('replacement'))
denied('unlink', soul.unlink)
denied('rename identity', lambda: soul.rename(home / 'old-soul'))
denied('replace identity', lambda: replacement.replace(soul))
denied('chmod identity', lambda: soul.chmod(0o666))
denied('replace entire Hermes home', lambda: home.rename(data / 'old-hermes'))
denied('replace default-home alias', alias.unlink)
for directory in (data, home, alias.parent):
    denied('chmod protected directory', lambda directory=directory: directory.chmod(0o777))
for name in ('.sotto-runtime.lock', '.sotto-volume.json'):
    protected = data / name
    denied('unlink ' + name, protected.unlink)
    denied('replace ' + name, lambda protected=protected: replacement.replace(protected))
assert soul.read_text() == expected
for directory in ('sessions', 'memories'):
    target = home / directory / 'owner-state.txt'
    target.parent.mkdir(exist_ok=True)
    target.write_text('kept')
    target.with_suffix('.tmp').write_text('updated')
    target.with_suffix('.tmp').replace(target)
for relative in ('preferences.json', 'knowledge/master.md'):
    target = data / relative
    target.parent.mkdir(exist_ok=True)
    target.write_text('owner preference')
(home / 'new-runtime-state').write_text('writable')
assert (home / 'new-runtime-state').read_text() == 'writable'
print('identity protected; pinned Hermes and owner state writable')
'''


def main():
    if not sys.platform.startswith('linux') or os.geteuid() != 0:
        raise SystemExit('identity image gate requires Linux root and the managed runtime')
    account = pwd.getpwnam('sotto')
    with tempfile.TemporaryDirectory(prefix='sotto-identity-check-') as directory:
        root = Path(directory)
        root.chmod(0o755)
        data = root / 'data'
        home = data / 'hermes'
        home.mkdir(parents=True)
        owner_home = root / 'owner'
        owner_home.mkdir()
        alias = owner_home / '.hermes'
        alias.symlink_to(home, target_is_directory=True)
        for name in ('.sotto-runtime.lock', '.sotto-volume.json'):
            (data / name).write_text('fixture')
        outside = root / 'outside'
        outside.write_text('must not change')
        # These image-owned seed copies run before permissions are installed.
        # GNU cp -a alone follows an old volume's destination symlink.
        start = (HERE / 'start.sh').read_text()
        for command, source, destination in (
            ('cp -a --remove-destination /root/.hermes/skill-bundles/sotto.yaml',
             Path('/root/.hermes/skill-bundles/sotto.yaml'), home / 'skill-bundles/sotto.yaml'),
            ('cp -a --remove-destination /app/hermes-image-version.txt',
             Path('/app/hermes-image-version.txt'), home / '.image-version'),
        ):
            assert command in start
            destination.parent.mkdir(exist_ok=True)
            destination.symlink_to(outside)
            subprocess.run(['cp', '-a', '--remove-destination', str(source), str(destination)], check=True)
            assert outside.read_text() == 'must not change'
            assert not destination.is_symlink()
        # Old volumes can contain a customized identity or an agent-created symlink.
        # Migration replaces that directory entry, never follows it as root.
        (home / 'SOUL.md').symlink_to(outside)
        (home / 'SOUL.tmp').symlink_to(outside)
        verify_home(home)
        env = {'SOTTO_MODEL_PROXY_URL': 'https://fixture.invalid',
               'SOTTO_MODEL_PROXY_TOKEN': 'fixture', 'PHOTON_HOME_CHANNEL': '+15555550100',
               'PHOTON_ALLOWED_USERS': '+15555550100'}
        reconcile(home, env)
        expected = root / 'expected'
        expected.write_text((home / 'SOUL.md').read_text())
        assert outside.read_text() == 'must not change'
        assert not (home / 'SOUL.md').is_symlink()
        # Model the old deployment's ownership before the first protected boot.
        for path in (data, home, owner_home, alias, *home.rglob('*'),
                     data / '.sotto-runtime.lock', data / '.sotto-volume.json'):
            os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
        protect(data, home, alias)
        child_env = {**os.environ, 'HERMES_HOME': str(home), 'SOTTO_DATA': str(data),
                     'SOTTO_DEPLOYMENT_MODE': 'managed', 'HERMES_SKIP_UPDATE_CHECK': '1'}
        child_env.pop('HERMES_UID', None)
        child_env.pop('HERMES_GID', None)
        command = [sys.executable, str(HERE / 'managed_exec.py'), 'gateway',
                   str(HERMES / 'venv/bin/python'), '-c', PROBE,
                   str(data), str(home), str(alias), str(expected)]
        for _ in range(2):
            subprocess.run(command, env=child_env, check=True, timeout=60)
            # An image upgrade replaces product identity without replacing owner state.
            reconcile(home, env)
            for name in ('config.yaml', '.env'):
                os.chown(home / name, account.pw_uid, account.pw_gid)
            protect(data, home, alias)
            assert (data / 'knowledge/master.md').read_text() == 'owner preference'
            assert (data / 'preferences.json').read_text() == 'owner preference'
            assert (home / 'sessions/owner-state.txt').read_text() == 'updated'
        print(json.dumps({'identity_boundary': 'passed', 'runtime_uid': account.pw_uid,
                          'existing_volume_upgrade': 'passed'}))


if __name__ == '__main__':
    main()
