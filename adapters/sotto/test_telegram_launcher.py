"""Offline contract for the closed self-host Telegram launch projection."""
from __future__ import annotations

import hashlib
import os
import stat
from types import SimpleNamespace

import pytest

import process_handoff
import root_supervisor
import startup_config


def fixture():
    owner = '8675309'
    token = '123456789:synthetic-telegram-bot-token-0123456789'
    manifest = {
        'version': 1, 'deployment': 'self_host', 'channel': 'telegram',
        'tenantId': 'local-tenant-a',
        'owner': {'accountId': 'local-account-a', 'ownerId': owner},
        'lineKey': 'telegram-owner', 'target': 'telegram',
        'providerThreadId': f'telegram:{owner}',
        'telegramLink': {'bot_token': token, 'allowed_user': int(owner)},
        'stateDirectory': '/data/node-state', 'mediaRoot': '/data/node-media',
        'instructions': 'Synthetic owner instructions.',
        'runtime': {'port': 19981, 'bearerToken': 'runtime-token-01234567890123456789'},
        'relay': {'receiverPort': 19982, 'rootBearer': 'relay-root-012345678901234567890'},
        'modelDirect': {'provider': 'google',
                        'apiKey': 'synthetic-google-api-key-01234567890123',
                        'chatModelId': 'gemini-2.5-flash',
                        'nativeMediaModelId': 'gemini-2.5-flash'},
        'credentials': {'telegramToken': token},
    }
    authority = {
        'SOTTO_MCP_TOKEN': manifest['relay']['rootBearer'],
        'SOTTO_CONTROL_TOKEN': 'control-token-012345678901234567890',
        'SOTTO_TENANT_ID': manifest['tenantId'],
        'SOTTO_DATA': '/data', 'SOTTO_HOME': '/data/sotto-home',
        'SOTTO_VOLUME_ID': 'local-volume-a',
    }
    identity = {
        'channel': 'telegram', 'target': 'telegram',
        'tenantId': manifest['tenantId'], 'accountId': 'local-account-a',
        'ownerId': owner, 'lineKey': 'telegram-owner',
        'providerThreadId': f'telegram:{owner}',
        'botFingerprint': hashlib.sha256(
            ('sotto-telegram-bot-v1:' + token).encode()).hexdigest(),
    }
    return manifest, authority, identity


def build(manifest=None, authority=None, identity=None):
    original_manifest, original_authority, original_identity = fixture()
    return process_handoff.build_child_handoff(
        manifest or original_manifest, authority or original_authority,
        stable_identity=identity or original_identity,
        node_config_path='/data/node-config/config/runtime-config.json',
        data_directory='/data', skills_root='/app/sotto-skills',
        limited_profile='self-host-telegram-fixed-five-v1')


def test_self_host_telegram_projection_binds_pairing_owner_and_direct_model():
    manifest, authority, identity = fixture()
    config, projection = startup_config.render(manifest)
    handoff = build(manifest, authority, identity)
    expected = startup_config.receiver_channel_expectation(config)
    assert dict(handoff.receiver_channel_expectation) == expected
    assert expected['botFingerprint'] == identity['botFingerprint']
    assert dict(handoff.receiver_env).items() >= {
        'SOTTO_RUNTIME_PROFILE': 'self-host-telegram-fixed-five-v1',
        'SOTTO_DEPLOYMENT_MODE': 'self_host',
        'SOTTO_CRON_DELIVER': 'telegram',
        'TELEGRAM_BOT_TOKEN': manifest['credentials']['telegramToken'],
        'TELEGRAM_ALLOWED_USERS': '8675309',
        'TELEGRAM_HOME_CHANNEL': '8675309',
        'GOOGLE_AI_API_KEY': manifest['modelDirect']['apiKey'],
    }.items()
    assert not any(key.startswith('SOTTO_MODEL_PROXY') for key in handoff.receiver_env)
    assert 'GEMINI_API_KEY' not in handoff.receiver_env
    assert 'GOOGLE_AI_API_KEY' not in handoff.node_env
    assert not any(key.startswith('PHOTON_') for key in handoff.receiver_env)
    assert handoff.node_env['HOME'] == '/home/sotto-runtime'
    assert projection['SOTTO_CHAT_RUNTIME_URL'] == handoff.receiver_env['SOTTO_CHAT_RUNTIME_URL']


@pytest.mark.parametrize('change', ['bot', 'owner', 'authority', 'profile'])
def test_self_host_telegram_refuses_identity_and_authority_drift(change):
    manifest, authority, identity = fixture()
    if change == 'bot':
        identity['botFingerprint'] = '0' * 64
    elif change == 'owner':
        manifest['telegramLink']['allowed_user'] = 12345
    elif change == 'authority':
        authority['SOTTO_MODEL_PROXY_URL'] = 'https://proxy.example'
    else:
        manifest['deployment'] = 'managed'
    with pytest.raises(process_handoff.HandoffError):
        build(manifest, authority, identity)


def test_root_bundle_allows_local_receiver_without_public_attachment_only_for_telegram():
    manifest, authority, identity = fixture()
    bundle = {
        'version': 1, 'tenantRoot': '/data', 'mount': {},
        'manifest': manifest, 'receiverAuthority': authority,
        'stableIdentity': identity, 'publicReceiverAuthority': None,
        'trustedServiceId': '', 'receiverOverrides': {},
    }
    assert root_supervisor._validated_bundle(bundle)[5] is None
    manifest['deployment'] = 'managed'
    with pytest.raises(root_supervisor.SupervisorUnavailableError):
        root_supervisor._validated_bundle(bundle)


def test_protected_pairing_stage_refuses_tampered_or_changed_link(tmp_path, monkeypatch):
    manifest, _, _ = fixture()
    link = manifest['telegramLink']
    real_fstat = os.fstat

    def synthetic_sticky_parent(fd):
        value = real_fstat(fd)
        if stat.S_ISDIR(value.st_mode):
            return SimpleNamespace(st_uid=0, st_gid=os.getegid(),
                                   st_mode=stat.S_IFDIR | 0o1771)
        return value

    monkeypatch.setattr(startup_config.os, 'fstat', synthetic_sticky_parent)
    startup_config.stage_telegram_link(tmp_path, link)
    path = tmp_path / 'telegram-link.json'
    original = path.read_bytes()
    startup_config.stage_telegram_link(tmp_path, link)  # identical restart
    assert path.read_bytes() == original
    with pytest.raises(startup_config.ManifestError):
        startup_config.stage_telegram_link(tmp_path, {**link, 'allowed_user': 12345})
    path.write_bytes(original.replace(b'123456789:', b'987654321:'))
    with pytest.raises(startup_config.ManifestError):
        startup_config.stage_telegram_link(tmp_path, link)
