"""The activation receipt is durable before the owner's first-brief gate opens."""
import importlib.util
import json
from pathlib import Path
import types

import pytest


@pytest.fixture(params=['managed', 'self-host'])
def photon(request, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', request.param)
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', 'chat')
    # Managed activation requires the same exact owner allowlist as production.
    monkeypatch.setenv('PHOTON_ALLOWED_USERS', 'chat')
    spec = importlib.util.spec_from_file_location('photon_owner_test', Path(__file__).with_name('photon_owner.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_activation_receipt_syncs_new_config_parent_then_renamed_receipt_directory(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture-project')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fixture-tenant')
    source = types.SimpleNamespace(chat_type='dm', user_id='chat', chat_id='owner-guid')
    receipt = tmp_path / 'config/photon-activation.json'
    synced = []
    original = photon._sync_directory

    def observe(path):
        synced.append((Path(path), receipt.exists()))
        original(path)

    monkeypatch.setattr(photon, '_sync_directory', observe)
    assert photon.record_owner_activation(source) is True
    assert synced == [(tmp_path, False), (tmp_path / 'config', True)]
    assert json.loads(receipt.read_text())['chat_id'] == 'owner-guid'
    before = receipt.stat().st_mtime_ns
    synced.clear()
    assert photon.record_owner_activation(source) is True
    assert synced == [(tmp_path, True), (tmp_path / 'config', True)]
    assert receipt.stat().st_mtime_ns == before


def test_failed_activation_directory_sync_propagates_and_identical_retry_resyncs(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture-project')
    source = types.SimpleNamespace(chat_type='dm', user_id='chat', chat_id='owner-guid')
    receipt = tmp_path / 'config/photon-activation.json'
    original = photon._sync_directory

    def fail_after_rename(path):
        if Path(path) == receipt.parent:
            assert receipt.exists(), 'rename must precede the durability barrier'
            raise OSError('simulated directory sync failure')
        original(path)

    monkeypatch.setattr(photon, '_sync_directory', fail_after_rename)
    with pytest.raises(OSError, match='simulated directory sync failure'):
        photon.record_owner_activation(source)
    assert json.loads(receipt.read_text())['chat_id'] == 'owner-guid'
    before = receipt.stat().st_mtime_ns
    calls = []

    def retry_sync(path):
        calls.append(Path(path))
        original(path)

    monkeypatch.setattr(photon, '_sync_directory', retry_sync)
    assert photon.record_owner_activation(source) is True
    assert calls == [tmp_path, receipt.parent]
    assert receipt.stat().st_mtime_ns == before


def test_failed_new_config_parent_sync_is_repeated_on_retry(photon, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'fixture-project')
    source = types.SimpleNamespace(chat_type='dm', user_id='chat', chat_id='owner-guid')
    receipt = tmp_path / 'config/photon-activation.json'
    original = photon._sync_directory

    def fail_parent(path):
        assert Path(path) == tmp_path
        raise OSError('simulated parent sync failure')

    monkeypatch.setattr(photon, '_sync_directory', fail_parent)
    with pytest.raises(OSError, match='simulated parent sync failure'):
        photon.record_owner_activation(source)
    assert receipt.parent.is_dir() and not receipt.exists()
    synced = []

    def retry_sync(path):
        synced.append(Path(path))
        original(path)

    monkeypatch.setattr(photon, '_sync_directory', retry_sync)
    assert photon.record_owner_activation(source) is True
    assert synced == [tmp_path, receipt.parent]
