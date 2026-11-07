"""Synthetic fixed Google HTTP responses through the production typed reader."""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path

source_root = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('real_sotto_google_api', source_root / 'adapters/sotto/google_api.py')
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class Response:
    status_code = 200
    headers = {}
    def __init__(self, value):
        self.raw = json.dumps(value, ensure_ascii=False).encode()
    def iter_content(self, chunk_size):
        for offset in range(0, len(self.raw), chunk_size):
            yield self.raw[offset:offset + chunk_size]
    def close(self): pass


class Session:
    def get(self, url, **kwargs):
        assert kwargs['stream'] and kwargs['allow_redirects'] is False
        assert url.startswith(('https://gmail.googleapis.com/', 'https://www.googleapis.com/calendar/v3/'))
        if url.endswith('/gmail/v1/users/me/messages'):
            value = {'messages': [{'id': 'synthetic1'}]}
        elif url.endswith('/gmail/v1/users/me/messages/synthetic1'):
            value = {'id': 'synthetic1', 'threadId': 'thread1', 'snippet': 'Fixture preview',
                     'payload': {'headers': [{'name': 'Subject', 'value': 'Résumé 📨 / 東京'}]}}
        elif url.endswith('/gmail/v1/users/me/threads/thread1'):
            value = {'id': 'thread1', 'messages': [
                {'id': mid, 'threadId': 'thread1', 'internalDate': str(stamp), 'labelIds': ['SENT'] if mid == 'sent1' else ['INBOX'],
                 'payload': {'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode(body.encode()).decode()},
                             'headers': [{'name': 'From', 'value': sender}]}}
                for mid, stamp, sender, body in [
                    ('sent1', 2000, 'owner@example.test', 'Approved, please proceed.'),
                    ('incoming1', 1000, 'counsel@example.test', 'Please approve the Cedar grant.')]]}
            # An unsent newest draft must never become approval evidence.
            draft = {'id': 'draft1', 'threadId': 'thread1', 'internalDate': '9999', 'labelIds': ['DRAFT'],
                     'payload': {'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode(b'Unsent draft approval.').decode()}}}
            value['messages'].append(draft)
            if (Path(os.environ['SOTTO_GOOGLE_FIXTURE_ROOT']) / 'unicode_thread').exists():
                extra = copy.deepcopy(value['messages'][1])
                extra.update(id='forward1', internalDate='1500')
                value['messages'].append(extra)
                for message in value['messages']:
                    if 'DRAFT' not in message.get('labelIds', []):
                        message['payload']['body']['data'] = base64.urlsafe_b64encode(('漢' * 3000).encode()).decode()
        elif url.endswith('/calendar/v3/users/me/calendarList'):
            value = {'items': [{'id': 'primary', 'accessRole': 'owner'}]}
            if (Path(os.environ['SOTTO_GOOGLE_FIXTURE_ROOT']) / 'shared_calendar').exists():
                value['items'].append({'id': 'team', 'accessRole': 'reader'})
        elif url.endswith('/calendar/v3/calendars/primary/events'):
            value = {}  # A valid empty Google collection may omit items.
        elif url.endswith('/calendar/v3/calendars/team/events'):
            assert 'organizer(email,self)' in kwargs['params']['fields']
            assert 'attendees(email,responseStatus,self,resource)' in kwargs['params']['fields']
            value = {'items': [
                {'id': 'other', 'summary': 'Other person meeting', 'attendees': [{'email': 'colleague@example.com', 'self': True}]},
                {'id': 'mine', 'summary': 'Owner invited', 'attendees': [{'email': 'owner@example.com', 'responseStatus': 'accepted'}]},
                {'id': 'declined', 'summary': 'Declined meeting', 'attendees': [{'email': 'owner@example.com', 'responseStatus': 'declined'}]},
            ]}
        else:
            raise AssertionError('Unexpected Google URL')
        if (Path(os.environ['SOTTO_GOOGLE_FIXTURE_ROOT']) / 'revoke_during_read').exists():
            (Path(os.environ['SOTTO_HOME']) / 'google_token.json').unlink(missing_ok=True)
        return Response(value)
    def close(self): pass


def read_owner(operation, arguments):
    return api.read_owner(operation, arguments, session_factory=Session)
