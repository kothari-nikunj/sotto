"""Two-proof iMessage binding, independent of Mac enrollment and model behavior.

Only a trusted transport calls observe(). Only an authenticated account can start,
confirm or revoke. A text challenge alone never creates a route. No messages are sent.
"""
import base64
import hashlib
import json
import re
import secrets
import time

LINK_TTL = 600
PROOF_ATTEMPTS = 5


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def sender(kind, value):
    if not isinstance(value, str):
        raise ValueError('Invalid sender')
    if kind == 'phone' and re.fullmatch(r'\+[1-9][0-9]{6,14}', value):
        return value
    if kind == 'email' and re.fullmatch(r'[^\s<>@]{1,200}@[^\s<>@]{1,100}', value):
        return value.lower()
    raise ValueError('An exact provider phone or email handle is required')


class Linking:
    def __init__(self, registry, clock=time.time):
        self.registry, self.db, self.clock = registry, registry.db, clock
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS link_intents ('
                       'id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES cloud_accounts(id), '
                       'challenge_hash TEXT NOT NULL UNIQUE, secret BLOB NOT NULL, expires REAL NOT NULL, '
                       'status TEXT NOT NULL, candidate TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS channel_bindings ('
                       'id TEXT PRIMARY KEY, account_id TEXT NOT NULL UNIQUE REFERENCES cloud_accounts(id), '
                       'line TEXT NOT NULL, kind TEXT NOT NULL, sender TEXT NOT NULL, conversation TEXT NOT NULL, '
                       'version INTEGER NOT NULL, active INTEGER NOT NULL)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS active_imessage_sender '
                       'ON channel_bindings(line,kind,sender) WHERE active=1')
            db.execute('CREATE TABLE IF NOT EXISTS proof_attempts ('
                       'sender_hash TEXT PRIMARY KEY, started REAL NOT NULL, attempts INTEGER NOT NULL)')
            if 'expected_line' not in {r['name'] for r in db.execute('PRAGMA table_info(link_intents)')}:
                db.execute('ALTER TABLE link_intents ADD COLUMN expected_line TEXT')
                # Old unscoped challenges must not authorize a different Sotto line.
                db.execute("UPDATE link_intents SET status='revoked',secret=? WHERE status IN ('pending','observed')",
                           (self.registry.seal({}),))

    def _active(self, db, account):
        row = db.execute("SELECT id FROM tenants WHERE account_id=? AND status='active'", (account,)).fetchone()
        if not row:
            raise PermissionError('Account is not active')
        return row['id']

    def start(self, account, *, line):
        if not isinstance(line, str) or not 1 <= len(line) <= 255:
            raise ValueError('A verified provider line is required')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._active(db, account)
            db.execute("UPDATE link_intents SET status='expired',secret=? WHERE expires<=? AND status IN ('pending','observed')",
                       (self.registry.seal({}), self.clock()))
            db.execute("UPDATE link_intents SET status='revoked',secret=? WHERE account_id=? "
                       "AND expected_line!=? AND status IN ('pending','observed')", (self.registry.seal({}), account, line))
            prior = db.execute("SELECT * FROM link_intents WHERE account_id=? AND expected_line=? AND status IN ('pending','observed') "
                               'AND expires>? ORDER BY expires DESC LIMIT 1', (account, line, self.clock())).fetchone()
            if prior:
                return self._view(prior)
            token = base64.b32encode(secrets.token_bytes(8)).decode()[:12]
            identifier, expires = secrets.token_urlsafe(24), self.clock() + LINK_TTL
            db.execute('INSERT INTO link_intents VALUES (?,?,?,?,?,?,NULL,?)',
                       (identifier, account, digest(token), self.registry.seal({'challenge': token}), expires, 'pending', line))
            row = db.execute('SELECT * FROM link_intents WHERE id=?', (identifier,)).fetchone()
            return self._view(row)

    def _view(self, row):
        result = {'intent_id': row['id'], 'expires_at': row['expires'], 'status': row['status'],
                  'message': 'Connect Sotto ' + self.registry.unseal(row['secret'])['challenge']}
        if row['candidate']:
            result.update(candidate=json.loads(row['candidate']), proof_revision=digest(row['candidate']))
        return result

    def pending(self, account, *, line):
        with self.db() as db:
            self._active(db, account)
            row = db.execute("SELECT * FROM link_intents WHERE account_id=? AND expected_line=? "
                             "AND status IN ('pending','observed') AND expires>? ORDER BY expires DESC LIMIT 1",
                             (account, line, self.clock())).fetchone()
        return self._view(row) if row else None

    def observe(self, *, line, kind, handle, conversation, text, is_group):
        """Trusted transport event only. Caller has already verified the provider."""
        if is_group is not False or not isinstance(text, str):
            return False
        match = re.fullmatch(r'Connect Sotto ([A-Z2-7]{12})', text.strip(), re.IGNORECASE)
        if not match:
            return False
        handle = sender(kind, handle)
        if any(not isinstance(v, str) or not 1 <= len(v) <= 255 for v in (line, conversation)):
            raise ValueError('Invalid provider route')
        candidate = json.dumps({'line': line, 'kind': kind, 'sender': handle, 'conversation': conversation}, sort_keys=True)
        sender_hash = digest(json.dumps([line, kind, handle]))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self.clock()
            db.execute('DELETE FROM proof_attempts WHERE started<=?', (now - LINK_TTL,))
            attempt = db.execute('SELECT attempts FROM proof_attempts WHERE sender_hash=?', (sender_hash,)).fetchone()
            if attempt and attempt['attempts'] >= PROOF_ATTEMPTS:
                return False
            db.execute('INSERT INTO proof_attempts VALUES (?,?,1) ON CONFLICT(sender_hash) '
                       'DO UPDATE SET attempts=attempts+1', (sender_hash, now))
            row = db.execute("SELECT * FROM link_intents WHERE challenge_hash=? AND expires>? "
                             "AND status IN ('pending','observed')", (digest(match[1].upper()), now)).fetchone()
            if not row or row['expected_line'] != line:
                return False
            self._active(db, row['account_id'])
            if row['candidate']:
                return row['candidate'] == candidate  # A different sender cannot replace the displayed proof.
            db.execute("UPDATE link_intents SET candidate=?,status='observed' WHERE id=?", (candidate, row['id']))
            return True

    def confirm(self, account, identifier, proof_revision, *, line=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._active(db, account)
            row = db.execute('SELECT * FROM link_intents WHERE id=? AND account_id=? AND expires>?',
                             (identifier, account, self.clock())).fetchone()
            if (not row or (line is not None and row['expected_line'] != line)
                    or row['status'] not in ('observed', 'confirmed') or not row['candidate']
                    or digest(row['candidate']) != proof_revision):
                raise PermissionError('Confirm the current observed sender')
            candidate = json.loads(row['candidate'])
            existing = db.execute('SELECT * FROM channel_bindings WHERE account_id=?', (account,)).fetchone()
            if row['status'] == 'confirmed':
                # A revoked binding cannot be resurrected by replaying an old confirmation.
                if not existing or not existing['active'] or any(existing[k] != v for k, v in candidate.items()):
                    raise PermissionError('Connection changed; start again')
                return dict(existing)
            conflict = db.execute('SELECT account_id FROM channel_bindings WHERE line=? AND kind=? AND sender=? '
                                  'AND active=1', (candidate['line'], candidate['kind'], candidate['sender'])).fetchone()
            if conflict and conflict['account_id'] != account:
                raise PermissionError('Sender already connected to another account')
            identifier = existing['id'] if existing else secrets.token_urlsafe(24)
            version = existing['version'] + 1 if existing else 1
            db.execute('INSERT INTO channel_bindings VALUES (?,?,?,?,?,?,?,1) ON CONFLICT(account_id) DO UPDATE SET '
                       'line=excluded.line,kind=excluded.kind,sender=excluded.sender,conversation=excluded.conversation,'
                       'version=excluded.version,active=1', (identifier, account, candidate['line'], candidate['kind'],
                        candidate['sender'], candidate['conversation'], version))
            db.execute("UPDATE link_intents SET status='confirmed',secret=? WHERE id=?", (self.registry.seal({}), row['id']))
            return dict(db.execute('SELECT * FROM channel_bindings WHERE id=?', (identifier,)).fetchone())

    def current(self, account):
        with self.db() as db:
            self._active(db, account)
            row = db.execute('SELECT * FROM channel_bindings WHERE account_id=? AND active=1', (account,)).fetchone()
        return dict(row) if row else None

    def cancel(self, account, identifier):
        """Reject a displayed candidate without changing an already active route."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._active(db, account)
            result = db.execute("UPDATE link_intents SET status='revoked',secret=? WHERE id=? AND account_id=? "
                                "AND status IN ('pending','observed')", (self.registry.seal({}), identifier, account))
            if result.rowcount != 1:
                raise PermissionError('Challenge is no longer pending')

    def revoke(self, account):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._active(db, account)
            db.execute('UPDATE channel_bindings SET active=0,version=version+1 WHERE account_id=? AND active=1', (account,))
            db.execute("UPDATE link_intents SET status='revoked',secret=? WHERE account_id=?", (self.registry.seal({}), account))

    def resolve(self, *, line, kind, handle):
        handle = sender(kind, handle)
        with self.db() as db:
            row = db.execute('SELECT b.*,t.id AS tenant_id FROM channel_bindings b JOIN tenants t '
                             "ON t.account_id=b.account_id WHERE b.active=1 AND t.status='active' "
                             'AND b.line=? AND b.kind=? AND b.sender=?', (line, kind, handle)).fetchone()
        return dict(row) if row else None
