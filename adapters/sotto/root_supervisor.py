"""Explicit root entrypoint for one managed Photon fixed-five runtime lineage.

The operator file is a root-private deployment input, not a source of model
authority. The kernel mount check binds it to a currently attached volume;
the deployment operator remains responsible for authenticating the provider's
volume and public-service metadata before placing that file.
"""
from __future__ import annotations

import json
from math import isfinite
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

if not __package__:
    # `python -I` omits the script directory. Resolve only this packaged file's
    # directory; neither the current directory nor PYTHONPATH is an import root.
    module_dir = str(Path(__file__).resolve().parent)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)

try:
    from . import fixed_five, native_replacement, process_handoff, startup_config, tenant_lifecycle
except ImportError:
    import fixed_five
    import native_replacement
    import process_handoff
    import startup_config
    import tenant_lifecycle


class SupervisorUnavailableError(ValueError):
    """The trusted fresh-profile launch cannot be established."""


_BUNDLE_KEYS = frozenset({
    'version', 'tenantRoot', 'mount', 'manifest', 'receiverAuthority',
    'stableIdentity', 'publicReceiverAuthority', 'trustedServiceId',
    'receiverOverrides',
})
_MOUNT_KEYS = frozenset({'mountPoint', 'source', 'fsType', 'major', 'minor'})
_MOUNT_FIELD = re.compile(r'[A-Za-z0-9_./:+-]{1,256}\Z')
_REJECT_FS = frozenset({'overlay', 'tmpfs', 'ramfs', 'proc', 'sysfs', 'squashfs'})
_MAX_BUNDLE_BYTES = 128 * 1024
_MAX_MOUNTINFO_BYTES = 1024 * 1024


def _reject() -> None:
    raise SupervisorUnavailableError('root supervisor input unavailable')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _reject()
        value[key] = item
    return value


def _strict_json(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=lambda _: _reject(),
                           parse_float=lambda text: _finite_float(text))
    except (UnicodeError, ValueError, TypeError, RecursionError):
        _reject()
    if not isinstance(value, dict):
        _reject()
    return value


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not isfinite(parsed):
        _reject()
    return parsed


def _open_directory(path: Path) -> int:
    """Walk every absolute component without following an ancestor symlink."""
    if not path.is_absolute() or any(part in ('.', '..') for part in path.parts):
        _reject()
    current = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _private_input(path: str | Path) -> dict:
    selected = Path(path)
    parent_fd = file_fd = None
    try:
        parent_fd = _open_directory(selected.parent)
        parent = os.fstat(parent_fd)
        if (parent.st_uid != 0 or stat.S_IMODE(parent.st_mode) != 0o700):
            _reject()
        file_fd = os.open(selected.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > _MAX_BUNDLE_BYTES):
            _reject()
        raw = os.read(file_fd, _MAX_BUNDLE_BYTES + 1)
        after = os.fstat(file_fd)
        linked = os.stat(selected.name, dir_fd=parent_fd, follow_symlinks=False)
        if (len(raw) != before.st_size
                or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
                or (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)):
            _reject()
        return _strict_json(raw)
    except OSError as error:
        raise SupervisorUnavailableError('root supervisor input unavailable') from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _mount_record(mount_point: Path) -> tuple[str, str, int, int, int]:
    """Get the one kernel mount at this exact path; no marker-only proof."""
    try:
        with open('/proc/self/mountinfo', 'rb', buffering=0) as stream:
            raw = _read_mountinfo(stream)
        matches = []
        for line in raw.splitlines():
            parts = line.split(b' ')
            if len(parts) < 10 or b'-' not in parts:
                continue
            divider = parts.index(b'-')
            if divider + 2 >= len(parts) or parts[4] != os.fsencode(mount_point):
                continue
            major_minor = parts[2].split(b':')
            if len(major_minor) != 2:
                _reject()
            matches.append((parts[divider + 1].decode('ascii'),
                            parts[divider + 2].decode('ascii'),
                            int(major_minor[0]), int(major_minor[1]), int(parts[0])))
        if len(matches) != 1:
            _reject()
        return matches[0]
    except (OSError, ValueError, UnicodeError) as error:
        raise SupervisorUnavailableError('managed volume attachment unavailable') from error


def _read_mountinfo(stream) -> bytes:
    """Read a proc sequence file through EOF; raw reads may return short chunks."""
    chunks = []
    total = 0
    while True:
        chunk = stream.read(min(65536, _MAX_MOUNTINFO_BYTES + 1 - total))
        if not chunk:
            return b''.join(chunks)
        total += len(chunk)
        if total > _MAX_MOUNTINFO_BYTES:
            _reject()
        chunks.append(chunk)


