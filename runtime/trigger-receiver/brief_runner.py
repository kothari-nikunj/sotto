"""One deterministic brief procedure in both modes, with durable preparation and learning.

The receiver owns work admission and delivery. A saved useful artifact survives retries;
optional research and ancillary learning are admitted separately from its initial delivery.
"""
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

PREPARATION_MAX_AGE_SECONDS = 30 * 60
# A saved composition older than this is re-gathered, not resent: its greeting and its "today" have aged.
ARTIFACT_MAX_AGE_SECONDS = 2 * 3600
NOTES_CACHE_MAX_AGE_SECONDS = 24 * 3600
WELCOME_SEED_BUDGET_SECONDS = 20
FOLLOWUP_RETENTION_SECONDS = 7 * 24 * 3600
LEARNING_DIAGNOSTIC_MAX_BYTES = 16 * 1024
LEARNING_DIAGNOSTIC_MAX_ITEMS = 64


class BriefBusyError(Exception):
    """Another generation operation owns the inputs; retry without charging a failed attempt."""


class BriefModelBudgetError(Exception):
    """Fresh composition needs an account allowance update, not repeated source reads."""


class BriefModelAdmissionBusyError(Exception):
    """The shared proxy refused an unbilled attempt; durable work retries within its deadline."""


@contextmanager
def _generation_lock(scratch):
    import jsonstore
    with ExitStack() as stack:
        try:
            stack.enter_context(jsonstore.lock(str(scratch / 'run')))
        except TimeoutError as error:
            raise BriefBusyError('brief generation is busy') from error
        yield  # Timeouts inside the operation are real failures, not lock contention.


