"""Offline binding and process-mode checks for the managed entrypoint."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import managed_bootstrap as boot


IDS = {
    'projectId': '11111111-1111-4111-8111-111111111111',
    'environmentId': '22222222-2222-4222-8222-222222222222',
    'serviceId': '33333333-3333-4333-8333-333333333333',
    'volumeId': '44444444-4444-4444-8444-444444444444',
}


def _input():
    provider = {**IDS, 'domain': 'tenant.up.railway.app',
                'provisionRequest': 'A' * 32,
                'volumeCreationReceipt': {'service_id': IDS['serviceId'],
                                          'volume_id': IDS['volumeId']}}
    bundle = {'version': 1, 'tenantRoot': '/data', 'manifest': {'tenantId': 'tenant'},
              'receiverAuthority': {'SOTTO_VOLUME_ID': IDS['volumeId']},
              'stableIdentity': {},
              'publicReceiverAuthority': {'tenantId': 'tenant', 'serviceId': IDS['serviceId'],
                                          'domain': provider['domain'], 'targetPort': 8080},
              'trustedServiceId': IDS['serviceId'], 'receiverOverrides': {}}
    env = {'SOTTO_MANAGED_LAUNCH': json.dumps({'version': 1, 'provider': provider,
                                               'bundle': bundle}),
           'RAILWAY_PROJECT_ID': IDS['projectId'],
           'RAILWAY_ENVIRONMENT_ID': IDS['environmentId'],
           'RAILWAY_SERVICE_ID': IDS['serviceId'],
           'RAILWAY_PUBLIC_DOMAIN': provider['domain'],
           'RAILWAY_VOLUME_NAME': 'tenant-volume',
           'RAILWAY_VOLUME_MOUNT_PATH': '/data',
           'SOTTO_PROVISION_REQUEST': provider['provisionRequest'], 'PORT': '8080'}
    return env


def test_exact_provider_binding_and_closed_schema():
    env = _input()
    assert boot._envelope(env)['provider']['volumeId'] == IDS['volumeId']
    for key in ('RAILWAY_PROJECT_ID', 'RAILWAY_ENVIRONMENT_ID', 'RAILWAY_SERVICE_ID',
                'RAILWAY_PUBLIC_DOMAIN',
                'RAILWAY_VOLUME_MOUNT_PATH', 'SOTTO_PROVISION_REQUEST', 'PORT'):
        changed = dict(env)
        changed[key] = 'wrong'
        with pytest.raises(ValueError):
            boot._envelope(changed)
    with pytest.raises(ValueError):
        boot._envelope({**env, 'RAILWAY_VOLUME_NAME': ''})
    changed = copy.deepcopy(json.loads(env['SOTTO_MANAGED_LAUNCH']))
    for mutation in (
        lambda value: value['provider']['volumeCreationReceipt'].update(volume_id='other'),
        lambda value: value['bundle']['receiverAuthority'].update(SOTTO_VOLUME_ID='other'),
        lambda value: value['bundle']['publicReceiverAuthority'].update(serviceId='other'),
        lambda value: value['bundle'].update(mount={}),
        lambda value: value['provider'].update(unexpected=True),
    ):
        value = copy.deepcopy(changed)
        mutation(value)
        with pytest.raises(ValueError):
            boot._envelope({**env, 'SOTTO_MANAGED_LAUNCH': json.dumps(value)})
    with pytest.raises(ValueError):
        boot._envelope({**env, 'SOTTO_MANAGED_LAUNCH': '{"version":1,"version":1}'})


def test_no_staging_before_topology_and_envelope_validation(monkeypatch):
    with (patch.object(boot, '_preflight', side_effect=boot.BootstrapUnavailableError),
          patch.object(boot, '_stage') as stage):
        with pytest.raises(ValueError):
            boot.stage_operator_file(_input())
        stage.assert_not_called()
    with (patch.object(boot, '_preflight'), patch.object(boot, '_stage') as stage):
        with pytest.raises(ValueError):
            boot.stage_operator_file({})
        stage.assert_not_called()


def _staged(tmp_path):
    path = tmp_path / 'sotto-operator-fixture' / 'runtime.json'
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text('{"receiverAuthority":{"SOTTO_MCP_TOKEN":"secret"}}')
    path.chmod(0o600)
    return path


def test_modes_clean_ambient_secrets_and_do_not_cross_launch_paths(tmp_path):
    env = _input()
    env['UNRELATED_SECRET'] = 'must-not-pass'
    path = tmp_path / 'sotto-operator-fixture' / 'runtime.json'
    seen = []
    with (patch.dict(os.environ, {'UNRELATED_SECRET': 'must-not-pass'}, clear=True),
          patch.object(boot, 'stage_operator_file', side_effect=lambda _env: _staged(tmp_path)),
          patch.object(boot.prepare_fresh, 'prepare', side_effect=lambda item: seen.append(('prepare', item))),
          patch.object(boot, '_health_only',
                       side_effect=lambda: seen.append(('health', path.parent.exists()))),
          patch.object(boot.root_supervisor, 'run_operator_file',
                       side_effect=lambda item: seen.append(('run', item)))):
        boot.run('--prepare', env)
        assert 'UNRELATED_SECRET' not in os.environ
        assert 'SOTTO_MANAGED_LAUNCH' not in os.environ
        # The secret-bearing staged file is gone before the health-only wait begins.
        assert seen == [('prepare', path), ('health', False)]
        seen.clear()
        boot.run('--run', env)
        assert seen == [('run', path)]
        assert not path.parent.exists()


def test_failed_launch_removes_the_staged_secret_bundle(tmp_path):
    path = _staged(tmp_path)

    def unavailable(_path):
        raise boot.root_supervisor.SupervisorUnavailableError('injected')
    with (patch.object(boot, 'stage_operator_file', return_value=path),
          patch.object(boot, '_clean_environment'),
          patch.object(boot.root_supervisor, 'run_operator_file', side_effect=unavailable)):
        with pytest.raises(boot.root_supervisor.SupervisorUnavailableError):
            boot.run('--run', {})
    assert not path.exists() and not path.parent.exists()


def test_invalid_cli_has_no_traceback_or_secret(tmp_path):
    result = subprocess.run([sys.executable, '-I', '-B', str(Path(boot.__file__).resolve()),
                             '--unknown'], cwd=tmp_path, text=True, capture_output=True,
                            timeout=10, check=False)
    assert result.returncode == 2
    assert result.stdout == ''
    assert result.stderr.strip() == '[sotto] managed bootstrap arguments unavailable'
