"""Validate Node's durable owner-DM evidence before the existing Photon receipt writer.

No provider imports, network access, grant file, or clock live here. The receiver owns
the configured tenant, owner and project; Node owns account, line and thread scopes.
"""
import hmac
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace


_HEX64 = re.compile(r'\A[a-f0-9]{64}\Z')
_FIELDS = frozenset({'version', 'event_id', 'provider_event_id', 'tenant_id',
                     'project_fingerprint', 'owner_id', 'account_id', 'line_key',
                     'provider_thread_id', 'admitted_at'})
_RECEIPT_FIELDS = frozenset({'tenant_id', 'owner', 'chat_id', 'activated',
                             'project_fingerprint'})


def _bounded(value, *, empty=False):
    return (isinstance(value, str) and (empty or bool(value)) and len(value) <= 512
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def verified_chat_guid(evidence, *, tenant_id, owner_id, project_fingerprint):
    """Return the pinned SDK's decoded chat GUID, or reject the entire evidence."""
    if not isinstance(evidence, dict) or set(evidence) != _FIELDS:
        raise ValueError('invalid activation evidence')
    if (type(evidence['version']) is not int or evidence['version'] != 1
            or type(evidence['admitted_at']) is not int
            or not 0 <= evidence['admitted_at'] <= 2**53 - 1
            or not _bounded(evidence['tenant_id'], empty=True)
            or not all(_bounded(evidence[field]) for field in (
                'provider_event_id', 'owner_id', 'account_id', 'line_key', 'provider_thread_id'))
            or not isinstance(evidence['event_id'], str)
            or _HEX64.fullmatch(evidence['event_id']) is None
            or not isinstance(evidence['project_fingerprint'], str)
            or _HEX64.fullmatch(evidence['project_fingerprint']) is None):
        raise ValueError('invalid activation evidence')
    if (evidence['tenant_id'] != tenant_id or evidence['owner_id'] != owner_id
            or not hmac.compare_digest(evidence['project_fingerprint'], project_fingerprint)):
        raise ValueError('activation identity mismatch')
    # @photon-ai/chat-adapter-imessage 3.2.0 encodes
    # `imessage:<chatGuid>[~<phone>]` and splits at the FIRST tilde.
    thread = evidence['provider_thread_id']
    if not thread.startswith('imessage:'):
        raise ValueError('invalid Photon thread')
    rest = thread[len('imessage:'):]
    guid, sep, line = rest.partition('~')
    if (not guid or '~' in guid or len(guid) > 400 or (sep and (not line or '~' in line))
            or any(ord(c) < 32 or ord(c) == 127 for c in guid)):
        raise ValueError('invalid Photon thread')
    return guid


def consume(evidence, *, tenant_id, owner_id, project_fingerprint, writer, ack):
    """Call the sole receipt writer, then ACK; a lost ACK safely retries the writer."""
    guid = verified_chat_guid(evidence, tenant_id=tenant_id, owner_id=owner_id,
                              project_fingerprint=project_fingerprint)
    source = SimpleNamespace(chat_type='dm', user_id=owner_id, chat_id=guid)
    if writer(source) is not True:
        return False
    return ack(evidence['event_id']) is True


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate receipt field')
        value[key] = item
    return value


def verified_current_photon_route(data_root: str | Path, policy, *,
                                  expected_uid: int | None = None) -> dict:
    """Derive one exact DM route from current policy and the sole activation writer.

    The caller supplies receiver-owned configuration, never a self-attested
    owner route. The receipt must carry the current project fingerprint.
    """
    # The ordinary receiver reader is limited to its own files. A root-owned
    # isolated lifecycle supervisor may instead pass its proven native UID.
    if expected_uid is not None and (type(expected_uid) is not int or expected_uid < 0
                                     or os.geteuid() != 0):
        raise ValueError('receiver file owner override unavailable')
    owner = policy.get('PHOTON_HOME_CHANNEL', '')
    tenant = policy.get('SOTTO_TENANT_ID', '')
    project = policy.get('PHOTON_PROJECT_ID', '')
    if (policy.get('SOTTO_CRON_DELIVER', '').strip() != 'photon'
            or not _bounded(owner) or policy.get('PHOTON_ALLOWED_USERS') != owner
            or not _bounded(tenant, empty=True)
            or (policy.get('SOTTO_DEPLOYMENT_MODE') == 'managed' and not tenant)
            or (policy.get('SOTTO_DEPLOYMENT_MODE') != 'managed'
                and re.fullmatch(r'\+[1-9][0-9]{7,14}', owner) is None)
            or not isinstance(project, str) or not project.strip()
            or not policy.get('PHOTON_PROJECT_SECRET')
            or policy.get('PHOTON_ALLOW_ALL_USERS', '').strip().lower()
            not in ('', 'false', '0', 'no', 'off')):
        raise ValueError('current Photon owner policy is unavailable')
    fingerprint = hashlib.sha256(('sotto-photon-project-v1:' + project).encode()).hexdigest()
    if not Path(data_root).is_absolute():  # reject relative roots before opening
        raise ValueError('receiver data root must be absolute')
    directories = []
    receipt_fd = None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(data_root, flags)
        directories.append(root_fd)
        root_meta = os.fstat(root_fd)
        uid = os.geteuid() if expected_uid is None else expected_uid
        # Managed /data is root-owned, group-writable and sticky. The config
        # directory and receipt must still belong to the receiver UID.
        if root_meta.st_uid not in (uid, 0) or (
                root_meta.st_mode & 0o022 and
                not (root_meta.st_uid == 0 and root_meta.st_mode & stat.S_ISVTX)):
            raise ValueError('receiver data root is not protected')
        config_fd = os.open('config', flags, dir_fd=root_fd)
        directories.append(config_fd)
        config_meta = os.fstat(config_fd)
        if config_meta.st_uid != uid or config_meta.st_mode & 0o022:
            raise ValueError('receiver config directory is not protected')
        receipt_fd = os.open('photon-activation.json',
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=config_fd)
        before = os.fstat(receipt_fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > 2048):
            raise ValueError('activation receipt is not private')
        raw = os.read(receipt_fd, 2049)
        after = os.fstat(receipt_fd)
        if (len(raw) > 2048 or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError('activation receipt changed')
        receipt = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
        if (type(receipt) is not dict or set(receipt) != _RECEIPT_FIELDS
                or receipt['activated'] is not True
                or receipt['owner'] != owner or receipt['tenant_id'] != tenant
                or not isinstance(receipt['project_fingerprint'], str)
                or _HEX64.fullmatch(receipt['project_fingerprint']) is None
                or not hmac.compare_digest(receipt['project_fingerprint'], fingerprint)
                or not _bounded(receipt['chat_id'])
                or len(receipt['chat_id']) > 400
                or ';-;' not in receipt['chat_id'] or '~' in receipt['chat_id']):
            raise ValueError('activation receipt does not match current owner policy')
        current_file = os.stat('photon-activation.json', dir_fd=config_fd,
                               follow_symlinks=False)
        current_config = os.stat('config', dir_fd=root_fd, follow_symlinks=False)
        current_root = os.stat(data_root, follow_symlinks=False)
        if ((current_file.st_dev, current_file.st_ino) != (before.st_dev, before.st_ino)
                or (current_config.st_dev, current_config.st_ino)
                != (config_meta.st_dev, config_meta.st_ino)
                or (current_root.st_dev, current_root.st_ino)
                != (root_meta.st_dev, root_meta.st_ino)):
            raise ValueError('activation receipt path changed')
        return {'platform': 'imessage', 'chat_id': receipt['chat_id'], 'thread_id': None}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('activation receipt unavailable') from error
    finally:
        if receipt_fd is not None:
            os.close(receipt_fd)
        for descriptor in reversed(directories):
            os.close(descriptor)
