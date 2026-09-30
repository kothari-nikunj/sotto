"""Container restart must not copy skills from the volume back onto itself."""
import importlib.util
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('sync_sotto_skills', HERE / 'sync_sotto_skills.py')
sync_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_module)


def test_two_boots_after_root_home_becomes_volume_alias(tmp_path):
    image = tmp_path / 'app'
    source = image / 'sotto-skills'
    script = source / '_shared/scripts/compose_brief.py'
    script.parent.mkdir(parents=True)
    script.write_text('reviewed skill\n')
    bundle = image / 'adapters/hermes/sotto.bundle.yaml'
    bundle.parent.mkdir(parents=True)
    bundle.write_text('reviewed bundle\n')
    home = tmp_path / 'data/hermes'
    home.mkdir(parents=True)
    sync_module.sync(home, source, bundle)

    # start.sh replaces /root/.hermes with the persisted volume after boot.
    root_home = tmp_path / 'root/.hermes'
    root_home.parent.mkdir()
    root_home.symlink_to(home, target_is_directory=True)
    (home / 'skills/sotto/stale.py').write_text('unreviewed\n')
    (home / 'skill-bundles/sotto.yaml').write_text('stale bundle\n')
    sync_module.sync(home, source, bundle)

    assert root_home.is_symlink()
    assert script.read_bytes() == (home / 'skills/sotto/_shared/scripts/compose_brief.py').read_bytes()
    assert not (home / 'skills/sotto/stale.py').exists()
    assert bundle.read_bytes() == (home / 'skill-bundles/sotto.yaml').read_bytes()
    start = (HERE / 'start.sh').read_text()
    assert 'sync_sotto_skills.py "$HSTATE"' in start
    assert '/app/sotto-skills /app/adapters/hermes/sotto.bundle.yaml' in start


def test_volume_alias_cannot_be_used_as_image_source(tmp_path):
    home = tmp_path / 'data/hermes'
    script = home / 'skills/sotto/_shared/scripts/compose_brief.py'
    script.parent.mkdir(parents=True)
    script.write_text('keep me\n')
    bundle = home / 'skill-bundles/sotto.yaml'
    bundle.parent.mkdir()
    bundle.write_text('keep bundle\n')
    root_home = tmp_path / 'root/.hermes'
    root_home.parent.mkdir()
    root_home.symlink_to(home, target_is_directory=True)

    with pytest.raises(RuntimeError, match='aliases'):
        sync_module.sync(home, root_home / 'skills/sotto', root_home / 'skill-bundles/sotto.yaml')
    assert script.read_text() == 'keep me\n'
    assert bundle.read_text() == 'keep bundle\n'


def test_bundle_symlink_is_replaced_without_writing_through_it(tmp_path):
    source = tmp_path / 'image/skills'
    script = source / '_shared/scripts/compose_brief.py'
    script.parent.mkdir(parents=True)
    script.write_text('reviewed\n')
    bundle = tmp_path / 'image/sotto.bundle.yaml'
    bundle.write_text('image bundle\n')
    home = tmp_path / 'volume/hermes'
    destination = home / 'skill-bundles/sotto.yaml'
    destination.parent.mkdir(parents=True)
    unrelated = tmp_path / 'unrelated'
    unrelated.write_text('preserve\n')
    destination.symlink_to(unrelated)

    sync_module.sync(home, source, bundle)

    assert unrelated.read_text() == 'preserve\n'
    assert not destination.is_symlink()
    assert destination.read_text() == 'image bundle\n'


@pytest.mark.parametrize('missing', ('compose', 'bundle', 'empty_compose'))
def test_missing_immutable_image_input_preserves_existing_skills(tmp_path, missing):
    home = tmp_path / 'volume/hermes'
    previous = home / 'skills/sotto/_shared/scripts/compose_brief.py'
    previous.parent.mkdir(parents=True)
    previous.write_text('previous working skill\n')
    previous_bundle = home / 'skill-bundles/sotto.yaml'
    previous_bundle.parent.mkdir()
    previous_bundle.write_text('previous working bundle\n')
    source = tmp_path / 'image/skills'
    compose = source / '_shared/scripts/compose_brief.py'
    compose.parent.mkdir(parents=True)
    compose.write_text('' if missing == 'empty_compose' else 'new image skill\n')
    bundle = tmp_path / 'image/sotto.bundle.yaml'
    bundle.write_text('new image bundle\n')
    if missing == 'compose':
        compose.unlink()
    if missing == 'bundle':
        bundle.unlink()

    with pytest.raises(RuntimeError, match='Immutable Sotto skills'):
        sync_module.sync(home, source, bundle)

    assert previous.read_text() == 'previous working skill\n'
    assert previous_bundle.read_text() == 'previous working bundle\n'
