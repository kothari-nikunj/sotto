import json
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PACK = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'


@pytest.fixture
def flow(tmp_path, monkeypatch, request):
    for part in ('_shared/lib', '_shared/scripts', '_shared/knowledge'):
        monkeypatch.syspath_prepend(str(PACK / part))
    import relationship_pulse_pipeline as pipeline
    import source_context
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-host')
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'a'*32)
    monkeypatch.setenv('SOTTO_SKILL_COMPOSITION_PROOF', json.dumps({'source_revision':'b'*64,'route_digest':'c'*64}))
    now = datetime.now(timezone.utc)
    local = {'contacts':[{'name':'Dana','phones':['+1']}], 'emails':[],
        'source_status':{'imessage':'ok','contacts':'ok'},
        'imessage':[{'handle':'+1','resolved_name':'Dana','timestamp':(now-timedelta(days=5)).isoformat(),
                     'is_from_me':False,'is_group_chat':False, 'text':'Could you share the Cedar deck?'}]}
    calls = []
    def read(*args, **kwargs):
        assert kwargs == {'timeout': 8}
        calls.append(args)
        return local
    monkeypatch.setattr(source_context, 'bridge_call', read)
    state = {'complete':True,'revision':'b'*64,'bridge_sources':['imessage','contacts'],'google_sources':[]}
    def invoke():
        if getattr(request, 'param', None) == 'scheduled':
            import procedure_runner
            return {'text': procedure_runner.run({'pack': str(PACK), 'kind': 'pulse'})}
        return pipeline.run(PACK, {'mode':'current'}, lambda *a,**k:pytest.fail('ungranted Google'),
                            authority=lambda:state.copy())
    if getattr(request, 'param', None) == 'scheduled':
        # Only the external authority endpoint is synthetic; run the actual scheduled
        # procedure, bounded snapshot/query/compute, persistence and effect staging.
        monkeypatch.setattr(pipeline.run, '__kwdefaults__', {'authority': lambda: state.copy()})
    return tmp_path, monkeypatch, local, calls, state, invoke


@pytest.mark.parametrize('flow', ['attended', 'scheduled'], indirect=True)
def test_original_compute_state_and_restart_once(flow):
    root,_,_,calls,_,invoke = flow
    result = invoke()
    assert 'Dana' in result['text']
    state = root/'knowledge/relationship_state.json'
    before = state.read_bytes()
    assert invoke() == result and state.read_bytes() == before
    assert calls == [('read_local', {'since_hours':25})]


def test_no_usable_interactions_no_state_write(flow):
    root,_,local,_,_,invoke = flow
    local['imessage'] = []
    assert 'skipping the pulse' in invoke()['text']
    assert not (root/'knowledge/relationship_state.json').exists()


@pytest.mark.parametrize('flow', ['attended', 'scheduled'], indirect=True)
def test_persistence_sync_loss_holds_no_repeat(flow):
    root, monkeypatch, _, _, _, invoke = flow
    import preferences
    sync = preferences._sync_existing_preferences
    def fail(path):
        if str(path).endswith('relationship_state.json'):
            raise OSError('synthetic sync loss')
        sync(path)
    monkeypatch.setattr(preferences, '_sync_existing_preferences', fail)
    with pytest.raises(OSError):
        invoke()
    before = (root/'knowledge/relationship_state.json').read_bytes()
    monkeypatch.setattr(preferences, '_sync_existing_preferences', sync)
    with pytest.raises(RuntimeError, match='unconfirmed'):
        invoke()
    assert (root/'knowledge/relationship_state.json').read_bytes() == before


@pytest.mark.parametrize('flow', ['attended', 'scheduled'], indirect=True)
def test_current_authority_change_withholds_cached_result(flow):
    _,_,_,_,state,invoke = flow
    invoke()
    state['revision'] = 'd'*64
    with pytest.raises(RuntimeError, match='authority unavailable'):
        invoke()


def test_mixed_bridge_receipts_withhold_unavailable_content(flow):
    root,_,local,_,state,invoke = flow
    state['bridge_sources'].append('whatsapp')
    local['source_status']['imessage'] = 'unavailable'
    local['source_status']['whatsapp'] = 'ok'
    local['whatsapp'] = []
    result = invoke()
    assert 'imessage' in result['text'] and 'skipping the pulse' in result['text']
    assert not (root/'knowledge/relationship_state.json').exists()


