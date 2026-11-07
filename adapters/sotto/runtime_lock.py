"""Hold the tenant runtime lock across exec so no second writer can share the volume."""
import fcntl
import os
from pathlib import Path
import sys

LOCK = '.sotto-runtime.lock'
# The current replacement epoch; only the root supervisor's verified re-entry may proceed past it.
EPOCH_FILE = '.sotto-epoch.json'


def acquire(data):
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(data / EPOCH_FILE):
        raise RuntimeError('Tenant has a replacement epoch; only the root supervisor may re-enter it')
    fd = os.open(data / LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError('Another runtime owns this tenant volume') from None
    # A supervisor may publish the epoch between the initial fast check and acquiring this lock.
    if os.path.lexists(data / EPOCH_FILE):
        os.close(fd)
        raise RuntimeError('Tenant has a replacement epoch; only the root supervisor may re-enter it')
    os.set_inheritable(fd, True)
    return fd


if __name__ == '__main__':
    try:
        acquire(os.environ.get('SOTTO_DATA', '/data'))
        os.execvp(sys.argv[1], sys.argv[1:])
    except (OSError, RuntimeError) as error:
        print('[sotto] ' + str(error), file=sys.stderr)
        sys.exit(1)
