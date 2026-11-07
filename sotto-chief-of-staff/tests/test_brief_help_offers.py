"""Brief assistance is visible beside the actual ask, never a send or a new ledger action."""
import importlib.util
from pathlib import Path
import pytest

PACK = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('brief_help_compose', PACK / '_shared/scripts/compose_brief.py')
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)

DRAFT_OFFER = 'I can draft a reply.'


def _calendar_prep_fixture(base=None):
    import json
    from datetime import datetime

    path = PACK / 'evals/fixtures/early_prep_day.json'
    spec = importlib.util.spec_from_file_location('early_prep_evals', PACK / 'evals/run_evals.py')
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    resolved = ev.resolve_tokens(json.loads(path.read_text()), base or datetime(2026, 10, 26, 12))
    from source_context import source_result
    calendar = resolved['inputs']['source_results']['calendar']
    bounds = calendar['coverage']
    resolved['inputs']['source_results']['calendar'] = source_result(
        calendar['status'], complete=calendar['complete'],
        since=bounds['since'], until=bounds['until'])
    return resolved['stub_response'], resolved['inputs']


def _offer_count(markdown):
    return (markdown.count(DRAFT_OFFER) + markdown.count('I can research them')
            + markdown.count('I can prepare talking points') + markdown.count('I can prepare a briefing'))


def fixture():
    names = ['Alex Stone', 'Dana Reed', 'Sam Green', 'Morgan Bell']
    kinds = ['draft', 'research', 'research', 'draft']
    actions, lines, identifiers = [], ['# Morning brief', '## Needs Attention Now'], set()
    for index, (name, help_kind) in enumerate(zip(names, kinds)):
        identifier = f'person{index}@example.test'
        identifiers.add(identifier)
        actions.append({'type': 'meeting_prep' if help_kind == 'prep' else 'reply', 'contactName': name, 'contactIdentifier': identifier,
                        'channel': 'email', 'contextAsk': f'Review {name} proposal', 'helpKind': help_kind})
        lines.append(f'- **{name}**<!--id:{identifier}|ch:email-->: Asked for a proposal review.')
    return {'brief_markdown': '\n'.join(lines), 'actions': actions}, identifiers


def test_three_offers_stay_with_visible_items_and_do_not_create_actions():
    out, allow = fixture()
    original = [dict(a) for a in out['actions']]
    result = cb._add_help_offers(out, allow)
    lines = result['brief_markdown'].splitlines()
    assert _offer_count(result['brief_markdown']) == 3
    assert lines[2].endswith(DRAFT_OFFER) and 'Draft a reply to Alex Stone' not in lines[2]
    assert 'Research Dana Reed' in lines[3]
    assert 'I can research them' in lines[4]
    assert _offer_count(lines[5]) == 0
    assert result['actions'] == original
    assert cb._add_help_offers(result, allow) == result


def test_no_offer_for_handled_missing_source_or_ambiguous_task():
    out, allow = fixture()
    out['brief_markdown'] = out['brief_markdown'].replace('## Needs Attention Now', '## Already Handled')
    assert _offer_count(cb._add_help_offers(out, allow)['brief_markdown']) == 0
    out, allow = fixture()
    assert _offer_count(cb._add_help_offers(out, set())['brief_markdown']) == 0
    out, allow = fixture()
    out['actions'].append({**out['actions'][0], 'contextAsk': 'Return the signed contract'})
    result = cb._add_help_offers(out, allow)['brief_markdown']
    assert _offer_count(result) == 3 and result.splitlines()[2].endswith(DRAFT_OFFER)


def test_thin_brief_does_not_invent_three_and_birthday_is_not_work():
    out, allow = fixture()
    out['actions'] = out['actions'][:1]
    result = cb._add_help_offers(out, allow)['brief_markdown']
    assert _offer_count(result) == 1 and result.count(DRAFT_OFFER) == 1
    out, allow = fixture()
    out['actions'][0]['type'] = 'birthday'
    result = cb._add_help_offers(out, allow)['brief_markdown']
    assert DRAFT_OFFER not in result.splitlines()[2]


def test_offers_survive_image_and_text_presentation():
    import visual_brief
    from chatfmt import to_imessage
    out, allow = fixture()
    markdown = cb._add_help_offers(out, allow)['brief_markdown']
    deck = visual_brief.build(markdown)
    assert deck and visual_brief.fits(deck)
    cards = '\n'.join(b['text'] for c in deck['cards'] for b in c['blocks'])
    assert _offer_count(cards) == 3
    assert _offer_count(to_imessage(markdown)) == 3


def test_same_person_wrong_task_does_not_get_an_offer():
    out, allow = fixture()
    out['actions'] = [{**out['actions'][0], 'contextAsk': 'Review acquisition contract', 'helpKind': 'prep'}]
    before = out['brief_markdown']
    assert cb._add_help_offers(out, allow)['brief_markdown'] == before


def test_copied_wrong_loop_marker_cannot_bind_an_offer():
    out, allow = fixture()
    out['actions'] = [{**out['actions'][0], 'contextAsk': 'Review acquisition contract', 'loopId': 'contract'}]
    out['brief_markdown'] = out['brief_markdown'].replace('proposal review.', 'proposal review. <!--loop:contract-->')
    before = out['brief_markdown']
    assert cb._add_help_offers(out, allow)['brief_markdown'] == before


def test_compose_delivers_three_inline_offers_without_another_model_pass(monkeypatch):
    import json
    out, _ = fixture()
    monkeypatch.setenv('SOTTO_CRITIC', 'off')
    calls = []
    def model(prompt, inputs, **kwargs):
        calls.append(prompt)
        return json.dumps(out)
    inputs = {'type': 'morning', 'local': {}, 'google': {'emails': [
        {'senderEmail': a['contactIdentifier'], 'from': a['contactIdentifier'],
         'subject': 'Proposal review', 'body': 'Please review my proposal'} for a in out['actions']]}}
    result = cb.compose(inputs, llm=model)
    assert len(calls) == 1
    assert _offer_count(result['brief_markdown']) == 3
    assert _offer_count(result['brief_text']) == 3
    assert result['brief_markdown'].count('Draft a reply to') == 0


@pytest.mark.parametrize('selection', [None, 'none', 'invalid'])
def test_no_selected_offer_never_becomes_a_generic_draft(selection):
    out, allow = fixture()
    out['actions'] = out['actions'][:1]
    out['actions'][0]['helpKind'] = selection
    out['brief_markdown'] = out['brief_markdown'].replace('proposal review.',
        'proposal review. <!--loop:proposal--> ', 1)
    rows = [{'anchor_key': 'proposal', 'contact_name': 'Alex Stone',
             'contact_identifier': 'person0@example.test', 'action_type': 'reply',
             'summary': 'Review Alex Stone proposal', 'status': 'open'}]
    assert _offer_count(cb._add_help_offers(dict(out), allow, rows)['brief_markdown']) == 0
    out['actions'][0]['helpKind'] = 'draft'
    assert _offer_count(cb._add_help_offers(dict(out), allow, rows)['brief_markdown']) == 1


def test_unsupported_prep_is_not_converted_into_a_draft():
    out, allow = fixture()
    out['actions'] = [{**out['actions'][0], 'type': 'reply', 'helpKind': 'prep'}]
    assert _offer_count(cb._add_help_offers(out, allow)['brief_markdown']) == 0



def test_name_only_offer_holds_conflicting_source_recipients():
    row = {'anchor_key': 'deck', 'contact_name': 'Alex Stone', 'status': 'open',
           'contact_identifier': 'alex@example.test', 'action_type': 'reply',
           'summary': 'Alex Stone asked for the strategy deck.'}
    peer = {**row, 'anchor_key': 'budget', 'contact_identifier': '+15550000001',
            'summary': 'Alex Stone asked about the budget.'}
    line = ('## Needs Attention Now\n- **Alex Stone**<!--id:alex@example.test|ch:email-->: '
            'Alex Stone asked for the strategy deck. <!--loop:deck-->')
    action = {'type': 'reply', 'contactName': 'Alex Stone',
              'contactIdentifier': 'alex@example.test', 'contextAsk': 'strategy deck',
              'loopId': 'deck', 'helpKind': 'draft'}
    allow = {'alex@example.test', '+15550000001'}
    for actions in ([action], []):
        out = {'brief_markdown': line, 'actions': actions}
        assert _offer_count(cb._add_help_offers(out, allow, [row, peer])['brief_markdown']) == 0
        assert _offer_count(cb._add_help_offers({'brief_markdown': line, 'actions': actions},
                                                allow, [row])['brief_markdown']) == (1 if actions else 0)


