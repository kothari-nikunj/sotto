"""Structured capture: what is owed (`deliverable`), who owes it (`owedBy`), and the direction check.

Fictional people and reserved example domains only. Reproduces three live symptoms: an ask stored
as the counterpart's FYI, a question the owner asked filed as the owner owing a reply, and an
owner promise that must stay owner-owes.
"""
from datetime import datetime, timezone
import os
import re

import pytest

import capture_shape
import compose_brief as cb
import continuity_resolve as cr
import knowledge as kg
import ledger_io
import loops_query
from ledger_provenance import classify_ledger_sources, digest_frontmatter, source_loop_sources

NOW = datetime(2026, 11, 10, 10, tzinfo=timezone.utc)
EMAIL = 'robin.vale@example.com'
PHONE = '+14155550142'
HERE = os.path.dirname(__file__)
GOLD_TASKS = 3   # the gold examples' task actions (Jordan, Avery, Morgan; more are allowed)
PROMPT = open(os.path.join(HERE, '..', 'morning-brief', 'references', 'extraction-prompt.md'),
              encoding='utf-8').read()


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-hosted')
    monkeypatch.delenv('SOTTO_PROACTIVE_CAPTURED_SOURCES', raising=False)
    people = tmp_path / 'knowledge/people'
    people.mkdir(parents=True)
    kg.write_person_file(str(people / 'c_rv0142.md'), kg.PersonFile(
        name='Robin Vale', canonical_id='c_rv0142', identifiers=[EMAIL, PHONE]))


def text(ident, body, from_me):
    return {'id': ident, 'handle': PHONE, 'timestamp': '2026-11-10T08:00:00Z',
            'is_from_me': from_me, 'text': body}


def action(kind, quote, source_id, **extra):
    return {'type': kind, 'channel': 'imessage', 'contactName': 'Robin Vale',
            'contactIdentifier': PHONE, 'contextSummary': '', 'helpKind': 'none',
            'contextDeadline': '', 'deadlineDate': '', 'newObligation': True,
            'evidence': [{'sourceType': 'imessage', 'sourceId': source_id, 'snippet': quote}], **extra}


def capture(message, candidate):
    return cr.resolve({'today': '2026-11-10', 'local': {'imessage': [message]},
                       'new_actions': [candidate]}, NOW)


FYI = 'FYI I am out of the country until Nov 22. Could you send the signed side letter before then?'


def fyi_capture(**extra):
    return capture(text('fyi-1', FYI, False), action(
        'reply', FYI, 'fyi-1', contextAsk='noting she is out of the country until Nov 22',
        deliverable='Send Robin the signed side letter', owedBy='owner', **extra))


def test_fyi_as_ask_shows_the_deliverable_on_every_owner_line():
    row, = fyi_capture()['active']
    assert row['deliverable'] == 'Send Robin the signed side letter'
    assert row['owed_by'] == 'owner'
    assert row['ask'] == 'noting she is out of the country until Nov 22'   # stored text is not rewritten
    owed = loops_query._query_rows([row], None, log_diagnostic=False, now=NOW)['you_owe']
    assert [e['what'] for e in owed] == ['Send Robin the signed side letter']
    assert 'Send Robin the signed side letter' in cb._open_loop_line([row], NOW)
    assert 'out of the country' not in cb._open_loop_line([row], NOW)


def test_provenance_and_digest_stay_valid_with_the_new_fields():
    fyi_capture()
    row, = cr._load_items().values()
    assert source_loop_sources(row) == {'imessage'}
    # A stamped source row binds the new fields in its digest, and a later carry keeps it valid.
    row['content_provenance'] = {'version': 1, 'kind': 'sources', 'sources': ['imessage'],
                                 'digest': digest_frontmatter(row)}
    cr._persist(row)
    assert classify_ledger_sources(cr._load_items()[row['anchor_key']]) == ('sources', frozenset({'imessage'}))
    fyi_capture(loopId=row['anchor_key'])
    kept = cr._load_items()[row['anchor_key']]
    assert kept['deliverable'] == 'Send Robin the signed side letter'
    assert classify_ledger_sources(kept) == ('sources', frozenset({'imessage'}))
    assert kept['content_provenance']['digest'] == digest_frontmatter(kept)


