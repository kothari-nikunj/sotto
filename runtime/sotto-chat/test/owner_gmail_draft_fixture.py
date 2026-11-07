"""Offline Gmail provider for connected Node tests; real readers/MIME/writer remain in use."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(os.environ['SOTTO_DATA'])


def message():
    return {'id': 'message-123', 'threadId': 'thread-123', 'snippet': 'Please review the cedar packet.',
            'payload': {'mimeType': 'text/plain', 'headers': [
                {'name': 'From', 'value': 'Alice <alice@example.com>'},
                {'name': 'Reply-To', 'value': 'bob@example.com' if (DATA / 'target_changed').exists() else 'alice@example.com'},
                {'name': 'To', 'value': 'owner@example.com'},
                {'name': 'Subject', 'value': 'Cedar review'},
                {'name': 'Message-ID', 'value': '<cedar@example.com>'}]}}


class Gmail:
    def users(self): return self
    def messages(self): return self
    def drafts(self): return self
    def get(self, **kwargs):
        assert kwargs == {'userId': 'me', 'id': 'message-123', 'format': 'full'}
        return SimpleNamespace(execute=message)
    def getProfile(self, **kwargs):  # noqa: N802 -- mirrors the Google API method.
        assert kwargs == {'userId': 'me'}
        return SimpleNamespace(execute=lambda: {'emailAddress': 'owner@example.com'})
    def create(self, **kwargs):
        def execute():
            with (DATA / 'provider-calls.jsonl').open('a') as stream:
                stream.write(json.dumps({'operation': 'drafts.create', 'args': kwargs}) + '\n')
            if (DATA / 'provider_unknown').exists():
                raise TimeoutError('synthetic provider accepted before response loss')
            return {'id': 'draft-123', 'message': {'threadId': 'thread-123'}}
        return SimpleNamespace(execute=execute)
    def send(self, **kwargs):
        raise AssertionError('send APIs are forbidden in this fixture')


class Response:
    def __init__(self, value=None): self.value = message() if value is None else value
    status_code = 200
    headers = {}
    def iter_content(self, chunk_size):
        yield json.dumps(self.value).encode()
    def close(self): pass


class Session:
    def get(self, url, **kwargs):
        if os.environ.get('SOTTO_TEST_STYLE_APPLY') == '1' and url == 'https://gmail.googleapis.com/gmail/v1/users/me/messages':
            return Response({'messages': [{'id': 'message-123', 'threadId': 'thread-123'}], 'resultSizeEstimate': 1})
        assert url == 'https://gmail.googleapis.com/gmail/v1/users/me/messages/message-123'
        return Response()
    def close(self): pass


def read_owner(operation, arguments):
    spec = importlib.util.spec_from_file_location('real_gmail_reader', ROOT / 'adapters/sotto/google_api.py')
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api.read_owner(operation, arguments, session_factory=Session)


def configure(receiver):
    if os.environ.get('SOTTO_TEST_STYLE_APPLY') == '1':
        import managed  # noqa: PLC0415 -- loaded only by the opt-in receiver fixture.
        managed.record_google_consent(str(DATA), ['https://www.googleapis.com/auth/gmail.modify'])
        style = DATA / 'style.json'
        if not style.exists():
            style.write_text(json.dumps({'schema_version': 2, 'canonical': {'work_email': [
                {'text': 'Thanks for asking. I will pass on this one.', 'channel': 'email',
                 'context': 'work', 'bucket': 'work_email', 'quality': 0.95,
                 'recipient': 'Alice', 'canonical_id': 'c_abcdef', 'date': '2026-10-05'}]},
                'recent': [], 'confirmed': [], 'per_person': {}}))
    tool = receiver.RUNTIME_OWNER_GMAIL_DRAFT_TOOL
    google = tool._google()
    google._gmail_service = Gmail
    google.capabilities = lambda: {'gmail_read': True, 'gmail_draft': not (DATA / 'draft_revoked').exists()}
    tool._google = lambda: google
    receiver._google_api_py = lambda: str(Path(__file__).resolve())
    receiver._runtime_source_authority = lambda: {'version': 2, 'complete': True,
        'revision': 'a' * 64, 'bridge_sources': [], 'google_sources': ['gmail']}
