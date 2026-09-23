"""Protect the managed product identity while leaving tenant state writable."""
import os
from pathlib import Path
import pwd
import stat
import sys


def _real_directory(path):
    path = Path(path).absolute()
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise RuntimeError(f'Managed identity path must be a real directory: {path}')
    return path


def verify_home(home):
    """Reject a redirected Hermes home before the root supervisor seeds or writes it."""
    home = _real_directory(home)
    for name in ('skills', 'skill-bundles'):
        _real_directory(home / name)
    return home


def _root_file(path, mode, *, required=False):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if required:
            raise
        return
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError(f'Managed identity path must be a regular file: {path}')
        os.fchown(fd, 0, 0)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def protect(data, home, alias):
    """Install a sticky directory-entry boundary around the managed SOUL.md."""
    if os.geteuid() != 0:
        raise RuntimeError('Managed identity protection requires the root supervisor')
    account = pwd.getpwnam('sotto')
    data = _real_directory(data)
    home = verify_home(home)
    alias = Path(alias).absolute()
    alias_parent = _real_directory(alias.parent)
    if home.parent != data or not data.is_dir() or not home.is_dir():
        raise RuntimeError('Managed Hermes home must be a directory directly under the data mount')
    if not alias.is_symlink() or Path(os.readlink(alias)).absolute() != home:
        raise RuntimeError('Managed Hermes alias must point at the verified home')
    # A read-only file in a user-owned directory can be replaced with rename(2).
    # Sticky root-owned parents prevent the workload UID from replacing either
    # the Hermes directory or SOUL.md, while retaining top-level state writes.
    for directory in (data, home, alias_parent):
        os.chown(directory, 0, account.pw_gid)
        os.chmod(directory, stat.S_ISVTX | 0o770)
    _root_file(home / 'SOUL.md', 0o444, required=True)
    # The process-level flock must remain tied to an entry the workload cannot
    # replace, or two containers could each lock a different inode.
    _root_file(data / '.sotto-runtime.lock', 0o600)
    _root_file(data / '.sotto-volume.json', 0o600)
    os.lchown(alias, 0, 0)


if __name__ == '__main__':
    try:
        if sys.argv[1] == 'verify-home' and len(sys.argv) == 3:
            verify_home(sys.argv[2])
        elif sys.argv[1] == 'protect' and len(sys.argv) == 5:
            protect(sys.argv[2], sys.argv[3], sys.argv[4])
        else:
            raise ValueError('usage: managed_identity.py verify-home HOME | protect DATA HOME ALIAS')
    except (IndexError, OSError, ValueError, RuntimeError) as error:
        sys.exit('[sotto] managed identity protection failed: ' + str(error))