def run(request):
    pack = Path(request['pack'])
    sys.path.insert(0, str(pack / '_shared' / 'lib'))
    import jsonstore
    import work_queue
    from gemini_transport import brief_model_preflight, ModelBudgetUnavailableError
    from source_context import (allowed, email_dependency_sources, project_emails, project_local,
                                project_x, read_local, record_bridge_status, used_sources)
    from local_snapshot import resolve_effective_local
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
            if (relative in ('_shared/scripts/compose_brief.py',
                             '_shared/scripts/research_attendees.py')
                    and result.returncode == work_queue.MODEL_ADMISSION_BUSY_EXIT):
                raise BriefModelAdmissionBusyError('Model admission busy')
            if (relative == '_shared/scripts/compose_brief.py'
                    and result.returncode == work_queue.MODEL_BUDGET_EXIT):
                raise BriefModelBudgetError('Model allowance reached')
            # The receiver's sanitizer, not a second one: only an allowlisted exception name or
            # a presence flag may cross from a subprocess's stderr into a persisted error.
            raise RuntimeError(f'{Path(relative).name} failed (exit {result.returncode}; '
                               f'{work_queue.diagnostic_category(result.stderr)})')
        if output:
            jsonstore.write_atomic(str(output), json.loads(result.stdout))
        return result.stdout

    def check_model():
        try:
            brief_model_preflight()
        except ModelBudgetUnavailableError as error:
            raise BriefModelBudgetError('Model allowance reached') from error

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
                                {'kind': 'eligibility', 'valid_until':
                                 float(artifact.get('composed_at') or 0) + ARTIFACT_MAX_AGE_SECONDS},
                                *artifact.get('_calendar_eligibility', []),
                                *artifact.get('_parking_notices', []),
                                *([artifact['_review_candidate']] if artifact.get('_review_candidate') else [])])

    def learn(paths, phase):
        # The composition file is already the effective local view. Reapply current consent at
        # each learning phase, including a later background retry, and keep the resolver's copy
        # in step with the exact file passed to style learning.
        local = project_local(load(paths['local'], {}))
        save(paths['local'], local)
        continuity = load(paths['continuity'], {})
        if isinstance(continuity, dict):
            continuity['local'] = local
            if not allowed('calendar'):
                continuity['events'] = []
        gmail = project_emails(load(paths['gmail'], []))
        save(paths['gmail'], gmail)
        if isinstance(continuity, dict):
            continuity['emails'] = gmail if isinstance(gmail, list) else gmail.get('emails', [])
            save(paths['continuity'], continuity)
        if not allowed('granola'):
            save(paths['granola'], {'meetings': []})
        learning_payload = load(paths['knowledge_out'], {})
        learning_evidence = learning_payload.get('_learning_evidence')
        if isinstance(learning_evidence, dict):
            sources = artifact.get('_source_permissions', [])
            learning_evidence['consent'] = {source: bool(allowed(source)) for source in sources
                                            if isinstance(source, str)}
            learning_evidence['consent_checked_at'] = datetime.now(timezone.utc).isoformat()
            save(paths['knowledge_out'], learning_payload)
        output = command('_shared/scripts/learn_step.py', '--type', request['kind'], '--day', day,
                         '--phase', phase, '--run-key', learning_key,
                         '--local', paths['local'], '--gmail', paths['gmail'], '--granola', paths['granola'],
                         '--knowledge-out', paths['knowledge_out'], '--continuity', paths['continuity'])
        try:
            receipt = json.loads(output.strip().splitlines()[-1])
        except (IndexError, TypeError, ValueError) as error:
            raise RuntimeError('learning receipt missing or unreadable') from error
        if not isinstance(receipt, dict) or receipt.get('completion') not in ('complete', 'partial'):
            raise RuntimeError('learning receipt did not confirm a finished run')
        _retain_partial_evidence(receipt, paths['continuity'], artifact)
        return receipt

    def _retain_partial_evidence(receipt, continuity_path, source_artifact):
        """Keep only rejected proposal evidence briefly, and only while its sources remain allowed."""
        from hashlib import sha256
        destination = root / 'events' / 'learning-diagnostics' / (
            'brief-' + sha256(learning_key.encode()).hexdigest()[:32] + '.json')
        step = receipt.get('steps', {}).get('continuity', {})
        items = (step.get('proof') or {}).get('loop_update_items') or {}
        rejected = items.get('rejected')
        sources = source_artifact.get('_source_permissions', [])
        if not isinstance(rejected, list) or not isinstance(sources, list) or not sources:
            rejected = []
        retained = []
        retained_source_types = set()
        try:
            payload = load(continuity_path, {}) if rejected else {}
            proposals = payload.get('loop_updates') if isinstance(payload, dict) else None
            proposals = proposals if isinstance(proposals, list) else []
            for result in rejected[:LEARNING_DIAGNOSTIC_MAX_ITEMS]:
                index = result.get('index') if isinstance(result, dict) else None
                if not isinstance(index, int) or not 0 <= index < len(proposals):
                    continue
                proposal = proposals[index]
                canonical = json.dumps(proposal, sort_keys=True, default=str, separators=(',', ':'))
                digest = sha256(canonical.encode()).hexdigest()[:24]
                if digest != result.get('digest'):
                    continue
                row = {'index': index, 'digest': digest, 'reason': result.get('reason', 'unknown')}
                evidence = proposal.get('evidence') if isinstance(proposal, dict) else None
                evidence = evidence if isinstance(evidence, list) else [evidence]
                refs, snippets = [], []
                for entry in evidence[:4]:
                    if not isinstance(entry, dict):
                        continue
                    source_type = next((entry.get(k) for k in ('sourceType', 'source_type', 'source')
                                        if isinstance(entry.get(k), str) and entry.get(k)), None)
                    source_type = 'gmail' if source_type == 'email' else source_type
                    if source_type not in sources or not allowed(source_type):
                        continue
                    retained_source_types.add(source_type)
                    ref = next((entry.get(k) for k in
                                ('sourceId', 'source_id', 'source_ref', 'ref', 'id')
                                if isinstance(entry.get(k), str) and entry.get(k)), None)
                    if ref:
                        refs.append(ref[:240])
                    snippet = next((entry.get(k) for k in ('snippet', 'quote', 'text', 'excerpt')
                                    if isinstance(entry.get(k), str) and entry.get(k)), None)
                    if snippet:
                        snippets.append(snippet[:500])
                row['source_refs'] = refs[:4]
                row['evidence_snippets'] = snippets[:4]
                if refs or snippets:
                    retained.append(row)
            if not retained and not destination.exists():
                return  # nothing to keep and nothing kept earlier: no file, no lock sidecar
            diagnostic = {'schema': 1, 'created_at': time.time(), 'expires_after_days': 7,
                          'learning_revision': str(source_artifact.get('learning_revision') or '')[:80],
                          'rejected': retained}
            serialized = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
            while len(serialized.encode()) > LEARNING_DIAGNOSTIC_MAX_BYTES and retained:
                retained.pop()
                diagnostic['rejected'] = retained
                serialized = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
            # The directory gate is shared with consent revocation. It covers the last permission
            # check and file creation, including the interval when the file does not exist yet.
            with jsonstore.lock(str(destination.parent / '.consent')), jsonstore.lock(str(destination)):
                if not retained or any(not allowed(source) for source in retained_source_types):
                    # This pass may keep nothing, so an earlier pass's file (whose source may since
                    # have been revoked) goes too; a rewrite never carries an old row forward.
                    destination.unlink(missing_ok=True)
                    return
                # The window runs from first capture: a later rewrite of the same run's diagnostic
                # keeps the earlier of its recorded capture time and its mtime (which the retention
                # sweep reads), so a rewrite never extends the seven days.
                if destination.exists():
                    previous = load(destination, {})
                    recorded = previous.get('created_at') if isinstance(previous, dict) else None
                    for captured in (recorded, destination.stat().st_mtime):
                        if (isinstance(captured, (int, float)) and not isinstance(captured, bool)
                                and 0 < captured < diagnostic['created_at']):
                            diagnostic['created_at'] = captured
                jsonstore.write_atomic(str(destination), diagnostic)
                os.utime(destination, (diagnostic['created_at'], diagnostic['created_at']))
        except (OSError, TypeError, ValueError, KeyError, AttributeError):
            # The primary result receipt stays authoritative if optional diagnostic storage fails;
            # a diagnostic this pass could not re-check is dropped rather than kept.
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass

    def followup():
        followup_request = {'pack': str(pack), 'kind': request['kind'], 'day': day,
                            'work_key': key, 'learn_only': True, 'learning_revision': artifact.get('learning_revision')}
        return work_queue.enqueue(str(root), 'run', {
            'runner': [sys.executable, str(Path(__file__).resolve())],
            'prompt': json.dumps(followup_request), 'label': 'background:sotto-learn:' + learning_key,
            'decision_ids': []}, key='brief-learn:' + learning_key, priority=work_queue.BACKGROUND_PRIORITY,
            valid_until=time.time() + FOLLOWUP_RETENTION_SECONDS)

    if request.get('learn_only'):
        with _generation_lock(scratch):
            artifact = load(scratch / 'artifact.json', {})
            if (not artifact.get('brief_text')
                    or request.get('learning_revision') != artifact.get('learning_revision')):
                return 'NO_NUDGES'  # superseded job cannot read or delete the replacement's inputs
            learning_key = run_key + ':' + artifact['learning_revision'] if artifact.get('learning_revision') else run_key
            if load(scratch / 'ancillary-done.json', {}).get('complete'):
                return 'NO_NUDGES'
            paths = paths_at(scratch / 'current')
            # Every learning phase reapplies consent before reading persisted inputs.
            if not load(scratch / 'essential-done.json', {}).get('complete'):
                receipt = learn(paths, 'essential')
                save(scratch / 'essential-done.json', {'complete': True,
                     'learning_status': receipt['completion'], 'finished_at': time.time()})
            if load(paths['granola'], {}).get('warnings'):
                gather_notes(paths)  # retry missing optional context in the background lane
            receipt = learn(paths, 'ancillary')
            save(scratch / 'ancillary-done.json', {'complete': True,
                 'learning_status': receipt['completion'], 'finished_at': time.time()})
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
            check_model()
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
            try:
                command('_shared/scripts/research_attendees.py', '--attendees', paths['selection'],
                        '--context', paths['cal'], '--comms', paths['gmail'], '--out', paths['research'])
            except BriefModelAdmissionBusyError:
                # A due brief may compose while this optional job waits for shared capacity.
                # Keep a content-free marker so that fallback research is disclosed.
                save(scratch / 'research-status.json', {'status': 'admission_busy'})
                raise
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
            (scratch / 'research-status.json').unlink(missing_ok=True)
        return 'NO_NUDGES'

    with _generation_lock(scratch):
        artifact = load(scratch / 'artifact.json', {})
        # A retry hours later must not resend the 6:30 composition: the greeting and every "today"
        # in it were stamped at compose time, so past ARTIFACT_MAX_AGE_SECONDS the saved artifact is
        # treated as absent and this run gathers and composes afresh. Still the SAME brief — the
        # day, the work key, the deliver-once marker and not_before are untouched, and the marker is
        # claimed at the send seam, so a fresher artifact for an undelivered day cannot double-send.
        if time.time() - float(artifact.get('composed_at') or 0) > ARTIFACT_MAX_AGE_SECONDS:
            artifact = {}
        paths = paths_at(scratch / 'current')
        if not isinstance(artifact.get('brief_text'), str) or not artifact['brief_text'].strip():
            check_model()
            # Retire the old generation before touching its inputs. Learning uses this same lock
            # and a revision-bound job, so a crash during replacement cannot consume partial files
            # or mistake old completion receipts for learning the new actions.
            for name in ('artifact.json', 'essential-done.json', 'ancillary-done.json'):
                (scratch / name).unlink(missing_ok=True)
            shutil.rmtree(scratch / 'current', ignore_errors=True)
            paths = paths_at(scratch / 'current')
            payload_path = request.get('payload_path')
            if payload_path:
                staged = json.loads(Path(payload_path).read_text())
                local = staged.get('local_data', staged)
            else:
                local = read_local(168 if request['kind'] == 'welcome' else 24)
            local = unwrap_tool_result(local)
            record_bridge_status(local)
            local = resolve_effective_local(project_local(local))
            save(paths['local'], local)
            consumed_sources = set(used_sources(local, already_projected=True))
            # The pinned Google search makes a metadata GET for every hit. The former two
            # 200-hit slices cost 8,000 quota units before reading a single body (new Google
            # projects allow 6,000/user/minute). Keep a seven-day starter sample, both date
            # slices and the sent lane; capped slices are disclosed by the source receipt.
            # With 50 hits/slice and 20 bodies, even the attachment/fallback allowance stays
            # below 5,000 units. Concurrent jobs can still hit quota and must report partial.
            extra = ['--window-days', '7', '--max', '50', '--bodies', '20'] if request['kind'] == 'welcome' else []
            command('_shared/scripts/gather_google.py', '--gmail-out', paths['gmail'], '--cal-out', paths['cal'],
                    '--source-results-out', paths['source_results'], '--loop-local', paths['local'], *extra)
            # Source status is required for an honest current brief, even when legacy arrays are empty.
            source_results = load(paths['source_results'], {})
            if not source_results:
                raise RuntimeError('gather omitted source observation receipts')
            # A grant can change while Google is gathering. Do not let an earlier file reach
            # seeding, knowledge query, or the pre-compose resolver under a revoked grant.
            local = project_local(load(paths['local'], {}))
            save(paths['local'], local)
            gmail = project_emails(load(paths['gmail'], []))
            save(paths['gmail'], gmail)
            if not allowed('calendar'):
                save(paths['cal'], [])
            if any(not allowed(source) for source in consumed_sources):
                raise RuntimeError('source permission changed during brief gather')
            prepared = load(scratch / 'prepared.json', {})
            usable_prep = prepared.get('complete') and 0 <= time.time() - prepared.get('observed_at', 0) < PREPARATION_MAX_AGE_SECONDS
            if usable_prep:
                save(paths['granola'], load(scratch / 'prepared/granola.json', {}))
                save(paths['research'], load(scratch / 'prepared/research.json', {'attendees': []}))
                save(paths['x'], project_x(load(scratch / 'prepared/x.json', {})))
            else:
                # Reuse durable research already prepared today. No uncached web research delays
                # delivery; the scheduled preparation job and ordinary prep lane can complete it.
                research = load(root / 'cache' / ('research_' + day + '.json'), {'attendees': []})
                if load(scratch / 'research-status.json', {}).get('status') == 'admission_busy':
                    research = dict(research) if isinstance(research, dict) else {'attendees': research}
                    research['status'] = 'capacity_pending'
                save(paths['research'], research)
                save(paths['x'], {})
                save(paths['granola'], cached_notes())
            if not allowed('granola'):
                save(paths['granola'], {'meetings': []})
            # Prepared attendee research and X context were selected from Calendar. A source
            # toggle after preparation must not carry those names into a new composition.
            if not allowed('calendar'):
                save(paths['research'], {'attendees': []})
                save(paths['x'], {})
            gmail = project_emails(load(paths['gmail'], []))
            save(paths['gmail'], gmail)
            emails = gmail if isinstance(gmail, list) else gmail.get('emails', [])
            events = load(paths['cal'], [])
            consumed_sources.update(used_sources(
                local, gmail, events, load(paths['granola'], {}), load(paths['x'], {}),
                already_projected=True))
            consumed_sources.update(email_dependency_sources(gmail))
            research = load(paths['research'], {})
            attendees = research.get('attendees') if isinstance(research, dict) else research
            x_context = load(paths['x'], {})
            if attendees or (isinstance(x_context, dict) and x_context.get('attendees')):
                consumed_sources.add('calendar')
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
            if any(not allowed(source) for source in consumed_sources):
                raise RuntimeError('source permission changed during brief preparation')
            continuity = {'today': day, 'signals': {}, 'local': local, 'emails': emails, 'events': events}
            save(paths['continuity'], continuity)
            command('morning-brief/scripts/continuity_resolve.py', '--resolve-only', paths['continuity'])
            composed = json.loads(command('_shared/scripts/compose_brief.py', '--type', request['kind'],
                '--local', paths['local'], '--local-resolved', '--effective-local-out', paths['local'],
                '--gmail', paths['gmail'], '--calendar', paths['cal'],
                '--granola', paths['granola'], '--knowledge', paths['knowledge'],
                '--source-results', paths['source_results'],
                '--attendee-research', paths['research'], '--x-context', paths['x']))
            if not isinstance(composed.get('brief_text'), str) or not composed['brief_text'].strip():
                raise RuntimeError('composer produced no chat text')
            # Compose can heal a partial Bridge read with permitted cached sources. Its preview
            # and the later durable resolver must inspect the same rows and source provenance.
            local = project_local(load(paths['local'], {}))
            save(paths['local'], local)
            continuity['local'] = local
            # Keep every source that earlier stages actually consumed. Rechecking only the
            # composer's currently permitted rows could erase a source revoked mid-run.
            composed['_source_permissions'] = sorted(consumed_sources | set(
                composed.get('_source_permissions') or []))
            composed['_source_cutoff'] = coverage_until
            composed['_calendar_eligibility'] = [
                {'kind': 'eligibility', 'source': 'calendar',
                 'calendar_event_id': event['id'], 'calendar_start': event['start'],
                 'calendar_observed_at': source_results.get('calendar', {}).get('observed_at') or coverage_until}
                for event in events if isinstance(event, dict) and event.get('id') and event.get('start')
                and event.get('my_response') != 'declined']
            knowledge_payload = dict(composed.get('extracted_knowledge') or {})
            if isinstance(composed.get('learning_evidence'), dict):
                knowledge_payload['_learning_evidence'] = {
                    key: int(composed['learning_evidence'].get(key, 0))
                    for key in ('accepted', 'rejected')
                    if isinstance(composed['learning_evidence'].get(key, 0), int)
                    and composed['learning_evidence'].get(key, 0) >= 0}
            save(paths['knowledge_out'], knowledge_payload)
            continuity['new_actions'] = composed.get('actions') or []
            continuity['loop_updates'] = composed.get('loop_updates') or []
            save(paths['continuity'], continuity)
            # Persist last: every input needed to resume required writes now exists atomically.
            composed['composed_at'] = time.time()
            composed['learning_revision'] = uuid.uuid4().hex
            save(scratch / 'artifact.json', composed)
            artifact = composed
        learning_key = run_key + ':' + artifact['learning_revision'] if artifact.get('learning_revision') else run_key
        stage_artifact(artifact)
        if not load(scratch / 'essential-done.json', {}).get('complete'):
            try:
                receipt = learn(paths, 'essential')
                save(scratch / 'essential-done.json', {'complete': True,
                     'learning_status': receipt['completion'], 'finished_at': time.time()})
            except Exception as error:  # learning is after the source/consent delivery gate
                # Consent/source validation and delivery effects are already staged. Preserve the
                # composed brief for delivery; the durable follow-up completes required writes.
                print(f'[brief_runner] essential learning deferred: {type(error).__name__}',
                      file=sys.stderr)
        # Count only the exact obligations represented in this saved artifact. Essential learning
        # may restate bookkeeping; a material change to an obligation invalidates its count.
        import delivery_effects
        import ledger_io
        live = {row.get('anchor_key'): row for row in ledger_io.load_entries()}
        represented = []
        for row in artifact.get('_represented_loops', []):
            current = live.get(row.get('anchor_key'))
            same_identity = (row.get('loop_identity')
                             and delivery_effects.loop_identity(current or {}) == row['loop_identity'])
            legacy_exact = (not row.get('loop_identity') and current
                            and delivery_effects.loop_version(current) == row.get('loop_version'))
            if current and current.get('status') in ledger_io.ACTIVE and (same_identity or legacy_exact):
                # Finalization checks the same identity, so a restatement between staging and the
                # channel's acceptance (the follow-up merge, a deadline edit, the evening's pass
                # while a morning brief waits on a slow channel) cannot drop the count either.
                represented.append({'kind': 'loop_surfaced', 'anchor_key': row['anchor_key'],
                                    'loop_identity': delivery_effects.loop_identity(current),
                                    'loop_version': delivery_effects.loop_version(current)})
        delivery_effects.stage(represented)
        if not load(scratch / 'ancillary-done.json', {}).get('complete'):
            try:
                followup()  # stable work key; a retry cannot duplicate successful learning
            except Exception as error:
                # Keep the archived inputs and pending essential marker for recovery. Failure of
                # background admission cannot withhold an already validated, composed brief.
                print(f'[brief_runner] learning follow-up admission failed: {type(error).__name__}',
                      file=sys.stderr)
        # Bind reusable content to exact delivery jobs. Only accepted outbox IDs may replay it.
        run_id = os.environ.get('SOTTO_DELIVERY_RUN_ID', '')
        if len(run_id) == 32 and all(c in '0123456789abcdef' for c in run_id):
            artifact['_delivery_run_ids'] = list(dict.fromkeys([
                *artifact.get('_delivery_run_ids', []), run_id]))[-16:]
            save(scratch / 'artifact.json', artifact)
        return artifact['brief_text']


if __name__ == '__main__':
    try:
        print(run(json.loads(sys.argv[1])))
    except BriefBusyError:
        print('[brief_runner] BriefBusyError', file=sys.stderr)
        sys.exit(75)  # Receiver refunds only this runner's explicit contention signal.
    except BriefModelBudgetError:
        from work_queue import MODEL_BUDGET_EXIT
        print('[brief_runner] Model allowance reached', file=sys.stderr)
        sys.exit(MODEL_BUDGET_EXIT)
    except BriefModelAdmissionBusyError:
        from work_queue import MODEL_ADMISSION_BUSY_EXIT
        print('[brief_runner] Model admission busy', file=sys.stderr)
        sys.exit(MODEL_ADMISSION_BUSY_EXIT)
    except Exception as error:
        print(f'[brief_runner] {type(error).__name__}: {error}', file=sys.stderr)
        sys.exit(1)