@pytest.mark.parametrize('flow', ['attended', 'scheduled'], indirect=True)
def test_completed_checkpoint_sync_failure_retry_does_not_reapply(flow):
    root,monkeypatch,_,calls,_,invoke = flow
    result = invoke()
    before = (root/'knowledge/relationship_state.json').read_bytes()
    import preferences
    sync = preferences._sync_existing_preferences
    monkeypatch.setattr(preferences, '_sync_existing_preferences', lambda path: (_ for _ in ()).throw(OSError('sync')))
    with pytest.raises(OSError):
        invoke()
    monkeypatch.setattr(preferences, '_sync_existing_preferences', sync)
    assert invoke() == result
    assert (root/'knowledge/relationship_state.json').read_bytes() == before and len(calls) == 1


@pytest.mark.parametrize('known', [True, False])
def test_original_gmail_only_graph_known_or_unknown(flow, known):
    root,_,_,_,state,_ = flow
    import relationship_pulse_pipeline as pipeline
    import knowledge as kg
    if known:
        person = Path(kg.people_dir())/'dana.md'
        person.parent.mkdir(parents=True, exist_ok=True)
        person.write_text(kg.serialize_person_file(kg.PersonFile(name='Dana',canonical_id='c_abcdef',
                                                               identifiers=['dana@example.com'])))
    state['bridge_sources'] = []
    state['google_sources'] = ['gmail']
    calls = []
    def command(script, *args, **kwargs):
        calls.append((script, args))
        assert '--window-days' not in args
        assert '--pulse-since-epoch' in args
        Path(args[args.index('--gmail-out')+1]).write_text(json.dumps([
            {'from':'Dana <dana@example.com>', 'to':'owner@example.com',
             'subject':'Cedar deck', 'snippet':'Could you share the Cedar deck?', 'date':(datetime.now(timezone.utc)-timedelta(days=5)).isoformat()}]))
        Path(args[args.index('--source-results-out')+1]).write_text(json.dumps({'gmail':{'status':'ok'}}))
    result = pipeline.run(PACK, {'mode':'current'}, command, authority=lambda:state.copy())
    assert len(calls) == 1 and 'Fresh message check unavailable' in result['text']
    if known:
        assert 'Dana' in result['text'] and (root/'knowledge/relationship_state.json').exists()
    else:
        assert 'known-person interactions' in result['text']
        assert not (root/'knowledge/relationship_state.json').exists()


def test_authority_change_after_compute_prevents_state_write(flow):
    root,monkeypatch,_,_,state,invoke = flow
    import jsonstore
    write = jsonstore.write_atomic
    def changed(path, value):
        write(path, value)
        if isinstance(value, dict) and value.get('compute', {}).get('status') == 'completed':
            state['revision'] = 'd'*64
    monkeypatch.setattr(jsonstore, 'write_atomic', changed)
    with pytest.raises(RuntimeError, match='authority unavailable'):
        invoke()
    assert not (root/'knowledge/relationship_state.json').exists()


def test_neither_source_preserves_prior_state(flow):
    root,_,_,_,state,invoke = flow
    previous = root/'knowledge/relationship_state.json'
    previous.parent.mkdir(parents=True)
    previous.write_text('{"history": {"existing": {"name": "Existing"}}}')
    before = previous.read_bytes()
    state['bridge_sources'] = []
    result = invoke()
    assert 'Fresh message and email checks unavailable.' in result['text']
    assert 'skipping the pulse' in result['text'] and previous.read_bytes() == before


def _lapsed_history(root, channel='imessage'):
    old = (datetime.now(timezone.utc) - timedelta(days=60)).date()
    days = [(old - timedelta(days=i)).isoformat() for i in range(4)]
    path = root/'knowledge/relationship_state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'history': {'Maya': {
        'name': 'Maya', 'last_contact': days[0], 'interactions': 12, 'trend': 'stable',
        'channels': {channel: days[0]},
        'importance_evidence': {'sent_days': days, 'received_days': days, 'active_days': days}}}}))


def test_recent_first_run_check_does_not_claim_full_window_absence(flow):
    """The attended pulse hard-coded incomplete coverage, so `lapsed` never appeared and every
    pulse ended with the missing-history line, even when every read source reported `ok`."""
    root,_,_,_,_,invoke = flow
    _lapsed_history(root)
    text = invoke()['text']
    assert 'Fully lost touch' not in text and 'Maya' not in text
    assert 'Some history may be missing' in text


def test_partial_channel_withholds_lapse_and_says_so(flow):
    root,_,local,_,_,invoke = flow
    _lapsed_history(root)
    local['source_status']['imessage'] = 'partial'
    text = invoke()['text']
    assert 'Fully lost touch' not in text
    assert 'partial or unavailable for: imessage' in text and 'Some history may be missing' in text


