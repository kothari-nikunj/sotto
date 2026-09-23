"""Offline delivery contract, also run as the unprivileged user during image builds.

Only external boundaries are fixtures: source input, Hermes CLI and Photon sidecar.
Receiver, scanner, composer templates, adapters, outbox and effects are real. This
proves our delivery contract, not live provider availability or device display.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
BODY = ('Needs Attention Now\nAlex: Review the proposal today.\n\nShould Handle Today\n'
        'Sam: Confirm lunch for tomorrow.\n\nComing Up\n'
        '9:30 AM: Planning meeting (100 Market St)\n\nStill open\n'
        'Dana: Send the promised document.')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@contextmanager
def transport(root):
    """Real CLI subprocess/loopback HTTP with synthetic provider replies."""
    state, calls = root / 'transport.json', root / 'transport.jsonl'
    write(state, {'text': {'success': True, 'message_id': 'test-text'},
                  'gallery': {'ok': True, 'messageId': 'test-gallery',
                              'messageIds': ['caption', 'photo-1', 'photo-2', 'photo-3', 'photo-4']}})
    executable = root / 'bin/hermes'
    executable.parent.mkdir()
    executable.write_text(f'#!{sys.executable}\n' + '''import json, sys
from pathlib import Path
root = Path(__file__).resolve().parents[1]
assert sys.argv[1:3] == ['send', '--to'] and sys.argv[4:] == ['--json', '-f', '-']
assert sys.stdin.read().strip(), 'empty send'
with (root / 'transport.jsonl').open('a') as f:
    f.write(json.dumps({'kind': 'text', 'target': sys.argv[3]}) + '\\n')
print(json.dumps(json.loads((root / 'transport.json').read_text())['text']))
''')
    executable.chmod(0o700)
    os.environ['PATH'] = str(executable.parent) + os.pathsep + os.defpath

    class Sidecar(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.headers.get('X-Hermes-Sidecar-Token') == 'synthetic-token'
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/gallery-capability':
                reply = {'galleryVersion': 1}
            else:
                assert self.path == '/send-gallery' and value['spaceId'] == 'synthetic-owner'
                assert value['summary'] and len(value['paths']) == 4
                assert all(Path(p).read_bytes().startswith(b'\x89PNG\r\n\x1a\n') for p in value['paths'])
                with calls.open('a') as stream:
                    stream.write(json.dumps({'kind': 'gallery', 'images': len(value['paths'])}) + '\n')
                reply = json.loads(state.read_text())['gallery']
            encoded = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Sidecar)
    write(root / 'home/.hermes/runtime/photon-sidecar.json',
          {'port': server.server_port, 'token': 'synthetic-token'})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, lambda: [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def rows(receiver):
    return {r['id']: r for r in json.loads(Path(receiver.OUTBOX.path()).read_text())['rows']}


def due(receiver, key):
    # Advance only the saved retry deadline, without bypassing validity or ACKs.
    with receiver.CONNECTORS.json_transaction(receiver.OUTBOX.path(), default={}) as doc:
        row = next(r for r in doc['rows'] if r['id'] == key)
        row['next_at'] = row['effects_next_at'] = 0


def accepted(receiver, key):
    row = rows(receiver)[key]
    assert row['status'] == 'delivered' and row['acceptance'] == 'accepted', row
    assert row['receipt']['message_id'] and row['receipt']['accepted_at']
    assert row['effects_status'] == 'applied' and not row['effects_pending'], row


def proactive(receiver, root, pack):
    """A real nonempty scan must reach a receipt; an empty tick cannot satisfy this check."""
    procedure = load('checked_procedure', HERE / 'procedure_runner.py')
    receiver._shared_effects()
    import source_context
    import gemini
    now = datetime.now(timezone.utc)
    meeting = {'id': 'synthetic-meeting', 'summary': 'Alex planning',
               'start': (now + timedelta(minutes=30)).isoformat(),
               'end': (now + timedelta(minutes=60)).isoformat(),
               'attendees': [{'email': 'alex@example.org', 'displayName': 'Alex', 'status': 'accepted'}]}
    real_run = subprocess.run

    def gather(argv, **kwargs):
        if len(argv) > 1 and Path(argv[1]).name == 'gather_google.py':
            write(Path(argv[argv.index('--cal-out') + 1]), [meeting])
            write(Path(argv[argv.index('--gmail-out') + 1]), [])
            return subprocess.CompletedProcess(argv, 0, 'Synthetic calendar ready\n', '')
        return real_run(argv, **kwargs)

    ident = 'a' * 32
    os.environ['SOTTO_DELIVERY_RUN_ID'] = ident
    with patch.object(source_context, 'read_local', return_value={}), \
            patch.object(procedure.subprocess, 'run', side_effect=gather), \
            patch.object(gemini, 'model_once', side_effect=AssertionError('contract must not call a model')):
        text = procedure.run({'kind': 'proactive', 'pack': str(pack)})
    staged = json.loads((root / f'events/delivery-effects-{ident}.json').read_text())
    assert 'Alex' in text and text != 'NO_NUDGES', 'nonempty scan was suppressed'
    assert {'unsolicited_nudge', 'eligibility', 'proactive_seen'} <= {e['kind'] for e in staged['effects']}
    assert receiver._deliver_text(text, 'cron:sotto-proactive', run_id=ident, effects=staged['effects'])
    accepted(receiver, ident)
    # The scanner may have crossed UTC midnight after this harness created the meeting.
    seen_effect = next(e for e in staged['effects'] if e['kind'] == 'proactive_seen')
    date, occurrence_key = seen_effect['date'], seen_effect['key']
    state = json.loads((root / f'proactive/{date}.json').read_text())
    assert occurrence_key.startswith('mtg:synthetic-meeting@')
    assert occurrence_key in state['nudged'] and occurrence_key not in state['pending']
    os.environ.pop('SOTTO_DELIVERY_RUN_ID')


def check():
    pack = next(p for p in (HERE.parent / 'sotto-skills', HERE.parents[1] / 'sotto-chief-of-staff')
                if (p / '_shared').is_dir())
    mode = os.environ.get('SOTTO_DEPLOYMENT_MODE', 'managed')
    with tempfile.TemporaryDirectory(prefix='sotto-runtime-check-') as scratch:
        root = Path(scratch)
        # Always a subprocess. Never inherit live credentials or owner preferences.
        os.environ.clear()
        os.environ.update(HOME=str(root / 'home'), HERMES_HOME=str(root / 'home/.hermes'),
                          SOTTO_DATA=scratch, SOTTO_SKILLS_ROOT=str(pack), SOTTO_DEPLOYMENT_MODE=mode,
                          SOTTO_TENANT_ID='synthetic-tenant', PHOTON_HOME_CHANNEL='synthetic-owner',
                          SOTTO_CRON_DELIVER='photon', SOTTO_VISUAL_BRIEFS='1', SOTTO_TIMEZONE='UTC',
                          SOTTO_USER_EMAIL='owner@example.com', SOTTO_QUIET_START='0', SOTTO_QUIET_END='0',
                          SOTTO_NUDGE_BUDGET='10', PYTHONDONTWRITEBYTECODE='1')
        write(root / 'config/managed-capabilities.json',
              {'tenant_id': 'synthetic-tenant', 'sources': {'calendar': {'connected': True, 'consented': True}}})
        receiver = load('checked_receiver', HERE / 'receiver.py')
        effects = receiver._shared_effects()
        with transport(root) as (state, calls):
            proactive(receiver, root, pack)
            for kind in ('morning', 'evening'):
                day = receiver.DASHBOARD._local_today()
                write(root / f'briefs/{day}_{kind}.json', {'body': BODY})
                write(root / f'briefs/{day}.{kind}.learned.json', {'steps': {}})
                label, ident = f'cron:sotto-{kind}-brief', f'contract-{kind}'
                body = f'Good {kind} - {day}\n\n' + BODY
                before = len(calls())
                assert receiver._deliver_text(body, label, run_id=ident,
                                              effects=[{'kind': 'source_permissions', 'sources': ['calendar']}])
                accepted(receiver, ident)
                assert calls()[before:] == [{'kind': 'gallery', 'images': 4}], 'brief fell back to text'
                assert len(rows(receiver)[ident]['receipt']['message_ids']) == 5
                assert (root / 'events/last_digest.txt').exists()
                assert Path(receiver.delivered_marker(day, kind)).read_text() == ident
                receiver._deliver_text(body, label, run_id=ident)
                receiver._deliver_text(body, label, run_id=ident + '-duplicate')
                assert len(calls()) == before + 1, 'brief sent twice'

            # CLI success without a message ID must remain pending, on both channels.
            for channel in ('photon', 'telegram'):
                os.environ['SOTTO_CRON_DELIVER'] = channel
                value = json.loads(state.read_text())
                value['text'] = {'success': True}
                write(state, value)
                ident = 'retry-' + channel
                assert not receiver._deliver_text('Synthetic follow-up', 'event:contract', run_id=ident)
                row = rows(receiver)[ident]
                assert row['status'] == 'pending' and row['acceptance'] == 'unknown' and not row.get('receipt')
                value['text'] = {'success': True, 'message_id': ident}
                write(state, value)
                # Reconstruct receiver and outbox from disk; no in-memory success may survive.
                receiver = load('restarted_receiver', HERE / 'receiver.py')
                due(receiver, ident)
                receiver.OUTBOX.drain()
                accepted(receiver, ident)
                count = len(calls())
                receiver.OUTBOX.drain()
                assert len(calls()) == count, 'accepted message resent after restart'

            os.environ['SOTTO_CRON_DELIVER'] = 'photon'
            value = json.loads(state.read_text())
            value['gallery'] = {'ok': True}  # HTTP 200 alone is not acceptance.
            write(state, value)
            before = len(calls())
            assert not receiver._deliver_text('Good morning\n\n' + BODY, 'preview:morning-brief',
                                             run_id='ambiguous-gallery')
            row = rows(receiver)['ambiguous-gallery']
            assert row['status'] == 'pending' and row['acceptance'] == 'unknown'
            receiver = load('recovered_gallery_receiver', HERE / 'receiver.py')
            due(receiver, 'ambiguous-gallery')
            receiver.OUTBOX.drain()
            assert calls()[before:] == [{'kind': 'gallery', 'images': 4}], 'ambiguous gallery duplicated'

            os.environ['SOTTO_NUDGE_BUDGET'] = '0'
            before = len(calls())
            bundle = effects.for_bundle({'events': [{'event': {}}]})
            assert not receiver._deliver_text('Muted notification', 'event:disabled', run_id='disabled',
                                             effects=bundle['effects'])
            assert rows(receiver)['disabled']['status'] == 'superseded' and len(calls()) == before
        return {'delivery_helpers': 'ok', 'brief_images': 4, 'nudge_validity': 'ok',
                'scheduled_nudge': 'accepted', 'briefs': ['morning', 'evening'],
                'restart_recovery': 'ok', 'ambiguous_gallery': 'held'}


if __name__ == '__main__':
    print(json.dumps(check()))
