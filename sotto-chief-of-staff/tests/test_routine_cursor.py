"""Pinned Hermes croniter-6 observations, with no scheduler tick or live state."""

from datetime import datetime
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest


MODULE = Path(__file__).parents[1] / '_shared/lib/routine_cursor.py'
spec = importlib.util.spec_from_file_location('routine_cursor', MODULE)
cursor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cursor
spec.loader.exec_module(cursor)


def plan(*, now, next_run='2026-02-03T09:00:00-08:00', expr='0 9 * * *', **changes):
    options = {
        'expr': expr, 'recorded_next_at': next_run, 'last_run_at': None,
        'completed': 0, 'now': datetime.fromisoformat(now),
        'zone': 'America/Los_Angeles',
        'attested_source_zone': 'America/Los_Angeles',
    }
    options.update(changes)
    return cursor.plan_native_cron_cursor(**options)


@pytest.mark.parametrize(('now', 'next_run', 'expr', 'kind', 'candidate', 'successor'), [
    ('2026-02-03T09:01:00-08:00', '2026-02-03T09:00:00-08:00', '0 9 * * *',
     'on_time', '2026-02-03T17:00:00+00:00', '2026-02-04T09:00:00-08:00'),
    ('2026-02-03T10:30:00-08:00', '2026-02-03T09:00:00-08:00', '0 9 * * *',
     'late', '2026-02-03T17:00:00+00:00', '2026-02-04T09:00:00-08:00'),
    ('2026-02-03T13:00:00-08:00', '2026-02-03T09:00:00-08:00', '0 9 * * *',
     'catch_up', '2026-02-03T17:00:00+00:00', '2026-02-04T09:00:00-08:00'),
    ('2026-02-13T13:00:00-08:00', '2026-02-03T09:00:00-08:00', '0 9 * * *',
     'catch_up', '2026-02-03T17:00:00+00:00', '2026-02-14T09:00:00-08:00'),
    ('2026-02-02T13:00:00-08:00', '2026-02-02T09:00:00-08:00', '0 9 * * 1',
     'catch_up', '2026-02-02T17:00:00+00:00', '2026-02-09T09:00:00-08:00'),
    ('2026-03-08T03:31:00-07:00', '2026-03-08T02:30:00-08:00', '30 2 * * *',
     'on_time', '2026-03-08T10:30:00+00:00', '2026-03-09T02:30:00-07:00'),
    ('2026-11-01T01:31:00-07:00', '2026-11-01T01:30:00-07:00', '30 1 * * *',
     'on_time', '2026-11-01T08:30:00+00:00', '2026-11-02T02:30:00-08:00'),
])
def test_pinned_native_due_vectors(now, next_run, expr, kind, candidate, successor):
    result = plan(now=now, next_run=next_run, expr=expr)
    assert (result.disposition, result.candidate_utc, result.next_cursor) == (
        kind, candidate, successor)


def test_native_thresholds_are_300_seconds_and_cadence_grace():
    assert plan(now='2026-02-03T09:05:00-08:00').disposition == 'on_time'
    assert plan(now='2026-02-03T09:05:01-08:00').disposition == 'late'
    assert plan(now='2026-02-03T11:00:00-08:00').disposition == 'late'
    assert plan(now='2026-02-03T11:00:01-08:00').disposition == 'catch_up'


def test_not_due_preserves_original_cursor_and_completed_anchor():
    result = plan(now='2026-02-03T08:00:00-08:00',
                  last_run_at='2026-02-02T09:01:00-08:00', completed=12)
    assert result == cursor.CursorPlan('not_due', next_cursor='2026-02-03T09:00:00-08:00')
    assert plan(now='2026-02-03T10:00:00-08:00',
                last_run_at='2026-02-02T09:01:00-08:00', completed=12).disposition == 'late'


def test_settled_prior_dispatch_does_not_hold_next_slot():
    result = plan(now='2026-02-03T10:00:00-08:00',
                  last_run_at='2026-02-02T09:01:00-08:00', completed=12,
                  last_dispatch={'scheduled_at': '2026-02-02T09:00:00-08:00',
                                 'dispatched_at': '2026-02-02T09:00:30-08:00'})
    assert result.disposition == 'late'


@pytest.mark.parametrize(('changes', 'expected'), [
    ({'attested_source_zone': 'America/New_York'}, 'hold_timezone_change'),
    ({'last_run_at': None, 'completed': 2}, 'hold_completion_anchor'),
    ({'manual_run_at': '2026-02-03T09:00:00-08:00'}, 'hold_manual_run'),
    ({'fire_claim': {'by': 'native-worker'}}, 'hold_native_claim'),
    ({'last_dispatch': {'dispatched_at': '2026-02-03T09:01:00-08:00'}},
     'hold_dispatch_evidence'),
    ({'next_run': '2026-02-03T09:00:00+00:00'}, 'hold_offset_migration'),
    ({'next_run': '2026-02-03T04:00:00-08:00'}, 'hold_stale_expression'),
])
def test_ambiguous_or_changed_native_state_is_held(changes, expected):
    now = '2026-02-03T10:00:00-08:00'
    assert plan(now=now, **changes).disposition == expected


