"""Replacement epoch persistence and same-volume reentry; no test authorizes a boot."""
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tenant_lifecycle
import replacement_epoch as epoch
import native_replacement


def binding(root):
    meta = root.stat()
    return epoch.binding_for_handoff(
        tenant_id='tenant', volume_id='volume', profile='managed-photon-fixed-five-v1',
        runtime_config_sha256='a' * 64,
        node_argv=('node', 'main.js'), receiver_argv=('python', 'managed_exec.py'),
        node_env={'TOKEN': 'private-one'}, receiver_env={'TOKEN': 'private-two'},
        receiver_expectation={'version': 1, 'channel': 'photon'},
        tenant_root_identity=(meta.st_dev, meta.st_ino))


def save(path, value):
    path.write_text(json.dumps(value, separators=(',', ':')))
    path.chmod(0o600)


def volume_marker(root, *, volume='volume'):
    save(root / '.sotto-volume.json',
         {'schema': 1, 'tenant_id': 'tenant', 'volume_id': volume})


def fixtures(root):
    root.chmod(0o700)
    data = binding(root)
    volume_marker(root)
    save(root / epoch.EPOCH_FILE, epoch.record_for_launch(data))
    return data


def reenter(root, data):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return epoch._verify_existing_epoch(fd, data, supervisor_uid=os.geteuid())
    finally:
        os.close(fd)


def test_epoch_records_exact_role_projection_without_raw_secrets(tmp_path):
    first = binding(tmp_path)
    second = epoch.binding_for_handoff(
        tenant_id='tenant', volume_id='volume', profile='managed-photon-fixed-five-v1',
        runtime_config_sha256='a' * 64,
        node_argv=('node', 'main.js'), receiver_argv=('python', 'managed_exec.py'),
        node_env={'TOKEN': 'different'}, receiver_env={'TOKEN': 'private-two'},
        receiver_expectation={'version': 1, 'channel': 'photon'},
        tenant_root_identity=(tmp_path.stat().st_dev, tmp_path.stat().st_ino))
    assert first['role_projection_sha256'] != second['role_projection_sha256']
    assert set(first) == {'tenant_id', 'volume_id', 'profile', 'runtime_config_sha256',
                          'role_projection_sha256', 'receiver_expectation_sha256',
                          'tenant_root_identity'}
    record = epoch.record_for_launch(first)
    assert record['version'] == 3 and len(record['epoch_id']) == 32
    assert 'private-one' not in json.dumps(record)


def test_supervisor_writes_private_no_clobber_hold_before_launch(tmp_path, monkeypatch):
    # Isolate the persistence method; kernel flock/process proof is covered by
    # the existing private-PID lifecycle fixture, not forged by this unit test.
    supervisor = object.__new__(tenant_lifecycle.Lifecycle)
    supervisor._root_path = tmp_path
    supervisor._root_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    supervisor._assert_lock = lambda: None
    data = binding(tmp_path)
    volume_marker(tmp_path)
    monkeypatch.setattr(epoch, 'verify_volume_marker',
                        lambda fd, supplied: epoch._verify_volume_marker(
                            fd, supplied, supervisor_uid=os.geteuid()))
    supervisor._hold_replacement_epoch(data)
    path = tmp_path / epoch.EPOCH_FILE
    record = json.loads(path.read_text())
    assert record['version'] == 3 and record['binding'] == data
    assert path.stat().st_mode & 0o777 == 0o600 and path.stat().st_nlink == 1
    with pytest.raises(tenant_lifecycle.LifecycleUnavailableError):
        supervisor._hold_replacement_epoch(data)
    assert json.loads(path.read_text()) == record


