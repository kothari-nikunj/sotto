"""Telegram owner commands require the current protected pair and loaded Node identity."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import runtime_owner_loop as loop
import runtime_owner_reminder as reminder
import receiver
import telegram_channel_proof as telegram
from test_telegram_channel_proof import _fixture, OWNER, TOKEN, ZONE

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'sotto-chief-of-staff' / '_shared' / 'lib'))
import owner_loop_capability as capability  # noqa: E402

ROOT = 'telegram-owner-command-root-' + 'x' * 40
WHEN = datetime(2026, 10, 5, 17, 35, tzinfo=timezone.utc)
THREAD = f'telegram:{OWNER}'
IDENTITY = {'tenant_id': 'selfhost-fixture', 'channel': 'telegram',
            'account_id': 'account-1', 'owner_id': OWNER, 'line_key': 'line-1',
            'target': 'telegram', 'route_mode': 'fixed', 'route_id': THREAD,
            'bot_fingerprint': hashlib.sha256(
                ('sotto-telegram-bot-v1:' + TOKEN).encode()).hexdigest()}


@pytest.fixture
def context(tmp_path, monkeypatch):
    data, link, expected, client, opener, policy = _fixture(tmp_path)
    monkeypatch.setenv('SOTTO_DATA', str(data))
    policy.update(GOOGLE_AI_API_KEY='test-key',
                  _configured_tz_name=lambda: ZONE,
                  _tz_resolve=lambda name: ZoneInfo(name))
    return data, link, expected, client, opener, policy


def _loop_request(operation, event, text, *, zone=None):
    return {'version': 1, 'operation': operation, 'identity': IDENTITY,
            'action_id': hashlib.sha256(json.dumps(['sotto.owner-loop-command.v1', event],
                                                   separators=(',', ':')).encode()).hexdigest(),
            'event_id': event, 'payload_hash': 'b' * 64,
            'text_hash': hashlib.sha256(text.encode()).hexdigest(), 'text': text,
            'admitted_at_ms': int(WHEN.timestamp() * 1000), 'zone': zone,
            'provider_thread_id': THREAD}


def _reminder_request(operation, event, text):
    return {'version': 1, 'operation': operation, 'identity': IDENTITY,
            'action_id': hashlib.sha256(json.dumps(['sotto.owner-reminder-command.v1', event],
                                                   separators=(',', ':')).encode()).hexdigest(),
            'event_id': event, 'payload_hash': 'c' * 64,
            'text_hash': hashlib.sha256(text.encode()).hexdigest(), 'text': text,
            'admitted_at_ms': int(WHEN.timestamp() * 1000),
            'provider_thread_id': THREAD}


def _loop_close_request(operation, event, anchor, *, zone=None, digest=None):
    text = '/loop close ' + anchor
    return {**_loop_request(operation, event, text, zone=zone),
            'action_id': hashlib.sha256(json.dumps(['sotto.owner-loop-close-command.v1', event],
                                                   separators=(',', ':')).encode()).hexdigest(),
            'anchor_key': anchor, 'expected_row_digest': digest}


def _call(module, body, context):
    data, _link, _expected, client, _opener, policy = context
    return module.perform(body, bearer=capability.derive(ROOT, body['identity']),
                          root=ROOT, policy=policy, data_root=data,
                          capability=capability, activation=None,
                          telegram_verifier=telegram, chat_client=client)


def test_telegram_owner_task_and_reminder_use_existing_canonical_writers(context):
    text = '/loop add Send the signed deck'
    event = 'a' * 64
    assert _call(loop, _loop_request('prepare', event, text), context)['status'] == 'prepared'
    committed = _call(loop, _loop_request('write', event, text, zone=ZONE), context)
    assert committed['status'] == 'committed' and committed['anchor_key']
    assert _call(loop, _loop_request('inspect', event, text, zone=ZONE), context) == committed
    close_event = 'c' * 64
    anchor = committed['anchor_key']
    prepared = _call(loop, _loop_close_request('close_prepare', close_event, anchor), context)
    closed = _call(loop, _loop_close_request('close_write', close_event, anchor,
                                            zone=ZONE, digest=prepared['expected_row_digest']), context)
    assert closed['status'] == 'committed'
    assert _call(loop, _loop_close_request('close_inspect', close_event, anchor,
                                          zone=ZONE, digest=prepared['expected_row_digest']), context) == closed
    command = '/reminder at 2026-10-07T08:15:00Z Review the cedar packet'
    body = _reminder_request('create', 'd' * 64, command)
    reminder_receipt = _call(reminder, body, context)
    assert reminder_receipt['status'] == 'committed'
    assert _call(reminder, body, context) == reminder_receipt
    assert _call(reminder, _reminder_request('inspect', 'd' * 64, command), context) == reminder_receipt
    listed = _call(reminder, _reminder_request('list', 'e' * 64, '/reminder list'), context)
    assert [item['id'] for item in listed['items']] == [reminder_receipt['reminder_id']]


def test_telegram_link_node_identity_and_profile_changes_refuse_before_effect(context):
    data, link, _expected, _client, opener, policy = context
    text = '/reminder at 2026-10-07T08:15:00Z Review the cedar packet'
    body = _reminder_request('create', 'f' * 64, text)
    original = link.read_text()
    link.write_text(json.dumps({'bot_token': TOKEN, 'allowed_user': 999}))
    with pytest.raises(reminder.UnavailableError):
        _call(reminder, body, context)
    link.write_text(original)
    proof = opener.wire['credential_proof']
    opener.wire['credential_proof'] = '0' * 64
    with pytest.raises(reminder.UnavailableError):
        _call(reminder, body, context)
    opener.wire['credential_proof'] = proof
    policy['SOTTO_RUNTIME_PROFILE'] = 'self-host-photon-fixed-five-v1'
    with pytest.raises(reminder.UnavailableError):
        _call(reminder, body, context)
    policy['SOTTO_RUNTIME_PROFILE'] = 'self-host-telegram-fixed-five-v1'
    policy['TELEGRAM_ALLOWED_USERS'] = '999'
    with pytest.raises(reminder.UnavailableError):
        _call(reminder, body, context)
    assert reminder._writer().current() == []


def test_telegram_owner_loop_rejects_wrong_route_and_bot_fingerprint(context):
    body = _loop_request('write', 'a' * 64, '/loop add Send the deck', zone=ZONE)
    changed = dict(body, provider_thread_id='telegram:999')
    with pytest.raises(loop.UnavailableError):
        _call(loop, changed, context)
    identity = dict(IDENTITY, bot_fingerprint='0' * 64)
    changed = dict(body, identity=identity)
    with pytest.raises(loop.UnavailableError):
        _call(loop, changed, context)


def test_telegram_private_http_routes_admit_only_exact_profile(context, monkeypatch):
    data, _link, _expected, client, _opener, policy = context
    for name, value in policy.items():
        if isinstance(value, str):
            monkeypatch.setenv(name, value)
    monkeypatch.setenv('SOTTO_MCP_TOKEN', ROOT)
    monkeypatch.setenv('SOTTO_TIMEZONE', ZONE)
    monkeypatch.setattr(receiver, 'DATA', str(data))
    monkeypatch.setattr(receiver, '_runtime_adapter',
                        lambda: SimpleNamespace(_client=lambda timeout: client))

    def post(path, body):
        raw = json.dumps(body).encode()
        sent = []
        fake = SimpleNamespace(
            path=path, client_address=('127.0.0.1', 1234),
            headers={'Content-Length': str(len(raw)), 'Content-Type': 'application/json'},
            rfile=io.BytesIO(raw),
            _authed=lambda token: token == capability.derive(ROOT, IDENTITY),
            _send=lambda status, payload: sent.append((status, payload)))
        receiver.Handler.do_POST(fake)
        return sent[0]

    task = _loop_request('prepare', 'a' * 64, '/loop add Send the deck')
    assert post('/runtime/owner-loop-intent', task)[0] == 200
    listed = _reminder_request('list', 'b' * 64, '/reminder list')
    assert post('/runtime/owner-reminder-intent', listed) == (
        200, {'version': 1, 'status': 'listed', 'items': []})
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host-photon-fixed-five-v1')
    assert post('/runtime/owner-loop-intent', task)[0] == 404
    assert post('/runtime/owner-reminder-intent', listed)[0] == 404
