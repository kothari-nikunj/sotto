"""Offline HTTP fixture for the private owner-command receiver route."""
import json
import os
import pathlib
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

if os.environ.get('SOTTO_TEST_BRIDGE_SEND') == '1' or os.environ.get('SOTTO_TEST_DOCSEND') == '1' or os.environ.get('SOTTO_TEST_CONTACTS') == '1' or os.environ.get('SOTTO_TEST_CALENDAR_MOVE') == '1' or os.environ.get('SOTTO_TEST_DIRECT_PROCEDURE') == '1' or os.environ.get('SOTTO_TEST_USEFULNESS') == '1':
    os.environ['SOTTO_TEST_MEETING_PREP'] = '1'

if os.environ.get('SOTTO_TEST_RELATIONSHIP_PULSE') == '1' or os.environ.get('SOTTO_TEST_PROACTIVE') == '1' or os.environ.get('SOTTO_TEST_ASK_COMPANY') == '1':
    os.environ['SOTTO_TEST_MEETING_PREP'] = '1'

if os.environ.get('SOTTO_TEST_FOLLOWUP') == '1':
    os.environ['SOTTO_TEST_MEETING_PREP'] = '1'
    os.environ['SOTTO_TEST_GRANOLA_HISTORY'] = '1'
    os.environ['SOTTO_TEST_GMAIL_DRAFT'] = '1'

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
    fixture_data, _, _, client, _, policy = _fixture(fixture_parent)
    # Retain protected pairing across process restarts; never overwrite a relink.
    if fixture_data != data_root:
        import shutil
        for relative in ('telegram-link.json', 'runtime/expected-channel.json'):
            destination = data_root / relative
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(fixture_data / relative, destination)
    policy['SOTTO_DATA'] = str(data_root)
    for key, value in policy.items():
        os.environ[key] = value
    os.environ['GOOGLE_AI_API_KEY'] = 'fixture-key'
    receiver.DATA = str(data_root)
    receiver._runtime_adapter()._client = lambda **kwargs: client
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
    if os.environ.get('SOTTO_TEST_NUMBERED_SHARED') == '1':
        identity.update(route_mode='first_contact_shared_project', route_id='shared_project')
        import runtime_channel_proof
        startup = runtime_channel_proof._startup_module()
        config = {'version': 1, 'tenantId': identity['tenant_id'], 'channel': 'photon',
                  'owner': {'accountId': identity['account_id'], 'ownerId': owner},
                  'lineKey': identity['line_key'], 'target': 'photon',
                  'port': int(os.environ['SOTTO_TEST_NODE_PORT']),
                  'firstContact': {'mode': 'owner_dm_shared_project'},
                  'credentials': {'mode': 'cloud', 'photonProjectId': os.environ['PHOTON_PROJECT_ID'],
                                  'photonProjectSecret': os.environ['PHOTON_PROJECT_SECRET']}}
        runtime = data_root / 'runtime'
        runtime.mkdir(mode=0o700, exist_ok=True)
        expectation = runtime / 'expected-channel.json'
        expectation.write_text(json.dumps(startup.receiver_channel_expectation(config)))
        expectation.chmod(0o600)
capability = receiver.OWNER_LOOP_CAPABILITY.derive(os.environ['SOTTO_MCP_TOKEN'], identity)
if os.environ.get('SOTTO_TEST_GMAIL_DRAFT') == '1':
    import owner_gmail_draft_fixture
    owner_gmail_draft_fixture.configure(receiver)

if os.environ.get('SOTTO_TEST_GMAIL_SEND') == '1':
    import owner_gmail_send_fixture
    owner_gmail_send_fixture.configure(receiver)

