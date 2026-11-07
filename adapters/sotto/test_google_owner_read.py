"""Fixed typed Google reads use synthetic bounded HTTP responses; no OAuth/network."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('owner_google_api_test', Path(__file__).with_name('google_api.py'))
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class Response:
    status_code = 200
    headers = {}

    def __init__(self, value):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.raw), chunk_size):
            yield self.raw[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, gmail=(), calendar=(), *, next_page=False):
        self.gmail = gmail
        self.calendar = calendar
        self.next_page = next_page
        self.calls = []
        self.responses = []
        self.closed = False

    def get(self, url, **kwargs):
        assert kwargs['stream'] is True and 0 < kwargs['timeout'] <= 5
        assert kwargs['allow_redirects'] is False and kwargs['params']['fields']
        self.calls.append((url, kwargs['params']))
        if url.endswith('/gmail/v1/users/me/messages'):
            value = {'messages': [{'id': row['id']} for row in self.gmail]}
            if self.next_page:
                value['nextPageToken'] = 'opaque'
        elif '/gmail/v1/users/me/messages/' in url:
            row = next(row for row in self.gmail if url.endswith('/' + row['id']))
            value = {'id': row['id'], 'threadId': 'thread', 'payload': {'headers': [
                {'name': 'Subject', 'value': row.get('subject', '')}]}, 'snippet': 'preview'}
        elif url.endswith('/calendar/v3/users/me/calendarList'):
            value = {'items': [{'id': 'primary', 'accessRole': 'owner'}]}
        elif url.endswith('/calendar/v3/calendars/primary/events'):
            value = {'items': self.calendar}
            if self.next_page:
                value['nextPageToken'] = 'opaque'
        elif '/calendar/v3/calendars/primary/events/' in url:
            value = self.calendar[0]
        else:
            pytest.fail('Unexpected Google URL')
        response = Response(value)
        self.responses.append(response)
        return response

    def close(self):
        self.closed = True


def test_gmail_empty_success_and_bounded_partial_metadata():
    empty = Session()
    result = api.read_owner('gmail_search', {'query': 'in:inbox', 'max': 2}, lambda: empty)
    assert result['items'] == [] and result['observation']['status'] == 'ok'
    assert result['observation']['complete'] is True and empty.closed
    gmail = Session(gmail=[{'id': 'm1', 'subject': 'one'}, {'id': 'm2', 'subject': 'two'}], next_page=True)
    result = api.read_owner('gmail_search', {'query': 'newer_than:1d', 'max': 2}, lambda: gmail)
    assert [row['id'] for row in result['items']] == ['m1', 'm2']
    assert result['observation']['status'] == 'partial' and result['observation']['complete'] is False
    assert len(gmail.calls) == 3 and all(response.closed for response in gmail.responses)


def test_calendar_exact_window_and_projection_without_raw_provider_fields():
    row = {'id': 'e1', 'summary': 'Meeting', 'start': {'dateTime': '2026-10-01T10:00:00Z'},
           'end': {'dateTime': '2026-10-01T11:00:00Z'}, 'attendees': [{'email': 'ada@example.com'}],
           'raw_provider_secret': 'do not expose'}
    calendar = Session(calendar=[dict(row, id='e'+str(i)) for i in range(100)], next_page=True)
    result = api.read_owner('calendar_list', {'start': '2026-10-01T00:00:00Z',
        'end': '2026-10-02T00:00:00Z'}, lambda: calendar)
    assert result['source'] == 'calendar' and result['observation']['status'] == 'partial'
    assert result['items'][0]['summary'] == 'Meeting' and 'raw_provider_secret' not in str(result)
    assert calendar.closed and calendar.calls[1][1]['maxResults'] == 100
    class MissingItems(Session):
        def get(self, url, **kwargs):
            if url.endswith('/calendar/v3/calendars/primary/events'):
                response = Response({})
                self.responses.append(response)
                return response
            return super().get(url, **kwargs)
    empty = MissingItems()
    empty_result = api.read_owner('calendar_list', {'start': '2026-10-01T00:00:00Z',
        'end': '2026-10-02T00:00:00Z'}, lambda: empty)
    assert empty_result['items'] == [] and empty_result['observation']['status'] == 'ok'
    assert api.read_owner('calendar_get', {'event_id': 'e1@google.com'},
        lambda: Session(calendar=[row]))['item']['id'] == 'e1'


def test_invalid_requests_and_oversized_results_never_become_partial_success():
    gmail = Session(gmail=[{'id': 'm1', 'subject': 'x' * 3000}])
    for operation, args in [('gmail_send', {'to': 'ada@example.com'}),
                            ('gmail_search', {'query': 'in:inbox', 'max': 21}),
                            ('calendar_list', {'start': '2026-10-01T00:00:00Z',
                                               'end': '2026-12-01T00:00:00Z'}),
                            ('gmail_get', {'message_id': '../secret'})]:
        with pytest.raises(ValueError):
            api.read_owner(operation, args, lambda: gmail)
    with pytest.raises(ValueError, match='bound'):
        api.read_owner('gmail_search', {'query': 'in:inbox', 'max': 2}, lambda: gmail)
    assert gmail.closed


def test_every_typed_get_bounds_wire_before_json_and_projection(monkeypatch):
    parsed = []
    monkeypatch.setattr(api, '_owner_projection', lambda *args: parsed.append(args))
    class Oversize(Session):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs['params']))
            response = Response(b'X' * (api._OWNER_GMAIL_WIRE_BYTES + 1))
            self.responses.append(response)
            return response
    for operation, args in [
        ('gmail_search', {'query': 'in:inbox', 'max': 1}),
        ('gmail_get', {'message_id': 'm1'}),
        ('calendar_list', {'start': '2026-10-01T00:00:00Z', 'end': '2026-10-02T00:00:00Z'}),
        ('calendar_get', {'event_id': 'e1'}),
        ('contacts_search', {'query': 'Ada', 'max': 1}),
        ('contacts_get', {'resource_name': 'people/c123'}),
    ]:
        session = Oversize()
        with pytest.raises(ValueError, match='wire bound'):
            api.read_owner(operation, args, lambda: session)
        assert session.closed and session.responses[0].closed
    assert parsed == []


def test_calendar_occurrence_exposes_only_actual_parent_identifier():
    row = {'id': 'occurrence', 'recurringEventId': 'actual-master',
           'start': {'dateTime': '2026-10-01T10:00:00Z'}, 'end': {'dateTime': '2026-10-01T11:00:00Z'},
           'etag': 'private-review-etag', 'attendees': [{'email': 'owner@example.com', 'self': True}]}
    session = Session(calendar=[row])
    result = api.read_owner('calendar_get', {'event_id': 'occurrence'}, lambda: session)
    assert result['item']['recurringEventId'] == 'actual-master'
    assert 'recurringEventId' in session.calls[0][1]['fields']
    assert 'etag' not in result['item'] and 'self' not in result['item']['attendees'][0]


class ContactsSession(Session):
    def __init__(self, people):
        super().__init__()
        self.people = people

    def get(self, url, **kwargs):
        assert kwargs['stream'] and kwargs['allow_redirects'] is False
        assert 0 < kwargs['timeout'] <= 5
        params = kwargs['params']
        assert params['sources'] == ['READ_SOURCE_TYPE_CONTACT']
        self.calls.append((url, params))
        if url == 'https://people.googleapis.com/v1/people:searchContacts':
            assert 'metadata' in params['readMask']
            value = {} if params['query'] == '' else {'results': [{'person': p} for p in self.people]}
        else:
            assert url == 'https://people.googleapis.com/v1/people/c123'
            assert 'metadata' in params['personFields']
            value = self.people[0]
        response = Response(value)
        self.responses.append(response)
        return response


def contact():
    return {'resourceName': 'people/c123', 'etag': 'person-v1',
            'names': [{'givenName': 'Ada', 'familyName': 'Example'}],
            'emailAddresses': [{'value': 'ada@example.com', 'type': 'work'}],
            'phoneNumbers': [{'value': '+15550000001'}, {'value': '+15550000002'}],
            'metadata': {'sources': [{'type': 'CONTACT', 'id': '123', 'etag': 'source-v1'},
                                     {'type': 'PROFILE', 'id': 'private-profile', 'etag': 'other'}]},
            'photos': [{'url': 'not part of contact projection'}]}


def test_contacts_prefix_search_warms_cache_and_marks_capped_results_partial():
    session = ContactsSession([contact()])
    result = api.read_owner('contacts_search', {'query': 'Ada', 'max': 1}, lambda: session)
    assert [p['query'] for _, p in session.calls] == ['', 'Ada']
    assert result['source'] == 'google_contacts'
    assert result['observation']['complete'] is False
    assert result['observation']['status'] == 'partial'
    assert result['items'][0]['resource_name'] == 'people/c123'
    assert result['items'][0]['contact_sources'] == [{'type': 'CONTACT', 'id': '123', 'etag': 'source-v1'}]
    assert len(result['items'][0]['fields']['phoneNumbers']) == 2
    assert result['items'][0]['fields']['birthdays'] == []
    assert 'photos' not in str(result) and 'private-profile' not in str(result)
    assert session.closed and all(r.closed for r in session.responses)
    empty = ContactsSession([])
    result = api.read_owner('contacts_search', {'query': 'Unknown', 'max': 30}, lambda: empty)
    assert result['items'] == [] and result['observation']['complete'] is True


def test_contacts_exact_get_rejects_wrong_identity_and_invalid_requests():
    session = ContactsSession([contact()])
    result = api.read_owner('contacts_get', {'resource_name': 'people/c123'}, lambda: session)
    assert result['item']['fields']['emailAddresses'][0]['value'] == 'ada@example.com'
    assert result['observation']['complete'] is True and session.closed
    wrong = contact()
    wrong['resourceName'] = 'people/c999'
    with pytest.raises(ValueError, match='different contact'):
        api.read_owner('contacts_get', {'resource_name': 'people/c123'}, lambda: ContactsSession([wrong]))
    for operation, args in [
        ('contacts_search', {'query': 'Ada', 'max': True}),
        ('contacts_search', {'query': 'Ada', 'max': 31}),
        ('contacts_search', {'query': '\nAda', 'max': 1}),
        ('contacts_get', {'resource_name': 'people/me'}),
        ('contacts_get', {'resource_name': 'people/c123/../me'}),
    ]:
        invalid = ContactsSession([])
        with pytest.raises(ValueError):
            api.read_owner(operation, args, lambda: invalid)
        assert not invalid.calls and invalid.closed


def test_selected_calendar_get_encodes_exact_pair_and_echoes_selector(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    settings = tmp_path / 'config/settings.json'
    settings.parent.mkdir()
    settings.write_text(json.dumps({'google_calendar_selection': {
        'mode': 'selected', 'calendar_ids': ['team#calendar%@example.com']}}))
    class Selected(Session):
        def get(self,url,**kwargs):
            urls = ['https://www.googleapis.com/calendar/v3/users/me/calendarList/team%23calendar%25%40example.com',
                    'https://www.googleapis.com/calendar/v3/calendars/team%23calendar%25%40example.com/events/e1']
            assert url == urls[len(self.calls)]
            self.calls.append((url,kwargs))
            response=Response({'id':'team#calendar%@example.com','accessRole':'reader'}
                              if len(self.calls) == 1 else {'id':'e1','summary':'Secondary'})
            self.responses.append(response)
            return response
    session=Selected()
    result=api.read_owner('calendar_get',{'event_id':'e1','calendar_id':'team#calendar%@example.com'},lambda:session)
    assert result['item']['calendar_id']=='team#calendar%@example.com' and result['item']['id']=='e1'
    assert session.closed


def test_selected_calendar_get_rejects_mismatched_provider_identity():
    with pytest.raises(ValueError,match='identity'):
        api.read_owner('calendar_get',{'event_id':'different','calendar_id':'primary'},
            lambda:Session(calendar=[{'id':'e1'}]))


def test_typed_multi_calendar_selection_partial_and_exact_event_pairs(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    class Multi(Session):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs['params']))
            if url.endswith('/calendarList'):
                value = {'items':[{'id':'primary','accessRole':'owner'}, {'id':'team#calendar','accessRole':'reader'}, {'id':'revoked','accessRole':'reader'}]}
            elif '/revoked/events' in url:
                raise RuntimeError('revoked')
            else:
                assert '/primary/events' in url or '/team%23calendar/events' in url
                value = {'items':[{'id':'same','summary':'Event', 'start':{'date':'2026-11-01'},'end':{'date':'2026-11-02'}}]}
            response = Response(value); self.responses.append(response); return response
    session = Multi()
    result = api.read_owner('calendar_list', {'start':'2026-11-01T00:00:00Z','end':'2026-11-02T00:00:00Z'},lambda:session)
    assert result['observation']['status'] == 'partial'
    assert [(row['calendar_id'], row['id']) for row in result['items']] == [('primary','same'),('team#calendar','same')]
    assert result['calendars'][-1] == {'calendar_id':'revoked','status':'unavailable'}
    assert session.closed and all(response.closed for response in session.responses)


def test_calendar_evidence_retains_availability_and_owner_response(monkeypatch):
    monkeypatch.delenv('SOTTO_USER_EMAIL', raising=False)
    row = {'id': 'evidence', 'summary': 'Evening hold', 'status': 'tentative',
           'transparency': 'transparent', 'eventType': 'default',
           'start': {'dateTime': '2026-11-03T18:00:00-08:00', 'timeZone': 'America/Los_Angeles'},
           'end': {'dateTime': '2026-11-03T19:00:00-08:00', 'timeZone': 'America/Los_Angeles'},
           'recurringEventId': 'series',
           'originalStartTime': {'dateTime': '2026-11-03T17:00:00-08:00'},
           'attendees': [{'email': 'owner@example.com', 'self': True, 'responseStatus': 'tentative'},
                         {'email': 'other@example.com', 'responseStatus': 'accepted'}]}
    for operation, arguments in [('calendar_list', {'start': '2026-11-03T00:00:00Z', 'end': '2026-11-04T00:00:00Z'}),
                                 ('calendar_get', {'event_id': 'evidence'})]:
        session = Session(calendar=[row])
        result = api.read_owner(operation, arguments, lambda: session)
        event = result['items'][0] if operation == 'calendar_list' else result['item']
        for key in ('transparency', 'eventType', 'status', 'start', 'end', 'originalStartTime'):
            assert event[key] == row[key]
            assert key in session.calls[-1][1]['fields']
        assert event['attendees'][0]['owner'] is True
        assert event['attendees'][0]['responseStatus'] == 'tentative'
        assert event['attendees'][1]['owner'] is False
    shared = api._owner_projection('calendar', {**row, 'calendar_id': 'team'})
    assert shared['attendees'][0]['owner'] is False
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'OWNER@example.com')
    assert api._owner_projection('calendar', {**row, 'calendar_id': 'team'})['attendees'][0]['owner'] is True