def test_owner_question_filed_as_owner_reply_is_corrected_to_waiting_on():
    asked = 'Can you send me the updated cap table by Friday?'
    result = capture(text('ask-1', asked, True), action(
        'reply', asked, 'ask-1', contextAsk='asked Robin for the updated cap table',
        deliverable='Send the updated cap table', owedBy='them'))
    row, = result['active']
    assert row['action_type'] == 'waiting_on' and ledger_io.is_waiting_on(row['action_type'])
    assert row['owed_by'] == 'them'
    assert result['capture_outcomes']['direction_corrected'] == 1
    view = loops_query._query_rows([row], None, log_diagnostic=False, now=NOW)
    assert [e['what'] for e in view['waiting_on_them']] == ['Send the updated cap table']


@pytest.mark.parametrize('owed', [None, 'them'])
def test_owner_request_typed_waiting_on_opens_through_the_existing_path(owed):
    asked = 'Can you send me the updated cap table by Friday?'
    extra = {'owedBy': owed} if owed else {}
    result = capture(text('ask-1', asked, True), action(
        'waiting_on', asked, 'ask-1', contextAsk='Send the updated cap table', **extra))
    row, = result['active']
    assert row['action_type'] == 'waiting_on'
    assert set(result['capture_outcomes']) == {'source_verified', 'source_rejected', 'quotes_normalized'}


def test_model_type_and_owed_by_disagreeing_is_withheld():
    asked = 'Can you send me the updated cap table by Friday?'
    result = capture(text('ask-1', asked, True), action(
        'waiting_on', asked, 'ask-1', contextAsk='asked Robin for the updated cap table', owedBy='owner'))
    assert result['active'] == [] and cr._load_items() == {}
    assert result['capture_outcomes']['direction_held'] == 1
    assert result['capture_outcomes']['source_verified'] == 1


def test_their_promise_with_owner_type_is_withheld_not_flipped():
    promised = "I'll send you the updated deck tomorrow."
    result = capture(text('promise-1', promised, False), action(
        'reply', promised, 'promise-1', contextAsk='said they will send the updated deck tomorrow',
        deliverable='Send the updated deck', owedBy='them'))
    assert result['active'] == [] and result['capture_outcomes']['direction_held'] == 1


@pytest.mark.parametrize('owner_at, robin_at, saved', [('08:00', '08:05', True), ('08:05', '08:00', False),
                                                       ('08:00', '08:00', False)])
def test_owner_request_with_their_acknowledgement_is_waiting_on_only_when_the_request_came_first(owner_at, robin_at, saved):
    asked, ack = 'Can you send me the updated cap table by Friday?', 'will do tomorrow'
    candidate = action('reply', asked, 'ask-1', contextAsk='asked Robin for the updated cap table',
                       deliverable='Send the updated cap table', owedBy='them')
    candidate['evidence'].append({'sourceType': 'imessage', 'sourceId': 'ack-1', 'snippet': ack})
    result = cr.resolve({'today': '2026-11-10', 'new_actions': [candidate], 'local': {'imessage': [
        {**text('ask-1', asked, True), 'timestamp': f'2026-11-10T{owner_at}:00Z'},
        {**text('ack-1', ack, False), 'timestamp': f'2026-11-10T{robin_at}:00Z'}]}}, NOW)
    if saved:
        row, = result['active']
        assert row['action_type'] == 'waiting_on' and row['owed_by'] == 'them'
        assert result['capture_outcomes']['direction_corrected'] == 1
    else:
        assert result['active'] == [] and result['capture_outcomes']['direction_held'] == 1


