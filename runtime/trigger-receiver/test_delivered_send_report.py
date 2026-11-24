"""Check delivery-bound send reports through the admitted owner writer."""
import pytest

from test_delivered_task_completion import actual_writer_module, call, context, conversation, rows

__all__ = ['actual_writer_module', 'context']


@pytest.mark.parametrize('other', ['Bill Smith', 'Aman Example'])
@pytest.mark.parametrize(('owner_text', 'closed'), [('I sent the bill', {'cleanup-3'}), ('I sent Bill', set())])
def test_sent_object_can_share_a_word_with_an_unrelated_visible_name(context, other, owner_text, closed):
    tasks = [('Bill Smith', 'Review the party spreadsheet'), (other, 'Book the venue'),
             ('Tina Example', 'Send the bill'), ('Will', 'Send the budget')]
    text = 'Bill Smith — Review the party spreadsheet.\nTina Example — Send the bill.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply(owner_text, 'bill'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == closed


@pytest.mark.parametrize(('owner_text', 'closed'), [
    ('Sent bill', set()), ('I sent Bill', set()), ('I sent the bill', {'cleanup-3'}),
])
def test_articleless_sent_reply_cannot_choose_between_a_recipient_and_an_object(context, owner_text, closed):
    tasks = [('Bill Smith', 'Send the deck'), ('Aman Example', 'Book the venue'),
             ('Tina Example', 'Send the bill'), ('Will', 'Send the file')]
    text = 'Bill Smith — Send the deck.\nTina Example — Send the bill.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply(owner_text, 'bill'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == closed


