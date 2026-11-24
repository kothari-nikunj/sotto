"""Exercise accepted outbound messages through the admitted task writer and durable replay."""
import json

import pytest

from test_runtime_owner_loop_tool import call, context, ke, request, rows
from test_runtime_source_loop_tool import actual_writer_module
from test_runtime_loop_cleanup import prepare_cleanup

__all__ = ['context', 'actual_writer_module']


def conversation(context, kind, *, nudge_text=None, brief_case=None, task_fields=None):
    from delivery_effects import loop_version
    prepare_cleanup(context)
    tasks = [('Aman Example', 'Transfer the domain'), ('Ryan Example', 'Identify fast-moving investors'),
             ('Tina Example', 'Review the party spreadsheet'), ('Unseen Example', 'Send the deck')]
    if brief_case:
        tasks, brief_text = brief_case
    for row in rows():
        row['action_type'] = 'follow_up'  # These are the owner's obligations unless a case specifies otherwise.
        row['contact_name'], row['summary'] = tasks[int(row['anchor_key'][-1]) - 1]
        if task_fields:
            row.update(task_fields.get(row['anchor_key'], {}))
        ke._load_continuity()._persist(row)
    active = sorted(rows(), key=lambda r: r['anchor_key'])
    if kind == 'nudge':
        payload = {'label': 'proactive', 'body': nudge_text or 'Tina Example asked you to review the party spreadsheet, due Wednesday.',
                   'effects': [{'kind': 'eligibility', 'anchor_key': active[2]['anchor_key'],
                                'loop_version': loop_version(active[2])}]}
    elif kind == 'prep':
        offered = {row['anchor_key']: loop_version(row) for row in (active[0], active[3])}
        payload = {'body': brief_text if brief_case else 'Meeting prep\nYou agreed to transfer the domain.\nYou promised to send the deck.\n'
                           'Review the party spreadsheet before the call.',
                   'effects': [dict(kind='prep_tasks_offered', displayed_versions=offered)]}
    else:
        payload = {'body': brief_text if brief_case else 'Aman Example — You promised to redirect or transfer the domain.\nRyan Example — Any fast-moving investors in mind?',
                   'effects': [dict(kind='brief_named', displayed_versions={
                       row['anchor_key']: loop_version(row) for row in active[:2]})]}
    return delivered_conversation(context, payload, active)


def delivered_conversation(context, payload, active):
    from delivered_tasks import accepted, snapshot
    from retained_brief_proof import text_payload_hash
    route = 'b' * 64
    scope = {'source_revision': 'a' * 64, 'route_digest': route}
    context[1]['_retained_route_digest'] = lambda: route
    snap = snapshot(payload, scope, active)
    dispatch = {'dispatch_id': 'f' * 16, 'message_id': 'provider-id', 'route_digest': route,
                'payload_hash': text_payload_hash(payload['body'])}
    receipt = {**dispatch, 'message_ids': ['provider-id'], 'route_proof_version': 1, 'acceptance': 'accepted',
               'accepted_at': request()['admitted_at_ms'] / 1000 - 1}
    payload['task_snapshot'] = snap
    assert not accepted(payload, dict(receipt, payload_hash='0'*64), True)
    receipt.update(accepted(payload, receipt, True))
    path = context[0] / 'events' / 'outbox.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({'rows': [{'id': dispatch['dispatch_id'], 'status': 'delivered', 'acceptance': 'accepted', 'receipt': receipt}]}))
    path.chmod(0o600)
    def reply(text, *phrases):
        args = dict(operation='resolve_many', selection_mode='delivered', delivery=dispatch, allow_partial=True,
                    source_revision=scope['source_revision'], items=[dict(name=p, correction=p) for p in phrases])
        return request(args, text=text)
    return reply, path


@pytest.mark.parametrize(('kind', 'text', 'phrases', 'closed'), [
    ('nudge', 'Done', ['Done'], {'cleanup-3'}),
    ('brief', 'Redirect done. Close ryan as well', ['Redirect', 'ryan'], {'cleanup-1', 'cleanup-2'}),
    ('prep', 'Transfer done.', ['Transfer'], {'cleanup-1'}),
    ('prep', 'I sent the deck.', ['deck'], {'cleanup-4'}),
    ('brief', 'Done.', ['Done'], set()),
    ('brief', 'Redirect done. Close Unseen as well', ['Redirect', 'Unseen'], {'cleanup-1'}),
])
def test_reply_changes_only_visible_current_tasks(context, kind, text, phrases, closed):
    reply, path = conversation(context, kind)
    body = reply(text, *phrases)
    result = call(body, context)
    assert result['status'] == 'committed', result
    assert {r['anchor_key'] for r in rows() if r['status'] == 'resolved'} == closed
    path.unlink()
    assert call(body, context) == result == call(dict(body, operation='inspect'), context)


