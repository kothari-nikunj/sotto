import importlib.util
import io
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location('model_lease', Path(__file__).with_name('model_lease.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def environment():
    return {'SOTTO_DEPLOYMENT_MODE': 'managed', 'SOTTO_TENANT_ID': 'fixture',
            'SOTTO_MODEL_PROXY_URL': 'https://models.example', 'SOTTO_MODEL_PROXY_TOKEN': 'model-fixture',
            'SOTTO_CONTROL_TOKEN': 'control-fixture'}


def test_renewal_is_early_durable_and_never_stores_credentials(tmp_path):
    requests = []
    def open_request(request, **kwargs):
        requests.append(request)
        assert request.get_header('Authorization') == 'Bearer control-fixture'
        return io.BytesIO(json.dumps({'tenant_id': 'fixture', 'expires_at': 1000 + 30 * 86400}).encode())
    state = m.tick(tmp_path, environment(), now=1000, opener=open_request)
    assert state['last_success_at'] == 1000
    assert m.tick(tmp_path, environment(), now=2000, opener=open_request) == state
    assert len(requests) == 1
    raw = (tmp_path / 'config/model-lease.json').read_text()
    assert 'model-fixture' not in raw and 'control-fixture' not in raw
    assert (tmp_path / 'config/model-lease.json').stat().st_mode & 0o777 == 0o600


def test_failed_renewal_keeps_unexpired_lease_and_backs_off(tmp_path):
    env = environment()
    m.tick(tmp_path, env, now=1000, opener=lambda *a, **k:
           io.BytesIO(json.dumps({'tenant_id': 'fixture', 'expires_at': 1000 + 86400}).encode()))
    def fail(*args, **kwargs):
        raise OSError('model-fixture control-fixture')
    state = m.tick(tmp_path, env, now=2000, opener=fail)
    assert state['expires_at'] == 87400 and state['error'] == 'OSError'
    assert state['retry_at'] == 5600 and state['last_success_at'] == 1000
    assert m.tick(tmp_path, env, now=3000, opener=lambda *a, **k: 1) == state


def test_changed_tenant_or_bearer_never_reuses_old_receipt(tmp_path):
    env = environment()
    m.tick(tmp_path, env, now=1000, opener=lambda *a, **k:
           io.BytesIO(json.dumps({'tenant_id': 'fixture', 'expires_at': 1000 + 30 * 86400}).encode()))
    env['SOTTO_MODEL_PROXY_TOKEN'] = 'new-model-fixture'
    state = m.tick(tmp_path, env, now=2000, opener=lambda *a, **k:
           io.BytesIO(b'{"tenant_id":"other","expires_at":3000}'))
    assert 'expires_at' not in state and state['error'] == 'ValueError'