def test_anchored_historical_relative_time_does_not_repeat_visible_ask():
    old = {'anchor_key': 'old-intro', 'contact_name': 'Alex Stone', 'status': 'open',
           'contact_identifier': '+15550000001', 'canonical_id': 'phone-contact',
           'created_at': '2026-10-05 17:31:00', 'deadline': '2026-10-06',
           'summary': ('Alex reached out by email and a bot relayed that he wants to connect '
                       'today or tomorrow regarding Project Cedar.')}
    separate = {'anchor_key': 'contract', 'contact_name': 'Alex Stone', 'status': 'open',
                'contact_identifier': 'alex@example.test', 'canonical_id': 'email-contact',
                'created_at': '2026-10-07 09:00:00', 'deadline': '2026-10-08',
                'summary': 'Send the signed partnership contract.'}
    first = ('**Alex Stone**<!--id:alex@example.test|ch:email-->'
             '<!--loop:old-intro--><!--loop:contract-->: Alex reached out by '
             'email and a bot relayed that he wants to connect regarding Project Cedar, '
             'followed by a note asking for a call. A quick reply closes the intro loop.\n')
    main = '## Needs Attention Now\n' + first
    # Alone, the email tap may be the same person on another channel...
    assert cb._line_open_task_keys(first, [old], today='2026-10-10') == {'old-intro'}
    # ...but the ledger records that address as a different canonical person, so a shared
    # display name cannot carry the phone row's task: the backstop shows it again.
    assert cb._line_open_task_keys(first, [old, separate], today='2026-10-10') == set()
    out = cb._append_still_open({'brief_markdown': main}, [old, separate],
                                today='2026-10-10')['brief_markdown']
    assert 'Send the signed partnership contract' in out
    assert out.count('Project Cedar') == 2  # repeated rather than hidden


def test_relative_time_exception_requires_exact_anchor_and_ask_content():
    row = {'anchor_key': 'old-intro', 'contact_name': 'Alex Stone', 'status': 'open',
           'contact_identifier': '+15550000001', 'canonical_id': 'phone-contact',
           'summary': 'Alex wants to connect today or tomorrow regarding Project Cedar.'}
    visible = '**Alex Stone**<!--id:alex@example.test|ch:email-->: Alex wants to connect regarding Project Cedar.'
    assert cb._line_open_task_keys(visible, [row]) == set()
    assert cb._line_open_task_keys(visible + ' <!--loop:wrong-intro-->', [row]) == set()
    assert cb._line_open_task_keys('**Alex Stone**<!--id:alex@example.test|ch:email-->: '
                                   'Discuss the Project Cedar contract. '
                                   '<!--loop:old-intro-->', [row]) == set()
    collision = {**row, 'anchor_key': 'other-person',
                 'contact_identifier': '+15550000002', 'canonical_id': 'different-person'}
    assert cb._line_open_task_keys(visible + ' <!--loop:old-intro-->',
                                   [row, collision]) == set()
    assert cb._line_open_task_keys(visible + ' <!--loop:old-intro--><!--loop:other-person-->',
                                   [row, collision]) == set()
    dated = {**row, 'summary': 'Alex wants to connect on October 12 regarding Project Cedar.'}
    assert cb._line_open_task_keys(visible + ' <!--loop:old-intro-->', [dated]) == set()


@pytest.mark.parametrize('created_at, wording', [
    ('2026-10-10 08:00:00', 'today'),
    ('2026-10-10 08:00:00', ''),
    ('', ''),
    ('2026-10-05 08:00:00', 'today'),
])
def test_cross_channel_relative_day_is_not_silently_changed(created_at, wording):
    row = {'anchor_key': 'call', 'contact_name': 'Alex Stone', 'status': 'open',
           'contact_identifier': '+15550000001', 'canonical_id': 'phone-contact',
           'created_at': created_at,
           'summary': 'Alex wants to connect tomorrow regarding Project Cedar.'}
    line = ('**Alex Stone**<!--id:alex@example.test|ch:email-->: '
            f'Alex wants to connect {wording} regarding Project Cedar. <!--loop:call-->')
    assert cb._line_open_task_keys(line, [row], today='2026-10-10') == set()
    matching = line.replace(f'connect {wording} regarding', 'connect tomorrow regarding')
    assert cb._line_open_task_keys(matching, [row], today='2026-10-10') == {'call'}


def test_identifierless_thread_requires_source_sender_not_shared_name(monkeypatch):
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda source: source == 'gmail')
    thread = {'anchor_key': 'thread:source-thread', 'contact_name': 'Alex Stone',
              'contact_identifier': '', 'canonical_id': '', 'status': 'open',
              'created_at': '2026-10-08 08:00:00', 'deadline': '2026-10-09',
              'summary': 'Alex asked if you are free for a call regarding Project Cedar'}
    legacy_name = {**thread, 'anchor_key': 'email:follow_up:name:alex stone', 'deadline': None}
    different_task = {'anchor_key': 'contract', 'contact_name': 'Alex Stone',
                      'contact_identifier': '+15550000001', 'canonical_id': 'phone-person',
                      'status': 'open', 'summary': 'Send the signed partnership contract'}
    rows = [thread, legacy_name, different_task]
    line = ('**Alex Stone**<!--id:alex@example.test|ch:email-->'
            '<!--loop:thread:source-thread--><!--loop:email:follow_up:name:alex stone-->: '
            'Alex asked if you are free for a call regarding Project Cedar.')
    assert cb._line_open_task_keys(line, rows, today='2026-10-10') == set()
    source = {'google': {'userEmail': 'owner@example.test', 'emails': [
        {'threadId': 'source-thread', 'from': 'Alex Stone <alex@example.test>'},
        {'threadId': 'source-thread', 'from': 'Owner <owner@example.test>'},
    ]}}
    senders = cb._source_thread_senders(source)
    assert cb._line_open_task_keys(line, rows, today='2026-10-10',
                                   source_thread_senders=senders) == {'thread:source-thread'}
    for wrong in (line.replace('<!--loop:thread:source-thread-->', ''),
                  line.replace('Project Cedar', 'the partnership contract'),
                  line.replace('**Alex Stone**', '**Dana Reed**').replace('Alex asked', 'Asked'),
                  line.replace('id:alex@example.test', 'id:other@example.test')):
        assert 'thread:source-thread' not in cb._line_open_task_keys(
            wrong, rows, today='2026-10-10', source_thread_senders=senders)
    first_name = {**thread, 'contact_name': 'Alex'}
    assert cb._line_open_task_keys(line, [first_name, legacy_name, different_task],
                                   today='2026-10-10', source_thread_senders=senders) == set()
    main = '## Needs Attention Now\n' + line + '\n'
    out = cb._append_still_open({'brief_markdown': main}, [thread], today='2026-10-10',
                                source_thread_senders=senders)['brief_markdown']
    assert out == main


def test_identifierless_thread_source_proof_holds_ambiguous_or_revoked(monkeypatch):
    import source_context
    row = {'anchor_key': 'thread:source-thread', 'contact_name': 'Alex Stone',
           'contact_identifier': '', 'canonical_id': '', 'status': 'open',
           'summary': 'Alex asked if you are free for a call regarding Project Cedar'}
    other = {**row, 'anchor_key': 'email:follow_up:name:alex stone'}
    different_task = {**row, 'anchor_key': 'contract', 'canonical_id': 'other-person',
                      'contact_identifier': '+15550000001',
                      'summary': 'Send the signed partnership contract'}
    rows = [row, other, different_task]
    line = ('**Alex Stone**<!--id:alex@example.test|ch:email-->'
            '<!--loop:thread:source-thread-->: Alex asked if you are free for a call '
            'regarding Project Cedar.')
    source = {'google': {'userEmail': 'owner@example.test', 'emails': [
        {'threadId': 'source-thread', 'from': 'Alex Stone <alex@example.test>'},
        {'threadId': 'source-thread', 'from': 'Dana Reed <dana@example.test>'},
    ]}}
    monkeypatch.setattr(source_context, 'allowed', lambda source: source == 'gmail')
    assert cb._line_open_task_keys(line, rows, source_thread_senders=
                                   cb._source_thread_senders(source)) == {'thread:source-thread'}
    source['google']['emails'][1]['from'] = 'Alex Stone <other@example.test>'
    assert cb._line_open_task_keys(line, rows, source_thread_senders=
                                   cb._source_thread_senders(source)) == set()
    source['google']['emails'].pop()
    source['google']['emails'][0]['from'] = 'Different Person <alex@example.test>'
    assert cb._line_open_task_keys(line, rows, source_thread_senders=
                                   cb._source_thread_senders(source)) == set()
    source['google']['emails'][0]['from'] = 'Alex Stone <alex@example.test>'
    monkeypatch.setattr(source_context, 'allowed', lambda _source: False)
    assert cb._line_open_task_keys(line, rows, source_thread_senders=
                                   cb._source_thread_senders(source)) == set()