@pytest.mark.parametrize('kind, owed', [('follow_up', 'owner'), ('reply', 'owner'), ('reply', None)])
def test_owner_promise_stays_owner_owes(kind, owed):
    # An owner-sent message without `owedBy: them` may be a promise: it is never flipped or held.
    promised = "I'll send the deck Friday."
    extra = {'owedBy': owed} if owed else {}
    result = capture(text('mine-1', promised, True), action(
        kind, promised, 'mine-1', contextAsk='Send the deck Friday', deliverable='Send Robin the deck', **extra))
    row, = result['active']
    assert row['action_type'] == kind and not ledger_io.is_waiting_on(row['action_type'])
    assert 'direction_held' not in result['capture_outcomes']
    assert 'direction_corrected' not in result['capture_outcomes']


def test_counterpart_request_with_owner_type_and_no_owed_by_is_unchanged():
    asked = 'Could you send the signed side letter this week?'
    result = capture(text('req-1', asked, False), action(
        'reply', asked, 'req-1', contextAsk='asked you to send the signed side letter this week'))
    row, = result['active']
    assert row['action_type'] == 'reply' and 'deliverable' not in row and 'owed_by' not in row
    assert result['capture_outcomes'] == {'source_verified': 1, 'source_rejected': 0, 'quotes_normalized': 0}


def test_legacy_row_displays_exactly_as_before():
    legacy = {'anchor_key': 'legacy', 'contact_name': 'Robin Vale', 'action_type': 'reply',
              'status': 'open', 'summary': 'Robin is travelling', 'ask': 'noting she is away',
              'created_at': '2026-11-01'}
    assert ledger_io.task_label(legacy) == 'noting she is away'
    assert ledger_io.deliverable(legacy) == ''
    line = cb._open_loop_line([legacy], NOW)
    assert 'Robin is travelling' in line


def test_recapture_never_rewrites_an_existing_rows_text():
    asked = 'Could you send the signed side letter this week?'
    first = capture(text('req-1', asked, False), action('reply', asked, 'req-1', contextAsk='asked for the side letter'))
    row, = first['active']
    again = capture(text('req-1', asked, False), {**action('reply', asked, 'req-1', contextAsk='asked for the side letter'),
                    'loopId': row['anchor_key'], 'newObligation': False,
                    'deliverable': 'Send the signed side letter', 'owedBy': 'owner'})
    kept, = again['active']
    assert 'deliverable' not in kept and kept['ask'] == row['ask']


def closed_fyi_row():
    row, = fyi_capture()['active']
    cr._terminate(row, 'resolved', 'user_resolved', '2026-11-10', now=datetime(2026, 11, 10, 8, 30, tzinfo=timezone.utc))
    cr._persist(row)
    return row


@pytest.mark.parametrize('extra, shown', [
    ({}, 'asked you to review the deck'),
    ({'deliverable': 'Review the deck and send feedback', 'owedBy': 'owner'}, 'Review the deck and send feedback'),
    ({'deliverable': 'Review the deck and send feedback', 'owedBy': 'them'}, 'asked you to review the deck')])
def test_reopen_for_a_new_ask_never_shows_the_old_deliverable(extra, shown):
    closed = closed_fyi_row()
    deck = 'Can you review the deck?'
    result = capture({**text('deck-1', deck, False), 'timestamp': '2026-11-10T09:00:00Z'}, {
        **action('reply', deck, 'deck-1', contextAsk='asked you to review the deck', **extra),
        'loopId': closed['anchor_key'], 'newObligation': False})
    row, = result['active']
    assert row['anchor_key'] == closed['anchor_key'] and row['reopened_at'] == '2026-11-10'
    assert row['ask'] == 'asked you to review the deck'
    assert row.get('deliverable') == (shown if shown == extra.get('deliverable') else None)
    assert ledger_io.task_label(row) == shown
    assert 'side letter' not in cb._open_loop_line([row], NOW)
    assert ('owed_by' in row) == (extra.get('owedBy') == 'owner')


