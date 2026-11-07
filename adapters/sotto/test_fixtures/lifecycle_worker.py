"""Synthetic detached worker for the private-PID-namespace lifecycle contract."""
import json
import ctypes
import os
from pathlib import Path
import signal
import sys
import time


def _ignore(_signal, _frame):
    return None


def main():
    marker = Path(sys.argv[1])
    time.sleep(0.2)  # let the supervisor record the initial child's kernel identity
    first = os.fork()
    if first:
        os.waitpid(first, 0)
        return
    os.setsid()
    second = os.fork()
    if second:
        os._exit(0)
    signal.signal(signal.SIGTERM, _ignore)
    # Linux task names are raw bytes, not guaranteed ASCII or line-oriented.
    libc = ctypes.CDLL(None)
    libc.prctl(15, ctypes.c_char_p(b'\xe9) native\n'), 0, 0, 0)
    marker.write_text(json.dumps({'pid': os.getpid(), 'uid': os.geteuid(),
                                  'pgid': os.getpgid(0), 'ppid': os.getppid()}))
    while True:
        time.sleep(0.1)


if __name__ == '__main__':
    main()