def _launch_fixture_supervisor(root, *, reentry=False):
    supervisor = object.__new__(tenant_lifecycle.Lifecycle)
    supervisor._root_path = root
    supervisor._root_identity = (root.stat().st_dev, root.stat().st_ino)
    supervisor._native_uid = 10001
    supervisor._reentry_epoch = reentry
    supervisor._reentry_epoch_id = None
    supervisor._replacement_epoch_id = None
    supervisor._state = 'quiescent'
    supervisor._replacement_leaders = {}
    supervisor._replacement_children = []
    supervisor._assert_lock = lambda: None
    supervisor._assert_empty = lambda: None
    supervisor._replacement_tree = lambda: None
    return supervisor


def _capture_launches(monkeypatch):
    launches = []

    class Child:
        def __init__(self, pid):
            self.pid = pid

    def popen(argv, **kwargs):
        launches.append((argv, dict(kwargs['env'])))
        return Child(len(launches))

    monkeypatch.setattr(tenant_lifecycle.subprocess, 'Popen', popen)
    monkeypatch.setattr(tenant_lifecycle, '_read_process', lambda pid: SimpleNamespace(
        uid=10000 if pid % 2 else 10001, starttime=pid + 100))
    return launches


def test_receiver_gets_only_the_persisted_verified_epoch_on_fresh_and_restart(tmp_path, monkeypatch):
    data = binding(tmp_path)
    volume_marker(tmp_path)
    monkeypatch.setattr(epoch, 'verify_volume_marker',
                        lambda fd, supplied: epoch._verify_volume_marker(
                            fd, supplied, supervisor_uid=os.geteuid()))
    monkeypatch.setattr(epoch, 'verify_existing_epoch',
                        lambda fd, supplied: epoch._verify_existing_epoch(
                            fd, supplied, supervisor_uid=os.geteuid()))
    launches = _capture_launches(monkeypatch)
    node_env = {'NODE_ONLY': 'yes', 'SOTTO_ROUTINE_EPOCH': 'a' * 32}
    receiver_env = {'RECEIVER_ONLY': 'yes', 'SOTTO_ROUTINE_EPOCH': 'b' * 32}

    fresh = _launch_fixture_supervisor(tmp_path)
    fresh._launch_replacement_roles(('node', 'main.js'), ('python', 'receiver.py'),
                                    node_env, receiver_env, epoch_binding=data,
                                    reuse_epoch=False)
    epoch_id = json.loads((tmp_path / epoch.EPOCH_FILE).read_text())['epoch_id']
    assert re.fullmatch(r'[a-f0-9]{32}', epoch_id)
    assert 'SOTTO_ROUTINE_EPOCH' not in launches[0][1]
    assert launches[1][1]['SOTTO_ROUTINE_EPOCH'] == epoch_id

    resumed = _launch_fixture_supervisor(tmp_path, reentry=True)
    resumed._launch_replacement_roles(('node', 'main.js'), ('python', 'receiver.py'),
                                      node_env, receiver_env, epoch_binding=data,
                                      reuse_epoch=True)
    assert 'SOTTO_ROUTINE_EPOCH' not in launches[2][1]
    assert launches[3][1]['SOTTO_ROUTINE_EPOCH'] == epoch_id


def test_generic_epochless_launch_discards_caller_epoch_in_both_roles(tmp_path, monkeypatch):
    supervisor = _launch_fixture_supervisor(tmp_path)
    launches = _capture_launches(monkeypatch)
    supervisor._launch_replacement_roles(
        ('node', 'main.js'), ('python', 'receiver.py'),
        {'SOTTO_ROUTINE_EPOCH': 'a' * 32}, {'SOTTO_ROUTINE_EPOCH': 'b' * 32},
        epoch_binding=None, reuse_epoch=False)
    record = json.loads((tmp_path / tenant_lifecycle.runtime_lock.EPOCH_FILE).read_text())
    assert record == {'version': 1, 'status': 'reconciliation_required'}
    assert 'SOTTO_ROUTINE_EPOCH' not in launches[0][1]
    assert 'SOTTO_ROUTINE_EPOCH' not in launches[1][1]


