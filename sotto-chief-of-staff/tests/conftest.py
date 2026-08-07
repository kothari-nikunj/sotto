"""Shared sys.path setup for the whole suite (pytest loads conftest before any test module).

Every test file used to open with its own sys.path.insert block (~45 copies of the same three
lines). The paths live here once instead: _shared/lib, _shared/scripts, and every skill's
scripts/ directory. No two of those directories share a module name (verified across the pack),
so a flat search path is unambiguous; if a collision is ever introduced, drop the colliding dir
from the loop below and pin it per-file again.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _insert(*parts):
    p = os.path.join(ROOT, *parts)
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)


_insert("_shared", "lib")
_insert("_shared", "scripts")
for _entry in sorted(os.listdir(ROOT)):
    if not _entry.startswith((".", "_")):
        _insert(_entry, "scripts")
