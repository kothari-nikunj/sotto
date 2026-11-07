"""Trusted, one-boot Linux lifecycle boundary for the supervised Sotto runtime.

The root supervisor constructs this before starting any child in an exclusive
container PID namespace. It retains the tenant flock for its whole life, proves
the namespace is empty before launch, then owns exactly the Node and receiver
role trees. It does not decide whether a send or fire was successful, and
nothing it records can be saved for a later process to trust.
"""
from __future__ import annotations

import ctypes
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass

try:
    from . import replacement_epoch, runtime_lock
except ImportError:
    import replacement_epoch
    import runtime_lock


class LifecycleUnavailableError(RuntimeError):
    """A sanitized refusal; no launch or release authority was produced."""


_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37


@dataclass(frozen=True)
class _Process:
    pid: int
    ppid: int
    uid: int
    starttime: int
    state: bytes


@dataclass(frozen=True)
class _TenantLock:
    fd: int
    root_identity: tuple[int, int]
    root_policy: tuple[int, int]
    lock_identity: tuple[int, int]
    lock_policy: tuple[int, int]


def _subreaper() -> None:
    if sys.platform != 'linux' or not hasattr(os, 'pidfd_open') \
            or not hasattr(signal, 'pidfd_send_signal'):
        raise LifecycleUnavailableError('Linux pidfd and subreaper support required')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise LifecycleUnavailableError('could not establish child subreaper')
    observed = ctypes.c_int(-1)
    if libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(observed), 0, 0, 0) != 0 \
            or observed.value != 1:
        raise LifecycleUnavailableError('child subreaper could not be verified')


def _topology() -> None:
    """Require the current image's direct-child-of-tini supervisor layout."""
    try:
        init_name = Path('/proc/1/comm').read_bytes().strip()
    except OSError as error:
        raise LifecycleUnavailableError('isolated process topology unavailable') from error
    if os.geteuid() != 0 or os.getpid() == 1 or os.getppid() != 1 \
            or init_name != b'tini':
        raise LifecycleUnavailableError('isolated process topology required')