def test_folding_a_duplicate_takes_the_newer_rows_deliverable_with_its_text():
    keeper = {'anchor_key': 'old', 'created_at': '2026-11-01', 'summary': 'Robin is away', 'ask': 'noting she is away',
              'deliverable': 'Send Robin the signed side letter', 'owed_by': 'owner', 'action_type': 'reply'}
    newer = {'anchor_key': 'new', 'created_at': '2026-11-08', 'summary': '', 'ask': 'asked you to review the deck',
             'action_type': 'reply'}
    cr._fold_duplicate(keeper, newer, '2026-11-10')
    assert ledger_io.task_label(keeper) == 'asked you to review the deck' and 'owed_by' not in keeper
    structured = {**newer, 'deliverable': 'Review the deck', 'owed_by': 'owner'}
    cr._fold_duplicate(keeper, structured, '2026-11-10')
    assert keeper['deliverable'] == 'Review the deck' and keeper['owed_by'] == 'owner'


@pytest.mark.parametrize('cited', ['ask-1', 'never-seen', None])
def test_first_capture_reply_owed_by_them_is_waiting_on_with_evidence_and_held_without(cited):
    # Not marked newObligation: bound evidence runs the same flip; no bound evidence holds the
    # capture instead of saving the owner's own request as a task the owner owes.
    asked = 'Can you send me the updated cap table by Friday?'
    candidate = {**action('reply', asked, cited or 'ask-1', contextAsk='asked Robin for the updated cap table',
                          deliverable='Send the updated cap table', owedBy='them'), 'newObligation': False}
    if cited is None:
        candidate.pop('evidence')
    result = capture(text('ask-1', asked, True), candidate)
    if cited == 'ask-1':
        row, = result['active']
        assert row['action_type'] == 'waiting_on' and row['owed_by'] == 'them'
        assert row['deliverable'] == 'Send the updated cap table'
        assert datetime.fromisoformat(row['created_at']) == NOW.replace(hour=8)
        assert result['capture_outcomes']['direction_corrected'] == 1
        view = loops_query._query_rows([row], None, log_diagnostic=False, now=NOW)
        assert view['you_owe'] == [] and [e['what'] for e in view['waiting_on_them']] == ['Send the updated cap table']
    else:
        assert result['active'] == [] and cr._load_items() == {}
        assert result['capture_outcomes']['direction_held'] == 1


def test_first_capture_hold_leaves_an_existing_reply_row_alone():
    asked = 'Can you send me the updated cap table by Friday?'
    plain = {**action('reply', asked, 'ask-1', contextAsk='asked Robin for the updated cap table'), 'newObligation': False}
    capture(text('ask-1', asked, True), plain)
    result = capture(text('ask-1', asked, True), {**plain, 'owedBy': 'them'})
    row, = result['active']
    assert row['action_type'] == 'reply' and 'direction_held' not in result['capture_outcomes']


@pytest.mark.parametrize('channel', ['gmail', 'imessage', 'whatsapp'])
@pytest.mark.parametrize('source_id, quote, source_direction, captured', [
    ('promise-1', "I'll send the updated deck tomorrow.", False, True),
    ('promise-1', 'Can you send me the updated deck?', True, True),
    ('made-up', "I'll send the updated deck tomorrow.", False, False),
    ('promise-1', 'A fabricated quote.', False, False),
    (None, '', False, False),
])
def test_first_waiting_on_capture_binds_message_evidence(channel, source_id, quote, source_direction, captured):
    original = ('Can you send me the updated deck?' if source_direction else
                "I'll send the updated deck tomorrow.")
    candidate = action('waiting_on', quote, source_id, contextAsk='Send the updated deck',
                       deliverable='Send the updated deck', owedBy='them', newObligation=False,
                       channel=channel, contactIdentifier=EMAIL if channel == 'gmail' else PHONE)
    if source_id:
        candidate['evidence'][0]['sourceType'] = channel
    else:
        candidate.pop('evidence')
    if channel == 'gmail':
        other, owner = EMAIL, 'owner@example.com'
        sender, recipient = (owner, other) if source_direction else (other, owner)
        local = {'emails': [{'id': 'promise-1', 'from': sender, 'to': recipient, 'body': original,
                             'date': '2026-11-10T08:00:00Z', 'isSent': source_direction}]}
    else:
        local = {channel: [text('promise-1', original, source_direction)]}
    result = cr.resolve({'today': '2026-11-10', 'new_actions': [candidate], 'local': local},
                        NOW, resolve_existing=False)
    if captured:
        row, = result['active']
        assert row['action_type'] == 'waiting_on'
        assert row['deliverable'] == 'Send the updated deck'
        assert datetime.fromisoformat(row['created_at']) == NOW.replace(hour=8)
    else:
        assert result['active'] == [] and cr._load_items() == {}
        assert result['capture_outcomes']['source_rejected'] == 1


