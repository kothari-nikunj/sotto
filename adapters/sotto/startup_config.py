"""Offline, explicit-input Sotto chat config renderer. This is not a boot selector.

The caller attests that the supplied manifest came from its trusted deployment and
pairing stores. This module checks shape and cross-field identity only; it never
discovers credentials, contacts a provider, or grants source access.
"""
import argparse
from collections.abc import Mapping
import ctypes
import hashlib
import hmac
import importlib.util
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

MAX_MANIFEST_BYTES = 64 * 1024
MANAGED_PHOTON_CAPABILITIES = (
    "Managed Photon capabilities: use only tools offered for this turn and sources currently granted by the owner. "
    "An owner must send the literal `/loop add <text>` command to add an open loop. To close one, read "
    "`owner_open_loops` for its actual `anchor_key` and ask the owner to send `/loop close <anchor_key>`; "
    "never invent an anchor or claim a plain-language request changed a loop. "
    "The owner can schedule a one-time reminder by sending `/reminder at <ISO8601-with-offset> <text>`, "
    "see pending reminders with `/reminder list`, or cancel one with `/reminder cancel <int_id>`. "
    "For a clear one-time reminder requested in an attended owner message, use `owner_reminder_context` "
    "for the configured timezone and current time, then `owner_reminder_create` when those tools are offered. "
    "Private reminder creation needs no second approval; clarify ambiguous day or time before writing. "
    "Use an explicit offset-bearing due time and self-contained reminder text. These tools do not authorize "
    "conditional checks, recurring jobs or external actions. If unavailable, offer the literal command "
    "without claiming the reminder was created. Scheduled delivery uses the existing 15-minute "
    "heartbeat, quiet-hours, and delivery checks, so do not promise exact-minute delivery. "
    "The owner can pause nudges with `/quiet for <Nh|Nm>` (up to 24 hours) and resume them with "
    "`/quiet off`; these commands do not change briefs or ordinary quiet hours. "
    "`/stop` cancels the active turn but cannot undo an effect already contacted; `/new` starts fresh chat "
    "context but does not undo task changes or resolve uncertain sends. The receiver owns five built-in "
    "procedures on fixed schedules; eligibility and delivery checks still apply. You cannot create or change "
    "personal routines or other schedules, change other preferences, write "
    "Gmail or Calendar, or send through Bridge. You may draft text in chat, but cannot create an external "
    "draft. Claim an effect only after a confirmed receipt; otherwise say it is unavailable or uncertain."
)
SELF_HOST_PHOTON_CAPABILITIES = MANAGED_PHOTON_CAPABILITIES.replace(
    'Managed Photon capabilities:', 'Self-host Photon capabilities:', 1)
SELF_HOST_TELEGRAM_CAPABILITIES = MANAGED_PHOTON_CAPABILITIES.replace(
    'Managed Photon capabilities:', 'Self-host Telegram capabilities:', 1)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_USER = re.compile(r"[1-9][0-9]{0,15}\Z")
_PHONE = re.compile(r"\+[1-9][0-9]{7,14}\Z")
_FINGERPRINT = re.compile(r"[a-f0-9]{64}\Z")
# preferences._durable_preferences_replace's mkstemp file, orphaned if the receiver is killed
# before its rename; it is never the published store, so receiver restaging removes it.
_ORPHAN_RECEIVER_TEMP = re.compile(r"\.preferences-[A-Za-z0-9_]{1,64}\Z")
# This stager's own per-file replacement temp, orphaned if a restart restage is killed mid-way.
_ORPHAN_STAGE_TEMP = re.compile(r"\.sotto-stage-[A-Za-z0-9_]{1,64}\Z")


