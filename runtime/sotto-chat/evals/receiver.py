"""Isolated conversation-eval peer: real receiver, synthetic owner and saved world.

No live source or delivery credentials. The Node runner owns the real model connection.
This is a developer harness, never imported by the installed receiver.
"""
import json
import importlib.util
import hashlib
from datetime import datetime, timezone
import re
import os
from pathlib import Path
import sys
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

def network_guard(event, args):
    if event == 'socket.connect':
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
            with Path(sys.argv[1]).with_name('source-reads.jsonl').open('a') as log:
                log.write(json.dumps({'source': 'denied', 'reason': 'external receiver connection'}) + '\n')
            raise PermissionError('Conversation eval blocks external receiver connections')

sys.addaudithook(network_guard)

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / 'sotto-chief-of-staff'
sys.path[:0] = [str(ROOT / 'runtime/trigger-receiver'), str(ROOT)]
data = Path(sys.argv[2]).resolve()
if not data.is_dir() or any(data.iterdir()):
    raise ValueError('Eval receiver requires an empty, explicit data directory')
os.environ['SOTTO_DATA'] = str(data)

import receiver  # noqa: E402
from adapters.sotto import startup_config  # noqa: E402

world = json.loads(Path(sys.argv[1]).read_text())
if world.get('meetingPrep'):
    tokens = importlib.util.spec_from_file_location('eval_clock_tokens', PACK / 'evals/run_evals.py')
    clock_tokens = importlib.util.module_from_spec(tokens)
    tokens.loader.exec_module(clock_tokens)
    world = clock_tokens.resolve_tokens(world, datetime.now(timezone.utc))
    Path(sys.argv[1]).write_text(json.dumps(world))
    os.environ.update(SOTTO_USER_EMAIL='owner@inside.test', SOTTO_GEMINI_MODEL='gemini-3.8-flash', SOTTO_FALLBACK_MODEL='')

if ('contacts' in world or 'whatsappMessages' in world) and world.get('bridgeSend') is True:
    raise ValueError('Mac read and Bridge-send worlds require a combined transport fixture; do not silently replace either source')
os.environ['SOTTO_EVAL_WORLD'] = str(Path(sys.argv[1]).resolve())
os.environ['PYTHONPATH'] = str(Path(__file__).parent / 'provider')
os.environ['SOTTO_MODEL_PROXY_URL'] = 'https://eval-source.invalid'
os.environ['SOTTO_MODEL_PROXY_TOKEN'] = 'fixture-source-token'
os.environ['GOOGLE_AI_API_KEY'] = 'fixture-source-key'
# Apply the same HTTP source fixture to inline reads and worker subprocesses.
sys.path.insert(0, str(Path(__file__).parent / 'provider'))
source_spec = importlib.util.spec_from_file_location('eval_sources', Path(__file__).parent / 'provider/sitecustomize.py')
source_module = importlib.util.module_from_spec(source_spec)
source_spec.loader.exec_module(source_module)
if world.get('gmailTriage') is True:
    source_module.install_triage(receiver)
if world.get('meetingPrep'):
    # The worker loads this source fixture as sitecustomize. Check that exact child
    # environment before spending model calls; a host sitecustomize can hide missing deps.
    import subprocess
    checked = subprocess.run([sys.executable, '-c', 'import googleapiclient, google.auth, requests'],
                             capture_output=True, timeout=15)
    if checked.returncode:
        raise RuntimeError('Prep eval worker dependencies unavailable under the source fixture; use a dependency-complete virtual environment')
receiver.DATA = str(data)
receiver._find_sotto_script = lambda *parts: str(PACK.joinpath(*parts))
# Local checkout has the same code but not the image's root-owned filesystem.
profile = receiver._runtime_adapter()._fixed_five_module()
profile._PACKAGED_ROOT = PACK
profile._PACKAGED_RECEIVER = ROOT / 'runtime/trigger-receiver'
profile._image_owned = lambda meta: not meta.st_mode & 0o022
os.environ['SOTTO_SKILLS_ROOT'] = str(PACK)

