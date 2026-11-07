"""Authenticated read of the receiver's live source authority; no retained grant inference."""
import hashlib
import hmac
import json
import os
import urllib.request

def current_authority():
    key = os.environ.get('BRIDGE_TOKEN') or os.environ.get('SOTTO_MCP_TOKEN') or os.environ.get('SOTTO_TRIGGER_TOKEN')
    if not key:
        raise RuntimeError('Ask source authority unavailable')
    token = hmac.new(key.encode(), b'sotto-mcp', hashlib.sha256).hexdigest()
    port = os.environ.get('PORT', os.environ.get('SOTTO_TRIGGER_PORT', '8787'))
    request = urllib.request.Request('http://127.0.0.1:' + port + '/runtime/source-authority',
        data=b'{}', headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)

