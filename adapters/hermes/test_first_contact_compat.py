"""Pinned gateway and dedicated-config first-contact contracts, entirely offline."""
import asyncio
import ast
import hashlib
import importlib.util
import logging
import os
from pathlib import Path
import sys
import types

import pytest
import yaml


HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('first_contact_compat', HERE / 'first_contact_compat.py')
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)
config_spec = importlib.util.spec_from_file_location('quiet_first_contact', HERE / 'quiet_first_contact.py')
quiet = importlib.util.module_from_spec(config_spec)
config_spec.loader.exec_module(quiet)


def _fixture_gateway(tmp_path, monkeypatch, cfg):
    source = (HERE / 'first_contact_pinned_excerpt.py.txt').read_text()
    target = tmp_path / 'run_turn.py'
    target.write_text(source)
    monkeypatch.setattr(compat, 'PINNED_SOURCE_SHA256', hashlib.sha256(source.encode()).hexdigest())
    compat.check(target)
    compat.patch(target)
    patched = target.read_text()
    compat.patch(target)
    assert target.read_text() == patched
    gateway = types.ModuleType('gateway')
    gateway_run = types.ModuleType('gateway.run')
    gateway_run._hermes_home = tmp_path
    gateway_run._home_target_env_var = lambda platform: None
    gateway_run._load_gateway_config = lambda: cfg
    agent = types.ModuleType('agent')
    onboarding = types.ModuleType('agent.onboarding')
    onboarding.PROFILE_BUILD_FLAG = 'profile_build_offered'
    onboarding.is_seen = lambda config, flag: False
    onboarding.mark_seen = lambda path, flag: True
    onboarding.profile_build_mode = lambda config: config.get('onboarding', {}).get('profile_build', 'ask')
    onboarding.profile_build_directive = lambda: 'Offer a quick profile after an intro mentioning /help.'
    for name, module in [('gateway', gateway), ('gateway.run', gateway_run),
                         ('agent', agent), ('agent.onboarding', onboarding)]:
        monkeypatch.setitem(sys.modules, name, module)
    scope = {}
    exec(compile(patched, str(target), 'exec'), scope)
    return scope['Gateway']


def test_only_explicit_dedicated_sotto_quiets_intro_and_keeps_home_hint(tmp_path, monkeypatch):
    cfg = {'agent': {'name': 'Sotto'}, 'onboarding': {'profile_build': 'off',
                                                    'sotto_quiet_first_contact': True}}
    gateway_class = _fixture_gateway(tmp_path, monkeypatch, cfg)
    async def no_sessions():
        return False
    source = types.SimpleNamespace(platform='photon')
    instance = gateway_class(cfg, no_sessions)
    notes = []
    asyncio.run(instance._hmwa_first_contact_notes(source, [], notes))
    assert notes == []
    assert instance.notices == ['Set your home channel']
    for non_dedicated in ({'agent': {'name': 'Hermes'}, 'onboarding': cfg['onboarding']},
                          {'agent': {'name': 'Sotto'}, 'onboarding': {'profile_build': 'off'}},
                          {'agent': {'name': 'Sotto'}, 'onboarding': {'profile_build': 'off',
                                                                     'sotto_quiet_first_contact': 'true'}}):
        monkeypatch.setattr(sys.modules['gateway.run'], '_load_gateway_config', lambda: non_dedicated)
        instance = gateway_class(non_dedicated, no_sessions)
        notes = []
        asyncio.run(instance._hmwa_first_contact_notes(source, [], notes))
        assert len(notes) == 1 and '/help' in notes[0]


def test_shared_hermes_keeps_profile_offer_and_dedicated_writer_is_idempotent(tmp_path, monkeypatch):
    general = {'agent': {'name': 'Hermes'}, 'onboarding': {'profile_build': 'ask'}}
    gateway_class = _fixture_gateway(tmp_path, monkeypatch, general)
    async def no_sessions():
        return False
    instance = gateway_class(general, no_sessions)
    notes = []
    asyncio.run(instance._hmwa_first_contact_notes(types.SimpleNamespace(platform='local'), [], notes))
    assert notes == ['Offer a quick profile after an intro mentioning /help.']
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(general))
    quiet.select(path)
    first = path.read_bytes()
    quiet.select(path)
    assert path.read_bytes() == first
    assert yaml.safe_load(first)['onboarding'] == {'profile_build': 'off',
                                                   'sotto_quiet_first_contact': True}
    assert yaml.safe_load(first)['agent']['name'] == 'Sotto'
    assert path.stat().st_mode & 0o777 == 0o600


def test_unreviewed_gateway_fails_without_writing(tmp_path):
    path = tmp_path / 'run_turn.py'
    path.write_text('unreviewed gateway')
    with pytest.raises(RuntimeError, match='reviewed pin'):
        compat.patch(path)
    assert path.read_text() == 'unreviewed gateway'


def test_real_pinned_gateway_loader_preserves_explicit_quiet_flag_when_available(tmp_path):
    root = Path(os.environ.get('HERMES_PINNED_CHECKOUT', '/usr/local/lib/hermes-agent'))
    source = root / 'gateway/run.py'
    if not source.is_file():
        pytest.skip('full pinned Hermes checkout is not installed here')
    provider_spec = importlib.util.spec_from_file_location('provider_error_compat', HERE / 'provider_error_compat.py')
    provider = importlib.util.module_from_spec(provider_spec)
    provider_spec.loader.exec_module(provider)
    text = source.read_text()
    assert hashlib.sha256(text.encode()).hexdigest() == provider.PINNED_SOURCE_SHA256
    function = next(node for node in ast.parse(text).body
                    if isinstance(node, ast.FunctionDef) and node.name == '_load_gateway_config')
    # Compile the actual pinned loader without importing its unrelated gateway services.
    scope = {'logger': logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), scope)
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({'agent': {'name': 'Sotto'},
                                    'onboarding': {'profile_build': 'off',
                                                   'sotto_quiet_first_contact': True}}))
    loaded = scope['_load_gateway_config'](path)
    assert loaded['agent']['name'] == 'Sotto'
    assert loaded['onboarding']['sotto_quiet_first_contact'] is True


def test_dedicated_writer_preserves_custom_persona_name_and_comments(tmp_path):
    path = tmp_path / 'config.yaml'
    custom = ('# owner persona\nagent:\n  name: Jarvis  # my assistant\n'
              'onboarding:\n  profile_build: ask\n')
    path.write_text(custom)
    quiet.select(path)
    written = yaml.safe_load(path.read_text())
    assert written['agent']['name'] == 'Jarvis'
    assert written['onboarding'] == {'profile_build': 'off', 'sotto_quiet_first_contact': True}
    # Once selected, later boots leave the owner's file (and any comments they add) untouched.
    commented = '# kept\n' + path.read_text()
    path.write_text(commented)
    quiet.select(path)
    assert path.read_text() == commented
    for generic in ({}, {'agent': None}, {'agent': {'name': 'Hermes'}}, {'agent': {'name': ''}}):
        path.write_text(yaml.safe_dump(generic))
        quiet.select(path)
        assert yaml.safe_load(path.read_text())['agent']['name'] == 'Sotto'


@pytest.mark.parametrize('text', ['agent: [unclosed\n', '- a list\n', 'agent: plain\n'])
def test_malformed_config_is_logged_once_and_left_without_failing_boot(tmp_path, capsys, text):
    path = tmp_path / 'config.yaml'
    path.write_text(text)
    quiet.select(path)
    assert path.read_text() == text
    assert len(capsys.readouterr().err.strip().splitlines()) == 1
