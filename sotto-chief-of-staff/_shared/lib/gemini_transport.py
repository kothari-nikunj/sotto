"""Native Gemini transport shared by compose, triage, research and vision.

Managed requests change only destination/authentication. No schema or grounding
translation, and no direct-key fallback when managed configuration is incomplete.
"""
import json
import os
import re
import urllib.parse
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar


_BACKGROUND_PROXY = ContextVar('sotto_background_proxy', default=False)


class BackgroundModelHeldError(RuntimeError):
    """Background learning has no explicitly authorized model-spend path."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def background_proxy_required():
    return _BACKGROUND_PROXY.get()


def _proxy_base():
    base = os.environ['SOTTO_MODEL_PROXY_URL'].strip().rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Model proxy URL must not contain credentials, query or fragment')
    if parsed.scheme != 'https' and not (
            parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}):
        raise ValueError('Model proxy requires HTTPS (except local tests)')
    return base


def _background_budget_preflight():
    """Require authenticated versioned proof that the next conservative reservation can fit."""
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
    if (not isinstance(result, dict) or result.get('version') != 1
            or result.get('finite') is not True or result.get('can_admit') is not True):
        raise BackgroundModelHeldError(
            'background_budget_unavailable', 'background budget is not finite and available')


def _background_provider_preflight():
    """Background proxy supports Gemini's native surface; reject configured family escapes early."""
    for name in ('SOTTO_BRIEF_MODEL', 'SOTTO_FALLBACK_MODEL'):
        ref = os.environ.get(name, '').strip()
        if '/' in ref and ref.split('/', 1)[0].strip().lower() != 'gemini':
            raise BackgroundModelHeldError(
                'background_provider_unsupported',
                f'{name} must use Gemini for metered background learning')


@contextmanager
def background_learning():
    """Select the owner's background-spend policy for one memory-cycle invocation."""
    if managed() or os.environ.get('SOTTO_BACKGROUND_UNMETERED') == 'true':
        yield
        return
    if not (os.environ.get('SOTTO_MODEL_PROXY_URL', '').strip()
            and os.environ.get('SOTTO_MODEL_PROXY_TOKEN', '').strip()):
        raise BackgroundModelHeldError(
            'background_budget_not_configured',
            'background model proxy and tenant credential are not configured')
    _background_provider_preflight()
    _background_budget_preflight()
    token = _BACKGROUND_PROXY.set(True)
    try:
        yield
    finally:
        _BACKGROUND_PROXY.reset(token)


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


def request(model, body, key=None):
    url, headers = endpoint(model, key)
    if background_proxy_required():
        headers['X-Sotto-Require-Finite-Budget'] = 'true'
    return urllib.request.Request(url, data=json.dumps(body).encode(),
                                  headers={'Content-Type': 'application/json', **headers}, method='POST')
