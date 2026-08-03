#!/usr/bin/env python3
"""
followup_cron.py — windowing + run-marker + silence gate for the sotto-followup EVENING CRON.

The followup skill runs on-demand AND on a light evening cron (16:45 local — see start.sh/install.sh).
On the cron path we must process only the meetings that ENDED SINCE THE LAST CRON RUN, not a fixed 36h
window that would re-surface yesterday's follow-ups every evening. This tiny helper is that state (the
deterministic mirror of proactive_scan.py's marker): pure decision logic here, testable with a stubbed
clock; the skill only gathers, runs compose_followup.py, and delivers.

  followup_cron.py --since-hours            → prints the integer #hours to look back (now − last_cron
                                              marker, clamped), which the skill passes straight to
                                              `compose_followup.py --since-hours <N>`.
  followup_cron.py --silent-check <file>    → prints "silent" (nothing actionable → say nothing),
                                              "deliver", or "error" (the file is missing/unparseable —
                                              compose_followup didn't produce a valid result, so DON'T
                                              stamp the marker; the next run re-covers the window),
                                              reading a compose_followup output JSON.
  followup_cron.py --stamp                  → records THIS run's time to $SOTTO_DATA/followup/last_cron.

Marker: $SOTTO_DATA/followup/last_cron holds an ISO-8601 UTC timestamp of the last cron run. Missing
(first ever run) → the default bootstrap window. The window is clamped to [MIN, MAX] hours so a long
outage can't make one run scan weeks of transcripts, and clock skew / a rapid double-fire still looks
back at least MIN hours.

Env: SOTTO_DATA (state dir), SOTTO_FOLLOWUP_DEFAULT_HOURS (first-run window, default 36),
     SOTTO_FOLLOWUP_MIN_HOURS (default 1), SOTTO_FOLLOWUP_MAX_HOURS (default 72).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

# Share the pack's one timestamp parser instead of duplicating fromisoformat handling. The UTC-coercion
# wrapper (_parse_iso below) stays local — it's this module's own marker semantics.
_SHARED_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shared", "lib")
if _SHARED_LIB not in sys.path:
    sys.path.insert(0, _SHARED_LIB)
from timeutil import _parse_ts  # noqa: E402


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def marker_path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "followup", "last_cron")


def _parse_iso(raw: str | None):
    """Parse an ISO-8601 timestamp → aware UTC datetime, or None if empty/unparseable. Thin UTC-coercion
    wrapper over the shared timeutil._parse_ts: strip, parse, then treat a naive result as UTC and
    normalize to UTC (the marker's own semantics — kept local)."""
    if not raw:
        return None
    dt = _parse_ts(raw.strip())
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_last_cron(path: str | None = None):
    """The last cron run time as an aware UTC datetime, or None if never run / unreadable."""
    path = path or marker_path()
    try:
        with open(path, encoding="utf-8") as f:
            return _parse_iso(f.read())
    except OSError:
        return None


def write_last_cron(now: datetime, path: str | None = None) -> None:
    """Persist `now` as the last cron run (atomic). Best-effort — a missed stamp just widens the next
    window, never loses a follow-up."""
    path = path or marker_path()
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(stamp)
        os.replace(tmp, path)
    except OSError:
        pass


def window_hours(now: datetime, last, default_hours: int | None = None,
                 min_hours: int | None = None, max_hours: int | None = None) -> int:
    """Pure: how many hours back compose_followup should look. First run (`last` is None) → the default
    bootstrap window; otherwise ceil(now − last) in hours, clamped to [min, max]. Clock skew (last in
    the future → negative delta) clamps up to `min`, never scanning zero/negative."""
    default_hours = _int_env("SOTTO_FOLLOWUP_DEFAULT_HOURS", 36) if default_hours is None else default_hours
    min_hours = _int_env("SOTTO_FOLLOWUP_MIN_HOURS", 1) if min_hours is None else min_hours
    max_hours = _int_env("SOTTO_FOLLOWUP_MAX_HOURS", 72) if max_hours is None else max_hours
    if last is None:
        h = default_hours
    else:
        delta_h = (now.astimezone(timezone.utc) - last.astimezone(timezone.utc)).total_seconds() / 3600.0
        h = math.ceil(delta_h)
    return max(min_hours, min(max_hours, h))


def is_silent(result) -> bool:
    """A cron run is SILENT when nothing is actionable — no commitments AND no drafts. Mirrors
    proactive_scan's 'empty → say nothing': a bare recap with no items isn't worth a cron ping (the
    on-demand path always speaks; the evening/morning brief is the backstop)."""
    if not isinstance(result, dict):
        return True
    return not (result.get("commitments") or []) and not (result.get("drafts") or [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-hours", action="store_true",
                    help="print the look-back window in hours (default action)")
    ap.add_argument("--silent-check", metavar="FILE",
                    help="print 'silent'/'deliver'/'error' for a compose_followup output JSON")
    ap.add_argument("--stamp", action="store_true",
                    help="record this run's time as the last cron run")
    ap.add_argument("--now", help="ISO timestamp override (tests)")
    a = ap.parse_args()
    now = _parse_iso(a.now) or datetime.now(timezone.utc)

    if a.stamp:
        write_last_cron(now)
        print("stamped")
        return
    if a.silent_check:
        # A missing/unparseable file means compose_followup did NOT produce a valid result (it errored
        # or never ran). That is NOT the same as a valid-but-empty run: emit a distinct "error" so the
        # skill knows NOT to stamp the marker — otherwise a failed run would mark this window done and
        # its commitments would be skipped forever. A valid result keeps silent/deliver semantics.
        try:
            with open(a.silent_check, encoding="utf-8") as f:
                result = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            print("error")
            return
        print("silent" if is_silent(result) else "deliver")
        return
    # default: print the look-back window
    print(window_hours(now, read_last_cron()))


if __name__ == "__main__":
    main()
