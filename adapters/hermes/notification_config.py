"""Keep Hermes lifecycle notices in operator logs across Sotto chat channels."""
from pathlib import Path
import sys

import yaml


def reconcile(home, channel='telegram'):
    path = Path(home) / 'config.yaml'
    config = yaml.safe_load(path.read_text()) if path.exists() else {}
    config = config or {}
    platforms = config.setdefault('platforms', {})
    names = {'telegram', 'photon', *platforms}
    primary = channel.split(':', 1)[0]
    if primary and primary != 'local':
        names.add(primary)
    for name in sorted(names):
        platforms.setdefault(name, {})['gateway_restart_notification'] = False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(yaml.safe_dump(config, sort_keys=False))
    temporary.chmod(0o600)
    temporary.replace(path)


if __name__ == '__main__':
    reconcile(*sys.argv[1:])
    print('[sotto] gateway lifecycle notices stay in operator logs')
