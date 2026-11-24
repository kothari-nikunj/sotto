#!/usr/bin/env python3
"""One credential-free Python/release contract, shared by source and distribution.

Run with the interpreter whose dependencies were installed from requirements-dev.txt.
The Bridge core suite runs here on Linux only; the macOS Bridge checks remain an explicit platform
step (sotto-bridge/README.md). No deployment is performed.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib.util import find_spec
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

# What the gate imports before it can say anything useful, and where to get it. Checked up front so
# a Mac whose Homebrew Python has none of it reads one line — not a CalledProcessError traceback out
# of the first subprocess (ship.sh, Sep 13).
TOOLING = (('pytest', 'pytest'), ('ruff', 'ruff'), ('yaml', 'pyyaml'), ('vulture', 'vulture'))

# The Python and Node lanes run side by side; each shards its big test suite by file across this
# many processes: a quarter of the CPUs per lane (tests spawn child processes and bound startup
# time), at most 8 (a container can report more host CPUs than its quota). Each Node shard still
# runs its files one at a time.
SHARD_CAP = 8
SHARDS = max(1, min(SHARD_CAP, (os.cpu_count() or 4) // 4))
# These pack test files each run the publication generator many times and dominate the suite; they
# are dealt first so no two share a shard.
GENERATOR_TEST_FILES = ('tests/test_publish_guards.py', 'tests/test_personal_data_guard.py')
# The Railway runner sets SOTTO_CHECK_RUNNER=1: there every lane whose source this tree carries must
# run, so a missing toolchain or install fails the gate instead of printing a skip.
RUNNER = os.environ.get('SOTTO_CHECK_RUNNER') == '1'
SKIPPED = []


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


def shard(paths, count, first=()):
    """Deal `paths` round-robin into at most `count` lists, the ones in `first` before the rest in
    sorted order; every path lands in exactly one."""
    paths = sorted(paths)
    order = sorted(paths, key=lambda path: (path not in first, path))
    shards = [order[index::count] for index in range(min(count, len(paths)))]
    dealt = sorted(path for files in shards for path in files)
    if dealt != paths:
        raise SystemExit('verify: test shards do not partition the full file list')
    return shards


PYTEST_FILE_LIST = """
import json
from pathlib import Path
import sys

import pytest

base = Path.cwd()

class FileList:
    def pytest_collection_finish(self, session):
        files = sorted({item.path.relative_to(base).as_posix() for item in session.items})
        Path(sys.argv[1]).write_text(json.dumps(files), encoding='utf-8')

