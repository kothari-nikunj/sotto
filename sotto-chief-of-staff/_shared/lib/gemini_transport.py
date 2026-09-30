"""Native Gemini transport shared by compose, triage, research and vision.

Managed requests change only destination/authentication. No schema or grounding
translation, and no direct-key fallback when managed configuration is incomplete.
"""
import json
import os
import re
from pathlib import Path
import urllib.parse
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar


WRITING_STYLE = (Path(__file__).resolve().parents[1] / 'references/writing-style.md').read_text().strip()


def writing_system(system=None):
    return (system + '\n\n' if system else '') + WRITING_STYLE


_BACKGROUND_PROXY = ContextVar('sotto_background_proxy', default=False)
_BACKGROUND_ACTIVE = ContextVar('sotto_background_active', default=False)


class ModelBudgetUnavailableError(RuntimeError):
    """The authenticated proxy cannot admit another model reservation."""


class _NoProxyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a tenant bearer to a redirected destination.


def brief_model_preflight():
    """Check one managed reservation before fresh gathering; this buys no model call.

    Direct self-host keys keep their existing behavior. An unlimited managed route is
    valid too. Availability here is not a promise that a whole brief will fit.
    """
    if not managed():
        return
    token = credential()
    req = urllib.request.Request(
        _proxy_base() + '/v1/capabilities/background-budget',
        headers={'Authorization': f'Bearer {token}'}, method='GET')
    with urllib.request.build_opener(_NoProxyRedirect()).open(req, timeout=30) as response:
        raw = response.read(4097)
    if len(raw) > 4096:
        raise ValueError('Model allowance capability is too large')
    result = json.loads(raw)
    if (not isinstance(result, dict) or type(result.get('version')) is not int
            or result['version'] != 1 or type(result.get('finite')) is not bool
            or type(result.get('can_admit')) is not bool
            or not isinstance(result.get('supported_native_models'), list)):
        raise ValueError('Model allowance capability is unsupported')
    provider, model = effective_compose_model()
    if provider != 'gemini' or model not in result['supported_native_models']:
        raise ValueError('Brief model is unsupported by this proxy')
    remaining = result.get('remaining_cents')
    if not result['finite']:
        if remaining is not None:
            raise ValueError('Model allowance capability is invalid')
        return
    if (type(remaining) is not int or remaining < 0
            or result['can_admit'] != (remaining >= 200)):
        raise ValueError('Model allowance capability is invalid')
    if not result['can_admit']:
        raise ModelBudgetUnavailableError('Model allowance reached; an account update is needed')


def proxy_budget_exhausted(error):
    """Recognize only this proxy's typed denial, not an arbitrary provider HTTP 402."""
    if (not isinstance(error, urllib.error.HTTPError) or error.code != 402
            or not (managed() or background_proxy_required())):
        return False
    try:
        prefix = _proxy_base() + '/native/v1beta/models/'
        url = error.geturl()
        if not isinstance(url, str) or not url.startswith(prefix):
            return False
        if not re.fullmatch(r'[A-Za-z0-9._-]+:generateContent', url[len(prefix):]):
            return False
        raw = error.read(4097)
        if len(raw) > 4096:
            return False
        result = json.loads(raw)
        return (isinstance(result, dict) and isinstance(result.get('error'), dict)
                and result['error'].get('code') == 'sotto_budget_exhausted')
    except (OSError, ValueError, KeyError):
        return False


