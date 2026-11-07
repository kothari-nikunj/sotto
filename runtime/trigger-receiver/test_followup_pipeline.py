"""Original follow-up composition/write/readback and interrupted-stage holds."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PACK = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'


@pytest.fixture
def flow(tmp_path, monkeypatch):
    for part in ('_shared/lib', '_shared/scripts', '_shared/knowledge'):
        monkeypatch.syspath_prepend(str(PACK / part))
    import followup_pipeline as pipeline
    import gather_granola
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'a'*32)
    monkeypatch.setenv('SOTTO_SKILL_COMPOSITION_PROOF', json.dumps({'source_revision':'b'*64,'route_digest':'c'*64}))
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    calls = []
    meeting = {'meeting_id':'m1', 'title':'Planning', 'end':(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),
        'attendee_emails':['dana@example.com'], 'your_notes':'Owner will send the deck to Dana.',
        'transcript':'Owner will send the deck to Dana.'}
    def gather(days, hours, **kwargs):
        calls.append(('gather', days, hours))
        assert 'max_meetings' not in kwargs
        return [dict(meeting, meeting_id='m'+str(n)) for n in range(4)], []
    monkeypatch.setattr(gather_granola, 'gather_mcp_with_retry', gather)
    authority = lambda: {'complete':True,'revision':'b'*64,'connector_sources':['granola'],
                         'bridge_sources':[],'google_sources':[]}
    def llm(prompt, inputs):
        calls.append(('model',))
        assert len(inputs['granola']) == 4
        return json.dumps({'followup_markdown':'Captured the meeting follow-ups.', 'commitments':[],
            'drafts':[{'to_name':'Dana','to_email':'dana@example.com','channel':'email','subject':None,'body':'Here is the deck.'},
                      {'to_name':'Unknown','to_email':'invented@example.com','channel':None,'subject':None,'body':'Full second body.'}]})
    def invoke(mode='recent', **kwargs):
        return pipeline.run(PACK, {'mode':mode}, lambda *a,**k:pytest.fail('ungranted calendar'),
            authority=kwargs.pop('authority',authority), client_factory=lambda:None,
            llm=kwargs.pop('llm',llm))
    return pipeline, tmp_path, calls, invoke


def test_batch_windows_real_compose_apply_readback_drafts_and_restart(flow):
    _, root, calls, invoke = flow
    result = invoke()
    assert calls == [('gather',3,36),('model',)]
    assert result['ledger']['written'] == 0 and 'you_owe' in result['open_loops']
    assert 'Here is the deck.' in result['text'] and 'Full second body.' in result['text']
    assert 'invented@example.com' not in result['text']
    before = {p.name:p.read_bytes() for p in (root/'events/research-artifacts').glob('*.json')}
    assert invoke() == result and calls == [('gather',3,36),('model',)]
    assert before == {p.name:p.read_bytes() for p in (root/'events/research-artifacts').glob('*.json')}


def test_backlog_preserves_full_window_without_automatic_cap(flow):
    _,_,calls,invoke = flow
    result = invoke('backlog')
    assert calls[0] == ('gather',14,0)
    assert 'Open loops after capture:' in result['text']


def test_no_meeting_notes_produces_no_model_commitment_or_ledger_write(flow, monkeypatch):
    pipeline, root, calls, _ = flow
    import gather_granola
    monkeypatch.setattr(gather_granola, 'gather_mcp_with_retry',
                        lambda *args, **kwargs: ([], []))
    result = pipeline.run(PACK, {'mode': 'recent'},
        lambda *args, **kwargs: pytest.fail('no calendar grant'),
        authority=lambda: {'complete': True, 'revision': 'b'*64,
                           'connector_sources': ['granola'], 'bridge_sources': [],
                           'google_sources': []},
        llm=lambda *args, **kwargs: pytest.fail('no notes means no model extraction'))
    assert result['commitments'] == [] and result['drafts'] == []
    assert 'do not have meeting transcripts or notes' in result['text']
    assert not list((root / 'knowledge/continuity').glob('*.md'))


@pytest.mark.parametrize('channel,app', [('imessage', 'Messages'), ('sms', 'Messages'),
                                        ('whatsapp', 'WhatsApp')])
def test_known_local_followup_hands_over_text_without_email_fields_or_address_question(flow, monkeypatch, channel, app):
    import pending_offer
    _, _, _, invoke = flow
    offers = []
    monkeypatch.setattr(pending_offer, 'set_offer', lambda *args, **kwargs: offers.append((args, kwargs)))
    result = invoke(llm=lambda *_: json.dumps({
        'followup_markdown': 'Meeting follow-up.', 'commitments': [],
        'drafts': [{'to_name': 'Dana', 'to_email': None, 'channel': channel,
                    'subject': None, 'body': 'Here is the deck.'}]}))
    assert 'Draft for Dana (not sent):\nHere is the deck.' in result['text']
    assert f'Paste this into the existing conversation in {app}.' in result['text']
    assert 'Subject:' not in result['text'] and 'Destination:' not in result['text']
    assert 'Which channel and address' not in result['text'] and 'Gmail drafts' not in result['text']
    assert offers == []


def test_unknown_followup_channel_still_requests_destination(flow):
    _, _, _, invoke = flow
    result = invoke(llm=lambda *_: json.dumps({
        'followup_markdown': 'Meeting follow-up.', 'commitments': [],
        'drafts': [{'to_name': 'Dana', 'to_email': None, 'channel': None,
                    'subject': None, 'body': 'Here is the deck.'}]}))
    assert 'Which channel and address should this draft use?' in result['text']
    assert 'Paste this into' not in result['text']


def test_lost_model_result_holds_without_second_paid_contact(flow):
    _,_,calls,invoke = flow
    def lost(prompt, inputs):
        calls.append(('lost-model',))
        raise TimeoutError('synthetic lost response')
    with pytest.raises(TimeoutError):
        invoke(llm=lost)
    with pytest.raises(RuntimeError, match='unconfirmed'):
        invoke()
    assert calls == [('gather',3,36),('lost-model',)]


def test_changed_granola_generation_refuses_cached_output(flow):
    _,_,calls,invoke = flow
    invoke()
    with pytest.raises(RuntimeError, match='authority'):
        invoke(authority=lambda:{'complete':True,'revision':'d'*64,'connector_sources':['granola']})
    assert calls == [('gather',3,36),('model',)]


def test_real_canonical_apply_and_lost_receipt_do_not_repeat_write(flow, monkeypatch):
    _, root, calls, invoke = flow
    import jsonstore
    import yaml
    monkeypatch.setenv('SOTTO_USER_NAME', 'Owner')
    def extract(prompt, inputs):
        calls.append(('model',))
        return json.dumps({'followup_markdown':'Recorded your commitment.', 'drafts':[],
            'commitments':[{'meeting_id':'m0','meeting':'Planning','owner':'you','owner_is_user':True,
                'what':'send the deck','to_email':'dana@example.com',
                'source_snippet':'Owner will send the deck to Dana.'}]})
    write = jsonstore.write_atomic
    def lose_receipt(path, value, *args, **kwargs):
        if str(path).endswith('followup-'+'a'*32+'.json') and value.get('apply', {}).get('status') == 'completed':
            raise OSError('synthetic crash after canonical write')
        return write(path, value, *args, **kwargs)
    monkeypatch.setattr(jsonstore, 'write_atomic', lose_receipt)
    with pytest.raises(OSError):
        invoke(llm=extract)
    files = list((root/'knowledge/continuity').glob('*.md'))
    assert len(files) == 1
    content = files[0].read_bytes()
    fm = yaml.safe_load(content.decode().split('---')[1])
    assert fm['status'] == 'open' and fm['contact_identifier'] == 'dana@example.com'
    monkeypatch.setattr(jsonstore, 'write_atomic', write)
    with pytest.raises(RuntimeError, match='unconfirmed'):
        invoke(llm=extract)
    assert files[0].read_bytes() == content and calls == [('gather',3,36),('model',)]


def test_calendar_list_shape_and_complete_context_union(flow):
    pipeline, _, _, _ = flow
    def command(script, *args, **kwargs):
        Path(args[args.index('--cal-out')+1]).write_text(json.dumps([{'summary':'Planning'}]))
    result = pipeline.run(PACK, {'mode':'recent'}, command, client_factory=lambda:None,
        authority=lambda:{'complete':True,'revision':'b'*64,'connector_sources':['granola'],
                          'bridge_sources':[],'google_sources':['calendar','gmail']},
        llm=lambda prompt,inputs:json.dumps({'followup_markdown':'Follow-up', 'commitments':[], 'drafts':[]}))
    assert result['sources'] == ['calendar','gmail','granola']


@pytest.mark.parametrize('mode', ['recent','backlog'])
def test_original_mcp_notes_only_without_transcript_tool_proceeds(flow, monkeypatch, mode):
    pipeline, _, _, _ = flow
    import gather_granola
    class Mcp:
        def initialize(self):
            return {}
        def list_tools(self):
            return [{'name':'list_meetings'}]
        def call_tool(self, name, arguments=None, timeout=60):
            return [{'id':'notes-only','title':'Planning','start_time':(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat(),
                     'notes_markdown':'Owner will send the deck.'}]
    monkeypatch.setattr(gather_granola, 'gather_mcp_with_retry',
        lambda days,hours,**kwargs:gather_granola.gather_mcp(days,hours,client=Mcp()))
    calls=[]
    result=pipeline.run(PACK, {'mode':mode}, lambda *a,**k:pytest.fail('no calendar'),
        authority=lambda:{'complete':True,'revision':'b'*64,'connector_sources':['granola']},
        client_factory=lambda:Mcp(), llm=lambda prompt,inputs:(calls.append(prompt) or json.dumps(
            {'followup_markdown':'Notes-only follow-up.', 'commitments':[], 'drafts':[]})))
    assert 'Notes-only follow-up.' in result['text'] and len(calls)==1


@pytest.mark.parametrize('evidence,expected', [
    ('Please follow up with Dana at dana@example.com.', 'dana@example.com'),
    ('Please follow up with Dana at dana@example.com.evil.', 'not provided'),
])
def test_draft_email_evidence_sentence_punctuation_is_not_prefix_matching(flow, monkeypatch, evidence, expected):
    pipeline, _, _, _ = flow
    import gather_granola
    monkeypatch.setattr(gather_granola, 'gather_mcp_with_retry', lambda *a,**k:([
        {'meeting_id':'email-evidence','title':'Planning',
         'end':(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),
         'transcript':evidence}], []))
    result = pipeline.run(PACK, {'mode':'recent'}, lambda *a,**k:pytest.fail('no calendar'),
        client_factory=lambda:None,
        authority=lambda:{'complete':True,'revision':'b'*64,'connector_sources':['granola']},
        llm=lambda prompt,inputs:json.dumps({'followup_markdown':'Follow-up', 'commitments':[],
            'drafts':[{'to_name':'Dana','to_email':'dana@example.com','channel':'email',
                       'subject':None,'body':'Following up.'}]}))
    assert 'Destination: '+expected in result['text']