# Explicit source grants are fixtures, not production consent. Optional synthetic
# contacts still travel through the real relay's request and response checks.
saved_grants = world.get('savedSourceGrants', ['gmail'] if 'loopTasks' in world else [])
if (type(saved_grants) is not list or any(type(source) is not str or source not in
        {'gmail', 'calendar', 'google_contacts', 'imessage', 'whatsapp', 'contacts', 'granola'} for source in saved_grants)):
    raise ValueError('Invalid synthetic saved-source grants')
google_scopes = {
    'gmail': ('gmailMessages', 'https://www.googleapis.com/auth/gmail.readonly'),
    'calendar': ('calendarEvents', 'https://www.googleapis.com/auth/calendar.readonly'),
}


def google_grants():
    grants = set(saved_grants) & {'gmail', 'calendar', 'google_contacts'}
    if 'config/managed-capabilities.json' not in world.get('files', {}):
        return sorted(grants)
    return sorted(source for source in grants if source not in google_scopes
                  or google_scopes[source][0] not in world or receiver._source_context().allowed(source))


def authority():
    return {'version': 2, 'complete': True, 'revision': 'a' * 64,
            'bridge_sources': sorted(set(saved_grants) & {'imessage', 'whatsapp', 'contacts'}
                                     | ({'contacts'} if 'contacts' in world else set())),
            'google_sources': google_grants(),
            'connector_sources': sorted(set(saved_grants) & {'granola'})}

receiver._runtime_source_authority = authority
if world.get('commitmentJourney', {}).get('actualModel') is True:
    # Install only the empty frozen transport initially; each journey stage reveals its rows.
    world['gmailMessages'] = {}
    Path(sys.argv[1]).write_text(json.dumps(world))
if 'gmailMessages' in world or 'calendarEvents' in world:
    receiver._google_api_py = lambda: str(Path(__file__).parent / 'provider/sitecustomize.py')
owner = os.environ['PHOTON_HOME_CHANNEL']
line = os.environ['SOTTO_IMESSAGE_NUMBER']
guid = f'iMessage;-;{owner}'
receiver._sotto_adapter('photon_owner').record_owner_activation(
    SimpleNamespace(chat_type='dm', user_id=owner, chat_id=guid))
identity = {'tenant_id': 'eval-tenant', 'channel': 'photon', 'account_id': 'eval-account',
            'owner_id': owner, 'line_key': 'eval-line', 'target': 'photon',
            'route_mode': 'fixed', 'route_id': f'imessage:{guid}~{line}',
            'project_fingerprint': receiver._sotto_adapter('photon_owner').project_fingerprint('eval-project')}
capability = receiver.OWNER_LOOP_CAPABILITY.derive(os.environ['SOTTO_MCP_TOKEN'], identity)

sys.path.insert(0, str(PACK / '_shared/knowledge'))
import knowledge_update  # noqa: E402
knowledge_update.apply(world.get('knowledge', {}))
for relative, content in world.get('files', {}).items():
    target = (data / relative).resolve()
    if not target.is_relative_to(data.resolve()) or target == data.resolve():
        raise ValueError('fixture path escapes sandbox')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(content)
    target.chmod(0o600)

scopes = [scope for source, (field, scope) in google_scopes.items()
          if source in google_grants() and field in world]
if world.get('meetingPrep'):
    scopes = [scope for scope in scopes if not scope.endswith('/calendar.readonly')]
    scopes.append('https://www.googleapis.com/auth/calendar')
if scopes:
    from prep_sources import seed_google_connection
    seed_google_connection(os.environ['SOTTO_HOME'], scopes)

# Canonical task fixtures use the real writer in this isolated receiver volume only.
if 'loopTasks' in world:
    import runtime_owner_loop
    fixture_writer=runtime_owner_loop._load_writer()
    for row in world['loopTasks']:
        fixture_writer._load_continuity()._persist(row)

def frozen_whatsapp(world):
    rows = {}
    for row in world.get('whatsappMessages', []):
        ident = row['source_id']
        stamp = datetime.fromisoformat(row['timestamp'])
        if (type(ident) is not int or ident < 1 or ident in rows or stamp.tzinfo is None
                or not re.fullmatch(r'[0-9]+@s\.whatsapp\.net', row['contact_jid'])
                or not isinstance(row.get('text'), str) or not row['text'].strip()):
            raise ValueError('Invalid frozen WhatsApp message')
        rows[ident] = row, stamp.timestamp()
    return dict(sorted(rows.items(), reverse=True))