def test_exact_thread_accepts_address_only_introducer_but_never_borrows_his_identity(monkeypatch):
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda source: source == 'gmail')
    row = {'anchor_key': 'thread:source-thread', 'contact_name': 'Alex Stone',
           'contact_identifier': '', 'canonical_id': '', 'status': 'open',
           'summary': 'Alex asked if you are free for a call regarding Project Cedar',
           'ask': 'Reply to Alex with a call time regarding Project Cedar'}
    peer = {'anchor_key': 'other-task', 'contact_name': 'Alex Stone',
            'contact_identifier': '+15550000001', 'canonical_id': 'other-person',
            'status': 'open', 'summary': 'Send the signed partnership contract'}
    rows = [row, peer]
    source = {'google': {'userEmail': 'owner@example.test', 'emails': [
        {'threadId': 'source-thread', 'from': 'introducer@example.test'},
        {'threadId': 'source-thread', 'from': 'Alex Stone <alex@example.test>'},
    ]}}
    line = ('**Alex Stone**<!--id:alex@example.test|ch:email-->'
            '<!--loop:thread:source-thread-->: Alex asked if you are free for a call '
            'regarding Project Cedar. Reply to Alex with a call time regarding Project Cedar.')
    senders = cb._source_thread_senders(source)
    assert senders == {'thread:source-thread': {'alex stone': 'alex@example.test'}}
    assert cb._line_open_task_keys(line, rows, source_thread_senders=senders) == {
        'thread:source-thread'}
    assert cb._line_open_task_keys(line.replace('id:alex@example.test',
                                           'id:introducer@example.test'), rows,
                                   source_thread_senders=senders) == set()
    assert cb._line_open_task_keys(line.replace('Project Cedar', 'Project Maple'), rows,
                                   source_thread_senders=senders) == set()
    source['google']['emails'][0]['from'] = 'unreadable sender'
    assert cb._source_thread_senders(source) == {}
    source['google']['emails'][0]['from'] = 'introducer@example.test'
    source['google']['emails'][0]['senderEmail'] = 'different@example.test'
    assert cb._source_thread_senders(source) == {}
    source['google']['emails'][0]['senderEmail'] = 'introducer@example.test'
    source['google']['emails'].append({'threadId': 'source-thread', 'from': 'alex@example.test'})
    assert cb._source_thread_senders(source) == {}  # candidate address has unknown alias
    source['google']['emails'].pop()
    source['google']['emails'].extend([
        {'threadId': 'source-thread', 'from': 'Alex Stone <second@example.test>'},
        {'threadId': 'source-thread', 'from': 'Different Person <second@example.test>'},
    ])
    assert cb._source_thread_senders(source) == {}  # all observed name owners count
    del source['google']['emails'][-2:]
    monkeypatch.setattr(source_context, 'allowed', lambda _source: False)
    assert cb._source_thread_senders(source) == {}


def test_source_thread_exact_precise_ask_accepts_present_participle_not_partial_topic():
    row = {'anchor_key': 'thread:cedar', 'contact_name': 'Alex Stone', 'status': 'open',
           'contact_identifier': '', 'canonical_id': '', 'deadline': '2026-10-10',
           'summary': 'Alex asked if you are free for a call regarding Project Cedar after the intro',
           'ask': 'Reply to Alex with your availability for a call or politely decline'}
    peer = {'anchor_key': 'contract', 'contact_name': 'Alex Stone', 'status': 'open',
            'contact_identifier': '+15550000001', 'canonical_id': 'phone-person',
            'deadline': '2026-10-10',
            'summary': 'Sign the Project Cedar contract'}
    source = {'thread:cedar': {'alex stone': 'alex@example.test'}}
    line = ('**Alex Stone**<!--id:alex@example.test|ch:email--><!--loop:thread:cedar-->: '
            'Alex reached out regarding Project Cedar after the intro and his ask for a call. '
            'Replying with your availability or politely declining closes the loop.')
    assert cb._line_open_task_keys(line, [row, peer]) == set()
    assert cb._line_open_task_keys(line, [row, peer], source_thread_senders=source) == {'thread:cedar'}
    for partial in (line.replace('politely declining', 'later'),
                    line.replace('availability', 'interest'),
                    line.replace('call', 'contract'),
                    line.replace('Project Cedar', 'Project Maple')):
        assert cb._line_open_task_keys(partial, [row, peer], source_thread_senders=source) == set()
    out = cb._append_still_open({'brief_markdown': '## Needs Attention Now\n' + line + '\n'},
                                [row, peer], today='2026-10-10', source_thread_senders=source)
    assert out['brief_markdown'].count('Project Cedar after the intro') == 1
    assert 'Sign the Project Cedar contract' in out['brief_markdown']


def test_meeting_item_offers_prep():
    out, inputs = _calendar_prep_fixture()
    assert 'I can prepare a briefing for Cedar rollout decision review.' in cb._add_help_offers(
        out, {'rollout-review'}, inputs=inputs)['brief_markdown']


@pytest.mark.parametrize('scenario, offered', [
    ('earlier_different_person', True),
    ('earlier_same_email', True),
    ('earlier_same_email_renamed', True),
    ('same_time_same_email', True),
    ('later_same_email', True),
    ('earlier_declined', True),
    ('earlier_cancelled', True),
    ('earlier_free_context', True),
    ('earlier_shared_nonparticipant', True),
    ('ambiguous_name', True),
    ('substring_name', True),
    ('duplicate_occurrence', False),
    ('duplicate_title', True),
    ('invalid_occurrence', True),
    ('invalid_target', False),
    ('wrong_action_start', True),
    ('declined_target', False),
    ('declined_attendee', False),
    ('free_target', True),
    ('nonparticipant_target', False),
    ('incomplete_calendar', False),
    ('missing_calendar_proof', False),
    ('narrow_calendar_window', False),
])
def test_production_prep_offer_targets_the_source_event_title(
        scenario, offered, monkeypatch, tmp_path):
    import copy
    import json

    out, inputs = _calendar_prep_fixture()
    earlier, target = inputs['google']['events']
    if scenario not in ('earlier_different_person', 'incomplete_calendar',
                        'missing_calendar_proof', 'narrow_calendar_window', 'invalid_target',
                        'wrong_action_start', 'declined_target', 'declined_attendee',
                        'free_target', 'nonparticipant_target',
                        'duplicate_occurrence'):
        earlier['attendees'][1] = copy.deepcopy(target['attendees'][1])
    if scenario == 'earlier_same_email_renamed':
        earlier['attendees'][1]['displayName'] = 'Dana Cedar'
    elif scenario == 'same_time_same_email':
        earlier['start'] = target['start']
    elif scenario == 'later_same_email':
        earlier['start'] = inputs['source_results']['calendar']['coverage']['until']
    elif scenario == 'earlier_declined':
        earlier['my_response'] = 'declined'
    elif scenario == 'earlier_cancelled':
        earlier['status'] = 'cancelled'
    elif scenario == 'earlier_free_context':
        earlier['transparency'] = 'transparent'
    elif scenario == 'earlier_shared_nonparticipant':
        earlier['calendar_id'] = 'shared@other.test'
        earlier['attendees'] = earlier['attendees'][1:]
    elif scenario in ('ambiguous_name', 'substring_name'):
        earlier['attendees'][1]['email'] = 'other@cedar.test'
        if scenario == 'substring_name':
            earlier['attendees'][1]['displayName'] = 'Dana Rivers Junior'
    elif scenario == 'duplicate_occurrence':
        inputs['google']['events'].append(copy.deepcopy(target))
    elif scenario == 'duplicate_title':
        earlier['summary'] = target['summary']
    elif scenario == 'invalid_occurrence':
        earlier['start'] = 'unknown'
    elif scenario == 'invalid_target':
        target['start'] = 'unknown'
    elif scenario == 'wrong_action_start':
        out['actions'][0]['meetingTime'] = earlier['start']
    elif scenario == 'declined_target':
        target['my_response'] = 'declined'
    elif scenario == 'declined_attendee':
        target['attendees'][1]['responseStatus'] = 'declined'
    elif scenario == 'free_target':
        target['transparency'] = 'transparent'
    elif scenario == 'nonparticipant_target':
        target['calendar_id'] = 'shared@other.test'
        target['attendees'] = target['attendees'][1:]
    elif scenario == 'incomplete_calendar':
        inputs['source_results']['calendar'].update(status='partial', complete=False)
    elif scenario == 'missing_calendar_proof':
        inputs.pop('source_results')
    elif scenario == 'narrow_calendar_window':
        inputs['source_results']['calendar']['coverage']['until'] = earlier['start']
    original = copy.deepcopy(out)
    calls = []
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@inside.test')
    monkeypatch.setenv('SOTTO_CRITIC', 'off')

    def model(prompt, payload, **kwargs):
        calls.append(payload)
        return json.dumps(out)

    result = cb.compose(inputs, llm=model)
    assert len(calls) == 1
    assert ('I can prepare a briefing for Cedar rollout decision review.' in result['brief_markdown']) is offered
    assert ('I can prepare a briefing for Cedar rollout decision review.' in result['brief_text']) is offered
    assert original['brief_markdown'].strip() in result['brief_markdown']
    action = next(action for action in result['actions'] if action['id'] == 'early-prep')
    assert all(action[key] == value for key, value in original['actions'][0].items())
    assert action['newObligation'] is False
    assert not list(tmp_path.glob('knowledge/continuity/*.md'))


