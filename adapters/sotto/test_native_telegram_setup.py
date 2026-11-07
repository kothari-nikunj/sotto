"""Captured native Telegram proof crosses the real compiler and root launch gates."""

import copy
import json
import os
from pathlib import Path

import pytest

from adapters.sotto import prepare_fresh, selfhost_bootstrap as bootstrap
from adapters.sotto import selfhost_configuration as compiler


@pytest.fixture
def native():
    return json.loads((Path(__file__).parent / 'fixtures/native-telegram-setup.json').read_text())


def compile_fixture(value):
    return compiler.compile_native(value['request'], value['attachment'], value['generatedSecrets'],
                                   value['telegramCapture'])


def railway_env(envelope):
    provider = envelope['provider']
    return {
        'SOTTO_SELFHOST_LAUNCH': json.dumps(envelope),
        'RAILWAY_PROJECT_ID': provider['projectId'],
        'RAILWAY_ENVIRONMENT_ID': provider['environmentId'],
        'RAILWAY_SERVICE_ID': provider['serviceId'],
        'RAILWAY_PUBLIC_DOMAIN': provider['domain'],
        'SOTTO_SETUP_REQUEST': provider['provisionRequest'],
        'RAILWAY_VOLUME_MOUNT_PATH': '/data', 'RAILWAY_VOLUME_NAME': 'fixture', 'PORT': '8080',
    }


def test_native_telegram_golden_projection_and_real_prepare_admission(native, monkeypatch):
    before = copy.deepcopy(native)
    envelope = compile_fixture(native)
    assert envelope == native['envelope']
    assert native == before
    assert compiler.parse_native_request(json.dumps(native['request']).encode()) == native['request']
    assert bootstrap._railway_envelope(railway_env(envelope)) == envelope
    staged = []
    monkeypatch.setattr(bootstrap.root_supervisor, '_mount_record',
                        lambda _root: ('ext4', '/dev/synthetic', 8, 4, 42))
    monkeypatch.setattr(bootstrap, '_stage', lambda bundle: staged.append(bundle) or bundle)
    bundle = bootstrap._railway_stage(envelope)
    assert len(staged) == 1
    assert 'telegramCapture' not in bundle
    assert native['telegramCapture']['phrase'] not in json.dumps(bundle)
    monkeypatch.setattr(prepare_fresh.root_supervisor, '_private_input', lambda _path: bundle)
    # Native host has no root-owned /app image pack; retain the real environment
    # and identity checks while replacing only its installed-filesystem check.
    monkeypatch.setattr(prepare_fresh.fixed_five, '_pack', lambda _env: None)
    mount, manifest, authority = prepare_fresh._bundle('/synthetic/private.json')
    assert mount['fsType'] == 'ext4'
    assert manifest['providerThreadId'] == 'telegram:8675309'
    assert authority['SOTTO_VOLUME_ID'] == native['attachment']['volumeId']
    rendered, _ = bootstrap.root_supervisor.startup_config.render(manifest)
    assert rendered['credentials']['telegramLinkFile'] == {
        'bot_token': native['request']['telegram']['botToken'], 'allowed_user': 8675309}


@pytest.mark.parametrize('change', [
    'missing', 'unknown', 'digest', 'request', 'phrase', 'bot_id', 'bot_human', 'bot_username',
    'webhook', 'group', 'sender_bot', 'sender_bool', 'sender_zero', 'sender_too_large',
    'mismatched_private_chat', 'phrase_substring', 'phrase_case', 'edited_message',
    'offset_bool', 'wrong_offset', 'unacknowledged', 'ack_object', 'ack_non_update',
    'invalid_tail_offset', 'second_claimant', 'later_owner_copy', 'batch_mismatch',
    'duplicate_update', 'unsorted_ack', 'oversize',
])
def test_native_capture_refuses_noncanonical_or_unacknowledged_owner(native, change):
    proof = native['telegramCapture']
    message = proof['update']['message']
    # JSON fixture copies the matching update in two fields; keep them synchronized
    # except in the test deliberately claiming an item absent from the poll response.
    proof['updates'][0] = proof['update']
    if change == 'missing':
        native['telegramCapture'] = None
    elif change == 'unknown':
        proof['ownerId'] = '8675309'
    elif change == 'digest':
        proof['requestDigest'] = '0' * 64
    elif change == 'request':
        proof['provisionRequest'] = 'r' * 32
    elif change == 'phrase':
        proof['phrase'] = 'short'
    elif change == 'bot_id':
        proof['bot']['id'] += 1
    elif change == 'bot_human':
        proof['bot']['is_bot'] = False
    elif change == 'bot_username':
        proof['bot']['username'] = 'bad/path'
    elif change == 'webhook':
        proof['webhook']['url'] = 'https://another.example/webhook'
    elif change == 'group':
        message['chat']['type'] = 'group'
    elif change == 'sender_bot':
        message['from']['is_bot'] = True
    elif change == 'sender_bool':
        message['from']['id'] = message['chat']['id'] = True
    elif change == 'sender_zero':
        message['from']['id'] = message['chat']['id'] = 0
    elif change == 'sender_too_large':
        message['from']['id'] = message['chat']['id'] = 9007199254740992
    elif change == 'mismatched_private_chat':
        message['chat']['id'] += 1
    elif change == 'phrase_substring':
        message['text'] += ' extra words'
    elif change == 'phrase_case':
        message['text'] = message['text'].upper()
    elif change == 'edited_message':
        proof['update']['edited_message'] = proof['update'].pop('message')
    elif change == 'offset_bool':
        proof['acknowledgedOffset'] = True
    elif change == 'wrong_offset':
        proof['acknowledgedOffset'] += 1
    elif change == 'unacknowledged':
        proof['acknowledgement'] = [proof['update']]
    elif change == 'ack_object':
        proof['acknowledgement'] = {}
    elif change == 'ack_non_update':
        proof['acknowledgement'] = [None]
    elif change == 'invalid_tail_offset':
        proof['updates'].append({'update_id': 20})
        proof['acknowledgedOffset'] = 19
    elif change == 'second_claimant':
        other = copy.deepcopy(proof['update'])
        other['update_id'] = 18
        other['message']['from']['id'] = other['message']['chat']['id'] = 424242
        proof['updates'].append(other)
        proof['acknowledgedOffset'] = 19
    elif change == 'later_owner_copy':
        proof['updates'].insert(0, copy.deepcopy(proof['update']))
        proof['updates'][0]['update_id'] = 16
    elif change == 'batch_mismatch':
        proof['updates'] = [{'update_id': 16}]
    elif change == 'duplicate_update':
        proof['updates'].append(proof['update'])
    elif change == 'unsorted_ack':
        proof['acknowledgement'] = [{'update_id': 20}, {'update_id': 19}]
    else:
        proof['bot']['unused'] = 'a' * 65536
    with pytest.raises(compiler.NativeSetupConfigurationError, match='^native setup configuration is invalid$'):
        compile_fixture(native)


