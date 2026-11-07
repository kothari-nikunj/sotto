"""Initial owner capture stays ahead of volume mutation and Node polling."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import selfhost_setup as setup
from test_telegram_launcher import fixture


def setup_input():
    manifest, authority, _ = fixture()
    return {
        'version': 1, 'tenantId': manifest['tenantId'],
        'accountId': manifest['owner']['accountId'], 'lineKey': manifest['lineKey'],
        'volumeId': authority['SOTTO_VOLUME_ID'],
        'instructions': manifest['instructions'],
        'botToken': manifest['credentials']['telegramToken'],
        'pairingPhrase': 'synthetic_owner_pairing_phrase_12345',
        'modelDirect': manifest['modelDirect'], 'runtime': manifest['runtime'],
        'relay': manifest['relay'],
        'controlToken': authority['SOTTO_CONTROL_TOKEN'], 'timezone': 'UTC',
    }


def _harness(tmp_path, monkeypatch, *, lost_ack=False):
    events = []
    config = setup_input()
    source = tmp_path / 'setup.json'
    output = tmp_path / 'operator.json'

    def private_input(path):
        if Path(path) == source:
            return config
        return json.loads(Path(path).read_text())

    def provider(url):
        if '/getMe' in url:
            events.append('getMe')
            return 200, json.dumps({'ok': True, 'result': {
                'id': 123456789, 'is_bot': True, 'first_name': 'Sotto',
                'username': 'sotto_brief_bot'}}).encode()
        assert '/getUpdates' in url
        if 'offset=' in url:
            events.append('ack')
            if lost_ack:
                return 0, b'synthetic lost acknowledgement'
            return 200, b'{"ok":true,"result":[]}'
        events.append('capture')
        return 200, json.dumps({'ok': True, 'result': [{
            'update_id': 40,
            'message': {'text': '/start ' + config['pairingPhrase'],
                        'chat': {'id': 8675309, 'type': 'private'},
                        'from': {'id': 8675309, 'is_bot': False,
                                 'first_name': 'Owner'}}}]}).encode()

    monkeypatch.setattr(setup.selfhost_bootstrap, '_preflight',
                        lambda: events.append('topology'))
    monkeypatch.setattr(setup.root_supervisor, '_private_input', private_input)
    monkeypatch.setattr(setup, '_private_parent',
                        lambda _path: os.open(tmp_path, os.O_RDONLY))
    monkeypatch.setattr(setup.root_supervisor, '_open_directory',
                        lambda _path: os.open(tmp_path, os.O_RDONLY))
    monkeypatch.setattr(setup, '_empty_volume', lambda: events.append('empty'))
    monkeypatch.setattr(setup.telegram_link, '_get', provider)
    monkeypatch.setattr(setup, '_publish',
                        lambda path, bundle: (events.append('publish'),
                                              Path(path).write_text(json.dumps(bundle))))
    monkeypatch.setattr(setup.selfhost_bootstrap, 'run',
                        lambda mode, path: events.append(('prepare', mode, Path(path))))
    return source, output, events


def test_capture_confirms_offset_before_operator_publication_and_prepare(tmp_path, monkeypatch):
    source, output, events = _harness(tmp_path, monkeypatch)
    setup.run(source, output)
    assert events == ['topology', 'empty', 'getMe', 'capture', 'ack', 'empty',
                      'publish', ('prepare', '--prepare', output)]
    bundle = json.loads(output.read_text())
    assert bundle['manifest']['owner']['ownerId'] == '8675309'
    assert bundle['manifest']['providerThreadId'] == 'telegram:8675309'
    assert bundle['receiverAuthority']['SOTTO_DATA'] == '/data'
    assert bundle['publicReceiverAuthority'] is None
    assert (tmp_path / 'telegram-capture.json').is_file()


def test_unconfirmed_offset_leaves_operator_and_volume_unmodified(tmp_path, monkeypatch):
    source, output, events = _harness(tmp_path, monkeypatch, lost_ack=True)
    with pytest.raises(setup.SelfHostSetupUnavailableError):
        setup.run(source, output)
    assert events == ['topology', 'empty', 'getMe', 'capture', 'ack']
    assert not output.exists()
    assert not (tmp_path / 'telegram-capture.json').exists()
    second = setup._volume_lock()
    os.close(second)  # failure released the first setup's directory flock


def test_existing_operator_output_refuses_before_polling(tmp_path, monkeypatch):
    source, output, events = _harness(tmp_path, monkeypatch)
    output.write_text('existing output')
    with pytest.raises(setup.SelfHostSetupUnavailableError):
        setup.run(source, output)
    assert events == ['topology']


def test_existing_capture_is_not_reused_without_ack_provenance(tmp_path, monkeypatch):
    source, output, events = _harness(tmp_path, monkeypatch)
    (tmp_path / 'telegram-capture.json').write_text(json.dumps({
        'bot_token': setup_input()['botToken'], 'allowed_user': 8675309}))
    with pytest.raises(setup.SelfHostSetupUnavailableError):
        setup.run(source, output)
    assert events == ['topology', 'empty']
    assert not output.exists()


def test_invalid_setup_refuses_before_polling(tmp_path, monkeypatch):
    source, output, events = _harness(tmp_path, monkeypatch)
    original = setup.root_supervisor._private_input
    monkeypatch.setattr(setup.root_supervisor, '_private_input',
                        lambda path: {**original(path), 'pairingPhrase': ''}
                        if Path(path) == source else original(path))
    with pytest.raises(setup.SelfHostSetupUnavailableError):
        setup.run(source, output)
    assert events == ['topology']


def test_cli_accepts_only_explicit_private_file_arguments(capsys):
    assert setup.main([]) == 2
    assert setup.main(['--setup-file', '/tmp/setup.json']) == 2
    assert 'self-host setup arguments unavailable' in capsys.readouterr().err


def test_competing_setup_refuses_same_volume_before_provider_contact(tmp_path, monkeypatch):
    source, output, events = _harness(tmp_path, monkeypatch)
    first = setup._volume_lock()
    try:
        with pytest.raises(setup.SelfHostSetupUnavailableError):
            setup.run(source, output)
        assert events == ['topology']
        assert not output.exists()
    finally:
        os.close(first)
    second = setup._volume_lock()
    os.close(second)