if os.environ.get('SOTTO_TEST_BRIEF_REQUEST') == '1' or os.environ.get('SOTTO_TEST_MEETING_PREP') == '1':
    pack = pathlib.Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff'
    receiver._find_sotto_script = lambda *parts: str(pack.joinpath(*parts))
    import onboarding
    onboarding.scheduled_hold = lambda *_: False
    profile = receiver._runtime_adapter()._fixed_five_module()
    profile._PACKAGED_ROOT = pack
    profile._PACKAGED_RECEIVER = receiver_dir
    profile._image_owned = lambda meta: not meta.st_mode & 0o022
    os.environ['SOTTO_SKILLS_ROOT'] = str(pack)
    os.environ['SOTTO_HOME'] = str(data_root / 'sotto-home')
    if os.environ.get('SOTTO_TEST_TELEGRAM_ROUTINE') == '1':
        receiver._fresh_selfhost_brief_hold = lambda: False
        receiver._runtime_source_authority = lambda: {
            'version': 2, 'complete': not (data_root / 'source-revoked').exists(),
            'revision': 'a' * 64, 'bridge_sources': [], 'google_sources': []}

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
            if (os.environ.get('SOTTO_TEST_FOLLOWUP') == '1' or os.environ.get('SOTTO_TEST_RELATIONSHIP_PULSE') == '1' or os.environ.get('SOTTO_TEST_PROACTIVE') == '1' or os.environ.get('SOTTO_TEST_ASK_COMPANY') == '1') and request.full_url.endswith('/v1/send'):
                body = json.loads(request.data)
                with (data_root/'synthetic-sends.jsonl').open('a') as log:
                    log.write(json.dumps(body)+'\n')
                return io.BytesIO(json.dumps({'acceptance':'accepted','message_id':'followup-output',
                    'message_ids':['followup-output'], 'route_proof_version': 1,
                    'route_digest': channel_proof['route_digest'],
                    'payload_hash': hashlib.sha256(json.dumps({'text': body['text']},
                        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}).encode())
            if request.full_url.endswith('/v1/activation-evidence'):
                return io.BytesIO(json.dumps({'version':1,'pending':None}).encode())
            return io.BytesIO(json.dumps(wire).encode())
    os.environ['SOTTO_CHAT_RUNTIME_URL'] = 'http://127.0.0.1:48317'
    os.environ['SOTTO_CHAT_RUNTIME_TOKEN'] = token
    receiver._runtime_adapter()._client = lambda *args, **kwargs: channel_client.ChatClient(
        os.environ['SOTTO_CHAT_RUNTIME_URL'], token, opener=FixtureOpener())
    receiver._runtime_source_authority = lambda: {'version': 2, 'complete': True,
        'revision': 'a'*64, 'bridge_sources': [], 'google_sources': ['calendar']}
    import managed
    managed.record_google_consent(data_root, ['https://www.googleapis.com/auth/calendar.readonly'])
    import runtime_channel_proof
    channel_proof = runtime_channel_proof.check_current_channel(
        chat_client=receiver._runtime_adapter()._client(timeout=5), expected_channel_path=expected,
        receiver_data_root=data_root, receiver_policy=os.environ,
        configured_zone_name=receiver._configured_tz_name, expected_zone=receiver._configured_tz_name())
    receiver._shared_effects()
    import pending_offer
    import delivery_effects
    meeting_offer_message = 'connected-prep-offer-message'
    if os.environ.get('SOTTO_TEST_PREP_OFFER_PROPOSAL') == '1':
        # Synthetic Calendar boundary for the real private HTTP proposal route.
        # No Google client, credentials, or provider API is involved.
        title = 'Cedar planning review'
        provider_event_id = 'fixture-event-123'
        calendar_id = 'calendar@example.com'
        start = os.environ['SOTTO_TEST_PREP_OFFER_START']
        event = {'id': provider_event_id, 'calendar_id': calendar_id, 'summary': title,
                 'status': 'confirmed', 'owner_participates': True,
                 'organizer': {'email': 'owner@example.com', 'self': True},
                 'start': {'dateTime': start}, 'attendees': [
                     {'email': 'owner@example.com', 'self': True},
                     {'email': 'ada@cedar.example', 'self': False}]}
        def read_calendar(operation, arguments):
            with (data_root / 'synthetic-calendar-reads.txt').open('a', encoding='utf-8') as log:
                log.write(operation + '\n')
            if (data_root / 'synthetic-calendar-unavailable').exists():
                raise RuntimeError('synthetic Calendar temporarily unavailable')
            if operation == 'calendar_list':
                return {'observation': {'complete': True}, 'items': [event]}
            if operation != 'calendar_get' or arguments.get('event_id') != provider_event_id:
                raise AssertionError('unexpected synthetic Calendar read')
            if arguments.get('calendar_id') not in ('primary', calendar_id):
                raise AssertionError('unexpected synthetic Calendar scope')
            current = {**event, 'status': 'cancelled'} if (data_root / 'synthetic-calendar-cancelled').exists() else event
            return {'observation': {'complete': True}, 'item': current}
        receiver._read_prep_offer_calendar = read_calendar
        os.environ['SOTTO_USER_EMAIL'] = 'owner@example.com'
        meeting_offer_message = None
    # Restart must retain the existing question; a new question is never silently seeded.
    elif not (data_root / 'proactive/pending_offer.json').exists():
        os.environ['SOTTO_DELIVERY_RUN_ID'] = 'e'*32
        age_seconds = int(os.environ.get('SOTTO_TEST_PREP_OFFER_AGE_SECONDS', '0'))
        ttl_minutes = int(os.environ.get('SOTTO_TEST_PREP_OFFER_TTL_MINUTES', '180'))
        assert 0 <= age_seconds <= 10800 and 1 <= ttl_minutes <= 180
        offered_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        clock = pending_offer._now
        pending_offer._now = lambda: offered_at
        try:
            offer = pending_offer.set_offer('meeting_prep', 'Pull full prep for Dana?', 'Dana', ttl_min=ttl_minutes)
            delivery_effects.finalize([{'kind': 'pending_offer', 'offer': offer}, {
                'kind': 'meeting_prep_delivered', 'mode': 'offer', 'calendar_event_id': 'connected-event',
                'calendar_start': (datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()}], {
                'message_id': meeting_offer_message, 'target': 'photon', 'chat_id': guid,
                'accepted_at': offered_at.timestamp()})
        finally:
            pending_offer._now = clock

if os.environ.get('SOTTO_TEST_KNOWLEDGE_READ') == '1':
    from datetime import datetime, timezone
    pack = pathlib.Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff'
    receiver._find_sotto_script = lambda *parts: str(pack.joinpath(*parts))
    receiver._runtime_source_authority = lambda: {'version': 2, 'complete': True,
        'revision': 'a'*64, 'bridge_sources': [], 'google_sources': []}
    sys.path.insert(0, str(pack / '_shared/knowledge'))
    import knowledge_update
    if not list((data_root / 'knowledge/people').glob('*.md')):
        knowledge_update.apply({'person_updates': [{'person_name': 'Dana Cedar',
            'identifier': 'dana@cedar.com', 'facts': [{'fact':
                'In 2020 Dana discussed pediatric clinic procurement with Cedar.',
                'source': 'gmail', 'source_ref': 'synthetic-old-topic-message', 'confidence': 0.95},
                *([{'fact': 'Dana enjoys hiking in Yosemite.', 'source': 'gmail',
                    'source_ref': 'synthetic-other-fact', 'confidence': 0.95}]
                  if os.environ.get('SOTTO_TEST_PERSON_CORRECTION') == '1' else [])]}]},
            now=datetime(2020, 1, 1, tzinfo=timezone.utc))

    if not list((data_root / 'knowledge/companies').glob('*.md')):
        knowledge_update.apply({'company_updates': [{'company_name': 'Cedar Labs',
            'domain': 'cedar.example', 'about': 'Builds clinic tools.',
            'context_updates': ['Saved owner meeting context.']}]},
            now=datetime(2020, 1, 1, tzinfo=timezone.utc))

if os.environ.get('SOTTO_TEST_RELATIONSHIP_EDIT') == '1':
    knowledge_update.apply({'person_updates': [{'person_name': 'Evan Birch',
        'identifier': 'evan@birch.com', 'facts': [{'fact': 'Evan enjoys cycling.',
        'source': 'gmail', 'source_ref': 'synthetic-evan', 'confidence': 0.95}]}]},
        now=datetime(2020, 1, 1, tzinfo=timezone.utc))

# Ordinary production-shaped source rows; no synthetic provenance stamp is added.
if os.environ.get('SOTTO_TEST_LOOPS_READ') == '1':
    import yaml
    pack = pathlib.Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff'
    receiver._find_sotto_script = lambda *parts: str(pack.joinpath(*parts))
    receiver._runtime_source_authority = lambda: {'version': 2, 'complete': True,
        'revision': 'a'*64, 'bridge_sources': [], 'google_sources': ['gmail']}
    directory = data_root / 'knowledge' / 'continuity'
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    for name, source, action, summary in (
        ('owe', 'gmail', 'reply', 'Send Dana the Cedar procurement review'),
        ('waiting', 'gmail', 'waiting_on', 'Waiting for Eli Cedar to approve procurement'),
        ('denied', 'imessage', 'reply', 'Denied private Mac obligation'),
        ('mixed', 'gmail', 'reply', 'Denied mixed-source obligation'),
        ('apple-mail', 'apple_mail', 'reply', 'Denied ambiguous Apple Mail obligation'),
    ):
        row = {'anchor_key': 'connected-'+name, 'status':'open', 'source':source,
            'channel':'email' if source=='gmail' else 'imessage', 'action_type':action,
            'contact_name':'Dana Cedar' if name=='owe' else 'Eli Cedar',
            'contact_identifier':'dana@cedar.com' if name=='owe' else 'eli@cedar.com',
            'source_thread_id':'connected-thread-'+name,'source_message_id':'connected-message-'+name,
            'source_refs':[{'sourceType':source,'sourceId':'connected-message-'+name}],
            'summary':summary,'created_at':'2026-10-01'}
        if name == 'mixed':
            row['source_refs'].append({'sourceType':'imessage','sourceId':'mixed-mac-message'})
        path = directory / (name+'.md')
        if not path.exists():
            path.write_text('---\n'+yaml.safe_dump(row,sort_keys=False)+'---\n')
            path.chmod(0o600)

if os.environ.get('SOTTO_TEST_LOOP_CORRECTION') == '1' or os.environ.get('SOTTO_TEST_LOOP_KEEP') == '1':
    pack = pathlib.Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff'
    receiver._find_sotto_script = lambda *parts: str(pack.joinpath(*parts))
    receiver._runtime_source_authority = lambda: {'version':2,'complete':True,'revision':'a'*64,
        'bridge_sources':[],'google_sources':['gmail'],'connector_sources':[]}
    receiver._shared_effects()
    import runtime_owner_loop
    correction_writer = runtime_owner_loop._load_writer()
    for key in ('source-loop','dismiss-loop'):
        if not any(row.get('anchor_key') == key for row in correction_writer._owner_close_rows(correction_writer._load_continuity())):
            correction_writer._load_continuity()._persist({'anchor_key':key,'action_type':'follow_up',
                'channel':'email','status':'open','created_at':'2026-10-05 10:00:00',
                'summary':'send the deck' if key=='source-loop' else 'review the packet',
                'ask':'send the deck','contact_name':'Dana','contact_identifier':'dana@example.com',
                'source':'gmail','source_message_id':'m1','source_thread_id':'thread1'})

if os.environ.get('SOTTO_TEST_LOOP_KEEP') == '1':
    marker = data_root / 'keep-fixture-seeded'
    if not marker.exists():
        import ledger_io
        for anchor, kind, status, summary in (
                ('keep-you', 'follow_up', 'parked', 'Send the Cedar deck'),
                ('keep-waiting', 'waiting_on', 'open', 'Await the Cedar notes')):
            row = {'anchor_key': anchor, 'action_type': kind, 'channel': 'email',
                   'status': status, 'created_at': '2026-10-01 10:00:00',
                   'summary': summary, 'ask': summary, 'contact_name': 'Dana',
                   'contact_identifier': 'dana@example.com', 'source': 'gmail',
                   'source_message_id': 'keep-message', 'source_thread_id': 'keep-thread'}
            if kind == 'waiting_on':
                row.update({field: 2 if field in ('chased_count', 'chase_stalls') else '2026-10-02'
                            for field in ledger_io.CHASE_STATE_FIELDS})
            correction_writer._load_continuity()._persist(row)
        marker.write_text('seeded')

if os.environ.get('SOTTO_TEST_LOOP_CLEANUP') == '1':
    pack = pathlib.Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff'
    receiver._find_sotto_script = lambda *parts: str(pack.joinpath(*parts))
    receiver._runtime_source_authority = lambda: {'version':2,'complete':True,'revision':'a'*64,
        'bridge_sources':[],'google_sources':['gmail'],'connector_sources':[]}
    receiver._shared_effects()
    import runtime_owner_loop
    import datetime
    cleanup_writer = runtime_owner_loop._load_writer()._load_continuity()
    marker = data_root / 'cleanup-fixture-seeded'
    if not marker.exists():
        created = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)).strftime('%Y-%m-%d 10:00:00')
        entries = (
                ('cleanup-deck', 'Send the Cedar deck', 'follow_up'),
                ('cleanup-notes', 'Review the Cedar notes', 'follow_up'),
                ('cleanup-reply', 'Await the Cedar reply', 'waiting_on'),
                ('cleanup-untouched', 'Review the unselected Cedar packet', 'follow_up'))
        if os.environ.get('SOTTO_TEST_NUMBERED_SELECTION') == '1':
            entries = [(f'numbered-{i:02d}', f'Synthetic numbered task {i:02d}', 'follow_up') for i in range(1, 46)]
        if os.environ.get('SOTTO_TEST_NUMBERED_MANUAL_SUBSET') == '1':
            entries = [(f'outside-{i:02d}', f'Synthetic outside task {i:02d}', 'follow_up') for i in range(1, 48)]
        for anchor, summary, kind in entries:
            cleanup_writer._persist({'anchor_key':anchor,'action_type':kind,'channel':'email',
                'status':'dismissed' if anchor == 'outside-47' else 'open','created_at':created,'summary':summary,'ask':summary,
                'contact_name':'Dana','contact_identifier':'dana@example.com','source':'gmail',
                'source_message_id':'cleanup-'+anchor,'source_thread_id':'cleanup-thread',
                **({'chased_count':2,'last_chased_at':'2026-10-01'} if kind=='waiting_on' else {})})
        marker.write_text('seeded')
    cleanup_load_writer = runtime_owner_loop._load_writer
    def cleanup_observed_writer():
        writer = cleanup_load_writer()
        load_continuity = writer._load_continuity
        def observed_continuity():
            continuity = load_continuity()
            persist = continuity._persist
            def observed_persist(row, **kwargs):
                with (data_root / 'cleanup-writes.jsonl').open('a') as log:
                    log.write(json.dumps({'anchor':row.get('anchor_key'),'operations':sorted(kwargs)})+'\n')
                    log.flush()
                    os.fsync(log.fileno())
                result = persist(row, **kwargs)
                if (os.environ.get('SOTTO_TEST_LOOP_CLEANUP_CRASH') == '1'
                        and row.get('anchor_key') == 'cleanup-deck' and kwargs.get('loop_dismiss')):
                    os._exit(86)  # Process loss after canonical write, before aggregate acknowledgement.
                return result
            continuity._persist = observed_persist
            return continuity
        writer._load_continuity = observed_continuity
        if os.environ.get('SOTTO_TEST_CLEANUP_WRITER_ERROR') == '1':
            # perform() calls this only after authority, owner span and proposal validation.
            admitted_cleanup = writer.op_admitted_loop_cleanup
            def fail_cleanup(args, proof, *, now, inspect, authority):
                if inspect:
                    return admitted_cleanup(args, proof, now=now, inspect=True, authority=authority)
                with (data_root / 'cleanup-failure-attempts.jsonl').open('a') as log:
                    log.write('synthetic writer entered after admission\n')
                raise OSError('SYNTHETIC-PRIVATE-WRITER-ERROR')
            writer.op_admitted_loop_cleanup = fail_cleanup
        return writer
    runtime_owner_loop._load_writer = cleanup_observed_writer

if os.environ.get('SOTTO_TEST_LOOP_RESOLVE_MANY') == '1':
    pack = pathlib.Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff'
    receiver._find_sotto_script = lambda *parts: str(pack.joinpath(*parts))
    receiver._runtime_source_authority = lambda: {'version':2,'complete':True,'revision':'a'*64,
        'bridge_sources':[],'google_sources':['gmail'],'connector_sources':[]}
    receiver._shared_effects()
    import runtime_owner_loop
    writer = runtime_owner_loop._load_writer()._load_continuity()
    if not (data_root / 'resolve-many-fixture-seeded').exists():
        for index, name in enumerate(('Tina Jiang','Shimolee Kothari','Hari Raghavan','Other Person'),1):
            writer._persist({'anchor_key':f'resolve-many-{index}','action_type':'follow_up',
                'channel':'email','status':'open','created_at':'2026-10-01 10:00:00',
                'summary': ('Discuss her product lead offer' if index==1 else 'Choose dates for family photos' if index==2 else f'Reply to {name}'),
                'ask': ('Message Tina about the offer check-in' if index==1 else 'Pick a date and location' if index==2 else f'Reply to {name}'),
                'contact_name':name,'contact_identifier':f'person{index}@example.com',
                **({'canonical_id':f'c_fixture{index}'} if os.environ.get('SOTTO_TEST_SAME_PERSON_TASKS')=='1' else {}),
                'source':'gmail','source_message_id':f'resolve-many-message-{index}',
                'source_thread_id':f'resolve-many-thread-{index}',
                **({'deadline':'Tonight', 'chase_pending':'tomorrow', 'last_chased_at':'2026-02-30',
                    'chase_after':'2026-1-2', 'handoff_asked_at':'last week'}
                   if index == 4 and os.environ.get('SOTTO_TEST_LOOP_LEGACY_DATES') == '1' else {})})
        if os.environ.get('SOTTO_TEST_SAME_PERSON_TASKS')=='1':
            writer._persist({'anchor_key':'resolve-many-extra','action_type':'follow_up',
                'channel':'email','status':'open','created_at':'2026-10-01 10:00:00',
                'summary':'Choose the catering menu','ask':'Confirm the catering menu',
                'contact_name':'Shimolee Kothari','contact_identifier':'person2@example.com',
                'canonical_id':'c_fixture2','source':'gmail','source_message_id':'extra-message',
                'source_thread_id':'extra-thread',
                **({'canonical_id':'c_distinct','contact_identifier':'other@example.com'}
                   if os.environ.get('SOTTO_TEST_TASK_PEER') in ('different-identity','same-ask','hidden-matching') else {}),
                **({'summary':'Choose family photo dates','ask':'Pick a date and location'}
                   if os.environ.get('SOTTO_TEST_TASK_PEER') in ('same-ask','hidden-matching') else {}),
                **({'snoozed_until':'2099-01-01'} if os.environ.get('SOTTO_TEST_TASK_PEER') == 'hidden-matching' else {})})
        (data_root / 'resolve-many-fixture-seeded').write_text('seeded')

if os.environ.get('SOTTO_TEST_CONDITIONAL_REMINDER') == '1':
    import managed
    pack = pathlib.Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff'
    receiver._find_sotto_script = lambda *parts: str(pack.joinpath(*parts))
    receiver._shared_effects()
    import runtime_owner_loop
    from source_context import allowed
    writer = runtime_owner_loop._load_writer()._load_continuity()
    marker = data_root / 'conditional-fixture-seeded'
    if not marker.exists():
        managed.record_google_consent(str(data_root), ['https://www.googleapis.com/auth/gmail.readonly'])
        for anchor in ('waiting-dana', 'waiting-resolved', 'waiting-held'):
            writer._persist({'anchor_key': anchor, 'action_type': 'waiting_on', 'channel': 'email',
                'status': 'open', 'created_at': '2026-10-05', 'summary': 'Send Cedar deck',
                'contact_name': 'Dana', 'contact_identifier': 'dana@example.com', 'source': 'gmail',
                'source_message_id': 'cedar-message', 'source_thread_id': 'cedar-thread',
                'source_refs': [{'sourceType': 'gmail', 'sourceId': 'cedar-message'}]})
        marker.write_text('seeded')
    receiver._runtime_source_authority = lambda: {'version': 2, 'complete': True,
        'revision': ('a' if allowed('gmail') else 'e') * 64, 'bridge_sources': [],
        'google_sources': ['gmail'] if allowed('gmail') else [], 'connector_sources': []}

# This opt-in uses the actual managed authority endpoint and protected generation.
if os.environ.get('SOTTO_TEST_GRANOLA_READ') == '1' or os.environ.get('SOTTO_TEST_GRANOLA_HISTORY') == '1':
    import yaml
    import managed
    pack = pathlib.Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff'
    receiver._find_sotto_script = lambda *parts: str(pack.joinpath(*parts))
    managed.record_connector_consent(data_root, 'granola', True)
    credentials = data_root / 'connectors'
    credentials.mkdir(parents=True, exist_ok=True)
    credentials.chmod(0o700)
    granola_token_path = credentials / 'granola.json'
    granola_token_path.write_text(json.dumps({'service':'granola','access_token':'synthetic-fixture',
                                'sotto_source_generation':'a'*32}))
    granola_token_path.chmod(0o600)
    if os.environ.get('SOTTO_TEST_GRANOLA_HISTORY') == '1':
        # Real isolated gather subprocess contacts this loopback TLS MCP peer only.
        import datetime
        import ipaddress
        import ssl
        from http.server import BaseHTTPRequestHandler
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic-loopback')])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
        cert_path, key_path = data_root/'synthetic-ca.pem', data_root/'synthetic-key.pem'
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key_path.chmod(0o600)
        os.environ['SSL_CERT_FILE'] = str(cert_path)
        class SyntheticGranola(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                assert self.path == '/mcp'
                assert self.headers['Authorization'] == 'Bearer synthetic-fixture'
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with (data_root/'synthetic-mcp-calls.jsonl').open('a') as log:
                    log.write(json.dumps({'method':request['method'], 'params':request.get('params')})+'\n')
                method = request['method']
                if method == 'notifications/initialized':
                    self.send_response(202); self.end_headers(); return
                if method == 'initialize':
                    result = {'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'synthetic','version':'1'}}
                elif method == 'tools/list':
                    result = {'tools':[{'name':name,'inputSchema':{'type':'object','properties':props}} for name,props in (
                        ('list_meetings',{}),('get_meetings',{'meeting_ids':{'type':'array','items':{'type':'string'}}}),
                        ('get_meeting_transcript',{'meeting_id':{'type':'string'}}))]}
                else:
                    assert method == 'tools/call'
                    name, args = request['params']['name'], request['params'].get('arguments',{})
                    if name == 'list_meetings':
                        if (data_root/'synthetic-mcp-block').exists():
                            (data_root/'synthetic-mcp-blocked').write_text('blocked')
                            time.sleep(2)
                            self.connection.settimeout(0.1)
                            try:
                                if self.connection.recv(1) == b'':
                                    (data_root/'synthetic-mcp-disconnected').write_text('disconnected')
                            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                                (data_root/'synthetic-mcp-disconnected').write_text('disconnected')
                            except TimeoutError:
                                pass
                        value = {'meetings':[{'id':mid,'title':'Cedar review','start_time':(now-datetime.timedelta(days=days)).isoformat(),
                            'end_time':(now-datetime.timedelta(days=days)+datetime.timedelta(hours=1)).isoformat()} for mid,days in ([('synthetic-meeting',1)] if os.environ.get('SOTTO_TEST_FOLLOWUP') == '1' else [('synthetic-meeting',1),('other-meeting',2)])]}
                        if os.environ.get('SOTTO_TEST_FOLLOWUP') == '1':
                            for meeting in value['meetings']:
                                meeting.update(attendee_emails=['owner@example.com','dana@example.com'],
                                    attendees=[{'name':'Owner','email':'owner@example.com'},{'name':'Dana','email':'dana@example.com'}])
                    elif name == 'get_meetings':
                        assert args['meeting_ids'] == ['synthetic-meeting']
                        value = {'meetings':[{'id':'synthetic-meeting','title':'Cedar review','notes':'Owner will send the deck to Dana at dana@example.com.', 'summary':'Owner will send the deck to Dana.'}]} if os.environ.get('SOTTO_TEST_FOLLOWUP') == '1' else {'meetings':[{'id':'synthetic-meeting','title':'Cedar review','notes':'Decision: use the pediatric clinic pilot.', 'summary':'Dana will send the Cedar commitment.'}]}
                    else:
                        assert name == 'get_meeting_transcript' and args['meeting_id'] == 'synthetic-meeting'
                        value = {'transcript':'Owner: I will send the deck to Dana at dana@example.com.'} if os.environ.get('SOTTO_TEST_FOLLOWUP') == '1' else {'transcript':'Dana: We agreed to the pediatric clinic pilot. I will send the commitment.'}
                    result = {'structuredContent':value,'content':[]}
                encoded = json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}).encode()
                try:
                    self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(encoded)));self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    (data_root/'synthetic-mcp-disconnected').write_text('disconnected')
        mcp_server = ThreadingHTTPServer(('127.0.0.1',0), SyntheticGranola)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(cert_path,key_path)
        mcp_server.socket = tls.wrap_socket(mcp_server.socket,server_side=True)
        threading.Thread(target=mcp_server.serve_forever,daemon=True).start()
        record = json.loads(granola_token_path.read_text());record['mcp_url'] = f'https://127.0.0.1:{mcp_server.server_port}/mcp'
        granola_token_path.write_text(json.dumps(record));granola_token_path.chmod(0o600)
    directory = data_root / 'knowledge/continuity'
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    for name, refs, summary in (
        ('granola', [{'sourceType':'granola','sourceId':'synthetic-meeting'}], 'Send Dana the Granola Cedar commitment'),
        ('mixed', [{'sourceType':'granola','sourceId':'synthetic-meeting'}, {'sourceType':'gmail','sourceId':'synthetic-mail'}], 'Denied mixed Granola Gmail commitment'),
    ):
        row = {'anchor_key':'connected-'+name,'status':'open','source':'granola','channel':'meeting',
            'action_type':'reply','contact_name':'Dana Cedar','contact_identifier':'dana@cedar.com',
            'source_thread_id':'granola:synthetic-meeting:commitment','source_refs':refs,
            'summary':summary,'created_at':'2026-10-01'}
        path = directory / (name+'.md')
        path.write_text('---\n'+yaml.safe_dump(row,sort_keys=False)+'---\n')
        path.chmod(0o600)