class _MountedTenant:
    def __init__(self, tenant_root: Path, expected: dict):
        if (not isinstance(expected, dict) or set(expected) != _MOUNT_KEYS
                or not tenant_root.is_absolute()
                or not (tenant_root == Path('/data')
                        or tenant_root.parent == Path('/data'))
                or expected.get('mountPoint') != '/data'
                or type(expected.get('major')) is not int
                or type(expected.get('minor')) is not int
                or expected['major'] < 0 or expected['minor'] < 0
                or not all(isinstance(expected.get(key), str)
                           and _MOUNT_FIELD.fullmatch(expected[key])
                           for key in ('source', 'fsType'))
                or expected['fsType'] in _REJECT_FS):
            _reject()
        self.tenant_root = tenant_root
        self.expected = expected
        self.mount_fd = _open_directory(Path('/data'))
        try:
            self.tenant_fd = _open_directory(tenant_root)
        except BaseException:
            os.close(self.mount_fd)
            raise
        self.mount_identity = self._identity(self.mount_fd)
        self.tenant_identity = self._identity(self.tenant_fd)
        try:
            self.mount_id = _mount_record(Path('/data'))[4]
            self.check()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _identity(fd: int) -> tuple[int, int]:
        item = os.fstat(fd)
        return item.st_dev, item.st_ino

    def check(self) -> None:
        mount = _mount_record(Path('/data'))
        if (mount != (self.expected['fsType'], self.expected['source'],
                      self.expected['major'], self.expected['minor'], self.mount_id)
                or self._identity(self.mount_fd) != self.mount_identity
                or self._identity(self.tenant_fd) != self.tenant_identity):
            raise SupervisorUnavailableError('managed volume attachment changed')
        for path, identity in ((Path('/data'), self.mount_identity),
                               (self.tenant_root, self.tenant_identity)):
            fd = _open_directory(path)
            try:
                if self._identity(fd) != identity:
                    raise SupervisorUnavailableError('managed volume attachment changed')
            finally:
                os.close(fd)
        root = os.fstat(self.tenant_fd)
        if (root.st_uid != 0 or root.st_gid != 10001
                or stat.S_IMODE(root.st_mode) != 0o1771
                or (os.major(root.st_dev), os.minor(root.st_dev))
                != (self.expected['major'], self.expected['minor'])):
            raise SupervisorUnavailableError('managed tenant policy unavailable')

    def close(self) -> None:
        os.close(self.tenant_fd)
        os.close(self.mount_fd)


def _prepare_private_directory(root_fd: int, name: str, uid: int, *, restart_epoch=False) -> None:
    try:
        os.mkdir(name, 0o700, dir_fd=root_fd)
        created = True
    except FileExistsError:
        created = False
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                 dir_fd=root_fd)
    try:
        if created:
            os.fchmod(fd, 0o700)
            os.fchown(fd, uid, uid)
            os.fsync(fd)
            os.fsync(root_fd)
        value = os.fstat(fd)
        linked = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if (value.st_uid != uid or value.st_gid != uid
                or stat.S_IMODE(value.st_mode) != 0o700
                or (value.st_dev, value.st_ino) != (linked.st_dev, linked.st_ino)):
            raise SupervisorUnavailableError('private role directory unavailable')
        entries = os.listdir(fd)
        if ((not restart_epoch and name in ('node-state', 'node-media') and entries)
                or (name == 'node-config' and any(entry != 'config' for entry in entries))):
            raise SupervisorUnavailableError('fresh role directory is not empty')
    finally:
        os.close(fd)


def _stage_as(uid: int, role: str, payload: dict) -> None:
    """Exec a minimal role-only writer; close all supervisor FDs and memory."""
    raw = startup_config._payload_bytes(payload)
    if len(raw) > 256 * 1024:
        raise SupervisorUnavailableError('private role staging unavailable')
    command = ('/usr/local/bin/python3', str(Path(__file__).with_name('role_stager.py')),
               role)
    try:
        child = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
                 'PYTHONDONTWRITEBYTECODE': '1'}, user=uid, group=uid,
            extra_groups=())
        try:
            child.communicate(input=raw, timeout=5.0)
        except subprocess.TimeoutExpired as error:
            child.kill()
            child.communicate()
            raise SupervisorUnavailableError('private role staging timed out') from error
        if child.returncode != 0:
            raise SupervisorUnavailableError('private role staging unavailable')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SupervisorUnavailableError('private role staging unavailable') from error


def _validated_bundle(value: dict) -> tuple[Path, dict, dict, dict, dict, dict, str]:
    if (set(value) != _BUNDLE_KEYS or type(value.get('version')) is not int
            or value['version'] != 1 or not isinstance(value.get('manifest'), dict)
            or not isinstance(value.get('receiverAuthority'), dict)
            or not isinstance(value.get('stableIdentity'), dict)
            or not (isinstance(value.get('publicReceiverAuthority'), dict)
                    or (value.get('publicReceiverAuthority') is None
                        and value['manifest'].get('deployment') == 'self_host'
                        and value['manifest'].get('channel') in ('telegram', 'photon')))
            or not isinstance(value.get('receiverOverrides'), dict)
            or not isinstance(value.get('trustedServiceId'), str)):
        _reject()
    root = Path(value['tenantRoot']) if isinstance(value.get('tenantRoot'), str) else None
    if (root is None or root.name in ('', '.', '..')
            or not (root == Path('/data') or root == Path('/data') / root.name)):
        _reject()
    return (root, value['mount'], value['manifest'], value['receiverAuthority'],
            value['stableIdentity'], value['publicReceiverAuthority'],
            value['trustedServiceId'])