@pytest.mark.parametrize('status', ['ok', 'partial'])
def test_email_coverage_line_follows_the_gmail_receipt(flow, status):
    root,_,_,_,state,_ = flow
    import relationship_pulse_pipeline as pipeline
    _lapsed_history(root, channel='email')
    state['google_sources'] = ['gmail']
    def command(script, *args, **kwargs):
        Path(args[args.index('--gmail-out')+1]).write_text('[]')
        Path(args[args.index('--source-results-out')+1]).write_text(json.dumps({'gmail':{'status':status}}))
    text = pipeline.run(PACK, {'mode':'current'}, command, authority=lambda:state.copy())['text']
    assert ('Latest email check was incomplete' in text) is (status == 'partial')
    assert 'Fully lost touch' not in text


@pytest.mark.parametrize('status,error,expected', [
    ('partial', 'rate_limited', 'Gmail rate limit'),
    ('unavailable', 'RefreshError', 'Gmail authorization'),
    ('unavailable', 'PermissionError', 'Gmail authorization'),
    ('partial', 'provider response containing private data', None),
])
def test_source_failure_is_specific_without_exposing_provider_text(flow, status, error, expected):
    root,_,_,_,state,_ = flow
    import relationship_pulse_pipeline as pipeline
    state['google_sources'] = ['gmail']
    _lapsed_history(root, channel='email')
    def command(script, *args, **kwargs):
        Path(args[args.index('--gmail-out')+1]).write_text('[]')
        Path(args[args.index('--source-results-out')+1]).write_text(json.dumps({
            'gmail': {'status': status, 'error': error}}))
    text = pipeline.run(PACK, {'mode':'current'}, command, authority=lambda:state.copy())['text']
    if expected:
        assert expected in text
    else:
        assert error not in text
    assert 'Fully lost touch' not in text and 'Some history may be missing' in text


def test_partial_email_keeps_useful_context_and_discloses_gap(flow):
    _,_,_,_,state,_ = flow
    import relationship_pulse_pipeline as pipeline
    state['google_sources'] = ['gmail']
    def command(script, *args, **kwargs):
        Path(args[args.index('--gmail-out')+1]).write_text('[]')
        Path(args[args.index('--source-results-out')+1]).write_text(json.dumps({
            'gmail': {'status': 'partial', 'error': 'rate_limited'}}))
    text = pipeline.run(PACK, {'mode':'current'}, command, authority=lambda:state.copy())['text']
    assert 'Cedar deck' in text and 'review and reply if still relevant' in text
    assert 'Latest email check was incomplete' in text and 'Gmail rate limit' in text


def _canonical_writer():
    spec = importlib.util.spec_from_file_location('pulse_ledger_writer', PACK / 'morning-brief/scripts/continuity_resolve.py')
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    return writer


def _write_source_commitment(writer, now, name='Morgan', **overrides):
    row = {'anchor_key': f'email:reply:{name}', 'status': 'open', 'action_type': 'reply',
           'contact_name': name, 'contact_identifier': name.lower() + '@example.com',
           'source': 'gmail', 'channel': 'gmail', 'source_thread_id': 'thread-' + name,
           'source_message_id': 'message-' + name, 'summary': 'Send the Orion launch packet',
           'deadline': now.date().isoformat(), 'created_at': (now - timedelta(days=2)).isoformat()}
    row.update(overrides)
    writer._persist(row)


def test_retained_history_and_canonical_commitment_survive_incremental_refresh_and_repeat(flow):
    root, monkeypatch, local, bridge_calls, state, _ = flow
    import relationship_pulse_pipeline as pipeline
    now = datetime.now(timezone.utc)
    writer = _canonical_writer()
    _write_source_commitment(writer, now)
    # Exercise the real history observer/writer, not a mocked query response.
    spec = importlib.util.spec_from_file_location('pulse_history_fixture', PACK / 'relationship-pulse/scripts/relationship_pulse.py')
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    observer.observe_history(local, now=now)
    before = json.loads((root / 'knowledge/relationship_state.json').read_text())
    stored = next(iter(before['history'].values()))['latest_inbound']
    assert stored['request'] is True and 'text' not in stored  # a request cue, never its wording
    until = int((now - timedelta(hours=1)).timestamp())
    (root / 'knowledge/history-state.json').write_text(json.dumps({'sources': {
        source: {'complete': True, 'until': until} for source in ('gmail', 'imessage')}}))
    state['google_sources'] = ['gmail']
    local['imessage'] = []
    calls = []
    def command(script, *args, **kwargs):
        calls.append(args)
        assert '--window-days' not in args and '--sent-max' not in args
        assert args[args.index('--pulse-since-epoch') + 1] == until - 1
        Path(args[args.index('--gmail-out') + 1]).write_text('[]')
        Path(args[args.index('--source-results-out') + 1]).write_text(json.dumps({'gmail': {'status': 'ok'}}))
    for job in ('d' * 32, 'e' * 32):
        monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', job)
        result = pipeline.run(PACK, {'mode': 'current'}, command, authority=lambda: state.copy())
        assert 'Morgan' in result['text'] and 'Send the Orion launch packet' in result['text']
        assert 'Follow-ups' in result['text'] and 'Waiting for them' not in result['text']
        # A request retained by observe_history is offered for review without stored wording.
        assert 'Earlier request on ' in result['text'] and 'Cedar' not in result['text']
        assert 'Fully lost touch' not in result['text']
    assert len(calls) == 2 and all(args[1]['since_hours'] <= 2 for args in bridge_calls)
    assert len(list((root / 'knowledge/continuity').glob('*.md'))) == 1


