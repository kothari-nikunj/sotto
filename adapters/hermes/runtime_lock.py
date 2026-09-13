"""Hold the tenant runtime lock across exec so offline recovery cannot race writers."""
import fcntl
import os
from pathlib import Path
import sys

LOCK = '.sotto-runtime.lock'
HOLD = '.sotto-recovery-hold.json'


def acquire(data):
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    if (data / HOLD).exists():
        raise RuntimeError('Restored tenant is on recovery hold; validate and explicitly resume it first')
    fd = os.open(data / LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError('Another runtime or offline recovery operation owns this tenant volume') from None
    # Restore may finish between the initial fast check and acquiring this lock.
    if (data / HOLD).exists():
        os.close(fd)
        raise RuntimeError('Restored tenant is on recovery hold; validate and explicitly resume it first')
    os.set_inheritable(fd, True)
    return fd


if __name__ == '__main__':
    try:
        acquire(os.environ.get('SOTTO_DATA', '/data'))
        os.execvp(sys.argv[1], sys.argv[1:])
    except (OSError, RuntimeError) as error:
        print('[sotto] ' + str(error), file=sys.stderr)
        sys.exit(1)
