"""Release preflight rejects the dependency drift that previously passed the Python gate.

verify_locks is purely file-based: it never compares anything against what's actually installed
in a running interpreter; exact offline uv regeneration reads only copied lock inputs.
"""
import importlib.util
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('release_verify', ROOT / 'sotto-chief-of-staff/tools/verify.py')
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)

UV_INSTALLED = shutil.which('uv') is not None


def fixture(root):
    for stem in ('requirements', 'requirements-dev'):
        (root / (stem + '.in')).write_text('example==1.0\n')
        (root / (stem + '.txt')).write_text('example==1.0 --hash=sha256:' + 'a' * 64 + '\n')


def real_tree(tmp_path):
    """Copy this repo's actual dependency files into tmp_path, so a case can mutate a copy and
    drive verify_locks exactly as ship.sh does, without touching the real files."""
    for relative in ('requirements', 'requirements-dev', 'cloud/accounts/requirements'):
        for suffix in ('.in', '.txt'):
            source = ROOT / (relative + suffix)
            if not source.exists():
                continue
            destination = tmp_path / (relative + suffix)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
    return tmp_path


def expect_closure_violation(tmp_path, relative='requirements'):
    """Assert verify_locks rejects a lock that outruns its manifest's closure. If uv can't
    regenerate offline in *this* environment (cold local cache, no network -- see
    _RegenUnavailableError), skip instead of asserting a false pass or failing for an unrelated reason:
    this repo's own dev container has the cache warm, which is what proved these cases fail
    against the pre-fix implementation and pass against the fix (see the task's verification run).
    """
    try:
        VERIFY.verify_locks(tmp_path)
    except SystemExit as raised:
        message = str(raised)
        assert 'not what uv resolves' in message or 'dependency resolution failed' in message, message
        return
    try:
        VERIFY._regenerate_locks_offline(tmp_path, [relative])
    except VERIFY._RegenUnavailableError as reason:
        pytest.skip(f'uv could not regenerate offline here ({reason}); cannot exercise this case')
    pytest.fail('verify_locks accepted a lock that is not the closure of its manifest')


def test_committed_lock_files_pass_verify_locks():
    VERIFY.verify_locks(ROOT)


def test_changed_manifest_requires_regeneration(tmp_path):
    fixture(tmp_path)
    (tmp_path / 'requirements.in').write_text('example==2.0\n')
    with pytest.raises(SystemExit, match='stale'):
        VERIFY.verify_locks(tmp_path)


def test_unhashed_transitive_entry_is_rejected(tmp_path):
    fixture(tmp_path)
    with (tmp_path / 'requirements.txt').open('a') as output:
        output.write('transitive==1.2\n')
    with pytest.raises(SystemExit, match='generated hashes'):
        VERIFY.verify_locks(tmp_path)


def test_test_lane_must_include_runtime_transitives(tmp_path):
    fixture(tmp_path)
    with (tmp_path / 'requirements.txt').open('a') as output:
        output.write('transitive==1.2 --hash=sha256:' + 'b' * 64 + '\n')
    with pytest.raises(SystemExit, match='exact runtime lock'):
        VERIFY.verify_locks(tmp_path)


# --- The four verified-finding cases: each one previously passed verify_locks with no error. ---

@pytest.mark.skipif(not UV_INSTALLED, reason='uv not installed; no offline closure check to exercise')
def test_case_a_removed_direct_leaves_orphaned_lock_entry(tmp_path):
    real_tree(tmp_path)
    manifest = tmp_path / 'requirements.in'
    manifest.write_text(''.join(
        line for line in manifest.read_text().splitlines(keepends=True)
        if not line.startswith('pyyaml==')))
    expect_closure_violation(tmp_path)


@pytest.mark.skipif(not UV_INSTALLED, reason='uv not installed; no offline closure check to exercise')
def test_case_e_unrequested_package_appended_to_both_locks(tmp_path):
    real_tree(tmp_path)
    entry = 'evilpkg==1.0 \\\n    --hash=sha256:' + 'c' * 64 + '\n'
    for relative in ('requirements.txt', 'requirements-dev.txt'):
        path = tmp_path / relative
        path.write_text(path.read_text() + entry)
    expect_closure_violation(tmp_path)


