"""Synthetic cold-restore drill; never opens the owner's volume or credentials."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile

import pytest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m, runtime = load('recovery'), load('runtime_lock')


def seed(tmp_path):
    data = tmp_path / 'source'
    (data / 'knowledge/people').mkdir(parents=True)
    (data / 'config').mkdir()
    (data / 'events').mkdir()
    (data / 'hermes').mkdir()
    (data / m.LOCK).touch(mode=0o600)
    (data / '.sotto-volume.json').write_text(json.dumps({'schema': 1, 'tenant_id': 'fixture', 'volume_id': 'old-volume'}))
    (data / 'config/managed-capabilities.json').write_text(json.dumps({'tenant_id': 'fixture'}))
    (data / 'knowledge/people/maya.md').write_text('# Maya\n\nFixture durable fact, explicitly corrected.\n')
    (data / 'hermes/google_token.json').write_text('{"refresh_token":"fixture-only-refresh"}')
    (data / 'events/outbox.json').write_text('{"rows":[{"key":"fixture-offer","status":"pending"}]}')
    (data / 'knowledge/latest.md').symlink_to('people/maya.md')
    with sqlite3.connect(data / 'events/operations.sqlite') as db:
        db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT)')
        db.execute('INSERT INTO jobs VALUES(?,?)', ('fixture-brief', 'queued'))
    return data


def test_cold_restore_preserves_identity_memory_work_and_credentials_but_cannot_send(tmp_path):
    data = seed(tmp_path)
    archive = tmp_path / 'backup.tgz'
    receipt = m.export(data, archive, 'fixture')
    assert archive.stat().st_mode & 0o777 == 0o600
    target = tmp_path / 'restored'
    result = m.restore(archive, target, 'fixture', volume_id='replacement-volume')
    assert result['on_hold'] and result['manifest_sha256'] == receipt['manifest_sha256']
    assert (target / 'knowledge/latest.md').read_text() == (data / 'knowledge/people/maya.md').read_text()
    assert json.loads((target / '.sotto-volume.json').read_text())['volume_id'] == 'replacement-volume'
    assert (target / 'hermes/google_token.json').read_text() == (data / 'hermes/google_token.json').read_text()
    assert json.loads((target / 'events/outbox.json').read_text())['rows'][0]['status'] == 'pending'
    with sqlite3.connect(target / 'events/operations.sqlite') as db:
        assert db.execute('SELECT * FROM jobs').fetchall() == [('fixture-brief', 'queued')]
    with pytest.raises(RuntimeError, match='recovery hold'):
        runtime.acquire(target)
    with pytest.raises(ValueError, match='does not match'):
        m.resume(target, 'fixture', 'wrong-receipt')
    assert m.resume(target, 'fixture', receipt['manifest_sha256'])['on_hold'] is False
    fd = runtime.acquire(target)
    os.close(fd)


def test_running_tenant_cannot_be_exported_and_empty_mount_can_be_restored(tmp_path):
    data = seed(tmp_path)
    fd = runtime.acquire(data)
    try:
        with pytest.raises(RuntimeError, match='Stop the tenant runtime'):
            m.export(data, tmp_path / 'live.tgz', 'fixture')
    finally:
        os.close(fd)
    archive = tmp_path / 'offline.tgz'
    m.export(data, archive, 'fixture')
    mount = tmp_path / 'empty-mount'
    mount.mkdir()
    assert m.restore(archive, mount, 'fixture')['on_hold']
    with pytest.raises(ValueError, match='empty target'):
        m.restore(archive, mount, 'fixture')


def test_wrong_tenant_or_tampered_archive_does_not_publish_state(tmp_path):
    data = seed(tmp_path)
    archive = tmp_path / 'backup.tgz'
    m.export(data, archive, 'fixture')
    with pytest.raises(ValueError, match='tenant or schema'):
        m.restore(archive, tmp_path / 'wrong', 'other')
    assert not (tmp_path / 'wrong').exists()
    tampered = tmp_path / 'tampered.tgz'
    with tarfile.open(archive, 'r:gz') as original, tarfile.open(tampered, 'w:gz') as changed:
        for member in original.getmembers():
            if member.name == 'knowledge/people/maya.md':
                raw = b'X' * member.size
                changed.addfile(member, io.BytesIO(raw))
            elif member.isfile():
                changed.addfile(member, original.extractfile(member))
            else:
                changed.addfile(member)
    with pytest.raises(ValueError, match='checksum'):
        m.restore(tampered, tmp_path / 'bad', 'fixture')
    assert not (tmp_path / 'bad').exists()


def test_archive_path_traversal_and_external_links_are_rejected(tmp_path):
    archive = tmp_path / 'evil.tgz'
    with tarfile.open(archive, 'w:gz') as tar:
        member = tarfile.TarInfo('../escape')
        member.size = 1
        tar.addfile(member, io.BytesIO(b'x'))
    with pytest.raises(ValueError, match='Unsafe archive path'):
        m.restore(archive, tmp_path / 'target', 'fixture')
    assert not (tmp_path / 'escape').exists()
    data = seed(tmp_path)
    (data / 'external').symlink_to('/etc/hosts')
    with pytest.raises(ValueError, match='external or directory'):
        m.export(data, tmp_path / 'external.tgz', 'fixture')


def test_runtime_rechecks_hold_after_acquiring_volume_lock(tmp_path, monkeypatch):
    """A completed restore between the fast check and flock cannot bypass activation review."""
    original = runtime.fcntl.flock
    def hold_then_lock(fd, flags):
        original(fd, flags)
        (tmp_path / runtime.HOLD).write_text('{}')
    monkeypatch.setattr(runtime.fcntl, 'flock', hold_then_lock)
    with pytest.raises(RuntimeError, match='recovery hold'):
        runtime.acquire(tmp_path)