def test_all_fresh_reads_unavailable_still_returns_permitted_commitments_and_excludes_hidden_rows(flow):
    root, monkeypatch, _, _, state, _ = flow
    import relationship_pulse_pipeline as pipeline
    import source_context
    now = datetime.now(timezone.utc)
    writer = _canonical_writer()
    _write_source_commitment(writer, now)
    _write_source_commitment(writer, now, 'Closed', status='resolved')
    _write_source_commitment(writer, now, 'Parked', status='parked')
    _write_source_commitment(writer, now, 'Snoozed', snoozed_until=(now + timedelta(days=2)).isoformat())
    writer._persist({'anchor_key': 'brief:birthday', 'status': 'open', 'action_type': 'reply',
        'contact_name': 'Birthday', 'summary': 'Send happy birthday wishes',
        'source_brief_at': now.isoformat(), 'created_at': now.isoformat()})
    state['google_sources'] = ['gmail']
    def unavailable(*args, **kwargs):
        assert kwargs == {'timeout': 8}
        raise TimeoutError('synthetic Bridge timeout')
    monkeypatch.setattr(source_context, 'bridge_call', unavailable)
    def command(script, *args, **kwargs):
        assert '--pulse-since-epoch' in args
        Path(args[args.index('--gmail-out') + 1]).write_text('[]')
        Path(args[args.index('--source-results-out') + 1]).write_text(json.dumps({'gmail': {'status': 'unavailable'}}))
    result = pipeline.run(PACK, {'mode': 'current'}, command, authority=lambda: state.copy())
    assert 'Morgan' in result['text'] and 'Orion launch packet' in result['text']
    assert 'Fresh message and email checks unavailable' in result['text']
    assert all(name not in result['text'] for name in ('Closed', 'Parked', 'Snoozed', 'Birthday'))
    # Current source authority governs retained canonical rows too.
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'f' * 32)
    state['google_sources'] = []
    result = pipeline.run(PACK, {'mode': 'current'}, command, authority=lambda: state.copy())
    assert 'Morgan' not in result['text'] and 'Orion' not in result['text']



def test_pulse_keeps_source_birthday_owner_and_event_tasks_like_the_brief(flow):
    root, _, _, _, _, invoke = flow
    writer = _canonical_writer()
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=6)).isoformat()
    base = {'status': 'open', 'action_type': 'follow_up', 'contact_identifier': '+15555550101',
            'source': 'imessage', 'channel': 'imessage', 'created_at': old}
    for key, name, summary in (
            ('phone-birthday', 'Legacy Birthday', 'Birthday was on Oct 4 and belated follow-up remains open'),
            ('venue', 'Venue Task', 'Book venue for birthday'),
            ('invoice', 'Invoice Task', 'Pay invoice for birthday')):
        writer._persist({**base, 'anchor_key': key, 'contact_name': name, 'summary': summary,
            **({'channel': 'phone'} if key == 'phone-birthday' else {})})
    writer._persist({'anchor_key': 'manual:birthday', 'status': 'open', 'action_type': 'follow_up',
        'source': 'user_added', 'channel': 'manual', 'source_thread_id': 'manual:birthday',
        'owner_event_id': 'owner-birthday', 'contact_name': 'Owner Reminder',
        'summary': 'Send happy birthday wishes', 'created_at': old}, new_owner_content=True)
    writer._persist({**base, 'anchor_key': 'relative', 'contact_name': 'Packet Task',
        'summary': 'Send the packet today and confirm tomorrow'})
    from loops_query import query_permitted_rows
    standard = query_permitted_rows(str(root), ['imessage'])
    assert all('created_at' not in row and 'birthday_courtesy' not in row and 'ask' not in row
               for row in standard['you_owe'])
    assert any(row['name'] == 'Legacy Birthday' and row['channel'] == 'phone'
               for row in standard['you_owe'])  # accepted source-attributable legacy shape
    import ledger_io
    brief_names = {row.get('contact_name') for row in ledger_io.load_active()}
    assert {row['name'] for row in standard['you_owe']} <= brief_names  # one visibility rule
    paths = list((root / 'knowledge/continuity').glob('*.md'))
    before = {path: path.read_bytes() for path in paths}
    text = invoke()['text']
    # One courtesy rule (the brief's): source-attributable birthday rows are observed requests.
    assert all(name in text for name in ('Legacy Birthday', 'Venue Task', 'Invoice Task', 'Owner Reminder', 'Packet Task'))
    recorded = now - timedelta(days=6)
    label = f"{recorded.strftime('%b')} {recorded.day}"
    assert f'From {label}: “Send the packet today and confirm tomorrow”' in text
    assert 'recorded open in your ledger' not in text and 'Saved wording' not in text
    assert {path: path.read_bytes() for path in paths} == before

