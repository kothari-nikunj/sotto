#!/usr/bin/env python3
"""Package the same accounting module into the independently built proxy image."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
source = root / 'sotto-chief-of-staff/_shared/lib/usage_accounting.py'
target = root / 'cloud/model-proxy/usage_accounting.py'
if '--check' in sys.argv:
    if not target.exists() or target.read_bytes() != source.read_bytes():
        raise SystemExit('Proxy accounting is stale: run python tools/sync-usage.py')
else:
    target.write_bytes(source.read_bytes())
