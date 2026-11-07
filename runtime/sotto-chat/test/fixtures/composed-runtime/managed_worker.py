import json
import os
import signal
import sys
import time
from pathlib import Path

ready = Path(sys.argv[1])
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
ready.write_text(json.dumps({'pid': os.getpid(), 'uid': os.getuid()}), encoding='utf-8')
while True:
    time.sleep(1)
