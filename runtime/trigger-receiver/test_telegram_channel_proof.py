"""Self-host Telegram route requires the current protected link and Node credential proof."""
import hashlib
import hmac
import importlib.util
import io
import json
from pathlib import Path

import pytest

import telegram_channel_proof as proof


ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


startup = _load('telegram_proof_startup', ROOT / 'adapters/sotto/startup_config.py')
client_module = _load('telegram_proof_client', ROOT / 'adapters/sotto/client.py')
OWNER = '123456789'
TOKEN = '123456789:' + 'fixtureBotSecret_ABCdef0123456789'
BEARER = 'telegram-private-runtime-bearer-123456789'
ZONE = 'America/Los_Angeles'


class Opener:
    def __init__(self, wire):
        self.wire = wire

    def open(self, _request, timeout):
        assert timeout > 0
        return io.BytesIO(json.dumps(self.wire).encode())


def _fixture(tmp_path):
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    link_path = data / 'telegram-link.json'
    link_path.write_text(json.dumps({'bot_token': TOKEN, 'allowed_user': int(OWNER)}))
    link_path.chmod(0o600)
    expected_dir = data / 'runtime'
    expected_dir.mkdir(mode=0o700)
    expected_dir.chmod(0o700)
    config = {
        'version': 1, 'tenantId': 'selfhost-fixture', 'port': 48317,
        'bearerToken': BEARER, 'channel': 'telegram',
        'owner': {'accountId': 'account-1', 'ownerId': OWNER},
        'lineKey': 'line-1', 'target': 'telegram',
        'providerThreadId': f'telegram:{OWNER}',
        'credentials': {'telegramToken': TOKEN,
                        'telegramLinkFile': {'bot_token': TOKEN, 'allowed_user': int(OWNER)}},
    }
    expected_path = expected_dir / 'expected-channel.json'
    expected_path.write_text(json.dumps(startup.receiver_channel_expectation(config)))
    expected_path.chmod(0o600)
    identity, fields = client_module._expected_configured_channel(config)
    wire = {'version': 1, 'identity': identity,
            'credential_proof': hmac.new(BEARER.encode(),
                client_module._identity_transcript(fields), hashlib.sha256).hexdigest()}
    opener = Opener(wire)
    client = client_module.ChatClient('http://127.0.0.1:48317', BEARER, opener=opener)
    policy = {
        'SOTTO_RUNTIME_PROFILE': 'self-host-telegram-fixed-five-v1',
        'SOTTO_DEPLOYMENT_MODE': 'self_host', 'SOTTO_CRON_DELIVER': 'telegram',
        'SOTTO_TENANT_ID': 'selfhost-fixture', 'SOTTO_DATA': str(data),
        'SOTTO_CHAT_RUNTIME_URL': 'http://127.0.0.1:48317',
        'SOTTO_CHAT_RUNTIME_TOKEN': BEARER, 'TELEGRAM_BOT_TOKEN': TOKEN,
        'TELEGRAM_ALLOWED_USERS': OWNER, 'TELEGRAM_HOME_CHANNEL': OWNER,
    }
    return data, link_path, expected_path, client, opener, policy


def _check(data, expected_path, client, policy, zone=lambda: ZONE):
    return proof.check_current_channel(
        chat_client=client, expected_channel_path=expected_path,
        receiver_data_root=data, receiver_policy=policy,
        configured_zone_name=zone, expected_zone=ZONE)


def test_exact_protected_link_and_node_identity_prove_fixed_route(tmp_path):
    data, _, expected_path, client, _, policy = _fixture(tmp_path)
    result = _check(data, expected_path, client, policy)
    assert result['status'] == 'current_channel_proved'
    assert result['action_authorized'] is False
    assert result['provider_thread_id'] == f'telegram:{OWNER}'
    assert result['bot_fingerprint'] == hashlib.sha256(
        ('sotto-telegram-bot-v1:' + TOKEN).encode()).hexdigest()
    assert result['owner_id'] == OWNER
    assert result['route_digest'] == proof._route_digest(
        startup.read_receiver_channel_expectation(expected_path))


def test_changed_link_node_identity_owner_or_zone_withholds_route(tmp_path):
    data, link_path, expected_path, client, opener, policy = _fixture(tmp_path)
    original = link_path.read_text()
    link_path.write_text(json.dumps({'bot_token': TOKEN, 'allowed_user': 999}))
    with pytest.raises(proof.TelegramChannelProofUnavailableError):
        _check(data, expected_path, client, policy)
    link_path.write_text(json.dumps({'bot_token': '123456789:' + 'rotatedSecret_ABCdef0123456789',
                                     'allowed_user': int(OWNER)}))
    with pytest.raises(proof.TelegramChannelProofUnavailableError):
        _check(data, expected_path, client, policy)
    link_path.write_text(original)
    original_proof = opener.wire['credential_proof']
    opener.wire['credential_proof'] = '0' * 64
    with pytest.raises(proof.TelegramChannelProofUnavailableError):
        _check(data, expected_path, client, policy)
    opener.wire['credential_proof'] = original_proof
    with pytest.raises(proof.TelegramChannelProofUnavailableError):
        _check(data, expected_path, client, {**policy, 'TELEGRAM_ALLOWED_USERS': '999'})
    with pytest.raises(proof.TelegramChannelProofUnavailableError):
        _check(data, expected_path, client, policy, zone=lambda: 'UTC')


def test_link_changed_during_node_crosscheck_is_withheld(tmp_path):
    data, link_path, expected_path, client, _, policy = _fixture(tmp_path)
    verify = client.verify_configured_identity
    calls = 0

    def changed(expected):
        nonlocal calls
        result = verify(expected)
        calls += 1
        if calls == 1:
            link_path.write_text(json.dumps({'bot_token': TOKEN, 'allowed_user': 999}))
        return result

    client.verify_configured_identity = changed
    with pytest.raises(proof.TelegramChannelProofUnavailableError):
        _check(data, expected_path, client, policy)
    assert calls == 1