@pytest.mark.skipif(not UV_INSTALLED, reason='uv not installed; no offline closure check to exercise')
def test_case_g_transitive_hand_downgraded(tmp_path):
    real_tree(tmp_path)
    for relative in ('requirements.txt', 'requirements-dev.txt'):
        path = tmp_path / relative
        assert 'pyparsing==3.3.2' in path.read_text(), 'fixture assumption: pyparsing is locked at 3.3.2'
        path.write_text(path.read_text().replace('pyparsing==3.3.2', 'pyparsing==2.0.0'))
    expect_closure_violation(tmp_path)


@pytest.mark.skipif(not UV_INSTALLED, reason='uv not installed; no offline closure check to exercise')
def test_case_h_new_direct_dependency_with_transitives_unresolved(tmp_path):
    real_tree(tmp_path)
    manifest = tmp_path / 'requirements.in'
    manifest.write_text(manifest.read_text() + 'httpx==0.28.1\n')
    entry = 'httpx==0.28.1 \\\n    --hash=sha256:' + 'd' * 64 + '\n'
    for relative in ('requirements.txt', 'requirements-dev.txt'):
        path = tmp_path / relative
        path.write_text(path.read_text() + entry)
    expect_closure_violation(tmp_path)


# --- The resolver-free fallback remains sound for universal locks on every platform. ---

def test_real_committed_locks_pass_without_uv(tmp_path, monkeypatch, capsys):
    root = real_tree(tmp_path)
    monkeypatch.setattr(VERIFY.shutil, 'which', lambda *_args, **_kwargs: None)

    VERIFY.verify_locks(root)

    output = capsys.readouterr().out
    assert 'full closure check skipped' in output
    assert 'uv is not installed' in output


def test_real_committed_locks_pass_when_uv_cache_is_cold(tmp_path, monkeypatch, capsys):
    root = real_tree(tmp_path)
    monkeypatch.setattr(VERIFY.shutil, 'which', lambda *_args, **_kwargs: '/fake/uv')

    class FailedCompile:
        returncode = 1
        stderr = ('No solution found when resolving dependencies:\n'
                  'Because pyyaml was not found in the cache.\n'
                  'hint: Packages were unavailable because the network was disabled.\n')

    monkeypatch.setattr(VERIFY.subprocess, 'run', lambda *_args, **_kwargs: FailedCompile())

    VERIFY.verify_locks(root)

    output = capsys.readouterr().out
    assert 'full closure check skipped' in output
    assert 'local uv cache' in output


@pytest.mark.parametrize('diagnostic', [
    'No solution found when resolving dependencies: conflicting requirements',
    "Couldn't parse requirement: no such comparison operator",
])
def test_real_resolver_errors_do_not_become_optional_checks(tmp_path, monkeypatch, diagnostic):
    from types import SimpleNamespace
    root = real_tree(tmp_path)
    monkeypatch.setattr(VERIFY.shutil, 'which', lambda *_args, **_kwargs: '/fake/uv')
    monkeypatch.setattr(VERIFY.subprocess, 'run', lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=1, stderr=diagnostic))

    with pytest.raises(SystemExit, match='dependency resolution failed'):
        VERIFY.verify_locks(root)


@pytest.mark.skipif(not UV_INSTALLED, reason='uv not installed; no resolver conflict to exercise')
def test_conflicting_direct_pins_fail_even_offline(tmp_path):
    fixture(tmp_path)
    (tmp_path / 'requirements.in').write_text('example==0.9\nexample==1.0\n')
    with pytest.raises(SystemExit, match='dependency resolution failed'):
        VERIFY.verify_locks(tmp_path)


def test_regenerate_offline_reports_unavailable_without_uv(tmp_path, monkeypatch):
    fixture(tmp_path)
    monkeypatch.setattr(VERIFY.shutil, 'which', lambda *_args, **_kwargs: None)
    with pytest.raises(VERIFY._RegenUnavailableError, match='uv is not installed'):
        VERIFY._regenerate_locks_offline(tmp_path, ['requirements'])
