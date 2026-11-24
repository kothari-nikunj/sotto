"""Targeted WhatsApp evidence reaches briefs under the source's own grant."""
import sys

import pytest

from test_loop_context import NOW, Service, gg, lc


JID = '15555550123@s.whatsapp.net'


def wa_loop(**kw):
    return dict({'anchor_key': 'follow_up:wa', 'status': 'open', 'channel': 'whatsapp', 'source': 'whatsapp',
                 'contact_identifier': JID, 'created_at': '2026-09-28T10:00:00Z'}, **kw)


def wa_row(text, at='2026-09-30T10:00:00Z', **kw):
    return dict({'contact_jid': JID, 'is_from_me': True, 'timestamp': at, 'text': text,
                 'is_group_chat': False, 'source_id': text}, **kw)


def wa_refresh(rows, read, allowed=lambda _: True):
    return lc.refresh_context(rows, {}, Service([]), gg.normalize_email, allowed=allowed, now=NOW,
                              aliases=lambda *_: (), read_messages=read)


def test_whatsapp_lane_reads_one_direct_chat_after_the_ask():
    calls = []
    def read(identifier, **kwargs):
        calls.append((identifier, kwargs.get('source')))
        return {'identifier': identifier, 'source': 'whatsapp', 'messages': [
            wa_row('Sent it over.'), wa_row('Before the ask.', at='2026-09-27T10:00:00Z'),
            wa_row('Group line.', is_group_chat=True), wa_row('Someone else.', contact_jid='15555550144@s.whatsapp.net')]}
    context, receipt = wa_refresh([wa_loop(), wa_loop(anchor_key='group', group_id='120363000@g.us')], read)
    assert calls == [(JID, 'whatsapp')]
    assert [m['text'] for m in context['whatsapp']] == ['Sent it over.']
    assert context['whatsapp'][0]['_loop_context_sources'] == ['whatsapp']
    assert receipt['message_counts'] == {'gmail': 0, 'imessage': 0, 'whatsapp': 1}
    assert 'whatsapp' in receipt['sources'] and 'whatsapp_error' not in receipt
    assert wa_refresh([wa_loop()], read, allowed=lambda s: s != 'whatsapp')[0] == {'emails': [], 'imessage': []}
    assert len(calls) == 1


@pytest.mark.parametrize('reply', [
    RuntimeError('unknown argument for get_messages'),     # a Bridge that rejects `source`
    {'identifier': JID, 'messages': []},                   # an older Bridge that ignores it
    {'identifier': JID, 'source': 'whatsapp', 'messages': [], 'status': 'disabled'},
])
def test_whatsapp_read_an_old_bridge_cannot_serve_is_failed_not_empty(reply):
    def read(identifier, **kwargs):
        if isinstance(reply, Exception):
            raise reply
        return reply
    context, receipt = wa_refresh([wa_loop()], read)
    assert context['whatsapp'] == [] and receipt['whatsapp_error'] == 'read_failed'
    assert receipt['status'] == 'partial' and receipt['complete'] is False and receipt['checked'] == 0


def test_whatsapp_lane_needs_an_observed_direct_jid():
    for identifier in ('+15555550123', '120363000@g.us', '123@lid'):
        assert lc._imessage_plan([wa_loop(contact_identifier=identifier)], now=NOW, allowed=lambda _: True,
                                 aliases=lambda *_: (), source='whatsapp') == []
    plan = lc._imessage_plan([wa_loop()], now=NOW, allowed=lambda _: True, aliases=lambda *_: (), source='whatsapp')
    assert [target[1][:2] for target in plan] == [('whatsapp', JID)]