if os.environ.get('SOTTO_TEST_IMPORTANCE_READ') == '1':
    import datetime
    pack = pathlib.Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff'
    receiver._find_sotto_script = lambda *parts: str(pack.joinpath(*parts))
    receiver._runtime_source_authority = lambda: {'version':2,'complete':True,'revision':'a'*64,
        'bridge_sources':[],'google_sources':[],'connector_sources':[]}
    today = datetime.datetime.now(datetime.timezone.utc).date()
    recent = [(today-datetime.timedelta(days=days)).isoformat() for days in (1,7,14,21,28,35)]
    old = [(today-datetime.timedelta(days=days)).isoformat() for days in (60,67,74,81,88,95)]
    def history(name, days):
        return {'name':name,'importance_evidence':{'active_days':days,'sent_days':days,'received_days':days}}
    knowledge = data_root/'knowledge'
    knowledge.mkdir(parents=True,exist_ok=True);knowledge.chmod(0o700)
    state = knowledge/'relationship_state.json'
    state.write_text(json.dumps({'history':{'dana':history('Dana Cedar',[]),'casey':history('Casey Cedar',recent),
        'morgan':history('Morgan Cedar',old),'alex-one':history('Alex Cedar',recent),'alex-two':history('Alex Cedar',recent)}}))
    state.chmod(0o600)
    preferences = data_root/'preferences.json'
    preferences.write_text(json.dumps({'explicit':{'vip_people':['Dana Cedar']}}));preferences.chmod(0o600)

