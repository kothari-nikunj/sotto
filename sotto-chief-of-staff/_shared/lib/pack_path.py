"""The one shared search path for pack code that runs as `python3 path/script.py`.

Skill scripts run with no install and no PYTHONPATH, so a function that imports `knowledge` or
`preferences` works only if something in that process put the directory on sys.path first. The
test suite puts every pack directory on sys.path, which hides a missing one until production
(tests/test_entry_script_imports.py checks each entry script in a fresh interpreter instead).

`ensure()` adds the real paths of `_shared/lib`, `_shared/scripts` and `_shared/knowledge` once.
Real paths, never a spelling with `..`: a second spelling of one directory has loaded a module twice.
"""
import os
import sys

SHARED = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DIRECTORIES = tuple(os.path.join(SHARED, name) for name in ('lib', 'scripts', 'knowledge'))


def ensure():
    """Put lib, scripts and knowledge on sys.path, each once, ahead of the interpreter's own entries."""
    for directory in reversed(DIRECTORIES):
        if directory not in sys.path:
            sys.path.insert(0, directory)