class BackgroundModelHeldError(RuntimeError):
    """Background learning has no explicitly authorized model-spend path."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def background_proxy_required():
    return _BACKGROUND_PROXY.get()


def background_learning_active():
    return _BACKGROUND_ACTIVE.get()


def effective_compose_model():
    """Resolve the effective primary into provider/model for admission and dispatch."""
    ref = ((os.environ.get('SOTTO_BRIEF_MODEL') or '').strip()
           or os.environ.get('SOTTO_GEMINI_MODEL', 'gemini-3.8-flash')).strip()
    if '/' not in ref:
        return 'gemini', ref
    provider, model = ref.split('/', 1)
    return provider.strip().lower(), model.strip()


def _proxy_base():
    base = os.environ['SOTTO_MODEL_PROXY_URL'].strip().rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Model proxy URL must not contain credentials, query or fragment')
    if parsed.scheme != 'https' and not (
            parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}):
        raise ValueError('Model proxy requires HTTPS (except local tests)')
    return base


def _background_capability_preflight(require_finite):
    """Authenticate the route/model before source access; require budget only for self-host."""
    token = os.environ['SOTTO_MODEL_PROXY_TOKEN'].strip()
    request = urllib.request.Request(
        _proxy_base() + '/v1/capabilities/background-budget',
        headers={'Authorization': f'Bearer {token}'}, method='GET')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except (OSError, ValueError, urllib.error.HTTPError, urllib.error.URLError) as error:
        raise BackgroundModelHeldError(
            'background_budget_capability_unavailable',
            'background budget capability is unavailable') from error
    models = result.get('supported_native_models') if isinstance(result, dict) else None
    if not isinstance(result, dict) or result.get('version') != 1 or not isinstance(models, list):
        # A pre-upgrade proxy answers without the model list. That is a proxy capability
        # problem, not a budget problem, and the operator must be told which one it is.
        raise BackgroundModelHeldError(
            'background_capability_unsupported',
            'background capability version is not supported by this proxy')
    provider, model = effective_compose_model()
    if provider != 'gemini' or model not in models:
        raise BackgroundModelHeldError(
            'background_model_unsupported', 'background model is not supported by the proxy')
    if ((require_finite and result.get('finite') is not True)
            or (result.get('finite') is True and result.get('can_admit') is not True)):
        raise BackgroundModelHeldError(
            'background_budget_unavailable', 'background budget is not finite and available')


def _background_provider_preflight():
    """Background proxy supports Gemini's native surface; fallback is disabled for this cycle."""
    provider, _ = effective_compose_model()
    if provider != 'gemini':
        raise BackgroundModelHeldError(
            'background_provider_unsupported',
            'The effective background model must use Gemini')


@contextmanager
def background_learning():
    """Select the owner's background-spend policy for one memory-cycle invocation."""
    active_token = _BACKGROUND_ACTIVE.set(True)
    try:
        if os.environ.get('SOTTO_BACKGROUND_UNMETERED') == 'true' and not managed():
            yield
            return
        if not (os.environ.get('SOTTO_MODEL_PROXY_URL', '').strip()
                and os.environ.get('SOTTO_MODEL_PROXY_TOKEN', '').strip()):
            raise BackgroundModelHeldError(
                'background_budget_not_configured',
                'background model proxy and tenant credential are not configured')
        _background_provider_preflight()
        _background_capability_preflight(require_finite=not managed())
        proxy_token = _BACKGROUND_PROXY.set(not managed())
        try:
            yield
        finally:
            _BACKGROUND_PROXY.reset(proxy_token)
    finally:
        _BACKGROUND_ACTIVE.reset(active_token)


def managed():
    return os.environ.get('SOTTO_DEPLOYMENT_MODE') == 'managed'


def credential(direct_key=None):
    if managed() or background_proxy_required():
        token = os.environ.get('SOTTO_MODEL_PROXY_TOKEN', '').strip()
        if not token or not os.environ.get('SOTTO_MODEL_PROXY_URL', '').strip():
            raise RuntimeError('Gemini model proxy is not configured')
        return token
    return (direct_key if direct_key is not None else os.environ.get('GOOGLE_AI_API_KEY', '')).strip()


def endpoint(model, key=None):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', model):
        raise ValueError('Invalid Gemini model identifier')
    token = credential(key)
    if managed() or background_proxy_required():
        base = _proxy_base()
        return f'{base}/native/v1beta/models/{model}:generateContent', {'Authorization': f'Bearer {token}'}
    return (f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={token}', {})


def attribution_headers(url):
    """The workload/operation headers, for any request that reaches the Sotto proxy — native or
    compatible-chat. A request bound elsewhere carries none (they would be meaningless there)."""
    import model_work
    proxy = os.environ.get('SOTTO_MODEL_PROXY_URL', '').strip().rstrip('/')
    if managed() or background_proxy_required() or (proxy and str(url).startswith(proxy)):
        return model_work.headers()
    return {}


def request(model, body, key=None):
    url, headers = endpoint(model, key)
    headers.update(attribution_headers(url))
    if background_proxy_required():
        headers['X-Sotto-Require-Finite-Budget'] = 'true'
    return urllib.request.Request(url, data=json.dumps(body).encode(),
                                  headers={'Content-Type': 'application/json', **headers}, method='POST')