def test_prep_without_calendar_inputs_fails_closed_and_removes_old_offer():
    out, _inputs = _calendar_prep_fixture()
    original = out['brief_markdown']
    out['brief_markdown'] = original.rstrip() + ' I can prepare talking points.\n'
    assert cb._add_help_offers(out, {'rollout-review'})['brief_markdown'] == original


def test_calendar_email_marker_binds_only_one_exact_source_action_and_full_ask():
    import copy
    out, inputs = _calendar_prep_fixture()
    out['actions'][0]['meetingTime'] = 'Wednesday afternoon'
    out['brief_markdown'] = out['brief_markdown'].replace(
        '<!--id:rollout-review|ch:calendar--><!--meeting:event_id:rollout-review-->',
        '<!--id:dana@cedar.test|ch:email-->')
    assert 'I can prepare a briefing for Cedar rollout decision review.' in cb._render_help_offers(
        copy.deepcopy(out), {'rollout-review', 'dana@cedar.test'}, inputs=inputs)['brief_markdown']
    inputs['google']['events'][0]['attendees'][1] = copy.deepcopy(inputs['google']['events'][1]['attendees'][1])
    out['actions'].append({**out['actions'][0], 'id': 'other', 'contactIdentifier': 'routine-checkin'})
    assert 'I can prepare a briefing' not in cb._render_help_offers(
        copy.deepcopy(out), {'rollout-review', 'routine-checkin', 'dana@cedar.test'}, inputs=inputs)['brief_markdown']
    out['actions'].pop()
    out['brief_markdown'] = out['brief_markdown'].replace(
        '<!--id:dana@cedar.test|ch:email-->', '<!--id:dana@cedar.test|ch:email--><!--meeting:event_id:routine-checkin-->')
    assert 'I can prepare a briefing' not in cb._render_help_offers(
        out, {'rollout-review', 'routine-checkin', 'dana@cedar.test'}, inputs=inputs)['brief_markdown']


def test_malformed_calendar_proof_with_prep_does_not_drop_valid_draft_offer():
    out, allow = fixture()
    prep, inputs = _calendar_prep_fixture()
    out['actions'].append(prep['actions'][0])
    inputs['source_results'] = ['invalid']
    inputs['google']['events'].append(None)
    rendered = cb._add_help_offers(out, allow, inputs=inputs)['brief_markdown']
    assert DRAFT_OFFER in rendered
    assert 'I can prepare a briefing' not in rendered


def test_valid_selected_prep_is_the_only_offer_but_other_tasks_stay_visible():
    import copy
    out, inputs = _calendar_prep_fixture()
    out['_prep_offer_record'] = {'offer_id': 'model-invented-delivery'}
    out['_prep_offer'] = {'person': 'Forged', 'meeting': {'event_id': 'invented', 'start': 'tomorrow'}}
    rendered = cb._add_help_offers(copy.deepcopy(out), {'rollout-review'}, inputs=inputs)
    assert '_prep_offer_record' not in rendered
    assert rendered['_prep_offer'] == {'person': 'Dana Rivers',
        'question': 'I can prepare a briefing for Cedar rollout decision review. Want me to?', 'ambiguous': False,
        'meeting': {'event_id': 'rollout-review', 'start': inputs['google']['events'][1]['start']}}
    assert ' — say ' not in rendered['brief_markdown']
    out['actions'][0]['helpKind'] = 'none'
    assert '_prep_offer' not in cb._add_help_offers(copy.deepcopy(out), {'rollout-review'}, inputs=inputs)
    out['actions'][0]['helpKind'] = 'prep'
    out['actions'].append({'type': 'reply', 'helpKind': 'draft', 'contactName': 'Alex Stone',
                          'contactIdentifier': 'alex@example.test', 'contextAsk': 'Review the deck'})
    out['brief_markdown'] += '- **Alex Stone**<!--id:alex@example.test|ch:email-->: Review the deck.\n'
    rendered = cb._add_help_offers(out, {'rollout-review', 'alex@example.test'}, inputs=inputs)
    assert 'I can prepare a briefing for Cedar rollout decision review.' in rendered['brief_markdown']
    assert DRAFT_OFFER not in rendered['brief_markdown']
    assert 'Review the deck' in rendered['brief_markdown']
    assert len(rendered['actions']) == 3
    assert rendered['_prep_offer']['ambiguous'] is False


def test_second_calendar_prep_offer_is_withheld_without_removing_its_task():
    out, inputs = _calendar_prep_fixture()
    action = out['actions'][1]
    action['helpKind'] = 'prep'
    out['brief_markdown'] += ('- **Sam Green**<!--id:routine-checkin|ch:calendar-->: '
                              'Routine partner check-in.\n')
    rendered = cb._add_help_offers(out, {'rollout-review', 'routine-checkin'}, inputs=inputs)
    assert rendered['brief_markdown'].count('I can prepare a briefing') == 1
    assert 'Sam Green' in rendered['brief_markdown'] and len(rendered['actions']) == 2
    assert rendered['_prep_offer']['meeting']['event_id'] == 'rollout-review'


def test_prep_suffix_never_eats_a_following_obligation():
    out, inputs = _calendar_prep_fixture()
    question = 'I can prepare a briefing for Cedar rollout decision review.'
    out['brief_markdown'] = out['brief_markdown'].rstrip() + f' {question} She also needs the deck by Friday.\n'
    rendered = cb._add_help_offers(out, {'rollout-review'}, inputs=inputs)
    assert 'She also needs the deck by Friday.' in rendered['brief_markdown']
    assert rendered['brief_markdown'].count(question) == 1
    assert rendered['_prep_offer']['question'] == question
    assert cb._strip_help_offer_suffix('One request. I can prepare a briefing for Dana. Another request.') == (
        'One request. I can prepare a briefing for Dana. Another request.')


@pytest.mark.parametrize('title', ['Acme <> Sotto Partnership', 'Acme Inc. decision review'])
def test_source_titled_prep_offer_is_idempotent_without_broad_suffix_matching(title):
    out, inputs = _calendar_prep_fixture()
    inputs['google']['events'][1]['summary'] = title
    rendered = cb._add_help_offers(out, {'rollout-review'}, inputs=inputs)
    first = rendered['brief_markdown']
    assert cb._add_help_offers(rendered, {'rollout-review'}, inputs=inputs)['brief_markdown'] == first
    assert first.count('I can prepare a briefing') == 1


def test_revoked_calendar_with_retained_complete_inputs_cannot_offer_prep(monkeypatch):
    import source_context
    out, inputs = _calendar_prep_fixture()
    original = out['brief_markdown']
    monkeypatch.setattr(source_context, 'allowed', lambda source: source != 'calendar')
    assert cb._add_help_offers(out, {'rollout-review'}, inputs=inputs)['brief_markdown'] == original


@pytest.mark.parametrize('field,value', [('complete', False), ('complete', 1),
                                       ('status', 'partial'), ('since', None), ('until', None)])
def test_calendar_proof_must_be_complete_and_bounded(field, value):
    out, inputs = _calendar_prep_fixture()
    row = inputs['source_results']['calendar']
    (row['coverage'] if field in ('since', 'until') else row)[field] = value
    original = out['brief_markdown']
    assert cb._add_help_offers(out, {'rollout-review'}, inputs=inputs)['brief_markdown'] == original


@pytest.mark.parametrize('zone', ['UTC', 'America/Los_Angeles'])
@pytest.mark.parametrize('date', ['2026-10-26T09:00:00', '2026-10-31T09:00:00'])
def test_pinned_naive_utc_clock_and_zoned_occurrences_select_the_same_instant(zone, date):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    out, inputs = _calendar_prep_fixture(datetime.fromisoformat(date))
    inputs['now'] = datetime.fromisoformat(date).strftime('%Y-%m-%d %H:%M:%S')
    inputs['google']['userTimezone'] = zone
    for event in inputs['google']['events']:
        for key in ('start', 'end'):
            event[key] = datetime.fromisoformat(event[key]).astimezone(ZoneInfo(zone)).isoformat()
    events = {event['id']: event for event in inputs['google']['events']}
    for action in out['actions']:
        action['meetingTime'] = events[action['contactIdentifier']]['start']
    if zone == 'America/Los_Angeles' and date.startswith('2026-10-31'):
        assert inputs['google']['events'][0]['start'].endswith('-07:00')
        assert inputs['google']['events'][1]['start'].endswith('-08:00')
    rendered = cb._add_help_offers(out, {'rollout-review'}, inputs=inputs)['brief_markdown']
    assert 'I can prepare a briefing for Cedar rollout decision review.' in rendered


