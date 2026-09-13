"""Short-lived browser account sessions, bound to the initiating OAuth cookie.

The cookie is never an API response field, polling bearer or URL parameter. Account
identity is attached only inside the verified Google callback transaction. Journeys
survive browser expiry; reauthentication resumes the same account's journey.
"""
import hashlib
import hmac
import secrets
import time

SESSION_TTL = 900
COOKIE = '__Host-sotto_session'
_UNSET = object()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class BrowserSessions:
    def __init__(self, registry):
        self.registry, self.db = registry, registry.db
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS browser_sessions ('
                       'token_hash TEXT PRIMARY KEY, expires REAL NOT NULL, secret BLOB NOT NULL, '
                       'account_id TEXT REFERENCES cloud_accounts(id), oauth_state TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS browser_oauth ('
                       'state TEXT PRIMARY KEY REFERENCES sessions(state) ON DELETE CASCADE, token_hash TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS journeys ('
                       'id TEXT PRIMARY KEY, account_id TEXT NOT NULL UNIQUE REFERENCES cloud_accounts(id))')

    def create(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM browser_sessions WHERE expires<=?', (time.time(),))
            anonymous = db.execute('SELECT COUNT(*) FROM browser_sessions WHERE account_id IS NULL').fetchone()[0]
            if anonymous >= 500:
                db.execute('DELETE FROM browser_sessions WHERE token_hash IN ('
                           'SELECT token_hash FROM browser_sessions b WHERE account_id IS NULL '
                           'AND NOT EXISTS (SELECT 1 FROM browser_oauth o JOIN sessions s ON s.state=o.state '
                           "WHERE o.token_hash=b.token_hash AND s.status IN ('pending','exchanging','provisioning')) "
                           'AND NOT EXISTS (SELECT 1 FROM device_confirmation d JOIN sessions s ON s.state=d.state '
                           "WHERE d.browser_id=b.token_hash AND s.status IN ('pending','exchanging','awaiting_device','provisioning')) "
                           'ORDER BY expires,token_hash LIMIT ?)', (anonymous - 499,))
                if db.execute('SELECT COUNT(*) FROM browser_sessions WHERE account_id IS NULL').fetchone()[0] >= 500:
                    raise ValueError('Sign-in capacity reached; try again shortly')
            db.execute('INSERT INTO browser_sessions VALUES (?,?,?,NULL,NULL)',
                       (digest(token), time.time() + SESSION_TTL, self.registry.seal({'csrf': csrf})))
        return token

    def require(self, token, *, csrf=_UNSET, authenticated=False, db=None):
        if db is None:
            with self.db() as connection:
                return self.require(token, csrf=csrf, authenticated=authenticated, db=connection)
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise PermissionError('Sign in again')
        row = db.execute('SELECT * FROM browser_sessions WHERE token_hash=? AND expires>?',
                         (digest(token), time.time())).fetchone()
        if not row:
            raise PermissionError('Sign in again')
        secret = self.registry.unseal(row['secret'])
        if csrf is not _UNSET and (not isinstance(csrf, str) or not hmac.compare_digest(csrf, secret['csrf'])):
            raise PermissionError('Refresh this page and try again')
        if authenticated and not row['account_id']:
            raise PermissionError('Sign in first')
        if row['account_id']:
            active = db.execute("SELECT id FROM tenants WHERE account_id=? AND status='active'",
                                (row['account_id'],)).fetchone()
            if not active:
                raise PermissionError('Account is not active')
        return {**dict(row), 'secret': secret}

    def bind(self, db, token, state, poll):
        row = self.require(token, db=db)
        db.execute('INSERT INTO browser_oauth VALUES (?,?)', (state, row['token_hash']))
        db.execute('UPDATE browser_sessions SET oauth_state=?,secret=? WHERE token_hash=?',
                   (state, self.registry.seal({**row['secret'], 'poll': poll}), row['token_hash']))

    def callback(self, db, token, state):
        row = self.require(token, db=db)
        if row['oauth_state'] != state:
            raise PermissionError('Return to the browser that started sign-in')
        return row

    def authorize(self, db, token, state, account):
        row = self.callback(db, token, state)
        if row['account_id'] and row['account_id'] != account:
            raise PermissionError('Sign out before connecting a different Google account')
        db.execute('INSERT OR IGNORE INTO journeys VALUES (?,?)', (secrets.token_urlsafe(24), account))
        rotated = secrets.token_urlsafe(32)
        secret = {**row['secret'], 'csrf': secrets.token_urlsafe(32)}
        db.execute('INSERT INTO browser_sessions VALUES (?,?,?,?,?)',
                   (digest(rotated), time.time() + SESSION_TTL, self.registry.seal(secret), account, state))
        db.execute('DELETE FROM browser_sessions WHERE token_hash=?', (row['token_hash'],))
        # Browser OAuth polling remains cookie-bound after session rotation.
        db.execute('UPDATE browser_oauth SET token_hash=? WHERE state=?', (digest(rotated), state))
        return rotated

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM browser_sessions WHERE token_hash=?', (digest(token),))
