"""The provisioner launcher must never start writers on an unverified volume."""

import json

import pytest

import managed_start as launcher


TENANT = 'tenant-a'
VOLUME = '12345678-1234-1234-1234-123456789abc'


class ExecObservedError(Exception):
    pass


@pytest.fixture
def managed_env():
    return {
        'SOTTO_DEPLOYMENT_MODE': 'managed',
        'SOTTO_TENANT_ID': TENANT,
        'SOTTO_VOLUME_ID': VOLUME,
        'RAILWAY_VOLUME_ID': VOLUME,
        'RAILWAY_VOLUME_MOUNT_PATH': '/data',
        'SOTTO_DATA': '/data',
    }


def launch(data, managed_env, *, ismount=None, execv=None):
    return launcher.launch(
        TENANT, VOLUME, environ=managed_env, data=data,
        ismount=(lambda path: path == str(data)) if ismount is None else ismount,
        execv=(lambda *_: pytest.fail('runtime exec before volume verification'))
        if execv is None else execv,
    )


def test_first_launch_initializes_only_empty_mounted_volume_then_execs(tmp_path, managed_env):
    (tmp_path / 'lost+found').mkdir()
    calls = []

    def record_exec(path, argv):
        calls.append((path, argv))
        raise ExecObservedError

    with pytest.raises(ExecObservedError):
        launch(tmp_path, managed_env, execv=record_exec)

    assert calls == [(launcher.PYTHON, launcher.RUNTIME)]
    assert json.loads((tmp_path / launcher.MARKER).read_text()) == {
        'schema': 1, 'tenant_id': TENANT, 'volume_id': VOLUME,
    }
    assert (tmp_path / launcher.MARKER).stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.sotto-volume-check-*'))


def test_matching_restart_reverifies_then_execs(tmp_path, managed_env):
    launcher.initialize(tmp_path, TENANT, VOLUME, ismount=lambda _: True)
    marker = tmp_path / launcher.MARKER
    original = marker.read_bytes()
    calls = []

    def record_exec(path, argv):
        calls.append((path, argv))
        raise ExecObservedError

    with pytest.raises(ExecObservedError):
        launch(tmp_path, managed_env, execv=record_exec)

    assert calls == [(launcher.PYTHON, launcher.RUNTIME)]
    assert marker.read_bytes() == original


@pytest.mark.parametrize('key,value', [
    ('SOTTO_DEPLOYMENT_MODE', 'self-host'),
    ('SOTTO_TENANT_ID', 'another'),
    ('SOTTO_VOLUME_ID', 'another'),
    ('RAILWAY_VOLUME_ID', 'another'),
    ('RAILWAY_VOLUME_ID', None),
    ('RAILWAY_VOLUME_MOUNT_PATH', '/other'),
    ('SOTTO_DATA', '/other'),
])
def test_wrong_managed_environment_fails_before_marker_write(tmp_path, managed_env, key, value):
    if value is None:
        managed_env.pop(key)
    else:
        managed_env[key] = value
    with pytest.raises(RuntimeError, match='environment does not match'):
        launch(tmp_path, managed_env)
    assert not (tmp_path / launcher.MARKER).exists()


def test_nonmount_and_symlinked_mount_fail_closed(tmp_path, managed_env):
    with pytest.raises(RuntimeError, match='persistent mount point'):
        launch(tmp_path, managed_env, ismount=lambda _: False)
    assert not (tmp_path / launcher.MARKER).exists()

    target = tmp_path / 'target'
    target.mkdir()
    redirected = tmp_path / 'redirected'
    redirected.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match='persistent mount point'):
        launch(redirected, managed_env)
    assert not (target / launcher.MARKER).exists()


@pytest.mark.parametrize('entry', ['config', 'hermes', 'arbitrary.txt'])
def test_populated_unmarked_volume_is_never_adopted(tmp_path, managed_env, entry):
    (tmp_path / entry).write_text('existing tenant data')
    with pytest.raises(RuntimeError, match='Unmarked managed volume is populated'):
        launch(tmp_path, managed_env)
    assert not (tmp_path / launcher.MARKER).exists()


def test_lost_and_found_must_be_a_real_directory(tmp_path, managed_env):
    (tmp_path / 'lost+found').write_text('data')
    with pytest.raises(RuntimeError, match='Unmarked managed volume is populated'):
        launch(tmp_path, managed_env)
    assert not (tmp_path / launcher.MARKER).exists()


@pytest.mark.parametrize('contents', [
    {'schema': 1, 'tenant_id': 'other', 'volume_id': VOLUME},
    {'schema': 1, 'tenant_id': TENANT, 'volume_id': 'other'},
    {'schema': 2, 'tenant_id': TENANT, 'volume_id': VOLUME},
    'not JSON',
])
def test_wrong_or_corrupt_marker_fails_closed(tmp_path, managed_env, contents):
    marker = tmp_path / launcher.MARKER
    marker.write_text(json.dumps(contents) if isinstance(contents, dict) else contents)
    with pytest.raises(RuntimeError, match='does not match|not been provisioned'):
        launch(tmp_path, managed_env)
    assert marker.read_text() == (json.dumps(contents) if isinstance(contents, dict) else contents)


def test_symlinked_marker_fails_closed(tmp_path, managed_env):
    outside = tmp_path.parent / 'outside-marker'
    outside.write_text(json.dumps({'schema': 1, 'tenant_id': TENANT, 'volume_id': VOLUME}))
    (tmp_path / launcher.MARKER).symlink_to(outside)
    with pytest.raises(RuntimeError, match='symlink'):
        launch(tmp_path, managed_env)
    assert outside.is_file()


def test_exec_failure_is_reported_without_starting_runtime(tmp_path, managed_env):
    calls = []

    def fail_exec(path, argv):
        calls.append((path, argv))
        raise OSError('exec failed')

    with pytest.raises(OSError, match='exec failed'):
        launch(tmp_path, managed_env, execv=fail_exec)
    assert calls == [(launcher.PYTHON, launcher.RUNTIME)]
    assert launcher.verify(tmp_path, TENANT, VOLUME, ismount=lambda _: True)['ready']


def test_unexpected_exec_return_fails_closed(tmp_path, managed_env):
    with pytest.raises(RuntimeError, match='exec returned unexpectedly'):
        launch(tmp_path, managed_env, execv=lambda *_: None)
