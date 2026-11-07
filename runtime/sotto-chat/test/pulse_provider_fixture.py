"""Synthetic external Google and Bridge transports; original pulse computation is untouched."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httplib2

DATA = Path(os.environ['SOTTO_DATA'])


def message(ident):
    sent = ident == 'pulse-sent'
    date = datetime.now(timezone.utc) - timedelta(days=7 if sent else 4)
    return {'id': ident, 'threadId': 'pulse-dana-thread', 'internalDate': str(int(date.timestamp() * 1000)),
            'snippet': 'The Cedar deck is ready.' if sent else 'Could you share the Cedar deck?',
            'labelIds': ['SENT'] if sent else ['INBOX'],
            'payload': {'mimeType': 'text/plain', 'headers': [
                {'name': 'From', 'value': 'Owner <owner@example.com>' if sent else 'Dana <dana@example.com>'},
                {'name': 'To', 'value': 'Dana <dana@example.com>' if sent else 'Owner <owner@example.com>'},
                {'name': 'Subject', 'value': 'Cedar deck'},
                {'name': 'Date', 'value': date.strftime('%a, %d %b %Y %H:%M:%S +0000')}], 'body': {'size': 0}}}


def configure_http():
    def request(_self, uri, method='GET', body=None, headers=None, **kwargs):
        assert method == 'GET', 'pulse gather cannot write Google'
        parsed = urlparse(uri)
        assert parsed.hostname in ('gmail.googleapis.com', 'www.googleapis.com')
        query = parse_qs(parsed.query)
        with (DATA / 'pulse-google-calls.jsonl').open('a') as log:
            log.write(json.dumps({'path': parsed.path, 'query': query}) + '\n')
        suffix = parsed.path.split('/users/me/')[-1]
        if suffix == 'messages':
            q = query['q'][0]
            assert 'after:' in q and 'before:' in q
            ident = 'pulse-sent' if q.startswith('in:sent') else 'pulse-inbound'
            value = {'messages': [{'id': ident, 'threadId': 'pulse-dana-thread'}], 'resultSizeEstimate': 1}
        elif suffix.startswith('messages/') and '/attachments/' not in suffix:
            value = message(suffix.split('/')[1])
        elif suffix == 'profile':
            value = {'emailAddress': 'owner@example.com'}
        else:
            raise AssertionError('unexpected pulse Google endpoint')
        return httplib2.Response({'status': '200', 'content-type': 'application/json'}), json.dumps(value).encode()
    httplib2.Http.request = request


def configure(receiver):
    import managed  # noqa: PLC0415 — receiver bootstraps shared paths; Google sitecustomize does not.
    managed.record_google_consent(DATA, ['https://www.googleapis.com/auth/gmail.readonly'])
    managed.record_bridge_consent(DATA, ['imessage', 'contacts'], ['imessage', 'contacts'])
    receiver._runtime_source_authority = lambda: {'version': 2, 'complete': True, 'revision': 'a' * 64,
        'bridge_sources': ['imessage', 'contacts'], 'google_sources': ['gmail'], 'connector_sources': []}
    os.environ['SOTTO_USER_EMAIL'] = 'owner@example.com'
    home = Path(os.environ['SOTTO_HOME'])
    home.mkdir(parents=True, exist_ok=True)
    token = home / 'google_token.json'
    token.write_text(json.dumps({'token': 'fixture-token', 'refresh_token': 'fixture-refresh',
        'client_id': 'fixture-client', 'client_secret': 'fixture-secret', 'token_uri': 'https://oauth2.googleapis.com/token',
        'expiry': '2099-01-01T00:00:00Z', 'scopes': ['https://www.googleapis.com/auth/gmail.modify'],
        'sotto_source_generation': 'a' * 64}))
    token.chmod(0o600)
    injection = DATA / 'pulse-python-boundary'
    injection.mkdir(parents=True, exist_ok=True, mode=0o700)
    fixture = str(Path(__file__).resolve())
    (injection / 'sitecustomize.py').write_text(
        f"import importlib.util; s=importlib.util.spec_from_file_location('pulse_http', {fixture!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); m.configure_http()")
    os.environ['PYTHONPATH'] = str(injection)
    def bridge(request):
        assert request['method'] == 'tools/call'
        # Bounded refresh: with no learned history cursor or snapshot stamp, the pulse re-reads only the
        # local-snapshot TTL (24h, rounded up past the whole-second cursor), never the old 42-day window.
        assert request['params'] == {'name': 'read_local', 'arguments': {'since_hours': 25}}
        with (DATA / 'pulse-bridge-calls.jsonl').open('a') as log:
            log.write(json.dumps(request) + '\n')
        stamp = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        local = {'contacts': [{'name': 'Dana', 'phones': ['+12065550001'], 'emails': ['dana@example.com']}],
            'imessage': [{'handle': '+12065550001', 'text': 'Can you send the Cedar deck?', 'timestamp': stamp,
                          'is_from_me': False, 'chat_guid': 'dana-thread'},
                         {'handle': '+18885559999', 'text': 'Your code', 'timestamp': stamp, 'is_from_me': False}],
            'whatsapp': [], 'calls': [],
            'source_status': {'imessage': 'ok', 'contacts': 'ok', 'whatsapp': 'disabled', 'calls': 'disabled'}}
        return {'jsonrpc': '2.0', 'id': request.get('id'), 'result': {'content': [{'type': 'text', 'text': json.dumps(local)}]}}
    receiver.RELAY.mcp_call = bridge
