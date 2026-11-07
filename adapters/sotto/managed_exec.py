#!/usr/bin/env python3
"""Drop managed workloads to the shared Sotto UID; make the receiver nondumpable before import."""
import ctypes
import os
from pathlib import Path
import pwd
import runpy
import sys

PR_SET_DUMPABLE = 4


def drop_user(name='sotto'):
    account = pwd.getpwnam(name)
    os.initgroups(name, account.pw_gid)
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)
    os.environ.update(HOME=account.pw_dir, USER=name, LOGNAME=name)


def nondumpable():
    if sys.platform.startswith('linux') and ctypes.CDLL(None, use_errno=True).prctl(
            PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'prctl(PR_SET_DUMPABLE) failed')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[0] not in ('receiver', 'gateway'):
        raise SystemExit('usage: managed_exec.py receiver <script> | gateway <command> [args...]')
    mode, command = argv[0], argv[1:]
    os.environ.pop('PYTHONPATH', None)
    os.environ.pop('PYTHONHOME', None)
    adapter = str(Path(__file__).resolve().parent)
    if adapter not in sys.path:
        sys.path.insert(0, adapter)
    import control_vault  # noqa: F401,PLC0415 — capture and pop before dropping privilege/importing runtime
    os.setsid()
    drop_user()
    if mode == 'receiver':
        nondumpable()
        receiver_dir = str(Path(command[0]).resolve().parent)
        if receiver_dir not in sys.path:
            sys.path.insert(0, receiver_dir)
        sys.argv = command
        runpy.run_path(command[0], run_name='__main__')
    else:
        os.execvp(command[0], command)


if __name__ == '__main__':
    main()
