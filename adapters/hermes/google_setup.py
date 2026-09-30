"""Sotto Google consent, using the token format consumed by Hermes.

Gmail, Calendar and Google Contacts consent with persisted PKCE state.
Self-hosted Web-client consent and the Cloud broker handoff share the runtime token format.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import parse_qs, urlparse

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from oauthlib.oauth2 import OAuth2Error
from requests.exceptions import RequestException

SCOPES = {
    'email': 'https://www.googleapis.com/auth/gmail.modify',
    'calendar': 'https://www.googleapis.com/auth/calendar',
    'contacts': 'https://www.googleapis.com/auth/contacts',
}
PENDING_TTL = 3600
WEB_CALLBACK = '/google/oauth/callback'
_BINDING_RE = re.compile(r'[0-9a-f]{64}\Z')


def home():
    return Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))


def write_private(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(payload)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def pending_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'google_oauth.lock').open('a') as lock:
        os.chmod(root / 'google_oauth.lock', 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def web_redirect_uri(explicit=None):
    domain = os.environ.get('RAILWAY_PUBLIC_DOMAIN', '').strip()
    expected = f'https://{domain}{WEB_CALLBACK}' if domain else None
    if explicit and expected and explicit != expected:
        raise ValueError('Google callback must match the configured Railway public domain.')
    redirect = explicit or expected
    if not redirect:
        raise ValueError('Set RAILWAY_PUBLIC_DOMAIN before connecting Google with a Web client.')
    parsed = urlparse(redirect)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path != WEB_CALLBACK):
        raise ValueError('Google Web callback must be a public HTTPS /google/oauth/callback URL.')
    return redirect


def _client(root):
    client = json.loads((root / 'google_client_secret.json').read_text())
    if not isinstance(client, dict):
        raise ValueError('Invalid Google OAuth client JSON.')
    if 'web' in client and isinstance(client['web'], dict):
        config = client['web']
        if (not all(isinstance(config.get(key), str) and config[key]
                    for key in ('client_id', 'client_secret'))
                or config.get('auth_uri') not in (
                    'https://accounts.google.com/o/oauth2/auth',
                    'https://accounts.google.com/o/oauth2/v2/auth')
                or config.get('token_uri') != 'https://oauth2.googleapis.com/token'):
            raise ValueError('Expected a complete Google Web client with official Google OAuth endpoints.')
        return client, 'web', config
    if 'installed' in client and isinstance(client['installed'], dict):
        return client, 'installed', client['installed']
    raise ValueError('Expected a Google Web application OAuth client JSON.')


def auth_url(root, services, reuse_pending=False, browser_binding='', redirect_uri=None):
    scopes = [SCOPES[s] for s in services.split(',')]
    client, kind, config = _client(root)
    if kind != 'web':
        raise ValueError('Desktop Google clients use a broken localhost callback. Create a Web application OAuth client with this Sotto HTTPS callback.')
    if not _BINDING_RE.fullmatch(browser_binding):
        raise ValueError('Google authorization needs a browser binding.')
    redirect = web_redirect_uri(redirect_uri)
    if redirect not in config.get('redirect_uris', []):
        raise ValueError(f'Add {redirect} to the Google Web client authorized redirect URIs.')
    with pending_lock(root):
        if reuse_pending:
            try:
                pending = json.loads((root / 'google_oauth_pending.json').read_text())
                url = (root / 'google_oauth_last_url.txt').read_text()
                query = parse_qs(urlparse(url).query)
                if (0 <= time.time() - pending['created_at'] < PENDING_TTL
                        and pending['client_id'] == config['client_id']
                        and pending.get('client_kind') == 'web'
                        and pending.get('browser_binding') == browser_binding
                        and pending['scopes'] == scopes
                        and pending['redirect_uri'] == redirect
                        and pending['code_verifier'] and pending['state']
                        and query.get('state') == [pending['state']]):
                    return url
            except (OSError, ValueError, KeyError, TypeError):
                pass
        flow = Flow.from_client_config(client, scopes=scopes, redirect_uri=redirect,
                                       autogenerate_code_verifier=True)
        url, state = flow.authorization_url(access_type='offline', prompt='consent')
        write_private(root / 'google_oauth_pending.json', json.dumps({
            'state': state, 'code_verifier': flow.code_verifier, 'scopes': scopes,
            'created_at': time.time(), 'redirect_uri': redirect,
            'client_id': config['client_id'], 'client_kind': kind,
            'browser_binding': browser_binding,
        }))
        write_private(root / 'google_oauth_last_url.txt', url)
        return url


def exchange(root, raw, browser_binding=''):
    with pending_lock(root):
        pending_path = root / 'google_oauth_pending.json'
        pending = json.loads(pending_path.read_text())
        age = time.time() - pending['created_at']
        if not 0 <= age < PENDING_TTL:
            raise ValueError('Google authorization expired; start a fresh connection.')
        _, kind, config = _client(root)
        # Only browser-bound Web sessions minted by auth_url() are exchanged. Pending files
        # from the old localhost/Desktop flow carry no client or browser binding.
        if (kind != 'web' or pending.get('client_kind') != 'web'
                or config.get('client_id') != pending.get('client_id')):
            raise ValueError('Google OAuth client changed; start a fresh connection.')
        if (web_redirect_uri(pending['redirect_uri']) not in config.get('redirect_uris', [])):
            raise ValueError('Google callback is no longer registered on this Web client.')
        if (not _BINDING_RE.fullmatch(browser_binding)
                or not hmac.compare_digest(browser_binding, pending.get('browser_binding', ''))):
            raise ValueError('Google authorization browser binding mismatch.')
        code = raw.strip()
        if not code.startswith('https://'):
            raise ValueError('Expected the complete Google HTTPS callback URL.')
        parsed = urlparse(code)
        expected = urlparse(pending['redirect_uri'])
        if (parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.fragment) != (
                expected.scheme, expected.netloc, expected.path, '', ''):
            raise ValueError('Google callback URL mismatch.')
        query = parse_qs(parsed.query, strict_parsing=True)
        if len(query.get('state', [])) != 1 or len(query.get('code', [])) != 1:
            raise ValueError('Expected one Google authorization code and state.')
        returned_state = query['state'][0]
        code = query['code'][0]
        if not hmac.compare_digest(returned_state, pending['state']):
            raise ValueError('Google authorization state mismatch.')
        if not code:
            raise ValueError('No Google authorization code provided.')
        flow = Flow.from_client_secrets_file(
            str(root / 'google_client_secret.json'), scopes=pending['scopes'],
            redirect_uri=pending['redirect_uri'], state=pending['state'],
            code_verifier=pending['code_verifier'])
        flow.fetch_token(code=code)
        credentials = flow.credentials
        if not credentials.refresh_token:
            raise ValueError('Google did not return offline access; reconnect with consent.')
        granted = credentials.granted_scopes
        if not granted:
            raise ValueError('Google did not report granted scopes; reconnect with consent.')
        granted = list(granted)
        if set(pending['scopes']) - set(granted):
            raise ValueError('Google did not grant all requested services; reconnect with consent.')
        payload = json.loads(credentials.to_json())
        payload['type'] = 'authorized_user'
        payload['scopes'] = granted
        write_private(root / 'google_token.json', json.dumps(payload))
        pending_path.unlink()
        (root / 'google_oauth_last_url.txt').unlink(missing_ok=True)


def check(root):
    path = root / 'google_token.json'
    if not path.exists():
        return False
    original = path.read_text()
    credentials = Credentials.from_authorized_user_info(json.loads(original))
    if not credentials.valid and credentials.refresh_token:
        credentials.refresh(Request())
        # Compare-and-write under the callback's lock: a reconnect that replaced the
        # token during this refresh wins over the old grant's refreshed copy.
        with pending_lock(root):
            if path.read_text() == original:
                write_private(path, credentials.to_json())
    return credentials.valid


def install_cloud_google(payload):
    """Accept the broker's consent in the existing Hermes authorized-user format.

    The active token is a private file on the tenant volume, as required by the
    upstream Google skill. The broker never receives any existing tenant token.
    """
    root = home()
    if payload is None:
        # Signing in without data consent is valid; it must not retain old grants.
        (root / 'google_token.json').unlink(missing_ok=True)
        return []
    if (not isinstance(payload, dict) or payload.get('type') != 'authorized_user'
            or payload.get('token_uri') != 'https://oauth2.googleapis.com/token'
            or not all(isinstance(payload.get(k), str) and payload[k]
                       for k in ('client_id', 'client_secret', 'refresh_token', 'token'))
            or not isinstance(payload.get('scopes'), list)
            or not all(isinstance(s, str) for s in payload['scopes'])):
        raise ValueError('Invalid Cloud Google credential')
    allowed = {'openid', 'email', 'profile', 'https://www.googleapis.com/auth/userinfo.email',
               'https://www.googleapis.com/auth/userinfo.profile', *SCOPES.values(),
               'https://www.googleapis.com/auth/gmail.compose',
               'https://www.googleapis.com/auth/gmail.readonly',
               'https://www.googleapis.com/auth/calendar.readonly',
               'https://www.googleapis.com/auth/contacts.readonly'}
    if set(payload['scopes']) - allowed:
        raise ValueError('Unexpected Google grant')
    write_private(root / 'google_token.json', json.dumps(payload))
    # Hermes' client reader accepts installed/web. Keep the web client metadata
    # beside its token so direct token refresh needs no live account service.
    write_private(root / 'google_client_secret.json', json.dumps({'web': {
        'client_id': payload['client_id'], 'client_secret': payload['client_secret'],
        'token_uri': payload['token_uri'], 'auth_uri': 'https://accounts.google.com/o/oauth2/auth'}}))
    return payload['scopes']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true')
    group.add_argument('--auth-url', action='store_true')
    group.add_argument('--auth-code')
    group.add_argument('--client-secret')
    parser.add_argument('--services', default='email,calendar,contacts')
    parser.add_argument('--format', choices=['json'], default='json')
    parser.add_argument('--reuse-pending', action='store_true',
                        help='Keep an unexpired authorization session for the same client and scopes')
    parser.add_argument('--browser-binding', default='',
                        help='SHA256 digest of the receiver-issued browser binding cookie')
    parser.add_argument('--redirect-uri', default=None,
                        help='Exact registered Web client callback URL')
    args = parser.parse_args()
    root = home()
    if args.check:
        return 0 if check(root) else 1
    if args.client_secret:
        write_private(root / 'google_client_secret.json', Path(args.client_secret).read_text())
    elif args.auth_url:
        auth_url(root, args.services, reuse_pending=args.reuse_pending,
                 browser_binding=args.browser_binding, redirect_uri=args.redirect_uri)
    else:
        exchange(root, args.auth_code, browser_binding=args.browser_binding)
    # Credentials, codes and authorize URLs never go into deployment logs.
    print(json.dumps({'ok': True}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, GoogleAuthError, OAuth2Error, RequestException):
        print('Google setup failed. Check the client and start a fresh authorization.')
        raise SystemExit(1) from None
