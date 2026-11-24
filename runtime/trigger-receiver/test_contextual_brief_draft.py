from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

import brief_runner
from test_brief_runner import PACK, procedure
from test_requested_brief import rec
from test_runtime_owner_gmail_draft_tool import WHEN, context, followup_offer, request, setup

__all__ = ['context', 'procedure', 'rec', 'setup']


@pytest.fixture
def fixture(monkeypatch, tmp_path, request):
    shared = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff/_shared'
    for name in ('scripts', 'lib'):
        monkeypatch.syspath_prepend(str(shared / name))
    import brief_followup_offer as offer
    import pending_offer
    import delivery_effects
    import gather_google
    import jsonstore

    friday = datetime(2026, 11, 13, 20, tzinfo=timezone.utc)
    monday = datetime(2026, 11, 16, 17, tzinfo=timezone.utc)
    proof = {'complete': True, 'revision': 'a' * 64, 'google_sources': ['gmail'],
             'connector_sources': ['granola']}
    meeting = {'id': 'meeting-one', 'title': 'Harbor debrief', 'end': friday.isoformat(),
               'attendee_emails': ['sam@example.net']}
    quote = 'Casey: I will draft the Harbor note for Sam before Monday.'
    row = {'anchor_key': 'harbor-note', 'status': 'open', 'action_type': 'follow_up',
           'contact_identifier': 'sam@example.net', 'deadline': '2026-11-16',
           'ask': 'draft the Harbor note for Sam', 'source_refs': [
               {'source': 'granola', 'source_id': 'meeting-one', 'snippet': quote}]}
    extracted = {'ledger': {'recorded_anchor_keys': ['harbor-note']}, 'followup': {
        'drafts': [{'to_name': 'Sam Rivera', 'to_email': 'sam@example.net', 'channel': 'email',
                    'subject': 'Harbor note', 'body': 'Sam, here is the Harbor decision.',
                    'commitment_quote': quote}]}}
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'photon')
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', '1' * 32)
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', 'owner-chat')
    monkeypatch.setattr(pending_offer, '_now', lambda: monday)
    monkeypatch.setattr(offer, 'current_authority', lambda: proof)
    if not getattr(request, 'param', False):
        monkeypatch.setattr(offer, 'cached_followup', lambda _meeting, _email: extracted)
        monkeypatch.setattr(offer, 'load_protected_scope', lambda _root, _anchors: ([row], True))
        monkeypatch.setattr(offer, 'source_loop_sources', lambda _row: {'granola'})
    monkeypatch.setattr(offer, 'allowed', lambda source: source in {'gmail', 'granola'})
    monkeypatch.setattr(gather_google, '_find_google_api', lambda: 'fixture')
    monkeypatch.setattr(gather_google, '_search_gmail', lambda *_args: [])
    return offer, pending_offer, delivery_effects.stage, lambda run: jsonstore.read(delivery_effects._path(run)), monday, meeting, row, proof, extracted


@pytest.mark.parametrize('singleton', [False, True])
def test_friday_draft_offered_and_bound_to_accepted_monday_brief(fixture, singleton):
    offer, pending, _stage, load, monday, meeting, row, _proof, _extracted = fixture
    if singleton:
        row['source_refs'], = row['source_refs']
    brief = {'brief_text': 'Good morning.', 'brief_markdown': 'Good morning.',
             '_source_permissions': ['gmail', 'granola']}
    offer.append_offer(brief, [meeting], [], 'me@example.net', monday)
    assert 'Draft the Harbor note for Sam' in brief['brief_text']
    assert 'Harbor debrief notes · Fri, Nov 13' in brief['brief_text']
    assert 'Due today' in brief['brief_text']
    assert 'Review draft' in brief['brief_text'] and 'Hide' in brief['brief_text']
    assert 'Sam, here is the Harbor decision.' not in brief['brief_text']
    offer.stage_offer(brief)
    effect, = load('1' * 32)['effects']
    assert effect['kind'] == 'pending_offer'
    record = effect['offer']
    assert pending.get_offer(offer_id=record['offer_id']) == {}
    offer.delivery_effects.finalize([effect], {'target': 'photon', 'chat_id': 'owner-chat',
                                   'accepted_at': monday.timestamp(), 'message_id': 'brief-message'})
    shown = pending.get_offer(message_id='brief-message', target='photon', chat_id='owner-chat')
    assert json.loads(shown['detail'])['body'] == 'Sam, here is the Harbor decision.'
    assert offer.is_current(shown)
    assert pending.clear_offer(shown)
    even_later = {'brief_text': 'Good morning.', 'brief_markdown': 'Good morning.',
                  '_source_permissions': ['gmail', 'granola']}
    offer.append_offer(even_later, [meeting], [], 'me@example.net', monday)
    assert 'Review draft' not in even_later['brief_text']