if os.environ.get('SOTTO_TEST_ATTENTION_READ') == '1':
    state_path = data_root/'knowledge'/'relationship_state.json'
    saved_state = json.loads(state_path.read_text())
    saved_state['attention_queue'] = [{'display_name':'Dana Cedar','queue_type':'waiting_on_you',
        'reason':'Saved observation: Dana sent the last message four days before the snapshot.',
        'canonical_id':'c_abcdef','contact_identifier':'dana@example.com'}]
    state_path.write_text(json.dumps(saved_state))
    state_path.chmod(0o600)

if os.environ.get('SOTTO_TEST_WEB_READ') == '1':
    # Actual provider ladder/model accounting; only external HTTP payloads are synthetic.
    pack = pathlib.Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff'
    receiver._find_sotto_script = lambda *parts: str(pack.joinpath(*parts))
    receiver._runtime_source_authority = lambda: {'version':2,'complete':os.environ.get('SOTTO_TEST_WEB_REPLY')=='1','revision':'a'*64 if os.environ.get('SOTTO_TEST_WEB_REPLY')=='1' else None,
        'bridge_sources':[],'google_sources':[],'connector_sources':[]}
    os.environ['EXA_API_KEY'] = 'synthetic-exa'
    os.environ['GOOGLE_AI_API_KEY'] = 'synthetic-gemini'
    os.environ['SOTTO_MODEL_PROXY_URL'] = 'https://proxy.synthetic.invalid'
    os.environ['SOTTO_MODEL_PROXY_TOKEN'] = 'synthetic-proxy-token'
    web = receiver.RUNTIME_WEB_READER._web()
    def synthetic_web_post(url, body, headers, timeout, **kwargs):
        with (data_root/'web-provider-calls.jsonl').open('a') as log:
            log.write(json.dumps({'url':url,'body':body})+'\n')
        if url == 'https://api.exa.ai/contents':
            return {'results':[]}  # actual resolver falls through to Gemini URL context
        assert 'generateContent' in url
        assert body['tools'] == [{'url_context':{}}]
        assert headers['X-Sotto-Operation'] and headers['X-Sotto-Parent-Operation']
        assert 'https://example.com/cedar' in body['contents'][0]['parts'][0]['text']
        return {'candidates':[{'urlContextMetadata':{'urlMetadata':[{'retrievedUrl':'https://example.com/cedar','urlRetrievalStatus':'URL_RETRIEVAL_STATUS_SUCCESS'}]},'finishReason':'STOP','content':{'parts':[{'text':'Cedar public page\nThe pediatric clinic pilot opens in November.'}]}}],
            'usageMetadata':{'promptTokenCount':10,'candidatesTokenCount':12}}
    web._post = synthetic_web_post

