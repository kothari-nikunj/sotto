"""Load one entry script the way production does, then resolve every import inside its functions.

Run as `python -I fresh_import_probe.py <script> <pack>` (test_entry_script_imports.py does this):
`-I` drops PYTHONPATH and the user site, and the probe replaces its own directory on sys.path with
the script's, exactly as `python3 path/script.py` would. The script's top level runs (its own
sys.path bootstrap included) under a run name that skips its `__main__` block, so nothing is sent,
fetched or composed. Then a static AST walk resolves each absolute import written inside a function
of the script and of every pack module it loaded, against that process's sys.path. A pack module
that a function imports lazily is then imported too (its top level only) and walked the same way.

An import that follows a sys.path change inside the same function is that function's own
bootstrap and is not judged. Prints one JSON object: {"error": str|null, "missing": [...]}.
"""
import ast
import importlib
import importlib.machinery
import json
import os
import runpy
import sys

# Imports that may legitimately be absent from an entry script's process, as (file, module).
# Keep this short; each entry says why. A pack module never belongs here: fix the bootstrap.
ALLOWED = {
    # Ask research's run() executes only inside runtime/trigger-receiver/procedure_runner.py, whose
    # own directory holds retention.py. Other scripts import this module for current_authority alone.
    ('_shared/scripts/ask_research_pipeline.py', 'retention'),
}


def _mutates_path(node):
    """True for `sys.path.insert/append/extend(...)` and `sys.path[...] = ...` / `sys.path = ...`."""
    def is_sys_path(n):
        return (isinstance(n, ast.Attribute) and n.attr == 'path'
                and isinstance(n.value, ast.Name) and n.value.id == 'sys')
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr in ('insert', 'append', 'extend') and is_sys_path(node.func.value)
    if isinstance(node, ast.Assign):
        return any(is_sys_path(t) or (isinstance(t, ast.Subscript) and is_sys_path(t.value))
                   for t in node.targets)
    return False


def lazy_imports(path):
    """(top-level module name, line) for each absolute import inside a function, before any
    sys.path change in that same function."""
    with open(path, encoding='utf-8') as handle:
        tree = ast.parse(handle.read(), path)
    found = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(ast.walk(fn))
        cutoff = min((n.lineno for n in nodes if _mutates_path(n)), default=float('inf'))
        for n in nodes:
            if getattr(n, 'lineno', cutoff) >= cutoff:
                continue
            if isinstance(n, ast.Import):
                found.update((a.name.split('.')[0], n.lineno) for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                found.add((n.module.split('.')[0], n.lineno))
    return sorted(found)


def _pack_files(pack):
    out = set()
    for module in list(sys.modules.values()):
        f = getattr(module, '__file__', None)
        if f and f.endswith('.py') and os.path.realpath(f).startswith(pack + os.sep):
            out.add(os.path.realpath(f))
    return out


def _resolve(name, path):
    if name in sys.modules or name in sys.builtin_module_names or name == '__future__':
        return 'loaded', None
    spec = importlib.machinery.PathFinder.find_spec(name, path)
    return ('found', spec.origin) if spec else ('missing', None)


def main(script, pack):
    script, pack = os.path.realpath(script), os.path.realpath(pack)
    sys.path[0] = os.path.dirname(script)
    sys.argv = [script]
    try:
        runpy.run_path(script, run_name='__sotto_fresh_import_probe__')
    except SystemExit:
        pass
    except BaseException as exc:  # noqa: BLE001 -- report any load failure as data
        return {'error': f'{type(exc).__name__}: {exc}', 'missing': []}
    missing, walked, wave = [], set(), {script} | _pack_files(pack)
    while wave:
        path_now, lazy = list(sys.path), set()
        for f in sorted(wave):
            walked.add(f)
            for name, line in lazy_imports(f):
                state, origin = _resolve(name, path_now)
                rel = os.path.relpath(f, pack)
                if state == 'missing' and (rel, name) not in ALLOWED:
                    missing.append(f'{rel}:{line} {name}')
                elif state == 'found' and origin and os.path.realpath(origin).startswith(pack + os.sep):
                    lazy.add(name)
        for name in sorted(lazy):
            try:
                importlib.import_module(name)
            except BaseException as exc:  # noqa: BLE001
                missing.append(f'import {name}: {type(exc).__name__}: {exc}')
        wave = _pack_files(pack) - walked
    return {'error': None, 'missing': sorted(set(missing))}


if __name__ == '__main__':
    print(json.dumps(main(sys.argv[1], sys.argv[2])))
