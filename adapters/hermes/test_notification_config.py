import importlib.util
from pathlib import Path

import pytest
import yaml

spec = importlib.util.spec_from_file_location('notification_config', Path(__file__).with_name('notification_config.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('channel', ['telegram', 'photon', 'whatsapp:fixture-chat', 'local'])
def test_quiet_lifecycle_preserves_channel_access_and_reply_behavior(tmp_path, channel):
    path = tmp_path / 'config.yaml'
    original = {'model': {'default': 'fixture-model'}, 'platforms': {
        'telegram': {'enabled': False, 'token': 'fixture-token', 'typing_indicator': True,
                     'gateway_restart_notification': True},
        'photon': {'enabled': True, 'home_channel': {'chat_id': 'fixture-owner'},
                   'extra': {'gateway_restart_notification': True}},
        'slack': {'enabled': True, 'reply_to_mode': 'first'},
    }}
    path.write_text(yaml.safe_dump(original))
    module.reconcile(tmp_path, channel)
    first = path.read_text()
    result = yaml.safe_load(first)
    for name, settings in original['platforms'].items():
        expected = {**settings, 'gateway_restart_notification': False}
        assert result['platforms'][name] == expected
    assert result['model'] == original['model']
    assert 'local' not in result['platforms']
    if channel.startswith('whatsapp:'):
        assert result['platforms']['whatsapp'] == {'gateway_restart_notification': False}
    module.reconcile(tmp_path, channel)
    assert path.read_text() == first
    assert path.stat().st_mode & 0o777 == 0o600


def test_new_install_keeps_both_chat_channels_quiet_without_enabling_them(tmp_path):
    module.reconcile(tmp_path / 'fresh')
    assert yaml.safe_load((tmp_path / 'fresh/config.yaml').read_text()) == {'platforms': {
        'photon': {'gateway_restart_notification': False},
        'telegram': {'gateway_restart_notification': False},
    }}
