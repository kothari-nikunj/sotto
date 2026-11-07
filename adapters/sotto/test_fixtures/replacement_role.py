"""Synthetic process roles for the private PID namespace lifecycle test."""
import json
import os
from pathlib import Path
import signal
import sys
import time


def main():
    marker, mode = Path(sys.argv[1]), sys.argv[2]
    if mode == 'detached':
        child = os.fork()
        if child == 0:
            os.setsid()
            child = os.fork()
            if child == 0:
                signal.signal(signal.SIGTERM, lambda *_: None)
                (marker.with_suffix('.detached')).write_text(str(os.getpid()))
                while True:
                    time.sleep(0.1)
            os._exit(0)
        os.waitpid(child, 0)
    marker.write_text(json.dumps({'pid': os.getpid(), 'uid': os.geteuid()}))
    if mode == 'exit':
        time.sleep(0.1)
        return
    while True:
        time.sleep(0.1)


if __name__ == '__main__':
    main()