def test_no_offer_on_birthday_lines_group_threads_or_first_names():
    out, allow = fixture()
    lines = out['brief_markdown'].split('\n')
    lines[2] = lines[2].replace('Asked for a proposal review.', 'Birthday today; asked for a proposal review.')
    out['brief_markdown'] = '\n'.join(lines)
    assert 'Alex Stone.”' not in cb._add_help_offers(out, allow)['brief_markdown']
    out, allow = fixture()
    group = '120363025555555555@g.us'
    out['actions'][0]['contactIdentifier'] = group
    out['brief_markdown'] = out['brief_markdown'].replace('person0@example.test', group)
    assert 'Alex Stone.”' not in cb._add_help_offers(out, allow | {group})['brief_markdown']
    out, allow = fixture()
    out['actions'][0]['contactName'] = 'Alex'
    out['brief_markdown'] = out['brief_markdown'].replace('**Alex Stone**', '**Alex**')
    assert 'to Alex.”' not in cb._add_help_offers(out, allow)['brief_markdown']


def test_malformed_ledger_rows_do_not_break_offers():
    out, allow = fixture()
    out['actions'] = []
    rows = [{'anchor_key': ['bad'], 'contact_name': 'Alex Stone', 'status': 'open',
             'contact_identifier': 'person0@example.test', 'summary': 'Review the funding proposal'}]
    out['brief_markdown'] = out['brief_markdown'].replace('Asked for a proposal review.', 'Review the funding proposal.')
    assert _offer_count(cb._add_help_offers(out, allow, rows)['brief_markdown']) == 0


def test_cross_channel_never_borrows_a_tap_identity_owned_by_another_ledger_person():
    landlord = {'anchor_key': 'lease', 'contact_name': 'John Smith', 'status': 'open',
                'contact_identifier': '+15550001000', 'summary': 'Send the signed lease agreement copy'}
    colleague = {'anchor_key': 'deck', 'contact_name': 'John Smith', 'status': 'open',
                 'contact_identifier': 'john@corp.test', 'summary': 'Share the board deck'}
    line = ('- **John Smith**<!--id:john@corp.test--><!--loop:lease-->: Send the signed lease '
            'agreement copy.\n')
    assert cb._line_open_task_keys(line, [landlord, colleague]) == set()
    md = ('## Needs Attention Now\n' + line + '## Should Handle Today\n- **John Smith**'
          '<!--id:+15550001000--><!--loop:lease-->: Send the signed lease agreement copy.\n')
    assert '+15550001000' in cb._dedupe_open_task_lines(md, [landlord, colleague])


def test_tomorrow_recorded_yesterday_is_today_and_stays_a_qualifier():
    row = {'anchor_key': 'call', 'contact_name': 'Alex Stone', 'status': 'open',
           'contact_identifier': '+15550000001', 'created_at': '2026-10-10', 'deadline': '2026-10-11',
           'summary': 'Alex wants to connect tomorrow regarding Project Cedar.'}
    line = ('**Alex Stone**<!--id:alex@example.test|ch:email--><!--loop:call-->: Alex wants to '
            'connect regarding Project Cedar.')
    assert cb._line_open_task_keys(line, [row], today='2026-10-11') == set()
    assert cb._line_open_task_keys(line, [row], today='2026-10-12') == {'call'}


def test_same_name_offer_guard_counts_canonical_ids_spacing_and_the_target_itself():
    out, allow = fixture()
    out['actions'] = out['actions'][:1]
    for peer in ({'contact_name': 'Alex  Stone', 'contact_identifier': 'other@example.test'},
                 {'contact_name': 'Alex Stone', 'canonical_id': 'c_other'},
                 {'contact_name': 'Alex Stone ', 'anchor_key': 'thread:abc'}):
        rows = [{**peer, 'status': 'open', 'summary': 'Something else'}]
        assert _offer_count(cb._add_help_offers(dict(out), allow, rows)['brief_markdown']) == 0
    same = [{'contact_name': 'Alex Stone', 'contact_identifier': 'person0@example.test',
             'status': 'open', 'summary': 'Something else'}]
    assert _offer_count(cb._add_help_offers(dict(out), allow, same)['brief_markdown']) == 1


def test_offer_guard_collapses_one_persons_linked_channels():
    out, allow = fixture()
    out['actions'] = out['actions'][:1]
    linked = [{'contact_name': 'Alex Stone', 'canonical_id': 'cid-alex', 'contact_identifier': '+15550000001',
               'status': 'open', 'summary': 'Call back'},
              {'contact_name': 'Alex Stone', 'canonical_id': 'cid-alex', 'contact_identifier': 'person0@example.test',
               'status': 'open', 'summary': 'Review the proposal'}]
    assert _offer_count(cb._add_help_offers(dict(out), allow, linked)['brief_markdown']) == 1


def test_model_offer_suffixes_are_replaced_once_and_share_the_three_offer_cap():
    out, allow = fixture()
    lines = out['brief_markdown'].splitlines()
    for index, action in enumerate(out['actions'], start=2):
        name = action['contactName']
        lines[index] += f' I can draft a reply for your review (say "Draft a reply to {name}").'
    lines[2] += ' I can draft a reply.'
    out['brief_markdown'] = '\n'.join(lines)
    original_actions = [dict(a) for a in out['actions']]
    result = cb._add_help_offers(out, allow)
    assert _offer_count(result['brief_markdown']) == 3
    assert 'for your review' not in result['brief_markdown']
    assert 'Draft a reply to' not in result['brief_markdown']
    assert result['brief_markdown'].count('proposal review.') == 4
    assert _offer_count(result['brief_markdown'].splitlines()[5]) == 0
    assert result['actions'] == original_actions
    once = result['brief_markdown']
    assert cb._add_help_offers(result, allow)['brief_markdown'] == once


def test_model_offer_cannot_bypass_missing_source_or_birthday_guard():
    out, _ = fixture()
    out['brief_markdown'] += ' I can draft a reply.'
    text = cb._add_help_offers(out, set())['brief_markdown']
    assert 'I can draft' not in text
    assert text.count('proposal review.') == 4
    out, allow = fixture()
    out['brief_markdown'] = out['brief_markdown'].replace(
        'Asked for a proposal review.', 'Birthday today. I can draft a reply for your review (say "Draft a reply to Alex Stone").', 1)
    text = cb._add_help_offers(out, allow)['brief_markdown']
    assert 'Birthday today.' in text
    assert 'I can draft' not in text.splitlines()[2]


def test_offer_suffix_cleanup_preserves_source_quotes_and_task_markers():
    quoted = '- **Alex Stone**: Alex wrote “I can draft a reply.”'
    assert cb._strip_help_offer_suffix(quoted) == quoted
    quoted = '- **Alex Stone**: Alex wrote "I can draft a reply."'
    assert cb._strip_help_offer_suffix(quoted) == quoted
    task = '- **Alex Stone**: Review the contract.'
    marker = ' <!--loop:contract--><!--id:alex@example.test|ch:email-->'
    assert cb._strip_help_offer_suffix(task + ' I can draft a reply.' + marker) == task + marker
    assert cb._strip_help_offer_suffix(task + ' I can draft the contract after you approve it.') == task + ' I can draft the contract after you approve it.'


@pytest.mark.parametrize('suffix', [
    '<!--loop:a--> I can draft a reply for your review.',
    '<!--loop:a--> I can draft a reply for your review — say “Draft a reply to Alex Stone.”',
    ' I can draft a reply for your review – say “Draft a reply to Alex Stone.”',
    ' I can draft a reply for your review; say “Draft a reply to Alex Stone.”',
    ' I can research the background — say “Research Alex Stone.”',
    ' I can research them (say “Research Alex Stone.”)',
])
def test_offer_cleanup_covers_marker_separated_and_punctuation_variants(suffix):
    task = '- **Alex Stone**<!--id:alex@example.test|ch:email-->: Asked for a proposal review.'
    marker = '<!--loop:a-->' if suffix.startswith('<!--loop:a-->') else ''
    assert cb._strip_help_offer_suffix(task + suffix) == task + marker


def test_marker_separated_old_offer_is_replaced_not_duplicated():
    out, allow = fixture()
    lines = out['brief_markdown'].splitlines()
    for index in (2, 5):
        lines[index] += '<!--loop:a--> I can draft a reply for your review.'
    out['brief_markdown'] = '\n'.join(lines)
    result = cb._add_help_offers(out, allow)['brief_markdown']
    assert _offer_count(result) == 3
    assert 'for your review' not in result
    assert result.splitlines()[2].count(DRAFT_OFFER) == 1


def test_offer_cleanup_never_removes_a_blockquoted_source_sentence():
    line = '> I can draft a reply.'
    assert cb._strip_help_offer_suffix(line) == line
    out = {'brief_markdown': '## Needs Attention Now\n' + line, 'actions': []}
    assert cb._add_help_offers(out, set())['brief_markdown'] == out['brief_markdown']


def test_offer_cleanup_requires_a_separate_sentence_not_part_of_the_ask():
    for line in ('- **Alex Stone**: Alex asked whether I can draft a reply.',
                 '- **Alex Stone**: Confirm that I can draft a reply.'):
        assert cb._strip_help_offer_suffix(line) == line


