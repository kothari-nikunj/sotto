"""Offline HTTP fixture for the private owner-command receiver route."""
import json
import os
import pathlib
import sys
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

os.environ['SOTTO_TEST_MEETING_PREP'] = '1'
receiver_dir = pathlib.Path(__file__).resolve().parents[2] / 'trigger-receiver'
sys.path.insert(0, str(receiver_dir))
import receiver  # noqa: E402

# Synthetic source consent for isolated fresh-routine tool integration only.
if os.environ.get('SOTTO_TEST_ROUTINE_SOURCE_REVISION'):
    receiver._runtime_source_authority = lambda: {
        'version': 2, 'complete': True,
        'revision': os.environ['SOTTO_TEST_ROUTINE_SOURCE_REVISION'],
        'bridge_sources': [], 'google_sources': []}

data_root = pathlib.Path(os.environ['SOTTO_DATA'])
receiver.DATA = str(data_root)
if os.environ.get('SOTTO_TEST_TELEGRAM_ROUTINE') == '1':
    from test_telegram_channel_proof import _fixture
    from test_telegram_owner_commands import IDENTITY as TELEGRAM_IDENTITY
    import tempfile
    identity = TELEGRAM_IDENTITY
    fixture_directory = tempfile.TemporaryDirectory() if data_root.exists() else None
    fixture_parent = pathlib.Path(fixture_directory.name) if fixture_directory else data_root.parent
    _, _, _, client, _, policy = _fixture(fixture_parent)
    policy['SOTTO_DATA'] = str(data_root)
    for key, value in policy.items():
        os.environ[key] = value
    os.environ['GOOGLE_AI_API_KEY'] = 'fixture-key'
    receiver.DATA = str(data_root)
    receiver._runtime_adapter = lambda: SimpleNamespace(_client=lambda **kwargs: client)
else:
    owner = os.environ['PHOTON_HOME_CHANNEL']
    line = os.environ['SOTTO_IMESSAGE_NUMBER']
    guid = f'iMessage;-;{owner}'
    assert receiver._sotto_adapter('photon_owner').record_owner_activation(
        SimpleNamespace(chat_type='dm', user_id=owner, chat_id=guid))
    fingerprint = receiver._sotto_adapter('photon_owner').project_fingerprint(os.environ['PHOTON_PROJECT_ID'])
    identity = dict(tenant_id=os.environ['SOTTO_TENANT_ID'], channel='photon',
                    account_id=os.environ['SOTTO_ACCOUNT_ID'], owner_id=owner,
                    line_key=os.environ['SOTTO_LINE_KEY'], target='photon',
                    route_mode='fixed', route_id=f'imessage:{guid}~{line}',
                    project_fingerprint=fingerprint)
capability = receiver.OWNER_LOOP_CAPABILITY.derive(os.environ['SOTTO_MCP_TOKEN'], identity)
if os.environ.get('SOTTO_TEST_GMAIL_DRAFT') == '1':
    import owner_gmail_draft_fixture
    owner_gmail_draft_fixture.configure(receiver)

# Isolated requested-brief admission uses real HTTP/queue; no model worker starts here.
if os.environ.get('SOTTO_TEST_BRIEF_REQUEST') == '1' or os.environ.get('SOTTO_TEST_MEETING_PREP') == '1':
    pack = pathlib.Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff'
    receiver._find_sotto_script = lambda *parts: str(pack.joinpath(*parts))
    import onboarding
    onboarding.scheduled_hold = lambda *_: False
    # Local test tree substitutes immutable image ownership only; validate_work remains real.
    profile = receiver._runtime_adapter()._fixed_five_module()
    profile._PACKAGED_ROOT = pack
    profile._PACKAGED_RECEIVER = receiver_dir
    profile._image_owned = lambda meta: not meta.st_mode & 0o022
    os.environ['SOTTO_SKILLS_ROOT'] = str(pack)
    os.environ['SOTTO_HOME'] = str(data_root / 'sotto-home')