def test_retained_rows_from_revoked_or_unknown_channels_are_not_projected(flow):
    root, _, _, _, state, invoke = flow
    now = datetime.now(timezone.utc)
    path = root / 'knowledge/relationship_state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'history': {'Hidden': {'name': 'Hidden', 'last_contact': now.date().isoformat(),
        'channels': {'email': now.date().isoformat()}, 'last_from_them': (now - timedelta(days=5)).isoformat(),
        'latest_inbound': {'at': (now - timedelta(days=5)).isoformat(), 'channel': 'email',
                           'text': 'Could you send the secret?', 'direct': True}}}}))
    result = invoke()
    assert 'Hidden' not in result['text'] and 'secret' not in result['text']


@pytest.mark.parametrize('gap,refresh_status,expect_lapse', [(False, 'ok', True), (True, 'ok', False), (False, 'partial', False)])
def test_complete_canonical_history_plus_contiguous_refresh_proves_lapse(flow, gap, refresh_status, expect_lapse):
    root, _, _, _, state, _ = flow
    import relationship_pulse_pipeline as pipeline
    _lapsed_history(root, channel='email')
    now = datetime.now(timezone.utc)
    (root / 'knowledge/history-state.json').write_text(json.dumps({'sources': {'gmail': {
        'initial_since': int((now - timedelta(days=70)).timestamp()),
        'initial_until': int((now - timedelta(days=28)).timestamp()), 'initial_complete': True,
        'since': int((now - timedelta(days=2)).timestamp()),
        'until': int((now - timedelta(hours=1)).timestamp()), 'complete': True,
        'excluded_pages': {'gap': {'reason': 'SAFETY'}} if gap else {}}}}))
    state['google_sources'] = ['gmail']
    def command(script, *args, **kwargs):
        since = args[args.index('--pulse-since-epoch') + 1]
        assert now.timestamp() - since < 3700  # an incremental refresh, not another 42 days
        Path(args[args.index('--gmail-out') + 1]).write_text('[]')
        Path(args[args.index('--source-results-out') + 1]).write_text(json.dumps({'gmail': {
            'status': refresh_status, 'complete': refresh_status == 'ok'}}))
    text = pipeline.run(PACK, {'mode': 'current'}, command, authority=lambda: state.copy())['text']
    assert ('Fully lost touch' in text) is expect_lapse
    assert ('Some history may be missing' in text) is (not expect_lapse)


def test_complete_history_and_empty_complete_refresh_can_still_report_lapse(flow):
    root, _, local, _, state, _ = flow
    import relationship_pulse_pipeline as pipeline
    _lapsed_history(root, channel='email')
    now = datetime.now(timezone.utc)
    (root / 'knowledge/history-state.json').write_text(json.dumps({'sources': {'gmail': {
        'initial_since': int((now - timedelta(days=70)).timestamp()), 'initial_complete': True,
        'until': int((now - timedelta(hours=1)).timestamp()), 'complete': True}}}))
    local['imessage'] = []
    state['google_sources'] = ['gmail']
    def command(script, *args, **kwargs):
        Path(args[args.index('--gmail-out') + 1]).write_text('[]')
        Path(args[args.index('--source-results-out') + 1]).write_text(json.dumps({'gmail': {'status': 'ok', 'complete': True}}))
    text = pipeline.run(PACK, {'mode': 'current'}, command, authority=lambda: state.copy())['text']
    assert 'Fully lost touch' in text and 'Maya' in text
    assert 'skipping the pulse' not in text


def test_canonical_commitment_deadline_uses_owner_local_date(flow):
    root, monkeypatch, _, _, state, invoke = flow
    import loops_query
    owner_now = datetime.fromisoformat('2026-10-09T23:30:00-07:00')
    monkeypatch.setenv('SOTTO_TIMEZONE', '-07:00')
    def owner_clock(tz):
        assert tz == '-07:00'
        return owner_now
    monkeypatch.setattr(loops_query, '_now_local', owner_clock)
    _write_source_commitment(_canonical_writer(), owner_now, deadline='2026-10-09')
    state['google_sources'] = ['gmail']
    import relationship_pulse_pipeline as pipeline
    def command(script, *args, **kwargs):
        Path(args[args.index('--gmail-out') + 1]).write_text('[]')
        Path(args[args.index('--source-results-out') + 1]).write_text(json.dumps({'gmail': {'status': 'ok'}}))
    result = pipeline.run(PACK, {'mode': 'current'}, command, authority=lambda: state.copy())
    assert 'Morgan' in result['text']
    checkpoint = json.loads(next((root / 'events/research-artifacts').glob('pulse-*.json')).read_text())
    row = checkpoint['snapshot']['result']['loops']['you_owe'][0]
    assert row['overdue'] is False  # UTC is already Oct10, but the owner's date is still Oct9