@pytest.mark.parametrize(('second_ask', 'rest', 'closed'), [
    ('Book a venue', '.', set()),
    ('Send the bill to accounting', '.\nTina Example — The bill to accounting is overdue.', set()),
    ('Send Bill Smith the deck', '.\nTina Example — Send Bill Smith the deck.', set()),
    ('Book a venue', '.\nAnother meeting starts after lunch.', set()),
    ('Book a venue', ', and the newsletter is out today.', set()),
])
def test_person_only_send_reply_does_not_confirm_what_was_sent(context, second_ask, rest, closed):
    tasks = [('Bill Smith', 'Send the contract'), ('Tina Example', second_ask),
             ('Aman Example', 'Review the deck'), ('Will', 'Send the budget')]
    reply, _ = conversation(context, 'brief', brief_case=(tasks, 'Bill Smith — Send the contract' + rest))
    call(reply('I sent Bill', 'Bill'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == closed


@pytest.mark.parametrize(('ask', 'text', 'selection', 'closed'), [
    ('Send the contract to Tina Example', 'I sent Bill', 'Bill', set()),
    ('Send the contract to accounting', 'I sent Bill', 'Bill', set()),
    ('Send Priya the contract', 'I sent Bill', 'Bill', set()),
    ('Send Priya the contract', 'I sent Priya', 'Priya', set()),
    ('Send the deck', 'I sent the deck', 'deck', {'cleanup-1'}),
    ('Send the bill', 'I sent the bill', 'bill', {'cleanup-1'}),
    ('Send the deck to Bill Smith', 'I sent the bill', 'bill', set()),
    ('Send the bill', 'I sent Bill', 'Bill', set()),
    ('Send the deck', 'Close Bill deck', 'Bill deck', {'cleanup-1'}),
    ('Send Priya the contract', 'I sent the contract', 'contract', set()),
    ('Send Priya the contract', 'Close contract', 'contract', {'cleanup-1'}),
    ('Send comments on the deck', 'I sent the deck', 'deck', set()),
])
def test_send_reports_bind_the_object_independently_of_the_task_owner(context, ask, text, selection, closed):
    tasks = [('Bill Smith', ask), ('Tina Example', 'Review the party spreadsheet'),
             ('Aman Example', 'Book a venue'), ('Will', 'Send the file')]
    reply, _ = conversation(context, 'brief', brief_case=(tasks, f'Bill Smith — {ask}.'))
    call(reply(text, selection), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == closed


@pytest.mark.parametrize(('continuation', 'closed'), [
    ('Sarah asked you to send the deck.', set()),
    ('Sarah asked you to send the deck. Send the deck.', set()),
    ('Send the deck.', {'cleanup-3'}),
    ('Send the deck before the call.', {'cleanup-3'}),
])
def test_new_sentence_only_uses_heading_when_it_continues_the_saved_task(context, continuation, closed):
    tasks = [('Aman Example', 'Send the deck'), ('Tina Example', 'Review the budget'),
             ('Ryan Example', 'Send the deck'), ('Will', 'Send the file')]
    text = 'Ryan Example — Follow-up from Tuesday. ' + continuation
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('I sent the deck', 'deck'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == closed


@pytest.mark.parametrize('object_word', ['it', 'this', 'that', 'them', 'these', 'those', 'Priya'])
def test_send_report_requires_a_named_object_in_anonymous_prep(context, object_word):
    ask = f'Send {object_word} to accounting'
    tasks = [('Bill Smith', 'Send the contract to legal'), ('Tina Example', 'Review the budget'),
             ('Aman Example', 'Book a venue'), ('Will', ask)]
    text = f'You promised to send the contract to legal.\nYou promised to send {object_word} to accounting.'
    reply, _ = conversation(context, 'prep', brief_case=(tasks, text))
    call(reply('I sent ' + object_word, object_word), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize('phrase', ['the it', 'the them', 'the these', 'the those'])
def test_object_evidence_drops_an_article_followed_only_by_stop_words(phrase):
    import delivered_tasks
    assert not delivered_tasks._send_object(('send ' + phrase).split())


def test_invoice_accompanying_an_unnamed_object_cannot_confirm_a_send(context):
    tasks = [('Bill Smith', 'Send the contract to legal'), ('Tina Example', 'Review the budget'),
             ('Aman Example', 'Book a venue'), ('Will', 'Send it with the invoice')]
    text = 'You promised to send the contract to legal.\nYou promised to send it with the invoice.'
    reply, _ = conversation(context, 'prep', brief_case=(tasks, text))
    call(reply('I sent the invoice', 'invoice'), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize('reported', ['Sarah asked you to send the deck.', 'Sarah needs you to send the deck.',
                                     'Sarah asked you to send the deck. Send the deck.'])
def test_first_sentence_below_a_heading_cannot_assign_another_request_to_its_owner(context, reported):
    tasks = [('Ryan Example', 'Send the deck'), ('Tina Example', 'Review the budget'),
             ('Aman Example', 'Book a venue'), ('Will', 'Send the file')]
    text = f'Ryan Example — {reported}'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('I sent the deck', 'deck'), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize(('competing_ask', 'competing_text'), [
    ('Send Robin the deck', 'Ryan Example — Send Robin the deck.'),
    ('Send the deck', 'Ryan Example — Sarah asked you to send the deck.'),
])
@pytest.mark.parametrize(('text', 'selection'), [('I sent the deck', 'deck'), ('Close deck', 'deck')])
def test_a_displayed_competing_task_remains_ambiguous_even_if_it_cannot_bind(context, competing_ask, competing_text, text, selection):
    tasks = [('Ryan Example', competing_ask), ('Tina Example', 'Send the deck'),
             ('Aman Example', 'Book a venue'), ('Will', 'Send the file')]
    reply, _ = conversation(context, 'brief', brief_case=(tasks, competing_text + '\nTina Example — Send the deck.'))
    call(reply(text, selection), context)
    assert all(row['status'] == 'open' for row in rows())


def test_different_displayed_send_objects_remain_independently_selectable(context):
    tasks = [('Ryan Example', 'Send Robin the deck'), ('Tina Example', 'Send the report'),
             ('Aman Example', 'Book a venue'), ('Will', 'Send the file')]
    text = 'Ryan Example — Send Robin the deck.\nTina Example — Send the report.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('I sent the report', 'report'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == {'cleanup-2'}


def test_an_object_word_prefacing_an_unrelated_request_does_not_identify_the_heading_owner(context):
    tasks = [('Ryan Example', 'Send the deck'), ('Tina Example', 'Review the spreadsheet'),
             ('Aman Example', 'Book a venue'), ('Will', 'Send the file')]
    text = 'Ryan Example — Deck request: Sarah asked you to send the deck.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('I sent the deck', 'deck'), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize('lead', ['', 'Follow-up from Tuesday. '])
@pytest.mark.parametrize('ask', ['Please send the deck.', 'Could you send the deck?', 'You promised to send the deck.'])
def test_owner_directed_send_uses_the_exact_displayed_ask(context, lead, ask):
    tasks = [('Ryan Example', 'Send the deck'), ('Tina Example', 'Review the budget'),
             ('Aman Example', 'Book a venue'), ('Will', 'Send the file')]
    reply, _ = conversation(context, 'brief', brief_case=(tasks, 'Ryan Example — ' + lead + ask))
    call(reply('I sent the deck', 'deck'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == {'cleanup-1'}


@pytest.mark.parametrize(('text', 'expected'), [
    ('Tina Example — Still has party spreadsheet review due today.', set()),
    ('Tina Example — Review the party spreadsheet due today.', set()),
    ('Tina Example — You agreed to review the party spreadsheet due today.', {'cleanup-3'}),
])
def test_only_explicit_assignment_under_a_heading_accepts_done(context, text, expected):
    reply, _ = conversation(context, 'nudge', nudge_text=text)
    call(reply('Done', 'Done'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == expected


@pytest.mark.parametrize(('text', 'selection', 'closed'), [
    ('I sent a contract', 'contract', {'cleanup-1'}),
    ('I sent the contract to legal', 'contract', set()),
    ('I sent the contract to legal', 'contract to legal', set()),
])
def test_one_word_send_admission_preserves_the_complete_owner_claim(context, text, selection, closed):
    tasks = [('Bill Smith', 'Send the contract to legal'), ('Tina Example', 'Review the budget'),
             ('Aman Example', 'Book a venue'), ('Will', 'Send the deck')]
    reply, _ = conversation(context, 'brief', brief_case=(tasks, 'Bill Smith — Send the contract to legal.'))
    call(reply(text, selection), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == closed


@pytest.mark.parametrize('text', ['I sent the deck', 'Close deck'])
def test_competing_object_mentions_in_one_sentence_still_need_a_named_task(context, text):
    tasks = [('Ryan Example', 'Send Robin the deck'), ('Tina Example', 'Send the deck'),
             ('Aman Example', 'Book a venue'), ('Will', 'Send the file')]
    brief = 'Tina Example — Send the deck, and send Robin the deck as well.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, brief))
    call(reply(text, 'deck'), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize(('bill_line', 'other_line', 'owner_reply', 'closed'), [
    ('The party is this evening.', 'Send the bill.', ('Close Bill', 'Bill'), set()),
    ('Review the budget.', 'Send the bill.', ('Close Bill', 'Bill'), set()),
    ('Review the budget.', 'Send the bill.', ('Close bill', 'bill'), set()),
    ('Review the budget before you send the bill.', 'The party is this evening.', ('Close the bill', 'the bill'), set()),
    ('Review the budget.', 'Send the bill.', ('Close the bill', 'the bill'), set()),
    ('Review the budget.', 'Send the bill.', ('Close Bill Smith', 'Bill Smith'), {'cleanup-1'}),
    ('Review the budget.', 'The party is this evening.', ('Close Bill', 'Bill'), {'cleanup-1'}),
    ('Review the budget.', 'The bill is overdue.', ('Close Bill', 'Bill'), set()),
    ('The party is this evening.', 'Send the bill.', ('I sent the bill', 'bill'), {'cleanup-3'}),
])
def test_name_only_close_can_select_only_the_printed_task_owner(context, bill_line, other_line, owner_reply, closed):
    tasks = [('Bill Smith', 'Review the budget'), ('Aman Example', 'Book the venue'),
             ('Tina Example', 'Send the bill'), ('Will', 'Send the file')]
    text = f'Bill Smith — {bill_line}\nTina Example — {other_line}'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    result = call(reply(*owner_reply), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == closed, result


@pytest.mark.parametrize(('summary', 'fields', 'displayed', 'closed'), [
    ('Send the deck to legal and book the room', {}, 'Send the deck to legal and book the room', False),
    ('Send the deck to legal and the invoice to finance', {}, 'Send the deck to legal and the invoice to finance', False),
    ('Send the deck', {'ask': 'Send the deck to legal and book the room'}, 'Send the deck', False),
    ('Send the deck', {'deliverable': 'Send the deck to legal and the invoice to finance'}, 'Send the deck', False),
    ('Send the deck', {'deliverable': 'Book the room'}, 'Send the deck', False),
    ('Deck follow-up', {'deliverable': 'Send the deck', 'ask': 'Send the deck to legal and book the room'},
     'Send the deck', False),
    ('Send the deck to legal and the invoice to finance', {'deliverable': 'Send the deck'}, 'Send the deck', False),
    ('Deck follow-up', {'deliverable': 'Send the deck to legal', 'ask': 'Send the deck to finance'},
     'Send the deck to legal', False),
    ('Sending the deck to legal and booking the room', {'deliverable': 'Send the deck'}, 'Send the deck', False),
    ('Email the deck and book the room', {'deliverable': 'Send the deck'}, 'Send the deck', False),
    ('Budget follow-up', {'deliverable': 'Send the deck'}, 'Send the deck', False),
    ('Deck follow-up', {'deliverable': 'Send the deck'}, 'Send the deck', True),
    ('Legal follow-up', {'deliverable': 'Send the deck to legal', 'ask': 'Send the deck to legal'},
     'Send the deck to legal', True),
    ('Legal follow-up', {'deliverable': 'Send the deck to legal'}, 'Send the deck to legal', True),
    ('Send the deck to Tina Example', {}, 'Send the deck to Tina Example', True),
    ('Send the deck', {'action_type': 'waiting-on'}, 'Send the deck', False),
])
def test_sent_object_must_cover_the_whole_canonical_task(context, summary, fields, displayed, closed):
    tasks = [('Ryan Example', summary), ('Tina Example', 'Review the budget'),
             ('Aman Example', 'Book a venue'), ('Will', 'Send the file')]
    reply, _ = conversation(context, 'brief', brief_case=(tasks, f'Ryan Example — {displayed}.'),
                            task_fields={'cleanup-1': fields})
    call(reply('I sent the deck', 'deck'), context)
    assert {r['anchor_key'] for r in rows() if r['status'] == 'resolved'} == ({'cleanup-1'} if closed else set())


@pytest.mark.parametrize(('owner_text', 'closed'), [('I sent the deck', False), ('Close deck', True)])
def test_an_older_send_object_receipt_still_supports_an_explicit_close(context, owner_text, closed):
    import hashlib
    import json
    tasks = [('Ryan Example', 'Send the deck to legal and book the room'), ('Tina Example', 'Review the budget'),
             ('Aman Example', 'Book a venue'), ('Will', 'Send the file')]
    reply, path = conversation(context, 'brief', brief_case=(tasks, 'Ryan Example — ' + tasks[0][1] + '.'))
    outbox = json.loads(path.read_text())
    saved = outbox['rows'][0]['receipt']['delivered_tasks']
    legacy_selector_version = 5
    assert saved['selector_version'] == legacy_selector_version
    saved['bindings'][0]['sent_object_phrases'] = [hashlib.sha256(b'deck').hexdigest()]
    path.write_text(json.dumps(outbox))
    call(reply(owner_text, 'deck'), context)
    assert {r['anchor_key'] for r in rows() if r['status'] == 'resolved'} == ({'cleanup-1'} if closed else set())


def test_recapture_can_change_what_is_owed_after_the_brief_is_delivered(context):
    import capture_shape
    from test_runtime_owner_loop_tool import ke
    tasks = [('Ryan Example', 'Send the deck'), ('Tina Example', 'Review the budget'),
             ('Aman Example', 'Book a venue'), ('Will', 'Send the file')]
    reply, _ = conversation(context, 'brief', brief_case=(tasks, 'Ryan Example — Send the deck.'))
    row = next(r for r in rows() if r['anchor_key'] == 'cleanup-1')
    capture_shape.retext(row, {'summary': row['summary'], 'deliverable': 'Send the deck to legal and book the room'},
                         ('summary', 'ask', 'deadline'))
    ke._load_continuity()._persist(row)
    result = call(reply('I sent the deck', 'deck'), context)
    assert all(r['status'] == 'open' for r in rows()), result


def test_sent_report_does_not_mean_bare_done_on_a_singleton_nudge(context):
    reply, _ = conversation(context, 'nudge')
    result = call(reply('I sent the DONE', 'DONE'), context)
    assert result['status'] == 'committed' and result['outcome'] == 'partial', result
    assert all(r['status'] == 'open' for r in rows())


@pytest.mark.parametrize(('owed_by', 'owner_text', 'closed'), [
    ('them', 'I sent the deck', False), ('them', 'Close deck', True), ('owner', 'I sent the deck', True),
])
def test_captured_promise_binds_the_person_who_owes_the_send(context, owed_by, owner_text, closed):
    from datetime import timedelta
    from delivery_effects import loop_version
    from test_delivered_task_completion import delivered_conversation
    from test_runtime_owner_loop_tool import WHEN, ke
    counterpart_owes = owed_by == 'them'
    quote, phone = 'I will send the deck.', '+15555550173'
    local = {'imessage': [{'id': 'promise-1', 'handle': phone, 'is_from_me': not counterpart_owes,
                           'timestamp': (WHEN - timedelta(minutes=2)).isoformat(), 'text': quote}]}
    action = {'type': 'waiting_on' if counterpart_owes else 'follow_up', 'newObligation': True,
              'channel': 'imessage', 'source': 'imessage', 'contactName': 'Ryan Example', 'contactIdentifier': phone,
              'contextSummary': 'Send the deck', 'contextAsk': 'Send the deck', 'deliverable': 'Send the deck',
              'owedBy': owed_by, 'evidence': [{'sourceType': 'imessage', 'sourceId': 'promise-1', 'snippet': quote}]}
    ke._load_continuity().resolve({'local': local, 'new_actions': [action]}, WHEN, resolve_existing=False)
    row, = rows()
    assert row['owed_by'] == owed_by and row['action_type'] == action['type']
    context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'a' * 64, 'bridge_sources': ['imessage']}
    payload = {'body': 'Ryan Example — Send the deck.', 'effects': [
        {'kind': 'brief_named', 'displayed_versions': {row['anchor_key']: loop_version(row)}}]}
    reply, _ = delivered_conversation(context, payload, [row])
    body = reply(owner_text, 'deck')
    result = call(body, context)
    assert (rows()[0]['status'] == 'resolved') == closed, result
    assert call(body, context) == call(dict(body, operation='inspect'), context) == result