def test_native_offset_repair_is_explicit_hold_not_silent_skip():
    result = plan(now='2026-02-03T04:00:00-08:00',
                  next_run='2026-02-03T09:00:00+00:00')
    assert result == cursor.CursorPlan('hold_native_offset_reanchor')


def test_fold_second_instant_is_not_synthesized_from_first():
    first = plan(now='2026-11-01T01:31:00-07:00',
                 next_run='2026-11-01T01:30:00-07:00', expr='30 1 * * *')
    assert first.next_cursor == '2026-11-02T02:30:00-08:00'
    second = plan(now='2026-11-01T01:31:00-08:00',
                  next_run=first.next_cursor, expr='30 1 * * *')
    assert second.disposition == 'not_due'


def test_fold_wall_order_disagrees_with_absolute_order_and_is_held():
    # Native get_due_jobs reports the second-fold 01:30 as due at first-fold
    # 01:45, even though its UTC instant is 45 minutes in the future.
    future = plan(now='2026-11-01T01:45:00-07:00',
                  next_run='2026-11-01T01:30:00-08:00', expr='30 1 * * *')
    assert future == cursor.CursorPlan('hold_fold_order')
    # Conversely, native defers a first-fold occurrence after the second fold
    # has begun, even though the recorded UTC instant is already past.
    overdue = plan(now='2026-11-01T01:15:00-08:00',
                   next_run='2026-11-01T01:30:00-07:00', expr='30 1 * * *')
    assert overdue == cursor.CursorPlan('hold_fold_order')


def test_fold_lateness_preserves_native_wall_formula_when_orders_agree():
    result = plan(now='2026-11-01T01:45:00-08:00',
                  next_run='2026-11-01T01:30:00-07:00', expr='30 1 * * *')
    assert result.disposition == 'late'
    assert result.lateness_seconds == 900.0  # Native wall arithmetic, not 4500 UTC seconds.
    assert result.candidate_utc == '2026-11-01T08:30:00+00:00'


def test_completion_anchor_uses_absolute_order_and_rejects_datetime_input():
    zone = ZoneInfo('America/Los_Angeles')
    first_fold_now = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0)
    future_second_fold_last = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    # The native store contract is an ISO string. A same-ZoneInfo datetime
    # would compare by wall time and incorrectly appear to be in the past.
    with pytest.raises(ValueError, match='last run'):
        cursor.plan_native_cron_cursor(
            expr='0 9 * * *', recorded_next_at='2026-11-02T09:00:00-08:00',
            last_run_at=future_second_fold_last, completed=1, now=first_fold_now,
            zone='America/Los_Angeles', attested_source_zone='America/Los_Angeles')
    assert cursor.plan_native_cron_cursor(
        expr='0 9 * * *', recorded_next_at='2026-11-02T09:00:00-08:00',
        last_run_at=future_second_fold_last.isoformat(), completed=1,
        now=first_fold_now, zone='America/Los_Angeles',
        attested_source_zone='America/Los_Angeles').disposition == 'hold_completion_anchor'


def test_completion_successor_uses_actual_completion_not_dispatch_time():
    assert cursor.successor_after_native_completion(
        expr='0 9 * * *', completed_at=datetime.fromisoformat('2026-02-14T10:00:00-08:00'),
        zone='America/Los_Angeles') == '2026-02-15T09:00:00-08:00'


@pytest.mark.parametrize('completed', [-1, True, 1.0])
def test_completion_count_is_nonnegative_exact_integer(completed):
    with pytest.raises(ValueError, match='completion count'):
        plan(now='2026-02-03T10:00:00-08:00', completed=completed)


def test_weekday_fixed_cursor_friday_due_monday_successor():
    result=plan(now='2026-10-09T09:01:00-07:00',next_run='2026-10-09T09:00:00-07:00',expr='0 9 * * 1-5')
    assert result.disposition=='on_time'
    assert result.candidate_utc=='2026-10-09T16:00:00+00:00'
    assert result.next_cursor=='2026-10-12T09:00:00-07:00'


def test_exact_first_of_month_fixed_cursor_and_completion():
    result=plan(now='2026-01-01T09:01:00-08:00',next_run='2026-01-01T09:00:00-08:00',expr='0 9 1 * *')
    assert result.candidate_utc=='2026-01-01T17:00:00+00:00'
    assert cursor.successor_after_native_completion(expr='0 9 1 * *',completed_at=datetime.fromisoformat('2026-01-01T09:01:00-08:00'),zone='America/Los_Angeles')=='2026-02-01T09:00:00-08:00'
    for expr in ('0 9 2 * *','0 9 1 2 *','0 9 1 * 1'):
        with pytest.raises(ValueError):
            cursor._fixed_expr(expr)


def test_native_cursor_still_refuses_hourly():
    with pytest.raises(ValueError):
        plan(now='2026-02-03T09:16:00-08:00',expr='15 * * * *')
