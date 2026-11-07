"""Durable replacement epoch: the lineage fresh routines bind to.

An epoch is written by the live supervisor before launching either role. It
lets a same-volume restart prove it is the same installation; it does not
authorize a restart on its own, clear a hold, replay an effect, or prove a
provider outcome.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat

try:
    from .runtime_lock import EPOCH_FILE
except ImportError:
    from runtime_lock import EPOCH_FILE

VERSION = 3
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_BINDING_KEYS = frozenset({
    'tenant_id', 'volume_id', 'profile', 'runtime_config_sha256',
    'role_projection_sha256', 'receiver_expectation_sha256', 'tenant_root_identity',
})

# What a same-volume restart must share with the epoch: whose history this is (tenant, volume
# marker, profile, owner/line/project expectation) and the tenant directory's inode, which
# survives a volume reattach that changes st_dev. The rendered Node config and the child
# projection hashes stay recorded but are rotatable configuration: every restart re-renders,
# re-stages and re-verifies them from the current operator file. Version 3 dropped the earlier
# native-store and profile bindings; an older record is refused, never upgraded (fresh volumes only).
_ROTATABLE_KEYS = frozenset({'runtime_config_sha256', 'role_projection_sha256'})


class EpochUnavailableError(ValueError):
    """The handoff or its offline evidence cannot be trusted."""


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8', 'strict')


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def binding_for_handoff(*, tenant_id: str, volume_id: str, profile: str,
                        runtime_config_sha256: str, node_argv: tuple[str, ...],
                        receiver_argv: tuple[str, ...], node_env: dict[str, str],
                        receiver_env: dict[str, str], receiver_expectation: dict,
                        tenant_root_identity: tuple[int, int]) -> dict:
    """Hash the exact verified child projection; never persist credentials or argv.

    This function accepts trusted supervisor inputs only. Recomputing it from an
    arbitrary caller's values cannot turn an epoch into authority.
    """
    result = {
        'tenant_id': tenant_id, 'volume_id': volume_id, 'profile': profile,
        'runtime_config_sha256': runtime_config_sha256,
        'role_projection_sha256': _hash({
            'node_argv': node_argv, 'receiver_argv': receiver_argv,
            'node_env': node_env, 'receiver_env': receiver_env,
        }),
        'receiver_expectation_sha256': _hash(receiver_expectation),
        'tenant_root_identity': list(tenant_root_identity),
    }
    _validate_binding(result)
    return result


def _validate_binding(value: object) -> None:
    if not isinstance(value, dict) or set(value) != _BINDING_KEYS:
        raise EpochUnavailableError('replacement epoch binding unavailable')
    if any(not isinstance(value[key], str) or not value[key] or len(value[key]) > 256
           for key in ('tenant_id', 'volume_id', 'profile')):
        raise EpochUnavailableError('replacement epoch binding unavailable')
    if any(not isinstance(value[key], str) or not _HEX64.fullmatch(value[key])
           for key in ('runtime_config_sha256', 'role_projection_sha256',
                       'receiver_expectation_sha256')):
        raise EpochUnavailableError('replacement epoch binding unavailable')
    identity = value['tenant_root_identity']
    if (not isinstance(identity, list) or len(identity) != 2
            or any(type(item) is not int or item < 0 for item in identity)):
        raise EpochUnavailableError('replacement epoch binding unavailable')


def _restart_identity(binding: dict) -> dict:
    """The part of a validated binding a restart must match; see _ROTATABLE_KEYS."""
    identity = {key: value for key, value in binding.items() if key not in _ROTATABLE_KEYS}
    # Inode only: st_dev changes on volume reattach.
    identity['tenant_root_identity'] = binding['tenant_root_identity'][1]
    return identity


def record_for_launch(binding: dict) -> dict:
    """Called only under the original live flock, immediately before first child."""
    _validate_binding(binding)
    return {'version': VERSION, 'status': 'reconciliation_required',
            'epoch_id': secrets.token_hex(16), 'binding': dict(binding)}


def verify_volume_marker(root_fd: int, binding: dict) -> None:
    """Read exact private marker relative to the already-pinned tenant root FD.

    This proves marker contents, not physical mount persistence. Deployment must
    independently authenticate the attached managed volume before supervisor use.
    """
    _verify_volume_marker(root_fd, binding, supervisor_uid=0)


def _verify_volume_marker(root_fd: int, binding: dict,
                          *, supervisor_uid: int) -> None:
    """Internal UID-injected fixture seam; the public call always requires root."""
    descriptor = None
    try:
        _validate_binding(binding)
        root = os.fstat(root_fd)
        if (not stat.S_ISDIR(root.st_mode)
                or [root.st_dev, root.st_ino] != binding['tenant_root_identity']
                or root.st_uid != supervisor_uid
                or (root.st_mode & 0o022 and stat.S_IMODE(root.st_mode) != 0o1771)):
            raise EpochUnavailableError('replacement volume identity unavailable')
        descriptor = os.open('.sotto-volume.json',
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=root_fd)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != supervisor_uid
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > 4096):
            raise EpochUnavailableError('replacement volume identity unavailable')
        raw = os.read(descriptor, 4097)
        after = os.fstat(descriptor)
        linked = os.stat('.sotto-volume.json', dir_fd=root_fd, follow_symlinks=False)
        if (len(raw) != before.st_size or len(raw) > 4096
                or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
                or (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)):
            raise EpochUnavailableError('replacement volume identity unavailable')
        marker = _strict_json(raw)
        if (isinstance(marker, dict)
                and set(marker) == {'schema', 'tenant_id', 'volume_id'}
                and type(marker['schema']) is int and marker['schema'] == 1
                and type(marker['tenant_id']) is str
                and type(marker['volume_id']) is str
                and marker['tenant_id'] == binding['tenant_id']
                and marker['volume_id'] == binding['volume_id']):
            return
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
    raise EpochUnavailableError('replacement volume identity unavailable')


def _strict_json(raw: bytes) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate field')
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


def verify_existing_epoch(root_fd: int, expected_binding: dict) -> str:
    """Verify same-runtime identity under the caller's original supervisor flock.

    This authenticates an existing epoch, not individual effects. The caller
    must also prove mount identity and absence of previous consumers. Each
    uncertain effect stays held per record; nothing here replays it.
    """
    return _verify_existing_epoch(root_fd, expected_binding, supervisor_uid=0)


def _verify_existing_epoch(root_fd: int, expected_binding: dict,
                           *, supervisor_uid: int) -> str:
    descriptor = None
    try:
        _verify_volume_marker(root_fd, expected_binding, supervisor_uid=supervisor_uid)
        descriptor = os.open(EPOCH_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=root_fd)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != supervisor_uid
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > 4096):
            raise EpochUnavailableError('replacement epoch unavailable')
        raw = os.read(descriptor, 4097)
        after = os.fstat(descriptor)
        linked = os.stat(EPOCH_FILE, dir_fd=root_fd, follow_symlinks=False)
        if (len(raw) != before.st_size or len(raw) > 4096
                or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
                or (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)):
            raise EpochUnavailableError('replacement epoch changed')
        record = _strict_json(raw)
        if (not isinstance(record, dict)
                or set(record) != {'version', 'status', 'epoch_id', 'binding'}
                or type(record['version']) is not int or record['version'] != VERSION
                or record['status'] != 'reconciliation_required'
                or not isinstance(record['epoch_id'], str)
                or not _HEX32.fullmatch(record['epoch_id'])):
            raise EpochUnavailableError('replacement epoch unavailable')
        _validate_binding(record['binding'])
        if _restart_identity(record['binding']) != _restart_identity(expected_binding):
            raise EpochUnavailableError('replacement epoch binding changed')
        return record['epoch_id']
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        if isinstance(error, EpochUnavailableError):
            raise
        raise EpochUnavailableError('replacement epoch unavailable') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