MAX_DIRECT_MESSAGES = 1000
MAX_HISTORY_PAGE = 500
LOCAL_SNAPSHOT_HOURS = 24


def whatsapp_read(messages, name, args):
    if args.get('source') != 'whatsapp':
        raise ValueError('Expected WhatsApp source')
    if name == 'get_messages':
        jid, limit = args.get('identifier'), args.get('limit', 20)
        if (set(args) - {'source', 'identifier', 'limit'} or type(limit) is not int or not 1 <= limit <= MAX_DIRECT_MESSAGES
                or not isinstance(jid, str) or not re.fullmatch(r'[0-9]+@s\.whatsapp\.net', jid)):
            raise ValueError('Invalid direct WhatsApp chat request')
        rows = [row for row, _ in messages.values() if row['contact_jid'] == jid][:limit]
        return {'source': 'whatsapp', 'identifier': jid, 'messages': rows}, rows
    since, until, before, limit = (args.get('since'), args.get('until'), args.get('before', 0), args.get('limit', MAX_HISTORY_PAGE))
    if (set(args) - {'source', 'since', 'until', 'before', 'limit'}
            or any(type(value) is not int for value in (since, until, before, limit))
            or not 0 <= since <= until or not 1 <= limit <= MAX_HISTORY_PAGE
            or before and (before not in messages or not since <= messages[before][1] <= until)):
        raise ValueError('Invalid WhatsApp history window or cursor')
    rows = [row for ident, (row, stamp) in messages.items()
            if since <= stamp <= until and (not before or ident < before)][:limit]
    return {'source': 'whatsapp', 'status': 'ok', 'rows': rows, 'since': since, 'until': until,
            'complete': len(rows) < limit, 'next_cursor': rows[-1]['source_id'] if len(rows) == limit else None}, rows


def local_snapshot(world, messages, args):
    sources = args.get('sources')
    hours = args.get('since_hours')
    if (set(args) != {'sources', 'since_hours'} or type(hours) is not int or not 1 <= hours <= LOCAL_SNAPSHOT_HOURS
            or not isinstance(sources, list) or not sources
            or any(source not in ('contacts', 'whatsapp') for source in sources)):
        raise ValueError('Invalid local snapshot scope')
    now = datetime.now(timezone.utc)
    payload = {'generated_at': now.isoformat(), 'window_hours': hours, 'source_status': {}}
    if 'contacts' in sources and 'contacts' in world:
        failed = world.get('contacts_error')
        rows = [] if failed else world['contacts']
        payload.update(contacts=rows, contacts_total=len(rows))
        payload['source_status']['contacts'] = 'degraded' if failed else 'ok'
    if 'whatsapp' in sources and 'whatsappMessages' in world:
        upper = now.timestamp()
        rows = [row for row, stamp in messages.values() if upper - hours * 3600 <= stamp <= upper]
        payload.update(whatsapp=rows, deferred_unread_whatsapp=[])
        payload['source_status']['whatsapp'] = 'ok'
    if set(payload['source_status']) != set(sources):
        raise ValueError('Local snapshot source is missing from world')
    return payload


