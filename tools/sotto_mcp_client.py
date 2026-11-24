#!/usr/bin/env python3
"""Small stdio transport for Sotto's opt-in read-only MCP endpoint. No local data readers.

Set SOTTO_CLIENT_MCP_URL and SOTTO_CLIENT_MCP_CREDENTIAL_FILE in the client configuration.
Never put the internal Bridge token in either. Python standard library only; no model calls.
"""
import json
import http.client
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

MAX_INPUT = 16 * 1024
# The receiver may escape Unicode and carries both text and structuredContent. Allow the
# full envelope of a bounded 128-KiB UTF-8 tool result, including emoji's worst-case expansion.
MAX_OUTPUT = 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def endpoint(value):
    url = urllib.parse.urlsplit(value)
    if (url.scheme not in ('https', 'http') or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment or url.path != '/client-mcp'
            or url.scheme == 'http' and url.hostname not in ('127.0.0.1', 'localhost', '::1')):
        raise ValueError('Use the exact HTTPS client-mcp endpoint (HTTP is allowed only on loopback).')
    return value


def credential(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError('Credential file must be a private regular file.')
        value = json.loads(os.read(fd, 4097))
    finally:
        os.close(fd)
    token = value.get('token') if isinstance(value, dict) else None
    if not isinstance(token, str) or not re.fullmatch(r'sotto_client_[a-f0-9]{24}\.[A-Za-z0-9_-]{43}', token):
        raise ValueError('Expected a dedicated Sotto client credential.')
    return token


def forward(url, token, payload):
    request = urllib.request.Request(url, data=payload, headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'})
    # No proxy environment inheritance or redirect credential forwarding.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=60) as response:
        if response.status == 202:
            return None
        if response.status != 200 or response.headers.get_content_type() != 'application/json':
            raise ValueError('Unexpected MCP response')
        raw = response.read(MAX_OUTPUT + 1)
    if len(raw) > MAX_OUTPUT:
        raise ValueError('MCP response exceeds bound')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('jsonrpc') != '2.0':
        raise ValueError('Invalid MCP response')
    return value


def main():
    try:
        url = endpoint(os.environ.get('SOTTO_CLIENT_MCP_URL', ''))
        path = os.environ['SOTTO_CLIENT_MCP_CREDENTIAL_FILE']
        credential(path)
    except (KeyError, ValueError, OSError):
        print('Sotto MCP needs its endpoint and a private client credential file.', file=sys.stderr)
        return 1
    while True:
        line = sys.stdin.buffer.readline(MAX_INPUT + 1)
        if not line:
            return 0
        if len(line) > MAX_INPUT:
            print('Sotto MCP request exceeds its bound.', file=sys.stderr)
            return 1
        rid, notification = None, False
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError('Expected one request')
            rid = request.get('id')
            notification = 'id' not in request
            result = forward(url, credential(path), line)
            if result is not None and result.get('id') != rid:
                raise ValueError('MCP response ID mismatch')
        except (ValueError, OSError, urllib.error.URLError, http.client.HTTPException):
            # No server body/URL/token or private source text is written to diagnostics.
            result = {'jsonrpc': '2.0', 'id': rid, 'error': {
                'code': -32000, 'message': 'Sotto read connection unavailable. No result was established.'}}
        if result is not None and not notification:
            print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