def _owner_loop_capability():
    """Load the sole stdlib derivation from the packaged skill tree."""
    for path in (Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff' / '_shared' / 'lib' / 'owner_loop_capability.py',
                 Path('/app/sotto-skills/_shared/lib/owner_loop_capability.py')):
        if path.is_file():
            spec = importlib.util.spec_from_file_location('sotto_owner_loop_capability', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise ManifestError('owner loop capability implementation is missing')


class ManifestError(ValueError):
    pass


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ManifestError('manifest shape is invalid')
    return value


def _utf16_units(value):
    if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ManifestError('manifest text is invalid')
    return sum(2 if ord(c) > 0xFFFF else 1 for c in value)


def _text(value, limit=512):
    if (not isinstance(value, str) or not 0 < _utf16_units(value) <= limit
            or value.strip() != value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ManifestError('manifest text is invalid')
    return value


def _instructions(value):
    if (not isinstance(value, str) or not 0 < _utf16_units(value) <= 32000
            or not value.strip()
            or '\x00' in value or any(ord(c) < 32 and c not in '\r\n\t' for c in value)):
        raise ManifestError('instructions are invalid')
    return value


def _path(value):
    value = _text(value, 4096)
    if not Path(value).is_absolute():
        raise ManifestError('paths must be absolute')
    return value


def _port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ManifestError('explicit nonzero loopback port required')
    return value


def _duplicate_free(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError('duplicate manifest key')
        result[key] = value
    return result


def _read_private_regular(path, limit):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ManifestError('input must be a private regular owner file')
        if metadata.st_size > limit:
            raise ManifestError('input is too large')
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(fd, min(8192, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > limit:
            raise ManifestError('input is too large')
        return bytes(raw)
    finally:
        os.close(fd)


def read_private_manifest(path):
    """Read one bounded regular 0600 owner file, without following its final symlink."""
    raw = _read_private_regular(path, MAX_MANIFEST_BYTES)
    def reject_constant(_value):
        raise ManifestError('nonfinite JSON number')

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ManifestError('nonfinite JSON number')
        return number

    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_duplicate_free,
                          parse_constant=reject_constant, parse_float=finite_float)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ManifestError('manifest JSON is invalid') from exc


def _proxy_origin(value):
    value = _text(value, 2048)
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError as exc:
        raise ManifestError('proxy port is invalid') from exc
    if url.username or url.password or url.path not in ('', '/') or url.query or url.fragment or not url.hostname:
        raise ManifestError('proxy must be one origin')
    if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1')):
        raise ManifestError('proxy must use HTTPS or loopback HTTP')
    if (url.hostname not in ('127.0.0.1', '::1', 'localhost')
            and not all(re.fullmatch(r'[a-z][a-z0-9-]*[a-z0-9]|[a-z]', label)
                        for label in url.hostname.split('.'))):
        raise ManifestError('proxy hostname is invalid')
    if port in (443 if url.scheme == 'https' else 80,):
        raise ManifestError('proxy default port must be omitted')
    hostname = f'[{url.hostname}]' if ':' in url.hostname else url.hostname
    canonical = f'{url.scheme}://{hostname}' + (f':{port}' if port is not None else '')
    if value.rstrip('/') != canonical:
        raise ManifestError('proxy origin is invalid')
    return canonical


def _payload_bytes(payload):
    return (json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n').encode()


def render(manifest):
    """Return (strict Node config, receiver env projection) without touching the host."""
    m = _object(manifest, ('version', 'deployment', 'channel', 'tenantId', 'owner', 'lineKey',
                           'target', 'stateDirectory', 'mediaRoot',
                           'instructions', 'runtime', 'relay', 'credentials'),
                ('providerThreadId', 'firstContact', 'activationReceipt', 'telegramLink',
                 'skillPackRoot', 'photonLineNumber', 'modelProxy', 'modelDirect'))
    if type(m['version']) is not int or m['version'] != 1 or m['deployment'] not in ('managed', 'self_host'):
        raise ManifestError('manifest version or deployment is invalid')
    channel = m['channel']
    if channel not in ('photon', 'telegram') or m['target'] != channel:
        raise ManifestError('unsupported or mismatched delivery channel')
    if m['deployment'] == 'managed' and channel != 'photon':
        raise ManifestError('managed channel must be Photon')
    tenant = m['tenantId']
    if (not isinstance(tenant, str) or _utf16_units(tenant) > 512 or tenant.strip() != tenant
            or any(ord(c) < 32 or ord(c) == 127 for c in tenant)):
        raise ManifestError('tenant identity is invalid')
    if m['deployment'] == 'managed' and not tenant:
        raise ManifestError('managed tenant identity is required')
    owner = _object(m['owner'], ('accountId', 'ownerId'))
    account_id, owner_id = _text(owner['accountId']), _text(owner['ownerId'])
    line_key = _text(m['lineKey'])
    runtime = _object(m['runtime'], ('port', 'bearerToken'))
    runtime_port = _port(runtime['port'])
    runtime_bearer = _text(runtime['bearerToken'], 4096)
    if len(runtime_bearer) < 32 or any(c.isspace() for c in runtime_bearer):
        raise ManifestError('runtime bearer is invalid')
    relay = _object(m['relay'], ('receiverPort', 'rootBearer'))
    receiver_port = _port(relay['receiverPort'])
    if runtime_port == receiver_port:
        raise ManifestError('runtime and receiver ports must differ')
    root_bearer = _text(relay['rootBearer'], 4096)
    read_bearer = hmac.new(root_bearer.encode(), b'sotto-mcp', hashlib.sha256).hexdigest()
    if ('modelProxy' in m) == ('modelDirect' in m):
        raise ManifestError('exactly one explicit model configuration is required')
    if 'modelDirect' in m:
        if m['deployment'] != 'self_host':
            raise ManifestError('direct model credentials require self-host deployment')
        direct = _object(m['modelDirect'], ('provider', 'apiKey', 'chatModelId', 'nativeMediaModelId'))
        if direct['provider'] != 'google':
            raise ManifestError('direct model provider is unsupported')
        model_config = {'provider': 'google', 'apiKey': _text(direct['apiKey'], 4096),
                        'chatModelId': _text(direct['chatModelId'], 128),
                        'nativeMediaModelId': _text(direct['nativeMediaModelId'], 128)}
        model_secret = model_config['apiKey']
        if any(ord(char) < 33 or ord(char) > 126 for char in model_secret):
            raise ManifestError('direct model API key is invalid')
        model_field = 'modelDirect'
    else:
        proxy = _object(m['modelProxy'], ('origin', 'bearer', 'chatModelId', 'nativeMediaModelId'))
        model_config = {'origin': _proxy_origin(proxy['origin']), 'bearer': _text(proxy['bearer'], 4096),
                        'chatModelId': _text(proxy['chatModelId'], 128),
                        'nativeMediaModelId': _text(proxy['nativeMediaModelId'], 128)}
        model_secret = model_config['bearer']
        model_field = 'modelProxy'
    if not _MODEL.fullmatch(model_config['chatModelId']) or not _MODEL.fullmatch(model_config['nativeMediaModelId']):
        raise ManifestError('model IDs are invalid')
    if root_bearer in (runtime_bearer, model_secret):
        raise ManifestError('relay root cannot be reused as another capability')
    instructions = _instructions(m['instructions'])
    if m['deployment'] == 'managed' and channel == 'photon':
        instructions = _instructions(instructions + '\n\n' + MANAGED_PHOTON_CAPABILITIES)
    elif m['deployment'] == 'self_host' and channel == 'photon' and model_field == 'modelDirect':
        if not tenant:
            raise ManifestError('self-host Photon owner-command tenant identity is required')
        instructions = _instructions(instructions + '\n\n' + SELF_HOST_PHOTON_CAPABILITIES)
    elif m['deployment'] == 'self_host' and channel == 'telegram' and model_field == 'modelDirect':
        if tenant:
            instructions = _instructions(instructions + '\n\n' + SELF_HOST_TELEGRAM_CAPABILITIES)
    credentials = m['credentials']
    if channel == 'telegram':
        if ('activationReceipt' in m or 'firstContact' in m or 'photonLineNumber' in m
                or 'providerThreadId' not in m or not _USER.fullmatch(owner_id)):
            raise ManifestError('Telegram owner/thread binding is invalid')
        thread_id = _text(m['providerThreadId'])
        if int(owner_id) > 9007199254740991 or thread_id != f'telegram:{owner_id}':
            raise ManifestError('Telegram owner/thread binding is invalid')
        creds = _object(credentials, ('telegramToken',))
        link = _object(m.get('telegramLink'), ('bot_token', 'allowed_user'),
                       ('bot_id', 'bot_username', 'linked_at'))
        token = _text(creds['telegramToken'], 4096)
        if (link['bot_token'] != token or type(link['allowed_user']) is not int
                or link['allowed_user'] > 9007199254740991 or str(link['allowed_user']) != owner_id):
            raise ManifestError('Telegram pairing does not match selected bot and owner')
        rendered_credentials = {'telegramToken': token,
                                'telegramLinkFile': {'bot_token': token, 'allowed_user': link['allowed_user']}}
    else:
        if 'telegramLink' in m:
            raise ManifestError('Photon cannot use a Telegram link')
        if not _PHONE.fullmatch(owner_id):
            raise ManifestError('Photon owner must be a concrete E.164 address')
        creds = _object(credentials, ('mode', 'photonProjectId', 'photonProjectSecret'))
        if creds['mode'] != 'cloud':
            raise ManifestError('only Photon Cloud-project fixtures are supported')
        project = _text(creds['photonProjectId'], 4096)
        secret = _text(creds['photonProjectSecret'], 4096)
        if 'firstContact' in m:
            if 'activationReceipt' in m or 'providerThreadId' in m:
                raise ManifestError('first contact cannot reuse an activated receipt or fixed thread')
            first_contact = m['firstContact']
            shared_project = first_contact == {'mode': 'owner_dm_shared_project'}
            if shared_project:
                expected_line = None
                if 'photonLineNumber' in m and not _PHONE.fullmatch(_text(m['photonLineNumber'])):
                    raise ManifestError('Photon display line is invalid')
            else:
                first_contact = _object(first_contact, ('mode', 'expectedProviderLine'))
                expected_line = _text(first_contact['expectedProviderLine'])
                if (first_contact['mode'] != 'owner_dm' or not _PHONE.fullmatch(expected_line)
                        or ('photonLineNumber' in m and m['photonLineNumber'] != expected_line)):
                    raise ManifestError('first contact requires one explicit physical Photon line')
        else:
            if 'providerThreadId' not in m:
                raise ManifestError('fixed Photon requires a provider thread')
            thread_id = _text(m['providerThreadId'])
            receipt = _object(m.get('activationReceipt'), ('tenant_id', 'owner', 'chat_id', 'activated', 'project_fingerprint'))
            fingerprint = hashlib.sha256(('sotto-photon-project-v1:' + project).encode()).hexdigest()
            if (receipt['activated'] is not True or receipt['tenant_id'] != tenant or receipt['owner'] != owner_id
                    or receipt['project_fingerprint'] != fingerprint or not _FINGERPRINT.fullmatch(fingerprint)):
                raise ManifestError('Photon activation receipt does not match declared identity')
            guid = _text(receipt['chat_id'], 400)
            # Match the pinned Photon adapter's public isDM GUID convention.
            if ';-;' not in guid:
                raise ManifestError('Photon activation chat is not a direct-message GUID')
            line_number = _text(m['photonLineNumber']) if 'photonLineNumber' in m else None
            if line_number is not None and not _PHONE.fullmatch(line_number):
                raise ManifestError('Photon line is invalid')
            expected_thread = f'imessage:{guid}' + (f'~{line_number}' if line_number else '')
            if '~' in guid or thread_id != expected_thread:
                raise ManifestError('Photon SDK thread does not match activation chat and line')
        rendered_credentials = {'mode': 'cloud', 'photonProjectId': project, 'photonProjectSecret': secret}
    config = {'version': 1, 'channel': channel, 'stateDirectory': _path(m['stateDirectory']),
              'mediaRoot': _path(m['mediaRoot']), 'bearerToken': runtime_bearer, 'port': runtime_port,
              'owner': {'accountId': account_id, 'ownerId': owner_id}, 'lineKey': line_key,
              'target': channel, 'instructions': instructions,
              model_field: model_config,
              'bridge': {'url': f'http://127.0.0.1:{receiver_port}/mcp', 'readBearer': read_bearer},
              'credentials': rendered_credentials, 'allowedSkills': [], 'turnPresence': False}
    if model_field == 'modelDirect':
        config['deployment'] = 'self_host'
    if tenant:
        config['tenantId'] = tenant
    if channel == 'photon' and 'firstContact' in m:
        config['firstContact'] = dict(first_contact)
    else:
        config['providerThreadId'] = thread_id
    if channel == 'photon' and (m['deployment'] == 'managed' or
                                (m['deployment'] == 'self_host' and model_field == 'modelDirect')):
        identity = {'tenant_id': tenant, 'channel': channel, 'account_id': account_id,
                    'owner_id': owner_id, 'line_key': line_key, 'target': channel,
                    'route_mode': ('first_contact_shared_project' if shared_project else 'first_contact') if 'firstContact' in m else 'fixed',
                    'route_id': ('shared_project' if shared_project else expected_line) if 'firstContact' in m else thread_id,
                    'project_fingerprint': hashlib.sha256(
                        ('sotto-photon-project-v1:' + project).encode()).hexdigest()}
        config['ownerLoop'] = {'mode': 'explicit_command', 'identity': identity,
                               'bearer': _owner_loop_capability().derive(root_bearer, identity)}
    elif (channel == 'telegram' and m['deployment'] == 'self_host'
          and model_field == 'modelDirect' and tenant):
        identity = {'tenant_id': tenant, 'channel': 'telegram', 'account_id': account_id,
                    'owner_id': owner_id, 'line_key': line_key, 'target': 'telegram',
                    'route_mode': 'fixed', 'route_id': thread_id,
                    'bot_fingerprint': hashlib.sha256(
                        ('sotto-telegram-bot-v1:' + token).encode()).hexdigest()}
        config['ownerLoop'] = {'mode': 'explicit_command', 'identity': identity,
                               'bearer': _owner_loop_capability().derive(root_bearer, identity)}
    if 'skillPackRoot' in m:
        config['skillPackRoot'] = _path(m['skillPackRoot'])
    receiver = {'SOTTO_CHAT_RUNTIME_URL': f'http://127.0.0.1:{runtime_port}',
                'SOTTO_CHAT_RUNTIME_TOKEN': runtime_bearer}
    if len(_payload_bytes(config)) > MAX_MANIFEST_BYTES:
        raise ManifestError('rendered Node config exceeds its 64 KiB limit')
    return config, receiver


def receiver_channel_expectation(config):
    """Project only nonsecret channel identity from the same rendered Node config."""
    owner = config.get('owner')
    credentials = config.get('credentials')
    if config.get('channel') == 'telegram':
        if (type(config.get('version')) is not int or config['version'] != 1
                or config.get('target') != 'telegram'
                or not isinstance(owner, dict) or set(owner) != {'accountId', 'ownerId'}
                or not isinstance(credentials, dict)
                or set(credentials) != {'telegramToken', 'telegramLinkFile'}):
            raise ManifestError('receiver channel expectation requires rendered Telegram config')
        token = _text(credentials['telegramToken'], 4096)
        link = credentials['telegramLinkFile']
        if (not isinstance(link, dict) or set(link) != {'bot_token', 'allowed_user'}
                or link['bot_token'] != token or type(link['allowed_user']) is not int
                or str(link['allowed_user']) != owner['ownerId']):
            raise ManifestError('receiver Telegram link differs from owner')
        thread = _text(config.get('providerThreadId'))
        if thread != f"telegram:{owner['ownerId']}":
            raise ManifestError('receiver Telegram route differs from owner')
        return {
            'version': 1, 'channel': 'telegram', 'tenantId': _text(config.get('tenantId')),
            'owner': {'accountId': _text(owner['accountId']),
                      'ownerId': _text(owner['ownerId'])},
            'lineKey': _text(config.get('lineKey')), 'target': 'telegram',
            'port': _port(config.get('port')),
            'route': {'mode': 'fixed', 'providerThreadId': thread},
            'botFingerprint': hashlib.sha256(
                ('sotto-telegram-bot-v1:' + token).encode()).hexdigest(),
        }
    if (type(config.get('version')) is not int or config['version'] != 1
            or config.get('channel') != 'photon' or config.get('target') != 'photon'
            or not isinstance(owner, dict) or set(owner) != {'accountId', 'ownerId'}
            or not isinstance(credentials, dict) or credentials.get('mode') != 'cloud'):
        raise ManifestError('receiver channel expectation requires rendered Photon Cloud config')
    project = _text(credentials.get('photonProjectId'), 4096)
    first = config.get('firstContact')
    fixed = config.get('providerThreadId')
    if (first is None) == (fixed is None):
        raise ManifestError('receiver channel route is ambiguous')
    shared = first == {'mode': 'owner_dm_shared_project'}
    if first is not None and not shared and (not isinstance(first, dict) or
                              set(first) != {'mode', 'expectedProviderLine'} or
                              first.get('mode') != 'owner_dm'):
        raise ManifestError('receiver channel route is invalid')
    route = ({'mode': 'first_contact_shared_project'} if shared else
             {'mode': 'first_contact', 'expectedProviderLine': _text(first['expectedProviderLine'])}
             if first is not None else
             {'mode': 'fixed', 'providerThreadId': _text(fixed)})
    return {
        'version': 1, 'channel': 'photon', 'tenantId': config.get('tenantId', ''),
        'owner': {'accountId': _text(owner['accountId']),
                  'ownerId': _text(owner['ownerId'])},
        'lineKey': _text(config.get('lineKey')), 'target': 'photon',
        'port': _port(config.get('port')), 'route': route,
        'projectFingerprint': hashlib.sha256(
            ('sotto-photon-project-v1:' + project).encode()).hexdigest(),
    }


def render_receiver_channel_expectation(manifest):
    """Render from the original explicit input, without discovering Node credentials."""
    config, _ = render(manifest)
    return receiver_channel_expectation(config)


def _publish_noreplace(source, destination):
    """Publish one completed directory atomically, refusing even an empty contender."""
    libc = ctypes.CDLL(None, use_errno=True)
    src, dst = os.fsencode(source), os.fsencode(destination)
    if sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        function = libc.renamex_np
        function.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        result = function(src, dst, 0x00000004)  # Darwin RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        function = libc.renameat2
        function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        result = function(-100, src, -100, dst, 1)  # Linux RENAME_NOREPLACE
    else:
        raise ManifestError('atomic no-replace directory publication is unavailable')
    if result != 0:
        error = ctypes.get_errno()
        raise ManifestError('staging destination cannot be published') from OSError(error, os.strerror(error))


def _fsync_directory(path):
    directory = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _stage_bundle(destination, payloads, *, receiver_sticky_parent=False, replace=False):
    """Create one private bundle; a rerun only removes crash-orphaned temps.

    With `replace` (a same-epoch restart re-rendering rotated configuration) a changed payload
    file is atomically replaced in place; without it a changed payload is a conflict.
    """
    final = Path(destination)
    parent = final.parent
    parent_meta = parent.lstat()
    owned_private = parent_meta.st_uid == os.getuid() and not parent_meta.st_mode & 0o077
    limited_receiver_parent = (
        receiver_sticky_parent
        and parent_meta.st_uid == 0 and parent_meta.st_gid == os.getgid()
        and stat.S_IMODE(parent_meta.st_mode) == 0o1771
    )
    if not stat.S_ISDIR(parent_meta.st_mode) or not (owned_private or limited_receiver_parent):
        raise ManifestError('staging parent must be an owned private real directory')
    if final.exists() or final.is_symlink():
        metadata = final.lstat()
        entries = {p.name for p in final.iterdir()} if stat.S_ISDIR(metadata.st_mode) else set()
        orphans = {name for name in entries if _ORPHAN_STAGE_TEMP.match(name)
                   or (receiver_sticky_parent and _ORPHAN_RECEIVER_TEMP.match(name))}
        extras = entries - payloads.keys() - orphans
        fresh_files = {'owner-routines.json', 'owner-routines.json.lock'} if receiver_sticky_parent else set()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077 or not payloads.keys() <= entries
                or not extras <= fresh_files):
            raise ManifestError('staging destination conflicts with current fixture')
        # Staging runs as the role UID under the supervisor's flock, before that role starts,
        # so no writer can still own one of these temps.
        for name in orphans:
            orphan = (final / name).lstat()
            if (not stat.S_ISREG(orphan.st_mode) or orphan.st_uid != os.getuid()
                    or orphan.st_nlink != 1):
                raise ManifestError('staging destination conflicts with current fixture')
            os.unlink(final / name)
        if orphans:
            _fsync_directory(final)
        # Fresh receiver definitions share this private directory. They are not
        # configuration payloads; offline startup separately checks their epoch.
        for name in extras:
            retained = (final / name).lstat()
            if (not stat.S_ISREG(retained.st_mode) or retained.st_uid != os.getuid()
                    or stat.S_IMODE(retained.st_mode) != 0o600 or retained.st_nlink != 1):
                raise ManifestError('staging destination conflicts with current fixture')
        changed = []
        for name, expected in payloads.items():
            entry = final / name
            metadata = entry.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o077):
                raise ManifestError('staging destination conflicts with current fixture')
            if _read_private_regular(entry, MAX_MANIFEST_BYTES) != expected:
                if not replace:
                    raise ManifestError('staging destination conflicts with current fixture')
                changed.append(name)
        for name in changed:
            descriptor, temporary = tempfile.mkstemp(prefix='.sotto-stage-', dir=final)
            try:
                os.fchmod(descriptor, 0o600)
                data = payloads[name]
                offset = 0
                while offset < len(data):
                    offset += os.write(descriptor, data[offset:])
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                os.replace(temporary, final / name)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if os.path.lexists(temporary):
                    os.unlink(temporary)
        if changed:
            _fsync_directory(final)
        # A prior rename may have succeeded just before its parent barrier failed.
        _fsync_directory(parent)
        return
    temporary = Path(tempfile.mkdtemp(prefix='.sotto-runtime-', dir=parent))
    try:
        for name, data in payloads.items():
            fd = os.open(temporary / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                offset = 0
                while offset < len(data):
                    offset += os.write(fd, data[offset:])
                os.fsync(fd)
            finally:
                os.close(fd)
        _fsync_directory(temporary)
        _publish_noreplace(temporary, final)
        _fsync_directory(parent)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def stage(destination, config, receiver, *, restart=False):
    """Publish the Node and receiver-runtime bundle; only a restart may replace a changed one."""
    _stage_bundle(destination, {
        'runtime-config.json': _payload_bytes(config),
        'receiver-projection.json': _payload_bytes(receiver),
    }, replace=restart)


def stage_receiver_channel_expectation(destination, expectation):
    """Run as the receiver UID with only the nonsecret projected document."""
    if not isinstance(expectation, Mapping):
        raise ManifestError('receiver expectation fields are invalid')
    document = _validate_receiver_channel_expectation(dict(expectation))
    _stage_bundle(destination, {
        'expected-channel.json': _payload_bytes(document),
    }, receiver_sticky_parent=True)


def stage_telegram_link(root, link):
    """Publish the receiver's protected pairing record once; a restart compares it."""
    if (not isinstance(link, dict) or set(link) != {'bot_token', 'allowed_user'}
            or not isinstance(link['bot_token'], str) or not link['bot_token']
            or type(link['allowed_user']) is not int
            or not 0 < link['allowed_user'] <= 9007199254740991):
        raise ManifestError('Telegram link staging input is invalid')
    payload = _payload_bytes(link)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ManifestError('Telegram link staging input is invalid')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parent = os.fstat(directory)
        if (parent.st_uid != 0 or parent.st_gid != os.getegid()
                or stat.S_IMODE(parent.st_mode) != 0o1771):
            raise ManifestError('Telegram link parent is unavailable')
        try:
            fd = os.open('telegram-link.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW, 0o600, dir_fd=directory)
        except FileExistsError:
            fd = os.open('telegram-link.json', os.O_RDONLY | os.O_NOFOLLOW |
                         os.O_NONBLOCK, dir_fd=directory)
            try:
                before = os.fstat(fd)
                observed = os.read(fd, MAX_MANIFEST_BYTES + 1)
                after = os.fstat(fd)
                linked = os.stat('telegram-link.json', dir_fd=directory,
                                 follow_symlinks=False)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                        or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                        or before.st_size != len(payload) or observed != payload
                        or (before.st_dev, before.st_ino, before.st_mtime_ns,
                            before.st_ctime_ns) !=
                           (after.st_dev, after.st_ino, after.st_mtime_ns,
                            after.st_ctime_ns)
                        or (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)):
                    raise ManifestError('Telegram link differs from staged pairing')
            finally:
                os.close(fd)
        else:
            try:
                os.fchmod(fd, 0o600)
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise ManifestError('Telegram link publication failed')
                    remaining = remaining[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        os.fsync(directory)
    finally:
        os.close(directory)


def _validate_receiver_channel_expectation(value):
    if isinstance(value, dict) and value.get('channel') == 'telegram':
        if set(value) != {'version', 'channel', 'tenantId', 'owner', 'lineKey',
                          'target', 'port', 'route', 'botFingerprint'}:
            raise ManifestError('receiver expectation fields are invalid')
        owner, route = value['owner'], value['route']
        if (type(value['version']) is not int or value['version'] != 1
                or value['target'] != 'telegram'
                or not isinstance(owner, dict) or set(owner) != {'accountId', 'ownerId'}
                or not isinstance(route, dict)
                or set(route) != {'mode', 'providerThreadId'}
                or route['mode'] != 'fixed'
                or not isinstance(value['botFingerprint'], str)
                or _FINGERPRINT.fullmatch(value['botFingerprint']) is None):
            raise ManifestError('receiver expectation fields are invalid')
        owner_id = _text(owner['ownerId'])
        _text(value['tenantId'])
        _text(owner['accountId'])
        _text(value['lineKey'])
        _port(value['port'])
        if not _USER.fullmatch(owner_id) or route['providerThreadId'] != f'telegram:{owner_id}':
            raise ManifestError('receiver expectation route is invalid')
        return value
    if not isinstance(value, dict) or set(value) != {
            'version', 'channel', 'tenantId', 'owner', 'lineKey', 'target',
            'port', 'route', 'projectFingerprint'}:
        raise ManifestError('receiver expectation fields are invalid')
    owner = value['owner']
    route = value['route']
    if (type(value['version']) is not int or value['version'] != 1
            or value['channel'] != 'photon' or value['target'] != 'photon'
            or not isinstance(owner, dict) or set(owner) != {'accountId', 'ownerId'}
            or not isinstance(route, dict)
            or not isinstance(value['tenantId'], str)
            or len(value['tenantId']) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in value['tenantId'])
            or not isinstance(value['projectFingerprint'], str)
            or _FINGERPRINT.fullmatch(value['projectFingerprint']) is None):
        raise ManifestError('receiver expectation fields are invalid')
    _text(owner['accountId'])
    _text(owner['ownerId'])
    _text(value['lineKey'])
    _port(value['port'])
    if route.get('mode') == 'fixed':
        if set(route) != {'mode', 'providerThreadId'}:
            raise ManifestError('receiver expectation route is invalid')
        _text(route['providerThreadId'])
    elif route.get('mode') == 'first_contact':
        if (set(route) != {'mode', 'expectedProviderLine'}
                or not _PHONE.fullmatch(_text(route['expectedProviderLine']))):
            raise ManifestError('receiver expectation route is invalid')
    elif route == {'mode': 'first_contact_shared_project'}:
        pass
    else:
        raise ManifestError('receiver expectation route is invalid')
    return value


def read_receiver_channel_expectation(path):
    """Read the receiver UID's explicit private projection, with no Node file access."""
    expected = Path(path)
    if not expected.is_absolute() or expected.name != 'expected-channel.json':
        raise ManifestError('receiver expectation path is invalid')
    parent_fd = file_fd = None
    try:
        parent_fd = os.open(expected.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parent = os.fstat(parent_fd)
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
            raise ManifestError('receiver expectation directory is not private')
        file_fd = os.open(expected.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > MAX_MANIFEST_BYTES):
            raise ManifestError('receiver expectation is not private')
        raw = os.read(file_fd, MAX_MANIFEST_BYTES + 1)
        after = os.fstat(file_fd)
        current = os.stat(expected.name, dir_fd=parent_fd, follow_symlinks=False)
        current_parent = os.stat(expected.parent, follow_symlinks=False)
        if (len(raw) > MAX_MANIFEST_BYTES or len(raw) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                    before.st_ctime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                     after.st_ctime_ns)
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
                or (parent.st_dev, parent.st_ino)
                != (current_parent.st_dev, current_parent.st_ino)):
            raise ManifestError('receiver expectation changed')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_duplicate_free,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               ManifestError('nonfinite receiver expectation')))
        return _validate_receiver_channel_expectation(value)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as error:
        if isinstance(error, ManifestError):
            raise
        raise ManifestError('receiver expectation unavailable') from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline Sotto runtime fixture renderer')
    parser.add_argument('manifest', help='explicit private JSON manifest; no ambient lookup')
    parser.add_argument('--stage-directory', help='new private output directory (no overwrite)')
    args = parser.parse_args(argv)
    try:
        config, receiver = render(read_private_manifest(args.manifest))
        if args.stage_directory:
            stage(args.stage_directory, config, receiver)
            print('staged private runtime configuration and receiver projection')
        else:
            print('valid offline fixture; channel and credentials withheld from preview')
    except (ManifestError, OSError) as exc:
        parser.exit(2, f'startup fixture rejected: {type(exc).__name__}\n')


if __name__ == '__main__':
    main()