raise SystemExit(pytest.main(['--collect-only', '-qq', '-p', 'no:cacheprovider', 'tests'], plugins=[FileList()]))
"""


def pytest_shards(pack, count):
    """Split pytest's own discovery by file, without importing the suite into the gate process."""
    with tempfile.TemporaryDirectory(prefix='sotto-pytest-files-') as directory:
        listing = Path(directory) / 'files.json'
        result = subprocess.run([sys.executable, '-c', PYTEST_FILE_LIST, str(listing)], cwd=pack,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if result.returncode:
            raise SystemExit(f'verify: pytest discovery failed (exit {result.returncode}):\n{result.stdout}')
        files = json.loads(listing.read_text(encoding='utf-8'))
    return [[sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *files]
            for files in shard(files, count, GENERATOR_TEST_FILES)]


def node_test_shards(chat, count):
    """package.json's `test` script (its last word is the file glob) split by file, so `npm test`
    stays the one definition of the Node suite and its flags."""
    *prefix, pattern = shlex.split(json.loads((chat / 'package.json').read_text())['scripts']['test'])
    if '*' not in pattern:
        raise SystemExit(f'verify: package.json test script must end with its file glob, not {pattern}')
    files = [str(path.relative_to(chat)) for path in chat.glob(pattern)]
    return [[*prefix, *files] for files in shard(files, count)]


def static_steps(root):
    """The checks that read source without running a suite: lint, dead code, size caps, usage sync."""
    python = sys.executable
    return [(cwd, [argv]) for cwd, argv in [
        (root, [python, '-m', 'ruff', 'check', '.']),
        (root, [python, '-m', 'vulture', '.', 'sotto-chief-of-staff/tools/vulture_whitelist.py',
                '--min-confidence', '80', '--exclude',
                '*/.venv/*,*/node_modules/*,*/__pycache__/*,*/target/*,*/.pytest_cache/*,*/.ruff_cache/*,*/runtime/sotto-chat/dist/*']),
        (root, ['node', 'tools/check-file-size.mjs']),
        *([(root, [python, 'tools/sync-usage.py', '--check'])] if (root / 'cloud/model-proxy').is_dir() else []),
    ]]


def skip(lane, reason):
    SKIPPED.append(f'{lane} ({reason})')
    print(f'[verify] {lane}: skipped, {reason}; the Railway runner runs it', flush=True)


def bridge_steps(root):
    """The Bridge core suite, as sotto-ci.yml ran it on Linux. Its macOS document tests need the
    Swift helper build (sotto-bridge/README.md), so other platforms skip it. The public tree has no
    Bridge."""
    core = root / 'sotto-bridge/core'
    if not (core / 'Cargo.toml').is_file():
        return []
    if sys.platform != 'linux':
        skip('bridge', f'it runs on Linux only (this is {sys.platform}; see sotto-bridge/README.md)')
        return []
    if shutil.which('cargo') is None:
        skip('bridge', 'cargo is not on PATH')
        return []
    target = os.environ.get('CARGO_TARGET_DIR') or str(core / 'target')
    return [(core, [[sys.executable, str(Path(__file__).resolve()), '--locked-target', target,
                     'cargo', 'test', '--locked']])]


def locked_target(target, argv):
    """Run argv holding an exclusive lock on the cargo target directory for the whole build and test
    run. Jobs share one target directory on the runner, and the tests run binaries out of it; the gate
    does not rely on cargo's own lock to keep a second job from replacing them meanwhile."""
    import fcntl
    os.makedirs(target, exist_ok=True)
    started = time.monotonic()
    with open(os.path.join(target, '.sotto-check.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        print(f'[verify] bridge: waited {time.monotonic() - started:.0f}s for the cargo target lock', flush=True)
        return subprocess.call(argv)


def foundation_steps(root):
    """The standalone local capability contracts from sotto-local-foundations.yml: the reader package
    (its caller runs npm ci there, as for the chat runtime) and the stdlib speech and browser suites."""
    runtime = root / 'runtime'
    reader = runtime / 'local-reader'
    steps = []
    if (reader / 'package.json').is_file():
        if (reader / 'node_modules').is_dir():
            steps += [(reader, [['npm', 'run', 'check']]), (reader, [['npm', 'test']])]
        else:
            skip('foundations', 'runtime/local-reader has no node_modules (npm ci --ignore-scripts there)')
    steps += [(root, [[sys.executable, '-m', 'unittest', 'discover', '-s', f'runtime/{name}/tests']])
              for name in ('local-speech', 'browser-harness') if (runtime / name / 'tests').is_dir()]
    return steps


def fast_lanes(root):
    """The seconds-long subset for a run before a push: the static checks, the doc drift guard,
    skill validation and the typecheck. It never replaces the full gate."""
    python = sys.executable
    pack = root / 'sotto-chief-of-staff'
    chat = root / 'runtime/sotto-chat'
    result = {'python': static_steps(root) + [
        (pack, [[python, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'tests/test_docs_drift.py']]),
        (pack, [[python, 'tools/validate_skills.py']])]}
    if (chat / 'package.json').is_file():
        result['node'] = [(chat, [['npm', 'run', 'typecheck']])]
    return result


def lanes(root, count=SHARDS):
    """{lane: [(cwd, [argv, ...]), ...]}: lanes run concurrently, a lane's steps in order, and the
    argvs of one step (shards) concurrently."""
    python = sys.executable
    pack = root / 'sotto-chief-of-staff'
    chat = root / 'runtime/sotto-chat'
    suites = ['runtime/trigger-receiver', 'adapters/sotto', 'cloud/model-proxy', 'cloud/accounts',
              *smoke_suites(root)]
    steps = static_steps(root)
    steps.append((pack, pytest_shards(pack, count)))
    steps.append((pack, [[python, 'tools/validate_skills.py']]))
    # The receiver, adapter, proxy, account and smoke suites share nothing with the pack suite, so
    # they are a lane of their own: in sequence they took longer than every pack shard together.
    others = [(cwd, [argv]) for cwd, argv in [
        (root, [python, '-m', 'pytest', 'tools/test_setup_selfhost_railway.py',
                        'tools/test_bundle_runtime_descriptor.py',
                        'tools/test_bundle_railway_oauth.py',
                        *(['tools/test_runtime_release_artifact.py'] if (root / 'tools/test_runtime_release_artifact.py').is_file() else []), '-q']),
        # One contract, two trees: the public distribution has no cloud/accounts (the hosted broker
        # never ships), so a suite runs where its directory is and is simply absent otherwise.
        *((root, [python, '-m', 'pytest', suite, '-q']) for suite in suites if (root / suite).is_dir()),
    ]]
    result = {'python': steps, 'suites': others}
    # Installed from the committed lock with lifecycle scripts disabled. These steps are
    # `npm run check` (typecheck, test, build) with the tests sharded; none installs packages or
    # contacts providers; CI and local setup perform npm ci explicitly.
    if (chat / 'package.json').is_file():
        result['node'] = [(chat, [['npm', 'run', 'typecheck']]), (chat, node_test_shards(chat, count)),
                          (chat, [['npm', 'run', 'build']])]
    for name, steps in (('bridge', bridge_steps(root)), ('foundations', foundation_steps(root))):
        if steps:
            result[name] = steps
    return result


# The lines that say what failed, repeated at the end because CI shows only the log's tail: pytest's
# FAILED/ERROR ids and count line, node:test's failed tests and counts, and cargo's failed tests,
# panic locations and result lines.
SUMMARY_LINE = re.compile(r'(FAILED|ERROR) \S|=* *\d+ (passed|failed|errors?)\b.* in [\d.]+s'
                          r'|✖ |not ok \d+\b|[ℹ#] (tests|pass|fail|cancelled) '
                          r"|test \S+ \.\.\. FAILED|thread '.*'.* panicked at |test result: FAILED")


def _summary(output):
    lines = output.decode('utf-8', 'replace').splitlines()
    return list(dict.fromkeys(line.strip() for line in lines if SUMMARY_LINE.match(line.lstrip())))


def run_lanes(lanes_by_name):
    """Run the lanes concurrently. Each command's output is buffered and printed whole when it ends.
    A failure stops its own lane before the next step; the other lanes finish, so one run reports
    every lane. Returns the failed commands."""
    failures, lock = [], threading.Lock()

    def execute(cwd, argv):
        started = time.monotonic()
        proc = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return proc.returncode, proc.stdout, time.monotonic() - started

    def lane(name, steps):
        failed = False
        for cwd, argvs in steps:
            if failed:
                return
            with ThreadPoolExecutor(len(argvs)) as pool:
                running = {pool.submit(execute, cwd, argv): (index, argv) for index, argv in enumerate(argvs, 1)}
                for future in as_completed(running):
                    (index, argv), (code, output, seconds) = running[future], future.result()
                    part = f' shard {index}/{len(argvs)}' if len(argvs) > 1 else ''
                    status = 'passed' if code == 0 else f'FAILED (exit {code})'
                    with lock:
                        print(f'[verify] {name}{part}: {" ".join(argv[1:])}', flush=True)
                        sys.stdout.buffer.write(output)
                        print(f'[verify] {name}{part}: {status} in {seconds:.0f}s', flush=True)
                        if code:
                            failures.append(f'{name}{part}: {" ".join(argv)}' + ''.join(
                                '\n    ' + line for line in _summary(output)))
                            failed = True

    with ThreadPoolExecutor(len(lanes_by_name)) as pool:
        for future in [pool.submit(lane, name, steps) for name, steps in lanes_by_name.items()]:
            future.result()
    return failures


def main():
    if sys.argv[1:2] == ['--locked-target']:
        raise SystemExit(locked_target(sys.argv[2], sys.argv[3:]))
    preflight()
    root = Path(__file__).resolve().parents[2]
    verify_locks(root)
    verify_unique_test_basenames(root)
    fast = sys.argv[1:] == ['--fast']
    selected = fast_lanes(root) if fast else lanes(root)
    if RUNNER and SKIPPED:
        raise SystemExit('verify: every lane must run on the CI runner; skipped: ' + ', '.join(SKIPPED))
    failures = run_lanes(selected)
    if failures:
        raise SystemExit('verify: failed:\n  ' + '\n  '.join(failures))
    for directory in ('adapters', 'tools'):
        for script in sorted((root / directory).rglob('*.sh')):
            subprocess.run(['bash', '-n', str(script)], check=True)
    if fast:
        print('[verify] fast checks passed; the full gate has not run', flush=True)
        return
    generator = root / 'tools/prepare-public-repo.sh'
    if generator.is_file():
        with tempfile.TemporaryDirectory(prefix='sotto-verify-') as scratch:
            subprocess.run(['bash', str(generator), str(Path(scratch) / 'distribution'),
                            'kothari-nikunj/sotto'], cwd=root, check=True)
    print('[verify] shared runtime and release contracts passed', flush=True)


if __name__ == '__main__':
    main()
