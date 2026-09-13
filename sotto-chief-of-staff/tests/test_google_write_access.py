"""Native Google write contracts, partial grants and background refusal."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

spec = importlib.util.spec_from_file_location('google_writes', Path(__file__).parents[1] / '_shared/scripts/google_action.py')
ga = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ga)


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_UNATTENDED', raising=False)
    monkeypatch.setattr(ga, 'capabilities', lambda: dict.fromkeys(
        ['gmail_write', 'calendar_write', 'contacts_write', 'contacts_read'], True))
    client = MagicMock()
    monkeypatch.setattr(ga, '_google_service', lambda *args: client)
    monkeypatch.setattr(ga, '_gmail_service', lambda: client)
    return client


def test_contact_update_uses_current_version_and_only_requested_fields(service):
    people = service.people.return_value
    sources = [{'type': 'CONTACT', 'id': 'c1', 'etag': 'source-version'}]
    people.get.return_value.execute.return_value = {'etag': 'person-version', 'metadata': {'sources': sources}}
    people.updateContact.return_value.execute.return_value = {'resourceName': 'people/c1'}
    result = ga._native_action(SimpleNamespace(cmd='contacts-update', resource_name='people/c1',
        fields_json='{"phoneNumbers":[{"value":"+15555550123"}]}'))
    assert result['status'] == 'updated'
    args = people.updateContact.call_args.kwargs
    assert args['updatePersonFields'] == 'phoneNumbers'
    assert args['body']['metadata']['sources'] == sources
    assert set(args['body']) == {'phoneNumbers', 'etag', 'metadata'}


def test_contact_update_without_version_and_unknown_fields_refuses(service):
    people = service.people.return_value
    people.get.return_value.execute.return_value = {'metadata': {'sources': []}}
    args = SimpleNamespace(cmd='contacts-update', resource_name='people/c1', fields_json='{"names":[]}')
    assert ga._native_action(args)['status'] == 'error'
    people.updateContact.assert_not_called()
    args.fields_json = '{"memberships":[]}'
    assert ga._native_action(args)['status'] == 'error'
    people.updateContact.assert_not_called()


def test_contact_create_requires_google_receipt(service):
    service.people.return_value.createContact.return_value.execute.return_value = {}
    assert ga._native_action(SimpleNamespace(cmd='contacts-create', fields_json='{"names":[{"givenName":"Test"}]}'))['status'] == 'error'


def test_calendar_move_patches_same_event_without_replacing_guests(service):
    events = service.events.return_value
    events.patch.return_value.execute.return_value = {'id': 'existing'}
    result = ga._native_action(SimpleNamespace(cmd='calendar-update', calendar='primary', event_id='existing',
        fields_json='{"start":{"dateTime":"2026-09-09T11:00:00-07:00"},"end":{"dateTime":"2026-09-09T11:30:00-07:00"}}'))
    assert result['event_id'] == 'existing'
    args = events.patch.call_args.kwargs
    assert set(args['body']) == {'start', 'end'}
    assert args['sendUpdates'] == 'all'
    events.insert.assert_not_called()
    events.delete.assert_not_called()


def test_gmail_archive_preserves_other_labels(service):
    modify = service.users.return_value.messages.return_value.modify
    modify.return_value.execute.return_value = {'id': 'mail1'}
    out = ga._native_action(SimpleNamespace(cmd='gmail-modify', message_id='mail1', add_labels='', remove_labels='INBOX'))
    assert out['status'] == 'modified'
    assert modify.call_args.kwargs['body'] == {'addLabelIds': [], 'removeLabelIds': ['INBOX']}


@pytest.mark.parametrize('argv', [
    ['calendar-update', '--event-id', 'event', '--fields-json', '{"summary":"new"}'],
    ['gmail-modify', '--message-id', 'mail', '--remove-labels', 'INBOX'],
    ['contacts-create', '--fields-json', '{"names":[]}'],
    ['contacts-update', '--resource-name', 'people/c1', '--fields-json', '{"names":[]}'],
    ['contacts-delete', '--resource-name', 'people/c1'],
])
def test_all_new_writes_refused_before_client_creation(argv, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_UNATTENDED', '1')
    monkeypatch.setattr(ga, '_native_action', lambda a: pytest.fail('must refuse before action'))
    monkeypatch.setattr('sys.argv', ['google_action.py', *argv])
    with pytest.raises(SystemExit) as exc:
        ga.main()
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)['fallback'] == 'propose_in_brief'


def test_actual_scope_capabilities(tmp_path, monkeypatch):
    token = tmp_path / 'token.json'
    monkeypatch.setattr(ga, '_token_path', lambda: str(token))
    token.write_text(json.dumps({'refresh_token': 'fixture', 'scopes': [
        'https://www.googleapis.com/auth/gmail.modify', 'https://www.googleapis.com/auth/calendar',
        'https://www.googleapis.com/auth/contacts']}))
    assert all(ga.capabilities()[key] for key in ('gmail_read', 'gmail_draft', 'gmail_write', 'gmail_send',
                                               'calendar_read', 'calendar_write', 'contacts_read', 'contacts_write'))
    token.write_text(json.dumps({'refresh_token': 'fixture', 'scopes': ['https://www.googleapis.com/auth/contacts.readonly']}))
    cap = ga.capabilities()
    assert cap['contacts_read'] and not cap['contacts_write'] and not cap['calendar_write']


def test_missing_grant_refuses_before_google(monkeypatch):
    monkeypatch.setattr(ga, 'capabilities', lambda: {'contacts_write': False})
    monkeypatch.setattr(ga, '_google_service', lambda *args: pytest.fail('must not build client'))
    assert ga._native_action(SimpleNamespace(cmd='contacts-delete', resource_name='people/c1'))['fallback'] == 'request_google_consent'


def test_contacts_search_warms_cache_and_returns_real_ids(service):
    search = service.people.return_value.searchContacts
    search.return_value.execute.return_value = {'results': [{'person': {'resourceName': 'people/c1'}}]}
    out = ga._contacts_read(SimpleNamespace(cmd='contacts-search', query='Alex'))
    assert [call.kwargs['query'] for call in search.call_args_list] == ['', 'Alex']
    assert out['results'][0]['person']['resourceName'] == 'people/c1'


def test_managed_rsvp_preserves_attendees_and_checks_event_version(service, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setattr(ga, '_token_path', lambda: '/fixture/token.json')
    events = service.events.return_value
    guest = {'email': 'other@example.com', 'responseStatus': 'accepted'}
    events.get.return_value.execute.return_value = {'id': 'e1', 'etag': 'version',
        'attendees': [{'email': 'me@example.com', 'self': True, 'responseStatus': 'needsAction'}, guest]}
    events.patch.return_value.execute.return_value = {'id': 'e1'}
    assert ga._rsvp('e1', 'accepted')['status'] == 'rsvped'
    attendees = events.patch.call_args.kwargs['body']['attendees']
    assert attendees[1] == guest and attendees[0]['responseStatus'] == 'accepted'
    events.patch.return_value.headers.__setitem__.assert_called_once_with('If-Match', 'version')