def test_waiting_on_with_a_citation_cannot_bypass_binding_by_omitting_structured_fields():
    promise = "I'll send the updated deck tomorrow."
    proposal = action('waiting_on', promise, 'made-up', contextAsk='Send the updated deck', newObligation=False)
    result = capture(text('promise-1', promise, False), proposal)
    assert result['active'] == [] and result['capture_outcomes']['source_rejected'] == 1


def test_existing_waiting_on_capture_survives_an_unavailable_message_read():
    promise = "I'll send the updated deck tomorrow."
    proposal = action('waiting_on', promise, 'promise-1', contextAsk='Send the updated deck',
                      deliverable='Send the updated deck', owedBy='them', newObligation=False)
    first, = capture(text('promise-1', promise, False), proposal)['active']
    later = {**proposal, 'loopId': first['anchor_key'], 'contextAsk': 'New phrasing'}
    result = cr.resolve({'today': '2026-11-10', 'new_actions': [later]}, NOW, resolve_existing=False)
    row, = result['active']
    assert row['anchor_key'] == first['anchor_key']
    assert row['ask'] == first['ask'] and row['source_refs'] == first['source_refs']
    assert result['capture_outcomes']['source_rejected'] == 0


@pytest.fixture
def admitted_stale_sent(tmp_path, monkeypatch):
    from datetime import timedelta
    import json
    import gather_google as gg
    now = datetime.now(timezone.utc)
    sent_at = str(int((now - timedelta(days=4)).timestamp() * 1000))
    asked = 'Could you send the signed term sheet by Friday?'

    def message(ident, thread, subject, label, recipient):
        name, address = recipient
        return {'id': ident, 'threadId': thread, 'internalDate': sent_at, 'labelIds': [label], 'snippet': asked,
                'payload': {'headers': [{'name': name, 'value': address}, {'name': 'Subject', 'value': subject}]}}

    sent = message('stale-1', 't1', 'Terms', 'SENT', ('Cc', 'dan@example.com'))
    incoming = message('inbound-only-1', 't1', 'Terms', 'INBOX', ('To', EMAIL))
    excluded = [
        message('stranger-1', 't2', 'Private stranger topic', 'SENT', ('To', 'stranger@example.com')),
        message('rsvp-1', 't3', 'Accepted: Private calendar RSVP @ Friday', 'SENT', ('To', EMAIL)),
    ]
    threads = {'t1': {'messages': [incoming, sent]}, **{msg['threadId']: {'messages': [msg]} for msg in excluded}}
    stale = gg.GmailRows(gg.stale_from_threads(
        [{'threadId': 't1', 'to': EMAIL}, {'threadId': 't2'}, {'threadId': 't3'}], threads, now))
    monkeypatch.setattr(gg, '_run', lambda api, args, timeout=60: [] if args[:2] == ['gmail', 'search'] else {})
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(gg, '_refresh_google_token', lambda: None)
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: [])
    monkeypatch.setattr(gg, 'gather_stale_sent', lambda api: stale)
    out = tmp_path / 'g.json'
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--bodies', '0', '--gmail-out', str(out),
                                     '--cal-out', str(tmp_path / 'c.json')])
    gg.main()
    google = json.loads(out.read_text())
    assert google['emails'] == []
    local = {'contacts': [{'name': 'Dan', 'emails': ['dan@example.com']},
                          {'name': 'Robin Vale', 'emails': [EMAIL]}]}
    (tmp_path / 'local.json').write_text(json.dumps(local))
    stub = tmp_path / 'brief-model.json'
    stub.write_text(json.dumps({'brief_markdown': '# Brief', 'actions': []}))
    monkeypatch.setenv('SOTTO_LLM_STUB', str(stub))
    effective = tmp_path / 'effective-local.json'
    monkeypatch.setattr('sys.argv', ['compose_brief.py', '--gmail', str(out), '--local',
                                     str(tmp_path / 'local.json'), '--local-resolved',
                                     '--effective-local-out', str(effective)])
    cb.main()
    admitted_local = json.loads(effective.read_text())
    return google, local, admitted_local, asked, now