if os.environ.get('SOTTO_TEST_ASK_COMPANY') == '1':
    receiver._runtime_source_authority = lambda: {'version':2,'complete':True,
        'revision':'a'*64,'bridge_sources':[],'google_sources':[],'connector_sources':[]}
    os.environ['GOOGLE_AI_API_KEY'] = 'synthetic-gemini'
    os.environ['SOTTO_MODEL_PROXY_URL'] = 'https://proxy.synthetic.invalid'
    os.environ['SOTTO_MODEL_PROXY_TOKEN'] = 'synthetic-model-proxy'
    injection = data_root/'synthetic-provider'
    injection.mkdir(exist_ok=True)
    injection.chmod(0o700)
    (injection/'sitecustomize.py').write_text("""import io,json,os,urllib.request
from pathlib import Path
original=urllib.request.urlopen
def open(request,*args,**kwargs):
    url=getattr(request,'full_url',str(request))
    if url=='http://127.0.0.1:48317/v1/run':
        body=json.loads(request.data)
        assert 'skill' not in body, 'managed answers use the stateless read-only runtime'
        assert ('Company-only public research' in body['prompt'] or '"website"' in body['prompt']) and 'Cedar Labs' in body['prompt']
        if '"website"' in body['prompt']:
            assert 'Their homepage describes clinic scheduling.' in body['prompt']
            assert '"url": "https://www.cedar.example/"' in body['prompt']
            assert any(text in body['prompt'] for text in ('their site too?', 'check their site too', 'check their website too', 'check their website - anything else worth knowing?'))
        with (Path(os.environ['SOTTO_DATA'])/'synthetic-answer-calls.jsonl').open('a') as log: log.write(json.dumps({'stateless':True})+'\\n')
        return io.BytesIO(json.dumps({'text':'Cedar Labs builds clinic tools. Company-only grounded research is saved.','usage':{}}).encode())
    if url.startswith('https://proxy.synthetic.invalid'):
        body=json.loads(request.data)
        if body['tools']==[{'url_context':{}}]:
            assert 'https://cedar.example/' in body['contents'][0]['parts'][0]['text']
            with (Path(os.environ['SOTTO_DATA'])/'synthetic-website-calls.jsonl').open('a') as log: log.write(json.dumps({'retrieved':True})+'\\n')
            return io.BytesIO(json.dumps({'candidates':[{'content':{'parts':[{'text':'Cedar Labs\\nTheir homepage describes clinic scheduling.'}]},'finishReason':'STOP','urlContextMetadata':{'urlMetadata':[{'retrievedUrl':'https://www.cedar.example/','urlRetrievalStatus':'URL_RETRIEVAL_STATUS_SUCCESS'}]}}],'usageMetadata':{'promptTokenCount':10,'candidatesTokenCount':20}}).encode())
        assert body['tools']==[{'google_search':{}}]
        assert 'Cedar Labs' in body['contents'][0]['parts'][0]['text']
        root=Path(os.environ['SOTTO_DATA'])
        with (root/'synthetic-company-calls.jsonl').open('a') as log: log.write(json.dumps({'grounded':True})+'\\n')
        result={'companies':[{'identity_status':'identified','company_deep':{'company':'Cedar Labs','builds':['Builds clinic tools.'],'founder_story':'Founded for pediatric clinics.','traction':['Public pilot (https://cedar.example/pilot)'],'market':'Clinic operations'}}]}
        return io.BytesIO(json.dumps({'candidates':[{'content':{'parts':[{'text':json.dumps(result)}]},'finishReason':'STOP'}],'usageMetadata':{'promptTokenCount':10,'candidatesTokenCount':20}}).encode())
    return original(request,*args,**kwargs)
urllib.request.urlopen=open
original_opener=urllib.request.OpenerDirector.open
def opener_open(self,request,*args,**kwargs):
    if getattr(request,'full_url','')=='http://127.0.0.1:48317/v1/run': return open(request,*args,**kwargs)
    return original_opener(self,request,*args,**kwargs)
urllib.request.OpenerDirector.open=opener_open
""".replace("Cedar Labs", os.environ.get("SOTTO_TEST_COMPANY_NAME", "Cedar Labs")))
    os.environ['PYTHONPATH'] = str(injection)

