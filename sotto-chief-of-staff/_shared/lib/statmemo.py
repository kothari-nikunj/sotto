"""Per-process reuse of a value built from files, for as long as those files are unchanged.

A value is reused only while every file's (name, inode, size, mtime, ctime) is the same AND every file
was already SETTLE_NS old when the value was built. A file written more recently can be rewritten
inside one filesystem timestamp tick with the same size, so its value is rebuilt on every call.
Callers always get their own deep copy; changing it never changes what the next caller sees."""
import copy
import glob
import os
import time

SETTLE_NS = 2_000_000_000


def reuse(cache: dict, name, stats, build):
    """build()'s value for `name`; `stats` is the [(label, os.stat_result)] it was read from."""
    started = time.time_ns()
    key = tuple((label, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns) for label, s in stats)
    hit = cache.get(name)
    if hit is not None and hit[0] == key:
        return copy.deepcopy(hit[1])
    value = build()
    if all(started - entry[3] >= SETTLE_NS for entry in key):
        cache[name] = (key, copy.deepcopy(value))
    else:
        cache.pop(name, None)
    return value


def by_files(pattern):
    """Decorate a zero-argument reader of the files matching pattern() (a glob)."""
    def wrap(build):
        cache: dict = {}

        def reader():
            where, stats = pattern(), []
            for path in sorted(glob.glob(where)):
                try:
                    stats.append((path, os.stat(path)))
                except OSError:
                    return build()  # a file vanished mid-listing: read fresh, remember nothing
            return reuse(cache, where, stats, build)
        reader.__doc__, reader.__name__ = build.__doc__, build.__name__
        return reader
    return wrap