def test_stale_evidence_uses_only_the_selected_sent_row_and_its_native_recipients(admitted_stale_sent, monkeypatch):
    from stale_sent import stale_waiting_on_evidence
    google, local, admitted_local, asked, now = admitted_stale_sent
    inputs = {'type': 'morning', 'local': local,
              'google': {'emails': google['emails'], 'staleThreads': google['stale_threads']}}
    prompt = cb.build_prompt(cb._load_prompt(), inputs)
    assert 'stale-1' in prompt
    for excluded_value in ('inbound-only-1', 'stranger-1', 'rsvp-1', 'Private stranger topic', 'Private calendar RSVP'):
        assert excluded_value not in prompt
    sent_rows = admitted_local['stale_sent']
    row, = sent_rows
    assert (row['id'], row['isSent'], row['to'], row['cc'], row['snippet']) == (
        'stale-1', True, '', 'dan@example.com', asked)

    import source_context
    allowed = source_context.allowed
    for address, revoked in [(EMAIL, ''), ('dan@example.com', 'gmail'), ('dan@example.com', 'contacts'),
                             ('dan@example.com', '')]:
        monkeypatch.setattr(source_context, 'allowed', lambda source: source != revoked and allowed(source))
        candidate = {**action('waiting_on', asked, 'stale-1', contextAsk='Send the signed term sheet'),
                     'channel': 'email', 'contactIdentifier': address}
        candidate['evidence'][0]['sourceType'] = 'email'
        result = cr.resolve({'today': now.strftime('%Y-%m-%d'), 'local': admitted_local,
                             'new_actions': [candidate]}, now)
        assert len(result['active']) == (address == 'dan@example.com' and not revoked)
        if result['active']:
            assert result['active'][0]['action_type'] == 'waiting_on'
            assert result['capture_outcomes']['source_verified'] == 1

    fresh = dict(row, body='Rich body for the same source message')
    assert stale_waiting_on_evidence({**google, 'emails': [fresh]}, local) == []


def test_owner_owed_follow_up_may_cite_the_stale_thread_it_was_shown(admitted_stale_sent):
    """Stale rows bind only a waiting-on, so an owner-owed follow-up citing one is kept, not withheld."""
    _google, _local, admitted_local, asked, now = admitted_stale_sent
    candidate = {**action('follow_up_stale', asked, 'stale-1', contextAsk='Chase the signed term sheet',
                          owedBy='owner', newObligation=False), 'channel': 'email',
                 'contactIdentifier': 'dan@example.com'}
    candidate['evidence'][0]['sourceType'] = 'email'
    result = cr.resolve({'today': now.strftime('%Y-%m-%d'), 'local': admitted_local,
                         'new_actions': [candidate]}, now)
    assert len(result['active']) == 1 and result['capture_outcomes']['source_rejected'] == 0


