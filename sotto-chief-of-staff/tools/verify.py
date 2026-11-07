#!/usr/bin/env python3
"""One credential-free Python/release contract, shared by source and distribution.

Run with the interpreter whose dependencies were installed from requirements-dev.txt.
Bridge compilation/tests remain an explicit platform step; no deployment is performed.
"""
from importlib.util import find_spec
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

# What the gate imports before it can say anything useful, and where to get it. Checked up front so
# a Mac whose Homebrew Python has none of it reads one line — not a CalledProcessError traceback out
# of the first subprocess (ship.sh, Sep 13).
TOOLING = (('pytest', 'pytest'), ('ruff', 'ruff'), ('yaml', 'pyyaml'), ('vulture', 'vulture'))


def _normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


class _RegenUnavailableError(Exception):
    """uv is missing, or its offline resolution can't complete without reaching the network."""


def _regenerate_locks_offline(root, relatives):
    """Reproduce tools/lock-requirements.sh's compile step for each of `relatives`, offline, in a
    scratch copy of the tree, and return {relative: (committed_text, regenerated_text)}.

    Each committed .txt is copied into the scratch tree at its own path *before* compiling, so uv
    treats it as the existing output and prefers its pins instead of re-resolving everything to
    latest — the same reason tools/lock-requirements.sh must never be pointed at a fresh path (see
    its header). --offline makes uv resolve only from its local cache; if that cache is missing
    something uv would otherwise fetch, this raises _RegenUnavailableError rather than touching the
    network, so verify.py never gains a network dependency by having uv installed.
    """
    uv = shutil.which('uv')
    if uv is None:
        raise _RegenUnavailableError('uv is not installed')
    with tempfile.TemporaryDirectory(prefix='verify-locks-') as scratch:
        scratch = Path(scratch)
        results = {}
        for relative in relatives:
            manifest = root / (relative + '.in')
            locked = root / (relative + '.txt')
            for source in (manifest, locked):
                if not source.exists():
                    continue
                destination = scratch / source.relative_to(root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
            argv = [uv, 'pip', 'compile', '--offline', '--python-version', '3.12', '--universal',
                    '--generate-hashes', '--no-build', '--no-annotate',
                    '--custom-compile-command', 'bash tools/lock-requirements.sh']
            if relative != 'requirements':
                argv += ['-c', 'requirements.txt']  # Matches lock-requirements.sh: dev/accounts pin to the runtime lock.
            argv += [str(manifest.relative_to(root)), '-o', str(locked.relative_to(root))]
            run = subprocess.run(argv, cwd=scratch, capture_output=True, text=True)
            if run.returncode != 0:
                detail = run.stderr.strip() or 'uv exited without a message'
                # uv names missing offline cache data explicitly. A malformed manifest or an
                # unsatisfiable constraint is a failed verification, not an optional check.
                if 'Packages were unavailable because the network was disabled.' in detail:
                    raise _RegenUnavailableError(
                        f'could not regenerate {relative}.txt from the local uv cache')
                raise SystemExit(f'verify: dependency resolution failed for {relative}.txt:\n{detail}')
            results[relative] = (locked.read_text(), (scratch / locked.relative_to(root)).read_text())
        return results


def _verify_lock_closure(root, locks):
    """The pin/hash checks in verify_locks are one-directional: they confirm every manifest pin
    is locked at the matching version, never that the lock has nothing *beyond* that closure. Close
    that gap exactly, offline, by asking uv to regenerate and diffing byte-for-byte when it can;
    otherwise retain verify_locks' deterministic pin/hash/version checks and say plainly that the
    full closure check could not run. Installed metadata cannot soundly reconstruct a universal
    lock: platform and extra markers describe dependencies absent from this interpreter.
    """
    try:
        regenerated = _regenerate_locks_offline(root, list(locks))
    except _RegenUnavailableError as reason:
        print(f'[verify] dependency lock closure: exact offline regeneration unavailable ({reason}); '
              'full closure check skipped (direct pins, hashes, and cross-lock versions were checked)',
              flush=True)
        return
    stale = [relative for relative, (committed, fresh) in regenerated.items() if committed != fresh]
    if stale:
        raise SystemExit(
            'verify: ' + ', '.join(f'{relative}.txt' for relative in stale) + ' is not what uv '
            'resolves from the manifest today (extra, missing, or re-pinned entries); run '
            'bash tools/lock-requirements.sh and review the diff')
    print('[verify] dependency lock closure: confirmed offline by regenerating with uv '
          f'({", ".join(locks)})', flush=True)


def verify_locks(root):
    """Reject stale direct pins, unhashed entries, divergent test/runtime versions, and (offline,
    see _verify_lock_closure) a lock that is not the closure of its manifest."""
    def entries(path):
        return [line.strip() for line in path.read_text().replace('\\\n', ' ').splitlines()
                if line.strip() and not line.lstrip().startswith('#')]

    def pins(path, hashed=False):
        result = {}
        for line in entries(path):
            if not hashed and line.startswith('-r '):
                result.update(pins(path.parent / line[3:].strip()))
                continue
            match = re.match(r'^([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s|;|$)', line)
            if not match or (hashed and not re.search(r'--hash=sha256:[a-f0-9]{64}(?:\s|$)', line)):
                raise SystemExit(f'verify: {path} needs exact pins and generated hashes; run bash tools/lock-requirements.sh')
            result[_normalize(match[1])] = match[2]
        return result

    locks = {}
    for relative in ('requirements', 'requirements-dev', 'cloud/accounts/requirements'):
        manifest = root / (relative + '.in')
        if not manifest.exists():
            if relative.startswith('cloud/') and not manifest.parent.exists():
                continue  # The hosted account service is intentionally absent from public releases.
            raise SystemExit(f'verify: missing dependency manifest {manifest}')
        direct = pins(manifest)
        locked = pins(root / (relative + '.txt'), hashed=True)
        if any(locked.get(name) != version for name, version in direct.items()):
            raise SystemExit(f'verify: {relative}.txt is stale; run bash tools/lock-requirements.sh')
        locks[relative] = locked
    if any(locks['requirements-dev'].get(name) != version
           for name, version in locks['requirements'].items()):
        raise SystemExit('verify: development dependencies must include the exact runtime lock')
    for name, version in locks.get('cloud/accounts/requirements', {}).items():
        if name in locks['requirements'] and version != locks['requirements'][name]:
            raise SystemExit('verify: account dependencies diverge from the runtime lock')
    _verify_lock_closure(root, locks)


def preflight(python=None):
    python = python or sys.executable
    missing = [package for module, package in TOOLING if find_spec(module) is None]
    if missing:
        raise SystemExit(
            f"verify: {python} is missing {', '.join(missing)}.\n"
            "Install the pinned test tooling into a virtualenv next to this tree (Homebrew's Python "
            "refuses system-wide pip installs) and re-run — ship.sh picks .venv up by itself:\n"
            "  python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt")


def smoke_suites(root):
    """Hermetic fixture-model/transport unit suites beside the packaged smokes (no Docker, no network).

    The packaged smoke runners themselves need built images and stay explicit release steps; only
    their offline `test_*.py` contracts run here, wherever a smoke directory carries them.
    """
    smoke = root / 'smoke'
    if not smoke.is_dir():
        return []
    return [str(path.relative_to(root)) for path in sorted(smoke.iterdir())
            if path.is_dir() and any(path.glob('test_*.py'))]


def verify_unique_test_basenames(root):
    """A bare `pytest` from the root imports test modules by basename (no packages here), so two
    test files with one name make collection error before any test runs."""
    seen = {}
    for path in sorted(root.rglob('test_*.py')):
        relative = path.relative_to(root)
        if any(part in ('node_modules', '__pycache__', 'sotto-bridge') for part in relative.parts):
            continue
        if (path.parent / '__init__.py').exists():
            continue
        if path.name in seen:
            raise SystemExit(f'verify: {seen[path.name]} and {relative} share a test module name; '
                             'rename one so a root pytest run can collect both')
        seen[path.name] = relative


def commands(root):
    python = sys.executable
    pack = root / 'sotto-chief-of-staff'
    suites = ['runtime/trigger-receiver', 'adapters/sotto', 'cloud/model-proxy', 'cloud/accounts',
              *smoke_suites(root)]
    return [
        (root, [python, '-m', 'ruff', 'check', '.']),
        (root, [python, '-m', 'vulture', '.', 'sotto-chief-of-staff/tools/vulture_whitelist.py',
                '--min-confidence', '80', '--exclude',
                '*/.venv/*,*/node_modules/*,*/__pycache__/*,*/target/*,*/.pytest_cache/*,*/.ruff_cache/*,*/runtime/sotto-chat/dist/*']),
        (root, ['node', 'tools/check-file-size.mjs']),
        *([(root, [python, 'tools/sync-usage.py', '--check'])] if (root / 'cloud/model-proxy').is_dir() else []),
        (pack, [python, '-m', 'pytest', 'tests', '-q']),
        (pack, [python, 'tools/validate_skills.py']),
        (root, [python, '-m', 'pytest', 'tools/test_setup_selfhost_railway.py',
                        'tools/test_bundle_runtime_descriptor.py',
                        'tools/test_bundle_railway_oauth.py',
                        *(['tools/test_runtime_release_artifact.py'] if (root / 'tools/test_runtime_release_artifact.py').is_file() else []), '-q']),
        # One contract, two trees: the public distribution has no cloud/accounts (the hosted broker
        # never ships), so a suite runs where its directory is and is simply absent otherwise.
        *((root, [python, '-m', 'pytest', suite, '-q']) for suite in suites if (root / suite).is_dir()),
        # Installed from the committed lock with lifecycle scripts disabled. npm check never
        # installs packages or contacts providers; CI and local setup perform npm ci explicitly.
        *([(root / 'runtime/sotto-chat', ['npm', 'run', 'check'])]
          if (root / 'runtime/sotto-chat/package.json').is_file() else []),
    ]


def main():
    preflight()
    root = Path(__file__).resolve().parents[2]
    verify_locks(root)
    verify_unique_test_basenames(root)
    for cwd, argv in commands(root):
        print('[verify] ' + ' '.join(argv[1:]), flush=True)
        subprocess.run(argv, cwd=cwd, check=True)
    for directory in ('adapters', 'tools'):
        for script in sorted((root / directory).rglob('*.sh')):
            subprocess.run(['bash', '-n', str(script)], check=True)
    generator = root / 'tools/prepare-public-repo.sh'
    if generator.is_file():
        with tempfile.TemporaryDirectory(prefix='sotto-verify-') as scratch:
            subprocess.run(['bash', str(generator), str(Path(scratch) / 'distribution'),
                            'kothari-nikunj/sotto'], cwd=root, check=True)
    print('[verify] shared runtime and release contracts passed', flush=True)


if __name__ == '__main__':
    main()
