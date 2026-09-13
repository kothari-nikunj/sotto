import importlib.util
from pathlib import Path

import pytest
import yaml

spec = importlib.util.spec_from_file_location('managed_config', Path(__file__).with_name('managed_config.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_managed_start_fails_fast_on_every_runtime_identity():
    start = Path(__file__).with_name('start.sh').read_text()
    for name in ('SOTTO_TENANT_ID', 'BRIDGE_TOKEN', 'SOTTO_CONTROL_TOKEN', 'SOTTO_IMESSAGE_NUMBER',
                 'PHOTON_PROJECT_ID', 'PHOTON_PROJECT_SECRET', 'PHOTON_ALLOWED_USERS',
                 'PHOTON_HOME_CHANNEL', 'SOTTO_MODEL_PROXY_URL', 'SOTTO_MODEL_PROXY_TOKEN',
                 'SOTTO_VOLUME_ID'):
        assert f'${{{name}:?managed mode requires' in start


def test_reconcile_removes_stale_credentials_and_route_overrides(tmp_path):
    owner = '+15555550100'
    env = {'SOTTO_MODEL_PROXY_URL': 'https://proxy.example', 'SOTTO_MODEL_PROXY_TOKEN': 'tenant-token',
           'PHOTON_HOME_CHANNEL': owner, 'PHOTON_ALLOWED_USERS': owner}
    (tmp_path / '.env').write_text('GOOGLE_AI_API_KEY=root\nWHATSAPP_ENABLED=false\nPHOTON_STREAM_SILENCE_PROBE_MS=600000\nBRIDGE_TOKEN=bridge\n')
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({
        'auxiliary': {'vision': {'api_key': 'root', 'base_url': 'https://old.example'}},
        'fallback_providers': [{'provider': 'openrouter'}],
        'plugins': {'enabled': ['whatsapp-platform']}}))
    module.reconcile(tmp_path, env)
    first = (tmp_path / 'config.yaml').read_text()
    cfg = yaml.safe_load(first)
    assert (tmp_path / 'SOUL.md').read_text() == Path(__file__).with_name('sotto-persona.md').read_text()
    assert cfg['agent'] == {'name': 'Sotto', 'reasoning_effort': 'medium', 'max_turns': 60}
    assert cfg['model']['base_url'] == 'https://proxy.example/openai/v1'
    assert cfg['auxiliary']['vision'] == {'provider': 'main', 'model': ''}
    assert cfg['plugins']['enabled'] == ['photon-platform']
    assert 'SOTTO_MODEL_PROXY_TOKEN' in cfg['terminal']['env_passthrough']
    assert not {'BRIDGE_TOKEN', 'SOTTO_SETUP_CODE', 'GOOGLE_AI_API_KEY'} & set(cfg['terminal']['env_passthrough'])
    assert not cfg['fallback_providers']
    assert (tmp_path / '.env').read_text() == 'BRIDGE_TOKEN=bridge\n'
    assert (tmp_path / 'config.yaml').stat().st_mode & 0o777 == 0o600
    module.reconcile(tmp_path, env)
    assert (tmp_path / 'config.yaml').read_text() == first
    env['PHOTON_ALLOWED_USERS'] += ',+15555550101'
    with pytest.raises(ValueError, match='only the tenant owner'):
        module.reconcile(tmp_path, env)


def test_probe_patch_fails_closed_on_an_unreviewed_upstream_file(tmp_path):
    spec = importlib.util.spec_from_file_location('probe_compat', Path(__file__).with_name('photon_probe_compat.py'))
    patch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patch)
    upstream = tmp_path / 'stream-staleness.mjs'
    upstream.write_text('different upstream source')
    with pytest.raises(RuntimeError, match='reviewed pin'):
        patch.apply(upstream)
    assert upstream.read_text() == 'different upstream source'


def test_shared_photon_install_preserves_self_host_config_and_is_idempotent(tmp_path):
    spec = importlib.util.spec_from_file_location('photon_setup', Path(__file__).with_name('photon_setup.py'))
    setup = importlib.util.module_from_spec(spec); spec.loader.exec_module(setup)
    lib = tmp_path / 'skills/sotto/_shared/lib'
    lib.mkdir(parents=True); (lib / 'chatfmt.py').write_text('# fixture shared formatter')
    original = {'model': {'default': 'my-model'}, 'plugins': {'enabled': ['other-plugin'], 'disabled': ['photon-platform']}}
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump(original))
    (tmp_path / '.env').write_text('KEEP=value\nPHOTON_STREAM_SILENCE_PROBE_MS=600000\n')
    setup.install(tmp_path); setup.install(tmp_path)
    cfg = yaml.safe_load((tmp_path / 'config.yaml').read_text())
    assert cfg['model'] == original['model']
    assert cfg['plugins']['enabled'] == ['other-plugin', 'photon-platform']
    assert cfg['plugins']['disabled'] == []
    assert (tmp_path / 'plugins/photon-platform/chatfmt.py').read_text() == '# fixture shared formatter'
    assert (tmp_path / '.env').read_text() == 'KEEP=value\nPHOTON_STREAM_SILENCE_PROBE_MS=0\n'