def test_model_copies_visible_task_clause_without_repeating_an_instruction():
    out, allow = fixture()
    clause = 'asked if you can introduce him to Arpan'
    out['brief_markdown'] = ('## Should Handle Today\n**Alex Stone**'
        '<!--id:person0@example.test|ch:email--> - Planning an acquisition and ' + clause + '.')
    out['actions'] = [{**out['actions'][0], 'contextAsk': clause}]
    result = cb._add_help_offers(out, allow)['brief_markdown']
    assert result.endswith(DRAFT_OFFER)
    assert result.count(clause) == 1
    # A separate task for the same person remains ineligible.
    out['actions'][0]['contextAsk'] = 'approve the acquisition contract'
    assert DRAFT_OFFER not in cb._add_help_offers(out, allow)['brief_markdown']


def test_nameless_backstop_task_is_not_repeated_by_weekly_review(monkeypatch):
    from datetime import datetime, timezone
    import ledger_io, retune_scan
    now = datetime(2026, 10, 16, 18, tzinfo=timezone.utc)
    row = {'anchor_key': 'thread:meeting', 'status': 'open', 'action_type': 'follow_up',
           'contact_name': '', 'contact_identifier': '',
           'summary': 'Alex invited you to meet on Thursday at the office',
           'created_at': '2026-10-10', 'deadline': '2026-10-15'}
    monkeypatch.setattr(cb, 'explicit_prefs', lambda: {})
    monkeypatch.setattr(ledger_io, 'load_entries', lambda: [])
    monkeypatch.setattr(retune_scan, 'scan', lambda **kw: {'stale_loops': [{
        'anchor_key': row['anchor_key'], 'name': '(unknown)', 'what': row['summary']}]})
    out = cb._append_still_open({'brief_markdown': '## Filtered\nNothing.'}, [row], now, '2026-10-16')
    assert row['summary'] in out['brief_markdown']
    out = cb._append_weekly_review(out, {'type': 'evening', '_procedure_offer': True}, now, open_ledger=[row])
    assert out['brief_markdown'].count(row['summary']) == 1
    assert '(unknown)' not in out['brief_markdown']
    # Model-supplied claims cannot bypass the ordinary identity and whole-ask rules.
    spoof = cb._normalize_output({'brief_markdown': '## Filtered\nNothing.',
                                 '_rendered_loop_lines': {'thread:meeting': 'Nothing.'}})
    assert '_rendered_loop_lines' not in spoof


def test_weekly_review_shares_one_remaining_count_even_when_cards_are_full(monkeypatch):
    from datetime import datetime, timezone
    import ledger_io, retune_scan, visual_brief
    now = datetime(2026, 10, 16, 18, tzinfo=timezone.utc)
    rows = [{'anchor_key': f'quiet-{i}', 'status': 'open', 'type': 'waiting_on',
             'contact_name': f'Alex Person{chr(65+i)}', 'contact_identifier': f'person{i}@example.test',
             'summary': f'Send the unique planning document {i} next month.',
             'created_at': '2026-10-15'} for i in range(6)]
    monkeypatch.setattr(cb, 'explicit_prefs', lambda: {})
    monkeypatch.setattr(ledger_io, 'load_entries', lambda: [])
    monkeypatch.setattr(retune_scan, 'scan', lambda **kw: {'stale_loops': [
        {'anchor_key': row['anchor_key'], 'name': row['contact_name'], 'what': row['summary']}
        for row in rows]})
    out = cb._append_still_open({'brief_markdown': '## Filtered\nNothing.'}, rows, now, '2026-10-16')
    assert '6 other open loops' in out['brief_markdown']
    out = cb._append_weekly_review(out, {'type': 'evening', '_procedure_offer': True}, now, open_ledger=rows)
    monkeypatch.setattr(visual_brief, 'fits', lambda *a, **kw: False)
    out = cb._expand_quiet_loops(out, rows, now, '2026-10-16')
    assert '3 other open loops' in out['brief_markdown']
    assert 'more to review' not in out['brief_markdown']
    assert out['brief_markdown'].count("Ask me what's still open.") == 1
    assert all(out['brief_markdown'].count(row['summary']) == 1 for row in rows[:3])


def test_coherence_flags_split_email_prep_and_title_only_calendar_action():
    out, inputs = _calendar_prep_fixture()
    meeting = next(a for a in out['actions'] if a.get('helpKind') == 'prep')
    email = 'maya@vendor.example.test'
    name = meeting['contactName']
    ask = meeting['contextAsk']
    out['brief_markdown'] = (f'## Should Handle Today\n**{name}**<!--id:{email}|ch:email-->: {ask}.')
    out['actions'] = [{**meeting, 'type': 'reply', 'channel': 'gmail',
                       'contactIdentifier': email},
                      {**meeting, 'contactName': 'Pilot pricing review',
                       'contextAsk': 'Pilot pricing review'}]
    findings = cb._help_offer_coherence_violations(out, {email, meeting['contactIdentifier']})
    assert any('non-meeting action' in finding for finding in findings)
    assert any('do not invent a meeting or change the original task' in finding for finding in findings)
    result = cb._add_help_offers(out, {email, meeting['contactIdentifier']}, inputs=inputs)
    assert '_prep_offer' not in result
    out['actions'][0]['helpKind'] = 'none'
    assert not cb._help_offer_coherence_violations(out, {email, meeting['contactIdentifier']})


def test_explicit_calendar_prep_shape_passes_coherence_and_exact_offer_binding():
    out, inputs = _calendar_prep_fixture()
    meeting = next(a for a in out['actions'] if a.get('helpKind') == 'prep')
    out['actions'] = [meeting]
    identifier = meeting['contactIdentifier']
    out['brief_markdown'] = (f"## Should Handle Today\n**{meeting['contactName']}**"
        f"<!--id:{identifier}|ch:calendar-->: {meeting['contextAsk']}.")
    assert cb._help_offer_coherence_violations(out, {identifier}) == []
    result = cb._add_help_offers(out, {identifier}, inputs=inputs)
    assert result['_prep_offer']['person'] == meeting['contactName']
    assert result['_prep_offer']['meeting']['event_id'] == identifier


def _email_only_calendar_prep_fixture():
    out, inputs = _calendar_prep_fixture()
    action = out['actions'][0]
    event = next(e for e in inputs['google']['events'] if e['id'] == action['contactIdentifier'])
    external = next(a for a in event['attendees'] if not a.get('self'))
    external.pop('displayName', None)
    external.pop('name', None)
    action['contactName'] = external['email']
    out['actions'] = [action]
    out['brief_markdown'] = (f"## Should Handle Today\n**{event['summary']}**"
        f"<!--id:{event['id']}|ch:calendar-->: {action['contextAsk']}.")
    return out, inputs, action, event, external


@pytest.mark.parametrize('placeholder', [None, 'displayName', 'name'])
def test_email_only_prep_uses_exact_source_attendee_and_visible_meeting(placeholder):
    out, inputs, action, event, external = _email_only_calendar_prep_fixture()
    action['contactName'] = external['email'].upper()
    if placeholder:
        external[placeholder] = external['email'].upper()
    result = cb._add_help_offers(out, {event['id']}, inputs=inputs)
    assert result['_prep_offer'] == {
        'person': external['email'].lower(),
        'question': f"I can prepare a briefing for {event['summary']}. Want me to?",
        'meeting': {'event_id': event['id'], 'start': event['start']}, 'ambiguous': False}
    assert result['brief_markdown'].endswith(result['_prep_offer']['question'])


@pytest.mark.parametrize('change', ['second_external', 'wrong_email', 'source_named', 'named_duplicate',
    'different_email_label', 'contradictory_alias', 'invented_name', 'company_name', 'no_title', 'title_only_in_marker', 'no_marker',
    'wrong_marker', 'partial_ask', 'draft', 'research', 'partial_coverage'])
