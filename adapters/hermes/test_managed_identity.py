"""Portable migration regressions; the Docker build exercises the real UID boundary."""
from pathlib import Path

import pytest

from managed_config import reconcile
from managed_identity import verify_home


def test_redirected_home_is_rejected_before_root_seed(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    home = tmp_path / 'hermes'
    home.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match='real directory'):
        verify_home(home)
    home.unlink()
    home.write_text('not a directory')
    with pytest.raises(RuntimeError, match='real directory'):
        verify_home(home)
    home.unlink()
    assert verify_home(home) == home  # a genuinely new volume can be seeded


def test_identity_upgrade_replaces_symlink_without_following_it(tmp_path):
    home = tmp_path / 'hermes'
    home.mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('owner data')
    (home / 'SOUL.md').symlink_to(outside)
    (home / 'SOUL.tmp').symlink_to(outside)
    env = {'SOTTO_MODEL_PROXY_URL': 'https://fixture.invalid',
           'SOTTO_MODEL_PROXY_TOKEN': 'fixture', 'PHOTON_HOME_CHANNEL': 'owner',
           'PHOTON_ALLOWED_USERS': 'owner'}
    reconcile(home, env)
    assert outside.read_text() == 'owner data'
    soul = home / 'SOUL.md'
    assert not soul.is_symlink()
    assert soul.read_text().startswith(Path(__file__).with_name('sotto-persona.md').read_text())
    assert soul.stat().st_mode & 0o777 == 0o444
    assert not list(home.glob('.SOUL-*'))
    expected = soul.read_text()
    reconcile(home, env)
    assert soul.read_text() == expected


def test_identity_permission_gate_is_required_by_image_build():
    docker = (Path(__file__).resolve().parents[2] / 'Dockerfile').read_text()
    assert 'RUN python3 /app/adapters/hermes/check_identity.py' in docker
