"""Offline self-host entrypoint input and mount boundary tests."""
from __future__ import annotations

from unittest.mock import patch

import pytest

import selfhost_bootstrap as bootstrap
from test_telegram_launcher import fixture


def operator_input():
    manifest, authority, identity = fixture()
    return {
        'version': 1, 'tenantRoot': '/data', 'manifest': manifest,
        'receiverAuthority': authority, 'stableIdentity': identity,
        'publicReceiverAuthority': None, 'trustedServiceId': '',
        'receiverOverrides': {'SOTTO_TIMEZONE': 'UTC'},
    }


def test_private_local_input_derives_live_ext4_mount_and_closed_handoff():
    supplied = operator_input()
    with (patch.object(bootstrap.root_supervisor, '_private_input', return_value=supplied),
          patch.object(bootstrap.root_supervisor, '_mount_record',
                       return_value=('ext4', '/dev/synthetic', 8, 4, 42))):
        result = bootstrap._operator_bundle('/run/sotto/selfhost-operator.json')
    assert result['mount'] == {'mountPoint': '/data', 'source': '/dev/synthetic',
                               'fsType': 'ext4', 'major': 8, 'minor': 4}
    assert 'mount' not in supplied
    assert result['publicReceiverAuthority'] is None


@pytest.mark.parametrize('change', ['managed', 'public', 'wrong_bot', 'proxy', 'mount'])
def test_private_local_input_refuses_wrong_profile_or_mount(change):
    supplied = operator_input()
    mount = ('ext4', '/dev/synthetic', 8, 4, 42)
    if change == 'managed':
        supplied['manifest']['deployment'] = 'managed'
    elif change == 'public':
        supplied['publicReceiverAuthority'] = {'tenantId': 'local-tenant-a'}
    elif change == 'wrong_bot':
        supplied['stableIdentity']['botFingerprint'] = '0' * 64
    elif change == 'proxy':
        supplied['receiverAuthority']['SOTTO_MODEL_PROXY_URL'] = 'https://proxy.example'
    else:
        mount = ('overlay', '/dev/synthetic', 8, 4, 42)
    with (patch.object(bootstrap.root_supervisor, '_private_input', return_value=supplied),
          patch.object(bootstrap.root_supervisor, '_mount_record', return_value=mount)):
        with pytest.raises(ValueError):
            bootstrap._operator_bundle('/run/sotto/selfhost-operator.json')


def test_entrypoint_argument_shape_is_closed(capsys):
    assert bootstrap.main([]) == 2
    assert bootstrap.main(['--run', '/run/sotto/selfhost-operator.json']) == 2
    assert 'self-host bootstrap arguments unavailable' in capsys.readouterr().err
