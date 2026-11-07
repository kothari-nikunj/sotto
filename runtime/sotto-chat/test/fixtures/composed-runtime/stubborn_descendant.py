import json
import os
import signal
import sys
import time
from pathlib import Path

ready_path = Path(sys.argv[1] + '.descendant')
signal.signal(signal.SIGTERM, signal.SIG_IGN)
ready_path.write_text(json.dumps({'pid': os.getpid(), 'uid': os.getuid()}), encoding='utf-8')
while True:
    time.sleep(1)
