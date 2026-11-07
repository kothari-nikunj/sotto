"""Offline fresh-volume admission tests; kernel mount proof runs in the image fixture."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from unittest.mock import patch

import pytest

import prepare_fresh as fresh


def _bundle() -> dict:
    project = 'synthetic-project'
    owner = '+15551234567'
    line = '+15557654321'
    manifest = {
        'version': 1, 'deployment': 'managed', 'channel': 'photon',
        'tenantId': 'synthetic-tenant', 'owner': {'accountId': 'synthetic-account',
                                               'ownerId': owner},
        'lineKey': 'physical-line-a', 'target': 'photon',
        'firstContact': {'mode': 'owner_dm', 'expectedProviderLine': line},
        'photonLineNumber': line, 'stateDirectory': '/data/node-state',
        'mediaRoot': '/data/node-media', 'instructions': 'Synthetic offline fixture.',
        'runtime': {'port': 19981, 'bearerToken': 'runtime-token-fixture-0123456789012345'},
        'relay': {'receiverPort': 19982, 'rootBearer': 'relay-root-fixture-0123456789012345'},
        'modelProxy': {'origin': 'https://proxy.example',
                       'bearer': 'proxy-fixture-token-0123456789012345',
                       'chatModelId': 'chat-v1', 'nativeMediaModelId': 'media-v1'},
        'credentials': {'mode': 'cloud', 'photonProjectId': project,
                        'photonProjectSecret': 'project-secret-fixture'},
    }
    return {
        'version': 1, 'tenantRoot': '/data',
        'mount': {'mountPoint': '/data', 'source': '/dev/fake-ext4',
                  'fsType': 'ext4', 'major': 1, 'minor': 2},
        'manifest': manifest,
        'receiverAuthority': {
            'SOTTO_MCP_TOKEN': manifest['relay']['rootBearer'],
            'SOTTO_CONTROL_TOKEN': 'control-fixture-0123456789012345',
            'SOTTO_MODEL_PROXY_URL': manifest['modelProxy']['origin'],
            'SOTTO_MODEL_PROXY_TOKEN': manifest['modelProxy']['bearer'],
            'SOTTO_TENANT_ID': 'synthetic-tenant', 'SOTTO_DATA': '/data',
            'SOTTO_HOME': '/data/sotto-home', 'SOTTO_VOLUME_ID': 'synthetic-volume-id',
        },
        'stableIdentity': {
            'channel': 'photon', 'target': 'photon', 'tenantId': 'synthetic-tenant',
            'accountId': 'synthetic-account', 'ownerId': owner,
            'lineKey': 'physical-line-a', 'providerThreadId': None,
            'expectedProviderLine': line,
            'projectFingerprint': hashlib.sha256(
                ('sotto-photon-project-v1:' + project).encode()).hexdigest(),
        },
        'publicReceiverAuthority': {
            'tenantId': 'synthetic-tenant', 'serviceId': 'synthetic-service',
            'domain': 'synthetic.example', 'targetPort': 19982,
        },
        'trustedServiceId': 'synthetic-service',
        'receiverOverrides': {'SOTTO_TIMEZONE': 'UTC'},
    }


def test_real_handoff_validation_before_mutation():
    value = _bundle()
    with (patch.object(fresh.root_supervisor, '_private_input', return_value=value),
          patch.object(fresh.fixed_five, 'validate_environment')):
        mount, manifest, authority = fresh._bundle('/unused/private/input.json')
        assert mount == value['mount']
        assert manifest is value['manifest']
        assert authority is value['receiverAuthority']
        for mutate in (
            lambda item: item['manifest'].update(deployment='self_host'),
            lambda item: item['manifest'].update(providerThreadId='imessage:wrong'),
            lambda item: item['stableIdentity'].update(ownerId='+15550000000'),
            lambda item: item.update(tenantRoot='/other'),
        ):
            changed = json.loads(json.dumps(value))
            mutate(changed)
            with patch.object(fresh.root_supervisor, '_private_input', return_value=changed):
                with pytest.raises(ValueError):
                    fresh._bundle('/unused/private/input.json')


def _host_root(tmp_path, monkeypatch):
    root = tmp_path / 'fresh'
    root.mkdir(mode=0o700)
    value = _bundle()
    monkeypatch.setattr(fresh, 'ROOT', root)
    monkeypatch.setattr(fresh, '_bundle',
                        lambda _: (value['mount'], value['manifest'],
                                   value['receiverAuthority']))
    monkeypatch.setattr(fresh, '_empty_private_namespace', lambda: None)
    monkeypatch.setattr(fresh, '_mount', lambda _fd, _expected, pinned=None: pinned or (1, 2, 3))
    original_fstat = os.fstat
    def root_fstat(fd):
        found = original_fstat(fd)
        if stat.S_ISDIR(found.st_mode) or stat.S_ISREG(found.st_mode):
            data = list(found)
            data[4:6] = [0, 0]
            return os.stat_result(data)
        return found
    monkeypatch.setattr(fresh.os, 'fstat', root_fstat)
    monkeypatch.setattr(fresh.os, 'fchown', lambda *_: None)
    return root


def test_marker_last_no_overwrite_and_partial_failure(tmp_path, monkeypatch):
    root = _host_root(tmp_path, monkeypatch)
    writes = []
    original = fresh._write_new
    def record(parent_fd, name, payload, uid, gid):
        if name == '.sotto-volume.json':
            assert (root / 'sotto-home').is_dir()
            assert (root / fresh.LOCK).is_file()
        writes.append(name)
        return original(parent_fd, name, payload, uid, gid)
    monkeypatch.setattr(fresh, '_write_new', record)
    fresh.prepare('/unused')
    # Only the marker is written: no schedule store is minted for a fresh volume.
    assert writes == ['.sotto-volume.json']
    assert stat.S_IMODE((root / 'sotto-home').stat().st_mode) == 0o700
    assert list((root / 'sotto-home').iterdir()) == []
    assert sorted(path.name for path in root.iterdir()) == sorted(
        [fresh.LOCK, '.sotto-volume.json', 'sotto-home'])
    before = (root / '.sotto-volume.json').read_bytes()
    with pytest.raises(fresh.FreshPreparationUnavailableError):
        fresh.prepare('/unused')
    assert (root / '.sotto-volume.json').read_bytes() == before

    second = tmp_path / 'interrupted'
    second.mkdir(mode=0o700)
    monkeypatch.setattr(fresh, 'ROOT', second)
    def fail_marker(parent_fd, name, payload, uid, gid):
        if name == '.sotto-volume.json':
            raise OSError('injected interruption')
        return original(parent_fd, name, payload, uid, gid)
    monkeypatch.setattr(fresh, '_write_new', fail_marker)
    with pytest.raises(fresh.FreshPreparationUnavailableError):
        fresh.prepare('/unused')
    assert not (second / '.sotto-volume.json').exists()
    assert (second / 'sotto-home').is_dir()
    with pytest.raises(fresh.FreshPreparationUnavailableError):
        fresh.prepare('/unused')


def test_zero_write_fails_bounded(tmp_path, monkeypatch):
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(fresh.os, 'write', lambda *_: 0)
        monkeypatch.setattr(fresh.os, 'fchown', lambda *_: None)
        with pytest.raises(OSError, match='incomplete'):
            fresh._write_new(parent, 'partial', b'data', 0, 0)
    finally:
        os.close(parent)


def test_existing_content_refuses_before_claim(tmp_path, monkeypatch):
    root = _host_root(tmp_path, monkeypatch)
    (root / 'foreign').write_text('must not be changed')
    with pytest.raises(fresh.FreshPreparationUnavailableError):
        fresh.prepare('/unused')
    assert not (root / fresh.LOCK).exists()
    assert (root / 'foreign').read_text() == 'must not be changed'


def test_isolated_direct_cli_uses_only_installed_package(tmp_path):
    installed = tmp_path / 'adapters/sotto'
    installed.mkdir(parents=True)
    source = Path(fresh.root_supervisor.__file__).resolve().parent
    for sibling in source.glob('*.py'):
        if sibling.name != 'prepare_fresh.py':
            (installed / sibling.name).symlink_to(sibling)
    (installed.parent / 'sotto-home').symlink_to(source.parent / 'sotto-home')
    shutil.copy2(fresh.__file__, installed / 'prepare_fresh.py')
    result = subprocess.run(
        [sys.executable, '-I', '-B', str(installed / 'prepare_fresh.py'),
         '--invalid'],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ''
    assert result.stderr.strip() == '[sotto] fresh preparation unavailable'


def test_only_empty_ext4_lost_found_is_preserved(tmp_path, monkeypatch):
    root = _host_root(tmp_path, monkeypatch)
    formatter = root / 'lost+found'
    formatter.mkdir(mode=0o700)
    original_inode = formatter.stat().st_ino
    fresh.prepare('/unused')
    assert formatter.stat().st_ino == original_inode
    assert list(formatter.iterdir()) == []
    assert (root / '.sotto-volume.json').is_file()

    second = tmp_path / 'not-empty'
    second.mkdir(mode=0o700)
    monkeypatch.setattr(fresh, 'ROOT', second)
    formatter = second / 'lost+found'
    formatter.mkdir(mode=0o700)
    (formatter / 'existing').write_text('do not erase')
    with pytest.raises(fresh.FreshPreparationUnavailableError):
        fresh.prepare('/unused')
    assert not (second / fresh.LOCK).exists()
    assert (formatter / 'existing').read_text() == 'do not erase'
