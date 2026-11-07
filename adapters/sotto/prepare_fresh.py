"""One-shot, root-only preparation of a truly empty selected-profile volume.

This does not launch either runtime, activate an owner, or authorize a retry. An
interrupted preparation deliberately leaves a nonempty volume for an operator;
it never cleans up, overwrites, or resumes a partial attempt.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
import sys

# Isolated direct execution (-I) omits the script directory from sys.path.
# Only this installed file's own package directory is an import root.
if not __package__:
    package_dir = str(Path(__file__).resolve().parent)
    if package_dir not in sys.path:
        sys.path.insert(0, package_dir)

try:
    from . import fixed_five, process_handoff, root_supervisor, startup_config
except ImportError:
    import fixed_five
    import process_handoff
    import root_supervisor
    import startup_config

tenant_lifecycle = root_supervisor.tenant_lifecycle
LOCK = tenant_lifecycle.runtime_lock.LOCK
ROOT = Path('/data')


class FreshPreparationUnavailableError(ValueError):
    """The fresh-volume preparation contract could not be established."""


def _refuse() -> None:
    raise FreshPreparationUnavailableError('fresh managed volume unavailable')


def _bundle(path: str | Path) -> tuple[dict, dict, dict]:
    value = root_supervisor._private_input(path)
    root, mount, manifest, authority, identity, public, service_id = (
        root_supervisor._validated_bundle(value))
    if root != ROOT:
        _refuse()
    try:
        config, _ = startup_config.render(manifest)
    except (ValueError, TypeError, AttributeError, RecursionError):
        _refuse()
    first_contact = config.get('firstContact')
    photon = (config.get('channel') == 'photon'
               and config.get('credentials', {}).get('mode') == 'cloud'
               and isinstance(first_contact, dict)
               and first_contact.get('mode') in ('owner_dm', 'owner_dm_shared_project')
               and config.get('providerThreadId') is None)
    managed = (photon and manifest.get('deployment') == 'managed'
               and isinstance(config.get('modelProxy'), dict))
    selfhost_photon = (photon and manifest.get('deployment') == 'self_host'
                       and isinstance(config.get('modelDirect'), dict))
    telegram = (config.get('channel') == 'telegram'
                and manifest.get('deployment') == 'self_host'
                and config.get('providerThreadId') ==
                    f"telegram:{config.get('owner', {}).get('ownerId')}"
                and isinstance(config.get('modelDirect'), dict))
    if not (managed or selfhost_photon or telegram):
        _refuse()
    profile = (fixed_five.TELEGRAM_PROFILE if telegram else
               fixed_five.SELF_HOST_PHOTON_PROFILE if selfhost_photon else fixed_five.PROFILE)
    handoff = process_handoff.build_child_handoff(
        manifest, authority, stable_identity=identity,
        node_config_path='/data/node-config/config/runtime-config.json',
        data_directory='/data', skills_root='/app/sotto-skills',
        receiver_overrides=value['receiverOverrides'],
        public_receiver_authority=public, trusted_service_id=service_id,
        limited_profile=profile)
    fixed_five.validate_environment(handoff.receiver_env)
    return mount, manifest, authority


def _empty_private_namespace() -> None:
    tenant_lifecycle._topology()
    tenant_lifecycle._subreaper()
    if set(tenant_lifecycle._processes()) != {1, os.getpid()}:
        _refuse()


def _mount(root_fd: int, expected: dict, pinned: tuple | None = None) -> tuple:
    if (not isinstance(expected, dict)
            or set(expected) != root_supervisor._MOUNT_KEYS
            or expected.get('mountPoint') != '/data'
            or expected.get('fsType') != 'ext4'
            or not isinstance(expected.get('source'), str)
            or not root_supervisor._MOUNT_FIELD.fullmatch(expected['source'])
            or type(expected.get('major')) is not int
            or type(expected.get('minor')) is not int
            or expected['major'] < 0 or expected['minor'] < 0):
        _refuse()
    observed = root_supervisor._mount_record(ROOT)
    if observed[:4] != (expected['fsType'], expected['source'],
                        expected['major'], expected['minor']):
        _refuse()
    root = os.fstat(root_fd)
    linked = os.stat(ROOT, follow_symlinks=False)
    identity = (root.st_dev, root.st_ino, observed[4])
    if (not stat.S_ISDIR(root.st_mode)
            or (root.st_dev, root.st_ino) != (linked.st_dev, linked.st_ino)
            or (os.major(root.st_dev), os.minor(root.st_dev))
            != (expected['major'], expected['minor'])
            or pinned is not None and identity != pinned):
        _refuse()
    return identity


def _fresh_entries(root_fd: int, *, claimed: bool) -> None:
    """Allow only the empty ext4 formatter directory and our own new lock."""
    names = set(os.listdir(root_fd))
    expected = {LOCK} if claimed else set()
    if names == expected:
        return
    if names != expected | {'lost+found'}:
        _refuse()
    lost_fd = os.open('lost+found', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                      dir_fd=root_fd)
    try:
        lost = os.fstat(lost_fd)
        linked = os.stat('lost+found', dir_fd=root_fd, follow_symlinks=False)
        if (not stat.S_ISDIR(lost.st_mode)
                or lost.st_uid != 0 or lost.st_gid != 0
                or stat.S_IMODE(lost.st_mode) != 0o700
                or lost.st_dev != os.fstat(root_fd).st_dev
                or (lost.st_dev, lost.st_ino) != (linked.st_dev, linked.st_ino)
                or os.listdir(lost_fd)):
            _refuse()
    finally:
        os.close(lost_fd)


def _write_new(parent_fd: int, name: str, payload: bytes, uid: int, gid: int) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=parent_fd)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, uid, gid)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError('fresh volume write incomplete')
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(parent_fd)


def _mkdir_new(parent_fd: int, name: str, uid: int, gid: int) -> int:
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                 dir_fd=parent_fd)
    try:
        os.fchmod(fd, 0o700)
        os.fchown(fd, uid, gid)
        os.fsync(fd)
        os.fsync(parent_fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def prepare(path: str | Path) -> None:
    mount, manifest, authority = _bundle(path)
    _empty_private_namespace()
    root_fd = lock_fd = home_fd = None
    try:
        root_fd = root_supervisor._open_directory(ROOT)
        root = os.fstat(root_fd)
        if (root.st_uid != 0 or root.st_gid != 0
                or stat.S_IMODE(root.st_mode) not in (0o700, 0o755)):
            _refuse()
        pinned = _mount(root_fd, mount)
        _fresh_entries(root_fd, claimed=False)
        lock_fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_EXCL
                          | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=root_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock.st_mode) or lock.st_nlink != 1
                or lock.st_uid != 0 or stat.S_IMODE(lock.st_mode) != 0o600):
            _refuse()
        os.fsync(lock_fd)
        os.fsync(root_fd)
        _fresh_entries(root_fd, claimed=True)
        _empty_private_namespace()
        _mount(root_fd, mount, pinned)

        # The receiver's private credential home (SOTTO_HOME); preflight requires it.
        home_fd = _mkdir_new(root_fd, 'sotto-home', 10001, 10001)
        _mount(root_fd, mount, pinned)
        _empty_private_namespace()

        os.fchown(root_fd, 0, 10001)
        os.fchmod(root_fd, 0o1771)
        os.fsync(root_fd)
        # The root-owned marker is the final completion record. A crash before
        # its fsync leaves a nonempty volume and is never treated as fresh.
        marker = json.dumps({'schema': 1, 'tenant_id': manifest['tenantId'],
                             'volume_id': authority['SOTTO_VOLUME_ID']},
                            sort_keys=True, separators=(',', ':')).encode('utf-8')
        _write_new(root_fd, '.sotto-volume.json', marker, 0, 0)
        _mount(root_fd, mount, pinned)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        raise FreshPreparationUnavailableError('fresh managed volume unavailable') from error
    finally:
        for fd in (home_fd, lock_fd, root_fd):
            if fd is not None:
                os.close(fd)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != '--operator-file':
        print('[sotto] fresh preparation unavailable', file=sys.stderr)
        return 2
    try:
        prepare(argv[1])
    except (FreshPreparationUnavailableError, root_supervisor.SupervisorUnavailableError,
            startup_config.ManifestError, process_handoff.HandoffError,
            fixed_five.ProfileUnavailableError,
            tenant_lifecycle.LifecycleUnavailableError):
        print('[sotto] fresh preparation unavailable', file=sys.stderr)
        return 1
    print('{"prepared":true}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
