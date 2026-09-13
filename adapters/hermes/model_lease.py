"""Renew the managed model lease without restarting Hermes or changing its bearer.

The receiver calls tick from its existing maintenance loop. Only content-free status
is persisted; the independent renewal credential never enters a skill environment.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

try:
    from control_vault import get as control_token
except ModuleNotFoundError:
    control_token = lambda env=None: (env or os.environ).get('SOTTO_CONTROL_TOKEN', '')

RENEW_BEFORE_SECONDS = 3 * 86400
RETRY_SECONDS = 3600
MAX_LEASE_SECONDS = 31 * 86400


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def status(data):
    try:
        state = json.loads((Path(data) / 'config/model-lease.json').read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data, value):
    parent = Path(data) / 'config'
    parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.model-lease-', dir=parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, parent / 'model-lease.json')
    finally:
        Path(name).unlink(missing_ok=True)


def tick(data, env=None, *, now=None, opener=None):
    """One bounded request when renewal is due; return sanitized lease state.

    Caller serializes ticks (the receiver's existing heartbeat is sufficient).
    Failure never invalidates the current model credential or restarts processes.
    """
    env = os.environ if env is None else env
    if env.get('SOTTO_DEPLOYMENT_MODE') != 'managed':
        return {}
    now = time.time() if now is None else now
    tenant = env.get('SOTTO_TENANT_ID', '')
    token = env.get('SOTTO_MODEL_PROXY_TOKEN', '')
    base = env.get('SOTTO_MODEL_PROXY_URL', '').rstrip('/')
    identity = hashlib.sha256((tenant + '\n' + base + '\n' + token).encode()).hexdigest()
    state = status(data)
    if state.get('configuration_id') != identity:
        state = {'tenant_id': tenant, 'configuration_id': identity}
    try:
        retry_at, expiry = float(state.get('retry_at', 0)), float(state.get('expires_at', 0))
    except (TypeError, ValueError):
        state = {'tenant_id': tenant, 'configuration_id': identity}
        retry_at = expiry = 0
    if retry_at > now or expiry - now > RENEW_BEFORE_SECONDS:
        return state
    state.update(last_attempt_at=now, retry_at=now + RETRY_SECONDS)
    try:
        parsed = urlsplit(base)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not tenant or not token):
            raise ValueError('Invalid managed model configuration')
        control = control_token(env)
        if not control:
            raise ValueError('No model renewal credential configured')
        body = json.dumps({'tenant_id': tenant,
                           'token_sha256': hashlib.sha256(token.encode()).hexdigest()}).encode()
        request = urllib.request.Request(base + '/v1/lease/renew', data=body, method='POST',
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + control})
        open_request = opener or urllib.request.build_opener(NoRedirect()).open
        with open_request(request, timeout=15) as response:
            result = json.loads(response.read(4096))
        expiry = result.get('expires_at')
        if (result.get('tenant_id') != tenant or isinstance(expiry, bool)
                or not isinstance(expiry, (int, float)) or not now < expiry <= now + MAX_LEASE_SECONDS):
            raise ValueError('Invalid model lease acknowledgment')
        state.update(expires_at=expiry, last_success_at=now, error=None, retry_at=0)
    except Exception as error:  # noqa: BLE001 — never expose token-bearing request/errors.
        state['error'] = type(error).__name__
    _write(data, state)
    return state
