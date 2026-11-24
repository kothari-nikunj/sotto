from datetime import datetime, timedelta, timezone

import pytest

from test_meeting_prep import mp, _taylor_inputs


def test_focused_prep_retains_terms_beyond_the_nudge_message_limit(monkeypatch):
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda _: True)
    now = datetime.now(timezone.utc)
    rows = [{'handle': '+14155550107', 'timestamp': (now - timedelta(hours=13-i)).isoformat(),
             'text': 'Fee waived only for the pilot' if i == 0 else f'Update {i}'} for i in range(13)]
    inputs = _taylor_inputs(focus='Taylor Reed', local={'imessage': rows}, prior_knowledge={
        'contact_index': [{'identifiers': ['taylor@startup.com', '+14155550107']}]})
    assert 'Fee waived only for the pilot' in mp.build_context(inputs)[0]


@pytest.mark.parametrize('outcome', ['ok', 'denied', 'wrong_endpoint', 'revoked', 'complete_snapshot', 'companion_partial'])
def test_targeted_prep_reread_excludes_groups_namesakes_and_revoked_sources(monkeypatch, outcome):
    import source_context
    allowed = {'contacts', 'imessage', 'whatsapp'}
    monkeypatch.setattr(source_context, 'allowed', allowed.__contains__)
    now = datetime.now(timezone.utc)
    jid, other = '14155550107@s.whatsapp.net', '14155550108@s.whatsapp.net'

    def row(peer, text, days=0, **extras):
        return dict(contact_jid=peer, timestamp=(now-timedelta(days=days)).isoformat(), text=text,
                    is_from_me=False, **extras)

    inputs = _taylor_inputs(focus='Taylor Reed', local={
        'source_status': {'whatsapp': 'ok', 'deferred_unread_whatsapp': 'partial'} if outcome == 'companion_partial'
                         else {'whatsapp': 'ok' if outcome == 'complete_snapshot' else 'partial'},
        'whatsapp': [row(jid, 'See you tomorrow'), row(other, 'WRONG PERSON', 1)]},
        prior_knowledge={'contact_index': [
            {'display_name': 'Taylor Reed', 'identifiers': ['taylor@startup.com', '+14155550107']},
            {'display_name': 'Taylor Reed', 'identifiers': ['taylor@other.test', '+14155550108']}]})
    requests = []

    def read(name, args, timeout):
        requests.append((name, args))
        if outcome == 'revoked':
            allowed.remove('whatsapp')
        return {'identifier': other if outcome == 'wrong_endpoint' else jid, 'source': 'whatsapp',
                'status': 'disabled' if outcome == 'denied' else 'ok',
                'messages': [row(other, 'WRONG PERSON'), row(jid, 'GROUP PLAN', is_group_chat=True),
                             row(jid, 'Only staff may attend Saturday', 8)]}
    monkeypatch.setattr(source_context, 'bridge_call', read)
    if outcome in ('revoked', 'denied'):
        with pytest.raises(RuntimeError, match='permission'):
            mp.refresh_prep_conversation(inputs)
        return
    mp.refresh_prep_conversation(inputs)
    if outcome == 'complete_snapshot':
        assert not requests
    else:
        assert len(requests) == 1 and requests[0][0] == 'get_messages'
        assert requests[0][1]['source'] == 'whatsapp' and requests[0][1]['identifier'] == jid
    text = mp.build_context(inputs)[0]
    assert ('Only staff may attend Saturday' in text) == (outcome in ('ok', 'companion_partial'))
    assert 'WRONG PERSON' not in text and 'GROUP PLAN' not in text
    if outcome == 'wrong_endpoint':
        assert 'partial' in text.lower()
    allowed.remove('whatsapp')
    assert 'Only staff may attend Saturday' not in mp.build_context(inputs)[0]


def test_shared_endpoint_and_bounded_source_coverages(monkeypatch):
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda _: True)
    now = datetime.now(timezone.utc)
    phone = '+14155550107'
    row = {'handle': phone, 'text': 'Decision', 'is_from_me': False}
    inputs = _taylor_inputs(focus='Taylor Reed', local={'imessage': [
        {**row, 'timestamp': (now-timedelta(minutes=i)).isoformat(), 'text': f'Contract clause {i}'}
        for i in range(25)]}, prior_knowledge={'contact_index': [
            {'identifiers': [phone, 'taylor@startup.com']}]})
    context = mp.attendee_text_context(inputs, 'taylor@startup.com', 'Taylor Reed')
    assert 'Partial conversation' in context and 'Contract clause 24' not in context
    inputs['prior_knowledge']['contact_index'].append({'identifiers': [phone, 'other@example.test']})
    assert mp.attendee_text_context(inputs, 'taylor@startup.com', 'Taylor Reed') == ''


@pytest.mark.parametrize('page_rows,partial', [([], False), ([{'bad': 'shape'}], True)])
def test_successful_empty_or_malformed_page_never_revives_snapshot_text(monkeypatch, page_rows, partial):
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda _: True)
    phone = '+14155550107'
    inputs = _taylor_inputs(focus='Taylor Reed', local={
        'source_status': {'imessage': 'partial'}, 'imessage': [{'handle': phone, 'is_from_me': False,
            'text': 'Previously observed private message', 'timestamp': datetime.now(timezone.utc).isoformat()}]},
        prior_knowledge={'contact_index': [{'identifiers': [phone, 'taylor@startup.com']}]})
    monkeypatch.setattr(source_context, 'bridge_call',
                        lambda name, args, timeout: {'identifier': phone, 'messages': page_rows})
    mp.refresh_prep_conversation(inputs)
    text = mp.attendee_text_context(inputs, 'taylor@startup.com', 'Taylor Reed')
    assert 'Previously observed private message' not in text
    assert ('Partial conversation' in text) == partial


def test_first_prep_uses_native_contact_card_and_drops_its_link_on_revoke(monkeypatch):
    import source_context
    grants = {'contacts', 'whatsapp'}
    monkeypatch.setattr(source_context, 'allowed', grants.__contains__)
    now = datetime.now(timezone.utc).isoformat()
    jid = '14155550107@s.whatsapp.net'
    inputs = _taylor_inputs(focus='Taylor Reed', local={
        'contacts': [{'name': 'Taylor Reed', 'emails': ['taylor@startup.com'], 'phones': ['+14155550107']}],
        'whatsapp': [{'timestamp': now, 'contact_jid': jid, 'text': 'CORRECTED TERMS'}]})
    assert 'CORRECTED TERMS' in mp.attendee_text_context(inputs, 'taylor@startup.com', 'Taylor Reed')
    grants.remove('contacts')
    assert mp.attendee_text_context(inputs, 'taylor@startup.com', 'Taylor Reed') == ''
