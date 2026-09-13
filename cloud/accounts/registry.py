"""Invite-only account ownership and pre-created tenant routes. No fleet credentials.

Only the operator registers tenant origins. Google issuer/subject owns the account;
email admits a new account once, and can never transfer an existing tenant.
"""
import re
import secrets
import time
from urllib.parse import urlsplit

GOOGLE_ISSUER = 'https://accounts.google.com'


def https_base(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('Expected an HTTPS origin')
    return value.rstrip('/')


def google_identity(identity):
    # Google uses either spelling of its issuer. No other issuer is accepted.
    issuer = identity.get('iss')
    sub, email = identity.get('sub'), identity.get('email')
    if (issuer not in (GOOGLE_ISSUER, 'accounts.google.com') or not isinstance(sub, str)
            or not 1 <= len(sub) <= 255 or identity.get('email_verified') is not True
            or not isinstance(email, str) or not 3 <= len(email) <= 320 or '@' not in email):
        raise ValueError('Invalid verified Google identity')
    return GOOGLE_ISSUER, sub, email.strip().lower()


class Registry:
    def __init__(self, connect, seal, unseal):
        self.db, self.seal, self.unseal = connect, seal, unseal
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS cloud_accounts ('
                       'id TEXT PRIMARY KEY, issuer TEXT NOT NULL, sub TEXT NOT NULL, '
                       'email TEXT NOT NULL, created REAL NOT NULL, UNIQUE(issuer,sub))')
            db.execute('CREATE TABLE IF NOT EXISTS tenants ('
                       'id TEXT PRIMARY KEY, origin TEXT NOT NULL UNIQUE, secret BLOB NOT NULL, '
                       'account_id TEXT UNIQUE REFERENCES cloud_accounts(id), '
                       "status TEXT NOT NULL CHECK(status IN ('reserved','active','suspended')))")
            db.execute('CREATE TABLE IF NOT EXISTS admissions ('
                       'email TEXT PRIMARY KEY, tenant_id TEXT NOT NULL UNIQUE REFERENCES tenants(id))')
            if 'generation' not in {r['name'] for r in db.execute('PRAGMA table_info(cloud_accounts)')}:
                db.execute('ALTER TABLE cloud_accounts ADD COLUMN generation INTEGER NOT NULL DEFAULT 0')

    def register(self, tenant, origin, control_token, email):
        """Operator-only command; callers cannot supply routes through sign-in."""
        origin = https_base(origin)
        if (not isinstance(tenant, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', tenant)
                or not isinstance(control_token, str) or not control_token
                or not isinstance(email, str) or not 3 <= len(email) <= 320 or '@' not in email):
            raise ValueError('Invalid tenant registration')
        email = email.strip().lower()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tenants WHERE id=?', (tenant,)).fetchone()
            admission = db.execute('SELECT email FROM admissions WHERE tenant_id=?', (tenant,)).fetchone()
            if admission and admission['email'] != email:
                raise ValueError('An admission cannot transfer tenant ownership')
            if row:
                # Route/credential rotation is explicit operator configuration. Never
                # reset active/suspended state or its immutable account ownership.
                db.execute('UPDATE tenants SET origin=?,secret=? WHERE id=?',
                           (origin, self.seal({'control_token': control_token}), tenant))
            else:
                db.execute('INSERT INTO tenants VALUES (?,?,?,NULL,?)',
                           (tenant, origin, self.seal({'control_token': control_token}), 'reserved'))
            db.execute('INSERT OR IGNORE INTO admissions VALUES (?,?)', (email, tenant))
            current = db.execute('SELECT tenant_id FROM admissions WHERE email=?', (email,)).fetchone()
            if not current or current['tenant_id'] != tenant:
                raise ValueError('This email already has an admitted tenant')

    def adopt_legacy(self, sub, tenant, email):
        """Migrate the already-verified pilot before accepting a new callback."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tenants WHERE id=?', (tenant,)).fetchone()
            if not row:
                raise ValueError('Legacy tenant must be registered first')
            owner = db.execute('SELECT * FROM cloud_accounts WHERE issuer=? AND sub=?',
                               (GOOGLE_ISSUER, sub)).fetchone()
            if row['account_id']:
                if not owner or owner['id'] != row['account_id']:
                    raise ValueError('Legacy account cannot replace tenant owner')
                return
            identifier = owner['id'] if owner else secrets.token_urlsafe(24)
            if not owner:
                db.execute('INSERT INTO cloud_accounts(id,issuer,sub,email,created) VALUES (?,?,?,?,?)',
                           (identifier, GOOGLE_ISSUER, sub, email.lower(), time.time()))
            db.execute("UPDATE tenants SET account_id=?,status='active' WHERE id=?", (identifier, tenant))

    def claim(self, db, identity):
        """Called inside the callback transaction with the durable handoff write."""
        issuer, sub, email = google_identity(identity)
        owner = db.execute('SELECT id FROM cloud_accounts WHERE issuer=? AND sub=?', (issuer, sub)).fetchone()
        if owner:
            row = db.execute('SELECT * FROM tenants WHERE account_id=?', (owner['id'],)).fetchone()
            if not row or row['status'] != 'active':
                raise ValueError('Account is not active')
            db.execute('UPDATE cloud_accounts SET email=? WHERE id=?', (email, owner['id']))
            return owner['id'], row['id']
        row = db.execute('SELECT t.* FROM tenants t JOIN admissions a ON a.tenant_id=t.id '
                         'WHERE a.email=?', (email,)).fetchone()
        if not row or row['status'] != 'reserved' or row['account_id'] is not None:
            raise ValueError('This account has not been invited')
        identifier = secrets.token_urlsafe(24)
        db.execute('INSERT INTO cloud_accounts(id,issuer,sub,email,created) VALUES (?,?,?,?,?)', (identifier, issuer, sub, email, time.time()))
        db.execute("UPDATE tenants SET account_id=?,status='active' WHERE id=?", (identifier, row['id']))
        return identifier, row['id']

    def next_generation(self, db, account):
        db.execute('UPDATE cloud_accounts SET generation=generation+1 WHERE id=?', (account,))
        return db.execute('SELECT generation FROM cloud_accounts WHERE id=?', (account,)).fetchone()[0]

    def route(self, tenant, account_id=None):
        with self.db() as db:
            row = db.execute('SELECT * FROM tenants WHERE id=?', (tenant,)).fetchone()
        if (not row or row['status'] != 'active'
                or (account_id is not None and row['account_id'] != account_id)):
            raise ValueError('Tenant route is not active')
        return row['origin'], self.unseal(row['secret'])['control_token']