def test_inline_email_only_prep_never_relaxes_other_offer_guards(change):
    out, inputs, action, event, external = _email_only_calendar_prep_fixture()
    if change == 'second_external':
        event['attendees'].append({'email': 'other@cedar.test'})
    elif change == 'named_duplicate':
        event['attendees'].append({**external, 'displayName': 'Different Person'})
    elif change == 'wrong_email':
        action['contactName'] = 'different@cedar.test'
    elif change == 'source_named':
        external['displayName'] = 'Dana Rivers'
    elif change == 'different_email_label':
        external['displayName'] = 'different@cedar.test'
    elif change == 'contradictory_alias':
        external['displayName'] = external['email']
        external['name'] = 'Different Person'
    elif change == 'invented_name':
        action['contactName'] = 'Invented Person'
    elif change == 'company_name':
        action['contactName'] = 'Cedar Company'
    elif change == 'no_title':
        out['brief_markdown'] = out['brief_markdown'].replace(event['summary'], 'Upcoming meeting')
    elif change == 'title_only_in_marker':
        out['brief_markdown'] = out['brief_markdown'].replace(event['summary'], f"<!--{event['summary']}-->")
    elif change == 'no_marker':
        out['brief_markdown'] = out['brief_markdown'].replace(f"<!--id:{event['id']}|ch:calendar-->", '')
    elif change == 'wrong_marker':
        out['brief_markdown'] = out['brief_markdown'].replace(f"id:{event['id']}|", 'id:routine-checkin|')
    elif change == 'partial_ask':
        out['brief_markdown'] = out['brief_markdown'].replace(action['contextAsk'], 'Review the options')
    elif change in ('draft', 'research'):
        action['helpKind'] = change
    elif change == 'partial_coverage':
        inputs['source_results']['calendar']['complete'] = False
    result = cb._render_help_offers(out, {event['id'], 'routine-checkin'}, inputs=inputs)
    assert '_prep_offer' not in result
    assert _offer_count(result['brief_markdown']) == 0
    # The public entry may add a separate source-titled offer, but never relax identity.
    result = cb._add_help_offers(out, {event['id'], 'routine-checkin'}, inputs=inputs)
    standalone = change in {'invented_name', 'company_name', 'no_title', 'title_only_in_marker',
                           'no_marker', 'wrong_marker', 'partial_ask'}
    assert ('_prep_offer' in result) is standalone
    if standalone:
        assert result['_prep_offer']['person'] == external['email']
        assert result['_prep_offer']['meeting']['event_id'] == event['id']
        assert result['brief_markdown'].count(result['_prep_offer']['question']) == 1


def test_email_only_prep_deduplicates_identical_source_attendee_email():
    out, inputs, action, event, external = _email_only_calendar_prep_fixture()
    event['attendees'].append(dict(external))
    assert cb._add_help_offers(out, {event['id']}, inputs=inputs)['_prep_offer']['person'] == external['email']


def test_email_only_prep_coherence_reuses_exact_source_binding():
    out, inputs, action, event, external = _email_only_calendar_prep_fixture()
    out['brief_markdown'] = out['brief_markdown'].replace(event['summary'], 'Cedar Review')
    event['summary'] = 'Cedar Review'
    assert cb._help_offer_coherence_violations(out, {event['id']}, inputs) == []
    external['displayName'] = 'Dana Rivers'
    assert cb._help_offer_coherence_violations(out, {event['id']}, inputs)


@pytest.mark.parametrize('title', ['Cedar Review', 'pricing review: tier 2'])
@pytest.mark.parametrize('failure', ['coverage', 'identity', 'title', 'ask'])
def test_failed_email_prep_coherence_preserves_email_without_full_name_repair(title, failure):
    out, inputs, action, event, external = _email_only_calendar_prep_fixture()
    out['brief_markdown'] = out['brief_markdown'].replace(event['summary'], title)
    event['summary'] = title
    if failure == 'coverage':
        inputs['source_results']['calendar']['complete'] = False
    elif failure == 'identity':
        action['contactName'] = 'other@cedar.test'
    elif failure == 'title':
        out['brief_markdown'] = out['brief_markdown'].replace(title, 'Upcoming meeting')
    elif failure == 'ask':
        out['brief_markdown'] = out['brief_markdown'].replace(action['contextAsk'], 'Review')
    findings = cb._help_offer_coherence_violations(out, {event['id']}, inputs)
    assert len(findings) == 1
    assert 'email-only prep' in findings[0]
    assert 'Preserve the exact source email as contactName' in findings[0]
    assert 'If the same source attendee supplies a full name, use that exact source name' in findings[0]
    assert 'use helpKind none' in findings[0]
    assert 'action name does not match a complete visible source-backed name' not in findings[0]
    assert '_prep_offer' not in cb._render_help_offers(out, {event['id']}, inputs=inputs)


def test_prep_after_three_drafts_is_still_the_single_handoff_and_failure_keeps_drafts():
    import copy
    out, inputs = _calendar_prep_fixture()
    drafts, allow = fixture()
    drafts['brief_markdown'] += '\n' + out['brief_markdown'].split('\n', 1)[1]
    drafts['actions'] += out['actions']
    before = copy.deepcopy(drafts)
    result = cb._add_help_offers(drafts, allow | {'rollout-review'}, inputs=inputs)
    assert _offer_count(result['brief_markdown']) == 1
    assert result['_prep_offer']['ambiguous'] is False
    assert result['actions'] == before['actions']
    for name in ('Alex Stone', 'Dana Reed', 'Sam Green', 'Morgan Bell'):
        assert name in result['brief_markdown']
    # A rejected prep cannot consume ordinary useful offers.
    inputs['source_results']['calendar']['complete'] = False
    fallback = cb._add_help_offers(before, allow | {'rollout-review'}, inputs=inputs)
    assert '_prep_offer' not in fallback
    assert _offer_count(fallback['brief_markdown']) == 3


@pytest.mark.parametrize('conflict', ['', 'other_email', 'other_event', 'no_event', 'same_name_other_email'])
def test_prep_explicit_event_and_attendee_fields_resolve_only_the_same_source(conflict):
    out, inputs = _calendar_prep_fixture()
    action = out['actions'][0]
    target = inputs['google']['events'][1]
    email = target['attendees'][1]['email']
    action['eventId'] = target['id']
    action['contactIdentifier'] = email
    out['brief_markdown'] = out['brief_markdown'].replace('id:rollout-review|', f'id:{email}|')
    if conflict == 'other_email':
        action['contactIdentifier'] = 'stranger@example.test'
    elif conflict == 'other_event':
        action['contactIdentifier'] = inputs['google']['events'][0]['id']
    elif conflict == 'no_event':
        del action['eventId']
    elif conflict == 'same_name_other_email':
        target['attendees'][1]['email'] = 'different@example.test'
    result = cb._add_help_offers(out, {email, 'rollout-review'}, inputs=inputs)
    if conflict:
        assert '_prep_offer' not in result
    else:
        assert result['_prep_offer']['meeting']['event_id'] == target['id']
        assert result['_prep_offer']['ambiguous'] is False


@pytest.mark.parametrize('named_source', [False, True])
def test_selected_occurrence_survives_missing_paragraph_without_trusting_model_name(named_source):
    import copy
    out, inputs, action, event, external = _email_only_calendar_prep_fixture()
    action['contactName'] = 'Dana Rivers' if named_source else 'Untrusted Company Label'
    if named_source:
        external['displayName'] = 'Dana Rivers'
    event['attendees'].append({'email': 'colleague@inside.test', 'displayName': 'Internal Colleague'})
    out['brief_markdown'] = ('# Morning brief\n## Needs Attention Now\n'
        '**Alex Stone**: Review the separate contract.\n'
        '**Sam Green**: Confirm the venue.\n'
        '**Morgan Bell**: Approve the invoice.\n'
        '## Coming Up\n- ' + event['summary'] + '\n## Filtered\nNothing else.')
    before = copy.deepcopy(out)
    rendered = cb._add_help_offers(out, {event['id']}, inputs=inputs)
    offer = rendered['_prep_offer']
    assert offer['person'] == external['email']
    assert offer['meeting'] == {'event_id': event['id'], 'start': event['start']}
    assert offer['ambiguous'] is False
    assert rendered['brief_markdown'].count(offer['question']) == 1
    assert rendered['brief_markdown'].index(offer['question']) < rendered['brief_markdown'].index('## Coming Up')
    assert 'Untrusted Company Label' not in rendered['brief_markdown']
    assert rendered['actions'] == before['actions']
    import visual_brief
    from chatfmt import to_imessage
    deck = visual_brief.build(rendered['brief_markdown'])
    assert deck and visual_brief.fits(deck)
    cards = '\n'.join(block['text'] for card in deck['cards'] for block in card['blocks'])
    assert offer['question'] in cards
    assert offer['question'] in to_imessage(rendered['brief_markdown'])
    assert cb._add_help_offers(copy.deepcopy(rendered), {event['id']}, inputs=inputs) == rendered


@pytest.mark.parametrize('change', ['partial', 'stale', 'cancelled', 'declined', 'different_owner',
    'other_contact', 'unknown_event', 'no_selection', 'two_events', 'two_attendees', 'group', 'not_allowed'])