@pytest.mark.parametrize(('contact', 'item', 'phrase', 'closed'), [
    ('Ryan Example', 'Ryan Example — Follow-up from Tuesday. Any fast-moving investors in mind?', 'Ryan', True),
    ('Dr. Ryan Example', 'Dr. Ryan Example — Follow-up from Tuesday. Any fast-moving investors in mind?', 'Ryan', True),
    ('J. P. Morgan', 'J. P. Morgan: Follow-up from Tuesday. Any fast-moving investors in mind?', 'Morgan', True),
    ('Ryan Example', 'Ryan Example — Follow-up from Tuesday.\nAny fast-moving investors in mind?', 'Ryan', False),
    ('Ryan Example', 'Ryan Example — Follow-up from Tuesday. Tina Example asked about fast-moving investors.', 'Ryan', False),
])
def test_only_the_same_displayed_item_supplies_its_leading_contact(context, contact, item, phrase, closed):
    tasks = [('Aman Example', 'Transfer the domain'), (contact, 'Identify fast-moving investors'),
             ('Tina Example', 'Review the party spreadsheet'), ('Unseen Example', 'Send the deck')]
    reply, _ = conversation(context, 'brief', brief_case=(tasks, 'Aman Example — Transfer the domain.\n' + item))
    call(reply('Close ' + phrase, phrase), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == ({'cleanup-2'} if closed else set())


@pytest.mark.parametrize(('ask', 'phrase', 'closed'), [
    ('Introduce Aman Example to Ryan Example', 'Aman', True),
    ('Introduce Aman Example to Ryan Example', 'deck', False),
    ('Introduce Aman Example to Ryan Example', 'Ryan', False),
    ('Introduce Aman Example to Ryan Example', 'Introduce Ryan', True),
    ('Send the deck', 'Aman', False),
])
def test_another_named_person_must_be_part_of_the_displayed_task(context, ask, phrase, closed):
    tasks = [('Aman Example', ask), ('Ryan Example', 'Send the deck'),
             ('Tina Example', 'Review the party spreadsheet'), ('Unseen Example', 'Send the deck')]
    text = 'Tina Example — Review the party spreadsheet.\nAman Example — You promised to introduce Aman Example to Ryan Example.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('Close ' + phrase, phrase), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == ({'cleanup-1'} if closed else set())


def test_leading_name_does_not_choose_between_namesakes(context):
    tasks = [('Ryan Example', 'Transfer the domain'), ('Ryan Example', 'Identify fast-moving investors'),
             ('Tina Example', 'Review the party spreadsheet'), ('Unseen Example', 'Send the deck')]
    text = 'Tina Example — Review the party spreadsheet.\nRyan Example — Follow-up from Tuesday. Any fast-moving investors in mind?'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('Close Ryan', 'Ryan'), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize(('kind', 'selection', 'expected'), [
    ('brief', 'fast-moving investors', {'cleanup-2'}), ('brief', 'Ryan', set()),
    ('brief', 'deck', set()), ('nudge', 'Done', {'cleanup-3'}),
])
def test_namesake_requires_an_exact_grounded_task_not_the_shared_name(context, kind, selection, expected):
    tasks = [('Aman Example', 'Transfer the domain'), ('Ryan Example', 'Identify fast-moving investors'),
             ('Ryan Example', 'Review the party spreadsheet'), ('Ryan Example', 'Send the deck')]
    text = ('Aman Example — Transfer the domain.\nRyan Example — Identify fast-moving investors.\n'
            'Ryan Example — Send the deck.')
    reply, _ = conversation(context, kind, brief_case=(tasks, text),
                            nudge_text='Ryan Example asked you to review the party spreadsheet.')
    call(reply('Done' if selection == 'Done' else 'Close ' + selection, selection), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == expected


def test_anonymous_sentence_under_a_shared_heading_cannot_move_to_another_grounded_owner(context):
    tasks = [('Aman Example', 'Send the deck'), ('Tina Example', 'Review the party spreadsheet'),
             ('Ryan Example', 'Send the deck'), ('Ryan Example', 'Send the budget')]
    text = 'Tina Example — Review the party spreadsheet.\nRyan Example — Follow-up. Send the deck.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('I sent the deck', 'deck'), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize('lead_in', ['', 'Follow-up from Tuesday. '])
def test_visible_task_words_can_select_a_namesakes_grounded_paraphrase(context, lead_in):
    tasks = [('Ryan Example', 'Transfer the domain'), ('Ryan Example', 'Identify fast-moving investors'),
             ('Tina Example', 'Review the party spreadsheet'), ('Unseen Example', 'Send the deck')]
    text = 'Tina Example — Review the party spreadsheet.\nRyan Example — ' + lead_in + 'Any fast-moving investors in mind?'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('Close investors', 'investors'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == {'cleanup-2'}


@pytest.mark.parametrize(('selection', 'expected'), [
    ('Ryan', set()), ('Ryan Chen', {'cleanup-1'}), ('Ryan Patel', {'cleanup-2'}),
])
def test_recipient_sharing_an_owners_name_keeps_ambiguous_replies_ambiguous(context, selection, expected):
    tasks = [('Ryan Chen', 'Introduce Ryan Chen to Ryan Patel'), ('Ryan Patel', 'Send the deck'),
             ('Tina Example', 'Review the party spreadsheet'), ('Unseen Example', 'Send the budget')]
    text = 'Ryan Chen — Introduce Ryan Chen to Ryan Patel.\nRyan Patel — Send the deck.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('Close ' + selection, selection), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == expected


@pytest.mark.parametrize(('selection', 'closed'), [
    ('Ryan', set()), ('Ryan Chen', {'cleanup-3'}), ('investors', {'cleanup-1'}),
])
def test_shared_name_is_ambiguous_even_when_printed_apart_from_the_ask(context, selection, closed):
    tasks = [('Ryan Example', 'Identify fast-moving investors'), ('Ryan Example', 'Send the deck'),
             ('Ryan Chen', 'Review the party spreadsheet'), ('Will', 'Send the budget')]
    text = ('Ryan Example — Follow-up from Tuesday. Identify fast-moving investors.\n'
            'Ryan Chen — Review the party spreadsheet.')
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('Close ' + selection, selection), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == closed


@pytest.mark.parametrize(('text', 'selection'), [('Close Ryan', 'Ryan'), ('I sent Ryan', 'Ryan')])
def test_visible_contact_without_a_task_still_makes_a_name_reply_ambiguous(context, text, selection):
    tasks = [('Ryan Example', 'Identify fast-moving investors'), ('Ryan Example', 'Sign the contract'),
             ('Ryan Chen', 'Send the deck'), ('Will', 'Review the budget')]
    brief = 'Ryan Example — The party is this evening.\nRyan Chen — Send the deck.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, brief))
    call(reply(text, selection), context)
    assert all(row['status'] == 'open' for row in rows())


def test_hidden_namesake_words_cannot_mask_a_different_displayed_owner(context):
    tasks = [('Ryan Example', 'Identify fast-moving investors'), ('Ryan Example', 'Send the deck'),
             ('Ryan Chen', 'Review the party spreadsheet'), ('Will', 'Send the budget')]
    text = 'Identify fast-moving investors before the call.\nRyan Chen — Review the party spreadsheet.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('Close Ryan', 'Ryan'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == {'cleanup-3'}


def test_second_sentence_with_its_own_subject_binds_that_contacts_task(context):
    tasks = [('Aman Example', 'Transfer the domain'), ('Ryan Example', 'Identify fast-moving investors'),
             ('Tina Example', 'Review the party spreadsheet'), ('Unseen Example', 'Send the deck')]
    text = 'Aman Example — Transfer the domain. Tina Example asked you to review the party spreadsheet.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('Close spreadsheet', 'spreadsheet'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == {'cleanup-3'}


@pytest.mark.parametrize(('item', 'closed'), [
    ('Ryan Example — Follow-up from Tuesday. Will you send the deck?', set()),
    ('Will you send the deck?', set()),
    ('Will — Send the deck.', {'cleanup-3'}),
])
def test_one_word_in_prose_does_not_identify_the_owner_of_a_task(context, item, closed):
    tasks = [('Aman Example', 'Transfer the domain'), ('Ryan Example', 'Send the deck'),
             ('Will', 'Send the deck'), ('Unseen Example', 'Send the budget')]
    reply, _ = conversation(context, 'brief', brief_case=(tasks, 'Aman Example — Transfer the domain.\n' + item))
    call(reply('I sent the deck', 'deck'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == closed


@pytest.mark.parametrize(('kind', 'owner_text', 'selection'), [
    ('nudge', 'Done', 'Done'), ('nudge', 'Close deck', 'deck'), ('brief', 'I sent the deck', 'deck'),
])
def test_grounding_one_namesake_cannot_disambiguate_the_same_displayed_ask(context, kind, owner_text, selection):
    tasks = [('Ryan Example', 'Send the deck'), ('Tina Example', 'Review the budget'),
             ('Ryan Example', 'Send the deck'), ('Will', 'Send the file')]
    text = 'Ryan Example — Send the deck.'
    reply, _ = conversation(context, kind, brief_case=(tasks, text), nudge_text=text)
    call(reply(owner_text, selection), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize(('displayed', 'closed'), [
    ('Could you review the January budget?', set()),
    ('Could you review the January budget for Jamie?', set()),
    ('Alex and Jamie asked for your January budget review.', set()),
    ('Alex needs your January budget review.', {'cleanup-1'}),
])
def test_displayed_evidence_must_distinguish_a_namesake_task_from_its_alternative(context, displayed, closed):
    tasks = [('Ryan Example', 'Review January budget with Alex'), ('Tina Example', 'Review the venue'),
             ('Ryan Example', 'Review January budget with Jamie'), ('Will', 'Send the file')]
    reply, _ = conversation(context, 'brief', brief_case=(tasks, 'Ryan Example — ' + displayed))
    call(reply('Close budget', 'budget'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == closed


def test_generic_object_cannot_choose_the_shorter_namesake_task(context):
    tasks = [('Ryan Example', 'Review the budget'), ('Ryan Example', 'Review the January budget'),
             ('Tina Example', 'Send the deck'), ('Will', 'Send the file')]
    reply, _ = conversation(context, 'brief', brief_case=(tasks, 'Ryan Example — The budget is overdue.'))
    call(reply('Close budget', 'budget'), context)
    assert all(row['status'] == 'open' for row in rows())


def test_distinct_verbatim_action_on_the_same_object_still_selects_a_namesake_task(context):
    tasks = [('Ryan Example', 'Send the deck'), ('Tina Example', 'Review the venue'),
             ('Ryan Example', 'Review the deck'), ('Will', 'Send the file')]
    reply, _ = conversation(context, 'brief', brief_case=(tasks, 'Ryan Example — Send the deck.'))
    call(reply('I sent the deck', 'deck'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == {'cleanup-1'}


def test_a_possible_single_word_contact_stops_attribution_to_the_previous_subject(context):
    tasks = [('Aman Example', 'Transfer the domain'), ('Tina Example', 'Review the budget'),
             ('Ryan Example', 'Send the deck'), ('Will', 'Review the documents')]
    text = 'Aman Example — Transfer the domain.\nRyan Example — Follow-up from Tuesday. Will needs this today. Send the deck.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('I sent the deck', 'deck'), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize(('ending', 'closed'), [
    ('Send the deck.', set()), ('Aman Example asked you to send the deck.', {'cleanup-1'}),
    ('Aman Example needs this today. Send the deck.', set()),
])
def test_grounded_other_owner_cannot_pick_up_an_ask_after_the_heading_is_lost(context, ending, closed):
    tasks = [('Aman Example', 'Send the deck'), ('Tina Example', 'Review the budget'),
             ('Ryan Example', 'Send the deck'), ('Will', 'Review the documents')]
    text = 'Tina Example — Review the budget.\nRyan Example — Follow-up from Tuesday. Will needs this today. ' + ending
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('I sent the deck', 'deck'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == closed


def test_anonymous_next_line_after_contact_heading_cannot_move_to_another_grounded_owner(context):
    tasks = [('Aman Example', 'Send the deck'), ('Tina Example', 'Review the party spreadsheet'),
             ('Ryan Example', 'Send the deck'), ('Will', 'Send the budget')]
    text = 'Tina Example — Review the party spreadsheet.\nRyan Example — Follow-up from Tuesday.\nSend the deck.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('I sent the deck', 'deck'), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize('separator', [' ', '\n'])
def test_anonymous_ask_after_a_named_sentence_cannot_borrow_another_grounded_owner(context, separator):
    tasks = [('Aman Example', 'Send the deck'), ('Tina Example', 'Review the budget'),
             ('Ryan Example', 'Send the deck'), ('Will', 'Send the file')]
    text = 'Ryan Example asked about Tuesday.' + separator + 'Send the deck.\nTina Example — Review the budget.'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('I sent the deck', 'deck'), context)
    assert all(row['status'] == 'open' for row in rows())


def test_grounding_two_owners_with_the_same_ask_cannot_turn_will_you_into_an_owner(context):
    tasks = [('Will', 'Send the deck'), ('Ryan Example', 'Send the deck'),
             ('Tina Example', 'Review the party spreadsheet'), ('Aman Example', 'Transfer the domain')]
    text = 'Tina Example — Review the party spreadsheet.\nWill you send the deck?'
    reply, _ = conversation(context, 'brief', brief_case=(tasks, text))
    call(reply('I sent the deck', 'deck'), context)
    assert all(row['status'] == 'open' for row in rows())


def test_grounded_introduction_can_name_a_one_word_owner_in_the_ask(context):
    tasks = [('Will', 'Introduce Will to Ryan Example'), ('Ryan Example', 'Send the deck'),
             ('Tina Example', 'Review the party spreadsheet'), ('Aman Example', 'Transfer the domain')]
    reply, _ = conversation(context, 'brief', brief_case=(tasks, 'You promised to introduce Will to Ryan Example.'))
    call(reply('Close Will', 'Will'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == {'cleanup-1'}


def test_ambiguous_second_item_is_still_visible_to_bare_done(context):
    tasks = [('Ryan Example', 'Transfer the domain'), ('Ryan Example', 'Identify fast-moving investors'),
             ('Tina Example', 'Review the party spreadsheet'), ('Unseen Example', 'Send the deck')]
    text = "Tina Example’s party spreadsheet review was due Wednesday.\nRyan Example — Call tomorrow."
    reply, _ = conversation(context, 'nudge', nudge_text=text, brief_case=(tasks, ''))
    call(reply('Done.', 'Done'), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize('recipient', ['Ryan Example', 'J. P. Morgan'])
def test_single_nudge_can_complete_an_explicit_introduction(context, recipient):
    ask = f'Introduce Tina Example to {recipient}'
    tasks = [('Aman Example', 'Transfer the domain'), (recipient, 'Identify fast-moving investors'),
             ('Tina Example', ask), ('Unseen Example', 'Send the deck')]
    text = f'Tina Example asked you to introduce Tina Example to {recipient}.'
    reply, _ = conversation(context, 'nudge', nudge_text=text, brief_case=(tasks, ''))
    call(reply('Done.', 'Done'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == {'cleanup-3'}


def test_matching_words_cannot_close_a_task_the_prep_was_not_given(context):
    """Tina's task shares the line's words, but the prep was never given it and the line names nobody."""
    reply, _ = conversation(context, 'prep')
    call(reply('Spreadsheet done.', 'Spreadsheet'), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize(('text', 'selection'), [('Announcement done.', 'Announcement'), ('Done.', 'Done')])
def test_a_second_topic_in_a_nudge_cannot_close_the_task(context, text, selection):
    reply, _ = conversation(context, 'nudge', nudge_text=
        'Tina Example’s party spreadsheet review was due Wednesday.\n'
        'Ryan Example asked for feedback on an announcement.')
    call(reply(text, selection), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize(('text', 'selection'), [('Announcement done.', 'Announcement'), ('Done.', 'Done')])
def test_another_subject_for_the_same_person_is_not_a_task_selector(context, text, selection):
    reply, _ = conversation(context, 'nudge', nudge_text=
        "Tina Example's party spreadsheet review was due Wednesday, and her announcement goes out Friday.")
    call(reply(text, selection), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize('aside', [', her announcement goes out Friday', ' — her announcement goes out Friday',
                                   ' plus her announcement goes out Friday'])
def test_bare_done_requires_one_task_even_without_a_conjunction(context, aside):
    reply, _ = conversation(context, 'nudge', nudge_text=
        "Tina Example's party spreadsheet review was due Wednesday" + aside + '.')
    call(reply('Done.', 'Done'), context)
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize('lead', ['You promised to', 'You agreed to', 'Tina Example asked you to'])
def test_statement_assigning_a_complete_introduction_to_the_owner_accepts_done(context, lead):
    ask = 'Introduce Tina Example to Ryan Example'
    reply, _ = conversation(context, 'nudge', task_fields={'cleanup-3': {'summary': ask}},
                            nudge_text=f'{lead} introduce Tina Example to Ryan Example.')
    call(reply('Done', 'Done'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == {'cleanup-3'}


def test_bare_done_keeps_a_conjunction_inside_the_task(context):
    reply, _ = conversation(context, 'nudge', task_fields={'cleanup-3': {'summary': 'Review and sign the contract'}},
                            nudge_text='Tina Example asked you to review and sign the contract.')
    call(reply('Done.', 'Done'), context)
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == {'cleanup-3'}


def test_legacy_singleton_flag_alone_cannot_close_a_partly_displayed_task(context):
    reply, path = conversation(context, 'nudge', nudge_text='Tina Example — Send the deck.',
                               task_fields={'cleanup-3': {'deliverable': 'Send the deck',
                                                         'summary': 'Send the deck and book the room'}})
    doc = json.loads(path.read_text())
    saved = doc['rows'][0]['receipt']['delivered_tasks']
    assert [binding['anchor_key'] for binding in saved['bindings']] == ['cleanup-3']
    # Version 5 receipts could mark one visible phrase as the entire task.
    saved['singleton'] = True
    saved['bindings'][0].pop('task_label_digest', None)
    path.write_text(json.dumps(doc))
    result = call(reply('Done', 'Done'), context)
    assert result['items'][0]['status'] == 'refused'
    assert all(row['status'] == 'open' for row in rows())


@pytest.mark.parametrize('old_version', [None, 2, 3, 4])
def test_old_retained_selectors_cannot_authorize_a_new_completion(context, old_version):
    reply, path = conversation(context, 'nudge')
    doc = json.loads(path.read_text())
    saved = doc['rows'][0]['receipt']['delivered_tasks']
    saved.pop('visible_names')
    if old_version is None:
        saved.pop('selector_version')
    else:
        saved['selector_version'] = old_version
    path.write_text(json.dumps(doc))
    assert call(reply('Done.', 'Done'), context)['status'] == 'rejected'
    assert all(row['status'] == 'open' for row in rows())


def test_changed_task_refused_independently(context):
    reply, _ = conversation(context, 'brief')
    row = next(r for r in rows() if r['anchor_key'] == 'cleanup-1')
    row['summary'] = 'An unrelated new request'
    ke._load_continuity()._persist(row)
    result = call(reply('Redirect done. Close Ryan as well', 'Redirect', 'Ryan'), context)
    assert [i['status'] for i in result['items']] == ['refused', 'applied']


def test_permission_route_and_owner_wording_are_required(context):
    reply, _ = conversation(context, 'nudge')
    for owner_text in ('Not done', 'Done?'):
        assert call(reply(owner_text, 'done'), context)['status'] == 'rejected'
    context[1]['_retained_route_digest'] = lambda: 'c' * 64
    assert call(reply('Done', 'Done'), context)['status'] == 'rejected'
    assert all(r['status'] == 'open' for r in rows())


def test_real_nudge_delivery_retains_the_task_for_the_next_reply(context, monkeypatch):
    import hashlib
    import receiver
    from delivery_effects import loop_version
    from retained_brief_proof import text_payload_hash
    from test_protected_brief_detail import _fixture
    prepare_cleanup(context)
    _, route, state = _fixture(context[0], monkeypatch)
    row = next(r for r in rows() if r['anchor_key'] == 'cleanup-2')
    row['summary'] = 'Review the fixture budget'
    ke._load_continuity()._persist(row)
    text = 'Person 2 asked you to review the fixture budget.'
    def send(actual, _target, _key, _attempt, *, source_revision):
        assert actual == text and source_revision == state['revision']
        return True, '', dict(acceptance='accepted', message_id='parent', message_ids=['parent'],
                              route_proof_version=1, route_digest=route, payload_hash=text_payload_hash(actual))
    monkeypatch.setitem(receiver.OUTBOX.HOOKS, 'send', send)
    assert receiver._deliver_text(text, 'cron:sotto-proactive', run_id='e' * 16,
        effects=[dict(kind='eligibility', anchor_key=row['anchor_key'], loop_version=loop_version(row))],
        source_revision_at_composition=state['revision'], route_digest_at_composition=route,
        source_text_sha256=hashlib.sha256(text.encode()).hexdigest())
    receipt = json.loads((context[0]/'events/outbox.json').read_text())['rows'][0]['receipt']
    assert receipt['delivered_tasks']['singleton']
    assert [b['anchor_key'] for b in receipt['delivered_tasks']['bindings']] == [row['anchor_key']]


def test_scanner_captures_scope_for_every_nudge_and_only_matching_output(monkeypatch):
    import delivered_tasks
    import source_authority
    import delivery_effects
    captures = []
    monkeypatch.setattr(source_authority, 'current_authority', lambda: {'complete': True, 'revision': 'a'*64})
    monkeypatch.setattr(delivery_effects, 'stage', captures.extend)
    def compose(kind, nudges):
        assert kind == 'proactive'
        return str(nudges[0])
    scope = dict(source_revision='a'*64, route_digest='b'*64)
    # A plain nudge also records its composing revision, for reply context only.
    text = delivered_tasks.compose_nudge(compose, ['nudge'], [])
    assert text == 'nudge' and delivered_tasks.nudge_scope(captures, text, lambda: scope) == scope
    captures.clear()
    text = delivered_tasks.compose_nudge(compose, ['nudge'], [dict(anchor_key='task')])
    assert delivered_tasks.nudge_scope(captures, text, lambda: scope) == scope
    assert delivered_tasks.nudge_scope(captures, text, lambda: dict(scope, source_revision='c'*64)) is None
    assert delivered_tasks.nudge_scope(captures, 'changed', lambda: pytest.fail('no matching output')) is None


def test_gallery_reply_binds_only_tasks_on_delivered_cards(context, monkeypatch):
    import hashlib
    import receiver
    from delivery_effects import loop_version
    from test_protected_brief_detail import BRIEF, _fixture
    prepare_cleanup(context)
    _, route, state = _fixture(context[0], monkeypatch)
    body = BRIEF.strip().replace('Alex — Complete', 'Person 1 — Obligation 1. Complete')
    body = body.replace('Lunch with Alex', 'Lunch with Person 2 about a newsletter')
    body += '\nPerson 2 — Obligation 2.'
    hidden_task = next(r for r in rows() if r['anchor_key'] == 'cleanup-2')
    original = receiver.OUTBOX.HOOKS['send_gallery']
    def send(presentation, *args):
        ok, detail, receipt = original(presentation, *args)
        return ok, detail, {**receipt, 'message_ids': [receipt['message_id']]}
    monkeypatch.setitem(receiver.OUTBOX.HOOKS, 'send_gallery', send)
    assert receiver._deliver_text(body, 'cron:sotto-morning-brief', run_id='d' * 32,
        effects=[{'kind': 'brief_named', 'displayed_versions': {'cleanup-2': loop_version(hidden_task)}}],
        source_revision_at_composition=state['revision'], route_digest_at_composition=route,
        source_text_sha256=hashlib.sha256(body.encode()).hexdigest())
    [row] = json.loads((context[0]/'events/outbox.json').read_text())['rows']
    assert row['receipt']['visual_artifact_id']
    bindings = row['receipt']['delivered_tasks']['bindings']
    assert {b['anchor_key'] for b in bindings} == {'cleanup-1'}


def test_older_gallery_receipt_cannot_bind_a_task_from_full_text(context):
    from delivered_tasks import accepted
    reply, path = conversation(context, 'brief')
    doc = json.loads(path.read_text())
    receipt = doc['rows'][0]['receipt']
    receipt['visual_artifact_id'] = 'd' * 24
    assert not accepted({'presentation': {'images': ['01.png']},
                         'task_snapshot': receipt['delivered_tasks']}, receipt, True)
    path.write_text(json.dumps(doc))
    result = call(reply('Close Ryan', 'Ryan'), context)
    assert result['status'] == 'rejected'
    assert all(row['status'] == 'open' for row in rows())
