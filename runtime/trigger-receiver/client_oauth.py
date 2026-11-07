"""Optional hosted-client authorization for the existing read-only MCP endpoint.

OAuth authorization code + S256 PKCE, public DCR clients, rotating refresh tokens.
Owner identity comes ONLY from the existing dashboard session. No provider tokens,
remote metadata fetches, new data readers, model calls, or source-permission changes.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

import client_mcp

SCOPE = 'sotto.read'
PREFIX = '/client-oauth'
COOKIE = '__Host-sotto_oauth_return'
RESOURCE_METADATA = '/.well-known/oauth-protected-resource/client-mcp'
PATHS = {RESOURCE_METADATA, '/.well-known/oauth-protected-resource',
         '/.well-known/oauth-authorization-server', *[PREFIX + '/' + p for p in
         ('register', 'authorize', 'token', 'revoke', 'connections')]}
DAY = 86400
TOKEN_TTL = 3600
MAX_TOKEN_ROWS = 4096
CONNECTION_TTL = 30 * DAY


def origin():
    if os.environ.get('SOTTO_MCP_OAUTH') == '0' or os.environ.get('SOTTO_DASHBOARD') == '0':
        return None
    value = os.environ.get('SOTTO_MCP_OAUTH_ORIGIN', '')
    if not value and os.environ.get('RAILWAY_PUBLIC_DOMAIN'):
        value = 'https://' + os.environ['RAILWAY_PUBLIC_DOMAIN']
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or not value.isascii()
            or not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname)
            or any(c.isspace() for c in value)):
        return None
    return 'https://' + parsed.hostname.lower() + (':' + str(port) if port and port != 443 else '')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def secret(prefix=''):
    return prefix + secrets.token_urlsafe(32)


def page(title, content):
    """Same local fonts and palette as the owner dashboard; no scripts or remote assets."""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{html.escape(title)}</title><link rel="stylesheet" href="/static/app.css">'
            '<link rel="stylesheet" href="/static/client-oauth.css"></head>'
            '<body class="oauth-page"><main class="oauth-card">'
            '<div class="oauth-brand" aria-label="Sotto">Sotto<span>.</span></div>'
            + content + '</main></body></html>')


def reply(h, status, value=None, *, headers=(), markup=None, form_origin=None):
    raw = markup.encode() if markup is not None else json.dumps(value).encode()
    h.send_response(status)
    for name, val in [('Content-Type', 'text/html; charset=utf-8' if markup is not None else 'application/json'),
                      ('Content-Length', str(len(raw))), ('Cache-Control', 'no-store'),
                      ('Pragma', 'no-cache'), ('Referrer-Policy', 'same-origin' if markup is not None else 'no-referrer'),
                      ('X-Content-Type-Options', 'nosniff'),
                      ('Content-Security-Policy', "default-src 'none'; style-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'" +
                       (' ' + form_origin if form_origin else '') + "; frame-ancestors 'none'"),
                      *headers]:
        h.send_header(name, val)
    h.end_headers()
    h.wfile.write(raw)


def challenge(h):
    base = origin()
    if not base:
        return h._send(401, {'error': 'client access unavailable or expired'})
    return reply(h, 401, {'error': 'invalid_token'}, headers=[('WWW-Authenticate',
        f'Bearer resource_metadata="{base}{RESOURCE_METADATA}", scope="{SCOPE}"' +
        (', error="invalid_token"' if h.headers.get('Authorization') else ''))])


@contextmanager
def store(root, *, readonly=False):
    # Reuse the descriptor-checked private grant directory. Refuse existing symlinks,
    # foreign owners or public files. SQLite transactions serialize all token consumption.
    with client_mcp.grant_directory(root, create=not readonly) as directory:
        fd = os.open('oauth.sqlite3', (os.O_RDONLY if readonly else os.O_RDWR | os.O_CREAT) | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
                raise ValueError('unsafe authorization store')
        finally:
            os.close(fd)
        path = os.path.join(root, 'config', 'mcp-clients', 'oauth.sqlite3')
        db = sqlite3.connect(path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if readonly:
                db.execute('PRAGMA query_only=ON')
                yield db
                return
            db.execute('PRAGMA foreign_keys=ON')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY, name TEXT, redirects TEXT, expires INTEGER);
                CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, body TEXT, expires INTEGER, session TEXT);
                CREATE TABLE IF NOT EXISTS connections(id TEXT PRIMARY KEY, client TEXT, session TEXT,
                    resource TEXT, expires INTEGER, tools TEXT, revoked INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS codes(hash TEXT PRIMARY KEY, connection TEXT, redirect TEXT,
                    challenge TEXT, expires INTEGER);
                CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, connection TEXT, kind TEXT,
                    expires INTEGER, used INTEGER DEFAULT 0);
            ''')
            db.execute('BEGIN IMMEDIATE')
            now = int(time.time())
            db.execute('DELETE FROM requests WHERE expires <= ?', (now,))
            db.execute('DELETE FROM codes WHERE expires <= ?', (now,))
            db.execute('DELETE FROM tokens WHERE expires <= ? OR connection IN (SELECT id FROM connections WHERE revoked=1 OR expires <= ?)', (now, now))
            db.execute('DELETE FROM codes WHERE connection IN (SELECT id FROM connections WHERE revoked=1 OR expires <= ?)', (now,))
            db.execute('DELETE FROM connections WHERE revoked=1 OR expires <= ?', (now,))
            db.execute('DELETE FROM clients WHERE expires <= ? AND id NOT IN (SELECT client FROM connections)', (now,))
            yield db
            db.commit()
        finally:
            db.close()


def parameters(raw):
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=25)
    if len(dict(pairs)) != len(pairs):
        raise ValueError('duplicate parameter')
    return dict(pairs)


def body(h, *, json_body=False):
    if h.headers.get('Transfer-Encoding'):
        raise ValueError('transfer encoding')
    size = int(h.headers.get('Content-Length', '0'))
    if not 0 < size <= 16384:
        raise ValueError('body size')
    expected = 'application/json' if json_body else 'application/x-www-form-urlencoded'
    if h.headers.get('Content-Type', '').split(';')[0] != expected:
        raise ValueError('content type')
    raw = h.rfile.read(size).decode('utf-8')
    result = client_mcp.decode(raw) if json_body else parameters(raw)
    if type(result) is not dict:
        raise ValueError('object required')
    return result


def valid_redirect(uri):
    if type(uri) is not str or len(uri) > 2048 or not uri.isascii():
        return False
    p = urlsplit(uri)
    return (p.scheme == 'https' and bool(p.hostname) and not p.username and not p.password
            and re.fullmatch(r'[A-Za-z0-9.-]+', p.hostname) and (p.port is None or 1 <= p.port <= 65535)
            and '#' not in uri and not any(ord(c) < 33 or c == '\\' for c in uri)
            and not {'code', 'state', 'iss', 'error'} & dict(parse_qsl(p.query, keep_blank_values=True)).keys())


def register(db, data):
    redirects = data.get('redirect_uris')
    if (not isinstance(redirects, list) or not 1 <= len(redirects) <= 5
            or not all(valid_redirect(u) for u in redirects)
            or data.get('token_endpoint_auth_method', 'none') != 'none'
            or data.get('grant_types', ['authorization_code']) not in
               (['authorization_code'], ['authorization_code', 'refresh_token'], ['refresh_token', 'authorization_code'])
            or data.get('response_types', ['code']) != ['code']
            or data.get('scope', SCOPE) != SCOPE):
        raise ValueError('unsupported client metadata')
    name = data.get('client_name', 'External client')
    client_mcp.validate(name, client_mcp.string(80))
    if db.execute('SELECT count(*) FROM clients').fetchone()[0] >= 256:
        db.execute('''DELETE FROM clients WHERE id IN (SELECT id FROM clients
            WHERE id NOT IN (SELECT client FROM connections)
            AND id NOT IN (SELECT json_extract(body, '$.client_id') FROM requests WHERE session IS NOT NULL)
            ORDER BY expires LIMIT 1)''')
        if db.execute('SELECT count(*) FROM clients').fetchone()[0] >= 256:
            raise ValueError('registration capacity')
    cid = secret('sotto_oauth_client_')
    db.execute('INSERT INTO clients VALUES(?,?,?,?)',
               (cid, name, json.dumps(redirects), int(time.time()) + 365 * DAY))
    return {'client_id': cid, 'client_name': name, 'redirect_uris': redirects,
            'token_endpoint_auth_method': 'none', 'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'], 'scope': SCOPE}


def request_authorization(db, data, base):
    client = db.execute('SELECT * FROM clients WHERE id=? AND expires>?',
                        (data.get('client_id', ''), time.time())).fetchone()
    if (not client or data.get('redirect_uri') not in json.loads(client['redirects'])
            or data.get('response_type') != 'code' or data.get('code_challenge_method') != 'S256'
            or not re.fullmatch(r'[A-Za-z0-9_-]{43}', data.get('code_challenge', ''))
            or data.get('resource') != base + client_mcp.PATH
            or data.get('scope', SCOPE) != SCOPE
            or not 1 <= len(data.get('state', '')) <= 1024):
        raise ValueError('invalid authorization request')
    if db.execute('SELECT count(*) FROM requests').fetchone()[0] >= 256:
        db.execute('DELETE FROM requests WHERE id IN (SELECT id FROM requests ORDER BY session IS NOT NULL, expires LIMIT 1)')
    rid = secret()
    allowed = ('client_id', 'redirect_uri', 'code_challenge', 'resource', 'state')
    db.execute('INSERT INTO requests VALUES(?,?,?,NULL)',
               (rid, json.dumps({k: data[k] for k in allowed}), int(time.time()) + 600))
    return rid


def issue(db, connection):
    if db.execute('SELECT count(*) FROM tokens WHERE connection=?', (connection['id'],)).fetchone()[0] >= MAX_TOKEN_ROWS:
        db.execute('UPDATE connections SET revoked=1 WHERE id=?', (connection['id'],))
        return None
    now = int(time.time())
    access, refresh = secret('sotto_oauth_access_'), secret('sotto_oauth_refresh_')
    for token, kind, expiry in ((access, 'access', min(now + TOKEN_TTL, connection['expires'])),
                                (refresh, 'refresh', connection['expires'])):
        db.execute('INSERT INTO tokens VALUES(?,?,?,?,0)', (digest(token), connection['id'], kind, expiry))
    return {'access_token': access, 'token_type': 'Bearer', 'expires_in': min(TOKEN_TTL, connection['expires'] - now),
            'refresh_token': refresh, 'scope': SCOPE}


def exchange(db, data, base, session_lookup):
    now = time.time()
    kind = data.get('grant_type')
    if kind == 'authorization_code':
        row = db.execute('SELECT * FROM codes WHERE hash=?', (digest(data.get('code', '')),)).fetchone()
    elif kind == 'refresh_token':
        row = db.execute("SELECT * FROM tokens WHERE hash=? AND kind='refresh'",
                         (digest(data.get('refresh_token', '')),)).fetchone()
    else:
        return None
    if not row or row['expires'] <= now:
        return None
    conn = db.execute('SELECT * FROM connections WHERE id=?', (row['connection'],)).fetchone()
    if (not conn or conn['revoked'] or conn['expires'] <= now or conn['client'] != data.get('client_id')
            or conn['resource'] != base + client_mcp.PATH or data.get('resource', conn['resource']) != conn['resource']
            or data.get('scope', SCOPE) != SCOPE or not session_lookup(conn['session'])):
        return None
    if kind == 'authorization_code':
        verifier = data.get('code_verifier', '')
        if not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier):
            return None
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        if data.get('redirect_uri') != row['redirect'] or not hmac.compare_digest(challenge, row['challenge']):
            return None
        db.execute('DELETE FROM codes WHERE hash=?', (row['hash'],))
    else:
        if row['used']:
            db.execute('UPDATE connections SET revoked=1 WHERE id=?', (conn['id'],))
            return None
        db.execute('UPDATE tokens SET used=1 WHERE hash=?', (row['hash'],))
    return issue(db, conn)


def authenticate(root, token, session_lookup):
    base = origin()
    if not base or not re.fullmatch(r'sotto_oauth_access_[A-Za-z0-9_-]{43}', token):
        return None
    try:
        with store(root, readonly=True) as db:
            row = db.execute('''SELECT c.* FROM tokens t JOIN connections c ON c.id=t.connection
                WHERE t.hash=? AND t.kind='access' AND t.expires>? AND c.expires>?
                AND c.revoked=0 AND c.resource=?''',
                (digest(token), time.time(), time.time(), base + client_mcp.PATH)).fetchone()
        if row and session_lookup(row['session']):
            names = json.loads(row['tools'])
            if type(names) is list and names and all(isinstance(name, str) and name in client_mcp.TOOLS for name in names):
                return {'client_id': row['id'], 'tools': names, 'expires_at': row['expires']}
    except (OSError, ValueError, sqlite3.Error):
        pass
    return None


def login_return(h):
    # Fixed local destination only; cookie is an expiring request id, never authority.
    for part in h.headers.get('Cookie', '').split(';'):
        key, _, val = part.strip().partition('=')
        if key == COOKIE and val == 'connections':
            return PREFIX + '/connections'
        if key == COOKIE and re.fullmatch(r'[A-Za-z0-9_-]{43}', val):
            return PREFIX + '/authorize?request=' + val
    return '/app'


def handle(h, method, root, dashboard):
    h.close_connection = True
    base = origin()
    if not base:
        if method == 'GET' and urlsplit(h.path).path == PREFIX + '/connections':
            return reply(h, 200, markup='<html lang="en"><title>Sotto connections</title>'
                '<h1>Other assistants are not enabled here</h1>'
                '<p>You can keep using Sotto normally. Your host must enable secure connector sign-in before you can connect another assistant.</p></html>')
        return reply(h, 404, {'error': 'not_found'})
    try:
        return _handle(h, method, root, dashboard, base)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return reply(h, 400, {'error': 'invalid_request'})
    except (OSError, sqlite3.Error):
        return reply(h, 503, {'error': 'temporarily_unavailable'})


def _handle(h, method, root, dashboard, base):
    path = urlsplit(h.path).path
    if method == 'GET' and path in (RESOURCE_METADATA, '/.well-known/oauth-protected-resource'):
        return reply(h, 200, {'resource': base + client_mcp.PATH, 'authorization_servers': [base],
                              'scopes_supported': [SCOPE], 'bearer_methods_supported': ['header']})
    if method == 'GET' and path == '/.well-known/oauth-authorization-server':
        return reply(h, 200, {'issuer': base, 'authorization_endpoint': base + PREFIX + '/authorize',
            'token_endpoint': base + PREFIX + '/token', 'registration_endpoint': base + PREFIX + '/register',
            'revocation_endpoint': base + PREFIX + '/revoke', 'scopes_supported': [SCOPE],
            'response_types_supported': ['code'], 'grant_types_supported': ['authorization_code', 'refresh_token'],
            'token_endpoint_auth_methods_supported': ['none'], 'code_challenge_methods_supported': ['S256'],
            'authorization_response_iss_parameter_supported': True})
    incoming = body(h, json_body=path == PREFIX + '/register') if method == 'POST' else None
    # Owner/broker checks may involve I/O: do them before acquiring the write lock.
    rec = dashboard._session_record(h) if path in (PREFIX + '/authorize', PREFIX + '/connections') else None
    checked_session = None
    if path == PREFIX + '/token' and incoming:
        with store(root, readonly=True) as lookup:
            key = incoming.get('code') if incoming.get('grant_type') == 'authorization_code' else incoming.get('refresh_token')
            table = 'codes' if incoming.get('grant_type') == 'authorization_code' else 'tokens'
            row = lookup.execute('SELECT c.session FROM ' + table + ' t JOIN connections c ON c.id=t.connection WHERE t.hash=?',
                                 (digest(key or ''),)).fetchone()
        if row and dashboard.session_by_digest(row['session']):
            checked_session = row['session']
    with store(root) as db:
        if path == PREFIX + '/register' and method == 'POST':
            result = register(db, incoming)
            db.commit()
            return reply(h, 201, result)
        if path == PREFIX + '/token' and method == 'POST':
            result = exchange(db, incoming, base, lambda key: key == checked_session)
            db.commit()
            return reply(h, 200 if result else 400, result or {'error': 'invalid_grant'})
        if path == PREFIX + '/revoke' and method == 'POST':
            data = incoming
            db.execute('''UPDATE connections SET revoked=1 WHERE client=? AND id IN
                (SELECT connection FROM tokens WHERE hash=?)''', (data.get('client_id', ''), digest(data.get('token', ''))))
            db.commit()
            return reply(h, 200, {})
        if path == PREFIX + '/authorize' and method == 'GET':
            data = parameters(urlsplit(h.path).query)
            rid = data['request'] if set(data) == {'request'} else request_authorization(db, data, base)
            pending = db.execute('SELECT * FROM requests WHERE id=?', (rid,)).fetchone()
            if not pending:
                raise ValueError('expired request')
            if not rec:
                db.commit()
                return reply(h, 302, {}, headers=[('Location', '/app'), ('Set-Cookie',
                    f'{COOKIE}={rid}; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=600')])
            session = digest(dashboard._cookie(h, 'sotto_session'))
            db.execute('UPDATE requests SET session=? WHERE id=?', (session, rid))
            data = json.loads(pending['body'])
            client = db.execute('SELECT * FROM clients WHERE id=?', (data['client_id'],)).fetchone()
            if not client:
                raise ValueError('client registration expired')
            label = html.escape(client['name'])
            callback = urlsplit(data['redirect_uri'])
            host = html.escape(data['redirect_uri'])
            form_origin = callback.scheme + '://' + callback.netloc
            db.commit()
            return reply(h, 200, form_origin=form_origin, headers=[('Set-Cookie',
                f'{COOKIE}=; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=0')], markup=page('Connect Sotto', f'''<h1>Connect {label} to Sotto?</h1>
                <p class="oauth-intro">Bring your context into this assistant. You choose what Sotto shares.</p>
                <div class="oauth-destination"><span>Return address</span><strong>{host}</strong>
                <small>The app calls itself “{label}”. Continue only if you recognize the app and this address.</small></div>
                <section aria-labelledby="read-heading"><h2 id="read-heading">Read-only access</h2>
                <ul><li>Your permitted email, calendar and Google Contacts</li>
                <li>Connected Granola meeting notes and transcripts</li>
                <li>Saved people, companies and commitments</li>
                <li>Mac sources you’ve chosen to share, including messages, calls, contacts,
                notes, reminders, browsing history, file metadata and Screen Time, while your Mac is online</li></ul></section>
                <p class="oauth-boundary">It cannot send messages, change tasks or run Sotto’s agent.</p>
                <form method="post" class="oauth-actions"><input type="hidden" name="request" value="{html.escape(rid)}">
                <input type="hidden" name="csrf" value="{html.escape(rec['csrf'])}">
                <button class="oauth-primary" name="decision" value="allow">Allow read-only access</button>
                <button class="oauth-secondary" name="decision" value="deny">Cancel</button></form>
                <p class="oauth-footnote">Access lasts up to 30 days and ends sooner if you sign out of Sotto
                or this Sotto sign-in expires. <a href="/client-oauth/connections">Disconnect anytime.</a></p>'''))
        if not rec and path == PREFIX + '/connections' and method == 'GET':
            return reply(h, 302, {}, headers=[('Location', '/app'), ('Set-Cookie',
                f'{COOKIE}=connections; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=600')])
        if not rec:
            return reply(h, 401, {'error': 'owner_login_required'})
        if path == PREFIX + '/authorize' and method == 'POST':
            data = incoming
            if (h.headers.get('Origin') != base or not hmac.compare_digest(data.get('csrf', ''), rec['csrf'])):
                return reply(h, 403, {'error': 'invalid_consent'})
            pending = db.execute('SELECT * FROM requests WHERE id=? AND session=?',
                (data.get('request', ''), digest(dashboard._cookie(h, 'sotto_session')))).fetchone()
            if not pending or data.get('decision') not in ('allow', 'deny'):
                raise ValueError('expired consent')
            request = json.loads(pending['body'])
            if not db.execute('SELECT id FROM clients WHERE id=?', (request['client_id'],)).fetchone():
                raise ValueError('client registration expired')
            db.execute('DELETE FROM requests WHERE id=?', (pending['id'],))
            result = {'state': request['state'], 'iss': base}
            if data['decision'] == 'deny':
                result['error'] = 'access_denied'
            else:
                cid, code = secret(), secret()
                db.execute('UPDATE connections SET revoked=1 WHERE client=?', (request['client_id'],))
                if db.execute('SELECT count(*) FROM connections WHERE revoked=0').fetchone()[0] >= 128:
                    raise ValueError('connection capacity')
                db.execute('UPDATE clients SET expires=? WHERE id=?',
                    (int(time.time()) + 365 * DAY, request['client_id']))
                db.execute('INSERT INTO connections VALUES(?,?,?,?,?,?,0)',
                    (cid, request['client_id'], pending['session'], request['resource'],
                     int(time.time()) + CONNECTION_TTL, json.dumps(sorted(client_mcp.TOOLS))))
                db.execute('INSERT INTO codes VALUES(?,?,?,?,?)',
                    (digest(code), cid, request['redirect_uri'], request['code_challenge'], int(time.time()) + 120))
                result['code'] = code
            db.commit()
            uri = request['redirect_uri']
            return reply(h, 302, {}, headers=[('Location', uri + ('&' if '?' in uri else '?') + urlencode(result)),
                ('Set-Cookie', f'{COOKIE}=; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=0')])
        if path == PREFIX + '/connections':
            if method == 'POST':
                data = incoming
                if h.headers.get('Origin') != base or not hmac.compare_digest(data.get('csrf', ''), rec['csrf']):
                    return reply(h, 403, {'error': 'invalid_consent'})
                db.execute('UPDATE connections SET revoked=1 WHERE id=?', (data.get('connection', ''),))
                db.commit()
                return reply(h, 303, {}, headers=[('Location', PREFIX + '/connections')])
            if method == 'GET':
                rows = db.execute('''SELECT c.id, COALESCE(cl.name, 'Disconnected registration') AS name FROM connections c LEFT JOIN clients cl ON cl.id=c.client
                    WHERE c.revoked=0 AND c.expires>?''', (time.time(),)).fetchall()
                forms = ''.join(f'<form method="post"><p>{html.escape(row["name"])}</p>'
                    f'<input type="hidden" name="connection" value="{row["id"]}">'
                    f'<input type="hidden" name="csrf" value="{html.escape(rec["csrf"])}">'
                    '<button>Disconnect</button></form>' for row in rows)
                return reply(h, 200, headers=[('Set-Cookie',
                    f'{COOKIE}=; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=0')], markup=
                    '<!doctype html><html lang="en"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<title>Connect other assistants · Sotto</title><h1>Connect other assistants</h1>'
                    '<p>Optional: use your Sotto context in an assistant that supports remote MCP sign-in.</p>'
                    '<ol><li>In your assistant, add a custom connector with this server URL:</li></ol>'
                    f'<p><code>{html.escape(base + client_mcp.PATH)}</code></p>'
                    '<p>Choose sign-in (OAuth), then approve access in Sotto. No API key or client secret is needed. '
                    'If your app does not offer custom connectors, this option is not supported there yet.</p>'
                    '<p>Only your selected sources are readable. Local history needs your Mac online. '
                    'This does not allow sending, changing tasks or running Sotto’s proactive agent. '
                    'The assistant receives retrieved excerpts, so its own data policies also apply.</p>'
                    '<p>No skill pack is required. Your assistant can use these tools directly; '
                    'Sotto’s scheduled briefs and workflows continue separately.</p>'
                    '<p>Connections expire within 30 days, or sooner if this Sotto sign-in expires or you sign out.</p>'
                    '<h2>Connected assistants</h2>' + (forms or '<p>No active connections. Nothing is shared until you approve.</p>')
                    + '<p><a href="/app">Not now · Back to Sotto</a></p></html>')
        return reply(h, 405, {'error': 'method_not_allowed'})
