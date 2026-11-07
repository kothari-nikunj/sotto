"""Single-purpose, short-lived UID writer for trusted root supervisor projections."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

try:
    from . import fixed_five, startup_config
except ImportError:
    import fixed_five
    import startup_config

_MAX_BYTES = 256 * 1024


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate staging field')
        value[key] = item
    return value


def main(argv: list[str]) -> int:
    """Consume only the projection needed by this UID; no root bundle or env lookup."""
    try:
        if len(argv) != 2 or argv[1] not in ('node', 'receiver'):
            raise ValueError('staging role unavailable')
        raw = sys.stdin.buffer.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError('staging payload unavailable')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict) or not isinstance(value.get('tenantRoot'), str):
            raise ValueError('staging payload unavailable')
        root = Path(value['tenantRoot'])
        if (root != Path('/data') and root != Path('/data') / root.name) or root.name in ('.', '..'):
            raise ValueError('staging root unavailable')
        if argv[1] == 'node':
            if (set(value) != {'tenantRoot', 'config', 'receiverProjection', 'restart'}
                    or type(value['restart']) is not bool or os.geteuid() != 10000):
                raise ValueError('Node staging unavailable')
            # A same-epoch restart re-renders rotated credentials or a new release's config.
            startup_config.stage(root / 'node-config/config', value['config'],
                                 value['receiverProjection'], restart=value['restart'])
        else:
            if (set(value) not in ({'tenantRoot', 'expectation'},
                                   {'tenantRoot', 'expectation', 'telegramLink'})
                    or os.geteuid() != 10001):
                raise ValueError('receiver staging unavailable')
            startup_config.stage_receiver_channel_expectation(root / 'runtime',
                                                               value['expectation'])
            # Under the supervisor's flock and as the queue's own UID, roll back a hot
            # journal a killed receiver left, so the root read-only preflight can open it.
            fixed_five.recover_work_queue_journal(root)
            if 'telegramLink' in value:
                startup_config.stage_telegram_link(root, value['telegramLink'])
        return 0
    except (OSError, ValueError, UnicodeError, RecursionError, TypeError,
            startup_config.ManifestError):
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
