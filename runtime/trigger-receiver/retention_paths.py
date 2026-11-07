"""Path and lock checks for the volume retention sweep."""
import contextlib
import glob
import os
import stat


def guard_path(root: str, path: str) -> None:
    rel = os.path.relpath(path, root)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise OSError("retention path leaves the data volume")
    # The configured mount itself may be a symlink. Only its children belong to the sweep.
    child = root
    for part in rel.split(os.sep):
        child = os.path.join(child, part)
        if os.path.islink(child):
            raise OSError("retention refuses a symbolic link")


def matches(root: str, pattern: str, errors: list):
    # Expand one segment at a time: a full glob would enumerate a linked directory before
    # the per-file check and could report its private filenames in errors.
    *parts, leaf = pattern.split("/")
    directories = [root]
    for part in parts:
        children = []
        for directory in directories:
            for name in glob.iglob(part, root_dir=directory):
                child = os.path.join(directory, name)
                if os.path.islink(child):
                    errors.append({"path": os.path.relpath(child, root), "error": "retention refuses a symbolic link"})
                elif os.path.isdir(child):
                    children.append(child)
        directories = children
    for directory in directories:
        for name in glob.iglob(leaf, root_dir=directory):
            yield os.path.join(directory, name)


def guard_lock(root: str, path: str) -> None:
    guard_path(root, path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("retention requires a regular, single-link lock file")


def diagnostic_gate(root: str, diagnostic: bool, lock):
    if diagnostic:
        directory = os.path.join(root, "events", "learning-diagnostics")
        consent = os.path.join(directory, ".consent")
        guard_lock(root, consent + ".lock")
        if os.path.isdir(directory):
            return lock(consent)
    return contextlib.nullcontext()


@contextlib.contextmanager
def open_log_for_truncate(path: str):
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r+b") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("retention requires a regular, single-link log")
        yield stream, info.st_size
