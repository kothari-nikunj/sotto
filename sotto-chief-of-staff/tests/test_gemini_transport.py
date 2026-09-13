import json
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_shared/lib'))
import gemini_transport as transport
import gemini


@pytest.fixture
def managed_proxy(monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'tenant-test-token')
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'must-not-escape')


def test_native_payload_preserved_across_tiers(monkeypatch, managed_proxy):
    body = {'contents': [{'parts': [{'text': 'test'}, {'inline_data': {'mime_type': 'image/png', 'data': 'AA=='}}]}],
            'systemInstruction': {'parts': [{'text': 'system'}]}, 'tools': [{'google_search': {}}],
            'generationConfig': {'responseSchema': {'type': 'object'}, 'temperature': 0.4}}
    cloud = transport.request('gemini-3.8-flash', body, 'direct-key-ignored')
    assert cloud.full_url == 'https://proxy.example/native/v1beta/models/gemini-3.8-flash:generateContent'
    assert cloud.get_header('Authorization') == 'Bearer tenant-test-token'
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE')
    direct = transport.request('gemini-3.8-flash', body, 'direct-key')
    assert cloud.data == direct.data and json.loads(cloud.data) == body
    assert direct.get_header('Authorization') is None


def test_missing_managed_credentials_cannot_fall_back(monkeypatch, managed_proxy):
    monkeypatch.delenv('SOTTO_MODEL_PROXY_TOKEN')
    with pytest.raises(RuntimeError):
        transport.request('gemini-3.8-flash', {}, 'direct-key')


def test_managed_pipeline_rejects_provider_switch(managed_proxy):
    with pytest.raises(RuntimeError, match='requires provider gemini'):
        gemini.provider_key('openai')


@pytest.mark.parametrize('base', ['http://public.example', 'https://user:secret@proxy.example', 'https://proxy.example?key=x'])
def test_proxy_credentials_cannot_leak_via_unsafe_url(monkeypatch, managed_proxy, base):
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', base)
    with pytest.raises(ValueError):
        transport.request('gemini-3.8-flash', {})


def test_self_host_background_is_proxy_scoped_and_foreground_stays_direct(monkeypatch):
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    monkeypatch.delenv('SOTTO_BACKGROUND_UNMETERED', raising=False)
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'tenant-token')
    monkeypatch.setattr(transport, '_background_budget_preflight', lambda: None)
    with transport.background_learning():
        background = transport.request('gemini-3.8-flash', {}, 'direct-must-not-escape')
        assert background.full_url.startswith('https://proxy.example/native/')
        assert background.get_header('Authorization') == 'Bearer tenant-token'
        assert background.get_header('X-sotto-require-finite-budget') == 'true'
        with pytest.raises(transport.BackgroundModelHeldError, match='requires provider gemini'):
            gemini.provider_key('openai')
    foreground = transport.request('gemini-3.8-flash', {}, 'direct-key')
    assert foreground.full_url.startswith('https://generativelanguage.googleapis.com/')
    assert foreground.get_header('X-sotto-require-finite-budget') is None


@pytest.mark.parametrize('value', ['', '1', 'TRUE', 'yes'])
def test_unmetered_background_opt_in_requires_exact_true(monkeypatch, value):
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    monkeypatch.delenv('SOTTO_MODEL_PROXY_URL', raising=False)
    monkeypatch.delenv('SOTTO_MODEL_PROXY_TOKEN', raising=False)
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', value)
    with pytest.raises(transport.BackgroundModelHeldError):
        with transport.background_learning():
            pass
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', 'true')
    with transport.background_learning():
        assert not transport.background_proxy_required()


def test_background_preflight_requires_versioned_finite_available_capability(monkeypatch):
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'tenant-token')

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_): pass

    seen = []
    def open_capability(request, timeout):
        seen.append(request)
        return Response(json.dumps({'version': 1, 'finite': True, 'remaining_cents': 200,
                                    'can_admit': True}).encode())
    monkeypatch.setattr(transport.urllib.request, 'urlopen', open_capability)
    with transport.background_learning():
        assert transport.background_proxy_required()
    assert seen[0].full_url == 'https://proxy.example/v1/capabilities/background-budget'
    assert seen[0].get_header('Authorization') == 'Bearer tenant-token'

    for payload in ({'version': 1, 'finite': False, 'can_admit': True},
                    {'version': 1, 'finite': True, 'can_admit': False},
                    {'finite': True, 'can_admit': True}):
        monkeypatch.setattr(transport.urllib.request, 'urlopen',
                            lambda *a, payload=payload, **k: Response(json.dumps(payload).encode()))
        with pytest.raises(transport.BackgroundModelHeldError):
            with transport.background_learning():
                pass


def test_old_proxy_404_holds_background_before_any_model_request(monkeypatch):
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://old-proxy.example')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'tenant-token')
    monkeypatch.setattr(transport.urllib.request, 'urlopen', lambda request, timeout: (_ for _ in ()).throw(
        __import__('urllib').error.HTTPError(request.full_url, 404, 'not found', {}, None)))
    with pytest.raises(transport.BackgroundModelHeldError):
        with transport.background_learning():
            pytest.fail('old proxy must not admit background work')


@pytest.mark.parametrize('name', ['SOTTO_BRIEF_MODEL', 'SOTTO_FALLBACK_MODEL'])
def test_background_provider_override_holds_before_proxy_or_source_work(monkeypatch, name):
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'tenant-token')
    monkeypatch.setenv(name, 'openai/model')
    monkeypatch.setattr(transport, '_background_budget_preflight',
                        lambda: pytest.fail('provider must be rejected before capability request'))
    with pytest.raises(transport.BackgroundModelHeldError) as error:
        with transport.background_learning():
            pass
    assert error.value.reason == 'background_provider_unsupported'


def test_managed_null_budget_semantics_do_not_gain_finite_budget_header(managed_proxy):
    with transport.background_learning():
        req = transport.request('gemini-3.8-flash', {})
    assert req.get_header('X-sotto-require-finite-budget') is None
