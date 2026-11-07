"""Private, owner-scoped receiver capability for explicit chat commands only."""
import hashlib
import hmac
import re
import struct

_PHOTON_FIELDS = ('tenant_id', 'channel', 'account_id', 'owner_id', 'line_key', 'target',
                  'route_mode', 'route_id', 'project_fingerprint')
_TELEGRAM_FIELDS = ('tenant_id', 'channel', 'account_id', 'owner_id', 'line_key', 'target',
                    'route_mode', 'route_id', 'bot_fingerprint')
_HEX64 = re.compile(r'[a-f0-9]{64}\Z')
_PHOTON_DOMAIN = b'sotto-owner-loop-write-v1\x00'
_TELEGRAM_DOMAIN = b'sotto-owner-loop-write-telegram-v1\x00'


def validate_identity(identity):
    if type(identity) is not dict:
        raise ValueError('owner command identity is invalid')
    channel = identity.get('channel')
    fields = _PHOTON_FIELDS if channel == 'photon' else _TELEGRAM_FIELDS if channel == 'telegram' else ()
    if not fields or set(identity) != set(fields):
        raise ValueError('owner command identity is invalid')
    for field in fields:
        value = identity[field]
        if (type(value) is not str or (not value and (field != 'tenant_id' or channel == 'telegram'))
                or len(value) > 512
                or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                       for char in value)):
            raise ValueError('owner command identity is invalid')
        if len(value.encode('utf-8')) > 2048:
            raise ValueError('owner command identity is invalid')
    if channel == 'photon' and (identity['target'] != 'photon'
                                or identity['route_mode'] not in ('fixed', 'first_contact', 'first_contact_shared_project')
                                or _HEX64.fullmatch(identity['project_fingerprint']) is None):
        raise ValueError('owner command identity is invalid')
    if channel == 'photon' and identity['route_mode'] == 'first_contact_shared_project' and identity['route_id'] != 'shared_project':
        raise ValueError('owner command identity is invalid')
    if channel == 'telegram' and (identity['target'] != 'telegram'
                                  or identity['route_mode'] != 'fixed'
                                  or not re.fullmatch(r'[1-9][0-9]{0,15}', identity['owner_id'])
                                  or int(identity['owner_id']) > 9007199254740991
                                  or identity['route_id'] != 'telegram:' + identity['owner_id']
                                  or _HEX64.fullmatch(identity['bot_fingerprint']) is None):
        raise ValueError('owner command identity is invalid')
    return identity


def derive(root, identity):
    validate_identity(identity)
    if type(root) is not str or len(root) < 32 or len(root) > 4096:
        raise ValueError('owner command root is invalid')
    telegram = identity['channel'] == 'telegram'
    chunks = [_TELEGRAM_DOMAIN if telegram else _PHOTON_DOMAIN]
    for field in (_TELEGRAM_FIELDS if telegram else _PHOTON_FIELDS):
        encoded = identity[field].encode('utf-8')
        chunks.extend((struct.pack('>I', len(encoded)), encoded))
    return hmac.new(root.encode('utf-8'), b''.join(chunks), hashlib.sha256).hexdigest()
