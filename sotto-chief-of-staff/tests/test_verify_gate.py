"""The shared gate (tools/verify.py) runs every hermetic suite the tree carries."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('release_verify_gate', ROOT / 'sotto-chief-of-staff/tools/verify.py')
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


def test_every_smoke_unit_suite_runs_in_the_gate():
    gated = {argv[-2] for _cwd, argv in VERIFY.commands(ROOT) if '-m' in argv and 'pytest' in argv}
    carried = {str(path.parent.relative_to(ROOT)) for path in (ROOT / 'smoke').glob('*/test_*.py')}
    assert carried <= gated, sorted(carried - gated)


def test_test_module_basenames_are_unique(tmp_path):
    VERIFY.verify_unique_test_basenames(ROOT)
    for directory in ('a', 'b'):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / 'test_same.py').write_text('')
    with pytest.raises(SystemExit, match='share a test module name'):
        VERIFY.verify_unique_test_basenames(tmp_path)
