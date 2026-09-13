"""Verify a managed tenant's provisioned persistent volume before starting writers.

Initialization is an explicit provisioning operation, never an automatic boot fallback.
A matching marker alone is insufficient: the data directory must be a mount point.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile

MARKER = '.sotto-volume.json'


def _mount(data, ismount):
    data = Path(data).absolute()
    if data.is_symlink() or not ismount(str(data)):
        raise RuntimeError('Managed data directory is not a persistent mount point')
    return data


def _flush_directory(data):
    fd = os.open(data, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def initialize(data, tenant, volume_id, *, ismount=os.path.ismount):
    data = _mount(data, ismount)
    if not tenant or not volume_id:
        raise ValueError('Managed tenant and volume identity are required')
    marker = data / MARKER
    identity = {'schema': 1, 'tenant_id': tenant, 'volume_id': volume_id}
    if marker.exists():
        if json.loads(marker.read_text()) != identity:
            raise RuntimeError('Existing volume identity does not match; refusing to reassign it')
        return verify(data, tenant, volume_id, ismount=ismount)
    capabilities = data / 'config/managed-capabilities.json'
    if capabilities.exists():
        previous = json.loads(capabilities.read_text()).get('tenant_id')
        if previous and previous != tenant:
            raise RuntimeError('Existing managed source state belongs to another tenant')
    # Exclusive creation prevents two provisioning requests from rebinding the same volume.
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(identity, stream)
        stream.flush()
        os.fsync(stream.fileno())
    _flush_directory(data)
    return verify(data, tenant, volume_id, ismount=ismount)


def verify(data, tenant, volume_id, *, ismount=os.path.ismount):
    data = _mount(data, ismount)
    if not tenant or not volume_id:
        raise ValueError('Managed tenant and volume identity are required')
    try:
        identity = json.loads((data / MARKER).read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError('Managed volume has not been provisioned') from error
    if identity != {'schema': 1, 'tenant_id': tenant, 'volume_id': volume_id}:
        raise RuntimeError('Managed volume does not match this tenant or provisioned volume')
    fd, filename = tempfile.mkstemp(prefix='.sotto-volume-check-', dir=data)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write('durable-write-probe')
            stream.flush()
            os.fsync(stream.fileno())
        if Path(filename).read_text() != 'durable-write-probe':
            raise RuntimeError('Managed volume readback failed')
        _flush_directory(data)
    finally:
        Path(filename).unlink(missing_ok=True)
    return {'ready': True, **identity}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('initialize', 'verify'))
    parser.add_argument('--data', default=os.environ.get('SOTTO_DATA', '/data'))
    parser.add_argument('--tenant', default=os.environ.get('SOTTO_TENANT_ID', ''))
    parser.add_argument('--volume', default=os.environ.get('SOTTO_VOLUME_ID', ''))
    args = parser.parse_args()
    operation = initialize if args.operation == 'initialize' else verify
    try:
        print(json.dumps(operation(args.data, args.tenant, args.volume)))
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, '[sotto] volume verification failed: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
