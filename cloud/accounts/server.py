"""Invite-only Cloud sign-in broker; adopts registered tenants without fleet credentials.

OAuth state and short-lived handoffs are encrypted on the persistent volume. Google
refresh credentials are erased from the broker after the tenant accepts them. No
request URLs, bodies, tokens or Google error bodies are logged.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import time
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from browser import BrowserSessions, COOKIE, SESSION_TTL
from linking import Linking
import pages
from registry import Registry, google_identity, https_base

TTL = 900
PENDING_SIGNINS = 50
MAX_SIGNINS = 500
DEVICE_CODE_ATTEMPTS = 5
SCOPES = ['openid', 'email', 'profile', 'https://www.googleapis.com/auth/gmail.modify',
          'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/contacts']


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def public_key(value):
    key = serialization.load_der_public_key(base64.b64decode(value, validate=True))
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError('Expected a P-256 device key')
    return value


class Broker:
    def __init__(self, path, config):
        self.path, self.config = str(path), config
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.cipher = Fernet(config['state_key'].encode())
        self.origin = https_base(config['origin'])
        self.instance = https_base(config['instance'])
        self.redirect = self.origin + '/oauth/google/callback'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (state TEXT PRIMARY KEY, poll TEXT UNIQUE, expires REAL, '
                       'status TEXT, secret BLOB, result BLOB)')
            db.execute('CREATE TABLE IF NOT EXISTS account (slot INTEGER PRIMARY KEY CHECK(slot=1), '
                       'sub TEXT UNIQUE NOT NULL, tenant TEXT NOT NULL)')
        self.registry = Registry(self.db, self.seal, self.open)
        self.browser = BrowserSessions(self.registry)
        self.linking = Linking(self.registry)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS device_confirmation ('
                       'state TEXT PRIMARY KEY REFERENCES sessions(state) ON DELETE CASCADE, '
                       'code_hash TEXT NOT NULL, browser_id TEXT, attempts INTEGER NOT NULL DEFAULT 0)')
            columns = {row[1] for row in db.execute('PRAGMA table_info(device_confirmation)')}
            if 'code_secret' not in columns:
                db.execute('ALTER TABLE device_confirmation ADD COLUMN code_secret BLOB')
        # Only the future authenticated transport supervisor may publish a ready
        # line here. Neither an HTTP caller nor a model can choose its destination.
        self.messaging_line = None
        self.registry.register(config['tenant'], self.instance, config['control_token'], config['owner_email'])
        with self.db() as db:
            legacy = db.execute('SELECT sub,tenant FROM account WHERE slot=1').fetchone()
        if legacy:
            self.registry.adopt_legacy(legacy['sub'], legacy['tenant'], config['owner_email'])
        os.chmod(self.path, 0o600)

    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def seal(self, value):
        return self.cipher.encrypt(json.dumps(value).encode())

    def open(self, value):
        return json.loads(self.cipher.decrypt(value))

    def start(self, body, *, browser_token=None):
        entry = body.get('entry', 'mac')
        if entry not in ('mac', 'browser'):
            raise ValueError('Invalid sign-in entry')
        device = {}
        if entry == 'mac':
            key = public_key(body['public_key'])
            challenge = body['challenge']
            if not isinstance(challenge, str) or not 32 <= len(challenge) <= 128:
                raise ValueError('Invalid device challenge')
            device = {'public_key': key, 'challenge': challenge}
        elif 'public_key' in body or 'challenge' in body:
            raise ValueError('Browser sign-in cannot enroll a device')
        elif not browser_token:
            raise PermissionError('Start browser sign-in in this browser')
        state, poll, verifier, nonce = [secrets.token_urlsafe(32) for _ in range(4)]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
            # Anonymous starts may replace old unverified attempts, never occupy
            # every slot until TTL or evict verified credential handoffs.
            evictable = ("status='pending' AND NOT EXISTS (SELECT 1 FROM device_confirmation d "
                         "WHERE d.state=sessions.state AND d.browser_id IS NOT NULL) "
                         "AND NOT EXISTS (SELECT 1 FROM browser_oauth b WHERE b.state=sessions.state)")
            pending_count = db.execute('SELECT COUNT(*) FROM sessions WHERE ' + evictable).fetchone()[0]
            if pending_count >= PENDING_SIGNINS:
                db.execute('DELETE FROM sessions WHERE state IN (SELECT state FROM sessions WHERE '
                           + evictable + ' ORDER BY expires,state LIMIT ?)',
                           (pending_count - PENDING_SIGNINS + 1,))
            if db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] >= MAX_SIGNINS:
                raise ValueError('Sign-in capacity reached; try again shortly')
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,NULL)', (
                digest(state), digest(poll), time.time() + TTL, 'pending', self.seal({
                    **device, 'entry': entry, 'verifier': verifier, 'nonce': nonce})))
            if entry == 'mac':
                db.execute('INSERT INTO device_confirmation(state,code_hash) VALUES (?,?)',
                           (digest(state), ''))
            if entry == 'browser':
                self.browser.bind(db, browser_token, digest(state), poll)
        result = {'authorization_url': self.google_url(state, verifier, nonce), 'expires_in': TTL}
        if entry == 'mac':
            result.update(authorization_url=self.origin + '/device/connect?' + urlencode({'state': state}),
                          poll_token=poll)
        return result

    def google_url(self, state, verifier, nonce):
        query = {'client_id': self.config['client']['client_id'], 'redirect_uri': self.redirect,
                 'response_type': 'code', 'scope': ' '.join(SCOPES), 'state': state, 'nonce': nonce,
                 'access_type': 'offline', 'prompt': 'consent',
                 'code_challenge_method': 'S256', 'code_challenge': base64.urlsafe_b64encode(
                     hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')}
        return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(query)

    def device_pending(self, state, db):
        row = db.execute('SELECT s.*,d.code_hash,d.browser_id,d.attempts,d.code_secret FROM sessions s '
                         'JOIN device_confirmation d ON d.state=s.state '
                         "WHERE s.state=? AND s.expires>? AND s.status IN ('pending','awaiting_device')",
                         (digest(state), time.time())).fetchone()
        if not row or row['attempts'] >= DEVICE_CODE_ATTEMPTS:
            raise PermissionError('Restart sign-in in Bridge')
        return row

    def begin_device(self, state, browser_token):
        """Bind Google consent to its browser; this never confirms the initiating device."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            session = self.browser.require(browser_token, db=db)
            row = self.device_pending(state, db)
            if row['browser_id'] and row['browser_id'] != session['token_hash']:
                raise PermissionError('Return to the browser that started sign-in')
            if row['status'] != 'pending':
                raise PermissionError('Google sign-in has already completed')
            db.execute('UPDATE device_confirmation SET browser_id=? WHERE state=?',
                       (session['token_hash'], row['state']))
            pending = self.open(row['secret'])
        return self.google_url(state, pending['verifier'], pending['nonce'])

    def device_code(self, state, browser_token):
        """Only the browser that completed Google consent can display this code."""
        with self.db() as db:
            session = self.browser.require(browser_token, db=db)
            row = self.device_pending(state, db)
            if row['status'] != 'awaiting_device' or row['browser_id'] != session['token_hash']:
                raise PermissionError('Finish Google sign-in in the same browser')
            return self.open(row['code_secret'])['code']

    def confirm_device(self, poll, code):
        """The native app proves it received the browser's code, then handoff may begin."""
        normalized = code.replace('-', '').replace(' ', '').upper() if isinstance(code, str) else ''
        rejected = False
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT s.*,d.code_hash,d.attempts FROM sessions s '
                             'JOIN device_confirmation d ON d.state=s.state '
                             'WHERE s.poll=? AND s.expires>?', (digest(poll), time.time())).fetchone()
            if (not row or row['status'] not in ('awaiting_device', 'provisioning', 'ready')
                    or row['attempts'] >= DEVICE_CODE_ATTEMPTS or not row['code_hash']):
                raise PermissionError('Finish Google sign-in and enter its code in Bridge')
            if not hmac.compare_digest(digest(normalized), row['code_hash']):
                db.execute('UPDATE device_confirmation SET attempts=attempts+1 WHERE state=?', (row['state'],))
                if row['attempts'] + 1 >= DEVICE_CODE_ATTEMPTS:
                    db.execute("UPDATE sessions SET status='failed',secret=NULL,result=NULL WHERE state=?", (row['state'],))
                    db.execute('UPDATE device_confirmation SET code_secret=NULL WHERE state=?', (row['state'],))
                rejected = True
            elif row['status'] == 'awaiting_device':
                db.execute("UPDATE sessions SET status='provisioning' WHERE state=?", (row['state'],))
                db.execute('UPDATE device_confirmation SET code_secret=NULL WHERE state=?', (row['state'],))
        if rejected:
            raise PermissionError('Enter the code displayed after Google sign-in')
        return {'status': 'ready' if row['status'] == 'ready' else 'provisioning'}

    def exchange_google(self, code, pending):
        client = self.config['client']
        response = requests.post('https://oauth2.googleapis.com/token', data={
            'code': code, 'client_id': client['client_id'], 'client_secret': client['client_secret'],
            'redirect_uri': self.redirect, 'grant_type': 'authorization_code',
            'code_verifier': pending['verifier']}, timeout=25, allow_redirects=False)
        response.raise_for_status()
        token = response.json()
        identity = id_token.verify_oauth2_token(token['id_token'], Request(), client['client_id'])
        if not hmac.compare_digest(identity.get('nonce', ''), pending['nonce']):
            raise ValueError('Invalid Google nonce')
        return token, identity

    def authorize(self, state, code, *, browser_token=None):
        state_hash = digest(state)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT s.*,b.token_hash AS browser_id FROM sessions s LEFT JOIN browser_oauth b ON b.state=s.state '
                             'WHERE s.state=? AND s.expires>?', (state_hash, time.time())).fetchone()
            if not row or row['status'] != 'pending':
                raise ValueError('Sign-in expired or already used')
            if row['browser_id']:
                if not browser_token or row['browser_id'] != digest(browser_token):
                    raise PermissionError('Return to the browser that started sign-in')
                self.browser.callback(db, browser_token, state_hash)
            if not row['browser_id']:
                confirmation = db.execute('SELECT browser_id FROM device_confirmation WHERE state=?',
                                          (state_hash,)).fetchone()
                if not confirmation or not confirmation['browser_id'] or not browser_token:
                    raise PermissionError('Open the sign-in link from Bridge first')
                session = self.browser.require(browser_token, db=db)
                if confirmation['browser_id'] != session['token_hash']:
                    raise PermissionError('Return to the browser that started sign-in')
            db.execute("UPDATE sessions SET status='exchanging' WHERE state=?", (state_hash,))
        try:
            pending = self.open(row['secret'])
            token, identity = self.exchange_google(code, pending)
            _, sub, email = google_identity(identity)
            scopes = token.get('scope', '').split()
            data_scopes = set(scopes) & set(SCOPES[3:])
            if data_scopes and not token.get('refresh_token'):
                raise ValueError('Offline consent missing; sign in again')
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                account_id, tenant = self.registry.claim(db, identity)
                rotated = (self.browser.authorize(db, browser_token, state_hash, account_id)
                           if row['browser_id'] else None)
                generation = self.registry.next_generation(db, account_id)
                if tenant == self.config['tenant']:
                    # Retain the verified pilot row for rollback to older releases.
                    db.execute('INSERT OR IGNORE INTO account VALUES (1,?,?)', (sub, tenant))
                credentials = None
                if data_scopes:
                    credentials = {'type': 'authorized_user', 'token': token['access_token'],
                                   'refresh_token': token['refresh_token'], 'scopes': scopes,
                                   'token_uri': 'https://oauth2.googleapis.com/token',
                                   'client_id': self.config['client']['client_id'],
                                   'client_secret': self.config['client']['client_secret']}
                handoff = {'request_id': state_hash, 'sub': sub, 'tenant_id': tenant, 'account_id': account_id,
                           'entry': pending.get('entry', 'mac'), 'credentials': credentials, 'email': email,
                           'credential_generation': generation}
                if handoff['entry'] == 'mac':
                    handoff.update(public_key=pending['public_key'], challenge=pending['challenge'])
                next_status = 'provisioning'
                if handoff['entry'] == 'mac':
                    next_status = 'awaiting_device'
                    confirmation_code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))
                    db.execute('UPDATE device_confirmation SET code_hash=?,code_secret=? WHERE state=?',
                               (digest(confirmation_code), self.seal({'code': confirmation_code}), state_hash))
                db.execute('UPDATE sessions SET status=?,secret=? WHERE state=?',
                           (next_status, self.seal(handoff), state_hash))
        except Exception:
            with self.db() as db:
                db.execute("UPDATE sessions SET status='failed',secret=NULL WHERE state=?", (state_hash,))
            raise
        # The callback does not wait for a sleeping/deploying tenant. Status polling
        # retries the durable handoff with the same request ID until it is accepted.
        return {'entry': pending.get('entry', 'mac'), 'browser_token': rotated}

    def deliver(self, handoff):
        origin, control = self.registry.route(handoff['tenant_id'], handoff.get('account_id'))
        response = requests.post(origin + '/cloud/bootstrap', json=handoff,
                                 headers={'Authorization': 'Bearer ' + control}, timeout=25, allow_redirects=False)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError('Invalid tenant handoff result')
        needs_device = handoff.get('entry', 'mac') == 'mac'
        if (result.get('tenant_id') != handoff['tenant_id'] or
                (needs_device and not result.get('pairing_grant')) or
                (not needs_device and result.get('account_connected') is not True)):
            raise ValueError('Unexpected tenant response')
        if ('credential_generation' in handoff
                and result.get('credential_generation') != handoff['credential_generation']):
            raise ValueError('Tenant must support ordered Google handoffs')
        # Only these protocol fields may cross the broker; never relay arbitrary
        # tenant response fields such as credentials into the browser or app.
        safe = {'tenant_id': handoff['tenant_id'], 'instance_url': origin}
        if needs_device:
            safe.update(pairing_grant=result['pairing_grant'], expires_at=result.get('expires_at'),
                        challenge=handoff['challenge'])
        else:
            safe.update(account_connected=True)
        return safe

    def status(self, poll, *, browser_token=None):
        with self.db() as db:
            row = db.execute('SELECT s.*,b.token_hash AS browser_id FROM sessions s LEFT JOIN browser_oauth b ON b.state=s.state '
                             'WHERE s.poll=? AND s.expires>?', (digest(poll), time.time())).fetchone()
        if not row:
            raise PermissionError('Sign-in expired')
        if row['browser_id']:
            session = self.browser.require(browser_token, authenticated=True)
            if session['token_hash'] != row['browser_id']:
                raise PermissionError('Sign-in belongs to another browser')
        if row['status'] == 'provisioning':
            try:
                result = self.deliver(self.open(row['secret']))
            except (requests.RequestException, ValueError):
                return {'status': 'provisioning'}
            with self.db() as db:
                db.execute("UPDATE sessions SET status='ready',result=?,secret=NULL WHERE state=?",
                           (self.seal(result), row['state']))
            return {'status': 'ready', **result}
        if row['status'] == 'ready':
            return {'status': 'ready', **self.open(row['result'])}
        return {'status': row['status']}

    def tenant_status(self, account):
        with self.db() as db:
            tenant = db.execute("SELECT id FROM tenants WHERE account_id=? AND status='active'", (account,)).fetchone()
        if not tenant:
            raise PermissionError('Account is not active')
        origin, control = self.registry.route(tenant['id'], account)
        response = requests.post(origin + '/cloud/status', json={},
                                 headers={'Authorization': 'Bearer ' + control}, timeout=10, allow_redirects=False)
        response.raise_for_status()
        data = response.json()
        if (not isinstance(data, dict) or type(data.get('messaging_active')) is not bool
                or type(data.get('context_connected')) is not bool
                or data.get('first_brief') not in ('waiting', 'composing', 'queued', 'delivered', 'existing')):
            raise ValueError('Invalid tenant readiness')
        return {key: data[key] for key in ('messaging_active', 'context_connected', 'first_brief')}

    def journey(self, token):
        session = self.browser.require(token, authenticated=True)
        handoff = self.status(session['secret']['poll'], browser_token=token)
        with self.db() as db:
            journey = db.execute('SELECT id FROM journeys WHERE account_id=?', (session['account_id'],)).fetchone()
        result = {'journey_id': journey['id'], 'google_connection': handoff['status'],
                  'next_action': 'wait_for_google', 'context_connected': None,
                  'messaging_active': False, 'first_brief': 'waiting'}
        if handoff['status'] != 'ready':
            if handoff['status'] == 'failed':
                result['next_action'] = 'reconnect_google'
            return result
        try:
            result.update(self.tenant_status(session['account_id']))
        except (requests.RequestException, ValueError):
            return {**result, 'next_action': 'retry_status'}
        if result['messaging_active']:
            result['next_action'] = ('ready' if result['first_brief'] in ('delivered', 'existing') else
                                     'wait_for_brief' if result['context_connected'] else 'connect_source')
        else:
            binding = self.linking.current(session['account_id'])
            result['next_action'] = ('wait_for_activation' if binding else
                                     'connect_messages' if self.messaging_line else 'wait_for_messages')
        return result

    def start_link(self, token, csrf):
        session = self.browser.require(token, csrf=csrf, authenticated=True)
        if not self.messaging_line:
            raise ValueError('Messages setup is not available yet')
        intent = self.linking.start(session['account_id'], line=self.messaging_line['id'])
        return {**intent, 'number': self.messaging_line['number']}

    def confirm_link(self, token, csrf, identifier, revision):
        session = self.browser.require(token, csrf=csrf, authenticated=True)
        if not self.messaging_line:
            raise ValueError('Messages setup is not available yet')
        binding = self.linking.confirm(session['account_id'], identifier, revision, line=self.messaging_line['id'])
        return {'binding_id': binding['id'], 'version': binding['version'], 'status': 'verified'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # OAuth callbacks carry codes in their URLs.

    def send(self, code, body, html=False, headers=None, content_type=None):
        raw = body.encode() if html or content_type else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type or ('text/html; charset=utf-8' if html else 'application/json'))
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'self'; form-action 'self' https://accounts.google.com; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def cookie(self):
        raw = self.headers.get('Cookie', '')
        if len(raw) > 4096 or raw.count(COOKIE + '=') != 1:
            return ''
        try:
            cookies = SimpleCookie(raw)
            return cookies[COOKIE].value
        except (CookieError, KeyError):
            return ''

    def cookie_header(self, token):
        return f'{COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL if token else 0}'

    def redirect(self, location, token=None):
        headers = {'Location': location}
        if token is not None:
            headers['Set-Cookie'] = self.cookie_header(token)
        return self.send(303, '', html=True, headers=headers)

    def setup_page(self):
        broker, token, headers = self.server.broker, self.cookie(), {}
        try:
            session = broker.browser.require(token)
        except PermissionError:
            token = broker.browser.create()
            session = broker.browser.require(token)
            headers['Set-Cookie'] = self.cookie_header(token)
        journey, intent = None, None
        if session['account_id']:
            journey = broker.journey(token)
            if broker.messaging_line:
                intent = broker.linking.pending(session['account_id'], line=broker.messaging_line['id'])
                if intent:
                    intent['number'] = broker.messaging_line['number']
        return self.send(200, pages.setup(session, journey, intent), html=True, headers=headers)

    def do_GET(self):  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path in ('/', '/setup'):
            try:
                return self.setup_page()
            except (PermissionError, ValueError):
                return self.send(400, pages.document('Please sign in again', '<p>Your setup is saved. '
                                 '<a href="/">Return to sign-in</a>.</p>'), html=True,
                                 headers={'Set-Cookie': self.cookie_header('')})
        if parsed.path == '/device/connect':
            try:
                query = parse_qs(parsed.query, max_num_fields=2)
                state = query['state'][0]
                broker, token, headers = self.server.broker, self.cookie(), {}
                with broker.db() as db:
                    pending = broker.device_pending(state, db)
                try:
                    broker.browser.require(token)
                except PermissionError:
                    token = broker.browser.create()
                    broker.browser.require(token)
                    headers['Set-Cookie'] = self.cookie_header(token)
                if pending['status'] == 'awaiting_device':
                    return self.send(200, pages.device_confirmation(broker.device_code(state, token)), html=True, headers=headers)
                return self.redirect(broker.begin_device(state, token), token if headers else None)
            except (PermissionError, ValueError, KeyError):
                return self.send(400, pages.document('Start again in Bridge',
                                 '<p>This Mac connection has expired. Open Sotto Bridge and start sign-in again.</p>'), html=True)
        if parsed.path == '/setup.css':
            return self.send(200, pages.CSS, content_type='text/css; charset=utf-8')
        if parsed.path == '/health':
            return self.send(200, {'ok': True})
        if parsed.path == '/v1/signin/status':
            try:
                header = self.headers.get('Authorization', '')
                if not header.startswith('Bearer ') or len(header) > 256:
                    raise PermissionError('Missing sign-in token')
                return self.send(200, self.server.broker.status(header[7:]))
            except PermissionError:
                return self.send(401, {'error': 'Sign-in expired. Start again in Bridge.'})
        if parsed.path == '/v1/journey':
            try:
                return self.send(200, self.server.broker.journey(self.cookie()))
            except PermissionError:
                return self.send(401, {'error': 'Sign in again in this browser.'})
        if parsed.path == '/oauth/google/callback':
            try:
                query = parse_qs(parsed.query, max_num_fields=20)
                result = self.server.broker.authorize(query['state'][0], query['code'][0], browser_token=self.cookie())
            except Exception:  # noqa: BLE001 — never expose OAuth errors/codes in HTTP or logs
                return self.send(400, '<!doctype html><title>Sotto</title><h1>Sign-in did not finish</h1>'
                                 '<p>Return to the browser or Sotto Bridge where you started and try again '
                                 'with your invited account.</p>', html=True)
            if result['entry'] == 'browser':
                return self.redirect('/setup', result['browser_token'])
            return self.send(200, pages.device_confirmation(
                self.server.broker.device_code(query['state'][0], self.cookie())), html=True)
        return self.send(404, {'error': 'Not found'})

    def do_POST(self):  # noqa: N802
        paths = ('/v1/signin/start', '/v1/signin/confirm', '/v1/channel-links', '/v1/channel-links/confirm',
                 '/browser/signin', '/browser/link', '/browser/confirm', '/browser/retry-link', '/browser/logout')
        if self.path not in paths:
            return self.send(404, {'error': 'Not found'})
        try:
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('Invalid request')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('Invalid request')
            raw = self.rfile.read(length)
            is_form = self.path.startswith('/browser/')
            if is_form:
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                    raise ValueError('Invalid form')
                values = parse_qs(raw.decode(), max_num_fields=10)
                if any(len(v) != 1 for v in values.values()):
                    raise ValueError('Duplicate form value')
                body = {k: v[0] for k, v in values.items()}
            else:
                body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError('Invalid request')
            broker, token = self.server.broker, self.cookie()
            if self.path == '/v1/signin/start' and body.get('entry', 'mac') == 'mac':
                if self.headers.get('Origin'):
                    raise PermissionError('Use the Bridge app')
                return self.send(200, broker.start(body))
            if self.path == '/v1/signin/confirm':
                authorization = self.headers.get('Authorization', '')
                if self.headers.get('Origin') or not authorization.startswith('Bearer ') or len(authorization) > 256:
                    raise PermissionError('Enter the browser code in Bridge')
                return self.send(200, broker.confirm_device(authorization[7:], body.get('code')))
            if self.headers.get('Origin') != broker.origin:
                raise PermissionError('Use the Sotto sign-in page')
            csrf = body.get('csrf')
            broker.browser.require(token, csrf=csrf)
            if self.path in ('/v1/signin/start', '/browser/signin'):
                result = broker.start({**body, 'entry': 'browser'}, browser_token=token)
                return self.redirect(result['authorization_url']) if is_form else self.send(200, result)
            if self.path == '/browser/logout':
                broker.browser.logout(token)
                return self.redirect('/', '')
            if self.path == '/browser/retry-link':
                session = broker.browser.require(token, csrf=csrf, authenticated=True)
                broker.linking.cancel(session['account_id'], body['intent_id'])
                broker.start_link(token, csrf)
                return self.redirect('/setup')
            if self.path in ('/v1/channel-links', '/browser/link'):
                result = broker.start_link(token, csrf)
            else:
                result = broker.confirm_link(token, csrf, body['intent_id'], body['proof_revision'])
            return self.redirect('/setup') if is_form else self.send(200, result)
        except PermissionError:
            return self.send(403, {'error': 'Refresh the Sotto page and sign in again.'})
        except (ValueError, KeyError, TypeError):
            return self.send(400, {'error': 'Setup did not finish. Refresh the Sotto page and try again.'})


def main():
    os.umask(0o077)
    config = json.loads(os.environ['SOTTO_ACCOUNT_CONFIG'])
    broker = Broker(Path(os.environ.get('SOTTO_DATA', '/data')) / 'accounts.sqlite', config)
    if sys.argv[1:] == ['--register-tenant']:
        registration = json.load(sys.stdin)
        broker.registry.register(registration['tenant'], registration['origin'],
                                 registration['control_token'], registration['email'])
        print(json.dumps({'registered': True, 'tenant_id': registration['tenant']}))
        return
    if sys.argv[1:]:
        raise SystemExit('Use --register-tenant with a JSON object on stdin, or no arguments to serve')
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), Handler)
    server.broker = broker
    server.serve_forever()


if __name__ == '__main__':
    main()
