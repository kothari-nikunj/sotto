"""Account/source bootstrap with optional device-bound Bridge enrollment.

The broker can install Google consent and issue a pairing handoff. Only a device
holding the corresponding P-256 private key can redeem it. Handoffs are one logical
operation, retryable with the same device for ten minutes after an ambiguous response.
This service adopts an existing tenant; it does not allocate customer infrastructure.
"""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import managed

try:
    from control_vault import get as control_token
except ModuleNotFoundError:
    control_token = lambda: os.environ.get('SOTTO_CONTROL_TOKEN', '')

TTL = 600


def device_key(raw):
    key = serialization.load_der_public_key(base64.b64decode(raw, validate=True))
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError('Invalid device key')
    return key


class Pairing:
    def __init__(self, root, adapter):
        self.root, self.adapter = Path(root), adapter
        self.path = self.root / 'config/cloud-pairing.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS account (slot INTEGER PRIMARY KEY CHECK(slot=1), sub TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, public_key TEXT, challenge TEXT, '
                       'expires REAL, status TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, '
                       'generation INTEGER NOT NULL, enabled INTEGER NOT NULL, created REAL NOT NULL, '
                       'revoked_before REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS bootstraps (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, '
                       'expires REAL NOT NULL, result TEXT NOT NULL)')
            if 'generation' not in {r['name'] for r in db.execute('PRAGMA table_info(bootstraps)')}:
                db.execute('ALTER TABLE bootstraps ADD COLUMN generation INTEGER NOT NULL DEFAULT 0')
            if 'applied' not in {r['name'] for r in db.execute('PRAGMA table_info(bootstraps)')}:
                db.execute('ALTER TABLE bootstraps ADD COLUMN applied INTEGER NOT NULL DEFAULT 1')
        self.path.chmod(0o600)

    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def bootstrap(self, body):
        tenant = os.environ['SOTTO_TENANT_ID']
        if body['tenant_id'] != tenant or not re.fullmatch('[a-f0-9]{64}', body['request_id']):
            raise ValueError('Invalid tenant handoff')
        entry = body.get('entry', 'mac')
        if entry not in ('mac', 'browser'):
            raise ValueError('Invalid sign-in entry')
        if entry == 'mac':
            device_key(body['public_key'])
            if not isinstance(body['challenge'], str) or not 32 <= len(body['challenge']) <= 128:
                raise ValueError('Invalid device challenge')
        elif 'public_key' in body or 'challenge' in body:
            raise ValueError('Browser sign-in cannot enroll a device')
        if not isinstance(body['sub'], str) or not 1 <= len(body['sub']) <= 255:
            raise ValueError('Invalid account')
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        generation = body.get('credential_generation', 0)
        if type(generation) is not int or not 0 <= generation < 2**63 - 1:
            raise ValueError('Invalid credential generation')
        grant = hmac.new(control_token().encode(),
                         ('pair:' + body['request_id']).encode(), hashlib.sha256).hexdigest()
        def result_for(expires):
            result = {'tenant_id': tenant, 'account_connected': True}
            if entry == 'mac':
                result = {'tenant_id': tenant, 'pairing_grant': body['request_id'] + '.' + grant,
                          'expires_at': expires}
            if 'credential_generation' in body:
                result['credential_generation'] = generation
            return result

        # Commit the generation before touching Google's credential file. If the process dies
        # after the file write, an older delayed request must still be unable to restore scopes.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            account = db.execute('SELECT sub FROM account WHERE slot=1').fetchone()
            if account and account['sub'] != body['sub']:
                raise PermissionError('Account cannot be changed by pairing')
            existing = db.execute('SELECT * FROM bootstraps WHERE id=?', (body['request_id'],)).fetchone()
            if existing:
                if existing['fingerprint'] != fingerprint or existing['expires'] <= time.time():
                    raise PermissionError('Handoff expired or changed')
                if existing['applied']:
                    return json.loads(existing['result'])
            else:
                legacy = db.execute('SELECT * FROM grants WHERE id=?', (body['request_id'],)).fetchone()
                if legacy:
                    if (entry != 'mac' or legacy['public_key'] != body['public_key']
                            or legacy['challenge'] != body['challenge'] or legacy['expires'] <= time.time()):
                        raise PermissionError('Handoff expired or changed')
                    result = result_for(legacy['expires'])
                    db.execute('INSERT INTO bootstraps VALUES (?,?,?,?,?,1)',
                               (body['request_id'], fingerprint, legacy['expires'], json.dumps(result), generation))
                    return result  # Old accepted grants never reinstall Google credentials.
                accepted = db.execute('SELECT COALESCE(MAX(generation),0) FROM bootstraps').fetchone()[0]
                if generation < accepted or (generation > 0 and generation == accepted):
                    raise PermissionError('A newer Google connection has already been accepted')
                db.execute('INSERT OR IGNORE INTO account VALUES (1,?)', (body['sub'],))
                db.execute('INSERT INTO bootstraps VALUES (?,?,?,?,?,0)',
                           (body['request_id'], fingerprint, time.time() + TTL, '{}', generation))

        # Serialize credential installation with reservations from other requests. A superseded
        # pending operation stays unapplied; retrying it cannot overwrite the newer generation.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            pending = db.execute('SELECT * FROM bootstraps WHERE id=?', (body['request_id'],)).fetchone()
            if pending['applied']:
                return json.loads(pending['result'])
            accepted = db.execute('SELECT COALESCE(MAX(generation),0) FROM bootstraps').fetchone()[0]
            if generation < accepted or pending['expires'] <= time.time():
                raise PermissionError('Handoff expired or superseded by a newer Google connection')
            scopes = self.adapter.install_cloud_google(body['credentials'])
            managed.record_google_consent(self.root, scopes)
            if entry == 'mac':
                db.execute('INSERT INTO grants VALUES (?,?,?,?,?)', (body['request_id'], body['public_key'],
                           body['challenge'], pending['expires'], 'issued'))
            result = result_for(pending['expires'])
            db.execute('UPDATE bootstraps SET result=?,applied=1 WHERE id=?',
                       (json.dumps(result), body['request_id']))
        return result

    def redeem(self, body):
        identifier, proof = body['pairing_grant'].split('.', 1)
        expected = hmac.new(control_token().encode(),
                            ('pair:' + identifier).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, proof):
            raise PermissionError('Invalid pairing grant')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM grants WHERE id=? AND expires>?', (identifier, time.time())).fetchone()
            if not row:
                raise PermissionError('Pairing grant expired')
            device_key(row['public_key']).verify(base64.b64decode(body['signature'], validate=True),
                ('sotto-cloud-pair\n' + body['pairing_grant'] + '\n' + row['challenge']).encode(), ec.ECDSA(hashes.SHA256()))
            device_id = hashlib.sha256(base64.b64decode(row['public_key'])).hexdigest()
            device = db.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
            if device and row['expires'] - TTL <= device['revoked_before']:
                raise PermissionError('This device grant was revoked; sign in again')
            generation = (device['generation'] + int(not device['enabled'])) if device else 1
            credential = hmac.new(control_token().encode(),
                f'bridge-device:{device_id}:{generation}'.encode(), hashlib.sha256).hexdigest()
            token_hash = hashlib.sha256(credential.encode()).hexdigest()
            db.execute('INSERT INTO devices VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                       'token_hash=excluded.token_hash,generation=excluded.generation,enabled=1',
                       (device_id, token_hash, generation, 1, time.time(), 0))
            db.execute("UPDATE grants SET status='redeemed' WHERE id=?", (identifier,))
        # A managed device receives its own Bridge capability, never the shared operator
        # setup credential. Google sign-in itself does not need that browser setup code.
        return {'tenant_id': os.environ['SOTTO_TENANT_ID'], 'bridge_token': credential, 'device_id': device_id,
                'setup_code': '',
                'sotto_number': os.environ['SOTTO_IMESSAGE_NUMBER'],
                'allowed_sources': list(managed.BRIDGE_SOURCE_FIELDS)}

    def authenticated(self, credential):
        if not isinstance(credential, str) or not credential:
            return False
        digest = hashlib.sha256(credential.encode()).hexdigest()
        with self.db() as db:
            devices = db.execute('SELECT token_hash,enabled FROM devices').fetchall()
        if not devices:
            # Adopt the already connected pilot without an implicit disconnect. Issuing the
            # first device capability permanently closes this compatibility path.
            root = os.environ.get('BRIDGE_TOKEN', '')
            return bool(root) and hmac.compare_digest(root, credential)
        return any(row['enabled'] and hmac.compare_digest(row['token_hash'], digest) for row in devices)

    def devices(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT id,enabled,created,revoked_before FROM devices ORDER BY created')]

    def revoke(self, device_id):
        if not isinstance(device_id, str) or not re.fullmatch('[a-f0-9]{64}', device_id):
            raise ValueError('Invalid device identity')
        with self.db() as db:
            result = db.execute('UPDATE devices SET enabled=0,revoked_before=? WHERE id=? AND enabled=1',
                                (time.time(), device_id))
            return result.rowcount > 0


def bridge_authenticated(root, credential):
    """Managed Bridge endpoint authorization, including migration from the old pilot bearer."""
    try:
        return Pairing(root, None).authenticated(credential)
    except (OSError, sqlite3.Error, ValueError):
        return False


def handle(handler, path, root, adapter=None, *, adapter_factory=None):
    """Called before legacy receiver auth; no managed control credential, no route."""
    control = control_token()
    if not managed.enabled() or not control or path not in ('/cloud/bootstrap', '/cloud/pair', '/cloud/consent', '/cloud/devices', '/cloud/status'):
        return handler._send(404, {'error': 'Not found'})
    if path in ('/cloud/bootstrap', '/cloud/devices') and not handler._authed(control):
        return handler._send(401, {'error': 'Unauthorized'})
    if path in ('/cloud/consent', '/cloud/status'):
        authorization = handler.headers.get('Authorization', '')
        control_status = path == '/cloud/status' and handler._authed(control)
        if not control_status and (not authorization.startswith('Bearer ') or not bridge_authenticated(root, authorization[7:])):
            return handler._send(401, {'error': 'Unauthorized'})
    try:
        if handler.headers.get('Origin') or handler.headers.get('Transfer-Encoding'):
            raise ValueError('Invalid request')
        length = int(handler.headers.get('Content-Length', '0'))
        if not 0 < length <= 32768:
            raise ValueError('Invalid request')
        body = json.loads(handler.rfile.read(length))
        if path == '/cloud/status':
            return handler._send(200, managed.connection_status(root))
        if path == '/cloud/consent':
            managed.record_bridge_consent(root, body['enabled'], body['connected'])
            return handler._send(200, {'ok': True})
        if path == '/cloud/bootstrap' and adapter_factory is not None:
            adapter = adapter_factory()
        pairing = Pairing(root, adapter)
        if path == '/cloud/devices':
            if body.get('operation') == 'list':
                return handler._send(200, {'devices': pairing.devices(), 'scope': 'bridge_access'})
            if body.get('operation') != 'revoke':
                raise ValueError('Invalid device operation')
            revoked = pairing.revoke(body.get('device_id'))
            return handler._send(200, {'revoked': revoked, 'scope': 'bridge_access'})
        result = pairing.bootstrap(body) if path == '/cloud/bootstrap' else pairing.redeem(body)
        return handler._send(200, result)
    except Exception:
        # Never echo Google credentials, signatures or grant bytes into logs/responses.
        return handler._send(400, {'error': 'Cloud connection did not finish. Sign in again in Bridge.'})
