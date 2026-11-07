"""Canonical birthday capture → permitted query → pulse; no inferred missed obligation."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import brief_validate
import continuity_resolve as cr
import knowledge as kg
import ledger_io
import loops_query
import prewarm_graph
import relationship_pulse as pulse
from ledger_provenance import source_loop_sources
from render_local import _format_birthdays

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
PHONE = '+14155551234'


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    monkeypatch.delenv('SOTTO_PROACTIVE_CAPTURED_SOURCES', raising=False)
    local = {'contacts': [{'name': 'Maya Chen', 'phones': [PHONE], 'emails': [], 'birthday': '10-04'}]}
    prewarm_graph.sync_contacts(local)
    person = kg.parse_person_file(Path(kg.find_person_file(identifier=PHONE)).read_text())
    return tmp_path, local, person.canonical_id


def important(context, vip=False, reciprocal=True):
    root, local, cid = context
    days = [(NOW - timedelta(days=n)).date().isoformat() for n in [1, 3, 9, 11, 17, 19]]
    history = {cid: {'name': 'Maya Chen', 'channels': {'imessage': days[0]},
        'importance_evidence': {'active_days': days, 'sent_days': days[::2],
                               'received_days': days[1::2] if reciprocal else []}}}
    (root / 'knowledge/relationship_state.json').write_text(json.dumps({'history': history}))
    (root / 'preferences.json').write_text(json.dumps({'explicit': {'vip_people': ['Maya Chen'] if vip else []}}))


def capture(local, summary='Send Maya a belated birthday greeting', **extra):
    action = {'type': 'follow_up', 'channel': 'imessage', 'contactName': 'Maya Chen',
              'contactIdentifier': PHONE, 'contextSummary': summary, **extra}
    return cr.resolve({'today': '2026-10-10', 'local': local, 'new_actions': [action]}, NOW,
                      resolve_existing=False)


def query(permitted=('contacts', 'imessage')):
    import os
    return loops_query.query_permitted_rows(os.environ['SOTTO_DATA'], list(permitted), now=NOW,
                                            include_request_context=True)


def test_ordinary_birthdays_visible_without_automatic_debt(context):
    root, local, cid = context
    assert 'Maya Chen' in _format_birthdays(local, today=(NOW - timedelta(days=6)).date())
    assert capture(local)['active'] == []
    assert query()['counts']['you_owe'] == 0


@pytest.mark.parametrize('vip', [False, True])
def test_important_birthday_capture_query_pulse_is_optional_and_idempotent(context, vip):
    important(context, vip=vip)
    _, local, cid = context
    captured = capture(local)
    assert len(captured['active']) == 1
    row = captured['active'][0]
    assert row['resolution_mode'] == 'birthday_greeting' and row['source'] == 'contacts'
    assert 'missed' not in row['summary'].lower() and 'belated' not in row['summary'].lower()
    assert source_loop_sources(row) == ({'contacts'} if vip else {'contacts', 'imessage'})
    assert all(ref['sourceType'] == 'contacts' for ref in row['source_refs'])
    assert not brief_validate.is_urgent({**row, 'deadline': '2026-10-01', 'chased_count': 2}, '2026-10-10')
    assert not ledger_io.park_candidate({**row, 'created_at': '2026-01-01'}, '2026-10-10')
    capture(local)
    assert len(cr._load_items()) == 1
    view = query()
    assert len(view['you_owe']) == 1 and view['you_owe'][0]['birthday_courtesy']
    assert not view['you_owe'][0]['overdue'] and not view['you_owe'][0]['deadline']
    rendered = pulse.with_commitments({'attention_queue': [], 'pulse_markdown': ''}, view)['pulse_markdown']
    assert 'Maya Chen' in rendered and 'Optional, no urgency' in rendered
    assert 'Your follow-up' not in rendered and 'due ' not in rendered and 'belated' not in rendered
    assert query(('imessage',))['you_owe'] == []
    if not vip:
        assert query(('contacts',))['you_owe'] == []


def test_one_way_volume_and_ambiguous_card_cannot_supply_importance(context):
    important(context, reciprocal=False)
    assert capture(context[1])['active'] == []
    important(context, vip=True)
    context[1]['contacts'][0]['emails'] = ['unresolved@example.com']
    assert capture(context[1])['active'] == []


def test_revoked_activity_and_regrant_do_not_supply_importance(context):
    important(context)
    root, local, cid = context
    config = root / 'config'
    config.mkdir(exist_ok=True)
    path = config / 'source-state.json'
    path.write_text(json.dumps({'sources': {'imessage': {'status': 'disabled'}}}))
    assert capture(local)['active'] == []
    path.write_text(json.dumps({'sources': {'imessage': {'status': 'ok',
        'consent_regrant_epoch': NOW.timestamp()}}}))
    assert capture(local)['active'] == []


def test_optional_greeting_does_not_overwrite_a_real_request(context):
    important(context, vip=True)
    root, local, cid = context
    source = {'rowid': 7, 'handle': PHONE, 'is_from_me': False,
              'timestamp': '2026-10-10 09:00:00', 'text': 'Please buy a gift for the birthday party.'}
    local['imessage'] = [source]
    capture(local, 'Buy a gift for the birthday party', source='imessage',
            evidence=[{'sourceType': 'imessage', 'sourceId': '7', 'snippet': source['text']}])
    capture(local)
    rows = list(cr._load_items().values())
    assert len(rows) == 2 and any(row['summary'] == 'Buy a gift for the birthday party' for row in rows)
    view = query()
    assert len(view['you_owe']) == 2
    assert sum(bool(row['birthday_courtesy']) for row in view['you_owe']) == 1
    rendered = pulse.with_commitments({'attention_queue': []}, view)
    assert len(rendered['commitments']) == 2
    assert 'Buy a gift for the birthday party' in rendered['pulse_markdown']
    assert rendered['pulse_markdown'].index('Buy a gift') < rendered['pulse_markdown'].index('Optional birthday')
    gift = next(line for line in rendered['pulse_markdown'].splitlines() if 'Buy a gift' in line)
    assert 'Optional' not in gift
    assert 'Optional, no urgency' in rendered['pulse_markdown']


def test_explicit_owner_wish_remains_a_task_without_importance_or_contacts(context):
    root, local, cid = context
    row = {'anchor_key': 'owner-birthday', 'status': 'open', 'action_type': 'follow_up',
           'channel': 'manual', 'source': 'user_added', 'source_thread_id': 'manual:birthday',
           'owner_event_id': 'owner:birthday', 'contact_name': 'Maya Chen',
           'contact_identifier': PHONE, 'canonical_id': cid, 'summary': 'Wish Maya a happy birthday',
           'created_at': NOW.isoformat(), 'deadline': '2026-10-11'}
    cr._persist(row, new_owner_content=True)
    view = query(())
    assert len(view['you_owe']) == 1 and not view['you_owe'][0]['birthday_courtesy']
    assert brief_validate.is_urgent(row, '2026-10-10')


@pytest.mark.parametrize('text, closed', [('The Cedar deck is attached.', False),
                                         ('Happy birthday Maya!', True)])
def test_only_actual_greeting_settles_optional_loop(context, text, closed):
    important(context, vip=True)
    root, local, cid = context
    row = capture(local)['active'][0]
    from delivery_effects import loop_version
    update = {'loopId': row['anchor_key'], 'loopVersion': loop_version(row), 'status': 'resolved',
              'evidence': [{'sourceType': 'imessage', 'sourceId': '99', 'snippet': text}]}
    update['criticReceipt'] = cr.record_critic_approval(update)
    fresh = {**local, 'imessage': [{'rowid': 99, 'handle': PHONE, 'is_from_me': True,
             'timestamp': '2026-10-10 13:00:00', 'text': text}]}
    cr.resolve({'today': '2026-10-10', 'local': fresh, 'loopUpdates': [update]},
               NOW + timedelta(hours=2))
    stored = cr._load_items()[row['anchor_key']]
    assert (stored['status'] == 'resolved') is closed


def test_shared_native_alias_cannot_borrow_vip_birthday(context):
    important(context, vip=True)
    root, local, cid = context
    other = kg.PersonFile(canonical_id='c_111111111111', name='Other Person', identifiers=[PHONE])
    kg.write_person_file(str(root / 'knowledge/people' / (other.canonical_id + '.md')), other, NOW)
    assert capture(local)['active'] == []


def test_call_activity_dependencies_are_retained_and_revocable(context):
    important(context)
    root, local, cid = context
    path = root / 'knowledge/relationship_state.json'
    state = json.loads(path.read_text())
    state['history'][cid]['channels'].update(calls='2026-10-09', whatsapp_calls='2026-10-08')
    path.write_text(json.dumps(state))
    row = capture(local)['active'][0]
    assert source_loop_sources(row) == {'contacts', 'imessage', 'calls', 'whatsapp_calls'}
    assert query()['you_owe'] == []
    visible = query(('contacts', 'imessage', 'calls', 'whatsapp_calls'))
    assert len(visible['you_owe']) == 1
    assert 'birthday_courtesy' not in loops_query.query_permitted_rows(
        str(root), ['contacts', 'imessage', 'calls', 'whatsapp_calls'], now=NOW)['you_owe'][0]


def test_named_courtesy_cannot_rewrite_an_ordinary_request(context):
    important(context, vip=True)
    root, local, cid = context
    source = {'rowid': 7, 'handle': PHONE, 'is_from_me': False,
              'timestamp': '2026-10-10 09:00:00', 'text': 'Please return the signed contract.'}
    local['imessage'] = [source]
    original = capture(local, 'Return the signed contract', source='imessage',
        evidence=[{'sourceType': 'imessage', 'sourceId': '7', 'snippet': source['text']}])['active'][0]
    capture(local, loopId=original['anchor_key'])
    stored = cr._load_items()
    assert stored[original['anchor_key']]['summary'] == 'Return the signed contract'
    assert len(stored) == 2


@pytest.mark.parametrize('source, field', [('imessage', 'imessage'), ('gmail', 'emails')])
def test_known_greeting_on_birthday_prevents_later_optional_capture(context, source, field):
    important(context, vip=True)
    root, local, cid = context
    if source == 'gmail':
        # The same canonical contact owns its email; no name-only cross-channel completion.
        person = kg.parse_person_file(Path(kg.find_person_file(identifier=PHONE)).read_text())
        person.identifiers.append('maya@example.com')
        kg.write_person_file(str(root / 'knowledge/people' / (cid + '.md')), person, NOW)
        local['contacts'][0]['emails'] = ['maya@example.com']
        local[field] = [{'id': 'birthday-mail', 'from': 'owner@example.com', 'to': 'maya@example.com',
                         'labelIds': ['SENT'], 'date': '2026-10-04T12:00:00Z',
                         'snippet': 'Happy birthday Maya! Hope the party is fun.'}]
    else:
        local[field] = [{'rowid': 104, 'handle': PHONE, 'is_from_me': True,
                         'timestamp': '2026-10-04 12:00:00',
                         'text': 'Happy birthday Maya! Hope the party is fun.'}]
    assert capture(local)['active'] == []
    assert query()['you_owe'] == []


def test_citing_already_sent_greeting_cannot_create_a_new_request(context):
    important(context, vip=True)
    root, local, cid = context
    text = 'Happy birthday Maya!'
    local['imessage'] = [{'rowid': 104, 'handle': PHONE, 'is_from_me': True,
                         'timestamp': '2026-10-04 12:00:00', 'text': text}]
    assert capture(local, evidence=[{'sourceType': 'imessage', 'sourceId': '104',
                                    'snippet': text}])['active'] == []


def test_legacy_source_backed_wish_is_an_ordinary_task_in_every_view(context):
    """Rows saved before the courtesy mode existed were admitted only with an observed request."""
    root, local, cid = context
    cr._persist({'status': 'open', 'action_type': 'follow_up', 'contact_identifier': PHONE,
                 'contact_name': 'Maya Chen', 'channel': 'imessage', 'source': 'imessage',
                 'source_message_id': 'm1', 'source_thread_id': 't1', 'anchor_key': 'imessage:maya',
                 'created_at': '2026-10-08 10:00:00', 'canonical_id': cid,
                 'summary': 'Send Maya happy birthday wishes from the team'})
    brief = [row['contact_name'] for row in ledger_io.active_entries(ledger_io.load_entries(), NOW)]
    view = query()['you_owe']
    assert brief == ['Maya Chen'] and [row['name'] for row in view] == brief
    assert not view[0]['birthday_courtesy'] and view[0]['what'].startswith('Send Maya')


def test_unclassifiable_greeting_provenance_stays_visible_not_optional(context):
    root, local, cid = context
    row = {'status': 'open', 'action_type': 'follow_up', 'contact_identifier': PHONE,
           'contact_name': 'Maya Chen', 'channel': 'imessage', 'source': 'contacts',
           'resolution_mode': 'birthday_greeting', 'birthday_importance_sources': ['not-a-source'],
           'summary': 'Optional birthday greeting (birthday 2026-10-04)'}
    assert not ledger_io.is_optional_birthday_courtesy(row)
    assert not ledger_io.birthday_greeting_retired(row, '2027-06-01')


def test_optional_greeting_expires_after_the_capture_window(context):
    important(context, vip=True)
    _, local, _ = context
    row = capture(local)['active'][0]
    owner = {'anchor_key': 'owner-birthday', 'status': 'open', 'action_type': 'follow_up',
             'channel': 'manual', 'source': 'user_added', 'source_thread_id': 'manual:birthday',
             'owner_event_id': 'owner:birthday', 'contact_name': 'Maya Chen',
             'summary': 'Wish Maya a happy birthday', 'created_at': '2026-10-04 00:00:00',
             'resolution_mode': 'birthday_greeting'}
    cr._persist(owner, new_owner_content=True)
    from relationship_importance import WINDOW_DAYS
    birthday = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    for days, status in ((WINDOW_DAYS, 'open'), (WINDOW_DAYS + 1, 'expired'), (300, None)):
        later = birthday + timedelta(days=days)
        out = cr.resolve({'today': later.date().isoformat(), 'local': {}}, later)
        stored = cr._load_items()
        # Expired through the ordinary terminal path, then removed after its normal retention.
        assert (stored.get(row['anchor_key']) or {}).get('status') == status
        assert stored['owner-birthday']['status'] == 'open'  # owner words never retire
        if status != 'open':
            assert row['anchor_key'] not in {item['anchor_key'] for item in out['active']}
        if status == 'expired':
            assert stored[row['anchor_key']]['resolution'] == 'birthday_window_passed'
            assert query()['you_owe'] == [] or all(not r['birthday_courtesy'] for r in query()['you_owe'])


def test_brief_and_pulse_share_one_birthday_visibility_rule(context):
    """One predicate: a legacy sourced row stays visible; a greeting that lost importance hides."""
    from render_local import _format_action_ledger
    important(context, vip=True)
    root, local, cid = context
    greeting = capture(local)['active'][0]
    cr._persist({'status': 'open', 'action_type': 'follow_up', 'contact_identifier': '+14155550999',
                 'contact_name': 'Legacy Friend', 'channel': 'imessage', 'source': 'imessage',
                 'source_message_id': 'm-legacy', 'source_thread_id': 't-legacy',
                 'anchor_key': 'imessage:legacy', 'created_at': '2026-10-06 10:00:00',
                 'summary': 'Birthday was on Oct 4 and belated follow-up remains open'})

    def views():
        rows = ledger_io.load_entries()
        brief = {row['anchor_key'] for row in ledger_io.active_entries(rows, NOW)}
        ledger_text = _format_action_ledger({'action_ledger': rows}, NOW)
        loops = query()
        pulse_text = pulse.with_commitments({'attention_queue': [], 'pulse_markdown': ''},
                                            loops)['pulse_markdown']
        return brief, {row['anchor_key'] for row in loops['you_owe']}, ledger_text, pulse_text

    brief, loops, ledger_text, pulse_text = views()
    assert brief == loops == {greeting['anchor_key'], 'imessage:legacy'}
    assert 'Legacy Friend' in ledger_text and 'Legacy Friend' in pulse_text
    assert 'Maya Chen' in ledger_text and 'Maya Chen' in pulse_text
    # The once-VIP greeting loses its importance: hidden in every view; the legacy row stays.
    (root / 'preferences.json').write_text(json.dumps({'explicit': {'vip_people': []}}))
    (root / 'knowledge/relationship_state.json').write_text(json.dumps({'history': {}}))
    brief, loops, ledger_text, pulse_text = views()
    assert brief == loops == {'imessage:legacy'}
    assert 'Maya Chen' not in ledger_text and 'Maya Chen' not in pulse_text
    assert 'Legacy Friend' in ledger_text and 'Legacy Friend' in pulse_text