def test_whatsapp_loop_rows_merge_beside_current_rows_and_render_as_loop_evidence(monkeypatch):
    import render_local
    import source_context
    current = wa_row('Sent it over.')
    loop_rows = [{**wa_row(text), '_loop_context': True, '_loop_context_sources': ['whatsapp']}
                 for text in ('Sent it over.', 'Later reply.')]
    local = lc.merge_texts({'whatsapp': [current]}, {'whatsapp': loop_rows})
    assert [row['text'] for row in local['whatsapp']] == ['Sent it over.', 'Later reply.']
    monkeypatch.setattr(source_context, 'allowed', lambda s: True)
    evidence = render_local._format_loop_message_evidence(local)
    assert 'whatsapp_jid: ' + JID in evidence and 'Later reply.' in evidence and 'Sent it over.' not in evidence
    monkeypatch.setattr(source_context, 'allowed', lambda s: s != 'whatsapp')
    assert render_local._format_loop_message_evidence(local) == ''


def test_whatsapp_loop_rows_stay_out_of_daily_rows_and_honor_mutes_and_dependencies(monkeypatch):
    import compose_brief
    import source_context
    from textutil import current_message_rows
    row = {**wa_row('Later reply.', partner_name='Alex Stone'), '_loop_context': True,
           '_loop_context_sources': ['imessage', 'whatsapp']}
    local = {'whatsapp': [wa_row('Today.'), row]}
    assert [m['text'] for m in current_message_rows(local['whatsapp'])] == ['Today.']
    monkeypatch.setattr(source_context, 'allowed', lambda s: True)
    for muted, shown in (([], True), (['Alex Stone'], False)):
        monkeypatch.setattr(compose_brief, 'explicit_prefs', lambda: {'mute_people': muted})
        assert ('Later reply.' in compose_brief._visible_loop_message_evidence(local)) is shown
    monkeypatch.setattr(compose_brief, 'explicit_prefs', lambda: {'mute_people': []})
    monkeypatch.setattr(source_context, 'allowed', lambda s: s != 'imessage')
    assert compose_brief._visible_loop_message_evidence(local) == ''