def _acquire_tenant_lock(data_root: str | Path, native_uid: int, *,
                         reentry_epoch: bool = False) -> _TenantLock:
    """Claim the existing tenant lock without following a special lock path.

    This is deliberately separate from the established runtime_lock CLI entry:
    the root lifecycle supervisor must reject symlinks/FIFOs before flock and
    retain its own descriptor rather than exec the old wrapper.
    """
    root = Path(data_root)
    if not root.is_absolute():
        raise LifecycleUnavailableError('tenant data root must be absolute')
    root_fd = None
    lock_fd = None
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        meta = os.fstat(root_fd)
        current = os.stat(root, follow_symlinks=False)
        if (not stat.S_ISDIR(meta.st_mode) or (meta.st_dev, meta.st_ino)
                != (current.st_dev, current.st_ino)
                or meta.st_uid not in (0, os.geteuid(), native_uid)
                or (meta.st_mode & 0o022 and not (
                    meta.st_uid == 0 and meta.st_mode & stat.S_ISVTX))):
            raise LifecycleUnavailableError('tenant data root is not protected')
        # A fresh start refuses an existing epoch; only same-epoch reentry may see one.
        forbidden_holds = () if reentry_epoch else (runtime_lock.EPOCH_FILE,)
        for hold in forbidden_holds:
            try:
                os.stat(hold, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise LifecycleUnavailableError('tenant has an unexpected replacement epoch')
        lock_fd = os.open(runtime_lock.LOCK,
                          os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                          0o600, dir_fd=root_fd)
        held = os.fstat(lock_fd)
        linked = os.stat(runtime_lock.LOCK, dir_fd=root_fd, follow_symlinks=False)
        if (not stat.S_ISREG(held.st_mode)
                or stat.S_IMODE(held.st_mode) != 0o600
                or held.st_nlink != 1 or linked.st_nlink != 1
                or held.st_uid not in (0, native_uid)
                or (held.st_dev, held.st_ino) != (linked.st_dev, linked.st_ino)):
            raise LifecycleUnavailableError('tenant lock is not protected')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for hold in forbidden_holds:
            try:
                os.stat(hold, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise LifecycleUnavailableError('tenant has an unexpected replacement epoch')
        current = os.stat(root, follow_symlinks=False)
        linked = os.stat(runtime_lock.LOCK, dir_fd=root_fd, follow_symlinks=False)
        if ((current.st_dev, current.st_ino, current.st_uid,
             stat.S_IMODE(current.st_mode)) !=
                (meta.st_dev, meta.st_ino, meta.st_uid, stat.S_IMODE(meta.st_mode))
                or (linked.st_dev, linked.st_ino, linked.st_nlink) !=
                (held.st_dev, held.st_ino, 1)):
            raise LifecycleUnavailableError('tenant data root changed')
        result, lock_fd = lock_fd, None
        return _TenantLock(
            result, (meta.st_dev, meta.st_ino),
            (meta.st_uid, stat.S_IMODE(meta.st_mode)),
            (held.st_dev, held.st_ino),
            (held.st_uid, stat.S_IMODE(held.st_mode)))
    except OSError as error:
        raise LifecycleUnavailableError('tenant lock unavailable') from error
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if root_fd is not None:
            os.close(root_fd)


def _read_process(pid: int) -> _Process | None:
    """Read kernel identity; a disappearing process is retried by the scan."""
    try:
        raw = Path(f'/proc/{pid}/stat').read_bytes()
        suffix = raw[raw.rfind(b')') + 2:].split()
        if len(suffix) < 20 or int(raw.split(b' ', 1)[0]) != pid:
            raise LifecycleUnavailableError('process identity unavailable')
        status = Path(f'/proc/{pid}/status').read_bytes()
        uid_line = next((line for line in status.splitlines() if line.startswith(b'Uid:')), None)
        if uid_line is None or len(uid_line.split()) != 5:
            raise LifecycleUnavailableError('process identity unavailable')
        uids = [int(item) for item in uid_line.split()[1:]]
        if len(set(uids)) != 1:
            raise LifecycleUnavailableError('process changed credentials')
        return _Process(pid, int(suffix[1]), uids[0], int(suffix[19]), suffix[0])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError, IndexError) as error:
        raise LifecycleUnavailableError('process identity unavailable') from error


def _processes() -> dict[int, _Process]:
    try:
        names = os.listdir('/proc')
    except OSError as error:
        raise LifecycleUnavailableError('process inventory unavailable') from error
    result = {}
    for name in names:
        if name.isdecimal():
            entry = _read_process(int(name))
            if entry is not None:
                result[entry.pid] = entry
    return result


def _owned_processes(processes: dict[int, _Process], *, supervisor_pid: int,
                     native_uid: int) -> list[_Process]:
    """Require a closed tree; a foreign or unclassifiable process blocks proof."""
    selected = []
    for entry in processes.values():
        if entry.pid in (1, supervisor_pid):
            continue
        ancestry = set()
        current = entry
        while current.pid != supervisor_pid:
            if current.pid in ancestry or current.pid == 1:
                raise LifecycleUnavailableError('process outside supervised tree')
            ancestry.add(current.pid)
            parent = processes.get(current.ppid)
            if parent is None:
                raise LifecycleUnavailableError('process ancestry changed during scan')
            current = parent
        if entry.uid != native_uid:
            raise LifecycleUnavailableError('process UID outside native boundary')
        selected.append(entry)
    return selected


def _signal_exact(entry: _Process, sig: signal.Signals) -> None:
    """The pidfd prevents PID reuse between identity check and signal delivery."""
    try:
        descriptor = os.pidfd_open(entry.pid, 0)
    except ProcessLookupError:
        return
    except OSError as error:
        raise LifecycleUnavailableError('could not open owned process') from error
    try:
        current = _read_process(entry.pid)
        if current is None:
            return
        if (current.starttime, current.uid, current.ppid) != (
                entry.starttime, entry.uid, entry.ppid):
            raise LifecycleUnavailableError('process identity changed before signal')
        try:
            signal.pidfd_send_signal(descriptor, sig, None, 0)
        except ProcessLookupError:
            return
        except OSError as error:
            raise LifecycleUnavailableError('could not signal owned process') from error
    finally:
        os.close(descriptor)


class Lifecycle:
    """Own the supervised role tree from an empty namespace through stop.

    `private_pid_namespace` is a trusted deployment precondition, not a
    user-provided assertion. The caller must not use this process to spawn
    other children while the role tree is live.
    """

    def __init__(self, data_root: str | Path, *, native_uid: int,
                 private_pid_namespace: bool,
                 reentry_epoch: bool = False) -> None:
        if not private_pid_namespace or type(native_uid) is not int or native_uid <= 0:
            raise LifecycleUnavailableError('exclusive native process topology required')
        _topology()
        _subreaper()
        if type(reentry_epoch) is not bool:
            raise LifecycleUnavailableError('replacement reentry is invalid')
        claimed = _acquire_tenant_lock(data_root, native_uid,
                                       reentry_epoch=reentry_epoch)
        self._lock_fd = claimed.fd
        self._guard_fd = os.dup(self._lock_fd)
        self._lock_path = Path(data_root) / runtime_lock.LOCK
        self._root_path = Path(data_root)
        self._root_identity = claimed.root_identity
        self._root_policy = claimed.root_policy
        self._lock_identity = claimed.lock_identity
        self._lock_policy = claimed.lock_policy
        self._native_uid = native_uid
        self._reentry_epoch = reentry_epoch
        self._reentry_epoch_id: str | None = None
        self._replacement_epoch_id: str | None = None
        self._pid = os.getpid()
        self._replacement_leaders: dict[int, tuple[int, int]] = {}
        self._replacement_children: list[subprocess.Popen] = []
        self._state = 'open'

    def _assert_lock(self) -> None:
        """Reject forked copies, replaced files/descriptors, or a dropped flock."""
        if os.getpid() != self._pid or self._lock_fd is None:
            raise LifecycleUnavailableError('supervisor lock owner changed')
        try:
            first = os.fstat(self._lock_fd)
            guard = os.fstat(self._guard_fd)
            path = os.stat(self._lock_path, follow_symlinks=False)
            root = os.stat(self._root_path, follow_symlinks=False)
            if (not all(stat.S_ISREG(item.st_mode) for item in (first, guard, path))
                    or any((item.st_dev, item.st_ino) != self._lock_identity
                           for item in (first, guard, path))
                    or any(item.st_nlink != 1 for item in (first, guard, path))
                    or not stat.S_ISDIR(root.st_mode)
                    or (root.st_dev, root.st_ino) != self._root_identity
                    or (root.st_uid, stat.S_IMODE(root.st_mode)) != self._root_policy
                    or any((item.st_uid, stat.S_IMODE(item.st_mode)) != self._lock_policy
                           for item in (first, guard, path))):
                raise LifecycleUnavailableError('tenant lock identity changed')
            # fdinfo lists locks on this *open file description*. Contention
            # alone is insufficient: another container might have acquired
            # the lock after our descriptor was unlocked.
            fdinfo = Path(f'/proc/self/fdinfo/{self._guard_fd}').read_bytes()
            owners = [int(match.group(1)) for line in fdinfo.splitlines()
                      if (match := re.fullmatch(
                          rb'lock:\s+\d+:\s+FLOCK\s+ADVISORY\s+WRITE\s+(\d+)\s+[^\s]+\s+0\s+EOF',
                          line))]
            if owners != [self._pid]:
                raise LifecycleUnavailableError('tenant flock owner changed')
            # A second open file description must conflict with our held flock.
            probe = os.open(self._lock_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                if (os.fstat(probe).st_dev, os.fstat(probe).st_ino) != self._lock_identity:
                    raise LifecycleUnavailableError('tenant lock path changed')
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    fcntl.flock(probe, fcntl.LOCK_UN)
                    raise LifecycleUnavailableError('tenant flock is not held')
            finally:
                os.close(probe)
        except OSError as error:
            raise LifecycleUnavailableError('tenant lock unavailable') from error

    def _reap(self) -> bool:
        """Reap adopted children; ECHILD is the kernel's closed-tree witness.

        A process under this subreaper either remains a child through a living
        ancestor or becomes an adopted child when that ancestor exits. An
        empty `/proc` listing alone can race a final fork/exit transition.
        """
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return True
            except OSError as error:
                raise LifecycleUnavailableError('native reap failed') from error
            if pid == 0:
                return False

    def _assert_empty(self) -> None:
        no_children = self._reap()
        if _owned_processes(_processes(), supervisor_pid=self._pid,
                            native_uid=self._native_uid):
            raise LifecycleUnavailableError('native workers remain')
        if not no_children:
            raise LifecycleUnavailableError('native children remain')
        no_children = self._reap()
        if _owned_processes(_processes(), supervisor_pid=self._pid,
                            native_uid=self._native_uid):
            raise LifecycleUnavailableError('native workers remain')
        if not no_children:
            raise LifecycleUnavailableError('native children remain')

    def prove_empty_start(self) -> None:
        """Require no other process in this namespace while the flock is held.

        Neither a first launch nor a same-epoch restart may stop a pre-existing
        consumer: any process besides PID 1 and this supervisor is a refusal,
        never a signal target. Only a proved-empty owner may launch or close; a
        refusal leaves the flock to be released by this process exiting.
        """
        if self._state != 'open':
            raise LifecycleUnavailableError('empty start refused')
        self._assert_lock()
        self._assert_empty()
        self._assert_lock()
        self._state = 'quiescent'

    def close(self) -> None:
        """Release the lock only after a proved-empty start; otherwise fail closed."""
        if self._lock_fd is None:
            return
        if self._state != 'quiescent':
            raise LifecycleUnavailableError('cannot release lock with native work unresolved')
        self._assert_lock()
        self._assert_empty()
        os.close(self._guard_fd)
        os.close(self._lock_fd)
        self._lock_fd = None
        self._state = 'closed'

    def _replacement_tree(self) -> list[_Process]:
        """Inventory the two role UIDs without mistaking adopted grandchildren for outsiders."""
        for _attempt in range(2):
            processes = _processes()
            selected = []
            missing_parent = False
            for entry in processes.values():
                if entry.pid in (1, self._pid):
                    continue
                ancestry = set()
                current = entry
                while current.pid != self._pid:
                    if current.pid in ancestry or current.pid == 1:
                        raise LifecycleUnavailableError('process outside replacement tree')
                    ancestry.add(current.pid)
                    parent = processes.get(current.ppid)
                    if parent is None:
                        missing_parent = True
                        break
                    current = parent
                if missing_parent:
                    break
                leader = self._replacement_leaders.get(entry.pid)
                if entry.uid not in (10000, self._native_uid) and not (
                        entry.uid == 0 and leader == (entry.starttime, 0)):
                    raise LifecycleUnavailableError('replacement UID outside boundary')
                if leader is not None and leader[0] != entry.starttime:
                    raise LifecycleUnavailableError('replacement leader identity changed')
                selected.append(entry)
            if not missing_parent:
                return selected
        raise LifecycleUnavailableError('replacement ancestry changed')

    def assert_replacement_root(self, data_root: str | Path) -> None:
        """Bind staged replacement identity to the lock's original tenant root."""
        if self._state != 'quiescent':
            raise LifecycleUnavailableError('replacement root unavailable')
        self._assert_lock()
        self._assert_empty()
        path = Path(data_root)
        if not path.is_absolute():
            raise LifecycleUnavailableError('replacement root unavailable')
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                meta = os.fstat(fd)
                current = os.stat(path, follow_symlinks=False)
                if ((meta.st_dev, meta.st_ino) != self._root_identity
                        or (current.st_dev, current.st_ino) != self._root_identity):
                    raise LifecycleUnavailableError('replacement tenant root changed')
            finally:
                os.close(fd)
        except OSError as error:
            raise LifecycleUnavailableError('replacement root unavailable') from error
        self._assert_lock()

    def _hold_replacement_epoch(self, epoch_binding: dict | None = None) -> None:
        """Durably block a later native/Sotto boot before replacement may contact."""
        root_fd = None
        hold_fd = None
        self._replacement_epoch_id = None
        temporary = '.sotto-epoch-' + secrets.token_hex(16)
        try:
            root_fd = os.open(self._root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if (os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino) != self._root_identity:
                raise LifecycleUnavailableError('tenant root changed')
            if epoch_binding is None:
                record = {'version': 1, 'status': 'reconciliation_required'}
            else:
                try:
                    replacement_epoch.verify_volume_marker(root_fd, epoch_binding)
                    record = replacement_epoch.record_for_launch(epoch_binding)
                except replacement_epoch.EpochUnavailableError as error:
                    raise LifecycleUnavailableError('replacement volume identity unavailable') from error
                epoch_id = record.get('epoch_id') if isinstance(record, dict) else None
                if not isinstance(epoch_id, str) or not re.fullmatch(r'[a-f0-9]{32}', epoch_id):
                    raise LifecycleUnavailableError('replacement epoch unavailable')
            hold_fd = os.open(temporary,
                              os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=root_fd)
            os.fchmod(hold_fd, 0o600)
            raw = json.dumps(record,
                             separators=(',', ':')).encode()
            if os.write(hold_fd, raw) != len(raw):
                raise OSError('replacement epoch write incomplete')
            os.fsync(hold_fd)
            os.link(temporary, runtime_lock.EPOCH_FILE,
                    src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
            os.fsync(root_fd)
            self._assert_lock()
            self._replacement_epoch_id = record.get('epoch_id') if epoch_binding is not None else None
        except OSError as error:
            raise LifecycleUnavailableError('replacement hold unavailable') from error
        finally:
            if hold_fd is not None:
                os.close(hold_fd)
            if root_fd is not None:
                try:
                    os.unlink(temporary, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
                os.close(root_fd)

    def _verify_reentry_epoch(self, binding: dict) -> None:
        """Read exact persisted lineage while retaining the original flock."""
        root_fd = None
        try:
            self._assert_lock()
            root_fd = os.open(self._root_path,
                              os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if (os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino) != self._root_identity:
                raise LifecycleUnavailableError('replacement tenant root changed')
            self._reentry_epoch_id = replacement_epoch.verify_existing_epoch(
                root_fd, binding)
            self._assert_lock()
        except (OSError, replacement_epoch.EpochUnavailableError) as error:
            raise LifecycleUnavailableError('replacement epoch unavailable') from error
        finally:
            if root_fd is not None:
                os.close(root_fd)

    def _start_replacement_roles(self, node_argv: tuple[str, ...], receiver_argv: tuple[str, ...],
                                 node_env: dict[str, str], receiver_env: dict[str, str],
                                 *, epoch_binding: dict | None = None) -> None:
        if self._reentry_epoch:
            raise LifecycleUnavailableError('fresh replacement cannot reuse an epoch')
        self._launch_replacement_roles(node_argv, receiver_argv, node_env,
                                       receiver_env, epoch_binding=epoch_binding,
                                       reuse_epoch=False)

    def _restart_replacement_roles(self, node_argv: tuple[str, ...], receiver_argv: tuple[str, ...],
                                   node_env: dict[str, str], receiver_env: dict[str, str],
                                   *, epoch_binding: dict) -> None:
        if not self._reentry_epoch:
            raise LifecycleUnavailableError('replacement epoch is not held')
        self._launch_replacement_roles(node_argv, receiver_argv, node_env,
                                       receiver_env, epoch_binding=epoch_binding,
                                       reuse_epoch=True)

    def _launch_replacement_roles(self, node_argv: tuple[str, ...], receiver_argv: tuple[str, ...],
                                  node_env: dict[str, str], receiver_env: dict[str, str],
                                  *, epoch_binding: dict | None,
                                  reuse_epoch: bool) -> None:
        """Kernel lifecycle primitive. Only a trusted verified handoff may call this.

        This deliberately does not accept a model-facing command or mint readiness.
        The supervisor alone retains the flock; children inherit no lock descriptor.
        """
        if self._state != 'quiescent' or self._replacement_leaders:
            raise LifecycleUnavailableError('replacement transition unavailable')
        self._assert_lock()
        self._assert_empty()
        if (not all(isinstance(value, str) and value and '\x00' not in value
                    for value in node_argv + receiver_argv)
                or not node_argv or not receiver_argv
                or not all(isinstance(env, dict) and all(
                    isinstance(k, str) and isinstance(v, str) and '\x00' not in k + v
                    for k, v in env.items()) for env in (node_env, receiver_env))):
            raise LifecycleUnavailableError('replacement launch refused')
        if reuse_epoch:
            if epoch_binding is None:
                raise LifecycleUnavailableError('replacement epoch binding unavailable')
            self._verify_reentry_epoch(epoch_binding)
            epoch_id = self._reentry_epoch_id
        else:
            self._hold_replacement_epoch(epoch_binding)
            epoch_id = self._replacement_epoch_id
        safe_node_env = dict(node_env)
        safe_node_env.pop('SOTTO_ROUTINE_EPOCH', None)
        safe_receiver_env = dict(receiver_env)
        safe_receiver_env.pop('SOTTO_ROUTINE_EPOCH', None)
        if epoch_id is not None:
            if not isinstance(epoch_id, str) or not re.fullmatch(r'[a-f0-9]{32}', epoch_id):
                raise LifecycleUnavailableError('replacement epoch unavailable')
            safe_receiver_env['SOTTO_ROUTINE_EPOCH'] = epoch_id
        self._state = 'replacement_starting'
        try:
            node = subprocess.Popen(node_argv, env=safe_node_env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    close_fds=True, start_new_session=True,
                                    user=10000, group=10000, extra_groups=())
            self._replacement_children.append(node)
            observed = _read_process(node.pid)
            if observed is None or observed.uid != 10000:
                raise LifecycleUnavailableError('Node role identity unavailable')
            self._replacement_leaders[node.pid] = (observed.starttime, 10000)
            self._assert_lock()
            # Only a self-host receiver's intended setup-link line reaches the owner's private
            # deployment log. A managed tenant's log belongs to the operator, so it never gets
            # the descriptor. General output stays suppressed; the receiver closes this fd.
            safe_receiver_env.pop('SOTTO_SETUP_LOG_FD', None)
            setup_log = os.dup(1) if receiver_env.get('SOTTO_DEPLOYMENT_MODE') == 'self_host' else None
            try:
                if setup_log is not None:
                    safe_receiver_env['SOTTO_SETUP_LOG_FD'] = str(setup_log)
                receiver = subprocess.Popen(receiver_argv, env=safe_receiver_env,
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, close_fds=True,
                                            pass_fds=() if setup_log is None else (setup_log,),
                                            start_new_session=False)  # managed_exec calls setsid itself
            finally:
                if setup_log is not None:
                    os.close(setup_log)
            self._replacement_children.append(receiver)
            observed = _read_process(receiver.pid)
            if observed is None or observed.uid not in (0, self._native_uid):
                raise LifecycleUnavailableError('receiver role identity unavailable')
            self._replacement_leaders[receiver.pid] = (observed.starttime, 0)
            self._assert_lock()
            self._replacement_tree()
            self._state = 'replacement'
        except BaseException as error:
            self._state = 'failed'
            # An uncertain launch is never made a normal close/retry path. Best effort
            # cleanup is still bounded by the same ownership inventory and flock.
            try:
                self._stop_replacement_tree(timeout=5.0, grace=0.2)
            except LifecycleUnavailableError:
                pass
            if isinstance(error, (OSError, ValueError, subprocess.SubprocessError)):
                raise LifecycleUnavailableError('replacement launch failed') from None
            raise

    def _stop_replacement_tree(self, *, timeout: float, grace: float,
                               node_quiesce_signal: signal.Signals | None = None) -> None:
        if not 0 < grace < timeout <= 60:
            raise LifecycleUnavailableError('replacement stop interval invalid')
        if node_quiesce_signal is not None and node_quiesce_signal is not signal.SIGUSR2:
            raise LifecycleUnavailableError('replacement Node quiesce signal invalid')
        started = time.monotonic()
        deadline = started + timeout
        term_deadline = started + grace
        # The supervised Node gets the full grace interval. Its own cleanup may
        # need 30 seconds; the receiver stays available throughout that wait.
        receiver_term_deadline: float | None = None
        signalled: set[tuple[int, int, signal.Signals]] = set()
        while time.monotonic() < deadline:
            self._assert_lock()
            self._reap()
            owned = self._replacement_tree()
            if not owned:
                self._assert_empty()
                return
            node = next((entry for entry in owned if self._replacement_leaders.get(entry.pid) ==
                         (entry.starttime, 10000)), None)
            if node_quiesce_signal is not None and node is not None and node.uid != 10000:
                raise LifecycleUnavailableError('replacement Node identity changed')
            now = time.monotonic()
            if node_quiesce_signal is not None and node is not None and now < term_deadline:
                identity = (node.pid, node.starttime, node_quiesce_signal)
                if identity not in signalled:
                    _signal_exact(node, node_quiesce_signal)
                    signalled.add(identity)
                time.sleep(0.025)
                continue
            if node_quiesce_signal is not None and receiver_term_deadline is None:
                receiver_term_deadline = now + min(5.0, (deadline - now) / 2)
            terminating = now < (receiver_term_deadline if receiver_term_deadline is not None
                                 else term_deadline)
            for entry in owned:
                sig = (signal.SIGKILL if (not terminating or
                       node_quiesce_signal is not None and node is entry) else signal.SIGTERM)
                identity = (entry.pid, entry.starttime, sig)
                if terminating and identity in signalled:
                    continue
                _signal_exact(entry, sig)
                signalled.add(identity)
            time.sleep(0.025)
        raise LifecycleUnavailableError('replacement workers did not stop')

    def stop_replacement(self, *, timeout: float = 10.0, grace: float = 0.5,
                         node_quiesce_signal: signal.Signals | None = None) -> None:
        """Stop all known replacement roles; effects remain unreconciled and lock held."""
        if self._state not in ('replacement', 'failed'):
            raise LifecycleUnavailableError('replacement stop unavailable')
        if node_quiesce_signal is not None and node_quiesce_signal is not signal.SIGUSR2:
            raise LifecycleUnavailableError('replacement Node quiesce signal invalid')
        self._state = 'failed'
        self._stop_replacement_tree(timeout=timeout, grace=grace,
                                    node_quiesce_signal=node_quiesce_signal)
        # Do not set quiescent: no provider-effect reconciliation exists here.

    def check_replacement(self) -> None:
        """A missing role leader is failure even if an adopted grandchild remains."""
        if self._state != 'replacement':
            raise LifecycleUnavailableError('replacement is not running')
        try:
            self._assert_lock()
            self._reap()
            self._replacement_tree()
            for pid, (starttime, expected_uid) in self._replacement_leaders.items():
                observed = _read_process(pid)
                if (observed is None or observed.starttime != starttime
                        or observed.state in (b'Z', b'X')
                        or observed.uid not in ((0, self._native_uid) if expected_uid == 0
                                                else (expected_uid,))):
                    raise LifecycleUnavailableError('replacement role exited')
        except LifecycleUnavailableError:
            self._state = 'failed'
            raise