def test_stale_email_is_critic_evidence_for_waiting_on_only(admitted_stale_sent):
    google, local, _admitted, asked, _now = admitted_stale_sent
    sources = cb._review_message_sources({'local': local, 'google': google})
    action_candidate = action('waiting_on', asked, 'stale-1', contactIdentifier='dan@example.com')
    ref, = action_candidate['evidence']
    ref['sourceType'] = 'email'
    assert cb._review_source_evidence(ref, sources, 500) is None
    evidence = cb._critic_action_evidence(action_candidate, sources)['evidence'][0]['observed_source']
    assert evidence['direction'] == 'sent_by_user' and evidence['quote_matches_source']
    other = cb._critic_action_evidence({**action_candidate, 'contactIdentifier': EMAIL}, sources)
    assert other['evidence'][0]['observed_source']['status'] == 'unavailable'


def test_stale_message_missing_metadata_remains_context_only():
    from datetime import timedelta
    import gather_google as gg
    row = {'id': 'old-mail', 'internalDate': str(int((NOW - timedelta(days=4)).timestamp() * 1000)),
           'labelIds': ['SENT'], 'snippet': 'Could you send the signed term sheet by Friday?'}
    stale, = gg.stale_from_threads([{'threadId': 't1', 'to': EMAIL}], {'t1': {'messages': [row]}}, NOW)
    assert stale['toEmail'] == EMAIL
    assert 'sent_message' not in stale


def test_trusted_task_read_shows_the_deliverable_over_the_summary():
    row = {**fyi_capture()['active'][0], 'summary': 'Robin is travelling until Nov 22'}
    view = loops_query._query_rows([row], None, log_diagnostic=False, now=NOW, include_ask=True)['you_owe'][0]
    assert view['what'] == 'Send Robin the signed side letter'
    assert view['ask'] == 'noting she is out of the country until Nov 22'


def test_still_open_line_is_attributed_to_its_own_deliverable_row():
    row, = fyi_capture()['active']
    assert cb._represented_loop_rows([row], cb._open_loop_line([row], NOW)) == [row]
    assert cb._loop_ask({'deliverable': 'Send Robin the signed side letter'}) == 'Send Robin the signed side letter'


def test_loop_view_fields_stay_inside_the_strict_chat_schema():
    legacy, = capture(text('req-1', 'Could you send the signed side letter this week?', False), action(
        'reply', 'Could you send the signed side letter this week?', 'req-1', contextAsk='asked for the side letter'))['active']
    structured = {**legacy, 'deliverable': 'Send the signed side letter', 'owed_by': 'owner'}
    view = lambda row: loops_query._query_rows([row], None, log_diagnostic=False, now=NOW)['you_owe'][0]
    assert set(view(structured)) == set(view(legacy))


def test_unit_rules():
    outcomes = {'source_verified': 0}
    assert not capture_shape.held({'action_type': 'follow_up', capture_shape.SENT_KEY: [(NOW, True)]}, outcomes, [])
    assert not capture_shape.held({'action_type': 'waiting_on', 'owed_by': 'them', capture_shape.SENT_KEY: [(NOW, True)]}, outcomes, [])
    assert not capture_shape.held({'action_type': 'reply', capture_shape.SENT_KEY: [(NOW, True), (NOW, False)]}, outcomes, [])
    # An unknown message direction never flips; the disagreement is withheld instead.
    assert capture_shape.held({'action_type': 'reply', 'owed_by': 'them', capture_shape.SENT_KEY: [(NOW, None)]}, outcomes, [])
    assert not capture_shape.held({'action_type': 'reply', capture_shape.SENT_KEY: [(NOW, True)]}, outcomes, [])
    assert capture_shape.held({'action_type': 'waiting_on', 'owed_by': 'owner', capture_shape.SENT_KEY: [(NOW, False)]}, outcomes, [])
    assert capture_shape.fields({'deliverable': '  Send  the deck ', 'owedBy': 'counterpart'}.get) == {
        'fulfillment': None, 'deliver_to': None, 'deliverable': 'Send the deck', 'owed_by': None}
    assert capture_shape.stored({'action_type': 'reply', 'owed_by': 'them', 'deliverable': 'x'}) == {}


