"""On-demand original follow-up extraction and canonical capture, with held uncertain stages."""
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import tempfile
import time
from datetime import datetime, timezone

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from source_authority import current_authority  # noqa: E402
from pipeline_checkpoint import CheckpointStages  # noqa: E402


def run(pack, selection, command, *, authority=current_authority, client_factory=None, llm=None):
    import jsonstore
    import gather_granola
    from granola_history import bounded_factory
    from loops_query import query_permitted_rows
    from source_context import read_local, used_sources
    from timeutil import configured_user_email, configured_tz
    path = Path(pack) / 'followup/scripts/compose_followup.py'
    spec = importlib.util.spec_from_file_location('sotto_attended_followup', path)
    compose = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compose)
    if selection not in ({'mode': 'recent'}, {'mode': 'backlog'}):
        raise ValueError('followup selection invalid')
    days, transcript_hours, hours = (3, 36, 36) if selection['mode'] == 'recent' else (14, 0, 336)
    proof = json.loads(os.environ['SOTTO_SKILL_COMPOSITION_PROOF'])
    if (set(proof) != {'source_revision', 'route_digest'}
            or any(type(v) is not str or len(v) != 64 for v in proof.values())):
        raise RuntimeError('followup composition proof unavailable')
    job = os.environ['SOTTO_DELIVERY_RUN_ID']
    binding = hashlib.sha256(json.dumps([selection, proof], sort_keys=True).encode()).hexdigest()
    root = Path(os.environ['SOTTO_DATA'])
    artifacts = root / 'events/research-artifacts'
    artifacts.mkdir(parents=True, exist_ok=True)
    checkpoint_path = str(artifacts / ('followup-' + job + '.json'))
    with jsonstore.lock(str(artifacts / ('lock-followup-' + hashlib.sha256(job.encode()).hexdigest()[:2]))):
        checkpoint = jsonstore.read(checkpoint_path, default={'binding': binding}, strict=True)
        if checkpoint.get('binding') != binding:
            raise RuntimeError('followup composition authority changed')

        def guard():
            current = authority()
            if (current.get('complete') is not True or current.get('revision') != proof['source_revision']
                    or 'granola' not in current.get('connector_sources', [])):
                raise RuntimeError('followup source authority unavailable')
            return current

        stage = CheckpointStages(checkpoint, lambda: jsonstore.write_atomic(checkpoint_path, checkpoint),
            guard, 'followup stage outcome unconfirmed; contact remains held')

        def snapshot():
            current = guard()
            permitted = current.get('bridge_sources', []) + current.get('google_sources', []) + current.get('connector_sources', [])
            meetings, warnings = gather_granola.gather_mcp_with_retry(days, transcript_hours,
                client_factory=client_factory or bounded_factory(time.monotonic() + 45))
            if any(warning != gather_granola.NO_TRANSCRIPT_TOOL_WARNING
                   for warning in warnings):
                raise RuntimeError('followup Granola retrieval unavailable')
            local = read_local() if current.get('bridge_sources') else {}
            google = {'events': [], 'userEmail': configured_user_email() or ''}
            if 'calendar' in permitted:
                with tempfile.TemporaryDirectory(prefix='sotto-followup-') as directory:
                    calendar = Path(directory) / 'calendar.json'
                    command('_shared/scripts/gather_google.py', '--skip-gmail', '--cal-out', calendar,
                        '--gmail-out', Path(directory) / 'gmail.json', decode=False)
                    value = json.loads(calendar.read_text())
                    google['events'] = value if isinstance(value, list) else value.get('events', [])
            # Protected loop context may depend on any currently permitted source.
            sources = sorted(set(used_sources(local, [], google['events'], already_projected=True)) | set(permitted))
            return {'inputs': {'granola': meetings, 'local': local, 'google': google,
                'user_email': google['userEmail'], 'user_timezone': configured_tz() or '',
                'open_loops': query_permitted_rows(str(root), permitted)},
                'permitted': permitted, 'sources': sources,
                'coverage_until': datetime.now(timezone.utc).isoformat()}

        captured = stage('snapshot', snapshot)
        inputs = captured['inputs']
        def extract():
            if not any(m.get('transcript') or m.get('your_notes') or m.get('ai_summary') for m in inputs['granola']):
                return {'out': {'followup_markdown': 'I do not have meeting transcripts or notes in this window. Please check the Granola connection.',
                                'commitments': [], 'drafts': []}, 'ended': []}
            _, ended = compose.build_context(inputs, hours)
            return {'out': compose.compose(inputs, hours, llm=llm), 'ended': ended}
        extraction = stage('compose', extract)
        out, ended = extraction['out'], extraction['ended']
        exposed = {row.get('anchor_key') for key in ('you_owe', 'waiting_on_them')
                   for row in inputs['open_loops'].get(key, [])[:40]}
        # The model may reconcile only anchors actually exposed in the permitted prompt.
        for commitment in out.get('commitments', []):
            if commitment.get('existing_anchor_key') not in exposed:
                commitment.pop('existing_anchor_key', None)
        ledger = stage('apply', lambda: compose._apply_ledger(out, inputs['user_email'], ended))
        result = stage('result', lambda: {**out, 'ledger': ledger,
            'open_loops': query_permitted_rows(str(root), captured['permitted'])})
        guard()
        import pending_offer
        evidence_emails = {email.rstrip('.') for email in re.findall(
            r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+',
            json.dumps(ended, ensure_ascii=False))}
        text = result['followup_markdown']
        if selection['mode'] == 'backlog':
            text += '\n\nOpen loops after capture:'
            for key, heading in (('you_owe', 'You owe'), ('waiting_on_them', 'Waiting on')):
                rows = result['open_loops'].get(key, [])
                text += '\n' + heading + ' (' + str(len(rows)) + '):'
                for row in rows:
                    text += '\n- ' + str(row.get('name') or row.get('who') or row.get('contact') or row.get('contact_name') or 'Unspecified person') + ': ' + str(row.get('what') or row.get('summary') or row.get('ask') or '')
            if ledger.get('skipped_terminal'):
                text += '\nAlready closed and not reopened: ' + str(ledger['skipped_terminal'])
        for draft in result.get('drafts', []):
            detail = {key: draft.get(key) for key in ('to_name', 'to_email', 'channel', 'subject', 'body')}
            detail['to_name'] = detail['to_name'] or ''
            if detail['channel'] in ('imessage', 'sms', 'whatsapp'):
                app = 'WhatsApp' if detail['channel'] == 'whatsapp' else 'Messages'
                text += '\n\nDraft for ' + str(detail['to_name'] or 'unspecified person') + ' (not sent):\n'
                text += str(draft.get('body') or draft.get('draft') or '')
                text += '\nPaste this into the existing conversation in ' + app + '.'
                continue
            if detail['to_email'] not in evidence_emails:
                detail['to_email'] = None
            detail.update(version=1, sources=captured['sources'], source_revision=proof['source_revision'])
            encoded = json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            question = 'Would you like to review this follow-up for Gmail drafts?' if detail['to_email'] and detail['channel'] == 'email' else 'Which channel and address should this draft use?'
            pending_offer.set_offer('followup_draft', question, person=detail['to_name'] or '',
                detail=encoded, payload_sha256=hashlib.sha256(encoded.encode()).hexdigest())
            text += '\n\nDraft for ' + str(detail['to_name'] or 'unspecified person') + ' (not sent):\n'
            text += 'Subject: ' + str(detail['subject'] or 'not provided') + '\n'
            text += str(draft.get('body') or draft.get('draft') or '')
            text += '\nDestination: ' + str(detail['to_email'] or 'not provided; please supply channel and address') + '\n' + question
        return {**result, 'text': text, 'sources': captured['sources'], 'coverage_until': captured['coverage_until']}
