"""Thin protocol adapter: shared search/fetch results, no separate vendor routing."""
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest
import yaml


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def provider(monkeypatch):
    api = ModuleType('agent.web_search_provider')
    api.WebSearchProvider = object
    monkeypatch.setitem(sys.modules, 'agent.web_search_provider', api)
    module = load('web_provider')
    shared = SimpleNamespace(provider_chain=lambda _: ['gemini'])
    monkeypatch.setattr(module, 'research_module', lambda: shared)
    return module, shared


def test_search_uses_shared_answer_and_does_not_misattribute_summary(provider):
    module, shared = provider
    calls = []
    def research(query):
        calls.append(query)
        return {'text': 'Grounded synthesis.', 'provider': 'gemini', 'citations': [
            {'title': 'Source one', 'uri': 'https://example.com/1'},
            {'title': 'Source two', 'uri': 'https://example.com/2'}]}
    shared.research = research
    backend = module.SottoWebProvider()
    registered = []
    module.register(SimpleNamespace(register_web_search_provider=registered.append))
    assert registered[0].name == 'sotto' and backend.is_available() and backend.supports_extract()
    result = backend.search('public synthetic query', 1)
    assert calls == ['public synthetic query'] and result['success']
    assert result['data']['answer'] == 'Grounded synthesis.'
    assert result['data']['web'] == [{'title': 'Source one', 'url': 'https://example.com/1',
                                     'description': 'Source one', 'position': 1}]


def test_failure_is_honest_and_extract_keeps_per_url_results(provider):
    module, shared = provider
    shared.research = lambda _: {'text': '', 'error': 'upstream failure with private diagnostic'}
    result = module.SottoWebProvider().search('query')
    assert not result['success'] and 'private diagnostic' not in result['error']
    shared.fetch_url = lambda url: {'text': 'Page text' if url.endswith('/ok') else '', 'provider': 'gemini'}
    result = module.SottoWebProvider().extract(['https://example.com/ok', 'https://example.com/fail'])
    assert result[0]['content'] == 'Page text' and 'error' not in result[0]
    assert result[1]['error'] and not result[1]['content']


def test_install_reconciles_both_backends_without_touching_other_settings(tmp_path):
    config = load('web_config')
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({'web': {'search_backend': 'firecrawl', 'extract_char_limit': 1234},
                                   'model': 'fixture-model'}))
    config.reconcile(tmp_path)
    first = path.read_text()
    value = yaml.safe_load(first)
    assert value == {'web': {'search_backend': 'sotto', 'extract_backend': 'sotto', 'extract_char_limit': 1234,
                             'keyless_rescue': False, 'keyless_fallback': False},
                     'model': 'fixture-model', 'plugins': {'enabled': ['sotto-web'], 'disabled': []}}
    assert (tmp_path / 'plugins/sotto-web/__init__.py').read_text() == Path(__file__).with_name('web_provider.py').read_text()
    assert yaml.safe_load((tmp_path / 'plugins/sotto-web/plugin.yaml').read_text())['provides_web_providers'] == ['sotto']
    config.reconcile(tmp_path)
    assert path.read_text() == first and path.stat().st_mode & 0o777 == 0o600