@pytest.mark.parametrize('change', ['closed', 'closed_during_search', 'sent', 'expired', 'unchanged', 'uncertain', 'rejected'])
def test_queued_brief_preserves_body_and_delivery_identity_when_offer_stales(rec, fixture, monkeypatch, change):
    import gather_google
    offer, pending, _stage, load, monday, meeting, row, _proof, _extracted = fixture
    ready = change in ('uncertain', 'rejected')
    sent, emails, accepted_effects, invalidated = [], [], [], []
    brief = {'brief_text': 'Good morning.', 'brief_markdown': 'Good morning.'}
    offer.append_offer(brief, [meeting], [], 'me@example.net', monday)
    offer.stage_offer(brief)
    effect, = load('1' * 32)['effects']

    def send(body, _target, key, _attempt):
        sent.append((body, key))
        if change == 'uncertain':
            return False, 'fixture missing acknowledgement', {'acceptance': 'unknown'}
        if change == 'rejected':
            return False, 'fixture definite refusal', {'acceptance': 'rejected'}
        return True, '', {'message_id': 'brief-message'}

    box = rec.OUTBOX
    monkeypatch.setitem(box.HOOKS, 'send', send)
    monkeypatch.setitem(box.HOOKS, 'delivery_ready', lambda _payload: ready)
    monkeypatch.setitem(box.HOOKS, 'brief_gate', lambda *_args: 'send')
    monkeypatch.setitem(box.HOOKS, 'offer_route', lambda _payload: {})
    monkeypatch.setitem(box.HOOKS, 'gallery_receipt', lambda _key: None)
    monkeypatch.setitem(box.HOOKS, 'record', lambda *_args, **_kwargs: None)
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda payload: accepted_effects.extend(payload['effects']))
    monkeypatch.setitem(box.HOOKS, 'on_invalid', lambda payload: invalidated.append(payload))
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: pytest.fail('contextual offers use text delivery'))
    assert not rec._deliver_text(brief['brief_text'], 'cron:sotto-morning-brief',
                                 run_id='1' * 32, effects=[effect])
    if change in ('closed', 'uncertain', 'rejected'):
        row['status'] = 'resolved'
    elif change == 'sent':
        emails.append({'subject': 'Re: Harbor note', 'to': 'sam@example.net', 'date': monday.isoformat()})
    elif change == 'expired':
        monkeypatch.setattr(pending, '_now', lambda: monday + timedelta(days=1))
    def search(*_args):
        if change == 'closed_during_search':
            row['status'] = 'resolved'
        return emails

    monkeypatch.setattr(gather_google, '_search_gmail', search)
    with rec.CONNECTORS.json_transaction(box.path(), default={}) as store:
        store['rows'][0]['next_at'] = 0
    ready = True
    box.drain()
    keep = change in ('unchanged', 'uncertain', 'rejected')
    assert sent == [(brief['brief_text'] if keep else 'Good morning.', '1' * 32)]
    assert accepted_effects == ([effect] if change == 'unchanged' else [])
    assert [(old['run_id'], old['acceptance']) for old in invalidated] == (
        [('1' * 32, 'rejected')] if change == 'rejected' else [])


def test_monday_offer_withheld_for_saturday_send_outside_brief_window(fixture, monkeypatch):
    import gather_google
    offer, _pending, _stage, _load, monday, meeting, _row, _proof, _extracted = fixture
    saturday = monday - timedelta(days=2)
    searches = []

    def search(_api, query, _max_results, _bodies, _timeout):
        searches.append(query)
        return [{'to': 'sam@example.net', 'date': saturday.isoformat(), 'subject': 'Re: Harbor note'}]

    monkeypatch.setattr(gather_google, '_search_gmail', search)
    brief = {'brief_text': 'Good morning.', 'brief_markdown': 'Good morning.'}
    offer.append_offer(brief, [meeting], [], 'me@example.net', monday)
    offer.stage_offer(brief)
    assert searches == [f"in:sent to:sam@example.net after:{int(datetime.fromisoformat(meeting['end']).timestamp())}"]
    assert brief['brief_text'] == 'Good morning.'


@pytest.mark.parametrize(('receipt', 'attempted'), [
    ({'status': 'partial', 'complete': False}, True),
    ({'status': 'partial', 'error': 'rate_limited'}, False),
    ({'status': 'unavailable'}, False),
])
def test_brief_tries_targeted_draft_check_after_a_capped_gmail_gather(procedure, monkeypatch, receipt, attempted):
    monkeypatch.syspath_prepend(str(PACK / '_shared' / 'scripts'))
    import brief_followup_offer
    brief_request, state = procedure
    state['gmail'] = receipt
    candidates = []
    monkeypatch.setattr(brief_followup_offer, 'append_offer', lambda *_args: candidates.append(True))
    assert brief_runner.run(brief_request) == 'A useful brief'
    assert bool(candidates) is attempted