def _stop_until_empty(lifecycle: tenant_lifecycle.Lifecycle) -> None:
    """Never voluntarily release this process's flock with unknown descendants."""
    while True:
        try:
            # The Node runtime allows 30 seconds to settle its gateway and
            # journal during supervised quiesce. Give it its full budget before
            # escalating, while retaining the flock until the tree is empty.
            lifecycle.stop_replacement(timeout=45.0, grace=35.0,
                                       node_quiesce_signal=signal.SIGUSR2)
            return
        except tenant_lifecycle.LifecycleUnavailableError:
            time.sleep(1.0)


def run_operator_file(path: str | Path) -> None:
    if os.geteuid() != 0 or os.getpid() == 1:
        # tini must own PID 1 and reap; this supervisor must be its direct child.
        raise SupervisorUnavailableError('root supervisor topology unavailable')
    value = _private_input(path)
    root, mount_input, manifest, receiver_authority, identity, public, service_id = _validated_bundle(value)
    mounted = _MountedTenant(root, mount_input)
    lifecycle = None
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    old_term = signal.signal(signal.SIGTERM, request_stop)
    old_int = signal.signal(signal.SIGINT, request_stop)
    try:
        restart_epoch = os.path.lexists(root / tenant_lifecycle.runtime_lock.EPOCH_FILE)
        node_config_path = root / 'node-config/config/runtime-config.json'
        config, receiver_projection = startup_config.render(manifest)
        profile = (fixed_five.TELEGRAM_PROFILE if manifest.get('deployment') == 'self_host'
                   and config.get('channel') == 'telegram' else
                   fixed_five.SELF_HOST_PHOTON_PROFILE if manifest.get('deployment') == 'self_host'
                   and config.get('channel') == 'photon' else fixed_five.PROFILE)
        handoff = process_handoff.build_child_handoff(
            manifest, receiver_authority, stable_identity=identity,
            node_config_path=str(node_config_path), data_directory=str(root),
            skills_root='/app/sotto-skills',
            receiver_overrides=value['receiverOverrides'],
            public_receiver_authority=public, trusted_service_id=service_id,
            limited_profile=profile)
        mounted.check()
        lifecycle = tenant_lifecycle.Lifecycle(
            root, native_uid=10001, private_pid_namespace=True,
            reentry_epoch=restart_epoch)
        # Neither first launch nor same-epoch reentry may stop a pre-existing
        # consumer. The original flock is retained through the transition.
        lifecycle.prove_empty_start()
        lifecycle.assert_replacement_root(root)
        for name in ('node-config', 'node-state', 'node-media'):
            _prepare_private_directory(mounted.tenant_fd, name, 10000,
                                       restart_epoch=restart_epoch)
        _stage_as(10000, 'node', {'tenantRoot': str(root), 'config': config,
                                  'receiverProjection': receiver_projection,
                                  'restart': restart_epoch})
        receiver_stage = {'tenantRoot': str(root),
                          'expectation': dict(handoff.receiver_channel_expectation)}
        if profile == fixed_five.TELEGRAM_PROFILE:
            receiver_stage['telegramLink'] = config['credentials']['telegramLinkFile']
        _stage_as(10001, 'receiver', receiver_stage)
        lifecycle.assert_replacement_root(root)
        mounted.check()
        if stopping:
            raise SupervisorUnavailableError('supervisor stopped before launch')
        native_replacement.start_configured_replacement(
            lifecycle, manifest, receiver_authority, stable_identity=identity,
            node_config_path=str(node_config_path), data_directory=str(root),
            skills_root='/app/sotto-skills', receiver_overrides=value['receiverOverrides'],
            public_receiver_authority=public,
            trusted_service_id=service_id, limited_profile=profile,
            restart_epoch=restart_epoch)
        while not stopping:
            lifecycle.check_replacement()
            mounted.check()
            time.sleep(0.2)
        _stop_until_empty(lifecycle)
    except BaseException:
        if lifecycle is not None and lifecycle._state in ('replacement', 'failed'):
            _stop_until_empty(lifecycle)
        elif lifecycle is not None and lifecycle._state == 'quiescent':
            lifecycle.close()
        raise
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        mounted.close()


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != '--operator-file':
        print('[sotto] root supervisor arguments unavailable', file=sys.stderr)
        return 2
    try:
        run_operator_file(argv[2])
        return 0
    except (SupervisorUnavailableError, tenant_lifecycle.LifecycleUnavailableError,
            native_replacement.ReplacementUnavailableError,
            process_handoff.HandoffError, startup_config.ManifestError, OSError):
        print('[sotto] root supervisor unavailable; review tenant before retry', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
