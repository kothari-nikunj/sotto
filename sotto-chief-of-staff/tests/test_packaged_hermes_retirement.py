"""Sensitivity checks for the assertion executed inside the actual packaged image."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "runtime/sotto-chat/test/fixtures/packaged-image/hermes-retirement-check.py"
SPEC = importlib.util.spec_from_file_location("packaged_hermes_retirement", CHECK)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


def test_retirement_check_allows_history_and_attributed_adaptation(tmp_path):
    (tmp_path / "history-hermes.md").write_text("Historical Hermes architecture")
    (tmp_path / "routine_cursor.NOTICE").write_text("Adapted from Hermes; MIT License")
    check.assert_external_hermes_absent(
        tmp_path, ["croniter", "hermes-parser-unrelated"], lambda _: None, lambda _: None
    )


@pytest.mark.parametrize("name", ["hermes", "Hermes_Agent", "hermes.ai"])
def test_retirement_check_rejects_installed_agent_distribution(tmp_path, name):
    with pytest.raises(AssertionError, match="distribution"):
        check.assert_external_hermes_absent(tmp_path, [name], lambda _: None, lambda _: None)


@pytest.mark.parametrize("module", check.MODULES)
def test_retirement_check_rejects_importable_agent_module(tmp_path, module):
    with pytest.raises(AssertionError, match="module"):
        check.assert_external_hermes_absent(
            tmp_path, [], lambda _: None, lambda name: object() if name == module else None
        )


@pytest.mark.parametrize("executable", check.EXECUTABLES)
def test_retirement_check_rejects_path_launcher(tmp_path, executable):
    with pytest.raises(AssertionError, match="launcher"):
        check.assert_external_hermes_absent(
            tmp_path, [], lambda name: "/custom/bin/" + name if name == executable else None, lambda _: None
        )


@pytest.mark.parametrize("relative", check.LEGACY_PATHS)
def test_retirement_check_rejects_packaged_artifact_even_if_not_importable(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to("/missing-target")
    with pytest.raises(AssertionError, match="artifact"):
        check.assert_external_hermes_absent(tmp_path, [], lambda _: None, lambda _: None)


def test_packaged_runner_invokes_retirement_check_before_runtime_smoke():
    script = CHECK.with_name("run-packaged-smoke.sh").read_text()
    assert script.index("python3 -I -B /tmp/sotto-fixture/hermes-retirement-check.py") < script.index(
        "python3 -I -B /tmp/sotto-fixture/package-runtime-smoke.py"
    )
