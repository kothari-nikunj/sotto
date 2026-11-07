"""Offline admission tests for self-host Sotto with Photon Cloud and direct Google."""
from __future__ import annotations

import copy
from unittest.mock import patch

import pytest

import fixed_five
import prepare_fresh
import process_handoff
import root_supervisor
import selfhost_bootstrap
import startup_config
from test_prepare_fresh import _bundle as managed_bundle


def photon_bundle():
    bundle = managed_bundle()
    manifest = bundle['manifest']
    manifest['deployment'] = 'self_host'
    manifest['modelDirect'] = {
        'provider': 'google', 'apiKey': 'direct-google-fixture-key-0123456789',
        'chatModelId': 'gemini-2.5-flash', 'nativeMediaModelId': 'gemini-2.5-flash',
    }
    del manifest['modelProxy']
    authority = bundle['receiverAuthority']
    del authority['SOTTO_MODEL_PROXY_URL']
    del authority['SOTTO_MODEL_PROXY_TOKEN']
    bundle['publicReceiverAuthority'] = None
    bundle['trustedServiceId'] = ''
    return bundle


def handoff(bundle, *, profile=fixed_five.SELF_HOST_PHOTON_PROFILE):
    return process_handoff.build_child_handoff(
        bundle['manifest'], bundle['receiverAuthority'],
        stable_identity=bundle['stableIdentity'],
        node_config_path='/data/node-config/config/runtime-config.json',
        data_directory='/data', skills_root='/app/sotto-skills',
        receiver_overrides=bundle['receiverOverrides'], limited_profile=profile)


def test_direct_photon_cloud_handoff_retains_activation_identity_and_private_model_key():
    bundle = photon_bundle()
    config, _ = startup_config.render(bundle['manifest'])
    result = handoff(bundle)
    env = result.receiver_env
    assert config['firstContact'] == {'mode': 'owner_dm', 'expectedProviderLine': '+15557654321'}
    owner_command = config['ownerLoop']
    assert owner_command['mode'] == 'explicit_command'
    assert owner_command['identity']['route_mode'] == 'first_contact'
    assert owner_command['identity']['route_id'] == '+15557654321'
    assert owner_command['identity']['project_fingerprint'] == bundle['stableIdentity']['projectFingerprint']
    assert owner_command['bearer'] == startup_config._owner_loop_capability().derive(
        bundle['manifest']['relay']['rootBearer'], owner_command['identity'])
    assert owner_command['bearer'] not in set(result.node_env.values()) | set(env.values())
    assert owner_command['bearer'] not in {
        config['bearerToken'], config['bridge']['readBearer'], config['modelDirect']['apiKey'],
        config['credentials']['photonProjectSecret'],
    }
    assert dict(result.receiver_channel_expectation) == startup_config.receiver_channel_expectation(config)
    assert env['SOTTO_RUNTIME_PROFILE'] == fixed_five.SELF_HOST_PHOTON_PROFILE
    assert env['SOTTO_DEPLOYMENT_MODE'] == 'self_host'
    assert env['SOTTO_CRON_DELIVER'] == 'photon'
    assert env['SOTTO_IMESSAGE_NUMBER'] == '+15557654321'
    assert env['PHOTON_HOME_CHANNEL'] == env['PHOTON_ALLOWED_USERS'] == '+15551234567'
    assert env['PHOTON_PROJECT_ID'] == 'synthetic-project'
    assert env['PHOTON_PROJECT_SECRET'] == 'project-secret-fixture'
    assert env['GOOGLE_AI_API_KEY'] == bundle['manifest']['modelDirect']['apiKey']
    assert 'GOOGLE_AI_API_KEY' not in result.node_env
    assert not any(key.startswith('SOTTO_MODEL_PROXY') for key in env)
    assert 'TELEGRAM_BOT_TOKEN' not in env


@pytest.mark.parametrize('change', ['profile', 'line', 'project', 'owner', 'proxy', 'model',
                                    'server'])
