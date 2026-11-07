"""Synthetic HTTP boundary; original Gmail reader, MIME writer and auth session run."""
from email import message_from_bytes
import base64
import importlib.util
import json
import io
import os
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from requests import Response
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(os.environ['SOTTO_DATA'])


def source_message():
    return {'id': 'message-123', 'threadId': 'thread-123', 'snippet': 'Please review Cedar.',
        'payload': {'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode(b'Please review Cedar.').decode()},
        'headers': [{'name': 'From', 'value': 'Alice <alice@example.com>'},
            {'name': 'Reply-To', 'value': 'bob@example.com' if (DATA / 'target_changed').exists() else 'alice@example.com'},
            {'name': 'To', 'value': 'owner@example.com'}, {'name': 'Subject', 'value': 'Cedar review'},
            {'name': 'Message-ID', 'value': '<cedar@example.com>'},
            {'name': 'References', 'value': '<earlier@example.com>'}]}}


def synthetic_send(_adapter, request, **kwargs):
    url = urlparse(request.url)
    assert url.scheme == 'https' and url.netloc == 'gmail.googleapis.com'
    path = url.path
    if request.method == 'POST':
        assert path == '/gmail/v1/users/me/messages/send'
        payload = json.loads(request.body)
        with (DATA / 'provider-calls.jsonl').open('a') as stream:
            stream.write(json.dumps({'operation': 'messages.send', 'payload': payload}) + '\n')
        raw = base64.urlsafe_b64decode(payload['raw'] + '=' * (-len(payload['raw']) % 4))
        if os.environ.get('SOTTO_TEST_GMAIL_NEW_SEND') == '1':
            assert 'threadId' not in payload
            mime=message_from_bytes(raw)
            assert all(mime.get(name) is None for name in ('In-Reply-To','References','Cc','Bcc','Resent-To','Resent-Cc','Resent-Bcc'))
        raw = b'From: owner@example.com\n' + raw
        (DATA / 'sent-message.json').write_text(json.dumps({'id': 'sent-123', 'threadId': 'thread-123',
            'labelIds': ['SENT'], 'raw': base64.urlsafe_b64encode(raw).decode().rstrip('=')}))
        if (DATA / 'provider_unknown').exists():
            raise ConnectionError('synthetic response lost')
        result = {'id': 'sent-123', 'threadId': 'thread-123'}
    else:
        assert request.method == 'GET'
        with (DATA / 'provider-reads.jsonl').open('a') as stream:
            stream.write(json.dumps({'path': path, 'format': parse_qs(url.query).get('format')}) + '\n')
        if path.endswith('/profile'):
            result = {'emailAddress': 'owner@example.com'}
        elif path == '/gmail/v1/users/me/messages':
            result = {'messages': [{'id': 'message-123', 'threadId': 'thread-123'}]}
        elif path.endswith('/messages/message-123'):
            result = source_message()
        elif path.endswith('/messages/sent-123'):
            result = json.loads((DATA / 'sent-message.json').read_text())
        else:
            raise AssertionError('unapproved synthetic Gmail target')
    response = Response()
    response.status_code = 200
    response.raw = io.BytesIO(json.dumps(result).encode())
    response.headers['Content-Type'] = 'application/json'
    response.request = request
    return response


def adapter():
    spec = importlib.util.spec_from_file_location('synthetic_original_google_api', ROOT / 'adapters/sotto/google_api.py')
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def read_owner(operation, arguments):
    HTTPAdapter.send = synthetic_send
    return adapter().read_owner(operation, arguments)


def configure(receiver):
    HTTPAdapter.send = synthetic_send
    home = Path(os.environ['SOTTO_HOME'])
    home.mkdir(parents=True, exist_ok=True)
    token = home / 'google_token.json'
    token.write_text(json.dumps({'token': 'fixture-token', 'refresh_token': 'fixture-refresh',
        'client_id': 'fixture-client', 'client_secret': 'fixture-secret',
        'token_uri': 'https://oauth2.googleapis.com/token', 'expiry': '2099-01-01T00:00:00Z',
        'scopes': ['https://www.googleapis.com/auth/gmail.modify']}))
    token.chmod(0o600)
    tool = receiver.RUNTIME_OWNER_GMAIL_DRAFT_TOOL
    google = tool._google()
    google.capabilities = lambda: {'gmail_read': True, 'gmail_draft': True,
        'gmail_send': not (DATA / 'send_revoked').exists()}
    tool._google = lambda: google
    if os.environ.get('SOTTO_TEST_TYPED_OUTCOME') == '1':
        marker = DATA / 'typed-style-seeded'
        if not marker.exists():
            style_path = DATA / 'style.json'
            style_path.unlink(missing_ok=True)
            pack = Path(os.environ.get('SOTTO_SKILLS_ROOT', str(ROOT / 'sotto-chief-of-staff')))
            spec = importlib.util.spec_from_file_location('original_fixture_style_extract', pack / '_shared/scripts/style_extract.py')
            extract = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(extract)
            extract.extract({'sent_messages': [{'text': 'Thanks for asking. I will pass on this one.',
                'channel': 'email', 'recipient': 'Alice', 'canonical_id': 'c_abcdef',
                'date': '2026-10-05'}]})
            marker.write_text('seeded')
        writer = tool._preferences()
        original_sync = writer._sync_existing_preferences
        def sync(path):
            if Path(path) == DATA / 'outcomes.jsonl' and (DATA / 'learning_fault').exists():
                raise OSError('synthetic local outcome sync unavailable')
            return original_sync(path)
        writer._sync_existing_preferences = sync
        tool._preferences = lambda: writer
    receiver._google_api_py = lambda: str(Path(__file__).resolve())
    receiver._runtime_source_authority = lambda: {'version': 2, 'complete': True,
        'revision': 'a' * 64, 'bridge_sources': [], 'google_sources': ['gmail']}