def test_pulse_groups_existing_phone_and_email_graph_aliases_without_editing_loops(flow):
    root, _, _, _, state, _ = flow
    import relationship_pulse_pipeline as pipeline
    now = datetime.now(timezone.utc)
    writer = _canonical_writer()
    kg = writer._kg
    folder = Path(kg.people_dir())
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'c_abc123.md').write_text(kg.serialize_person_file(kg.PersonFile(
        name='Austin', canonical_id='c_abc123', identifiers=['+15555550123', 'austin@example.com'])))
    _write_source_commitment(writer, now, 'Austin', anchor_key='imessage:call',
        source='imessage', channel='imessage', contact_identifier='+15555550123',
        summary='Connect today or tomorrow about Project Cedar', deadline='Tonight')
    _write_source_commitment(writer, now, 'Austin', anchor_key='email:call',
        summary='Free for a call about Project Cedar following the intro', deadline='Tonight')
    state['google_sources'] = ['gmail']
    before = {path: path.read_bytes() for path in (root / 'knowledge/continuity').glob('*.md')}
    def command(script, *args, **kwargs):
        Path(args[args.index('--gmail-out') + 1]).write_text('[]')
        Path(args[args.index('--source-results-out') + 1]).write_text(json.dumps({'gmail': {'status': 'ok'}}))
    text = pipeline.run(PACK, {'mode': 'current'}, command, authority=lambda: state.copy())['text']
    assert text.count('Austin') == 1 and '; 1 other open item' in text
    assert 'imessage' not in text and 'gmail' not in text and 'due Tonight' not in text
    assert {path: path.read_bytes() for path in before} == before


def _brief_attention(monkeypatch, *, revoked=()):
    """The daily brief's real read of the shared queue, under the current source permission."""
    for part in ('_shared/lib', '_shared/scripts'):
        monkeypatch.syspath_prepend(str(PACK / part))
    import compose_brief
    import source_context
    from render_local import _format_attention_queue
    monkeypatch.setattr(source_context, 'allowed', lambda source: source not in revoked)
    return _format_attention_queue(compose_brief._normalize_local({'local': {}}))


def test_shared_state_keeps_no_message_text_after_a_pulse(flow):
    root, _, _, _, _, invoke = flow
    text = invoke()['text']
    assert 'Cedar deck' in text  # the attended response may quote the fresh request
    stored = (root / 'knowledge/relationship_state.json').read_text()
    assert 'Cedar' not in stored and 'excerpt' not in stored and '"text"' not in stored


def test_revoked_channel_suggestion_never_reaches_the_brief_prompt(flow):
    root, monkeypatch, _, _, _, invoke = flow
    invoke()
    assert 'Dana' in _brief_attention(monkeypatch)
    rendered = _brief_attention(monkeypatch, revoked={'imessage'})
    assert 'Dana' not in rendered and 'Cedar' not in rendered


def test_existing_stored_excerpts_are_ignored_and_scrubbed_on_next_write(flow):
    root, monkeypatch, _, _, _, invoke = flow
    now = datetime.now(timezone.utc)
    sent = (now - timedelta(days=5)).isoformat()
    path = root / 'knowledge/relationship_state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        'history': {'c_0ld0ld0ld0ld': {'name': 'Riley', 'last_contact': (now - timedelta(days=5)).date().isoformat(),
            'channels': {'imessage': (now - timedelta(days=5)).date().isoformat()}, 'last_from_them': sent,
            'latest_inbound': {'at': sent, 'channel': 'imessage', 'text': 'Could you wire the secret payroll?',
                               'subject': 'secret subject'}}},
        'attention_queue': [{'display_name': 'Riley', 'queue_type': 'waiting_on_you', 'reason': 'Possible reply',
            'context': {'channel': 'imessage', 'date': sent[:10], 'subject': 'secret subject',
                        'excerpt': 'Could you wire the secret payroll?'}}]}))
    assert 'secret' not in _brief_attention(monkeypatch)
    result = invoke()
    assert 'secret' not in result['text']
    assert 'secret' not in path.read_text()


