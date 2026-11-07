"""Reviewed reply uses original MIME/writer; uncertain sends never replay."""
import base64
from email import message_from_bytes
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

import runtime_owner_gmail_draft_tool as tool
from test_runtime_owner_gmail_draft_tool import setup, request, Gmail
from test_runtime_owner_loop_close import context

__all__ = ['setup', 'context']


def adapter():
    path = Path(__file__).resolve().parents[2] / 'adapters/sotto/google_api.py'
    spec = importlib.util.spec_from_file_location('send_test_original_api', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SendGmail(Gmail):
    def __init__(self):
        super().__init__()
        self.posts = []
        self.loss = self.bad_readback = False
        self.raw = None
        self.closes = 0

    def close(self):
        self.closes += 1

    def get(self, **kwargs):
        if kwargs['id'] == 'sent-123':
            assert kwargs['format'] == 'raw'
            return SimpleNamespace(execute=lambda: {'id': 'sent-123', 'threadId': self.thread,
                'labelIds': [] if self.bad_readback else ['SENT'], 'raw': self.raw})
        return super().get(**kwargs)

    def send_reviewed(self, payload):
        self.posts.append(payload)
        mail = message_from_bytes(base64.urlsafe_b64decode(payload['raw'] + '=' * (-len(payload['raw']) % 4)))
        mail['From'] = 'owner@example.com'
        self.raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        if self.loss:
            raise TimeoutError('accepted response lost')
        return {'id': 'sent-123', 'threadId': self.thread}


@pytest.fixture
def sending(setup, monkeypatch):
    data, policy, _, grants, call = setup
    google = tool._google()
    gmail = SendGmail()
    grants['gmail_send'] = True
    monkeypatch.setattr(google, '_gmail_send_service', lambda: gmail)
    monkeypatch.setattr(google, '_gmail_adapter', adapter)
    return data, policy, gmail, grants, call


def send_request(sending):
    call = sending[-1]
    proposed = request({'operation': 'propose_send', 'message_id': 'message-123',
                        'body': 'Cedar reviewed.\nThank you.'}, 'preview', event='d' * 64,
                       text='Send a reply to Alice.')
    preview = call(proposed)
    assert preview['status'] == 'preview'
    assert preview['review']['kind'] == 'reply_send'
    assert not sending[2].posts
    return request({'operation': 'send', 'review': preview['review'], 'proposal': {
        'event_id': proposed['event_id'], 'action_id': proposed['action_id'],
        'args_hash': tool.digest(proposed['args']), 'accepted_dispatch_id': 'accepted-review',
        'source_revision': 'c' * 64}})


def test_original_send_and_historical_inspect(sending):
    body = send_request(sending)
    result = sending[-1](body)
    assert result['status'] == 'committed'
    assert result['outcome']['operation'] == 'send'
    assert result['outcome']['message_id'] == 'sent-123'
    assert len(sending[2].posts) == 1
    mail = message_from_bytes(base64.urlsafe_b64decode(sending[2].posts[0]['raw'] + '=='))
    assert mail['In-Reply-To'] == '<cedar@example.com>'
    assert mail['References'] == '<cedar@example.com>'
    assert mail.get_payload(decode=True).decode('utf-8') == body['args']['review']['body']
    sending[3]['gmail_send'] = False
    sending[1]['_source_authority'] = lambda: {'complete': False}
    assert sending[-1]({**body, 'operation': 'inspect'}) == result
    assert len(sending[2].posts) == 1


@pytest.mark.parametrize('fault', ['loss', 'bad_readback'])
def test_unknown_send_never_reapplied(sending, fault):
    body = send_request(sending)
    setattr(sending[2], fault, True)
    assert sending[-1](body) == {'version': 1, 'status': 'unknown'}
    setattr(sending[2], fault, False)
    assert sending[-1]({**body, 'operation': 'inspect'}) == {'version': 1, 'status': 'unknown'}
    assert sending[-1](body) == {'version': 1, 'status': 'unknown'}
    assert len(sending[2].posts) == 1


def test_aged_unknown_send_keeps_replay_fence_and_consumed_approval(sending):
    body = send_request(sending)
    sending[2].loss = True
    assert sending[-1](body) == {'version': 1, 'status': 'unknown'}
    path = sending[0] / 'events/owner-gmail-drafts.json'
    assert tool.compact(path, now=body['admitted_at_ms'] / 1000 + 31 * 86400) >= 1
    saved = tool._load(path)
    assert saved['actions'][body['event_id']]['status'] == 'contacting'
    assert saved['actions'][body['event_id']]['proof']['compacted'] is True
    assert saved['consumed'][body['args']['proposal']['action_id']] == body['event_id']
    sending[2].loss = False
    assert sending[-1]({**body, 'operation': 'inspect'}) == {'version': 1, 'status': 'unknown'}
    assert sending[-1](body) == {'version': 1, 'status': 'unknown'}
    assert len(sending[2].posts) == 1


@pytest.mark.parametrize('change', ['sender', 'recipient', 'grant', 'wrong_kind'])
def test_change_prevents_send(sending, monkeypatch, change):
    body = send_request(sending)
    if change == 'sender':
        monkeypatch.setattr(sending[2], 'getProfile', lambda **kw: SimpleNamespace(execute=lambda: {'emailAddress': 'other@example.com'}))
    elif change == 'recipient':
        sending[2].recipient = 'Other <other@example.com>'
    elif change == 'grant':
        sending[3]['gmail_send'] = False
    else:
        body['args']['operation'] = 'save'
        body = request(body['args'])
    if change == 'wrong_kind':
        with pytest.raises(ValueError):
            sending[-1](body)
    else:
        assert sending[-1](body)['status'] == 'rejected'
    assert not sending[2].posts


@pytest.mark.parametrize('mode', ['reset', '401', '500', 'redirect'])
def test_actual_authorized_transport_no_post_replay(monkeypatch, mode):
    """Installed requests/urllib3/auth stack, actual loopback socket, no provider."""
    import datetime
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import socket
    import threading
    from google.oauth2.credentials import Credentials
    from requests.adapters import HTTPAdapter

    api = adapter()
    counts, attempts, refreshes = [], [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):  # noqa: N802
            counts.append(self.path)
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            if mode == 'reset':
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(302 if mode == 'redirect' else int(mode))
            self.send_header('Content-Length', '0')
            if mode == 'redirect':
                self.send_header('Location', '/second')
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    credentials = Credentials(token='synthetic-token', expiry=datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + datetime.timedelta(hours=1))
    monkeypatch.setattr(Credentials, 'from_authorized_user_file', lambda path: credentials)
    def refresh(request):
        refreshes.append(True)
        raise AssertionError('send cannot refresh and reissue')
    monkeypatch.setattr(credentials, 'refresh', refresh)
    monkeypatch.setattr(api, '_shared_library', lambda: SimpleNamespace(token_path=lambda: 'synthetic-not-read'))
    original = HTTPAdapter.send
    def local_send(self, request, **kwargs):
        attempts.append((request.method, request.url))
        assert request.url == 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'
        assert self.max_retries.total == 0
        request.url = f'http://127.0.0.1:{server.server_port}/send'
        # Proxies were selected for Gmail before this fixture rewrote the destination.
        # Keep the synthetic loopback request local even in a proxy-configured CI environment.
        kwargs['proxies'] = {}
        return original(self, request, **kwargs)
    monkeypatch.setattr(HTTPAdapter, 'send', local_send)
    try:
        with pytest.raises(Exception):
            api._owner_send({'raw': 'synthetic'})
        assert counts == ['/send']
        assert attempts == [('POST', 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send')]
        assert refreshes == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_final_original_writer_get_cannot_rebase(sending, monkeypatch):
    body = send_request(sending)
    original = sending[2].get
    reads = []
    def changing(**kwargs):
        reads.append(kwargs)
        if len(reads) == 2:
            sending[2].recipient = 'Other <other@example.com>'
        return original(**kwargs)
    monkeypatch.setattr(sending[2], 'get', changing)
    assert sending[-1](body)['status'] == 'unknown'
    assert not sending[2].posts
    assert sending[-1]({**body, 'operation': 'inspect'})['status'] == 'unknown'


def test_post_send_revocation_prevents_private_readback(sending, monkeypatch):
    body = send_request(sending)
    original = sending[2].send_reviewed
    original_get = sending[2].get
    def accepted(payload):
        result = original(payload)
        sending[3]['gmail_send'] = False
        return result
    def get(**kwargs):
        assert kwargs['id'] != 'sent-123', 'no private read after revocation'
        return original_get(**kwargs)
    monkeypatch.setattr(sending[2], 'send_reviewed', accepted)
    monkeypatch.setattr(sending[2], 'get', get)
    # Gmail accepted the send: revocation skips only the private read-back, never the receipt.
    result = sending[-1](body)
    assert result['status'] == 'committed' and result['outcome']['message_id'] == 'sent-123'
    assert sending[-1]({**body, 'operation': 'inspect'}) == result
    assert len(sending[2].posts) == 1


@pytest.mark.parametrize('change', ['revision', 'revoked'])
def test_post_acceptance_authority_change_keeps_sent_receipt(sending, change):
    data, policy, gmail, grants, call = sending
    body = send_request(sending)
    original = gmail.send_reviewed

    def send_then_authority_changes(payload):
        result = original(payload)
        policy['_source_authority'] = (
            (lambda: {'complete': True, 'revision': 'e' * 64, 'google_sources': ['gmail']})
            if change == 'revision' else (lambda: {'complete': False}))
        return result

    gmail.send_reviewed = send_then_authority_changes
    first = call(body)
    assert len(gmail.posts) == 1  # the email really went out
    assert first['status'] == 'committed' and first['outcome']['message_id'] == 'sent-123'
    assert call({**body, 'operation': 'inspect'}) == first
    assert call(body) == first and len(gmail.posts) == 1


def test_extra_readback_recipient_holds(sending, monkeypatch):
    body = send_request(sending)
    original = sending[2].send_reviewed
    def accepted(payload):
        result = original(payload)
        mail = message_from_bytes(base64.urlsafe_b64decode(sending[2].raw))
        mail['Cc'] = 'extra@example.com'
        sending[2].raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        return result
    monkeypatch.setattr(sending[2], 'send_reviewed', accepted)
    assert sending[-1](body)['status'] == 'unknown'
    assert len(sending[2].posts) == 1


def test_committed_corrupt_receipt_refused(sending):
    import json
    body = send_request(sending)
    assert sending[-1](body)['status'] == 'committed'
    path = sending[0] / 'events/owner-gmail-drafts.json'
    doc = json.loads(path.read_text())
    doc['actions'][body['event_id']]['outcome']['thread_id'] = 'other'
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        sending[-1]({**body, 'operation': 'inspect'})
    assert len(sending[2].posts) == 1


def test_committed_persistence_failure_holds_without_repeat(sending, monkeypatch):
    body = send_request(sending)
    writer = tool._preferences()
    monkeypatch.setattr(tool, '_preferences', lambda: writer)
    original = writer._durable_preferences_replace
    def fail_commit(path, doc):
        record = doc.get('actions', {}).get(body['event_id'])
        if record and record['status'] == 'committed':
            raise OSError('synthetic receipt storage failure')
        return original(path, doc)
    monkeypatch.setattr(writer, '_durable_preferences_replace', fail_commit)
    assert sending[-1](body)['status'] == 'unknown'
    assert len(sending[2].posts) == 1
    monkeypatch.setattr(writer, '_durable_preferences_replace', original)
    assert sending[-1]({**body, 'operation': 'inspect'})['status'] == 'unknown'
    assert sending[-1](body)['status'] == 'unknown'
    assert len(sending[2].posts) == 1


@pytest.mark.parametrize('text', ['Yes, send it.', 'send this reply!', 'SEND IT'])
def test_explicit_send_affirmative(sending, text):
    body = send_request(sending)
    body = request(body['args'], text=text)
    assert sending[-1](body)['status'] == 'committed'
    assert len(sending[2].posts) == 1


@pytest.mark.parametrize('text', ['Do not send it.', 'Yes, send it to James.', 'send it and add James', 'No, send it.'])
def test_send_negation_or_extra_instruction_refused(sending, text):
    body = send_request(sending)
    body = request(body['args'], text=text)
    assert sending[-1](body) == {'version': 1, 'status': 'rejected', 'code': 'review_unavailable'}
    assert not sending[2].posts
