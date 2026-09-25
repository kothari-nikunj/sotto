"""Keep Hermes implementation details out of Sotto chat channels."""
from pathlib import Path
import sys

import yaml


def quiet_display(settings):
    settings.update(show_reasoning=False, memory_notifications='off')
    # Preserve footer field choices, but do not display model/context/path metadata.
    footer = settings.get('runtime_footer')
    settings['runtime_footer'] = {**(footer if isinstance(footer, dict) else {}), 'enabled': False}


def reconcile(home, channel='telegram'):
    path = Path(home) / 'config.yaml'
    config = yaml.safe_load(path.read_text()) if path.exists() else {}
    config = config or {}
    # The pinned gateway reads this before composing redirect/steer/queue/interrupt notices,
    # after it has routed the correction. Silence the acknowledgment, not the owner's input.
    display = config.setdefault('display', {})
    display['busy_ack_enabled'] = False
    quiet_display(display)
    # Explicit channel overrides otherwise win over the global display settings.
    for settings in display.get('platforms', {}).values():
        quiet_display(settings)
    config.setdefault('compression', {})['progress_notices'] = False
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
    print('[sotto] gateway display details stay in operator logs')