if os.environ.get('SOTTO_TEST_FOLLOWUP') == '1':
    import managed
    managed.record_google_consent(data_root, ['https://www.googleapis.com/auth/gmail.readonly',
        'https://www.googleapis.com/auth/gmail.compose'])
    receiver._runtime_source_authority = lambda: {'version':2,'complete':True,'revision':'a'*64,
        'bridge_sources':[],'google_sources':['gmail'],'connector_sources':['granola']}
    os.environ['SOTTO_USER_EMAIL'] = 'owner@example.com'
    os.environ['SOTTO_USER_NAME'] = 'Owner'
    os.environ['SOTTO_MODEL_PROXY_URL'] = 'https://proxy.synthetic.invalid'
    os.environ['SOTTO_MODEL_PROXY_TOKEN'] = 'synthetic-model-proxy'
    injection = data_root/'synthetic-provider'
    injection.mkdir(exist_ok=True)
    injection.chmod(0o700)
    (injection/'sitecustomize.py').write_text("""import io,json,os,urllib.request
from pathlib import Path
original=urllib.request.urlopen
def open(request,*args,**kwargs):
    url=getattr(request,'full_url',str(request))
    if url.startswith('https://proxy.synthetic.invalid'):
        body=json.loads(request.data)
        root=Path(os.environ['SOTTO_DATA'])
        with (root/'synthetic-model-calls.jsonl').open('a') as log: log.write(json.dumps({'body':body})+'\\n')
        result={'followup_markdown':'Recorded your meeting commitment.', 'commitments':[{'meeting_id':'synthetic-meeting','meeting':'Cedar review','owner':'you','owner_is_user':True,'what':'send the deck','to_email':'dana@example.com','source_snippet':'Owner: I will send the deck to Dana at dana@example.com.'}], 'drafts':[{'to_name':'Dana','to_email':'dana@example.com','channel':'email','subject':'Deck follow-up','body':'Here is the deck as promised.'}]}
        return io.BytesIO(json.dumps({'candidates':[{'content':{'parts':[{'text':json.dumps(result)}]},'finishReason':'STOP'}],'usageMetadata':{'promptTokenCount':10,'candidatesTokenCount':20}}).encode())
    return original(request,*args,**kwargs)
urllib.request.urlopen=open
""")
    os.environ['PYTHONPATH'] = str(injection)
    assert receiver._composition_evidence('skill:'+'a'*64+':followup') is not None, 'fixture composition proof unavailable'

