"""Exact owner quiet commands use the protected channel and one durable preference receipt."""
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import receiver
import runtime_owner_quiet as quiet
import telegram_channel_proof as telegram
from test_telegram_channel_proof import _fixture, OWNER, TOKEN, ZONE
from test_telegram_owner_commands import IDENTITY as TELEGRAM_IDENTITY, ROOT as TELEGRAM_ROOT

HERE = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(HERE.parents[1] / 'sotto-chief-of-staff' / '_shared' / 'lib'))
import owner_loop_capability as capability  # noqa: E402

WHEN = datetime(2026, 10, 5, 17, 35, tzinfo=timezone.utc)
THREAD = f'telegram:{OWNER}'


@pytest.fixture
def context(tmp_path, monkeypatch):
    data, link, expected, client, opener, policy = _fixture(tmp_path)
    monkeypatch.setenv('SOTTO_DATA', str(data))
    policy.update(GOOGLE_AI_API_KEY='test-key',
                  _configured_tz_name=lambda: ZONE,
                  _tz_resolve=lambda name: ZoneInfo(name))
    return data, link, expected, client, opener, policy


def request(operation, event, text, *, zone=None):
    return {'version': 1, 'operation': operation, 'identity': TELEGRAM_IDENTITY,
            'action_id': hashlib.sha256(json.dumps(['sotto.owner-quiet-command.v1', event],
                                                   separators=(',', ':')).encode()).hexdigest(),
            'event_id': event, 'payload_hash': 'b' * 64,
            'text_hash': hashlib.sha256(text.encode()).hexdigest(), 'text': text,
            'admitted_at_ms': int(WHEN.timestamp() * 1000), 'zone': zone,
            'provider_thread_id': THREAD}


def call(body, context):
    data, _link, _expected, client, _opener, policy = context
    return quiet.perform(body, bearer=capability.derive(TELEGRAM_ROOT, body['identity']),
                         root=TELEGRAM_ROOT, policy=policy, data_root=data,
                         capability=capability, activation=None,
                         telegram_verifier=telegram, chat_client=client)


def test_telegram_quiet_prepare_commit_inspect_and_off(context):
    data = context[0]
    event = 'a' * 64
    text = '/quiet for 2h'
    assert call(request('prepare', event, text), context) == {
        'version': 1, 'status': 'prepared', 'zone': ZONE}
    assert call(request('inspect', event, text, zone=ZONE), context) == {
        'version': 1, 'status': 'not_found'}
    committed = call(request('write', event, text, zone=ZONE), context)
    assert committed == {'version': 1, 'status': 'committed',
                         'action_id': request('write', event, text, zone=ZONE)['action_id'],
                         'event_id': event, 'payload_hash': 'b' * 64,
                         'zone': ZONE, 'snooze_until': '2026-10-05T12:35:00.000-07:00'}
    assert call(request('inspect', event, text, zone=ZONE), context) == committed
    saved = json.loads((data / 'preferences.json').read_text())
    assert saved['explicit']['nudge_snooze_until'] == committed['snooze_until']
    assert saved['owner_quiet_receipts'][committed['action_id']]['event_id'] == event
    off = call(request('write', 'c' * 64, '/quiet off', zone=ZONE), context)
    assert off['status'] == 'committed' and off['snooze_until'] == ''
    assert call(request('write', event, text, zone=ZONE), context) == committed
    assert json.loads((data / 'preferences.json').read_text())['explicit']['nudge_snooze_until'] == ''


def test_telegram_quiet_refuses_changed_pair_profile_zone_and_conflict(context):
    data, link, _expected, _client, opener, policy = context
    text = '/quiet for 90m'
    body = request('write', 'd' * 64, text, zone=ZONE)
    for invalid in ('/quiet for 0h', '/quiet for 25h', '/quiet for 1441m',
                    '/quiet for 1.5h', '/quiet for 01h', '/quiet until 3'):
        with pytest.raises(ValueError):
            call(request('write', 'e' * 64, invalid, zone=ZONE), context)
    changed = dict(body, zone='UTC')
    with pytest.raises(quiet.UnavailableError):
        call(changed, context)
    original = link.read_text()
    link.write_text(json.dumps({'bot_token': TOKEN, 'allowed_user': 999}))
    with pytest.raises(quiet.UnavailableError):
        call(body, context)
    link.write_text(original)
    old_proof = opener.wire['credential_proof']
    opener.wire['credential_proof'] = '0' * 64
    with pytest.raises(quiet.UnavailableError):
        call(body, context)
    opener.wire['credential_proof'] = old_proof
    policy['SOTTO_RUNTIME_PROFILE'] = 'self-host-photon-fixed-five-v1'
    with pytest.raises(quiet.UnavailableError):
        call(body, context)
    policy['SOTTO_RUNTIME_PROFILE'] = 'self-host-telegram-fixed-five-v1'
    committed = call(body, context)
    changed = request('write', 'd' * 64, '/quiet off', zone=ZONE)
    assert call(changed, context) == {'version': 1, 'status': 'rejected', 'code': 'event_conflict'}
    assert json.loads((data / 'preferences.json').read_text())['explicit']['nudge_snooze_until'] == committed['snooze_until']