def test_lapse_disclaimer_survives_open_commitments(flow):
    root, _, local, _, state, _ = flow
    import relationship_pulse_pipeline as pipeline
    _lapsed_history(root)
    local['source_status']['imessage'] = 'partial'
    state['google_sources'] = ['gmail']
    _write_source_commitment(_canonical_writer(), datetime.now(timezone.utc))
    def command(script, *args, **kwargs):
        Path(args[args.index('--gmail-out') + 1]).write_text('[]')
        Path(args[args.index('--source-results-out') + 1]).write_text(json.dumps({'gmail': {'status': 'ok'}}))
    text = pipeline.run(PACK, {'mode': 'current'}, command, authority=lambda: state.copy())['text']
    assert 'Morgan' in text and 'Fully lost touch' not in text
    assert 'Some history may be missing' in text


def test_bounded_bridge_read_never_rewrites_the_canonical_snapshot(flow):
    root, _, local, calls, _, invoke = flow
    now = datetime.now(timezone.utc)
    rows = [{'handle': '+1', 'resolved_name': 'Dana', 'timestamp': (now - timedelta(hours=3 + i)).isoformat(),
             'is_from_me': False, 'is_group_chat': False, 'text': f'brief row {i}'} for i in range(20)]
    snap = root / 'knowledge/last_local_snapshot.json'
    snap.parent.mkdir(parents=True, exist_ok=True)
    stamp = (now - timedelta(hours=2)).isoformat()
    snap.write_text(json.dumps({'captured_at': stamp, 'local': {'generated_at': stamp,
        'source_status': {'imessage': 'ok', 'contacts': 'ok'}, 'contacts': local['contacts'],
        'imessage': rows, '_source_observed_at': {'imessage': stamp, 'contacts': stamp}}}))
    before = snap.read_bytes()
    local['generated_at'] = now.isoformat()
    invoke()
    assert calls[0][1]['since_hours'] < 24  # a bounded read, narrower than the saved snapshot
    assert snap.read_bytes() == before
    assert not (root / 'knowledge/snapshots').exists()


@pytest.mark.parametrize('observed_request', [False, True])
def test_source_backed_birthday_request_matches_the_brief(flow, observed_request):
    root, _, local, _, _, invoke = flow
    import ledger_io
    writer = _canonical_writer()
    now = datetime.now(timezone.utc)
    summary = 'Send Dana happy birthday wishes from the team'
    if observed_request:
        quote = 'Please send Dana happy birthday wishes from the team.'
        local['contacts'].append({'name': 'Avery', 'phones': ['+15555550101']})
        local['imessage'].append({'rowid': 7, 'handle': '+15555550101', 'is_from_me': False,
            'timestamp': (now - timedelta(minutes=10)).isoformat(), 'text': quote})
        writer.resolve({'local': local, 'new_actions': [{'type': 'follow_up', 'channel': 'imessage',
            'contactName': 'Avery', 'contactIdentifier': '+15555550101', 'contextSummary': summary,
            'source': 'imessage', 'evidence': [{'sourceType': 'imessage', 'sourceId': '7', 'snippet': quote}]}]},
            now, resolve_existing=False)
    else:
        writer._persist({'status': 'open', 'action_type': 'follow_up', 'contact_identifier': '+15555550101',
            'source': 'imessage', 'channel': 'imessage', 'source_thread_id': 'thread-boss',
            'source_message_id': 'message-boss', 'created_at': now.isoformat(), 'anchor_key': 'imessage:boss',
            'contact_name': 'Avery', 'summary': summary})
    row = next(r for r in ledger_io.load_entries() if r.get('contact_name') == 'Avery')
    assert (row.get('resolution_mode') == 'explicit_birthday_request') is observed_request
    assert not ledger_io.is_unsourced_brief_birthday_courtesy(row)  # the brief keeps this observed request
    assert any(r.get('contact_name') == 'Avery' for r in ledger_io.load_active())
    from loops_query import query_permitted_rows
    entries = query_permitted_rows(str(root), ['imessage'], include_request_context=True)['you_owe']
    # A stored source-backed request (legacy rows carry no explicit mode) stays an ordinary task.
    assert any(r['name'] == 'Avery' and not r['birthday_courtesy'] for r in entries)
    text = invoke()['text']
    assert 'Avery' in text and summary in text and 'Follow-ups' in text
    assert 'Optional, no urgency' not in text and 'Waiting for them' not in text




def test_history_learning_note_ignores_sources_that_are_now_off(flow):
    root, _, _, _, _, invoke = flow
    now = datetime.now(timezone.utc)
    path = root / 'knowledge/relationship_state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'history': {'Dana': {'name': 'Dana', 'last_contact': now.date().isoformat(),
        'channels': {'imessage': now.date().isoformat()}}}}))
    (root / 'knowledge/history-state.json').write_text(json.dumps({'sources': {'whatsapp': {
        'initial_complete': True, 'complete': True}}}))
    assert 'Historical relationship learning is not yet available' in invoke()['text']


