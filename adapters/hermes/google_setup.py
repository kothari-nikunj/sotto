"""Managed pilot Google consent, using the token format consumed by Hermes.

Gmail, Calendar and Google Contacts consent with persisted PKCE state.
The legacy desktop flow and the Cloud broker handoff share the runtime token format.
"""
import argparse
import hmac
import json
import os
from pathlib import Path
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
REDIRECT = 'http://localhost:1'
PENDING_TTL = 3600


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


def auth_url(root, services):
    scopes = [SCOPES[s] for s in services.split(',')]
    client = json.loads((root / 'google_client_secret.json').read_text())
    if 'installed' not in client:
        raise ValueError('The personal pilot requires a Desktop app OAuth client.')
    flow = Flow.from_client_config(client, scopes=scopes, redirect_uri=REDIRECT,
                                   autogenerate_code_verifier=True)
    url, state = flow.authorization_url(access_type='offline', prompt='consent')
    write_private(root / 'google_oauth_pending.json', json.dumps({
        'state': state, 'code_verifier': flow.code_verifier, 'scopes': scopes,
        'created_at': time.time(), 'redirect_uri': REDIRECT,
    }))
    write_private(root / 'google_oauth_last_url.txt', url)
    return url


def exchange(root, raw):
    pending_path = root / 'google_oauth_pending.json'
    pending = json.loads(pending_path.read_text())
    age = time.time() - pending['created_at']
    if not 0 <= age < PENDING_TTL:
        raise ValueError('Google authorization expired; start a fresh connection.')
    code = raw.strip()
    if code.startswith('http'):
        parsed = urlparse(code)
        if (parsed.scheme, parsed.netloc) != ('http', 'localhost:1'):
            raise ValueError('Expected the localhost callback URL.')
        query = parse_qs(parsed.query)
        returned_state = (query.get('state') or [''])[0]
        if not hmac.compare_digest(returned_state, pending['state']):
            raise ValueError('Google authorization state mismatch.')
        code = (query.get('code') or [''])[0]
    if not code:
        raise ValueError('No Google authorization code provided.')
    # A manually pasted raw code is bound to this pending session by PKCE.
    flow = Flow.from_client_secrets_file(
        str(root / 'google_client_secret.json'), scopes=pending['scopes'],
        redirect_uri=pending['redirect_uri'], state=pending['state'],
        code_verifier=pending['code_verifier'])
    flow.fetch_token(code=code)
    credentials = flow.credentials
    if not credentials.refresh_token:
        raise ValueError('Google did not return offline access; reconnect with consent.')
    payload = json.loads(credentials.to_json())
    payload['type'] = 'authorized_user'
    payload['scopes'] = list(credentials.granted_scopes or pending['scopes'])
    write_private(root / 'google_token.json', json.dumps(payload))
    pending_path.unlink()
    (root / 'google_oauth_last_url.txt').unlink(missing_ok=True)


def check(root):
    path = root / 'google_token.json'
    if not path.exists():
        return False
    credentials = Credentials.from_authorized_user_file(str(path))
    if not credentials.valid and credentials.refresh_token:
        credentials.refresh(Request())
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
    args = parser.parse_args()
    root = home()
    if args.check:
        return 0 if check(root) else 1
    if args.client_secret:
        write_private(root / 'google_client_secret.json', Path(args.client_secret).read_text())
    elif args.auth_url:
        auth_url(root, args.services)
    else:
        exchange(root, args.auth_code)
    # Credentials, codes and authorize URLs never go into deployment logs.
    print(json.dumps({'ok': True}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, GoogleAuthError, OAuth2Error, RequestException):
        print('Google setup failed. Check the client and start a fresh authorization.')
        raise SystemExit(1) from None
