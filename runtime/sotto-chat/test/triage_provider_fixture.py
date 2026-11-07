"""Synthetic Google/Bridge HTTP only; original gathers, classifier and Gmail writers execute."""
import base64
import importlib.util
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httplib2

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(os.environ['SOTTO_DATA'])


def message(mid):
    rows = {
        'message-123': ('thread-123', 'Alice <alice@example.com>', 'Cedar review', ['INBOX', 'IMPORTANT', 'UNREAD'], 'Please review the cedar packet.'),
        'ordinary-456': ('thread-456', 'Dana <dana@example.com>', 'Lunch next week', ['INBOX'], 'Would Tuesday work?'),
        'promo-789': ('thread-789', 'Offers <offers@example.com>', 'Sale', ['CATEGORY_PROMOTIONS'], 'Special offer.'),
        'answered-111': ('thread-111', 'Eli <eli@example.com>', 'Deck received?', ['INBOX'], 'Did you get the deck?'),
        'sent-222': ('thread-111', 'Owner <owner@example.com>', 'Re: Deck received?', ['SENT'], 'Yes, received.'),
    }
    thread, sender, subject, labels, body = rows[mid]
    return {'id': mid, 'threadId': thread, 'snippet': body, 'labelIds': labels,
            'internalDate': '1791158400000' if mid == 'sent-222' else '1791154800000',
            'payload': {'mimeType': 'multipart/mixed', 'headers': [
                {'name': 'From', 'value': sender}, {'name': 'To', 'value': 'owner@example.com'},
                {'name': 'Subject', 'value': subject}, {'name': 'Message-ID', 'value': '<cedar@example.com>'},
                {'name': 'Date', 'value': 'Sun, 04 Oct 2026 13:00:00 -0700' if mid == 'sent-222' else 'Sun, 04 Oct 2026 12:00:00 -0700'}],
                'parts': [{'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode(body.encode()).decode(), 'size': len(body)}},
                          {'mimeType': 'application/pdf', 'filename': 'packet.pdf', 'body': {'attachmentId': 'never-download', 'size': 100}}]}}


def wire(url, params=None):
    parsed = urlparse(url)
    assert parsed.hostname in ('gmail.googleapis.com', 'www.googleapis.com'), 'unexpected external destination'
    query = {**parse_qs(parsed.query), **(params or {})}
    with (DATA / 'triage-http.jsonl').open('a') as log:
        log.write(json.dumps({'path': parsed.path, 'query': query}) + '\n')
    if '/attachments/' in parsed.path:
        raise AssertionError('triage must not download or convert attachments')
    suffix = parsed.path.split('/users/me/')[-1]
    if suffix == 'messages':
        q = query.get('q', '')
        q = q[0] if isinstance(q, list) else q
        ids = ['sent-222'] if q.startswith('in:sent') else ['message-123'] if 'from:alice@example.com' in q else ['ordinary-456', 'promo-789', 'answered-111', 'message-123']
        return {'messages': [{'id': mid, 'threadId': message(mid)['threadId']} for mid in ids], 'resultSizeEstimate': len(ids)}
    if suffix.startswith('messages/'):
        return message(suffix.split('/')[1])
    if suffix == 'profile':
        return {'emailAddress': 'owner@example.com'}
    raise AssertionError('unexpected Google HTTP operation')


def configure_http():
    def request(_self, uri, method='GET', body=None, headers=None, **kwargs):
        assert method == 'GET', 'provider write forbidden in gather'
        return httplib2.Response({'status': '200', 'content-type': 'application/json'}), json.dumps(wire(uri)).encode()
    httplib2.Http.request = request


class Response:
    status_code = 200
    headers = {}
    def __init__(self, value): self.value = value
    def iter_content(self, chunk_size): yield json.dumps(self.value).encode()
    def close(self): pass


class Session:
    def get(self, url, **kwargs): return Response(wire(url, kwargs.get('params')))
    def close(self): pass


def read_owner(operation, arguments):
    spec = importlib.util.spec_from_file_location('triage_real_google_reader', ROOT / 'adapters/sotto/google_api.py')
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api.read_owner(operation, arguments, session_factory=Session)


def configure(receiver):
    import managed  # noqa: PLC0415 -- receiver path is installed before fixture configuration.
    import owner_gmail_draft_fixture  # noqa: PLC0415 -- isolate provider setup from child transport bootstrap.
    owner_gmail_draft_fixture.configure(receiver)
    managed.record_google_consent(DATA, ['https://www.googleapis.com/auth/gmail.readonly', 'https://www.googleapis.com/auth/gmail.compose'])
    managed.record_bridge_consent(DATA, ['imessage', 'whatsapp', 'contacts'], ['imessage', 'whatsapp', 'contacts'])
    receiver._runtime_source_authority = lambda: {'version': 2, 'complete': True, 'revision': 'a' * 64,
        'bridge_sources': ['imessage', 'whatsapp', 'contacts'], 'google_sources': ['gmail']}
    receiver._google_api_py = lambda: str(Path(__file__).resolve())
    home = Path(os.environ['SOTTO_HOME'])
    home.mkdir(parents=True, exist_ok=True)
    token = home / 'google_token.json'
    token.write_text(json.dumps({'token': 'fixture-token', 'refresh_token': 'fixture-refresh',
        'client_id': 'fixture-client', 'client_secret': 'fixture-secret', 'token_uri': 'https://oauth2.googleapis.com/token',
        'expiry': '2099-01-01T00:00:00Z', 'scopes': ['https://www.googleapis.com/auth/gmail.modify'],
        'sotto_source_generation': 'a' * 64}))
    token.chmod(0o600)
    injection = DATA / 'triage-python-boundary'
    injection.mkdir(parents=True, exist_ok=True)
    fixture = str(Path(__file__).resolve())
    bootstrap = f"import importlib.util; s=importlib.util.spec_from_file_location('triage_http', {fixture!r}); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.configure_http()"
    (injection / 'sitecustomize.py').write_text(bootstrap)
    os.environ['PYTHONPATH'] = str(injection)
    module = receiver.RUNTIME_KNOWLEDGE_READ
    original = module.subprocess.Popen
    def start(command, *args, **kwargs):
        if any(str(item).endswith('/triage_read.py') for item in command):
            script = next(str(item) for item in command if str(item).endswith('/triage_read.py'))
            code = bootstrap + '; import runpy,sys; sys.argv=[sys.argv[1]]; runpy.run_path(sys.argv[0],run_name="__main__")'
            command = [command[0], '-I', '-B', '-c', code, script]
        return original(command, *args, **kwargs)
    module.subprocess.Popen = start
    def bridge(request):
        assert request['method'] == 'tools/call'
        assert request['params'] == {'name': 'read_local', 'arguments': {'since_hours': 72}}
        with (DATA / 'triage-bridge.jsonl').open('a') as log:
            log.write(json.dumps(request) + '\n')
        local = {'contacts': [{'name': 'Jake Rosen', 'phones': ['+12069994970']}, {'name': 'Mira', 'phones': ['+12065550000']}],
            'imessage': [{'handle': '+12069994970', 'text': 'Can you intro me?', 'is_from_me': False, 'timestamp': '2026-10-04T10:00'},
                         {'handle': '+18885551234', 'text': 'your code is 123', 'is_from_me': False, 'timestamp': '2026-10-04T09:00'},
                         {'handle': '+12069994970', 'chat_guid': 'group-chat', 'group_name': 'Planning group', 'is_group_chat': True,
                          'text': 'Are you coming?', 'is_from_me': False, 'timestamp': '2026-10-04T11:00'}],
            'whatsapp': [{'contact_jid': '12065550000@s.whatsapp.net', 'push_name': 'Mira', 'text': 'Tuesday still good?', 'is_from_me': False, 'timestamp': '2026-10-04T12:00'}],
            'source_status': {name: 'ok' for name in ('imessage', 'whatsapp', 'contacts')}}
        return {'jsonrpc': '2.0', 'id': request.get('id'), 'result': {'content': [{'type': 'text', 'text': json.dumps(local)}]}}
    receiver.RELAY.mcp_call = bridge
