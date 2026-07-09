#!/usr/bin/env python3
"""
timeutil.py — timezone / date / timestamp helpers for the brief pipeline.

Extracted verbatim from compose_brief.py (the 2,400-line monolith split) with ZERO behavior
change. Holds the timestamp parser (_parse_ts / _date_only), timezone resolution
(configured_tz / _resolve_tz via IANA zoneinfo or a fixed offset), the settings.json reader
(load_settings) and the user-local-day / time-frame helpers (port of getTimeFrame /
getUserLocalDate). These are what keep headless cron briefs on the user's local day, not UTC.

compose_brief.py re-exports every name here at its old location for the `import compose_brief as
cb` compat surface.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from textutil import _s  # noqa: E402


def _date_only(ts) -> str:
    """The YYYY-MM-DD date part of a timestamp, accepting either the Mac's space-separated
    "2026-06-24 09:00:00" or the Bridge's ISO "2026-06-24T13:00:00Z" — both must render as a date."""
    return re.split(r"[ T]", _s(ts), 1)[0]




# ---------------------------------------------------------------------------
# Timestamp parsing (port of contacts.ts) — the shared _parse_ts sibling scripts import via cb
# ---------------------------------------------------------------------------

def _parse_ts(ts: str):
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except Exception:
        return None




# ---------------------------------------------------------------------------
# Time helpers (port of getTimeFrame / getUserLocalDate)
# ---------------------------------------------------------------------------

def _tz_offset_minutes(tz: str) -> int:
    m = re.match(r"([+-])(\d{2}):(\d{2})", tz or "")
    if not m:
        return 0
    sign = 1 if m.group(1) == "+" else -1
    return sign * (int(m.group(2)) * 60 + int(m.group(3)))




def _env_tz() -> str:
    """The user's IANA zone from the environment (SOTTO_TIMEZONE / TZ). Set on Railway so headless
    cron briefs compute 'today' in the user's local day, not UTC — the source of the off-by-one date."""
    return (os.environ.get("SOTTO_TIMEZONE") or os.environ.get("TZ") or "").strip()




def _settings_path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "config", "settings.json")




def load_settings() -> dict:
    """Volume-persisted settings the setup wizard writes (browser-detected timezone, etc.). Reading
    these makes the Railway SOTTO_TIMEZONE var OPTIONAL — the wizard captures the zone once and we
    pick it up here. Never raises."""
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}




def configured_tz() -> str:
    """User's IANA zone: explicit env (SOTTO_TIMEZONE/TZ) wins, else the wizard-detected zone on the
    volume. Removes the off-by-one footgun when the Railway var is unset (the wizard supplies it)."""
    return _env_tz() or _s(load_settings().get("timezone"))




def _resolve_tz(tz: str):
    """Resolve a timezone string to a tzinfo. Accepts an IANA name ('America/Los_Angeles') via
    zoneinfo, or a fixed '+HH:MM' offset. Returns None when it can't be resolved (caller falls back
    to the integer-offset path)."""
    tz = (tz or "").strip()
    if not tz:
        return None
    if re.match(r"^[+-]\d{2}:\d{2}$", tz):
        return timezone(timedelta(minutes=_tz_offset_minutes(tz)))
    try:
        from zoneinfo import ZoneInfo  # py3.9+; honors the user's IANA zone incl. DST
        return ZoneInfo(tz)
    except Exception:
        return None




def _now_local(tz: str) -> datetime:
    """Current wall-clock time in the user's zone. Prefers a real tzinfo (IANA, DST-correct); falls
    back to the fixed-offset arithmetic for bare '+HH:MM' inputs."""
    tzinfo = _resolve_tz(tz)
    if tzinfo is not None:
        return datetime.now(timezone.utc).astimezone(tzinfo)
    return datetime.now(timezone.utc) + timedelta(minutes=_tz_offset_minutes(tz))




def _user_tz_offset(events) -> str:
    for e in events:
        dt = _s(e.get("start"))
        m = re.search(r"([+-]\d{2}:\d{2})$", dt)
        if m:
            return m.group(1)
    return configured_tz() or "+00:00"




def _user_local_date(tz: str) -> str:
    return _now_local(tz).strftime("%Y-%m-%d")




def _time_frame(tz: str) -> str:
    h = _now_local(tz).hour
    return "morning" if h < 12 else "afternoon" if h < 17 else "evening"