def mac_read(request, world, messages):
    method, params = request.get('method'), request.get('params', {})
    name, args = params.get('name'), params.get('arguments', {})
    if method == 'tools/list':
        tools = []
        if 'contacts' in world:
            tools.append({'name': 'get_contacts', 'description': 'Address book contacts.',
                          'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}})
        if 'whatsappMessages' in world:
            tools.append({'name': 'read_history', 'description': 'Read a bounded historical WhatsApp page.',
                          'inputSchema': {'type': 'object', 'required': ['source', 'since', 'until'],
                              'additionalProperties': False, 'properties': {'source': {'type': 'string', 'enum': ['whatsapp']},
                              **{key: {'type': 'integer'} for key in ('since', 'until', 'before', 'limit')}}}})
        return {'tools': tools}, None
    if method == 'tools/call' and name == 'read_local' and isinstance(args, dict):
        try:
            payload = local_snapshot(world, messages, args)
        except ValueError:
            pass
        else:
            return {'structuredContent': payload, 'content': [{'type': 'text', 'text': json.dumps(payload)}]}, {
                'source': 'bridge_snapshot', 'sources': list(payload['source_status'])}
    if method == 'tools/call' and name == 'get_contacts' and 'contacts' in world and not args:
        payload = ({'contacts': [], 'error': 'PRIVATE-SYNTHETIC-READ-ERROR'} if world.get('contacts_error')
                   else {'contacts': world['contacts']})
        return {'content': [{'type': 'text', 'text': json.dumps(payload)}]}, {
            'source': 'contacts', 'rows': len(payload['contacts'])}
    if (method == 'tools/call' and 'whatsappMessages' in world and isinstance(args, dict)
            and (name == 'read_history' or name == 'get_messages'
                 and 'whatsapp_chat_read_v1' in params.get('_meta', {}).get('sotto/accepts', []))):
        try:
            payload, rows = whatsapp_read(messages, name, args)
        except ValueError:
            pass
        else:
            return {'structuredContent': payload, 'content': [{'type': 'text', 'text': json.dumps(payload)}]}, {
                'source': 'whatsapp', 'operation': name, 'request': args,
                'message_ids': [row['source_id'] for row in rows],
                'jids': sorted({row['contact_jid'] for row in rows}), 'rows': len(rows)}
    return {'isError': True, 'content': [{'type': 'text', 'text': 'Bridge fixture has no such source read.'}]}, {
        'source': 'denied', 'reason': 'unexpected Bridge fixture call'}


if 'contacts' in world or 'whatsappMessages' in world:
    messages = frozen_whatsapp(world)

    def mac_bridge():
        # The real relay owns request authorization and rechecks current consent at response time.
        while True:
            request = receiver.RELAY.poll(timeout=1)
            if request is None:
                continue
            result, receipt = mac_read(request, world, messages)
            accepted = receiver.RELAY.respond({'jsonrpc': '2.0', 'id': request['id'], 'result': result})
            if not accepted:
                receiver.RELAY.cancel(request, 'Bridge fixture result was withheld.')
            if receipt is not None and (accepted or receipt['source'] == 'denied'):
                with Path(sys.argv[1]).with_name('source-reads.jsonl').open('a') as log:
                    log.write(json.dumps(receipt) + '\n')

    threading.Thread(target=mac_bridge, daemon=True).start()
# Explicit synthetic Mac sender; never connects to a real Bridge or Messages.
if world.get('bridgeSend') is True:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'test'))
    import owner_bridge_send_fixture
    owner_bridge_send_fixture.configure(receiver)

server = ThreadingHTTPServer(('127.0.0.1', 0), receiver.Handler)
if world.get('meetingPrep'):
    os.environ['SOTTO_TRIGGER_PORT'] = str(server.server_port)
os.environ['PORT'] = str(server.server_port)
threading.Thread(target=server.serve_forever, daemon=True).start()
if 'contacts' in world or 'whatsappMessages' in world:
    source_context = receiver._source_context()
    snapshot_sources = [source for source, field in (('contacts', 'contacts'), ('whatsapp', 'whatsappMessages'))
                        if field in world and source in saved_grants and source_context.allowed(source)]
    if snapshot_sources:
        # Publish the fixture worker's first poll before making a startup read.
        receiver.RELAY.poll(timeout=0)
        source_context.bridge_call('read_local', {'since_hours': LOCAL_SNAPSHOT_HOURS, 'sources': snapshot_sources}, timeout=5)
print(json.dumps({'event': 'ready', 'port': server.server_port, 'identity': identity,
                  'bearer': capability, 'readBearer': receiver.MCP_TOKEN}), flush=True)
