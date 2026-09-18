"""Resumable history learning and work-driven curation on the existing receiver scheduler."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError

PACK = Path(__file__).resolve().parents[2]
for folder in (PACK / '_shared/lib', PACK / '_shared/knowledge', PACK / 'relationship-pulse/scripts'):
    sys.path.insert(0, str(folder))
import context_learning  # noqa: E402
import dreamer  # noqa: E402
import gather_google  # noqa: E402
import jsonstore  # noqa: E402
import gemini_transport  # noqa: E402
import prewarm_graph  # noqa: E402
import relationship_pulse  # noqa: E402
import style_extract  # noqa: E402
from render_local import resolve_contact_names  # noqa: E402
from source_context import (HistoryContinuationError, HistoryProtocolError, allowed,  # noqa: E402
                            bridge_call, history_page)

HISTORY_DAYS = 42
PAGES_PER_RUN = 3
SOURCE_RETRY_SECONDS = 3600
UNCHANGED_EXTRACTION_ATTEMPTS = 2
UNCHANGED_PROTOCOL_ATTEMPTS = 2
UNCHANGED_DREAMER_ATTEMPTS = 2
PROTOCOL_RECHECK_SECONDS = 24 * 3600
SOURCES = ('imessage', 'whatsapp', 'gmail')


def history_protocol_revision():
    """Release parked history input when the local protocol adapter changes."""
    contract = [Path(gather_google.__file__).read_text(),
                Path(sys.modules[history_page.__module__].__file__).read_text()]
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


def _history_failure_key(source, window):
    value = {'source': source, 'since': window.get('since'), 'until': window.get('until'),
             'cursor': window.get('cursor'), 'protocol': history_protocol_revision()}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def state_path():
    return str(Path(os.environ.get('SOTTO_DATA', '/data')) / 'knowledge/history-state.json')


def _remove_legacy_episode_store():
    """Discard retired situational summaries; they must not be promoted into durable facts."""
    try:
        (Path(os.environ.get('SOTTO_DATA', '/data')) / 'knowledge/episodes.json').unlink(missing_ok=True)
    except OSError:
        pass


def page(source, window):
    if source == 'gmail':
        return gather_google.history_page(window['since'], window['until'], window.get('cursor') or '')
    return history_page(source, window['since'], window['until'], window.get('cursor') or 0, limit=context_learning.REVIEW_MESSAGES)


def _run(now, fetch, learn, curate, path):
    epoch = int(now.timestamp())
    # The process lock is distinct from the checkpoint lock. A crash releases it and the last
    # fully learned page resumes; feedback and ordinary briefs do not wait on this lock.
    with jsonstore.lock(path + '.run'):
        _remove_legacy_episode_store()
        state = jsonstore.read(path, default={}, strict=True)
        # Retire accounting from the former daily allowance. Progress is now driven by bounded
        # invocations and durable per-source cursors.
        for key in ('day', 'model_calls', 'dreamer_day'):
            state.pop(key, None)
        snapshot = jsonstore.read(str(Path(os.environ.get('SOTTO_DATA', '/data')) /
                                      'knowledge/last_local_snapshot.json'), default={})
        contacts = snapshot.get('local', {}).get('contacts', []) if allowed('contacts') else []
        if not contacts and allowed('contacts'):
            try:
                contacts = bridge_call('get_contacts').get('contacts', [])
            except (OSError, RuntimeError, ValueError):
                contacts = []
        results = []
        background_hold = None
        for _ in range(PAGES_PER_RUN):
            index = int(state.get('next_source', 0)) % len(SOURCES)
            source = SOURCES[index]
            state['next_source'] = (index + 1) % len(SOURCES)
            if not allowed(source):
                # Permission reads can fail closed and source toggles can be temporary. Hold the
                # paid-history checkpoint intact; explicit revoke/forget owns privacy deletion.
                # No fetch, extraction, or cursor advance occurs while this source is forbidden.
                results.append({'source': source, 'status': 'held',
                                'error_code': 'source_not_allowed'})
                continue
            window = state.setdefault('sources', {}).setdefault(source, {
                'since': epoch - HISTORY_DAYS * 86400, 'until': epoch, 'cursor': None,
                'complete': False, 'pages': 0, 'window_page_base': 0, 'rows': 0, 'reviewed': 0,
                'initial_since': epoch - HISTORY_DAYS * 86400, 'initial_until': epoch, 'initial_complete': False})
            rejected = window.get('rejected_request_revision')
            if rejected == context_learning.request_revision():
                results.append({'source': source, 'status': 'blocked', 'error_code': 'provider_request_rejected'})
                continue
            if rejected:
                window.pop('rejected_request_revision', None)
                window.pop('retry_after', None)
            if window.get('complete'):
                # Preserve boundary overlap; second-resolution timestamps must not skip a
                # message whose native timestamp has fractional seconds.
                window.update(since=window['until'], until=epoch, cursor=None, complete=False,
                              window_page_base=window.get('pages', 0))
            protocol_key = _history_failure_key(source, window)
            protocol_failure = window.get('protocol_failure') or {}
            if (protocol_failure.get('key') == protocol_key
                    and int(protocol_failure.get('attempts', 0)) >= UNCHANGED_PROTOCOL_ATTEMPTS
                    and int(protocol_failure.get('recheck_after', 0)) > epoch):
                # A window that cannot progress says which deterministic rejection parked it,
                # so an exhausted continuation restart is visible without reading the state file.
                results.append({'source': source, 'status': 'blocked',
                                'error_code': 'history_protocol_rejected',
                                'reason': protocol_failure.get('code')})
                continue
            if protocol_failure and protocol_failure.get('key') != protocol_key:
                window.pop('protocol_failure', None)
                window.pop('retry_after', None)
            if window.get('retry_after', 0) > epoch:
                continue
            stage = 'fetch'
            try:
                payload = fetch(source, window)
                if not allowed(source) or payload.get('status') != 'ok':
                    raise RuntimeError('context source unavailable')
                rows = payload['rows']
                if not isinstance(rows, list) or not isinstance(payload.get('complete'), bool):
                    raise HistoryProtocolError('invalid_history_page')
                if not payload['complete'] and (not payload.get('next_cursor')
                                               or payload['next_cursor'] == window.get('cursor')):
                    raise HistoryProtocolError('history_cursor_did_not_advance')
                stage = 'local_learning'
                local = {'contacts': contacts, **({source: rows} if source != 'gmail' else {})}
                local = resolve_contact_names(local)
                if not allowed(source):
                    raise RuntimeError('source consent changed during history read')
                style_extract.extract(local, now=now, gmail=rows if source == 'gmail' else None)
                if not allowed(source):
                    raise RuntimeError('source consent changed during history read')
                prewarm_graph.prewarm(local, research=False)
                activity = {**local, **({'emails': rows} if source == 'gmail' else {})}
                if not allowed(source):
                    raise RuntimeError('source consent changed during history read')
                relationship_pulse.observe_history(activity, now=now)
                records = context_learning.observations(source, rows if source == 'gmail' else local.get(source, []), now)
                stage = 'memory_extraction'
                if not allowed(source):
                    raise RuntimeError('source consent changed during history read')
                result = learn(records, now=now)
                if not allowed(source):
                    raise RuntimeError('source consent changed during history read')
                window.update(cursor=payload.get('next_cursor'), complete=payload['complete'],
                              pages=window.get('pages', 0) + 1, rows=window.get('rows', 0) + len(rows),
                              reviewed=window.get('reviewed', 0) + result['reviewed'], last_success=now.isoformat())
                if payload['complete']:
                    window['initial_complete'] = True
                    window.pop('continuation_reset', None)
                window.pop('retry_after', None)
                for key in ('error', 'error_stage', 'error_code', 'http_status'):
                    window.pop(key, None)
                window.pop('extraction_failure', None)
                window.pop('protocol_failure', None)
                window['last_extraction'] = {key: result[key] for key in ('reviewed', 'facts', 'rejected_people')
                                             if key in result}
                if result.get('rejected_people'):
                    counts = window.setdefault('rejected_people', {})
                    for code, count in result['rejected_people'].items():
                        counts[code] = counts.get(code, 0) + count
                results.append({'source': source, **result, 'complete': window['complete']})
            except Exception as error:  # one broken source cannot stop the other connected sources
                held_reason = (error.reason if isinstance(error, gemini_transport.BackgroundModelHeldError)
                               else 'background_budget_exhausted'
                               if isinstance(error, HTTPError) and error.code == 402 else None)
                if held_reason:
                    results.append({'source': source, 'status': 'held', 'error_code': held_reason})
                    background_hold = held_reason
                    jsonstore.write_atomic(path, state, indent=2)
                    break
                diagnostic = {'error': type(error).__name__, 'error_stage': stage}
                deterministic_protocol = isinstance(error, HistoryProtocolError) or (
                    stage == 'fetch' and isinstance(error, HTTPError) and error.code in (400, 422))
                if deterministic_protocol:
                    failure = window.get('protocol_failure') or {}
                    attempts = (int(failure.get('attempts', 0)) + 1
                                if failure.get('key') == protocol_key else 1)
                    window['protocol_failure'] = {'key': protocol_key, 'attempts': attempts,
                                                  'recheck_after': epoch + PROTOCOL_RECHECK_SECONDS,
                                                  'code': ('provider_request_rejected'
                                                           if isinstance(error, HTTPError)
                                                           else error.code)}
                    diagnostic['error_code'] = window['protocol_failure']['code']
                    # A page token can expire while a frozen window waits, and only the source
                    # adapter can tell that apart from a request the provider will always reject —
                    # so the error type is the rule here, not the source name. After the rejection
                    # is confirmed, restart that SAME window from page one, but only when this
                    # attempt reached further into the window than the attempt before its last
                    # restart did. A token failing at the same depth cannot cause repeated resets.
                    if isinstance(error, HistoryContinuationError) and window.get('cursor'):
                        marker = window.get('continuation_reset') or {}
                        # `pages` is a lifetime total. Measure from this traversal's start, not
                        # from the first window the source ever learned. Old checkpoints may
                        # lack a baseline: their first reset is allowed, but its unknown depth
                        # cannot earn another. A previously consumed legacy reset stays consumed.
                        baseline = marker.get('pages') if marker else window.get('window_page_base')
                        reached = (int(window.get('pages', 0)) - baseline
                                   if type(baseline) is int else None)
                        if marker and (reached is None or type(marker.get('reached')) is not int
                                       or reached <= marker['reached']):
                            # The restart budget is spent. Say so in the diagnostic instead of
                            # probing a dead token forever; the latch below parks the window.
                            window['protocol_failure']['code'] = 'continuation_restart_exhausted'
                            diagnostic['error_code'] = 'continuation_restart_exhausted'
                        elif attempts >= UNCHANGED_PROTOCOL_ATTEMPTS:
                            window['cursor'] = None
                            window['continuation_reset'] = {
                                'at': now.isoformat(), 'pages': int(window.get('pages', 0)),
                                'reached': reached}
                            window.pop('protocol_failure', None)
                            diagnostic['error_code'] = 'invalid_continuation_reset'
                if isinstance(error, context_learning.MemoryExtractionError):
                    diagnostic['error_code'] = error.code
                    revision = context_learning.request_revision()
                    failure = window.get('extraction_failure') or {}
                    attempts = (int(failure.get('attempts', 0)) + 1
                                if failure.get('revision') == revision else 1)
                    window['extraction_failure'] = {'revision': revision, 'attempts': attempts,
                                                    'code': error.code}
                    # Permit one stochastic malformed response, then park the unchanged page and
                    # request contract. Code/model/config changes release this same revision latch.
                    if attempts >= UNCHANGED_EXTRACTION_ATTEMPTS:
                        window['rejected_request_revision'] = revision
                if isinstance(error, HTTPError):
                    diagnostic['http_status'] = error.code
                    if stage == 'memory_extraction' and error.code in (400, 422):
                        diagnostic['error_code'] = 'provider_request_rejected'
                        window['rejected_request_revision'] = context_learning.request_revision()
                window.pop('error_code', None)
                window.pop('http_status', None)
                window.update(retry_after=epoch + SOURCE_RETRY_SECONDS, **diagnostic)
                results.append({'source': source, 'status': 'failed', **diagnostic})
            jsonstore.write_atomic(path, state, indent=2)
        # dreamer selects at most DREAM_PEOPLE changed records and returns without a model call
        # when nothing remains, so each invocation advances one bounded unfinished batch.
        try:
            prepared = dreamer.prepare()
            dreamer_key = {'request': prepared['request_revision'],
                           'candidates': prepared['candidate_revision']}
        except Exception as error:
            prepared, dreamer_key = None, None
            preparation_error = error
        dreamer_failure = state.get('dreamer_failure') or {}
        dreamer_blocked = bool(dreamer_key) and (
            all(dreamer_failure.get(key) == value for key, value in dreamer_key.items())
            and int(dreamer_failure.get('attempts', 0)) >= UNCHANGED_DREAMER_ATTEMPTS)
        if dreamer_failure and dreamer_key and not all(
                dreamer_failure.get(key) == value for key, value in dreamer_key.items()):
            state.pop('dreamer_failure', None)
        if background_hold:
            # The hold stopped the cycle; report it even when preparation also failed, because
            # the hold is the reason nothing ran and the one the receipt promises to retain.
            curation = {'status': 'held', 'reason': background_hold}
        elif prepared is None:
            curation = {'status': 'failed', 'error': type(preparation_error).__name__}
        elif dreamer_blocked:
            curation = {'status': 'blocked', 'error_code': 'unchanged_dreamer_response'}
        else:
            try:
                curation = (curate(now=now, prepared=prepared)
                            if curate is dreamer.run else curate(now=now))
                state.pop('dreamer_failure', None)
            except gemini_transport.BackgroundModelHeldError as error:
                curation = {'status': 'held', 'reason': error.reason}
            except HTTPError as error:
                if error.code in (400, 422):
                    previous = state.get('dreamer_failure') or {}
                    attempts = (int(previous.get('attempts', 0)) + 1
                                if all(previous.get(key) == value for key, value in dreamer_key.items()) else 1)
                    state['dreamer_failure'] = {**dreamer_key, 'attempts': attempts,
                                                'code': 'provider_request_rejected'}
                curation = ({'status': 'held', 'reason': 'background_budget_exhausted'} if error.code == 402
                            else {'status': 'failed', 'error': type(error).__name__})
            except dreamer.DreamerResponseError as error:
                previous = state.get('dreamer_failure') or {}
                attempts = (int(previous.get('attempts', 0)) + 1
                            if all(previous.get(key) == value for key, value in dreamer_key.items()) else 1)
                state['dreamer_failure'] = {**dreamer_key, 'attempts': attempts, 'code': str(error)}
                curation = {'status': 'failed', 'error': type(error).__name__}
            except Exception as error:
                curation = {'status': 'failed', 'error': type(error).__name__}
        state['last_run'] = now.isoformat()
        state['receipt'] = {'history': results, 'dreamer': curation}
        jsonstore.write_atomic(path, state, indent=2)
        return state['receipt']


def run(now=None, fetch=page, learn=context_learning.learn, curate=dreamer.run):
    now = now or datetime.now(timezone.utc)
    path = state_path()
    try:
        with gemini_transport.background_learning():
            return _run(now, fetch, learn, curate, path)
    except gemini_transport.BackgroundModelHeldError as error:
        receipt = {'history': [], 'dreamer': {
            'status': 'held', 'reason': error.reason}}
        with jsonstore.lock(path + '.run'):
            state = jsonstore.read(path, default={}, strict=True)
            state['last_run'] = now.isoformat()
            state['receipt'] = receipt
            jsonstore.write_atomic(path, state, indent=2)
        return receipt


if __name__ == '__main__':
    try:
        result = run()
        # Scheduler passes a JSON request; all diagnostics stay in the receipt, never in chat.
        print('NO_NUDGES' if len(sys.argv) > 1 else json.dumps(result))
    except Exception as error:
        print('[memory_cycle] ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