if os.environ.get('SOTTO_TEST_RELATIONSHIP_PULSE') == '1':
    import pulse_provider_fixture
    pulse_provider_fixture.configure(receiver)

if os.environ.get('SOTTO_TEST_PROACTIVE') == '1':
    receiver._runtime_source_authority = lambda: {'version':2,'complete':True,
        'revision':'a'*64,'bridge_sources':[],'google_sources':[],'connector_sources':[]}
    os.environ['SOTTO_QUIET_START'] = '0'
    os.environ['SOTTO_QUIET_END'] = '0'
    sys.path.insert(0, str(pack/'_shared/scripts'))
    import schedule_wakeup
    if not schedule_wakeup.current():
        schedule_wakeup.create((datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),
                               'Review the Cedar deck')

if os.environ.get('SOTTO_TEST_TRIAGE') == '1':
    import triage_provider_fixture
    triage_provider_fixture.configure(receiver)

if os.environ.get('SOTTO_TEST_CALENDAR_MOVE') == '1':
    import owner_calendar_fixture
    owner_calendar_fixture.configure(receiver)

if os.environ.get('SOTTO_TEST_BRIDGE_SEND') == '1':
    import owner_bridge_send_fixture
    owner_bridge_send_fixture.configure(receiver)

if os.environ.get('SOTTO_TEST_CONTACTS') == '1':
    import owner_contacts_fixture
    owner_contacts_fixture.configure(receiver)

if os.environ.get('SOTTO_TEST_DIRECT_PROCEDURE') == '1':
    from runtime_owner_loop import _load_writer
    _load_writer()
    import master_file
    preference_writer = receiver.RUNTIME_OWNER_PREFERENCE_TOOL._preferences()
    original_sync = preference_writer._sync_existing_preferences
    def direct_master_sync(path):
        if str(path) == master_file.path() and (data_root / 'direct-master-sync-fail').exists():
            raise OSError('synthetic master sync failure')
        return original_sync(path)
    preference_writer._sync_existing_preferences = direct_master_sync
    receiver.RUNTIME_OWNER_PREFERENCE_TOOL._preferences = lambda: preference_writer
    receiver._runtime_source_authority = lambda: {'version':2,'complete':False,'revision':None,'bridge_sources':[],'google_sources':[],'connector_sources':[]}

if os.environ.get('SOTTO_TEST_USEFULNESS') == '1':
    from datetime import datetime, timezone
    import managed
    from runtime_owner_loop import _load_writer
    _load_writer()
    import usefulness_feedback
    managed.record_google_consent(data_root, ['https://www.googleapis.com/auth/gmail.readonly'])
    day = datetime.now(timezone.utc).date().isoformat()
    archive = data_root / 'briefs' / f'{day}_morning.json'
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        archive.write_text(json.dumps({'brief_text': 'School deadline: return the form by Friday.\nDonation item: community fundraiser next week.', '_source_permissions': ['gmail']}))
        archive.chmod(0o600)
        marker = archive.parent / f'{day}.morning.delivered'
        marker.write_bytes(b'synthetic accepted delivery');marker.chmod(0o600)
    preference_writer = receiver.RUNTIME_OWNER_PREFERENCE_TOOL._preferences()
    original_sync = preference_writer._sync_existing_preferences
    def usefulness_sync(path):
        if str(path) == usefulness_feedback.log_outcome._path() and (data_root / 'usefulness-sync-fail').exists():
            raise OSError('synthetic outcomes sync failure')
        return original_sync(path)
    preference_writer._sync_existing_preferences = usefulness_sync
    receiver.RUNTIME_OWNER_PREFERENCE_TOOL._preferences = lambda: preference_writer
    receiver._runtime_source_authority = lambda: {'version':2,'complete':True,'revision':'a'*64,'bridge_sources':[],'google_sources':['gmail'],'connector_sources':[]}