def test_direct_photon_rejects_profile_identity_or_authority_drift(change):
    bundle = photon_bundle()
    if change == 'profile':
        with pytest.raises(process_handoff.HandoffError):
            handoff(bundle, profile=fixed_five.PROFILE)
        return
    if change == 'line':
        bundle['stableIdentity']['expectedProviderLine'] = '+15550000000'
    elif change == 'project':
        bundle['stableIdentity']['projectFingerprint'] = '0' * 64
    elif change == 'owner':
        bundle['stableIdentity']['ownerId'] = '+15550000000'
    elif change == 'proxy':
        bundle['receiverAuthority']['SOTTO_MODEL_PROXY_TOKEN'] = 'unexpected-proxy-secret-0123456789'
    elif change == 'model':
        bundle['manifest']['modelProxy'] = {'origin': 'https://proxy.example',
                                            'bearer': 'proxy-secret-fixture-0123456789',
                                            'chatModelId': 'chat-v1', 'nativeMediaModelId': 'media-v1'}
    else:
        bundle['manifest']['credentials'] = {'mode': 'local', 'serverUrl': 'http://localhost:3000',
                                             'apiKey': 'local-key'}
    with pytest.raises(process_handoff.HandoffError):
        handoff(bundle)


@pytest.mark.parametrize('source', ['relay_root', 'control', 'runtime', 'google'])
def test_photon_project_secret_cannot_reuse_another_capability(source):
    bundle = photon_bundle()
    manifest = bundle['manifest']
    other = {
        'relay_root': manifest['relay']['rootBearer'],
        'control': bundle['receiverAuthority']['SOTTO_CONTROL_TOKEN'],
        'runtime': manifest['runtime']['bearerToken'],
        'google': manifest['modelDirect']['apiKey'],
    }
    manifest['credentials']['photonProjectSecret'] = other[source]
    with pytest.raises(process_handoff.HandoffError):
        handoff(bundle)


def test_fresh_and_local_bootstrap_admit_only_closed_first_contact_bundle():
    bundle = photon_bundle()
    with (patch.object(prepare_fresh.root_supervisor, '_private_input', return_value=bundle),
          patch.object(prepare_fresh.fixed_five, 'validate_environment')):
        assert prepare_fresh._bundle('/unused/input')[0] == bundle['mount']
    operator = {key: copy.deepcopy(value) for key, value in bundle.items() if key != 'mount'}
    with (patch.object(root_supervisor, '_private_input', return_value=operator),
          patch.object(root_supervisor, '_mount_record',
                       return_value=('ext4', '/dev/fake-ext4', 1, 2, 42))):
        assert selfhost_bootstrap._operator_bundle('/unused/input')['mount'] == bundle['mount']
    fixed = copy.deepcopy(operator)
    fixed['manifest']['providerThreadId'] = 'imessage:iMessage;-;+15551234567~+15557654321'
    fixed['manifest'].pop('firstContact')
    with (patch.object(root_supervisor, '_private_input', return_value=fixed),
          patch.object(root_supervisor, '_mount_record',
                       return_value=('ext4', '/dev/fake-ext4', 1, 2, 42))):
        with pytest.raises(ValueError):
            selfhost_bootstrap._operator_bundle('/unused/input')


def test_fixed_five_environment_profile_matrix_rejects_crossed_model_authority(monkeypatch):
    bundle = photon_bundle()
    env = dict(handoff(bundle).receiver_env)
    monkeypatch.setattr(fixed_five, '_pack', lambda _env: None)
    assert fixed_five.validate_environment(env).as_posix() == '/data'
    for mutation in (
            {'SOTTO_RUNTIME_PROFILE': fixed_five.PROFILE},
            {'SOTTO_CRON_DELIVER': 'telegram'},
            {'SOTTO_MODEL_PROXY_TOKEN': 'proxy-secret-fixture-0123456789'},
            {'SOTTO_IMESSAGE_NUMBER': '+15550000000abc'},
            {'GOOGLE_AI_API_KEY': ''},
            {'PHOTON_ALLOWED_USERS': '+15550000000'},
    ):
        with pytest.raises(fixed_five.ProfileUnavailableError):
            fixed_five.validate_environment({**env, **mutation})