def test_private_http_quiet_route_requires_exact_profile(context, monkeypatch):
    data, _link, _expected, client, _opener, policy = context
    for name, value in policy.items():
        if isinstance(value, str):
            monkeypatch.setenv(name, value)
    monkeypatch.setenv('SOTTO_MCP_TOKEN', TELEGRAM_ROOT)
    monkeypatch.setenv('SOTTO_TIMEZONE', ZONE)
    monkeypatch.setattr(receiver, 'DATA', str(data))
    monkeypatch.setattr(receiver, '_runtime_adapter',
                        lambda: SimpleNamespace(_client=lambda timeout: client))

    def post(path, body):
        raw = json.dumps(body).encode()
        sent = []
        fake = SimpleNamespace(path=path, client_address=('127.0.0.1', 1234),
                               headers={'Content-Length': str(len(raw)), 'Content-Type': 'application/json'},
                               rfile=io.BytesIO(raw),
                               _authed=lambda token: token == capability.derive(TELEGRAM_ROOT, TELEGRAM_IDENTITY),
                               _send=lambda status, payload: sent.append((status, payload)))
        receiver.Handler.do_POST(fake)
        return sent[0]

    body = request('prepare', 'f' * 64, '/quiet off')
    assert post('/runtime/owner-quiet-intent', body) == (
        200, {'version': 1, 'status': 'prepared', 'zone': ZONE})
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'self-host-photon-fixed-five-v1')
    assert post('/runtime/owner-quiet-intent', body)[0] == 404


@pytest.mark.parametrize('deployment,profile', [
    ('managed', 'managed-photon-fixed-five-v1'),
    ('self_host', 'self-host-photon-fixed-five-v1'),
])
def test_photon_quiet_keeps_current_activation_gate(tmp_path, monkeypatch, deployment, profile):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    photon_thread = 'imessage:iMessage;-;owner~+15555550123'
    identity = {'tenant_id': 'tenant', 'channel': 'photon', 'account_id': 'account',
                'owner_id': '+15555550123', 'line_key': 'primary', 'target': 'photon',
                'route_mode': 'fixed', 'route_id': photon_thread,
                'project_fingerprint': hashlib.sha256(
                    b'sotto-photon-project-v1:synthetic-project').hexdigest()}
    policy = {'SOTTO_DEPLOYMENT_MODE': deployment,
              'SOTTO_RUNTIME_PROFILE': profile, 'SOTTO_CRON_DELIVER': 'photon',
              'SOTTO_TENANT_ID': 'tenant', 'SOTTO_DATA': str(tmp_path),
              'PHOTON_HOME_CHANNEL': identity['owner_id'],
              'PHOTON_ALLOWED_USERS': identity['owner_id'],
              'PHOTON_PROJECT_ID': 'synthetic-project',
              'SOTTO_IMESSAGE_NUMBER': '+15555550123',
              '_configured_tz_name': lambda: 'UTC', '_tz_resolve': lambda name: ZoneInfo(name)}
    if deployment == 'self_host':
        policy['GOOGLE_AI_API_KEY'] = 'test-key'
    activation = SimpleNamespace(verified_current_photon_route=lambda *_: {'chat_id': 'iMessage;-;owner'})
    body = {**request('write', '9' * 64, '/quiet for 30m', zone='UTC'),
            'identity': identity, 'provider_thread_id': photon_thread}
    root = 'photon-quiet-root-' + 'x' * 40

    def invoke():
        return quiet.perform(body, bearer=capability.derive(root, identity), root=root,
                             policy=policy, data_root=tmp_path, capability=capability,
                             activation=activation)

    assert invoke()['snooze_until'] == '2026-10-05T18:05:00.000+00:00'
    activation.verified_current_photon_route = lambda *_: {'chat_id': 'iMessage;-;other'}
    with pytest.raises(quiet.UnavailableError):
        invoke()
    if deployment == 'self_host':
        activation.verified_current_photon_route = lambda *_: {'chat_id': 'iMessage;-;owner'}
        policy['SOTTO_MODEL_PROXY_URL'] = 'https://forbidden.invalid'
        with pytest.raises(quiet.UnavailableError):
            invoke()


@pytest.mark.parametrize('admitted,expected', [
    ('2026-03-08T09:30:12.345+00:00', '2026-03-08T03:30:12.345-07:00'),
    ('2026-11-01T08:30:12.345+00:00', '2026-11-01T01:30:12.345-08:00'),
])
def test_duration_crosses_dst_as_exact_elapsed_time(context, admitted, expected):
    body = request('write', '7' * 64, '/quiet for 1h', zone=ZONE)
    start = datetime.fromisoformat(admitted)
    body['admitted_at_ms'] = int(start.timestamp() * 1000)
    committed = call(body, context)
    assert committed['snooze_until'] == expected
    writer = quiet._preferences()
    ex = {'nudge_snooze_until': expected}
    end = start + timedelta(hours=1)
    for clock in (timezone.utc, ZoneInfo(ZONE)):
        assert writer.snooze_active((end - timedelta(milliseconds=1)).astimezone(clock), ex)
        assert not writer.snooze_active(end.astimezone(clock), ex)
        assert not writer.snooze_active((end + timedelta(milliseconds=1)).astimezone(clock), ex)
    call(request('write', '8' * 64, '/quiet off', zone=ZONE), context)
    assert call(body, context) == committed
    assert call(dict(body, operation='inspect'), context) == committed
    assert json.loads((context[0] / 'preferences.json').read_text())['explicit']['nudge_snooze_until'] == ''
