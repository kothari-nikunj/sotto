import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('managed_volume', Path(__file__).with_name('managed_volume.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_missing_mount_or_marker_cannot_be_ready(tmp_path):
    with pytest.raises(RuntimeError, match='mount point'):
        m.initialize(tmp_path, 'tenant', 'volume', ismount=lambda _: False)
    assert not (tmp_path / m.MARKER).exists()
    with pytest.raises(RuntimeError, match='provisioned'):
        m.verify(tmp_path, 'tenant', 'volume', ismount=lambda _: True)


def test_provisioned_identity_is_idempotent_and_cannot_be_rebound(tmp_path):
    result = m.initialize(tmp_path, 'tenant', 'volume', ismount=lambda _: True)
    assert result['ready'] is True
    assert m.initialize(tmp_path, 'tenant', 'volume', ismount=lambda _: True) == result
    assert (tmp_path / m.MARKER).stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match='refusing to reassign'):
        m.initialize(tmp_path, 'other', 'volume', ismount=lambda _: True)
    with pytest.raises(RuntimeError, match='does not match'):
        m.verify(tmp_path, 'tenant', 'different-volume', ismount=lambda _: True)
    assert not list(tmp_path.glob('.sotto-volume-check-*'))


def test_old_pilot_state_must_match_adopting_tenant(tmp_path):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/managed-capabilities.json').write_text(json.dumps({'tenant_id': 'other'}))
    with pytest.raises(RuntimeError, match='another tenant'):
        m.initialize(tmp_path, 'tenant', 'volume', ismount=lambda _: True)
