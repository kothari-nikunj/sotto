"""A plain reply must select only the complete obligation shown on an accepted reminder."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from test_delivered_task_completion import actual_writer_module, call, context, conversation, delivered_conversation, ke, rows
from test_runtime_loop_cleanup import prepare_cleanup

__all__ = ['context', 'actual_writer_module']

ASK = 'Review the Orchard party spreadsheet and add guests'
PERSON = 'Lena Example'
PROSE = 'The guest additions for Lena Example were due on November 18. The spreadsheet review is still outstanding.'
CONTRACT_COPY = {
    'contact possesses item': f'{PERSON} still has the signed contract.',
    'contact performed action': f'{PERSON} signed the contract.',
    'possessive status': f'{PERSON}’s signed contract is overdue.',
    'unattributed status': 'Signed contract is overdue.',
    'verbatim heading only': f'{PERSON} — Signed contract.',
    'dash describes contact action': f'{PERSON} — signed the contract.',
    'dash reports task outcome': f'{PERSON} — Contract signed.',
}



def ledger(context, scenario):
    prepare_cleanup(context)
    selected = next(row for row in rows() if row['anchor_key'] == 'cleanup-3')
    selected.update(contact_name=PERSON, summary=ASK, deadline='2026-11-18')
    if scenario == 'shorter deliverable':
        selected.update(summary='Sending the deck to legal and booking the room', deliverable='Send the deck')
    if scenario == 'notice plus invitation exceeds budget':
        selected['summary'] = ('Review the monthly status spreadsheet. ' * 32)[:1152]
    if scenario in CONTRACT_COPY:
        selected['summary'] = 'Signed contract'
    if scenario == 'private label':
        selected['summary'] = 'Email the draft to private@example.test'
    ke._load_continuity()._persist(selected)
    if scenario in ('other open task for this person', 'another task in first paragraph'):
        other = next(row for row in rows() if row['anchor_key'] == 'cleanup-4')
        other.update(contact_name=PERSON, contact_identifier=selected['contact_identifier'], summary='Place catering order')
        ke._load_continuity()._persist(other)
    now = datetime(2026, 11, 23, 15, tzinfo=timezone.utc)
    return selected, now


def fixture_writer(scenario):
    def writer(_provider, _model, _key, prompt, **kwargs):
        item, = json.loads(prompt)
        if scenario in CONTRACT_COPY:
            return json.dumps({'items': [dict(id=item['id'], text=CONTRACT_COPY[scenario], draft='', decline='')]})
        if scenario == 'notice plus invitation exceeds budget':
            return json.dumps({'items': [dict(id=item['id'], text=item.get('reply_notice') or 'Your event has a pending task.', draft='', decline='')]})
        if scenario in ('shorter deliverable', 'private label'):
            assert not item.get('reply_notice')
        elif scenario != 'sensitive wording':
            assert "owner's instructions" in kwargs['system']
            assert ASK in item['reply_notice']
        if scenario in ('sensitive wording', 'private label'):
            text = 'Your event has a pending task.'
        elif scenario in ('free-form whole task', 'stale scan with verbatim task'):
            text = f'{PERSON} — {ASK}.'
        elif scenario == 'shorter deliverable':
            text = f'{PERSON} — Send the deck.'
        elif scenario == 'unscoped original prose':
            text = PROSE
        elif scenario == 'another task in first paragraph':
            text = f'{PERSON} also needs the catering order placed by Friday.\n\n' + item['reply_notice']
        elif scenario == 'two sentences and named target':
            text = PROSE + '\n\n' + item['reply_notice']
        else:
            text = item['reply_notice']
        return json.dumps({'items': [dict(id=item['id'], text=text, draft='', decline='')]})
    return writer


@pytest.mark.parametrize('scenario,closes', [
    ('one task', True), ('two sentences and named target', False), ('another task in first paragraph', False), ('other open task for this person', True),
    *[(scenario, False) for scenario in CONTRACT_COPY], ('free-form whole task', False), ('unscoped original prose', False), ('stale scan', False), ('body changed', False), ('body prefixed', False), ('invitation removed', False),
    ('stale scan with verbatim task', False), ('task edited', False), ('shorter deliverable', False), ('sensitive wording', False), ('private label', False), ('notice plus invitation exceeds budget', False),
])
def test_real_composer_reminder_to_done(context, monkeypatch, scenario, closes):
    import compose_notification as composer
    import delivery_effects as effects
    import personal_context
    from loops_query import _query_rows
    scan = composer._load('proactive/scripts/proactive_scan.py', 'fixture_commitment_scan')

    selected, now = ledger(context, scenario)
    if scenario == 'notice plus invitation exceeds budget':
        from delivered_tasks import DONE_INVITATION, commitment_notice
        notice = commitment_notice(next(row for row in rows() if row['anchor_key'] == selected['anchor_key']))
        assert len(notice) < composer.MAX_COPY_CHARS < len(notice + DONE_INVITATION)
    version = effects.loop_version(selected)
    candidate = {'kind': 'commitment', 'key': 'party-list', 'decision_id': 'nudge123', 'person': PERSON,
                 'identifier': selected['contact_identifier'], 'title': ASK, 'channel': 'email',
                 'anchor_key': selected['anchor_key'], 'loop_version': version, 'action_due_date': selected['deadline'],
                 'loop_identity': effects.loop_identity(selected)}
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'e' * 32)
    monkeypatch.setattr(composer, '_loop_ledger', lambda now: (
        _query_rows(rows(), None, log_diagnostic=False, now=now), {row['anchor_key']: row for row in rows()}))
    monkeypatch.setattr(personal_context, 'conversation_snapshot', lambda: [])
    monkeypatch.setattr(composer, '_read_json_script', lambda *args: {})
    monkeypatch.setattr(composer.gemini, 'provider_key', lambda _: 'fixture')
    assert scan._defer_delivery_effects([candidate], date=now.date().isoformat())

    text = composer.compose('proactive', [candidate], now=now, llm=fixture_writer(scenario))
    assert len(text) <= composer.MAX_COPY_CHARS
    if scenario in ('sensitive wording', 'private label', 'notice plus invitation exceeds budget'):
        assert text == 'Your event has a pending task.'
    if scenario in ('two sentences and named target', 'another task in first paragraph'):
        assert 'Reply to this message with “Done”.' not in text
    if scenario == 'body prefixed':
        assert 'Reply to this message with “Done”.' in text
        text = 'The catering order is also due Friday.\n\n' + text
    if scenario == 'invitation removed':
        from delivered_tasks import DONE_INVITATION
        assert text.endswith(DONE_INVITATION)
        text = text.removesuffix(DONE_INVITATION)
    if scenario == 'body changed':
        text += ' Also call the caterer.'
    staged = json.loads(Path(effects._path(effects.run_id())).read_text())
    assert staged['notification_decision_ids'] == ['nudge123']
    if scenario == 'stale scan':
        next(e for e in staged['effects'] if e.get('anchor_key'))['loop_version'] = 'f' * 64
    if scenario == 'stale scan with verbatim task':
        selected['deadline'] = '2026-11-25'
        ke._load_continuity()._persist(selected)
    reply, _ = delivered_conversation(context, {'label': 'cron:sotto-proactive', 'effects': staged['effects'],
                                               'body': text}, rows())
    if scenario == 'task edited':
        selected['deliverable'] = 'Place a separate catering order'
        ke._load_continuity()._persist(selected)
    request = reply('Done', 'Done')
    result = call(request, context)
    assert call(request, context) == result
    assert {row['anchor_key'] for row in rows() if row['status'] == 'resolved'} == (
        {selected['anchor_key']} if closes else set())


@pytest.mark.parametrize('fields', [{'action_type': 'waiting_on'}, {'contact_name': '', 'source': 'missing'}])
def test_cannot_offer_done_for_a_waiting_on_or_unknown_person(fields):
    from delivered_tasks import commitment_notice
    row = {'action_type': 'follow_up', 'contact_name': PERSON, 'summary': ASK, 'deadline': '2026-11-18'}
    assert not commitment_notice({**row, **fields})


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize(('kind', 'direction'), [('chase', 'waiting_on'), ('handoff', 'follow_up')])
def test_chasing_or_handing_off_is_not_completion_of_a_scanned_task(context, monkeypatch, kind, direction, legacy):
    import compose_notification as composer
    import delivery_effects as effects
    scan = composer._load('proactive/scripts/proactive_scan.py', 'fixture_waiting_scan')
    row, now = ledger(context, 'one task')
    row.update(summary='Signed contract', action_type=direction)
    ke._load_continuity()._persist(row)
    body = f'{PERSON} still has the signed contract.'
    item = {'kind': kind, 'key': 'wait', 'decision_id': 'nudge123', 'person': PERSON, 'detail': body,
            'anchor_key': row['anchor_key'], 'loop_version': effects.loop_version(row),
            'action_due_date': row['deadline'], 'loop_identity': effects.loop_identity(row)}
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'e' * 32)
    assert scan._defer_delivery_effects([item], date=now.date().isoformat())
    monkeypatch.setattr(composer, '_write', lambda items, llm: [dict(id=items[0]['id'], text=body, draft='', decline='')])
    text = composer.compose('proactive', [item], now=now, enrich_fn=lambda items, now: items)
    assert text == body
    staged = json.loads(Path(effects._path(effects.run_id())).read_text())
    roles = [(e.get('anchor_key'), e.get('notification_id')) for e in staged['effects'] if e.get('kind') == 'eligibility']
    assert roles == [(row['anchor_key'], None), (None, item['decision_id'])]
    if legacy:
        staged['effects'] = [e for e in staged['effects'] if not e.get('notification_id')]
    reply, _ = delivered_conversation(context, {'label': 'cron:sotto-proactive', 'effects': staged['effects'],
                                               'body': text}, rows())
    call(reply('Done', 'Done'), context)
    assert all(r['status'] == 'open' for r in rows())


@pytest.mark.parametrize('text', ['Tina Example: signed the contract.', 'Tina Example — Contract signed.',
                                  'Tina Example — Signed contract.'])
def test_accepted_contact_heading_with_no_owner_assignment_never_resolves(context, text):
    reply, _ = conversation(context, 'nudge', task_fields={'cleanup-3': {'summary': 'Signed contract'}}, nudge_text=text)
    call(reply('Done', 'Done'), context)
    assert all(row['status'] == 'open' for row in rows())
