"""Unmounted same-flock replacement launch boundary.

Only a trusted supervisor may call this module with authenticated deployment inputs.
The limited managed Photon profile is explicitly opt-in; general selection still refuses.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

try:
    from . import fixed_five, process_handoff, replacement_epoch, startup_config
except ImportError:
    import fixed_five
    import process_handoff
    import replacement_epoch
    import startup_config


class ReplacementUnavailableError(ValueError):
    """A staged role or selected runtime could not be verified."""


def _private_bytes(path: str | Path, *, uid: int, limit: int = 65536) -> bytes:
    """Read one owned, no-follow regular file under an owned private directory."""
    file_fd = None
    parent_fd = None
    try:
        selected = Path(path)
        if not selected.is_absolute():
            raise ReplacementUnavailableError('staged role path unavailable')
        parent_fd = os.open(selected.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parent = os.fstat(parent_fd)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != uid
                or parent.st_mode & 0o077):
            raise ReplacementUnavailableError('staged role directory unavailable')
        file_fd = os.open(selected.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > limit):
            raise ReplacementUnavailableError('staged role file unavailable')
        value = os.read(file_fd, limit + 1)
        after = os.fstat(file_fd)
        linked = os.stat(selected.name, dir_fd=parent_fd, follow_symlinks=False)
        current_parent = os.stat(selected.parent, follow_symlinks=False)
        if (len(value) > limit or len(value) != before.st_size
                or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
                or (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)
                or (parent.st_dev, parent.st_ino) !=
                (current_parent.st_dev, current_parent.st_ino)):
            raise ReplacementUnavailableError('staged role changed')
        return value
    except OSError as error:
        raise ReplacementUnavailableError('staged role unavailable') from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def verified_handoff(manifest, receiver_authority, *, stable_identity,
                     node_config_path, data_directory, skills_root,
                     receiver_overrides=None,
                     public_receiver_authority=None, trusted_service_id=None,
                     limited_profile=None):
    """Rebuild the closed process projection and compare both staged UID-private files."""
    try:
        handoff = process_handoff.build_child_handoff(
            manifest, receiver_authority, stable_identity=stable_identity,
            node_config_path=node_config_path, data_directory=data_directory,
            skills_root=skills_root, receiver_overrides=receiver_overrides,
            public_receiver_authority=public_receiver_authority,
            trusted_service_id=trusted_service_id, limited_profile=limited_profile)
        if (handoff.node_argv != (
                '/usr/local/bin/node', '/app/runtime/sotto-chat/dist/bootstrap/main.js',
                '--config', node_config_path)
                or handoff.receiver_argv != (
                    '/usr/local/bin/python3', '/app/adapters/sotto/managed_exec.py',
                    'receiver', '/app/trigger-receiver/receiver.py')
                or (handoff.node_launch_uid, handoff.receiver_wrapper_uid,
                    handoff.receiver_workload_uid) != (10000, 0, 10001)):
            raise ReplacementUnavailableError('replacement role projection unavailable')
        config = _private_bytes(node_config_path, uid=10000)
        if hashlib.sha256(config).hexdigest() != handoff.runtime_config_sha256:
            raise ReplacementUnavailableError('staged Node configuration changed')
        expectation = Path(data_directory) / 'runtime' / 'expected-channel.json'
        expected = startup_config._payload_bytes(dict(handoff.receiver_channel_expectation))
        if _private_bytes(expectation, uid=10001) != expected:
            raise ReplacementUnavailableError('staged receiver identity changed')
        return handoff
    except (process_handoff.HandoffError, startup_config.ManifestError,
            OSError, TypeError, ValueError) as error:
        if isinstance(error, ReplacementUnavailableError):
            raise
        raise ReplacementUnavailableError('replacement handoff unavailable') from error


def start_configured_replacement(lifecycle, manifest, receiver_authority, *,
                                 stable_identity, node_config_path, data_directory,
                                 skills_root, receiver_overrides=None,
                                 public_receiver_authority=None, trusted_service_id=None,
                                 limited_profile=None, restart_epoch=False) -> None:
    """Launch the closed local profile after live flock/native and staged checks.

    Receiver startup checks its own Node configured identity after Node begins
    listening. This supervisor checks the same closed offline profile before
    launching either child; a later receiver mismatch exits before workers.
    """
    if type(restart_epoch) is not bool:
        raise ReplacementUnavailableError('replacement mode unavailable')
    lifecycle.assert_replacement_root(data_directory)
    handoff = verified_handoff(
        manifest, receiver_authority, stable_identity=stable_identity,
        node_config_path=node_config_path, data_directory=data_directory,
        skills_root=skills_root, receiver_overrides=receiver_overrides,
        public_receiver_authority=public_receiver_authority,
        trusted_service_id=trusted_service_id, limited_profile=limited_profile)
    lifecycle.assert_replacement_root(data_directory)
    if limited_profile not in (fixed_five.PROFILE, fixed_five.TELEGRAM_PROFILE,
                                fixed_five.SELF_HOST_PHOTON_PROFILE):
        raise ReplacementUnavailableError('selected receiver runtime is not ready')
    try:
        fixed_five.validate_offline(dict(handoff.receiver_env), native_uid=10001,
                                    allow_supported_history=restart_epoch)
    except (fixed_five.ProfileUnavailableError, OSError, RuntimeError) as error:
        raise ReplacementUnavailableError('selected receiver runtime is not ready') from error
    lifecycle.assert_replacement_root(data_directory)
    binding = replacement_epoch.binding_for_handoff(
        tenant_id=handoff.receiver_env['SOTTO_TENANT_ID'],
        volume_id=handoff.receiver_env['SOTTO_VOLUME_ID'],
        profile=limited_profile,
        runtime_config_sha256=handoff.runtime_config_sha256,
        node_argv=handoff.node_argv, receiver_argv=handoff.receiver_argv,
        node_env=dict(handoff.node_env), receiver_env=dict(handoff.receiver_env),
        receiver_expectation=dict(handoff.receiver_channel_expectation),
        tenant_root_identity=lifecycle._root_identity)
    launch = (lifecycle._restart_replacement_roles if restart_epoch
              else lifecycle._start_replacement_roles)
    launch(
        handoff.node_argv, handoff.receiver_argv,
        dict(handoff.node_env), dict(handoff.receiver_env), epoch_binding=binding)
