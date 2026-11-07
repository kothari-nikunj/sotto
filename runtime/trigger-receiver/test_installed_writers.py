"""Installed writer lookup stays anchored to code across checkout and packaged layouts."""
from pathlib import Path
import subprocess
import sys

import pytest

import installed_writers as writers


@pytest.mark.parametrize('layout', ['source', 'standalone', 'image'])
def test_writer_lookup_uses_caller_code_and_never_private_home(tmp_path, monkeypatch, layout):
    root = tmp_path / layout
    anchor = root / ('trigger-receiver/owner.py' if layout == 'image' else 'runtime/trigger-receiver/owner.py')
    anchor.parent.mkdir(parents=True)
    anchor.touch()
    scripts = root / ('sotto-skills/_shared/scripts' if layout == 'image' else 'sotto-chief-of-staff/_shared/scripts')
    scripts.mkdir(parents=True)
    (scripts.parent / 'lib').mkdir()
    script = scripts / 'preferences.py'
    script.write_text('LOADED_FROM = __file__\n')
    private = tmp_path / 'private-home'
    private.mkdir()
    (private / 'preferences.py').write_text("raise AssertionError('private implementation')\n")
    monkeypatch.setenv('SOTTO_DATA', str(private))
    monkeypatch.setenv('SOTTO_HOME', str(private))
    original_path = Path
    monkeypatch.setattr(writers, 'Path', lambda value: original_path(str(value).replace(
        '/app/sotto-skills', str(root / 'sotto-skills'))))
    assert writers.preferences(anchor=anchor).LOADED_FROM == str(script)
    assert str(scripts.parent / 'lib') in sys.path
    script.unlink()
    with pytest.raises(writers.UnavailableError, match='installed writer unavailable'):
        writers.preferences(anchor=anchor)


def test_cached_loader_does_not_reuse_a_different_callers_source_tree(tmp_path):
    seen = []
    for name in ('first', 'second'):
        root = tmp_path / name
        anchor = root / 'runtime/trigger-receiver/owner.py'
        anchor.parent.mkdir(parents=True)
        anchor.touch()
        script = root / 'sotto-chief-of-staff/_shared/scripts/preferences.py'
        script.parent.mkdir(parents=True)
        script.write_text(f'ORIGIN = {name!r}\n')
        seen.append(writers.preferences(anchor=anchor).ORIGIN)
    assert seen == ['first', 'second']


def test_google_writer_import_does_not_need_a_quiet_or_preferences_import():
    receiver = Path(__file__).resolve().parent
    script = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from installed_writers import google_action
assert 'runtime_owner_quiet' not in sys.modules
module = google_action(anchor=Path(sys.argv[1]) / 'owner_google_effect.py')
assert callable(module.capabilities)
assert not any('preferences' in name for name in sys.modules)
'''
    subprocess.run([sys.executable, '-I', '-c', script, str(receiver)], check=True,
                   capture_output=True, text=True, timeout=30)