def test_standalone_prep_keeps_source_authority_and_single_occurrence(change):
    import copy
    out, inputs, action, event, external = _email_only_calendar_prep_fixture()
    out['brief_markdown'] = '## Coming Up\nA meeting.'
    allow = {event['id'], 'routine-checkin'}
    if change == 'partial':
        inputs['source_results']['calendar']['complete'] = False
    elif change == 'stale':
        inputs['source_results']['calendar']['coverage']['until'] = inputs['now']
    elif change == 'cancelled':
        event['status'] = 'cancelled'
    elif change == 'declined':
        event['my_response'] = 'declined'
    elif change == 'different_owner':
        inputs['google']['userEmail'] = 'stranger@elsewhere.test'
        event['calendar_id'] = 'shared'
        event['_owner_participates'] = False
    elif change == 'other_contact':
        action['eventId'] = event['id']
        action['contactIdentifier'] = 'stranger@outside.test'
        allow.add(action['contactIdentifier'])
    elif change == 'unknown_event':
        action['contactIdentifier'] = 'unknown'
        allow.add('unknown')
    elif change == 'no_selection':
        action['helpKind'] = 'none'
    elif change == 'two_events':
        other = copy.deepcopy(action)
        other['contactIdentifier'] = 'routine-checkin'
        other['contactName'] = 'Sam Green'
        out['actions'].append(other)
    elif change == 'two_attendees':
        event['attendees'].append({'email': 'other@outside.test'})
    elif change == 'group':
        action['groupId'] = 'group'
    elif change == 'not_allowed':
        allow = set()
    rendered = cb._add_help_offers(out, allow, inputs=inputs)
    assert '_prep_offer' not in rendered
    assert 'Want me to?' not in rendered['brief_markdown']


def test_standalone_offer_does_not_rewrite_a_conflicting_paragraph_or_obligation():
    out, inputs, action, event, external = _email_only_calendar_prep_fixture()
    action['contactName'] = 'Dana Rivers'
    original = '## Should Handle Today\nReview the separate contract.\n## Coming Up\nRoutine check-in.'
    out['brief_markdown'] = original
    rendered = cb._add_help_offers(out, {event['id']}, inputs=inputs)
    assert 'Review the separate contract.' in rendered['brief_markdown']
    assert 'Routine check-in.' in rendered['brief_markdown']
    assert '\n\n' + rendered['_prep_offer']['question'] in rendered['brief_markdown']
    assert rendered['_prep_offer']['person'] == external['email']
    assert rendered['_prep_offer']['meeting']['event_id'] == event['id']


@pytest.mark.parametrize('model_name', [None, 'Dana Rivers'])
@pytest.mark.parametrize('location', ['filtered', 'quote', 'embedded', 'coming_up', 'duplicate'])
def test_standalone_prep_never_adopts_hidden_quoted_or_embedded_offer(location, model_name):
    out, inputs, action, event, external = _email_only_calendar_prep_fixture()
    question = f"I can prepare a briefing for {event['summary']}. Want me to?"
    if model_name:
        action['contactName'] = model_name
    bodies = {'filtered': '## Filtered\n' + question,
              'quote': '## Should Handle Today\n> ' + question,
              'embedded': '## Should Handle Today\nReview another contract. ' + question,
              'coming_up': '## Coming Up\n' + question,
              'duplicate': '## Should Handle Today\n' + question + '\n' + question}
    out['brief_markdown'] = bodies[location]
    import copy
    assert '_prep_offer' not in cb._render_selected_prep(copy.deepcopy(out), {event['id']}, inputs)
    result = cb._add_help_offers(out, {event['id']}, inputs=inputs)
    if location == 'embedded':
        # Existing suffix cleanup removes the old offer, then the separate source offer is rendered.
        assert result['_prep_offer']['person'] == external['email']
        assert 'Review another contract.\n\n' + question in result['brief_markdown']
    else:
        assert '_prep_offer' not in result


def test_standalone_prep_upgrades_only_exact_visible_legacy_line():
    out, inputs, action, event, external = _email_only_calendar_prep_fixture()
    legacy = f"I can prepare a briefing for {event['summary']}."
    out['brief_markdown'] = '## Should Handle Today\n' + legacy + '\n'
    result = cb._add_help_offers(out, {event['id']}, inputs=inputs)
    assert result['brief_markdown'].count(legacy) == 1
    assert result['brief_markdown'].endswith(result['_prep_offer']['question'] + '\n')


@pytest.mark.parametrize('shape', ['event', 'attendee', 'legacy_type'])
def test_standalone_prep_resolves_supported_action_shapes(shape):
    out, inputs, action, event, external = _email_only_calendar_prep_fixture()
    action['contactName'] = 'Dana Rivers'
    out['brief_markdown'] = '## Coming Up (next 48h)\nA meeting.'
    allow = {event['id'], external['email']}
    if shape == 'attendee':
        action['eventId'] = event['id']
        action['contactIdentifier'] = external['email']
    elif shape == 'legacy_type':
        action['action_type'] = action.pop('type')
        action.pop('channel')
    result = cb._add_help_offers(out, allow, inputs=inputs)
    assert result['_prep_offer']['person'] == external['email']
    assert result['brief_markdown'].index('Want me to?') < result['brief_markdown'].index('## Coming Up')


def test_standalone_prep_rejects_configured_owner_mismatch(monkeypatch):
    out, inputs, action, event, external = _email_only_calendar_prep_fixture()
    monkeypatch.setattr(cb, 'configured_user_email', lambda: 'different@inside.test')
    out['brief_markdown'] = '## Coming Up\nA meeting.'
    assert '_prep_offer' not in cb._add_help_offers(out, {event['id']}, inputs=inputs)


def test_standalone_prep_rejects_named_source_conflict():
    out, inputs, action, event, external = _email_only_calendar_prep_fixture()
    external['displayName'] = 'Dana Rivers'
    action['contactName'] = 'Dana Rivera'
    out['brief_markdown'] = '## Coming Up\nA meeting.'
    assert '_prep_offer' not in cb._add_help_offers(out, {event['id']}, inputs=inputs)


@pytest.mark.parametrize('bounds', [None, [], {}, {'since': 'invalid', 'until': 'invalid'}])
def test_prep_requires_gather_coverage_envelope_even_with_flat_dates(bounds):
    out, inputs = _calendar_prep_fixture()
    row = inputs['source_results']['calendar']
    row.update(row['coverage'])  # legacy synthetic dates must not override missing source proof
    row['coverage'] = bounds
    rendered = cb._add_help_offers(out, {'rollout-review'}, inputs=inputs)
    assert '_prep_offer' not in rendered
    assert 'I can prepare a briefing' not in rendered['brief_markdown']


def test_gather_receipt_supports_source_occurrence_offer_without_named_paragraph():
    out, inputs = _calendar_prep_fixture()
    target = inputs['google']['events'][1]
    target['attendees'][1].pop('displayName', None)
    target['attendees'][1].pop('name', None)
    out['brief_markdown'] = '## Should Handle Today\nA separate obligation remains.\n'
    rendered = cb._add_help_offers(out, {'rollout-review'}, inputs=inputs)
    assert rendered['_prep_offer']['meeting']['event_id'] == target['id']
    assert rendered['_prep_offer']['person'] == target['attendees'][1]['email']
    assert 'A separate obligation remains.' in rendered['brief_markdown']
    assert rendered['brief_markdown'].count('Want me to?') == 1


@pytest.mark.parametrize('named', [True, False])
def test_native_calendar_gather_receipt_reaches_brief_offer(named, monkeypatch, tmp_path):
    """Freeze only Google responses; run real pagination, normalization and receipt serialization."""
    import json
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    import gather_google
    from source_context import source_result

    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@inside.test')
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    start = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    person = {'email': 'dana@cedar.test', 'responseStatus': 'accepted'}
    if named:
        person['displayName'] = 'Dana Rivers'
    raw = {'id': 'native-source-occurrence', 'summary': 'Cedar rollout decision review',
           'start': {'dateTime': start}, 'end': {'dateTime': start},
           'organizer': {'email': 'owner@inside.test', 'self': True},
           'attendees': [{'email': 'owner@inside.test', 'self': True, 'responseStatus': 'accepted'}, person]}
    calls = []

    class Service:
        def calendarList(self):  # noqa: N802 - Google protocol
            return SimpleNamespace(list=lambda **kwargs: SimpleNamespace(execute=lambda: {
                'items': [{'id': 'owner@inside.test', 'primary': True, 'accessRole': 'owner'}]}))

        def events(self):
            return self

        def list(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(execute=lambda: {'items': [raw]})

    observation = {}
    events = gather_google.gather_calendar('unused', service=Service(), observation=observation)
    receipt = source_result('ok' if observation['complete'] else 'partial',
                            complete=observation['complete'], **observation['coverage'])
    inputs = json.loads(json.dumps({'google': {'events': events, 'userEmail': 'owner@inside.test'},
                                   'source_results': {'calendar': receipt}}))
    event = events[0]
    assert calls and event['id'].startswith('gcal_')
    out = {'brief_markdown': '## Should Handle Today\nA separate obligation.\n', 'actions': [{
        'type': 'meeting_prep', 'channel': 'calendar', 'contactName': 'Dana Rivers',
        'contactIdentifier': event['id'], 'helpKind': 'prep'}]}
    rendered = cb._add_help_offers(out, cb.build_tap_target_allowlist(inputs), inputs=inputs)
    assert rendered['_prep_offer']['meeting'] == {'event_id': event['id'], 'start': start}
    assert rendered['_prep_offer']['person'] == 'dana@cedar.test'
    assert rendered['brief_markdown'].count('Want me to?') == 1
