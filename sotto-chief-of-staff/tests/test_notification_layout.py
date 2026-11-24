"""How a nudge reads on the owner's phone, and the send rule (notification_layout.py)."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_shared/scripts'))
import compose_notification as notification
import gemini


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'a' * 32)
    monkeypatch.setattr(notification, 'enrich', lambda items, now: items)
    monkeypatch.setattr(gemini, 'provider_key', lambda _: 'test')
    return tmp_path


# Outbound layout and the send rule (Nov 16, 2026: the owner's phone showed the summary, the draft
# and the question run together on three lines, and two nudges that asked nothing of him).
NOV16 = datetime(2026, 11, 16, 10, 30, tzinfo=timezone.utc)


def _effects(root):
    return json.loads((root / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())


def _one(monkeypatch, row):
    monkeypatch.setattr(notification, '_write', lambda items, llm: [{'id': items[0]['id'], 'decline': '', **row}])


def test_draft_nudge_is_three_paragraphs_with_the_draft_set_apart(isolated, monkeypatch):
    _one(monkeypatch, {'text': 'Priya texted and emailed asking you to add your Lisbon trip to the team calendars.',
                       'draft': 'Will add it to the calendars today.'})
    bundle = {'events': [{'decision_id': 'a', 'class': 'escalation', 'sender': 'Priya Shah',
                          'event': {'source': 'gmail', 'from': 'Priya Shah <priya@example.com>'}}]}
    text = notification.compose('event', bundle, now=NOV16)
    assert text == ('Priya texted and emailed asking you to add your Lisbon trip to the team calendars.\n\n'
                    'Draft:\n“Will add it to the calendars today.”\n\n'
                    'Want this in your Gmail drafts?')
    offer = next(e['offer'] for e in _effects(isolated)['effects'] if e['kind'] == 'pending_offer')
    assert offer['question'] == 'Want this in your Gmail drafts?'
    assert json.loads(offer['detail'])['body'] == 'Will add it to the calendars today.'


def test_scheduling_alternatives_are_marked_drafts_in_their_own_paragraphs(isolated, monkeypatch):
    _one(monkeypatch, {'text': 'Sam asked to meet Thursday.', 'draft': '{{slots}} works for me.',
                       'decline': 'I cannot make Thursday.'})
    item = {'kind': 'scheduling_ask', 'key': 'x', 'decision_id': 'x', 'channel': 'email',
            'identifier': 'sam@example.com', 'slots': ['Thu Nov 19, 10:00 AM PST']}
    text = notification.compose('proactive', [item], now=NOV16)
    assert text == ('Sam asked to meet Thursday.\n\nDraft to accept:\n“Thu Nov 19, 10:00 AM PST works for me.”'
                    '\n\nDraft to decline:\n“I cannot make Thursday.”')


def test_post_meeting_draft_says_what_it_is_before_the_draft(isolated, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(notification, 'allowed', lambda _: True)
    event = {'summary': 'Morning Team Sync', 'start': '2026-11-16T09:00:00Z',
             'attendees': [{'email': 'priya@example.com', 'name': 'Priya Shah'}]}
    def gather(argv, **kwargs):
        Path(argv[-1]).write_text(json.dumps({'meetings': [{'title': event['summary'], 'start': event['start']}]}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(notification.subprocess, 'run', gather)
    body = 'Hi Priya,\n\nHere is the Larkspur blurb.\n\nLet me know if you need more.'
    monkeypatch.setattr(notification, '_load', lambda *args: SimpleNamespace(
        extract_apply_meeting=lambda *a, **k: {'followup': {'drafts': [
            {'to_email': 'priya@example.com', 'to_name': 'Priya', 'subject': 'Re: Larkspur blurb', 'body': body}]}}))
    text = notification.compose('event', {'events': [{'decision_id': 'b', 'class': 'post_meeting', 'event': event}]}, now=NOV16)
    assert text == ('After Morning Team Sync, I drafted a follow-up to Priya Shah about “Larkspur blurb”.\n\n'
                    'Draft:\n“' + body + '”\n\nWant this in your Gmail drafts?')


@pytest.mark.parametrize('source, event, person', [
    ('imessage', {'handle': '+14155550142', 'text': 'Do you want to set up an SPV for Bramble?'}, 'Victor Sato'),
    ('gmail', {'from': 'Will Chen <will@example.com>', 'subject': 'Reference on Quillon'}, 'Will Chen'),
])
def test_direct_ask_without_a_draft_carries_a_self_contained_offer(isolated, monkeypatch, source, event, person):
    _one(monkeypatch, {'text': f'{person} asked you something today.', 'draft': ''})
    bundle = {'events': [{'decision_id': 'd', 'class': 'actionable', 'sender': person,
                          'event': {'source': source, **event}}]}
    text = notification.compose('event', bundle, now=NOV16)
    assert text == f'{person} asked you something today.\n\nSay “draft a reply to {person}” and I’ll write one.'
    assert _effects(isolated)['notification_decision_ids'] == ['d']


def test_nudge_with_nothing_to_answer_is_not_sent_unless_urgent(isolated, monkeypatch):
    _one(monkeypatch, {'text': 'The planning group asked who can host Friday.', 'draft': ''})
    group = {'source': 'whatsapp', 'sender_jid': '123@g.us', 'is_group_chat': True}
    bundle = {'events': [{'decision_id': 'g', 'class': 'actionable', 'sender': 'Planning', 'event': group}]}
    assert notification.compose('event', bundle, now=NOV16) == 'NO_NUDGES'
    assert _effects(isolated)['notification_decision_ids'] == []
    urgent = {'events': [{'decision_id': 'g', 'class': 'urgent', 'sender': 'Planning', 'event': group}]}
    assert notification.compose('event', urgent, now=NOV16) == 'The planning group asked who can host Friday.'


def test_dashboard_promotion_delivers_without_an_ask(isolated, monkeypatch):
    summary = 'The planning group asked who can host Friday.'
    _one(monkeypatch, {'text': summary, 'draft': ''})
    bundle = {'promoted': True, '_user_requested_delivery': True, 'events': [
        {'decision_id': 'g', 'class': 'actionable', 'sender': 'Planning', 'event': {
            'source': 'whatsapp', 'sender_jid': '123@g.us', 'is_group_chat': True}}]}
    assert notification.compose('event', bundle, now=NOV16) == summary
    assert _effects(isolated)['notification_decision_ids'] == ['g']


def test_withheld_automatic_promotion_returns_once_to_the_next_digest(isolated, monkeypatch):
    triage = notification._load('event-triage/scripts/triage_event.py', 'layout_triage')
    digest = notification._load('event-triage/scripts/digest_check.py', 'layout_digest')
    now = datetime.now(timezone.utc)
    observed = now - timedelta(minutes=30)
    entry = {'ts': observed.isoformat(), 'verdict_class': 'cooldown', 'held_class': 'actionable',
             'sender': 'Planning', 'event': {'source': 'whatsapp', 'rowid': 1, 'sender_jid': '123@g.us',
             'is_group_chat': True, 'timestamp': observed.isoformat(),
             'text': 'Who can host Friday?'}}
    triage._commit_queue([entry])
    outlined = triage.release_valve(now_local=NOV16, now_utc=now, now_ts=now.timestamp())
    assert outlined['verdict'] == 'agent'
    assert digest.entries_since(None) == []
    _one(monkeypatch, {'text': 'The planning group asked who can host Friday.', 'draft': ''})
    for _ in range(2):
        assert notification.compose('event', outlined['bundle'], now=now) == 'NO_NUDGES'
    assert _effects(isolated)['notification_decision_ids'] == []
    late = now + timedelta(hours=2)
    assert triage.release_valve(now_local=NOV16, now_utc=late, now_ts=late.timestamp())['verdict'] == 'drop'
    assert json.loads((isolated / 'events/budget.json').read_text())['count'] == 1
    reviewed = []
    monkeypatch.setattr(digest, 'read_stamp', lambda: now)
    monkeypatch.setattr(digest, 'advance_stamp', lambda _: None)
    monkeypatch.setattr(digest, 'check', lambda entries, **_: reviewed.extend(entries) or {'deliver': False})
    assert digest.run_check(datetime.now(timezone.utc)) == {'deliver': False}
    retained, = reviewed
    assert retained['event'] == entry['event']
    assert retained['ts'] == entry['ts']
    assert retained['held_class'] == 'actionable'
    assert triage.promote_one(triage.queue_key(json.dumps(retained)), now_local=NOV16, now_utc=late,
                              now_ts=late.timestamp())['ok'] is True


@pytest.mark.parametrize('item', [
    {'kind': 'commitment', 'action_due_date': '2026-11-16'},
    {'kind': 'commitment', 'action_due_date': '2026-11-17'},
    {'kind': 'birthday', 'lead_days': 3},
])
def test_due_within_a_day_and_birthdays_are_sent_without_an_ask(isolated, monkeypatch, item):
    _one(monkeypatch, {'text': 'Dana needs the Cedar deck.', 'draft': ''})
    assert notification.compose('proactive', [{'key': 'x', 'decision_id': 'x', 'person': 'Dana', **item}],
                                now=NOV16) == 'Dana needs the Cedar deck.'


def test_commitment_due_later_without_an_ask_stays_for_the_brief(isolated, monkeypatch):
    _one(monkeypatch, {'text': 'Dana needs the Cedar deck.', 'draft': ''})
    item = {'kind': 'commitment', 'key': 'x', 'decision_id': 'x', 'person': 'Dana', 'action_due_date': '2026-11-20'}
    assert notification.compose('proactive', [item], now=NOV16) == 'NO_NUDGES'
    assert _effects(isolated)['notification_decision_ids'] == []


def test_post_meeting_list_without_a_draft_yields(isolated, monkeypatch):
    monkeypatch.setattr(notification, 'allowed', lambda source: source != 'granola')
    bundle = {'events': [{'class': 'post_meeting', 'event': {'summary': 'Sync'}}]}
    monkeypatch.setattr(notification, 'enrich', lambda items, now: [
        {**items[0], 'open_loops': [{'what': 'Send the deck'}]}])
    assert notification.compose('event', bundle, now=NOV16) == 'NO_NUDGES'


@pytest.mark.parametrize('channel,label,uri', [
    ('imessage', 'Open draft in Messages', 'imessage://+14155550199?body=Here%20is%20the%20deck.'),
    ('sms', 'Open draft in Messages', 'sms:+14155550199&body=Here%20is%20the%20deck.'),
    ('whatsapp', 'Open draft in WhatsApp', 'https://wa.me/14155550199?text=Here%20is%20the%20deck.'),
    ('whatsapp_call', 'Open draft in WhatsApp', 'https://wa.me/14155550199?text=Here%20is%20the%20deck.'),
    ('phone', 'Call Dana', 'tel:+14155550199'),
    ('facetime', 'Call Dana', 'tel:+14155550199'),
    ('tel', 'Call Dana', 'tel:+14155550199'),
])
def test_nudge_action_names_what_its_link_opens(isolated, monkeypatch, channel, label, uri):
    _one(monkeypatch, {'text': 'You told Dana you would send the Cedar deck today.', 'draft': 'Here is the deck.'})
    item = {'kind': 'commitment', 'key': 'c', 'decision_id': 'c', 'person': 'Dana', 'channel': channel,
            'identifier': '+14155550199', 'action_due_date': '2026-11-16'}
    text = notification.compose('proactive', [item], now=NOV16)
    head, draft, link = text.split('\n\n')
    assert draft == 'Draft:\n“Here is the deck.”'
    assert link == f'{label}:\n{uri}'
    from chatfmt import to_imessage
    assert to_imessage(text) == text


@pytest.mark.parametrize('deadline, sent', [
    ('2026-11-16T17:00:00-08:00', True), ('2026-11-30T17:00:00Z', False), ('Friday', False), ('17', False)])
def test_event_deadline_counts_only_as_a_parsed_instant(isolated, monkeypatch, deadline, sent):
    _one(monkeypatch, {'text': 'The planning group needs a host.', 'draft': ''})
    bundle = {'events': [{'decision_id': 'g', 'class': 'actionable', 'sender': 'Planning', 'relevance_deadline': deadline,
                          'event': {'source': 'whatsapp', 'sender_jid': '123@g.us', 'is_group_chat': True}}]}
    assert (notification.compose('event', bundle, now=NOV16) != 'NO_NUDGES') is sent


@pytest.mark.parametrize('held', ['actionable', 'budget', 'event'])
def test_any_released_direct_ask_gets_the_reply_offer(isolated, monkeypatch, held):
    _one(monkeypatch, {'text': 'Will asked for a reference on Quillon.', 'draft': ''})
    bundle = {'events': [{'decision_id': 'e', 'class': held, 'sender': 'Will Chen',
                          'event': {'source': 'gmail', 'from': 'Will Chen <will@example.com>'}}]}
    assert notification.compose('event', bundle, now=NOV16).endswith('Say “draft a reply to Will Chen” and I’ll write one.')


def test_proactive_chase_without_draft_offers_a_note_or_is_withheld(isolated, monkeypatch):
    _one(monkeypatch, {'text': 'Sam has not sent the deck yet.', 'draft': ''})
    chase = {'kind': 'chase', 'key': 'c', 'decision_id': 'c', 'person': 'Sam', 'channel': 'imessage', 'identifier': '+14155550123'}
    assert notification.compose('proactive', [chase], now=NOV16).endswith('Say “draft a note to Sam” and I’ll write one.')
    assert notification.compose('proactive', [{**chase, 'identifier': ''}], now=NOV16) == 'NO_NUDGES'


def test_offered_slots_use_the_unpadded_time_style_of_every_other_nudge():
    now = datetime(2026, 11, 2, 7, tzinfo=timezone.utc)
    calendar = {'all_calendars_complete': True, 'since': now.isoformat(), 'until': '2026-11-06T00:00:00Z', 'events': []}
    assert notification.slots('30 minutes Tuesday?', calendar, now)[0] == 'Tue Nov 3, 9:00 AM UTC'


def test_send_rule_set_matches_both_rules_islands():
    import test_docs_drift as drift
    for island in (drift.ARCH, drift.LOOPS):
        assert set(island['send_rule']['without_ask']) == set(notification.SEND_WITHOUT_ASK_KINDS), drift.RULE


# A proactive item the send rule withholds is not re-proposed that day (Nov 16, 2026: the 4-hour
# reservation lapsed and the same item would be composed and charged again).
def _scan_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location('layout_ps', Path(__file__).resolve().parents[1] / 'proactive/scripts/proactive_scan.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scan(tmp_path, monkeypatch, capsys):
    ps = _scan_module()
    for key, value in {'SOTTO_DATA': str(tmp_path), 'SOTTO_TIMEZONE': '+00:00', 'SOTTO_QUIET_START': '0',
                       'SOTTO_QUIET_END': '0', 'SOTTO_NUDGE_BUDGET': '4'}.items():
        monkeypatch.setenv(key, value)
    clock = {'now': datetime(2026, 11, 16, 10, tzinfo=timezone.utc)}
    nudge = {'kind': 'chase', 'key': 'chase-sam', 'title': 'Deck', 'person': 'Sam', 'detail': 'the deck'}
    monkeypatch.setattr(ps, '_now_local', lambda _tz: clock['now'])
    monkeypatch.setattr(ps, 'scan', lambda *a, **k: {'nudges': [dict(nudge)]})
    monkeypatch.setattr(ps, '_open_loops', lambda: [])
    def run(run):
        monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', run * 32)
        monkeypatch.setattr(sys, 'argv', ['proactive_scan.py'])
        ps.main()
        return [n['key'] for n in json.loads(capsys.readouterr().out)['nudges']]
    def lapse():
        with ps.delivery_effects.proactive_state(clock['now'].date().isoformat()) as state:
            for row in state['pending'].values():
                row['valid_until'] = 0
    def spent():
        return json.loads((tmp_path / 'events/budget.json').read_text())['count']
    return run, lapse, spent, clock, nudge


def test_withheld_proactive_item_is_not_reoffered_or_charged_again_that_day(isolated, monkeypatch, scan):
    run, lapse, spent, clock, nudge = scan
    assert run('a') == ['chase-sam'] and spent() == 1
    _one(monkeypatch, {'text': 'Sam has not sent the deck yet.', 'draft': ''})
    assert notification.compose('proactive', [{**nudge, 'decision_id': 'c'}], now=clock['now']) == 'NO_NUDGES'
    lapse()
    assert run('b') == [] and spent() == 1
    clock['now'] = datetime(2026, 11, 17, 10, tzinfo=timezone.utc)
    assert run('c') == ['chase-sam']


def test_withheld_item_that_becomes_due_within_a_day_is_offered_again(isolated, monkeypatch, scan):
    run, lapse, spent, clock, nudge = scan
    run('a')
    _one(monkeypatch, {'text': 'Sam has not sent the deck yet.', 'draft': ''})
    notification.compose('proactive', [{**nudge, 'decision_id': 'c'}], now=clock['now'])
    lapse()
    nudge['deadline'] = '2026-11-17'
    assert run('b') == ['chase-sam']