def test_unrelated_updates_around_the_owner_start_cannot_block_pairing(native):
    proof = native['telegramCapture']
    stranger = {'chat': {'id': 424242, 'type': 'private'}, 'from': {'id': 424242, 'is_bot': False}}
    proof['updates'] = [
        {'update_id': 15, 'message': {**stranger, 'text': 'hello'}},
        {'update_id': 16, 'message': {**stranger, 'text': '/start ' + 'w' * 43}},
        proof['update'],
        {'update_id': 18, 'my_chat_member': {}},
    ]
    proof['acknowledgedOffset'] = 19
    assert compile_fixture(native)['bundle']['manifest']['owner']['ownerId'] == '8675309'


@pytest.mark.parametrize('offset', [18, 19])
def test_version_one_owner_tail_capture_compatibility(native, offset):
    proof = native['telegramCapture']
    tail = copy.deepcopy(proof['update'])
    tail['update_id'] = 18
    tail['message']['text'] = 'hello'
    proof['updates'].append(tail)
    proof['acknowledgedOffset'] = offset
    proof['acknowledgement'] = [tail] if offset == 18 else []
    assert compile_fixture(native)['bundle']['manifest']['owner']['ownerId'] == '8675309'


def test_acknowledgement_can_leave_new_unconsumed_updates(native):
    native['telegramCapture']['acknowledgement'] = [{'update_id': 18}, {'update_id': 19}]
    assert compile_fixture(native)['bundle']['manifest']['owner']['ownerId'] == '8675309'


@pytest.mark.parametrize('field', ['ownerId', 'phone', 'allowed_user'])
def test_native_telegram_never_accepts_typed_owner_surrogate(native, field):
    native['request']['owner'][field] = '8675309'
    with pytest.raises(compiler.NativeSetupConfigurationError):
        compile_fixture(native)


@pytest.mark.parametrize('change', ['missing_capture', 'owner', 'link', 'thread', 'bot', 'owner_shape', 'mount'])
def test_railway_cannot_bypass_capture_or_ext4_checks(native, change, monkeypatch):
    envelope = copy.deepcopy(native['envelope'])
    manifest = envelope['bundle']['manifest']
    if change == 'missing_capture':
        del envelope['telegramCapture']
    elif change == 'owner':
        manifest['owner']['ownerId'] = '999'
    elif change == 'link':
        manifest['telegramLink']['allowed_user'] = 999
    elif change == 'thread':
        manifest['providerThreadId'] = 'telegram:999'
    elif change == 'bot':
        manifest['credentials']['telegramToken'] = '123456789:another-synthetic-token-0123456789'
    elif change == 'owner_shape':
        manifest['owner'] = []
    else:
        monkeypatch.setattr(bootstrap.root_supervisor, '_mount_record',
                            lambda _root: ('overlay', '/dev/synthetic', 8, 4, 42))
    with pytest.raises(ValueError):
        checked = bootstrap._railway_envelope(railway_env(envelope))
        bootstrap._railway_stage(checked)


def test_setup_capture_state_on_volume_still_fails_freshness(tmp_path):
    (tmp_path / 'native-setup.json').write_text('{}')
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(prepare_fresh.FreshPreparationUnavailableError):
            prepare_fresh._fresh_entries(descriptor, claimed=False)
    finally:
        os.close(descriptor)
