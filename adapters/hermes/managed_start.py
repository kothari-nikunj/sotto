"""Provisioner-only first launch of one managed tenant on its exact /data volume.

This is an explicit Railway start command for a new source-mode tenant. Ordinary
Docker startup and start.sh remain verify-only. After the marker is initialized
or verified, this process is replaced by the existing locked runtime.
"""

import argparse
import os
from pathlib import Path
import re

from managed_volume import MARKER, initialize, verify

DATA = Path('/data')
PYTHON = '/usr/local/bin/python3'
RUNTIME = ('python3', '/app/adapters/hermes/runtime_lock.py',
           '/app/adapters/hermes/start.sh')


def launch(tenant, volume, *, environ=None, data=DATA, ismount=None, execv=None):
    environ = os.environ if environ is None else environ
    ismount = os.path.ismount if ismount is None else ismount
    execv = os.execv if execv is None else execv
    if (not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', tenant or '')
            or not re.fullmatch(r'[0-9a-f-]{36}', volume or '')):
        raise RuntimeError('Invalid managed tenant or volume identity')
    if (environ.get('SOTTO_DEPLOYMENT_MODE') != 'managed'
            or environ.get('SOTTO_TENANT_ID') != tenant
            or environ.get('SOTTO_VOLUME_ID') != volume
            or environ.get('RAILWAY_VOLUME_ID') != volume
            or environ.get('RAILWAY_VOLUME_MOUNT_PATH') not in (None, '/data')
            or environ.get('SOTTO_DATA', '/data') != '/data'):
        raise RuntimeError('Managed environment does not match the requested tenant volume')
    data = Path(data)
    if data.is_symlink() or not data.is_dir() or not ismount(str(data)):
        raise RuntimeError('Managed /data is not the expected persistent mount point')
    marker = data / MARKER
    if marker.is_symlink():
        raise RuntimeError('Managed volume marker is a symlink')
    if marker.exists():
        verify(data, tenant, volume, ismount=ismount)
    else:
        entries = [entry for entry in data.iterdir() if entry.name != 'lost+found']
        recovered = data / 'lost+found'
        if entries or (recovered.exists() and (recovered.is_symlink() or not recovered.is_dir())):
            raise RuntimeError('Unmarked managed volume is populated; refusing initialization')
        initialize(data, tenant, volume, ismount=ismount)
    # os.execv preserves the PID. runtime_lock.py acquires its durable lock
    # before start.sh can start the receiver, Photon, or any other writer.
    execv(PYTHON, RUNTIME)
    raise RuntimeError('Managed runtime exec returned unexpectedly')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant', required=True)
    parser.add_argument('--volume', required=True)
    args = parser.parse_args(argv)
    try:
        launch(args.tenant, args.volume)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, '[sotto] managed launch refused: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
