#!/usr/bin/env python3
"""Explicit opt-in real inference check. Provision the pack first; never downloads."""

import argparse
import json
from pathlib import Path
import time

import speech


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Private empty scratch directory")
    args = parser.parse_args()
    raw = json.dumps(
        {
            "text": "Your morning brief is ready. You have two meetings today and one follow up to review.",
            "voice": "default",
        }
    ).encode()
    start = time.monotonic()
    first = speech.synthesize(args.pack, args.output, raw)
    elapsed = time.monotonic() - start
    second = speech.synthesize(args.pack, args.output, raw)
    assert not first["cached"], "Use an empty output directory to test real inference"
    assert second["cached"]
    assert first["audio"]["sha256"] == second["audio"]["sha256"]
    assert first["audio"]["frames"] > 24000
    print(json.dumps({"ok": True, "cold_wall_seconds": elapsed, "result": first, "cache_verified": True}))


if __name__ == "__main__":
    main()