staged_offer = None
staged_offer_effects = []
for line in sys.stdin:
    request = json.loads(line)
    if request['op'] == 'stop':
        break
    if request['op'] == 'configure':
        config = request['config']
        if world.get('meetingPrep') or world.get('commitmentJourney', {}).get('actualModel') is True:
            os.environ['SOTTO_EVAL_NATIVE_PREP_RELAY'] = json.dumps(request['nativePrepRelay'])
        os.environ['SOTTO_CHAT_RUNTIME_URL'] = f"http://127.0.0.1:{config['port']}"
        os.environ['SOTTO_CHAT_RUNTIME_TOKEN'] = config['bearerToken']
        expected = data / 'runtime/expected-channel.json'
        expected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        expected.write_text(json.dumps(startup_config.receiver_channel_expectation(config)))
        expected.chmod(0o600)
        print(json.dumps({'event': 'configured'}), flush=True)
    elif request['op'] == 'deliver_gallery':
        from accepted_gallery import deliver_brief
        proof = deliver_brief(receiver, data, request['step'], request['text'])
        print(json.dumps({'event': 'deliver_gallery', **proof}), flush=True)
    elif request['op'] == 'prepare_prep_offer':
        # Synthetic calendar/source grants already pass the ordinary production source gate.
        # Do not seed pending-offer files or activate an unaccepted assistant question.
        import brief_runner
        events = [row for row in world['calendarEvents'] if row['id'] == request['eventId']]
        if len(events) != 1 or 'calendar' not in authority()['google_sources']:
            raise ValueError('Offer requires one selected calendar event and current source grant')
        event = events[0]
        if request.get('composeBrief'):
            from brief_prep import compose_offer
            artifact = compose_offer(world, PACK, data, event['id'], request.get('ownerText'))
            question = artifact['_prep_offer']['question']
        else:
            start = event['start']['dateTime']
            if sum(row.get('displayName') == request['person'] and not row.get('self')
                   for row in event.get('attendees', [])) != 1:
                raise ValueError('Offer person is not a calendar attendee')
            question = request['question']
            meeting = {'event_id': event['id'], 'start': start}
            artifact = {'brief_markdown': question, 'brief_text': question,
                        '_source_permissions': ['calendar'],
                        '_calendar_eligibility': [{'kind': 'eligibility', 'source': 'calendar',
                            'calendar_event_id': event['id'], 'calendar_start': start}],
                        '_prep_offer': {'person': request['person'], 'question': question,
                                        'meeting': meeting, 'ambiguous': False}}
        offer_run = hashlib.sha256(('eval-prep-offer:' + str(request['step']) + ':' + event['id']).encode()).hexdigest()
        previous = os.environ.get('SOTTO_DELIVERY_RUN_ID')
        os.environ['SOTTO_DELIVERY_RUN_ID'] = offer_run
        try:
            brief_runner._stage_prep_offer(artifact)
        finally:
            if previous is None:
                os.environ.pop('SOTTO_DELIVERY_RUN_ID', None)
            else:
                os.environ['SOTTO_DELIVERY_RUN_ID'] = previous
        staged_offer = artifact.get('_prep_offer_record')
        if not staged_offer:
            raise ValueError('Production prep offer staging refused fixture')
        staged_offer_effects = json.loads((data / 'events' / ('delivery-effects-' + offer_run + '.json')).read_text())['effects']
        print(json.dumps({'event': 'prepare_prep_offer', 'text': artifact['brief_text'],
                          'offer': staged_offer, 'source': 'calendar', 'fixture_mode': 'actual-model-brief' if request.get('composeBrief') else 'seeded-production-offer'}), flush=True)
    elif request['op'] == 'accept_prep_offer':
        import delivery_effects
        import pending_offer
        if not staged_offer or not request['messageId'].startswith('eval-out-'):
            raise ValueError('Missing staged offer or captured message receipt')
        receipt = {'message_id': request['messageId'], 'target': 'photon',
                   'chat_id': guid, 'accepted_at': request['acceptedAt']}
        if not delivery_effects.finalize(staged_offer_effects, receipt):
            raise ValueError('Production offer acceptance failed')
        accepted = pending_offer.get_offer(message_id=request['messageId'], latest=False,
                                          target='photon', chat_id=guid)
        if accepted.get('offer_id') != staged_offer['offer_id'] or not pending_offer._accepted_at(accepted):
            raise ValueError('Accepted offer proof unavailable')
        print(json.dumps({'event': 'accept_prep_offer', 'offer': accepted, 'receipt': receipt}), flush=True)
    elif request['op'] == 'prep_offer_proof':
        # Read only real accepted offer activation; never stage or finalize a fixture offer.
        offers = receiver._pending_offers()
        proofs = []
        for message_id in request['messageIds']:
            offer = offers.get_offer(message_id=message_id, latest=False, target='photon', chat_id=guid)
            if offer and offers._accepted_at(offer):
                proofs.append({'offer': offer, 'receipt': {key: offer.get(key) for key in
                    ('message_id', 'target', 'chat_id', 'accepted_at')}})
        print(json.dumps({'event': 'prep_offer_proof', 'proofs': proofs}), flush=True)
    elif request['op'] == 'tasks':
        import runtime_owner_loop
        writer=runtime_owner_loop._load_writer()
        continuity=writer._load_continuity()
        current=writer._owner_close_rows(continuity)
        records=[{'anchor_key':r['anchor_key'], 'title':r.get('summary',''), 'status':r.get('status','open'),
                  'path':str((Path(continuity._dir()) / f"{continuity._safe(r['anchor_key'])}.md").relative_to(data))}
                 for r in current]
        print(json.dumps({'event':'tasks','states':{r['anchor_key']:r.get('status','open') for r in current},'records':records}),flush=True)
    elif request['op'] == 'canonical':
        # Read-only questions must preserve the actual canonical graph and obligations.
        files = {str(path.relative_to(data)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted((data / 'knowledge').rglob('*.md'))}
        print(json.dumps({'event': 'canonical', 'files': files}), flush=True)
    elif request['op'] == 'journey':
        # Named fixture entrypoints drive existing production paths. They do not supply
        # completed ledger/knowledge snapshots or replace a receiver/tool implementation.
        if request['journey'] == 'learning':
            sys.path.insert(0, str(Path(__file__).parent))
            import learning_journey
            receipt = learning_journey.run(request['stage'], world, data)
        elif request['journey'] == 'commitment':
            sys.path[:0] = [str(PACK / 'tests'), str(PACK / '_shared/lib'),
                            str(PACK / '_shared/scripts'), str(PACK / 'morning-brief/scripts'),
                            str(PACK / 'proactive/scripts')]
            import commitment_journey_fixture
            receipt = commitment_journey_fixture.run_chronological_stage(data, request['stage'], request.get('scenario', 'email-to-text'),
                actual_model=world.get('commitmentJourney', {}).get('actualModel') is True)
            if receipt['actual_model']:
                for message in receipt['evidence']['emails']:
                    if message['id'] in (commitment_journey_fixture.PROMISE['id'], 'journey-new-pricing-request'):
                        world['gmailMessages'][message['id']] = commitment_journey_fixture.native_promise(
                            None if message['id'] == commitment_journey_fixture.PROMISE['id'] else message)
                Path(sys.argv[1]).write_text(json.dumps(world))
        else:
            raise ValueError('Unknown chronological journey')
        print(json.dumps({'event': 'journey', 'journey': request['journey'],
                          'stage': request['stage'], 'receipt': receipt}), flush=True)
    elif request['op'] == 'work':
        job = receiver.WORK_QUEUE.claim(str(data), 'conversation-eval')
        executed = None
        if job:
            if world.get('meetingPrep'):
                queued = json.loads(job['payload'].get('prompt', '{}'))
                executed = {'id': job['id'], 'kind': queued.get('kind'), 'selection': queued.get('selection'),
                            'offerMeeting': queued.get('offer', {}).get('meeting'),
                            'offerId': queued.get('offer', {}).get('offer_id')}
            receiver._work_one(job)
        receiver.OUTBOX.drain()
        with receiver.WORK_QUEUE._db(str(data)) as db:
            jobs = [dict(row) for row in db.execute('SELECT id,status,error FROM jobs')]
        deliveries = []
        if world.get('meetingPrep'):
            from accepted_gallery import project_gallery
            outbox = Path(receiver.OUTBOX.path())
            rows = json.loads(outbox.read_text()).get('rows', []) if outbox.exists() else []
            deliveries = [{'id': row.get('id'), 'status': row.get('status'), 'acceptance': row.get('acceptance'),
                           'provider_id': (row.get('receipt') or {}).get('message_id'),
                           'gallery': project_gallery(data, rows, row, authority()['revision'])} for row in rows]
        print(json.dumps({'event': 'work', 'claimed': bool(job), 'executed': executed,
                          'jobs': jobs, 'deliveries': deliveries}), flush=True)
server.shutdown()