# ── Prompt and schema: one contextAsk definition, structured fields declared ─────────────────────

def test_context_ask_has_one_definition():
    assert PROMPT.count('- contextAsk:') == 1
    definition = PROMPT[PROMPT.index('- contextAsk:'):].split('\n- ', 1)[0]
    assert 'never what is owed' in definition and '`deliverable`' in definition
    schema = cb._ACTION_ITEM_SCHEMA['properties']
    assert 'deliverable' in schema['contextAsk']['description']


def test_structured_fields_are_declared_and_defined_once():
    schema = cb._ACTION_ITEM_SCHEMA['properties']
    assert schema['owedBy']['enum'] == list(capture_shape.OWED_BY)
    assert schema['deliverable']['type'] == 'string'
    assert PROMPT.count('- deliverable:') == 1 and PROMPT.count('- owedBy:') == 1
    mapping = PROMPT[PROMPT.index('Action type mapping'):PROMPT.index('What fulfills it')]
    assert mapping.index('"waiting_on"') < mapping.index('"reply"')


def test_gold_examples_store_what_is_owed_not_their_statement():
    examples = PROMPT[PROMPT.index('### Representative "actionItems" entries'):PROMPT.index('### Example "extractedKnowledge"')]
    deliverables = re.findall(r'"deliverable": "([^"]*)"', examples)
    assert len(deliverables) >= GOLD_TASKS and all(deliverables)
    assert examples.count('"owedBy"') >= GOLD_TASKS



@pytest.mark.parametrize('wording', ['is noting she is out of the country until Nov 22',
                                     'needs you to send Robin the signed side letter'])
def test_a_line_showing_either_text_represents_the_row_and_adds_no_second_line(wording):
    row, = fyi_capture()['active']
    line = f"- **Robin Vale** {wording} <!--loop:{row['anchor_key']}-->"
    assert cb._represented_loop_rows([row], line) == [row]
    assert cb._line_open_task_keys(line, [row], '2026-11-10') == {row['anchor_key']}
    brief = '## Needs Attention Now\n' + line + '\n\n## Filtered\n- Nothing else.'
    shown = cb._append_still_open({'brief_markdown': brief}, [row], now=NOW, today='2026-11-10')
    assert 'open loop' not in shown['brief_markdown'] and 'Still open' not in shown['brief_markdown']
    silent = cb._append_still_open({'brief_markdown': brief.replace(line, '- Quiet day.')}, [row], now=NOW, today='2026-11-10')
    assert 'open loop' in silent['brief_markdown'] or 'Still open' in silent['brief_markdown']


def test_dedupe_recognizes_the_displayed_deliverable():
    row = {"anchor_key": "task-1", "status": "open", "action_type": "reply",
           "contact_name": "Robin Vale", "contact_identifier": "robin@example.com",
           "summary": "Robin is traveling this week",
           "ask": "Asked about the signing before departure",
           "deliverable": "Send the signed side letter"}
    item = "- Robin Vale: Send the signed side letter <!--loop:task-1-->\n"
    first = "**Needs you**\n" + item
    brief = first + "\n**Still open**\n" + item
    rendered = cb._dedupe_open_task_lines(brief, [row])
    assert rendered.count(item) == 1


def test_company_example_uses_the_domain_from_the_cited_email():
    import json

    example = PROMPT.split('### Example "extractedKnowledge" field:', 1)[1]
    knowledge, _ = json.JSONDecoder().raw_decode(example.lstrip())
    founder, = knowledge['person_updates']
    company, = knowledge['company_updates']
    assert company['domain'] == founder['identifier'].rsplit('@', 1)[1]


def test_domain_rule_example_uses_the_domain_of_its_email():
    match = re.search(r'`([^`]+@[^`]+)` about Northstar Labs → `"domain": "([^"]+)"`', PROMPT)
    assert match is not None
    email, domain = match.groups()
    assert domain == email.rsplit('@', 1)[1]
