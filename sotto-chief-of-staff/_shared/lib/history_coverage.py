"""Content-free history coverage shared by conversation reads and the dashboard.

Window counters describe extraction coverage, not semantic completeness or a person's
entire history. Never project cursors, errors, identifiers or message text.
"""
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_LEARNING_BYTES = 4 * 1024


def read_state(root):
    try:
        descriptor = os.open(Path(root) / 'knowledge/history-state.json',
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_STATE_BYTES:
                raise ValueError('checkpoint is not a bounded regular file')
            raw = stream.read(MAX_STATE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > MAX_STATE_BYTES or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('checkpoint too large')
        state = json.loads(raw)
        if not isinstance(state, dict) or not isinstance(state.get('sources', {}), dict):
            raise ValueError('invalid checkpoint')
        return 'available', state.get('sources', {})
    except FileNotFoundError:
        return 'absent', {}
    except (OSError, ValueError, RecursionError):
        return 'unknown', {}



def learning_coverage(root, permitted):
    """Fixed source-wide metadata under the caller's captured grants, never topic completeness.

    Reuse the bounded checkpoint read once. With no applicable grant, do not open it at all;
    withheld sources have the same shape whether their checkpoint exists or not.
    """
    if type(permitted) is not list or any(type(source) is not str for source in permitted):
        raise ValueError('history coverage grants invalid')
    names = ('imessage', 'whatsapp', 'gmail')
    status, sources = read_state(root) if set(names).intersection(permitted) else ('absent', {})
    result = {'sources': {source: source_coverage(status, sources, source, source in permitted)
                         for source in names},
            'scope': ('Source-wide, not person- or topic-specific completeness, current outcomes or absent obligations. '
                      'Gmail/iMessage/WhatsApp; other sources are unmeasured. An absent checkpoint means no saved measurement, not no history. '
                      'Window bounds are not proof of reads. Failed attempts do not erase prior reads. '
                      'Initial traversal is not review; unknown initial review stays unknown. '
                      'Outside-window review is unknown, not never reviewed. Counters are not obligation totals.')}
    # Model-facing bounds describe the request, not proof that the interval was read.
    # Keep the dashboard's window_coverage API unchanged.
    for value in result['sources'].values():
        for view in (value, value.get('initial_review')):
            if isinstance(view, dict) and 'window' in view:
                view['requested_window'] = view.pop('window')
    if len(json.dumps(result).encode('utf-8')) > MAX_LEARNING_BYTES:
        # Keep current health/window evidence ahead of historical counter detail. A bounded
        # projection must disclose its omission rather than erase a known current failure.
        for value in result['sources'].values():
            initial = value.get('initial_review')
            if isinstance(initial, dict) and 'observation_counts' in initial:
                initial.pop('observation_counts')
                initial['observation_counts_omitted'] = True
    if len(json.dumps(result).encode('utf-8')) > MAX_LEARNING_BYTES:
        # Drop window/counter detail, but retain the already validated latest health.
        result['sources'] = {source: {key: value[key] for key in
            ('checkpoint', 'latest_attempt', 'last_read_success_at', 'last_extraction_success_at')
            if key in value} for source, value in result['sources'].items()}
        # Consumers receive the whole payload. Disclose omitted coverage once, without
        # manufacturing traversal values for rows whose window detail was removed.
        result.update(detail_omitted=True, review_scope='unknown')
    return result


def source_coverage(status, sources, source, permitted):
    if not permitted:
        return {'checkpoint': 'withheld'}
    window = sources.get(source)
    if status == 'unknown' or source in sources and not isinstance(window, dict):
        return {'checkpoint': 'unknown'}
    if not isinstance(window, dict):
        return {'checkpoint': 'absent'}
    return {'checkpoint': 'present', **window_coverage(window)}


def window_coverage(window: dict) -> dict:
    """Project current and initial windows separately, never recursively trust saved data."""
    initial = window.get('initial_coverage')
    if window.get('since') == window.get('initial_since') and window.get('until') == window.get('initial_until'):
        initial = window
    return {**_window_coverage(window),
            'latest_attempt': _attempt_coverage(window),
            'last_read_success_at': _instant(window.get('last_read_success')),
            'last_extraction_success_at': _instant(window.get('last_success')),
            'initial_review': _window_coverage(initial) if isinstance(initial, dict) else {'review_scope': 'unknown'}}


def _instant(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _attempt_coverage(window):
    """Project attempt health separately from accumulated coverage; never copy diagnostic text."""
    raw = window.get('last_attempt')
    raw = raw if isinstance(raw, dict) else {}
    status = raw.get('status')
    if status not in ('succeeded', 'failed', 'held', 'excluded'):
        status = ('failed' if window.get('error') else 'unread'
                  if window.get('pages') == 0 and not window.get('last_success') and not raw else 'unknown')
    result = {'status': status}
    at = _instant(raw.get('at'))
    if at:
        result['at'] = at
    if status not in ('failed', 'held', 'excluded'):
        return result
    stage = raw.get('stage', window.get('error_stage'))
    stage = stage if stage in ('fetch', 'local_learning', 'memory_extraction') else None
    if stage:
        result['stage'] = stage
    if status == 'held':
        result['category'] = ('allowance_exhausted' if raw.get('reason') == 'background_budget_exhausted'
                              else 'model_unavailable')
    elif status == 'excluded':
        result['category'] = 'content_excluded'
    else:
        code, http = window.get('error_code'), window.get('http_status')
        # Codes are classified, never echoed; unexpected values remain unknown.
        failure = window.get('provider_failure')
        if failure in ('malformed_request', 'unsupported_model_settings', 'input_too_large',
                       'output_limit', 'authentication_failed', 'authorization_denied', 'allowance_exhausted',
                       'rate_limited', 'service_unavailable', 'timeout', 'provider_rejected',
                       'extraction_validation_failed'):
            category = failure
        elif code in ('bridge_offline', 'bridge_not_configured', 'source_unavailable'):
            category = 'source_unavailable'
        elif code in ('source_not_allowed', 'source_consent_changed'):
            category = 'permission_changed'
        elif code == 'bridge_timeout' or http in (408, 504):
            category = 'timeout'
        elif http == 429:
            category = 'rate_limited'
        elif type(http) is int and 500 <= http <= 599:
            category = 'service_unavailable'
        elif http in (400, 401, 403, 413, 422):
            category = 'provider_rejected'
        else:
            category = {'fetch': 'read_failed', 'local_learning': 'local_processing_failed',
                        'memory_extraction': 'extraction_failed'}.get(stage, 'unknown')
        result['category'] = category
    return result


def _window_coverage(window: dict) -> dict:
    """Bounded metadata from the existing cursor; traversal is not complete learning."""
    def count(value):
        return value if type(value) is int and 0 <= value <= 1_000_000_000 else None

    def instant(value):
        if type(value) is not int or not 0 <= value <= 253402300799:
            return None
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return None

    bounds = {key: instant(window.get(key)) for key in ('since', 'until', 'initial_since', 'initial_until')}
    raw = window.get('observation_totals')
    totals = {key: count(raw.get(key)) if isinstance(raw, dict) else None
              for key in ('pages', 'rows', 'eligible', 'selected', 'skipped', 'omitted',
                          'truncated', 'rejected_people')}
    excluded = window.get('excluded_pages', {})
    excluded_count = (count(window['excluded_page_count']) if 'excluded_page_count' in window
                      else sum(1 for receipt in excluded.values()
                               if not isinstance(receipt, dict)
                               or type(receipt.get('since')) is not int or type(receipt.get('until')) is not int
                               or (receipt['since'], receipt['until']) == (window.get('since'), window.get('until')))
                      if isinstance(excluded, dict) else None)
    pages, baseline = count(window.get('pages')), count(window.get('window_page_base'))
    measured = (pages is not None and baseline is not None and pages >= baseline
                and bounds['since'] is not None and bounds['until'] is not None
                and window['since'] <= window['until']
                and totals['pages'] == pages - baseline and all(v is not None for v in totals.values()))
    gaps = (excluded_count is None or excluded_count > 0
            or any(totals[key] for key in ('skipped', 'omitted', 'truncated', 'rejected_people')))
    scope = ('partial' if gaps else 'unknown' if not measured else
             'reviewed_window' if window.get('complete') is True else 'in_progress')
    return {'window': bounds, 'initial_traversal_complete': window.get('initial_complete') is True,
            'review_scope': scope, 'outside_window_review': 'unknown',
            'observation_counts': totals, 'excluded_pages': excluded_count}
