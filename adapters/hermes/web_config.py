"""Install the shared Sotto research backend for both Hermes deployment modes."""
from pathlib import Path
import sys

import yaml


def reconcile(home):
    home = Path(home)
    plugin = home / 'plugins/sotto-web'
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / '__init__.py').write_text(Path(__file__).with_name('web_provider.py').read_text())
    (plugin / 'plugin.yaml').write_text(yaml.safe_dump({
        'name': 'sotto-web', 'version': '1.0.0', 'description': 'Sotto shared web research',
        'kind': 'backend', 'provides_web_providers': ['sotto']}))
    path = home / 'config.yaml'
    config = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    plugins = config.setdefault('plugins', {})
    plugins['enabled'] = list(dict.fromkeys([*(plugins.get('enabled') or []), 'sotto-web']))
    plugins['disabled'] = [p for p in plugins.get('disabled', []) if p != 'sotto-web']
    config.setdefault('web', {}).update(search_backend='sotto', extract_backend='sotto',
                                        keyless_rescue=False, keyless_fallback=False)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(yaml.safe_dump(config, sort_keys=False))
    temporary.chmod(0o600)
    temporary.replace(path)


if __name__ == '__main__':
    reconcile(sys.argv[1])
    print('[sotto] Hermes web tools use shared Sotto research')
