#!/usr/bin/env python3
"""tzchain.py — THE timezone chain. One file, every runtime.

    SOTTO_TIMEZONE  →  TZ  →  $SOTTO_DATA/config/settings.json ("timezone", the wizard's)  →  UTC

Four copies of this chain used to live in four places — the skills' timeutil, the receiver, the
dashboard, and start.sh — each with a docstring saying "change one, change them all". They
drifted on the last rung (server local in two of them, UTC in the other two), which is invisible
on Railway (server local IS UTC) and cost an evening of failing tests on a Mac in PDT (Sep 3,
2026). Now there is one implementation and it ships to every runtime by path, not by import:

  - the skills tree imports it as a sibling (`_shared/lib` is on sys.path there);
  - the receiver image gets a copy at /app/trigger-receiver/tzchain.py (Dockerfile) and loads it
    by path — falling back to this file in a repo checkout, so the receiver's tests see the
    same code the container runs;
  - start.sh runs it (`python3 tzchain.py` prints `<zone>\\t<source>`), so the shell has no
    chain of its own either.

stdlib only, on purpose: nothing here may pull in the rest of either tree.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

OFFSET_RE = re.compile(r"^[+-]\d{2}:\d{2}$")


def data_root(root: str | None = None) -> str:
    return root or os.environ.get("SOTTO_DATA", "/data")


def configured_tz_name(root: str | None = None) -> str:
    """The user's zone as configured — an IANA name or a fixed `+HH:MM` — or "" when nothing is.
    Explicit env wins (SOTTO_TIMEZONE, then TZ), else the zone the setup wizard wrote to the
    volume. Never the server's own zone: that is what made a brief and its marker disagree about
    the date on a box whose clock is not UTC."""
    return configured_tz_source(root)[0]


def configured_tz_source(root: str | None = None) -> tuple[str, str]:
    """(zone, where it came from): "env:SOTTO_TIMEZONE" | "env:TZ" | "settings" | "" (none)."""
    for key in ("SOTTO_TIMEZONE", "TZ"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value, f"env:{key}"
    path = os.path.join(data_root(root), "config", "settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            settings = json.load(f) or {}
        value = settings.get("timezone") if isinstance(settings, dict) else ""
        value = value.strip() if isinstance(value, str) else ""
    except (OSError, ValueError):
        value = ""
    return (value, "settings") if value else ("", "")


def resolve(tz: str):
    """A tzinfo for an IANA name or a `+HH:MM` offset; None when it cannot be resolved (the
    caller's fallback is then UTC, the chain's last rung)."""
    tz = (tz or "").strip()
    if not tz:
        return None
    if OFFSET_RE.match(tz):
        sign = -1 if tz[0] == "-" else 1
        hours, minutes = int(tz[1:3]), int(tz[4:6])
        return timezone(timedelta(minutes=sign * (hours * 60 + minutes)))
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415 — stdlib; lazy, and tzdata may be absent
        return ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — an unknown name resolves to nothing, never to a guess
        return None


def local_now(root: str | None = None) -> datetime:
    """Now in the configured zone (aware), or in UTC (aware) when no zone is configured."""
    return datetime.now(resolve(configured_tz_name(root)) or timezone.utc)


def local_today(root: str | None = None) -> str:
    return local_now(root).strftime("%Y-%m-%d")


def main() -> int:
    zone, source = configured_tz_source()
    print(f"{zone}\t{source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
