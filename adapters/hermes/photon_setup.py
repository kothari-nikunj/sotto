"""Install one shared Photon plugin for Cloud and self-hosted container runtimes."""
from pathlib import Path
import shutil
import sys

import yaml


def install(home):
    home = Path(home)
    plugin = home / 'plugins/photon-platform'
    plugin.mkdir(parents=True, exist_ok=True)
    shutil.copytree(Path(__file__).with_name('sotto_photon'), plugin, dirs_exist_ok=True)
    shutil.copyfile(home / 'skills/sotto/_shared/lib/chatfmt.py', plugin / 'chatfmt.py')
    path = home / 'config.yaml'
    config = yaml.safe_load(path.read_text()) if path.exists() else {}
    config = config or {}
    plugins = config.setdefault('plugins', {})
    enabled = plugins.setdefault('enabled', [])
    if 'photon-platform' not in enabled:
        enabled.append('photon-platform')
    plugins['disabled'] = [name for name in plugins.get('disabled', []) if name != 'photon-platform']
    tmp = path.with_suffix('.tmp')
    tmp.write_text(yaml.safe_dump(config, sort_keys=False)); tmp.chmod(0o600); tmp.replace(path)
    # Keep the launcher policy authoritative even if Hermes reloads a saved .env.
    envfile = home / '.env'
    if envfile.exists():
        lines = [line for line in envfile.read_text().splitlines()
                 if line.split('=', 1)[0] != 'PHOTON_STREAM_SILENCE_PROBE_MS']
    else:
        lines = []
    envfile.write_text('\n'.join(lines + ['PHOTON_STREAM_SILENCE_PROBE_MS=0']) + '\n')
    envfile.chmod(0o600)


if __name__ == '__main__':
    install(sys.argv[1])
    print('[sotto] shared Photon behavior installed; idle-only restarts disabled')
