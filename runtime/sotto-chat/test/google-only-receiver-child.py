"""Run the actual receiver handler with synthetic private Google lineage and fake HTTPS."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
from http.server import ThreadingHTTPServer

source = Path(sys.argv[1]).resolve()
fixture = Path(sys.argv[2]).resolve()
data = fixture / 'data'
home = fixture / 'home'
data.mkdir(mode=0o700)
home.mkdir(mode=0o700)
sys.path.insert(0, str(source / 'runtime/trigger-receiver'))
sys.path.insert(0, str(source / 'sotto-chief-of-staff/_shared/lib'))
os.environ['SOTTO_DEPLOYMENT_MODE'] = 'selfhost'
os.environ['SOTTO_MCP_TOKEN'] = 'synthetic-root-bearer-for-offline-google-contract'
os.environ['SOTTO_DATA'] = str(data)
os.environ['SOTTO_HOME'] = str(home)
os.environ['SOTTO_GOOGLE_FIXTURE_ROOT'] = str(fixture)
os.environ['SOTTO_USER_EMAIL'] = 'owner@example.com'

import receiver as rec  # noqa: E402

rec.DATA = str(data)
rec.RELAY = type('OfflineBridge', (), {'bridge_connected': lambda self: False})()
reader = source / 'runtime/sotto-chat/test/google-only-receiver-reader.py'
rec._google_api_py = lambda: str(reader)
token = home / 'google_token.json'
token.write_text(json.dumps({'type': 'authorized_user', 'refresh_token': 'synthetic',
    'client_id': 'synthetic-client', 'client_secret': 'synthetic-secret',
    'token_uri': 'https://oauth2.googleapis.com/token',
    'scopes': ['https://www.googleapis.com/auth/gmail.readonly',
               'https://www.googleapis.com/auth/calendar.readonly'],
    'sotto_source_generation': 'a' * 32}))
token.chmod(0o600)
authority = rec._runtime_source_authority()
if not authority['complete'] or authority['google_sources'] != ['calendar', 'gmail']:
    raise RuntimeError('Synthetic Google authority is not complete')
server = ThreadingHTTPServer(('127.0.0.1', 0), rec.Handler)
print(json.dumps({'ready': True, 'url': f'http://127.0.0.1:{server.server_port}/mcp',
                  'bearer': rec.MCP_TOKEN, 'revision': authority['revision']}), flush=True)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
try:
    server.serve_forever()
finally:
    server.server_close()