def test_receiver_boot_output_reaches_supervisor_logs(tmp_path, monkeypatch):
    supervisor = _launch_fixture_supervisor(tmp_path)
    calls = []

    def popen(argv, **kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            assert kwargs['pass_fds'] == (int(kwargs['env']['SOTTO_SETUP_LOG_FD']),)
            os.fstat(kwargs['pass_fds'][0])  # the child receives a live descriptor
        return SimpleNamespace(pid=len(calls))

    monkeypatch.setattr(tenant_lifecycle.subprocess, 'Popen', popen)
    monkeypatch.setattr(tenant_lifecycle, '_read_process', lambda pid: SimpleNamespace(
        uid=10000 if pid == 1 else 10001, starttime=pid + 100))
    supervisor._launch_replacement_roles(
        ('node', 'main.js'), ('python', 'receiver.py'), {}, {'SOTTO_DEPLOYMENT_MODE': 'self_host'},
        epoch_binding=None, reuse_epoch=False)
    assert calls[0]['stdout'] == calls[0]['stderr'] == tenant_lifecycle.subprocess.DEVNULL
    assert calls[1]['stdout'] == calls[1]['stderr'] == tenant_lifecycle.subprocess.DEVNULL
    assert calls[1]['stdin'] == tenant_lifecycle.subprocess.DEVNULL
    with pytest.raises(OSError):
        os.fstat(calls[1]['pass_fds'][0])  # parent does not retain the child log descriptor


@pytest.mark.parametrize('mode', ['managed', None])
def test_managed_or_unspecified_launch_never_gets_the_setup_log(tmp_path, monkeypatch, mode):
    # A managed tenant's deployment log belongs to the operator, never the owner's setup code.
    supervisor = _launch_fixture_supervisor(tmp_path)
    calls = []
    monkeypatch.setattr(tenant_lifecycle.subprocess, 'Popen',
                        lambda argv, **kwargs: calls.append(kwargs) or SimpleNamespace(pid=len(calls)))
    monkeypatch.setattr(tenant_lifecycle, '_read_process', lambda pid: SimpleNamespace(
        uid=10000 if pid == 1 else 10001, starttime=pid + 100))
    receiver_env = {'SOTTO_SETUP_LOG_FD': '1'} | ({'SOTTO_DEPLOYMENT_MODE': mode} if mode else {})
    supervisor._launch_replacement_roles(('node', 'main.js'), ('python', 'receiver.py'), {}, receiver_env,
                                         epoch_binding=None, reuse_epoch=False)
    assert calls[1]['pass_fds'] == ()
    assert 'SOTTO_SETUP_LOG_FD' not in calls[1]['env']


def test_partial_epoch_write_fails_before_hold_or_child(tmp_path, monkeypatch):
    supervisor = object.__new__(tenant_lifecycle.Lifecycle)
    supervisor._root_path = tmp_path
    supervisor._root_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    supervisor._assert_lock = lambda: None
    data = binding(tmp_path)
    volume_marker(tmp_path)
    monkeypatch.setattr(epoch, 'verify_volume_marker',
                        lambda fd, supplied: epoch._verify_volume_marker(
                            fd, supplied, supervisor_uid=os.geteuid()))
    original = os.write
    with monkeypatch.context() as patcher:
        patcher.setattr(tenant_lifecycle.os, 'write',
                        lambda fd, raw: original(fd, raw[:-1]))
        with pytest.raises(tenant_lifecycle.LifecycleUnavailableError):
            supervisor._hold_replacement_epoch(data)
    assert not (tmp_path / epoch.EPOCH_FILE).exists()
    assert not any(path.name.startswith('.sotto-epoch-')
                   for path in tmp_path.iterdir())


def test_volume_marker_must_match_pinned_root_and_volume(tmp_path):
    data = binding(tmp_path)
    volume_marker(tmp_path, volume='other-volume')
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(epoch.EpochUnavailableError):
            epoch._verify_volume_marker(fd, data, supervisor_uid=os.geteuid())
        save(tmp_path / '.sotto-volume.json',
             {'schema': True, 'tenant_id': 'tenant', 'volume_id': 'volume'})
        with pytest.raises(epoch.EpochUnavailableError):
            epoch._verify_volume_marker(fd, data, supervisor_uid=os.geteuid())
        (tmp_path / '.sotto-volume.json').write_text(
            '{"schema":1,"schema":1,"tenant_id":"tenant","volume_id":"volume"}')
        with pytest.raises(epoch.EpochUnavailableError):
            epoch._verify_volume_marker(fd, data, supervisor_uid=os.geteuid())
        volume_marker(tmp_path)
        epoch._verify_volume_marker(fd, data, supervisor_uid=os.geteuid())
        (tmp_path / '.sotto-volume.json').unlink()
        (tmp_path / '.sotto-volume.json').symlink_to('/nowhere')
        with pytest.raises(epoch.EpochUnavailableError):
            epoch._verify_volume_marker(fd, data, supervisor_uid=os.geteuid())
    finally:
        os.close(fd)


def test_marker_read_stays_on_pinned_inode_after_path_swap(tmp_path):
    root = tmp_path / 'tenant'
    root.mkdir()
    data = binding(root)
    volume_marker(root, volume='wrong')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root.rename(tmp_path / 'old-tenant')
        root.mkdir()
        volume_marker(root)
        with pytest.raises(epoch.EpochUnavailableError):
            epoch._verify_volume_marker(fd, data, supervisor_uid=os.geteuid())
    finally:
        os.close(fd)


def test_same_epoch_identity_returns_the_epoch_without_rewriting_it(tmp_path):
    data = fixtures(tmp_path)
    path = tmp_path / epoch.EPOCH_FILE
    before = path.read_bytes()
    assert reenter(tmp_path, data) == json.loads(before)['epoch_id']
    assert path.read_bytes() == before


def test_restart_after_rotated_config_or_volume_reattach_keeps_the_epoch(tmp_path):
    # Rotating a token or Google key, a receiverOverrides model/timezone change, a release that
    # changes the rendered config, or a reattach with a new st_dev used to refuse every restart.
    current = fixtures(tmp_path)
    dev, inode = current['tenant_root_identity']
    first_launch = epoch.binding_for_handoff(
        tenant_id='tenant', volume_id='volume', profile='managed-photon-fixed-five-v1',
        runtime_config_sha256='e' * 64,
        node_argv=('node', 'main.js'), receiver_argv=('python', 'managed_exec.py'),
        node_env={'TOKEN': 'before-rotation'},
        receiver_env={'TOKEN': 'private-two', 'SOTTO_GEMINI_MODEL': 'gemini-a'},
        receiver_expectation={'version': 1, 'channel': 'photon'},
        tenant_root_identity=(dev + 1, inode))     # the volume's previous device number
    assert first_launch['role_projection_sha256'] != current['role_projection_sha256']
    save(tmp_path / epoch.EPOCH_FILE, epoch.record_for_launch(first_launch))
    path = tmp_path / epoch.EPOCH_FILE
    before = path.read_bytes()
    epoch_id = reenter(tmp_path, current)
    # Same epoch (routine bindings keep it), and the hold is never rewritten by a restart.
    assert epoch_id == json.loads(before)['epoch_id'] and path.read_bytes() == before


@pytest.mark.parametrize('field,value', [
    ('tenant_id', 'other-tenant'), ('volume_id', 'other-volume'),
    ('profile', 'self-host-photon-fixed-five-v1'), ('receiver_expectation_sha256', 'f' * 64),
    ('tenant_root_identity', 'inode'),
])
def test_restart_identity_change_still_refuses(tmp_path, field, value):
    data = fixtures(tmp_path)
    if field in ('tenant_id', 'volume_id'):
        save(tmp_path / '.sotto-volume.json', {'schema': 1,
             'tenant_id': value if field == 'tenant_id' else 'tenant',
             'volume_id': value if field == 'volume_id' else 'volume'})
    if field == 'tenant_root_identity':
        # A different directory: forge only the saved inode so the live marker check still passes.
        value = data[field]
        record = json.loads((tmp_path / epoch.EPOCH_FILE).read_text())
        record['binding'][field] = [value[0], value[1] + 1]
        save(tmp_path / epoch.EPOCH_FILE, record)
    else:
        data[field] = value
    with pytest.raises(epoch.EpochUnavailableError, match='binding changed'):
        reenter(tmp_path, data)


@pytest.mark.parametrize('change', ['binding', 'marker', 'duplicate', 'legacy', 'version2',
                                   'symlink', 'hardlink', 'mode'])
def test_same_epoch_reentry_rejects_changed_or_untrusted_identity(tmp_path, change):
    data = fixtures(tmp_path)
    path = tmp_path / epoch.EPOCH_FILE
    if change == 'binding':
        data['receiver_expectation_sha256'] = 'f' * 64
    elif change == 'marker':
        volume_marker(tmp_path, volume='other')
    elif change == 'duplicate':
        path.write_bytes(path.read_bytes().replace(b'"version":3', b'"version":3,"version":3'))
    elif change == 'legacy':
        save(path, {'version': 1, 'status': 'reconciliation_required'})
    elif change == 'version2':
        # A version-2 record (it still bound a native schedule store) is refused, not upgraded.
        record = json.loads(path.read_text())
        record['version'] = 2
        record['binding'] = dict(record['binding'], native_store_sha256='b' * 64,
                                 native_profile_identity=[1, 2])
        save(path, record)
    elif change == 'symlink':
        path.rename(tmp_path / 'original')
        path.symlink_to('original')
    elif change == 'hardlink':
        os.link(path, tmp_path / 'other')
    elif change == 'mode':
        path.chmod(0o640)
    with pytest.raises(epoch.EpochUnavailableError):
        reenter(tmp_path, data)


def test_verified_handoff_passes_epoch_before_first_child(monkeypatch, tmp_path):
    receiver_env = {'SOTTO_TENANT_ID': 'tenant', 'SOTTO_VOLUME_ID': 'volume',
                    'PRIVATE': 'do-not-save'}
    handoff = SimpleNamespace(
        node_argv=('node', 'main.js'), receiver_argv=('python', 'managed_exec.py'),
        node_env={'PRIVATE': 'also-do-not-save'}, receiver_env=receiver_env,
        receiver_channel_expectation={'version': 1, 'channel': 'photon'},
        runtime_config_sha256='a' * 64)
    class Lifecycle:
        _root_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
        def assert_replacement_root(self, root):
            assert root == str(tmp_path)
        def _start_replacement_roles(self, *args, **kwargs):
            assert args[:2] == (handoff.node_argv, handoff.receiver_argv)
            self.record = epoch.record_for_launch(kwargs['epoch_binding'])
    lifecycle = Lifecycle()
    monkeypatch.setattr(native_replacement, 'verified_handoff', lambda *a, **k: handoff)
    monkeypatch.setattr(native_replacement.fixed_five, 'validate_offline', lambda *a, **k: None)
    native_replacement.start_configured_replacement(
        lifecycle, {}, {}, stable_identity={}, node_config_path='/staged',
        data_directory=str(tmp_path), skills_root='/app/sotto-skills',
        limited_profile=native_replacement.fixed_five.PROFILE)
    assert lifecycle.record['version'] == 3
    assert lifecycle.record['binding']['tenant_root_identity'] == list(Lifecycle._root_identity)
    assert not {'native_store_sha256', 'native_profile_identity'} & set(lifecycle.record['binding'])
    assert 'do-not-save' not in json.dumps(lifecycle.record)
