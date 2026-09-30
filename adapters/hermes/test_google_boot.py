"""Exercise the Google boot branch against a disposable home and helper."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time


BOOT = Path(__file__).with_name('start.sh')


def run_boot(tmp_path, *, saved_client=None, env_client=None, pending=None,
             auth_code='', connected=False, mode='self-host'):
    root = tmp_path / '.hermes'
    root.mkdir(parents=True)
    data = tmp_path / 'data'
    data.mkdir()
    client_path = root / 'google_client_secret.json'
    if saved_client is not None:
        client_path.write_text(json.dumps(saved_client))
    if pending is not None:
        (root / 'google_oauth_pending.json').write_text(json.dumps(pending))
    if connected:
        (root / 'google_token.json').write_text('fixture')
    (data / 'google-auth-url.txt').write_text('stale-authorization-url')
    helper = tmp_path / 'helper.py'
    helper.write_text(
        'import json, os, sys\n'
        'from pathlib import Path\n'
        'with Path(os.environ["GOOGLE_BOOT_CALLS"]).open("a") as f:\n'
        '    f.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        'if "--check" in sys.argv and os.environ["GOOGLE_BOOT_CONNECTED"] != "1":\n'
        '    sys.exit(1)\n'
    )
    boot = BOOT.read_text()
    lane = boot.split('# 3.7) Google Workspace auth.', 1)[1].split('# 3.8) Granola', 1)[0]
    lane = lane[lane.index('GAUTH_URL_FILE='):]
    lane = lane.replace('/app/adapters/hermes/google_setup.py', str(helper))
    lane = lane.replace('PYBIN=$(command -v python || command -v python3)',
                        'PYBIN=' + shlex.quote(sys.executable))
    calls_path = tmp_path / 'calls.jsonl'
    env = {**os.environ, 'HOME': str(tmp_path), 'HERMES_HOME': str(root),
           'SOTTO_DATA': str(data), 'SOTTO_DEPLOYMENT_MODE': mode,
           'GOOGLE_BOOT_CALLS': str(calls_path), 'GOOGLE_BOOT_CONNECTED': str(int(connected)),
           'GOOGLE_AUTH_CODE': auth_code, 'RAILWAY_PUBLIC_DOMAIN': 'sotto.example',
           'GOOGLE_OAUTH_CLIENT_JSON': json.dumps(env_client) if env_client else ''}
    result = subprocess.run(['bash', '-c', 'set -euo pipefail\n' + lane], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()] if calls_path.exists() else []
    assert not (data / 'google-auth-url.txt').exists()
    assert not any('--auth-url' in call for call in calls)
    assert 'localhost:1' not in result.stdout
    assert auth_code not in result.stdout if auth_code else True
    return result, calls, client_path


def test_saved_web_client_wins_over_stale_environment_and_boot_never_starts_consent(tmp_path):
    saved = {'web': {'client_id': 'wizard-client', 'client_secret': 'wizard-secret'}}
    stale = {'installed': {'client_id': 'old-env-client'}}
    result, calls, path = run_boot(tmp_path, saved_client=saved, env_client=stale)
    assert json.loads(path.read_text()) == saved
    assert calls == [['--check']]
    assert 'finish connecting on your Sotto setup page' in result.stdout


def test_env_client_seeds_only_when_no_saved_client(tmp_path):
    client = {'web': {'client_id': 'env-client', 'client_secret': 'env-secret'}}
    _, calls, path = run_boot(tmp_path, env_client=client)
    assert json.loads(path.read_text()) == client
    assert calls == [['--check'], ['--client-secret', str(path)]]


def test_existing_token_remains_connected_even_without_client_env(tmp_path):
    result, calls, path = run_boot(tmp_path, connected=True)
    assert calls == [['--check']]
    assert not path.exists()
    assert 'already connected' in result.stdout


def test_auth_code_is_never_exchanged_at_boot_even_with_old_pending_files(tmp_path):
    desktop = {'installed': {'client_id': 'legacy-client'}}
    # What main's helper and upstream Hermes setup.py wrote before browser callbacks.
    for name, pending in (
        ('main', {'state': 'state', 'code_verifier': 'verifier', 'created_at': time.time(),
                  'scopes': ['https://www.googleapis.com/auth/calendar'],
                  'redirect_uri': 'http://localhost:1'}),
        ('upstream', {'state': 'state', 'code_verifier': 'verifier',
                      'redirect_uri': 'http://localhost:1'}),
    ):
        result, calls, _ = run_boot(tmp_path / name, saved_client=desktop, pending=pending,
                                    auth_code='private-code')
        assert calls == [['--check']]
        assert 'GOOGLE_AUTH_CODE is no longer used' in result.stdout
        assert 'private-code' not in result.stdout + result.stderr


def test_managed_boot_leaves_google_to_the_cloud_broker(tmp_path):
    saved = {'web': {'client_id': 'cloud-client', 'client_secret': 'cloud-secret'}}
    result, calls, path = run_boot(tmp_path, saved_client=saved, connected=True, mode='managed',
                                   auth_code='private-code')
    assert calls == []
    assert json.loads(path.read_text()) == saved
    assert 'Google' not in result.stdout and 'setup page' not in result.stdout