docsend_dashboard_token = None
if os.environ.get('SOTTO_TEST_DOCSEND') == '1':
    import owner_docsend_fixture
    docsend_dashboard_token = owner_docsend_fixture.configure(receiver)

if os.environ.get('SOTTO_TEST_PRIOR_PREP') == '1':
    __import__('prior_prep_research_fixture').configure(receiver)
server = ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('SOTTO_TEST_RECEIVER_PORT', '0'))), receiver.Handler)
os.environ['PORT'] = str(server.server_port)
threading.Thread(target=server.serve_forever, daemon=True).start()
print(json.dumps({'port': server.server_port, 'identity': identity,
                  'bearer': capability, 'readBearer': receiver.MCP_TOKEN,
                  **({'dashboardToken': docsend_dashboard_token} if docsend_dashboard_token else {}),
                  **({'meetingOfferMessageId': meeting_offer_message} if meeting_offer_message else {})}), flush=True)
for command in sys.stdin:
    if command == 'usefulness-brief-prompt\n':
        import compose_brief
        print(json.dumps({'prompt':compose_brief.build_prompt(compose_brief._load_prompt(), {'type':'morning','google':{},'local':{}})}), flush=True)
    if command in ('execute-followup\n', 'execute-relationship-pulse\n', 'execute-proactive\n', 'execute-ask-research\n'):
        job = receiver.WORK_QUEUE.claim(receiver.DATA, 'fixture-followup-worker')
        if job:
            receiver._work_one(job)
        receiver.OUTBOX.drain()
        jobs = receiver.WORK_QUEUE.get(receiver.DATA, job['id']) if job else None
        with receiver.WORK_QUEUE._db(receiver.DATA) as db:
            all_jobs = [dict(row) for row in db.execute('SELECT id,status,error,due,valid_until FROM jobs')]

        print(json.dumps({'executed':bool(job),'jobs':jobs,'all_jobs':all_jobs,'outbox':json.loads(pathlib.Path(receiver.OUTBOX.path()).read_text()) if pathlib.Path(receiver.OUTBOX.path()).exists() else None}), flush=True)
    if command.startswith('conditional-state='):
        import yaml
        anchor, status = command.removeprefix('conditional-state=').strip().split(':')
        for path in (data_root / 'knowledge' / 'continuity').glob('*.md'):
            row = receiver._owner_continuity().ledger_io.parse_frontmatter(path.read_text())
            if row and row.get('anchor_key') == anchor:
                row['status'] = 'open' if status == 'snoozed' else status
                if status == 'snoozed':
                    row['snoozed_until'] = '2099-01-01T00:00:00Z'
                elif status == 'open':
                    row.pop('snoozed_until', None)
                path.write_text('---\n' + yaml.safe_dump(row, sort_keys=False) + '---\n')
                path.chmod(0o600)
        print(json.dumps({'changed': True}), flush=True)
    if command == 'conditional-revoke\n':
        managed.record_google_consent(str(data_root), [])
        print(json.dumps({'revoked': True}), flush=True)
    if command.startswith('conditional-due='):
        from datetime import datetime, timezone
        import time
        import delivery_effects
        sys.path.insert(0, str(pack / 'proactive' / 'scripts'))
        import proactive_scan
        from zoneinfo import ZoneInfo
        import compose_notification
        now = datetime.fromisoformat(command.removeprefix('conditional-due=').strip().replace('Z', '+00:00')).astimezone(ZoneInfo('America/Los_Angeles'))
        candidates = proactive_scan._intention_candidates(now)
        result = proactive_scan.scan({}, [], {}, '', now, intentions=candidates)
        sends = []
        attempts = []
        box = receiver.OUTBOX
        box.HOOKS.update(valid=lambda payload: delivery_effects.valid(payload['effects'], time.time()),
            send=lambda body, target, dispatch_id, attempt: (sends.append(body) or
                (True, '', {'acceptance': 'accepted', 'message_id': 'synthetic-due-' + dispatch_id})),
            on_delivered=lambda payload: delivery_effects.finalize(payload['effects'], payload.get('receipt') or {}))
        funnel = proactive_scan._funnel()
        admitted = funnel.triage({'events': [proactive_scan._proactive_event(nudge) for nudge in result['nudges']]}, now_local=now)
        bundle = admitted.get('bundle') or {'events': []}
        accepted = {row['event']['key']: row['decision_id'] for row in bundle.get('events', [])}
        for nudge in result['nudges']:
            if nudge['key'] not in accepted:
                continue
            ident = nudge['intention_id']
            nudge = dict(nudge, decision_id=accepted[nudge['key']])
            body = compose_notification.compose('proactive', [nudge], now=now)
            if body == 'NO_NUDGES':
                continue
            effect = delivery_effects.for_bundle({'events': [row for row in bundle['events'] if row['event']['key'] == nudge['key']]})['effects']
            effect.append({'kind': 'intention', 'id': ident})
            payload = {'label': 'cron:sotto-proactive', 'body': body, 'target': 'owner', 'effects': effect}
            box._enqueue(ident, box.KIND_NUDGE, payload, time.time(), now.strftime('%Y-%m-%d'))
            attempts.append({'body': body, 'effects': effect, 'valid': delivery_effects.valid(effect, time.time()), 'attempted': box._attempt(ident)})
        box.drain()
        rows = receiver.RUNTIME_OWNER_REMINDER_TOOL.reminders._writer().current(strict=True)
        print(json.dumps({'candidates': len(candidates), 'funnel': admitted, 'attempts': attempts, 'sends': sends, 'rows': rows}), flush=True)
    if command == 'seed-reminder-list-exclusions\n':
        import hashlib
        writer = receiver.RUNTIME_OWNER_REMINDER_TOOL.reminders._writer()
        existing = writer.current(strict=True)[0]
        for label in ('foreign', 'fired'):
            proof = dict(existing['owner_create'], event_id=hashlib.sha256(label.encode()).hexdigest())
            if label == 'foreign':
                proof['identity_digest'] = 'f' * 64
            _, row = writer.create_tool_admitted(existing['due'], label + ' fixture reminder', proof)
            if label == 'fired':
                writer.transition(row['id'], 'fired')
        print(json.dumps({'seeded': True}), flush=True)
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
