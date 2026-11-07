"""Attended original relationship pulse; uncertain state writes are never repeated."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone, timedelta
from math import ceil

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from source_authority import current_authority  # noqa: E402
from pipeline_checkpoint import CheckpointStages  # noqa: E402

BRIDGE_REFRESH_TIMEOUT_SECONDS = 8


def run(pack, selection, command, *, authority=current_authority):
    import jsonstore
    from source_context import SOURCE_FIELDS, _local_statuses, bridge_call, project_local
    from preferences import _sync_existing_preferences
    from local_snapshot import _local_fallback, merge_local_unsaved
    from loops_query import query_permitted_rows
    from source_catalog import LOCAL_SNAPSHOT_TTL_HOURS
    from timeutil import _now_local, configured_tz
    if selection != {'mode': 'current'}:
        raise ValueError('relationship pulse selection invalid')
    path = Path(pack) / 'relationship-pulse/scripts/relationship_pulse.py'
    spec = importlib.util.spec_from_file_location('sotto_attended_relationship_pulse', path)
    pulse = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pulse)
    proof = json.loads(os.environ['SOTTO_SKILL_COMPOSITION_PROOF'])
    if (set(proof) != {'source_revision', 'route_digest'}
            or any(type(v) is not str or len(v) != 64 for v in proof.values())):
        raise RuntimeError('relationship pulse proof unavailable')
    job = os.environ['SOTTO_DELIVERY_RUN_ID']
    binding = hashlib.sha256(json.dumps([selection, proof], sort_keys=True).encode()).hexdigest()
    artifacts = Path(os.environ['SOTTO_DATA']) / 'events/research-artifacts'
    artifacts.mkdir(parents=True, exist_ok=True)
    checkpoint_path = str(artifacts / ('pulse-' + job + '.json'))
    with jsonstore.lock(str(artifacts / ('lock-pulse-' + hashlib.sha256(job.encode()).hexdigest()[:2]))):
        checkpoint = jsonstore.read(checkpoint_path, default={'binding': binding}, strict=True)
        if checkpoint.get('binding') != binding:
            raise RuntimeError('relationship pulse authority changed')

        def guard():
            current = authority()
            if current.get('complete') is not True or current.get('revision') != proof['source_revision']:
                raise RuntimeError('relationship pulse authority unavailable')
            return current

        def save():
            jsonstore.write_atomic(checkpoint_path, checkpoint)
            _sync_existing_preferences(checkpoint_path)

        stages = CheckpointStages(checkpoint, save, guard, 'relationship pulse stage unconfirmed; held')

        def stage(name, invoke):
            result = stages(name, invoke)
            # Re-prove durability on replay, including the independently persisted pulse state.
            _sync_existing_preferences(checkpoint_path)
            if name == 'persist':
                _sync_existing_preferences(pulse._state_path())
            return result

        def snapshot():
            current = guard()
            granted = current.get('bridge_sources', [])
            now = datetime.now(timezone.utc)
            permitted = set(granted) | set(current.get('google_sources', []))
            history = {key: row for key, row in pulse._load_history().items()
                       if isinstance(row, dict) and isinstance(row.get('channels'), dict) and row['channels']
                       and {('gmail' if ch == 'email' else ch) for ch in row['channels']} <= permitted}
            # Reuse the canonical consent-projected snapshot and history cursor, not a second cache.
            local = _local_fallback({})
            for source, fields in SOURCE_FIELDS.items():
                if source not in granted:
                    for field in fields:
                        local.pop(field, None)
            learned = jsonstore.read(str(Path(os.environ['SOTTO_DATA']) / 'knowledge/history-state.json'), default={})
            def observed_since(source):
                window = (learned.get('sources') or {}).get(source) or {}
                value = (window.get('until') if window.get('complete') else window.get('since')
                         if window.get('initial_complete') else None)
                if type(value) is int and 0 < value <= now.timestamp():
                    return max(value - 1, int((now - timedelta(days=42)).timestamp()))
                stamp = pulse._aware_ts((local.get('_source_observed_at') or {}).get(source))
                if stamp and stamp <= now:
                    return int((stamp - timedelta(hours=1)).timestamp())
                return int((now - timedelta(hours=LOCAL_SNAPSHOT_TTL_HOURS)).timestamp())
            def covers_history(source, since):
                cutoff = (now - timedelta(days=42)).timestamp()
                window = (learned.get('sources') or {}).get(source) or {}
                return (window.get('initial_complete') is True and window.get('complete') is True
                    and not window.get('excluded_pages')
                    and type(window.get('initial_since')) is int and window['initial_since'] <= cutoff
                    and type(window.get('until')) is int and since <= window['until'])
            bridge_ok, gmail_ok, partial = False, False, False
            complete = []
            bridge_missing, gmail_error = [], ''
            if granted:
                try:
                    since = min((observed_since(source) for source in granted if source in pulse.MESSAGE_CHANNELS),
                                default=int(now.timestamp()))
                    raw = bridge_call('read_local', {'since_hours': min(1008, max(1, ceil((now.timestamp() - since) / 3600)))}, timeout=BRIDGE_REFRESH_TIMEOUT_SECONDS)
                    local = merge_local_unsaved(raw)  # never rewrites the brief's snapshot
                    statuses = _local_statuses(raw.get('source_status'))
                    for source, fields in SOURCE_FIELDS.items():
                        if source not in granted or statuses.get(source) not in ('ok', 'partial'):
                            for field in fields:
                                local.pop(field, None)
                    bridge_missing = [source for source in pulse.MESSAGE_CHANNELS
                                      if source in granted and statuses.get(source) != 'ok']
                    actual_since = now.timestamp() - min(1008, max(1, ceil((now.timestamp() - since) / 3600))) * 3600
                    complete.extend(source for source in pulse.MESSAGE_CHANNELS if source in granted
                                    and statuses.get(source) == 'ok' and covers_history(source, actual_since))
                    bridge_ok = any(statuses.get(source) in ('ok', 'partial') for source in granted if source in ('imessage', 'whatsapp', 'calls'))
                except Exception:
                    local = project_local(local)
                    bridge_missing = [source for source in pulse.MESSAGE_CHANNELS if source in granted]
            emails = []
            if 'gmail' in current.get('google_sources', []):
                try:
                    with tempfile.TemporaryDirectory(prefix='sotto-pulse-') as directory:
                        gmail, receipts = Path(directory) / 'gmail.json', Path(directory) / 'sources.json'
                        gmail_since = observed_since('gmail')
                        command('_shared/scripts/gather_google.py', '--pulse-since-epoch', gmail_since,
                            '--skip-stale', '--skip-calendar',
                            '--gmail-out', gmail, '--cal-out', Path(directory) / 'calendar.json',
                            '--source-results-out', receipts, decode=False)
                        value = json.loads(gmail.read_text())
                        emails = value.get('emails', []) if isinstance(value, dict) else value
                        receipt = json.loads(receipts.read_text()).get('gmail', {})
                        status = receipt.get('status')
                        # Only bounded receipt codes enter the response, never provider text.
                        gmail_error = receipt.get('error') if receipt.get('error') in (
                            'rate_limited', 'RefreshError', 'PermissionError') else ''
                        gmail_ok = status in ('ok', 'partial')
                        # The gather's own receipt: `partial` when a read hit its limit or quota.
                        partial = status == 'partial'
                        if status == 'ok' and receipt.get('complete') is True and covers_history('gmail', gmail_since):
                            complete.append('email')
                        if not gmail_ok:
                            emails = []
                except Exception:
                    emails = []
            # Granted Google is the authoritative email lane, including an empty result.
            local['emails'] = emails
            return {'local': local, 'bridge_ok': bridge_ok, 'gmail_ok': gmail_ok, 'partial': partial,
                'gmail_error': gmail_error, 'bridge_missing': bridge_missing,
                'complete_channels': complete,
                'history_available': bool(history) and any(
                    isinstance(row, dict) and row.get('initial_complete') is True
                    for source, row in (learned.get('sources') or {}).items() if source in permitted),
                'loops': query_permitted_rows(os.environ['SOTTO_DATA'], sorted(permitted), include_request_context=True),
                'sources': sorted(permitted),
                'coverage_until': now.isoformat(), 'history': history}

        captured = stage('snapshot', snapshot)
        now = datetime.fromisoformat(captured['coverage_until'])
        # A snapshot checkpointed before coverage was recorded proves no absence.
        result = stage('compute', lambda: pulse.with_commitments(
            pulse.compute(captured['local'], now=now, history=captured['history'],
                          absence_complete=captured.get('complete_channels', [])),
            captured.get('loops', {}), now=_now_local(configured_tz() or '+00:00')))
        if not result.get('degraded'):
            def persist():
                pulse._persist_state(result, now=now)
                _sync_existing_preferences(pulse._state_path())
            stage('persist', persist)
        guard()
        coverage = ['From saved commitments and recent activity.']
        if not captured.get('history_available'):
            coverage.append('Historical relationship learning is not yet available; this check uses recent activity and saved commitments.')
        if not captured['bridge_ok']:
            coverage.append('Fresh message check unavailable; retained permitted observations may be older.')
        if not captured['gmail_ok']:
            coverage.append('Fresh email check unavailable; retained permitted observations may be older.')
        if not captured['bridge_ok'] and not captured['gmail_ok']:
            coverage.append('Fresh message and email checks unavailable.')
        if captured['bridge_missing']:
            coverage.append('Message coverage is partial or unavailable for: ' + ', '.join(captured['bridge_missing']) + '.')
        if captured['partial']:
            coverage.append('Latest email check was incomplete; some recent messages may be missing.')
        if captured.get('gmail_error') == 'rate_limited':
            coverage.append('Gmail rate limit prevented a complete email history read.')
        elif captured.get('gmail_error') in ('RefreshError', 'PermissionError'):
            coverage.append('Gmail authorization prevented a complete email history read.')
        return {'text': '\n'.join(coverage + [result['pulse_markdown']]),
                'sources': captured['sources'], 'coverage_until': captured['coverage_until']}