def test_scheduled_pulse_reads_real_canonical_commitment_with_bounded_refresh(flow):
    root, monkeypatch, local, bridge_calls, state, _ = flow
    import procedure_runner
    import relationship_pulse_pipeline as pipeline
    now = datetime.now(timezone.utc)
    _write_source_commitment(_canonical_writer(), now)
    state['google_sources'] = ['gmail']
    monkeypatch.setattr(pipeline.run, '__kwdefaults__', {'authority': lambda: state.copy()})
    calls = []
    from types import SimpleNamespace
    def command(argv, **kwargs):
        calls.append(argv)
        assert Path(argv[1]).name == 'gather_google.py'
        assert '--pulse-since-epoch' in argv and '--window-days' not in argv and '--sent-max' not in argv
        assert 0 < now.timestamp() - float(argv[argv.index('--pulse-since-epoch') + 1]) < 90000
        Path(argv[argv.index('--gmail-out') + 1]).write_text('[]')
        Path(argv[argv.index('--source-results-out') + 1]).write_text(json.dumps({'gmail': {'status': 'ok'}}))
        return SimpleNamespace(returncode=0, stdout='Saved 0 emails and 0 events\n', stderr='')
    monkeypatch.setattr(procedure_runner.subprocess, 'run', command)
    request = {'pack': str(PACK), 'kind': 'pulse'}
    text = procedure_runner.run(request)
    assert 'Morgan' in text and 'Send the Orion launch packet' in text and 'Follow-ups' in text
    before = (root / 'knowledge/relationship_state.json').read_bytes()
    # Restart/lost result receipt resumes this exact run without another gather/write.
    assert procedure_runner.run(request) == text
    assert (root / 'knowledge/relationship_state.json').read_bytes() == before
    assert len(calls) == 1 and bridge_calls == [('read_local', {'since_hours': 25})]
    effect = json.loads((root / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())['effects'][0]
    assert effect['sources'] == ['contacts', 'gmail', 'imessage']
    # A fresh run under a new grant cannot surface the now-unpermitted canonical row.
    state.update(revision='d' * 64, google_sources=[])
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'd' * 32)
    monkeypatch.setenv('SOTTO_SKILL_COMPOSITION_PROOF', json.dumps({'source_revision': 'd' * 64, 'route_digest': 'c' * 64}))
    assert 'Morgan' not in procedure_runner.run(request)
    assert len(calls) == 1


@pytest.mark.parametrize('excluded', [False, True])
def test_backfill_gap_still_withholds_pulse_absence_after_incremental_rollover(flow, excluded):
    root, monkeypatch, _, _, _, invoke = flow
    import memory_cycle
    from contextlib import nullcontext
    # No paid extraction in this fixture; exercise the real checkpoint and pulse paths.
    monkeypatch.setattr(memory_cycle.gemini_transport, 'background_learning', nullcontext)
    now = datetime.now(timezone.utc)
    since = int((now - timedelta(days=43)).timestamp())
    until = int((now - timedelta(hours=2)).timestamp())
    _lapsed_history(root)
    window = {'since': since, 'until': until, 'initial_since': since, 'initial_until': until,
              'initial_complete': True, 'complete': True, 'pages': 1, 'window_page_base': 0}
    if excluded:
        window['excluded_pages'] = {'blocked': {'since': since, 'until': until, 'reason': 'SAFETY'}}
    path = root / 'knowledge/history-state.json'
    path.write_text(json.dumps({'sources': {'imessage': window}}))
    monkeypatch.setattr(memory_cycle, 'allowed', lambda source: source == 'imessage')
    for target, method in ((memory_cycle.prewarm_graph, 'prewarm'),
                           (memory_cycle.style_extract, 'extract'),
                           (memory_cycle.relationship_pulse, 'observe_history')):
        monkeypatch.setattr(target, method, lambda *a, **k: None)
    memory_cycle.run(now, lambda *a: {'status': 'ok', 'rows': [], 'next_cursor': None, 'complete': True},
                     lambda *a, **k: {'reviewed': 0, 'facts': 0}, lambda **k: {'reviewed': 0})
    saved = json.loads(path.read_text())['sources']['imessage']
    assert saved['since'] == until and saved['complete'] is True
    assert bool(saved.get('excluded_pages')) == excluded
    invoke()
    checkpoint = json.loads((root / 'events/research-artifacts' / ('pulse-' + 'a'*32 + '.json')).read_text())
    assert ('imessage' in checkpoint['snapshot']['result']['complete_channels']) is not excluded
