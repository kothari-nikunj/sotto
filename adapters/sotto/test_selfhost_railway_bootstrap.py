"""Offline Railway envelope checks for self-host Photon bootstrap."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import selfhost_bootstrap as boot
from test_selfhost_photon_core import photon_bundle


IDS = {
    'projectId': '11111111-1111-4111-8111-111111111111',
    'environmentId': '22222222-2222-4222-8222-222222222222',
    'serviceId': '33333333-3333-4333-8333-333333333333',
    'volumeId': '44444444-4444-4444-8444-444444444444',
}


def railway_input():
    bundle = photon_bundle()
    del bundle['mount']
    bundle['receiverAuthority']['SOTTO_VOLUME_ID'] = IDS['volumeId']
    bundle['publicReceiverAuthority'] = {
        'tenantId': bundle['manifest']['tenantId'], 'serviceId': IDS['serviceId'],
        'domain': 'sotto.up.railway.app', 'targetPort': 8080,
    }
    bundle['trustedServiceId'] = IDS['serviceId']
    bundle['manifest']['relay']['receiverPort'] = 8080
    provider = {**IDS, 'domain': 'sotto.up.railway.app', 'provisionRequest': 'A' * 32,
                'volumeCreationReceipt': {'service_id': IDS['serviceId'],
                                          'volume_id': IDS['volumeId']}}
    env = {
        'SOTTO_SELFHOST_LAUNCH': json.dumps({'version': 1, 'provider': provider,
                                             'bundle': bundle}),
        'RAILWAY_PROJECT_ID': IDS['projectId'],
        'RAILWAY_ENVIRONMENT_ID': IDS['environmentId'],
        'RAILWAY_SERVICE_ID': IDS['serviceId'],
        'RAILWAY_PUBLIC_DOMAIN': provider['domain'],
        'RAILWAY_VOLUME_NAME': 'sotto-volume',
        'RAILWAY_VOLUME_MOUNT_PATH': '/data',
        'SOTTO_SETUP_REQUEST': provider['provisionRequest'],
        'PORT': '8080',
    }
    return env


def test_exact_railway_receipt_and_bundle_crossbindings():
    env = railway_input()
    assert boot._railway_envelope(env)['provider']['volumeId'] == IDS['volumeId']
    for key in ('RAILWAY_PROJECT_ID', 'RAILWAY_ENVIRONMENT_ID', 'RAILWAY_SERVICE_ID',
                'RAILWAY_PUBLIC_DOMAIN', 'RAILWAY_VOLUME_MOUNT_PATH',
                'SOTTO_SETUP_REQUEST', 'PORT'):
        with pytest.raises(ValueError):
            boot._railway_envelope({**env, key: 'wrong'})
    with pytest.raises(ValueError):
        boot._railway_envelope({**env, 'RAILWAY_VOLUME_NAME': ''})
    envelope = json.loads(env['SOTTO_SELFHOST_LAUNCH'])
    for mutation in (
        lambda value: value['provider']['volumeCreationReceipt'].update(volume_id='other'),
        lambda value: value['bundle']['receiverAuthority'].update(SOTTO_VOLUME_ID='other'),
        lambda value: value['bundle']['publicReceiverAuthority'].update(serviceId='other'),
        lambda value: value['bundle']['publicReceiverAuthority'].update(domain='other.example'),
        lambda value: value['bundle'].update(mount={}),
        lambda value: value['provider'].update(unexpected=True),
        lambda value: value['bundle']['manifest'].update(deployment='managed'),
        lambda value: value['bundle']['manifest'].update(channel='telegram'),
    ):
        changed = copy.deepcopy(envelope)
        mutation(changed)
        with pytest.raises(ValueError):
            boot._railway_envelope({**env, 'SOTTO_SELFHOST_LAUNCH': json.dumps(changed)})
    with pytest.raises(ValueError):
        boot._railway_envelope({**env, 'SOTTO_SELFHOST_LAUNCH': '{"version":1,"version":1}'})


def test_railway_staging_checks_kernel_mount_and_full_handoff_before_file_write():
    envelope = boot._railway_envelope(railway_input())
    with (patch.object(boot.root_supervisor, '_mount_record',
                       return_value=('ext4', '/dev/synthetic', 8, 4, 42)),
          patch.object(boot, '_stage', return_value=Path('/tmp/operator/runtime.json')) as stage):
        boot._railway_stage(envelope)
        staged = stage.call_args.args[0]
        assert staged['mount'] == {'mountPoint': '/data', 'source': '/dev/synthetic',
                                   'fsType': 'ext4', 'major': 8, 'minor': 4}
    with (patch.object(boot.root_supervisor, '_mount_record',
                       return_value=('overlay', '/dev/synthetic', 8, 4, 42)),
          patch.object(boot, '_stage') as stage):
        with pytest.raises(ValueError):
            boot._railway_stage(envelope)
        stage.assert_not_called()


def test_prepare_holds_health_without_node_and_run_never_prepares():
    env = railway_input()
    path = Path('/tmp/operator/runtime.json')
    seen = []
    with (patch.dict(os.environ, {'UNRELATED_SECRET': 'ambient'}, clear=True),
          patch.object(boot, '_preflight'),
          patch.object(boot, '_railway_stage', return_value=path),
          patch.object(boot.prepare_fresh, 'prepare',
                       side_effect=lambda item: seen.append(('prepare', item))),
          patch.object(boot, '_health_only', side_effect=lambda: seen.append(('health', None))),
          patch.object(boot.root_supervisor, 'run_operator_file',
                       side_effect=lambda item: seen.append(('run', item))),
          patch.object(Path, 'unlink'), patch.object(Path, 'rmdir')):
        boot.run_railway('--prepare', env)
        assert seen == [('prepare', path), ('health', None)]
        assert 'SOTTO_SELFHOST_LAUNCH' not in os.environ
        assert 'UNRELATED_SECRET' not in os.environ
        seen.clear()
        boot.run_railway('--run', env)
        assert seen == [('run', path)]