@pytest.mark.parametrize('lane', ['whatsapp', 'imessage'])
@pytest.mark.parametrize('gmail', ['disabled', 'quota', 'auth', 'missing_cli', 'client'])
def test_gather_can_reread_native_messages_when_gmail_is_unavailable(tmp_path, monkeypatch, gmail, lane):
    import json
    import ledger_io
    import source_context

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(source_context, 'allowed',
                        lambda source: source == lane or (source == 'gmail' and gmail != 'disabled'))
    monkeypatch.setattr(gg, '_find_google_api', lambda: None if gmail == 'missing_cli' else '/fixture/google.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: None)

    def auth():
        if gmail == 'disabled':
            pytest.fail('Local-only rereads must not check the Google token')
        if gmail == 'auth':
            raise RuntimeError('fixture Gmail authorization unavailable')

    def inbox(*args, **kwargs):
        rows = gg.GmailRows()
        rows.rate_limited = gmail == 'quota'
        return rows

    service_calls = []

    def service():
        service_calls.append(gmail)
        raise RuntimeError('fixture provider client unavailable')

    monkeypatch.setattr(gg, '_refresh_google_token', auth)
    monkeypatch.setattr(gg, 'gather_gmail', inbox)
    monkeypatch.setattr(gg, '_gmail_service', service)
    endpoint = JID if lane == 'whatsapp' else '+15555550123'
    task = wa_loop(channel=lane, source=lane, contact_identifier=endpoint)
    monkeypatch.setattr(ledger_io, 'load_active', lambda: [task])
    monkeypatch.setattr(lc, 'graph_aliases', lambda: lambda *_: ())
    calls = []

    def read(name, arguments, *, timeout):
        calls.append((name, arguments))
        message = (wa_row('Sent it.') if lane == 'whatsapp' else
                   {'handle': endpoint, 'is_from_me': True, 'timestamp': '2026-09-30T10:00:00Z',
                    'text': 'Sent it.', 'id': 'exact'})
        return {'identifier': endpoint, 'source': lane, 'messages': [message]}

    monkeypatch.setattr(source_context, 'bridge_call', read)
    local, mail, cal, receipt = (tmp_path / f'{name}.json' for name in ('local', 'gmail', 'cal', 'sources'))
    local.write_text('{}')
    monkeypatch.setattr(sys, 'argv', ['gather_google.py', '--loop-local', str(local),
        '--gmail-out', str(mail), '--cal-out', str(cal), '--source-results-out', str(receipt),
        '--skip-calendar', '--skip-sent', '--skip-stale'])
    gg.main()
    expected = {'identifier': endpoint, 'limit': lc.MAX_MESSAGES}
    if lane == 'whatsapp':
        expected['source'] = lane
    assert calls == [('get_messages', expected)]
    assert service_calls == (['client'] if gmail == 'client' else [])
    assert json.loads(local.read_text())[lane][0]['_loop_context_sources'] == [lane]
    assert json.loads(receipt.read_text())['gmail']['loop_context']['sources'] == [lane]


@pytest.mark.parametrize('origin', ['gmail', 'imessage'])
def test_late_gmail_revocation_only_removes_gmail_derived_reread(monkeypatch, origin):
    import source_context

    grants = {'gmail', 'imessage', 'whatsapp'}
    monkeypatch.setattr(source_context, 'allowed', grants.__contains__)
    phone = '+15555550144'
    task = wa_loop(anchor_key='follow_up:imessage', source=origin, channel='imessage',
                   contact_identifier=phone)
    historical = {'handle': phone, 'timestamp': '2026-09-30T10:00:00Z', 'is_from_me': True,
                  'id': 'historical-text', 'text': 'Old reply.'}

    def read(identifier, **scope):
        if scope.get('source') == 'whatsapp':
            return {'identifier': identifier, 'source': 'whatsapp', 'messages': [wa_row('WhatsApp reply.')]}
        return {'identifier': identifier, 'messages': [historical]}

    updates, receipt = wa_refresh([task, wa_loop()], read, allowed=grants.__contains__)
    assert set(receipt['sources']) == {origin, 'imessage', 'whatsapp'}
    latest = {**historical, 'id': 'new-day', 'text': 'Ordinary native row.'}
    staged = lc.merge_texts({'imessage': [latest]}, {s: updates[s] for s in ('imessage', 'whatsapp')})
    assert [row['id'] for row in source_context.project_local(staged)['imessage']] == ['new-day', 'historical-text']

    grants.remove('gmail')  # After the gather receipt, before the brief uses its staged evidence.
    projected = source_context.project_local(staged)
    assert [row['id'] for row in projected['imessage']] == (['new-day'] if origin == 'gmail' else ['new-day', 'historical-text'])
    assert [row['text'] for row in projected['whatsapp']] == ['WhatsApp reply.']
    assert ('gmail' in source_context.used_sources(staged, already_projected=True)) == (origin == 'gmail')


def test_local_imessage_read_without_gmail_requires_original_endpoint_and_source(monkeypatch):
    phone = '+15555550155'
    def unexpected(*args):
        pytest.fail('No graph or Bridge read should be needed')
    monkeypatch.setattr(lc, 'graph_aliases', unexpected)
    for origin, primary in [('gmail', phone), ('whatsapp', phone), ('imessage', 'opaque-id')]:
        row = wa_loop(channel=origin, source=origin, contact_identifier=primary)
        context, receipt = lc.refresh_context([row], {}, None, gg.normalize_email, now=NOW,
            allowed=lambda source: source == 'imessage', read_messages=unexpected)
        assert context['imessage'] == [] and receipt['requests'] == 0


def test_imessage_alias_keeps_gmail_dependency_even_for_native_origin():
    phone = '+15555550155'
    row = wa_loop(channel='imessage', source='imessage', contact_identifier='fixture@example.test')
    plan = lc._imessage_plan([row], now=NOW, allowed=lambda _: True, aliases=lambda *_: [phone])
    _, (lane, endpoint, sources, _) = plan[0]
    assert (lane, endpoint, sources) == ('imessage', phone, {'gmail', 'imessage'})


def test_original_imessage_handle_survives_equivalent_gmail_alias():
    primary = '4155550155'
    row = wa_loop(source='imessage', channel='imessage', contact_identifier=primary)
    plan = lc._imessage_plan([row], now=NOW, allowed=lambda _: True,
                             aliases=lambda *_: ['+14155550155'])
    _, (_, endpoint, sources, _) = plan[0]
    assert (endpoint, sources) == (primary, {'imessage'})