meeting_offer_message = None
if os.environ.get('SOTTO_TEST_MEETING_PREP') == '1':
    import hashlib
    import hmac
    import io
    from datetime import datetime, timedelta, timezone
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
    from adapters.sotto import client as channel_client, startup_config
    token = 'synthetic-connected-meeting-runtime-bearer'
    config = {'version': 1, 'port': 48317, 'bearerToken': token, 'channel': 'photon',
              'tenantId': identity['tenant_id'],
              'owner': {'accountId': identity['account_id'], 'ownerId': identity['owner_id']},
              'lineKey': identity['line_key'], 'target': 'photon',
              'providerThreadId': identity['route_id'],
              'credentials': {'mode': 'cloud', 'photonProjectId': os.environ['PHOTON_PROJECT_ID'],
                              'photonProjectSecret': os.environ['PHOTON_PROJECT_SECRET']}}
    expected = data_root / 'runtime/expected-channel.json'
    expected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    expected.parent.chmod(0o700)
    expected.write_text(json.dumps(startup_config.receiver_channel_expectation(config)))
    expected.chmod(0o600)
    current, fields = channel_client._expected_configured_channel(config)
    wire = {'version': 1, 'configured': {'version': 1, 'identity': current,
        'credential_proof': hmac.new(token.encode(), channel_client._identity_transcript(fields),
                                    hashlib.sha256).hexdigest()}, 'activation': {
        'status': 'acknowledged', 'mode': 'fixed', 'binding': {
            'tenant_id': identity['tenant_id'], 'project_fingerprint': identity['project_fingerprint'],
            'owner_id': identity['owner_id'], 'account_id': identity['account_id'],
            'line_key': identity['line_key'], 'provider_thread_id': identity['route_id']},
        'event_id': 'f'*64, 'admitted_at': 1780000000000, 'acknowledged_at': 1780000000001}}
    class FixtureOpener:
        def open(self, request, timeout):
            return io.BytesIO(json.dumps(wire).encode())
    os.environ['SOTTO_CHAT_RUNTIME_URL'] = 'http://127.0.0.1:48317'
    os.environ['SOTTO_CHAT_RUNTIME_TOKEN'] = token
    receiver._runtime_adapter()._client = lambda **kwargs: channel_client.ChatClient(
        os.environ['SOTTO_CHAT_RUNTIME_URL'], token, opener=FixtureOpener())
    receiver._runtime_source_authority = lambda: {'version': 2, 'complete': True,
        'revision': 'a'*64, 'bridge_sources': [], 'google_sources': ['calendar']}
    import managed
    scopes = ['https://www.googleapis.com/auth/calendar.readonly']
    if os.environ.get('SOTTO_TEST_OFFER_KIND', 'chase') != 'meeting_prep':
        scopes.append('https://www.googleapis.com/auth/gmail.readonly')
    if os.environ.get('SOTTO_TEST_GMAIL_OFFER') == '1':
        scopes.append('https://www.googleapis.com/auth/gmail.compose')
    managed.record_google_consent(data_root, scopes)
    import runtime_channel_proof
    runtime_channel_proof.check_current_channel(
        chat_client=receiver._runtime_adapter()._client(timeout=5), expected_channel_path=expected,
        receiver_data_root=data_root, receiver_policy=os.environ,
        configured_zone_name=receiver._configured_tz_name, expected_zone=receiver._configured_tz_name())
    receiver._shared_effects()
    import pending_offer
    import delivery_effects
    meeting_offer_message = 'connected-prep-offer-message'
    # Restart must retain the existing question; a new question is never silently seeded.
    if not (data_root / 'proactive/pending_offer.json').exists():
        os.environ['SOTTO_DELIVERY_RUN_ID'] = 'e'*32
        offer = pending_offer.set_offer('meeting_prep', 'Pull full prep for Dana?', 'Dana')
        delivery_effects.finalize([{'kind': 'pending_offer', 'offer': offer}, {
            'kind': 'meeting_prep_delivered', 'mode': 'offer', 'calendar_event_id': 'connected-event',
            'calendar_start': (datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()}], {
            'message_id': meeting_offer_message, 'target': 'photon', 'chat_id': guid,
            'accepted_at': datetime.now(timezone.utc).timestamp()})


# Explicit synthetic provider inputs; actual notification producer, finalize and source writer.
if os.environ.get('SOTTO_TEST_OFFER_KIND', 'chase') != 'meeting_prep':
    import yaml
    import compose_notification as notification
    import personal_context
    directory = data_root / 'knowledge/continuity'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / 'connected-source-loop.md'
    if not path.exists():
        row = {'anchor_key':'source:connected-cedar','status':'open','summary':'Cedar packet review',
            'source':'gmail','channel':'email','source_thread_id':'connected-source-thread',
            'source_message_id':'connected-source-message','source_refs':[{'sourceType':'gmail','sourceId':'connected-source-message'}],
            'contact_name':'Dana','contact_identifier':'dana@outside.com','action_type':'waiting_on',
            'created_at':'2026-10-04', 'chase_pending':datetime.now(timezone.utc).date().isoformat()}
        path.write_text('---\n'+yaml.safe_dump(row)+'---\n')
        path.chmod(0o600)
        notification._calendar = lambda: {}
        notification._read_json_script = lambda *args, **kwargs: {}
        notification.style_apply.apply = lambda *_: {}
        personal_context.conversation_snapshot = lambda: []
        notification.gemini.provider_key = lambda *_: 'synthetic-fixture-key'
        def fixture_model(*args, **kwargs):
            items = json.loads(args[3])
            return json.dumps({'items':[{'id':items[0]['id'],'text':'Dana has not returned the Cedar review.',
                'draft':'Any word on the Cedar review?','decline':''}]})
        os.environ['SOTTO_DELIVERY_RUN_ID'] = 'd'*32
        import importlib.util
        scan_spec = importlib.util.spec_from_file_location('connected_source_offer_scan', pack/'proactive/scripts/proactive_scan.py')
        scanner = importlib.util.module_from_spec(scan_spec); scan_spec.loader.exec_module(scanner)
        now = datetime.now(timezone.utc)
        selected = scanner.scan([], [], {}, '', now, chase_candidates=scanner._chase_candidates(now.date().isoformat()))
        notification.compose('proactive', selected['nudges'], llm=fixture_model)
        effects = json.loads((data_root/'events'/('delivery-effects-'+'d'*32+'.json')).read_text())['effects']
        assert any(effect.get('offer',{}).get('loop_snapshot') for effect in effects)
        delivery_effects.finalize(effects, {'message_id':'connected-source-offer-message','target':'photon',
            'chat_id':guid,'accepted_at':datetime.now(timezone.utc).timestamp()})
    receiver._runtime_source_authority = lambda: {'version': 2, 'complete': True,
        'revision': 'a'*64, 'bridge_sources': [], 'google_sources': ['calendar', 'gmail']}
    meeting_offer_message = 'connected-source-offer-message'

# Synthetic Gmail HTTP/service boundary; producer, reviewed MIME and durable ledger are real.
if os.environ.get('SOTTO_TEST_GMAIL_OFFER') == '1':
    from types import SimpleNamespace
    tool = receiver.RUNTIME_OWNER_GMAIL_DRAFT_TOOL
    google = tool._google()
    class OfferGmail:
        def users(self): return self
        def messages(self): return self
        def drafts(self): return self
        def get(self, **kwargs):
            assert kwargs == {'userId':'me','id':'connected-source-message','format':'full'}
            return SimpleNamespace(execute=lambda: {'id':'connected-source-message','threadId':'connected-source-thread',
                'payload':{'headers':[{'name':'From','value':'Dana <dana@outside.com>'},
                    {'name':'Subject','value':'Cedar review'}, {'name':'Message-ID','value':'<cedar@outside.com>'}]}})
        def getProfile(self, **kwargs):  # noqa: N802 -- Google API method.
            assert kwargs == {'userId':'me'}
            return SimpleNamespace(execute=lambda: {'emailAddress':'owner@example.com'})
        def create(self, **kwargs):
            def execute():
                with (data_root/'provider-calls.jsonl').open('a') as stream:
                    stream.write(json.dumps({'operation':'drafts.create','args':kwargs})+'\n')
                if (data_root/'provider_unknown').exists():
                    raise TimeoutError('synthetic acceptance response lost')
                return {'id':'draft-123','message':{'threadId':'connected-source-thread'}}
            return SimpleNamespace(execute=execute)
        def send(self, **kwargs):
            raise AssertionError('email sends forbidden')
    google._gmail_service = OfferGmail
    google.capabilities = lambda: {'gmail_read':True,'gmail_draft':not (data_root/'draft_revoked').exists()}
    tool._google = lambda: google

# Producer-created standing offer; only synthetic transcript extraction input is supplied.
if os.environ.get('SOTTO_TEST_PROCEDURE_OFFER') == '1':
    import compose_brief
    import master_file
    import pending_offer
    import delivery_effects
    meeting_offer_message = 'connected-procedure-offer-message'
    if not (data_root/'procedure-offer-seeded').exists():
        os.environ['SOTTO_DELIVERY_RUN_ID'] = 'f'*32
        rule = compose_brief._pick_procedural_candidate({'procedural_candidates':[
            {'rule':'Never book me Fridays.\nUse mornings instead.'}]})
        assert rule
        compose_brief._append_procedure_offer({'brief_markdown':'## Evening\nToday.'}, {'_procedure_offer':rule})
        assert master_file.read() == ''
        effects = json.loads((data_root/'events'/('delivery-effects-'+'f'*32+'.json')).read_text())['effects']
        delivery_effects.finalize(effects, {'message_id':meeting_offer_message,'target':'photon',
            'chat_id':guid,'accepted_at':datetime.now(timezone.utc).timestamp()})
        (data_root/'procedure-offer-seeded').write_text('seeded')

server = ThreadingHTTPServer(('127.0.0.1', 0), receiver.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
print(json.dumps({'port': server.server_port, 'identity': identity,
                  'bearer': capability, 'readBearer': receiver.MCP_TOKEN,
                  'offerMessageId': meeting_offer_message, 'providerThreadId': identity['route_id']}), flush=True)
for command in sys.stdin:
    if command == 'master-context\n':
        import compose_brief
        print(json.dumps({'master_context': compose_brief._format_master_context()}), flush=True)
    if command == 'quit\n':
        break
    if command.startswith('timezone='):
        os.environ['SOTTO_TIMEZONE'] = command.removeprefix('timezone=').strip()
        print(json.dumps({'timezone': os.environ['SOTTO_TIMEZONE']}), flush=True)
    if command == 'seed-close-targets\n':
        import yaml
        from ledger_provenance import digest_frontmatter
        directory = data_root / 'knowledge' / 'continuity'
        directory.mkdir(parents=True, exist_ok=True)
        for name, row in (
            ('legacy', {'anchor_key': 'legacy-open', 'action_type': 'follow_up', 'channel': 'manual',
                        'status': 'open', 'source': 'user_added', 'source_thread_id': 'manual:old',
                        'summary': 'Legacy secret', 'created_at': '2026-09-10'}),
            ('source', {'anchor_key': 'gmail-open', 'action_type': 'reply', 'channel': 'email',
                        'status': 'open', 'source': 'gmail', 'source_thread_id': 'gmail-thread',
                        'source_refs': [{'sourceType': 'gmail', 'sourceId': 'msg-1'}],
                        'summary': 'Source secret', 'created_at': '2026-09-10'}),
        ):
            if name == 'source':
                row['content_provenance'] = {'version': 1, 'kind': 'sources', 'sources': ['gmail'],
                                             'digest': digest_frontmatter(row)}
            path = directory / f'{name}.md'
            path.write_text('---\n' + yaml.safe_dump(row, sort_keys=False) + '---\n')
            path.chmod(0o600)
        print(json.dumps({'seeded': True}), flush=True)
    if command.startswith('change-owner-row='):
        import yaml
        from ledger_provenance import digest_frontmatter
        anchor = command.removeprefix('change-owner-row=').strip()
        changed = 0
        for path in (data_root / 'knowledge' / 'continuity').glob('*.md'):
            row = receiver._owner_continuity().ledger_io.parse_frontmatter(path.read_text())
            if row and row.get('anchor_key') == anchor:
                row['summary'] = 'Owner changed this loop while close was prepared'
                row['content_provenance']['digest'] = digest_frontmatter(row)
                path.write_text('---\n' + yaml.safe_dump(row, sort_keys=False) + '---\n')
                path.chmod(0o600)
                changed += 1
        print(json.dumps({'changed': changed}), flush=True)
server.shutdown()
server.server_close()
