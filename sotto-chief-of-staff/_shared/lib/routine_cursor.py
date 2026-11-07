"""Pure planning for a captured native Hermes fixed-cron cursor.

This adapts NousResearch/hermes-agent cron/jobs.py at commit
245e48008fa814b3251f50755eb656bd9fb86cb1; see adjacent
routine_cursor.NOTICE for the upstream MIT notice. It mirrors the pinned
Hermes croniter 6 calculation for a narrow daily or
single-weekday, exact Monday–Friday, or first-of-month shape. It neither reads a store nor grants a routine, starts a
clock, admits work, or proves a prior run completed. The receiver must resolve
held states and validate migration authority before using any candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


_FIXED = re.compile(r'\A([0-9]{1,2}) ([0-9]{1,2}) (?:\* \* (?:\*|[0-7]|1-5)|1 \* \*)\Z')
_MIN_GRACE_SECONDS = 120
_MAX_GRACE_SECONDS = 7200
_ON_TIME_SECONDS = 300


@dataclass(frozen=True)
class CursorPlan:
    disposition: str
    candidate_utc: str | None = None
    next_cursor: str | None = None
    lateness_seconds: float | None = None


def _zone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name:
        raise ValueError('invalid routine timezone')
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError('invalid routine timezone') from error


def _fixed_expr(expr: str) -> str:
    match = _FIXED.fullmatch(expr) if isinstance(expr, str) else None
    if match is None or int(match[1]) > 59 or int(match[2]) > 23:
        raise ValueError('unsupported fixed routine cron')
    if not croniter.is_valid(expr):
        raise ValueError('invalid fixed routine cron')
    return expr


def _instant(value: str | datetime, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError as error:
        raise ValueError(f'invalid routine {label}') from error
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError(f'invalid routine {label}')
    return parsed


def _next(expr: str, base: datetime) -> str:
    # Pinned Hermes jobs.py:compute_next_run uses croniter(expr, base).get_next(datetime).
    return croniter(expr, base).get_next(datetime).isoformat()


def _is_legal(expr: str, instant: datetime) -> bool:
    # Pinned Hermes jobs.py:_cron_next_run_matches_expr includes the instant by
    # starting one second later, then compares at second precision.
    previous = croniter(expr, instant + timedelta(seconds=1)).get_prev(datetime)
    return abs((previous - instant).total_seconds()) < 1.0


def _grace_seconds(expr: str, now: datetime) -> int:
    # Exact native cadence/grace formula, without its process-local cache.
    iterator = croniter(expr, now)
    first = iterator.get_next(datetime)
    gap = (iterator.get_next(datetime) - first).total_seconds()
    if gap <= 0:
        return _MIN_GRACE_SECONDS
    return max(_MIN_GRACE_SECONDS, min(int(gap) // 2, _MAX_GRACE_SECONDS))


def plan_native_cron_cursor(*, expr: str, recorded_next_at: str,
                            last_run_at: str | None, completed: int,
                            now: datetime, zone: str, attested_source_zone: str,
                            last_dispatch: dict | None = None,
                            manual_run_at: str | None = None,
                            fire_claim: object = None, run_claim: object = None) -> CursorPlan:
    """Describe one captured pending slot; never reconstruct it from ``now``.

    ``next_cursor`` on a due candidate is Hermes's provisional pre-completion
    advance from ``now``. Call ``successor_after_native_completion`` only after
    an actual run finishes. A held result deliberately supplies no candidate.
    Even a candidate is not authorization, quiescence, or delivery evidence.
    """
    expr = _fixed_expr(expr)
    target_zone = _zone(zone)
    _zone(attested_source_zone)
    observed_now = _instant(now, 'caller time').astimezone(target_zone)
    if zone != attested_source_zone:
        return CursorPlan('hold_timezone_change')
    if type(completed) is not int or completed < 0:
        raise ValueError('invalid native completion count')
    if not isinstance(recorded_next_at, str):
        raise ValueError('invalid recorded routine cursor')
    raw_next = _instant(recorded_next_at, 'recorded cursor')
    next_local = raw_next.astimezone(target_zone)
    if last_run_at is not None and not isinstance(last_run_at, str):
        raise ValueError('invalid routine last run')
    last_run = _instant(last_run_at, 'last run') if last_run_at is not None else None
    if (completed > 0) != (last_run is not None):
        return CursorPlan('hold_completion_anchor')
    observed_utc = observed_now.astimezone(timezone.utc)
    raw_next_utc = raw_next.astimezone(timezone.utc)
    last_run_utc = last_run.astimezone(timezone.utc) if last_run is not None else None
    if last_run_utc is not None and (last_run_utc > observed_utc or raw_next_utc <= last_run_utc):
        return CursorPlan('hold_completion_anchor')
    if manual_run_at is not None:
        return CursorPlan('hold_manual_run')
    if fire_claim is not None or run_claim is not None:
        return CursorPlan('hold_native_claim')
    if last_dispatch is not None:
        if (not isinstance(last_dispatch, dict)
                or not isinstance(last_dispatch.get('scheduled_at'), str)
                or not isinstance(last_dispatch.get('dispatched_at'), str)):
            return CursorPlan('hold_dispatch_evidence')
        try:
            scheduled = _instant(last_dispatch['scheduled_at'], 'last dispatch slot')
            dispatched = _instant(last_dispatch['dispatched_at'], 'last dispatch')
        except ValueError:
            return CursorPlan('hold_dispatch_evidence')
        scheduled_utc = scheduled.astimezone(timezone.utc)
        dispatched_utc = dispatched.astimezone(timezone.utc)
        if (scheduled_utc > dispatched_utc or dispatched_utc > observed_utc
                or last_run_utc is None or dispatched_utc > last_run_utc):
            # Hermes may already have advanced next_run_at before mark_job_run.
            return CursorPlan('hold_dispatch_evidence')
    # Pinned Hermes compares two values with the same ZoneInfo object. Python
    # then compares wall time across a fold, ignoring their different UTC
    # offsets: it can fire the SECOND 01:30 while the clock is still at the
    # FIRST 01:45, or defer the FIRST 01:30 after the second fold begins.
    # Report the native/absolute disagreement, never admit a future UTC slot.
    native_due = next_local <= observed_now
    absolute_due = raw_next_utc <= observed_utc
    if native_due != absolute_due:
        return CursorPlan('hold_fold_order')
    if next_local > observed_now:
        return CursorPlan('not_due', next_cursor=recorded_next_at)
    # Native offset repair skips this tick when an old-offset instant is due in
    # UTC but its original wall clock has not yet arrived. Migration holds it.
    if (raw_next.utcoffset() != observed_now.utcoffset()
            and raw_next.replace(tzinfo=None) > observed_now.replace(tzinfo=None)):
        return CursorPlan('hold_native_offset_reanchor')
    if not _is_legal(expr, next_local):
        if (raw_next.replace(tzinfo=None) != next_local.replace(tzinfo=None)
                and _is_legal(expr, raw_next)):
            return CursorPlan('hold_offset_migration')
        return CursorPlan('hold_stale_expression')
    lateness = max(0.0, (observed_now - next_local).total_seconds())
    grace = _grace_seconds(expr, observed_now)
    disposition = ('catch_up' if lateness > grace else
                   'late' if lateness > _ON_TIME_SECONDS else 'on_time')
    return CursorPlan(disposition,
                      candidate_utc=raw_next.astimezone(timezone.utc).isoformat(),
                      next_cursor=_next(expr, observed_now),
                      lateness_seconds=round(lateness, 1))


def successor_after_native_completion(*, expr: str, completed_at: datetime,
                                      zone: str) -> str:
    """The native recurring successor anchored on an actual completion time."""
    return _next(_fixed_expr(expr), _instant(completed_at, 'completion').astimezone(_zone(zone)))
