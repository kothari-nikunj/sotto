"""Native Bridge readiness and explicit setup-link capability for a prepared self-host.

The supervisor and channel linker remain configuration authorities. Readiness
contains no secrets; a separate owner action resolves the existing setup code.
"""
from __future__ import annotations

import json
import os
import re


def response(*, runtime, google_connected, context_connected, messaging_active,
             first_brief, number='', telegram_bot=''):
    channel = runtime['channel']
    if channel not in ('photon', 'telegram') or type(runtime.get('model_configured')) is not bool:
        raise ValueError('invalid readiness')
    if any(type(value) is not bool for value in
           (google_connected, context_connected, messaging_active)):
        raise ValueError('invalid readiness')
    if first_brief not in ('waiting', 'composing', 'queued', 'retrying', 'model_held',
                           'delivered', 'existing'):
        raise ValueError('unknown first brief state')
    result = {'version': 1, 'deployment': 'self_host', 'channel': channel,
              'configuration_mode': 'read_only',
              'model_configured': runtime['model_configured'],
              'google_connected': google_connected, 'context_connected': context_connected,
              'messaging_active': messaging_active, 'first_brief': first_brief}
    if channel == 'photon' and isinstance(number, str) and re.fullmatch(r'\+[1-9][0-9]{7,14}', number):
        result['sotto_number'] = number
    if (channel == 'telegram' and isinstance(telegram_bot, str)
            and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', telegram_bot)):
        result['telegram_chat_url'] = 'https://t.me/' + telegram_bot
    return result


def handle(handler, *, status, setup_code=None):
    setup_link = handler.path == '/selfhost/setup-link' and setup_code is not None
    if (handler.path != '/selfhost/status' and not setup_link
            or os.environ.get('SOTTO_DEPLOYMENT_MODE') != 'self_host'):
        return handler._send(404, {'error': 'not found'})
    if not handler._bridge_authed():
        return handler._send(401, {'error': 'unauthorized'})
    try:
        # A native bearer request, never browser cookies, URL credentials or a
        # cross-origin page. Bound and validate before any readiness work.
        if ('Origin' in handler.headers or 'Cookie' in handler.headers
                or 'Transfer-Encoding' in handler.headers
                or handler.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                != 'application/json'):
            raise ValueError('invalid request')
        lengths = handler.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            raise ValueError('invalid length')
        length = int(lengths[0])
        if not 0 < length <= 1024:
            raise ValueError('invalid length')
        raw = handler.rfile.read(length)
        if len(raw) != length or json.loads(raw) != {}:
            raise ValueError('invalid body')
    except (ValueError, TypeError, UnicodeError, OSError):
        return handler._send(400, {'error': 'invalid self-host status request'})
    try:
        readiness = status()  # Verify current loaded runtime authority before issuing any capability.
        if setup_link:
            code = setup_code()
            if not isinstance(code, str) or re.fullmatch(r'[A-Za-z0-9_-]{12,128}', code) is None:
                raise ValueError('invalid setup capability')
            # This explicit native action returns no host or provider secret. Ordinary
            # status polling never resolves or exposes the browser setup capability.
            return handler._send(200, {'version': 1, 'setup_code': code})
        return handler._send(200, readiness)
    except Exception:  # The upstream error can contain source paths or credentials.
        return handler._send(503, {'error': 'self-host readiness unavailable'})