@pytest.mark.parametrize('fixture', [True], indirect=True)
def test_transcript_capture_reuses_real_ledger_and_stable_alias(fixture, monkeypatch):
    offer, _pending, _stage, _load, monday, meeting, row, _proof, extracted = fixture
    followup = Path(offer.__file__).resolve().parents[2] / 'followup/scripts'
    monkeypatch.syspath_prepend(str(followup))
    import capture_commitments as capture
    quote = row['source_refs'][0]['snippet']
    meeting.update(meeting_id=meeting['id'], start='2026-11-13T19:00:00+00:00', your_notes=quote)
    friday = datetime.fromisoformat(meeting['end']) + timedelta(hours=1)
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'me@example.net')
    monkeypatch.setenv('SOTTO_USER_NAME', 'Casey')
    monkeypatch.setattr(capture.apply_commitments.cr, '_now_local', lambda _tz: friday.replace(tzinfo=None))
    monkeypatch.setattr(capture.compose_followup, '_now_local', lambda _tz: friday)
    monkeypatch.setattr(capture.source_context, 'allowed', lambda source: source in {'gmail', 'granola'})
    monkeypatch.setattr(capture.compose_followup, 'compose', lambda *_args, **_kwargs: {
        **extracted['followup'], 'commitments': [{
            'meeting_id': meeting['id'], 'owner': 'Casey', 'owner_is_user': True,
            'what': row['ask'], 'due': row['deadline'], 'to_email': row['contact_identifier'],
            'source_snippet': quote}]})
    receipt = capture.extract_apply_meeting(dict(meeting, transcript=quote), 'me@example.net')
    assert receipt['ledger']['written'] == 1, receipt['ledger']
    assert receipt['meeting_revision'] != receipt['stable_meeting_revision']
    brief = {'brief_text': 'Good morning.', 'brief_markdown': 'Good morning.'}
    offer.append_offer(brief, [meeting], [], 'me@example.net', monday)
    assert 'Draft the Harbor note for Sam?' in brief['brief_text']


@pytest.mark.parametrize('case', ['sent', 'done', 'revoked', 'unbound', 'wrong_recipient'])
def test_no_offer_for_completed_or_unbound_work(fixture, case):
    offer, _pending, _stage, _load, monday, meeting, row, proof, extracted = fixture
    emails = []
    if case == 'sent':
        emails = [{'to': 'Sam Rivera <sam@example.net>', 'date': monday.isoformat(),
                   'subject': 'Re: Harbor note', 'isSent': True}]
    if case == 'done':
        row['status'] = 'resolved'
    if case == 'revoked':
        proof['connector_sources'] = []
    if case == 'unbound':
        extracted['followup']['drafts'][0]['commitment_quote'] = 'unsupported copy'
    if case == 'wrong_recipient':
        extracted['followup']['drafts'][0]['to_email'] = 'other@example.net'
    brief = {'brief_text': 'Good morning.', 'brief_markdown': 'Good morning.',
             '_source_permissions': ['gmail', 'granola']}
    offer.append_offer(brief, [meeting], emails, 'me@example.net', monday)
    assert brief['brief_text'] == 'Good morning.'

def test_contextual_review_uses_read_grant_and_rechecks_completion(setup, monkeypatch):
    scheduled = followup_offer(setup, monkeypatch)
    manual = followup_offer(setup, monkeypatch, suffix=' manual')
    import pending_offer
    import brief_followup_offer
    import gather_google
    scheduled['action'] = brief_followup_offer.ACTION
    scheduled['message_id'] = 'accepted-contextual-brief'
    scheduled['anchor_key'] = 'cedar-note'
    scheduled['observed_end'] = (WHEN - timedelta(days=1)).isoformat()
    row = {'status': 'open', 'action_type': 'follow_up', 'anchor_key': 'cedar-note',
           'contact_identifier': 'alice@example.com', 'ask': 'draft the Cedar follow-up'}
    scheduled['loop_version'] = brief_followup_offer.delivery_effects.loop_version(row)
    storage = Path(pending_offer._path())
    store = json.loads(storage.read_text())
    store['offers'] = [scheduled if item.get('offer_id') == scheduled['offer_id'] else item for item in store['offers']]
    storage.write_text(json.dumps(store))
    monkeypatch.setattr(brief_followup_offer, '_open_row', lambda _anchor: row)
    monkeypatch.setattr(gather_google, '_find_google_api', lambda: 'fixture')
    emails = []
    monkeypatch.setattr(gather_google, '_search_gmail', lambda *_args: emails)
    _, _, gmail, grants, call = setup
    grants['gmail_draft'] = False
    list_args = {'operation': 'list_followup', 'message_id': scheduled['message_id']}
    assert call(request(dict(list_args, message_id='old-brief'), 'preview', text='Review draft'))['status'] == 'no_match'
    listing = call(request(list_args, 'preview', text='Review draft'))
    only, = listing['offers']
    assert only['offer_id'] == scheduled['offer_id'] != manual['offer_id']
    args = {'operation': 'propose_followup', 'offer_id': scheduled['offer_id'], 'message_id': scheduled['message_id']}
    review = call(request(args, 'preview', text='Review draft'))
    assert review['review']['body'] == 'I will send the Cedar packet.'
    assert not gmail.created
    emails.append({'to': 'alice@example.com', 'subject': 'Re: Cedar follow-up',
                   'date': WHEN.isoformat()})
    assert call(request(list_args, 'preview', text='Review draft'))['status'] == 'no_match'
    assert call(request(args, 'preview', text='Review draft'))['status'] == 'no_match'
    assert not gmail.created
