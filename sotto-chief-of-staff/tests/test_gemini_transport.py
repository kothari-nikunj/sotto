import json
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_shared/lib'))
import gemini_transport as transport
import gemini


SUPPORTED = ['gemini-3-flash-preview', 'gemini-3.5-flash-lite', 'gemini-3.8-flash']


def capability(*, finite=True, can_admit=True):
    return {'version': 1, 'finite': finite, 'remaining_cents': 200 if finite else None,
            'can_admit': can_admit, 'supported_native_models': SUPPORTED}


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
    assert cloud.data == direct.data
    sent = json.loads(cloud.data)
    assert sent == body  # raw extraction transport preserves source instructions exactly
    assert {k: v for k, v in sent.items() if k != 'systemInstruction'} == {k: v for k, v in body.items() if k != 'systemInstruction'}
    assert body['systemInstruction']['parts'] == [{'text': 'system'}]
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
    monkeypatch.setattr(transport, '_background_capability_preflight', lambda **k: None)
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
        return Response(json.dumps(capability()).encode())
    monkeypatch.setattr(transport.urllib.request, 'urlopen', open_capability)
    with transport.background_learning():
        assert transport.background_proxy_required()
    assert seen[0].full_url == 'https://proxy.example/v1/capabilities/background-budget'
    assert seen[0].get_header('Authorization') == 'Bearer tenant-token'

    for payload in (capability(finite=False), capability(can_admit=False),
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


def test_pre_upgrade_proxy_payload_holds_as_a_capability_problem_not_a_budget_one(monkeypatch):
    """The rollout shape: a proxy that predates the model list answers version 1 with a healthy
    finite budget and no `supported_native_models`. Background learning must hold — and say the
    proxy cannot serve this capability, not that the owner is out of budget."""
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://old-proxy.example')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'tenant-token')

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_): pass

    payload = {'version': 1, 'finite': True, 'remaining_cents': 200, 'can_admit': True}
    assert 'supported_native_models' not in payload
    monkeypatch.setattr(transport.urllib.request, 'urlopen',
                        lambda *a, **k: Response(json.dumps(payload).encode()))
    with pytest.raises(transport.BackgroundModelHeldError) as error:
        with transport.background_learning():
            pytest.fail('a pre-upgrade proxy must not admit background work')
    assert error.value.reason == 'background_capability_unsupported'


def test_background_provider_override_holds_before_proxy_or_source_work(monkeypatch):
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'tenant-token')
    monkeypatch.setenv('SOTTO_BRIEF_MODEL', 'openai/model')
    monkeypatch.setattr(transport, '_background_capability_preflight',
                        lambda require_finite: pytest.fail(
                            'provider must be rejected before capability request'))
    with pytest.raises(transport.BackgroundModelHeldError) as error:
        with transport.background_learning():
            pass
    assert error.value.reason == 'background_provider_unsupported'


def test_managed_null_budget_semantics_do_not_gain_finite_budget_header(monkeypatch, managed_proxy):
    monkeypatch.setattr(transport, '_background_capability_preflight', lambda require_finite: None)
    with transport.background_learning():
        req = transport.request('gemini-3.8-flash', {})
    assert req.get_header('X-sotto-require-finite-budget') is None


def test_managed_capability_allows_explicit_unlimited_but_rejects_exhausted_finite(monkeypatch,
                                                                                  managed_proxy):
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_): pass

    monkeypatch.setattr(transport.urllib.request, 'urlopen',
                        lambda *a, **k: Response(json.dumps(capability(finite=False,
                                                                       can_admit=False)).encode()))
    with transport.background_learning():
        assert transport.background_learning_active()
    monkeypatch.setattr(transport.urllib.request, 'urlopen',
                        lambda *a, **k: Response(json.dumps(capability(can_admit=False)).encode()))
    with pytest.raises(transport.BackgroundModelHeldError) as error:
        with transport.background_learning():
            pass
    assert error.value.reason == 'background_budget_unavailable'


def test_background_rejects_proxy_unsupported_primary(monkeypatch):
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'tenant-token')
    monkeypatch.setenv('SOTTO_GEMINI_MODEL', 'gemini-not-served')

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_): pass

    monkeypatch.setattr(transport.urllib.request, 'urlopen',
                        lambda *a, **k: Response(json.dumps(capability()).encode()))
    with pytest.raises(transport.BackgroundModelHeldError) as error:
        with transport.background_learning():
            pass
    assert error.value.reason == 'background_model_unsupported'


@pytest.mark.parametrize('mode', ['finite', 'managed', 'unmetered'])
def test_background_model_call_never_retries(monkeypatch, managed_proxy, mode):
    if mode != 'managed':
        monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    if mode == 'unmetered':
        monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', 'true')
    else:
        monkeypatch.delenv('SOTTO_BACKGROUND_UNMETERED', raising=False)
        monkeypatch.setattr(transport, '_background_capability_preflight', lambda **k: None)
    calls = []
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **k: (
        calls.append(True), (_ for _ in ()).throw(__import__('urllib').error.HTTPError(
            'https://provider.invalid', 503, 'down', {}, None)))[1])
    with transport.background_learning():
        with pytest.raises(__import__('urllib').error.HTTPError):
            gemini.call_gemini('prompt', {})
    assert calls == [True]


def test_background_uses_only_effective_primary_and_ignores_foreground_fallback(monkeypatch):
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', 'true')
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'test-key')
    monkeypatch.setenv('SOTTO_GEMINI_MODEL', '  gemini-3.8-flash  ')
    monkeypatch.delenv('SOTTO_BRIEF_MODEL', raising=False)
    monkeypatch.setenv('SOTTO_FALLBACK_MODEL', 'anthropic/foreign-model')
    calls = []
    def primary(provider, model, key, prompt, **kwargs):
        calls.append((provider, model, key))
        return 'result'
    monkeypatch.setattr(gemini, 'model_once', primary)
    with transport.background_learning():
        assert gemini.call_gemini('prompt', {}) == 'result'
    assert calls == [('gemini', 'gemini-3.8-flash', 'test-key')]
    with pytest.raises(RuntimeError, match='crosses model families'):
        gemini.call_gemini('prompt', {})
