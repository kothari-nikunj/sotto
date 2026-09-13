"""One deterministic brief procedure in both modes, with durable preparation and learning.

The receiver owns work admission and delivery. A saved useful artifact survives retries;
optional research and ancillary learning never sit between that artifact and the outbox.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

PREPARATION_MAX_AGE_SECONDS = 30 * 60
NOTES_CACHE_MAX_AGE_SECONDS = 24 * 3600
WELCOME_SEED_BUDGET_SECONDS = 20
FOLLOWUP_RETENTION_SECONDS = 7 * 24 * 3600


def run(request):
    pack = Path(request['pack'])
    sys.path.insert(0, str(pack / '_shared' / 'lib'))
    import jsonstore
    import work_queue
    from source_context import allowed, project_local, read_local, record_bridge_status, used_sources
    from textutil import unwrap_tool_result
    from timeutil import configured_tz, _user_local_date

    root = Path(os.environ.get('SOTTO_DATA', '/data'))
    day = request.get('day') or _user_local_date(configured_tz())
    key = str(request.get('work_key') or (day + ':' + request['kind']))
    run_key = hashlib.sha256(key.encode()).hexdigest()[:24]
    scratch = root / 'events' / 'work-inputs' / ('brief-' + run_key)
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    coverage_until = datetime.now(timezone.utc).isoformat()

    def command(relative, *args, output=None, timeout=600):
        result = subprocess.run([sys.executable, str(pack / relative), *map(str, args)],
                                capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            # The receiver's sanitizer, not a second one: only an allowlisted exception name or
            # a presence flag may cross from a subprocess's stderr into a persisted error.
            raise RuntimeError(f'{Path(relative).name} failed (exit {result.returncode}; '
                               f'{work_queue.diagnostic_category(result.stderr)})')
        if output:
            jsonstore.write_atomic(str(output), json.loads(result.stdout))
        return result.stdout

    def paths_at(directory):
        directory.mkdir(exist_ok=True, mode=0o700)
        return {key: directory / (key + '.json') for key in
                ('local', 'gmail', 'cal', 'granola', 'knowledge', 'selection', 'research',
                 'x', 'inputs', 'continuity', 'knowledge_out', 'source_results')}

    def save(path, value):
        jsonstore.write_atomic(str(path), value)

    def load(path, default):
        return jsonstore.read(str(path), default=default)

    def gather_notes(paths):
        if not allowed('granola'):
            save(paths['granola'], {'meetings': []})
            return {'meetings': []}
        command('_shared/scripts/gather_granola.py', '--out', paths['granola'])
        value = load(paths['granola'], {})
        if not value.get('warnings'):
            save(root / 'cache/brief-granola.json', {'observed_at': time.time(), 'data': value})
        return value

    def cached_notes():
        cached = load(root / 'cache/brief-granola.json', {})
        age = time.time() - cached.get('observed_at', 0)
        if 0 <= age < NOTES_CACHE_MAX_AGE_SECONDS and allowed('granola'):
            value = dict(cached.get('data') or {})
            if age >= PREPARATION_MAX_AGE_SECONDS:
                value['warnings'] = ['Using previously gathered meeting notes; latest refresh is pending']
            return value
        return {'meetings': [], 'warnings': ['Meeting notes refresh is pending']}

    def stage_artifact(artifact):
        import delivery_effects
        sources = artifact.get('_source_permissions', [])
        if any(not allowed(source) for source in sources):
            # The saved composition read a source the user has since switched off. It must not
            # be delivered — and it must not be RELOADED either: every retry found the same
            # artifact and raised the same error until the job went terminal and the day's brief
            # never came. Discard it here, so the retry composes afresh under current consent.
            (scratch / 'artifact.json').unlink(missing_ok=True)
            raise RuntimeError('source permission changed; a fresh brief is required')
        delivery_effects.stage([{'kind': 'source_permissions', 'sources': sources,
                                 'coverage_until': artifact.get('_source_cutoff', coverage_until)},
                                *artifact.get('_calendar_eligibility', [])])

    def learn(paths, phase):
        command('_shared/scripts/learn_step.py', '--type', request['kind'], '--day', day,
                '--phase', phase, '--run-key', run_key,
                '--local', paths['local'], '--gmail', paths['gmail'], '--granola', paths['granola'],
                '--knowledge-out', paths['knowledge_out'], '--continuity', paths['continuity'])

    def followup():
        followup_request = {'pack': str(pack), 'kind': request['kind'], 'day': day,
                            'work_key': key, 'learn_only': True}
        return work_queue.enqueue(str(root), 'run', {
            'runner': [sys.executable, str(Path(__file__).resolve())],
            'prompt': json.dumps(followup_request), 'label': 'background:sotto-learn:' + run_key,
            'decision_ids': []}, key='brief-learn:' + run_key, priority=work_queue.BACKGROUND_PRIORITY,
            valid_until=time.time() + FOLLOWUP_RETENTION_SECONDS)

    if request.get('learn_only'):
        with jsonstore.lock(str(scratch / 'learn')):
            if load(scratch / 'ancillary-done.json', {}).get('complete'):
                return 'NO_NUDGES'
            paths = paths_at(scratch / 'current')
            # Consent can change between the brief and this background run. Reapply it before
            # extracting voice or observations from persisted source inputs.
            save(paths['local'], project_local(load(paths['local'], {})))
            if not allowed('gmail'):
                save(paths['gmail'], [])
            if not load(scratch / 'essential-done.json', {}).get('complete'):
                learn(paths, 'essential')
                save(scratch / 'essential-done.json', {'complete': True, 'finished_at': time.time()})
            if load(paths['granola'], {}).get('warnings'):
                gather_notes(paths)  # retry missing optional context in the background lane
            learn(paths, 'ancillary')
            save(scratch / 'ancillary-done.json', {'complete': True, 'finished_at': time.time()})
            shutil.rmtree(scratch / 'current')
            with jsonstore.lock(str(scratch / 'prepare')):
                shutil.rmtree(scratch / 'prepared', ignore_errors=True)
        return 'NO_NUDGES'

    if request.get('prepare'):
        # Separate lock/files: slow optional research cannot hold the due brief's process lock.
        with jsonstore.lock(str(scratch / 'prepare')):
            if load(scratch / 'artifact.json', {}).get('brief_text'):
                return 'NO_NUDGES'
            previous = load(scratch / 'prepared.json', {})
            if previous.get('complete') and time.time() - previous.get('observed_at', 0) < PREPARATION_MAX_AGE_SECONDS:
                return 'NO_NUDGES'
            paths = paths_at(scratch / 'prepared')
            # Only Calendar is needed to prepare people ahead of time; the deadline pass gathers
            # current communications once. Existing graph research freshness prevents repeat buys.
            command('_shared/scripts/gather_google.py', '--skip-gmail', '--gmail-out', paths['gmail'],
                    '--cal-out', paths['cal'], '--source-results-out', paths['source_results'])
            status = load(paths['source_results'], {}).get('calendar', {}).get('status')
            if status not in ('ok', 'partial'):
                raise RuntimeError('Calendar preparation source unavailable')
            gather_notes(paths)
            save(paths['inputs'], {'google': {'emails': [], 'events': load(paths['cal'], [])}, 'local': {}})
            command('morning-brief/scripts/select_attendees.py', paths['inputs'], output=paths['selection'])
            command('meeting-prep/scripts/persist_prep.py', '--filter-fresh', paths['selection'])
            command('_shared/scripts/research_attendees.py', '--attendees', paths['selection'],
                    '--context', paths['cal'], '--comms', paths['gmail'], '--out', paths['research'])
            command('meeting-prep/scripts/persist_prep.py', '--research', paths['research'],
                    '--attendees', paths['selection'])
            command('_shared/scripts/x_connectivity.py', '--calendar', paths['cal'],
                    '--research', paths['research'], '--out', paths['x'])
            # A due run may have composed while this optional lane was working. Its durable
            # artifact wins; do not publish a stale "prepared" receipt or leave partial inputs for
            # a retry/background cleanup to mistake for preparation used by that brief.
            if load(scratch / 'artifact.json', {}).get('brief_text'):
                shutil.rmtree(scratch / 'prepared', ignore_errors=True)
                return 'NO_NUDGES'
            save(scratch / 'prepared.json', {'complete': True, 'observed_at': time.time()})
        return 'NO_NUDGES'

    with jsonstore.lock(str(scratch / 'run')):
        artifact = load(scratch / 'artifact.json', {})
        paths = paths_at(scratch / 'current')
        if not isinstance(artifact.get('brief_text'), str) or not artifact['brief_text'].strip():
            payload_path = request.get('payload_path')
            if payload_path:
                staged = json.loads(Path(payload_path).read_text())
                local = staged.get('local_data', staged)
            else:
                local = read_local(168 if request['kind'] == 'welcome' else 24)
            local = unwrap_tool_result(local)
            record_bridge_status(local)
            local = project_local(local)
            save(paths['local'], local)
            extra = ['--window-days', '7', '--max', '200', '--bodies', '60'] if request['kind'] == 'welcome' else []
            command('_shared/scripts/gather_google.py', '--gmail-out', paths['gmail'], '--cal-out', paths['cal'],
                    '--source-results-out', paths['source_results'], *extra)
            # Source status is required for an honest current brief, even when legacy arrays are empty.
            source_results = load(paths['source_results'], {})
            if not source_results:
                raise RuntimeError('gather omitted source observation receipts')
            prepared = load(scratch / 'prepared.json', {})
            usable_prep = prepared.get('complete') and 0 <= time.time() - prepared.get('observed_at', 0) < PREPARATION_MAX_AGE_SECONDS
            if usable_prep:
                save(paths['granola'], load(scratch / 'prepared/granola.json', {}))
                save(paths['research'], load(scratch / 'prepared/research.json', {'attendees': []}))
                save(paths['x'], load(scratch / 'prepared/x.json', {}))
            else:
                # Reuse durable research already prepared today. No uncached web research delays
                # delivery; the scheduled preparation job and ordinary prep lane can complete it.
                save(paths['research'], load(root / 'cache' / ('research_' + day + '.json'), {'attendees': []}))
                save(paths['x'], {})
                save(paths['granola'], cached_notes())
            if not allowed('granola'):
                save(paths['granola'], {'meetings': []})
            gmail = load(paths['gmail'], [])
            emails = gmail if isinstance(gmail, list) else gmail.get('emails', [])
            events = load(paths['cal'], [])
            if request['kind'] == 'welcome':
                # Local-only seeders have ONE small combined allowance on the first reply.
                # Slow optional learning resumes in the durable background job after delivery.
                seed_deadline = time.monotonic() + WELCOME_SEED_BUDGET_SECONDS
                for script, args in (
                    ('_shared/scripts/style_extract.py', (paths['local'], '--gmail', paths['gmail'])),
                    ('_shared/scripts/prewarm_graph.py', (paths['local'], '--identities-only'))):
                    remaining = seed_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        command(script, *args, timeout=remaining)
                    except (RuntimeError, subprocess.TimeoutExpired):
                        pass  # the durable ancillary job retries learning after delivery
            command('_shared/knowledge/knowledge_query.py', '--calendar', paths['cal'], '--gmail', paths['gmail'],
                    '--local', paths['local'], output=paths['knowledge'])
            continuity = {'today': day, 'signals': {}, 'local': local, 'emails': emails, 'events': events}
            save(paths['continuity'], continuity)
            command('morning-brief/scripts/continuity_resolve.py', '--resolve-only', paths['continuity'])
            composed = json.loads(command('_shared/scripts/compose_brief.py', '--type', request['kind'],
                '--local', paths['local'], '--gmail', paths['gmail'], '--calendar', paths['cal'],
                '--granola', paths['granola'], '--knowledge', paths['knowledge'],
                '--source-results', paths['source_results'],
                '--attendee-research', paths['research'], '--x-context', paths['x']))
            if not isinstance(composed.get('brief_text'), str) or not composed['brief_text'].strip():
                raise RuntimeError('composer produced no chat text')
            composed.setdefault('_source_permissions', used_sources(
                local, emails, events, load(paths['granola'], {})))
            composed['_source_cutoff'] = coverage_until
            composed['_calendar_eligibility'] = [
                {'kind': 'eligibility', 'source': 'calendar',
                 'calendar_event_id': event['id'], 'calendar_start': event['start'],
                 'calendar_observed_at': source_results.get('calendar', {}).get('observed_at') or coverage_until}
                for event in events if isinstance(event, dict) and event.get('id') and event.get('start')
                and event.get('my_response') != 'declined']
            save(paths['knowledge_out'], composed.get('extracted_knowledge') or {})
            continuity['new_actions'] = composed.get('actions') or []
            save(paths['continuity'], continuity)
            # Persist last: every input needed to resume required writes now exists atomically.
            save(scratch / 'artifact.json', composed)
            artifact = composed
        stage_artifact(artifact)
        if not load(scratch / 'essential-done.json', {}).get('complete'):
            try:
                learn(paths, 'essential')
                save(scratch / 'essential-done.json', {'complete': True, 'finished_at': time.time()})
            except Exception as error:  # learning is after the source/consent delivery gate
                # Consent/source validation and delivery effects are already staged. Preserve the
                # composed brief for delivery; the durable follow-up completes required writes.
                print(f'[brief_runner] essential learning deferred: {type(error).__name__}',
                      file=sys.stderr)
        if not load(scratch / 'ancillary-done.json', {}).get('complete'):
            try:
                followup()  # stable work key; a retry cannot duplicate successful learning
            except Exception as error:
                # Keep the archived inputs and pending essential marker for recovery. Failure of
                # background admission cannot withhold an already validated, composed brief.
                print(f'[brief_runner] learning follow-up admission failed: {type(error).__name__}',
                      file=sys.stderr)
        return artifact['brief_text']


if __name__ == '__main__':
    try:
        print(run(json.loads(sys.argv[1])))
    except Exception as error:
        print(f'[brief_runner] {type(error).__name__}: {error}', file=sys.stderr)
        sys.exit(1)
