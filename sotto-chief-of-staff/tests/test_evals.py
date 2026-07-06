"""test_evals.py — run the brief-quality eval INVARIANTS under pytest so they guard CI by default.

evals/run_evals.py is the runnable harness (offline `--deterministic` scorecard + human-invoked
`--live` scoring). This module imports that harness and asserts the SAME per-fixture invariants,
so a regression in muting, continuity resolution, tap-link safety, coverage, or the auto-critic
decision fails the test suite — no separate `python3 evals/run_evals.py` step required in CI.

Fully offline (SOTTO_LLM_STUB-style injected stub inside the harness), <5s, never touches repo files
(the harness sandboxes $SOTTO_DATA in a temp dir per fixture).
"""
import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
# The harness imports pack siblings (compose_brief, ledger_io) by bare name off these paths.
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))

_spec = importlib.util.spec_from_file_location("run_evals", os.path.join(ROOT, "evals", "run_evals.py"))
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


# One evaluate() per fixture is enough — cache so the pipeline runs once even though we assert
# each invariant as its own parametrized test (readable failures: test id = fixture/check name).
_CACHE = {}


def _evaluate(name):
    if name not in _CACHE:
        _CACHE[name] = ev.evaluate(name)
    return _CACHE[name]


@pytest.mark.parametrize("fixture", ev.FIXTURES)
def test_fixture_all_invariants_pass(fixture):
    """Every named invariant for a fixture must hold — the same set the scorecard prints."""
    _r, checks = _evaluate(fixture)
    failures = [f"{cname}: {detail}" for cname, ok, detail in checks if not ok]
    assert not failures, f"{fixture} invariant failures:\n  " + "\n  ".join(failures)


@pytest.mark.parametrize("fixture", ev.FIXTURES)
def test_fixture_runs_without_exception(fixture):
    """The pipeline itself must complete for every fixture (edge_day is the adversarial one)."""
    r, _ = _evaluate(fixture)
    assert r["exception"] is None, f"{fixture} raised:\n{r['exception']}"


def test_no_invented_group_deeplinks_anywhere():
    """Belt-and-suspenders across all fixtures: no action ever gets a group deep link, and every
    tap_link that IS emitted uses one of the safe universal schemes."""
    for name in ev.FIXTURES:
        r, _ = _evaluate(name)
        for a in (r["out"].get("actions") or []):
            link = a.get("tap_link")
            if not link:
                continue
            assert link.startswith(ev._ALLOWED_TAP_PREFIXES), f"{name}/{a.get('id')}: unsafe scheme {link}"
            assert not any(m in link for m in ev._GROUP_MARKERS), f"{name}/{a.get('id')}: group deep link {link}"


def test_deterministic_runner_exits_zero():
    """The offline scorecard entrypoint returns 0 when all invariants hold."""
    assert ev.run_deterministic() == 0


def test_token_resolution_is_wall_clock_independent():
    """Two loads with different base dates yield different literals but identical invariant outcomes:
    determinism comes from the FIXED injected base, not the wall clock."""
    from datetime import datetime
    fixture = {"a": "{{D}}", "b": "{{TS-2h}}", "c": "{{MD+3d}}", "d": "no tokens here"}
    b1 = datetime(2026, 1, 15, 12, 0, 0)
    b2 = datetime(2030, 9, 2, 12, 0, 0)
    r1 = ev.resolve_tokens(fixture, b1)
    r2 = ev.resolve_tokens(fixture, b2)
    assert r1["a"] == "2026-01-15" and r2["a"] == "2030-09-02"
    assert r1["b"] == "2026-01-15 10:00:00"
    assert r1["c"] == "01-18"
    assert r1["d"] == r2["d"] == "no tokens here"


def test_scores_path_honors_sotto_data(tmp_path, monkeypatch):
    """The live baseline must land on the $SOTTO_DATA volume in-container (the skills tree is read-only
    and wiped every boot), and fall back to the repo-local evals/baselines/ for dev."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert ev._scores_path() == os.path.join(str(tmp_path), "evals", "baselines", "scores.json")
    assert ev._baselines_dir() == os.path.join(str(tmp_path), "evals", "baselines")
    monkeypatch.delenv("SOTTO_DATA", raising=False)
    repo_local = os.path.join(ev.HERE, "baselines", "scores.json")
    assert ev._scores_path() == repo_local           # dev workflow unchanged


def test_a_broken_invariant_is_caught():
    """Sanity: the check framework must actually FAIL when an invariant is violated. We corrupt a
    copy of a passing result (drop the resolved loop) and confirm the loop check flips to failed."""
    r, _ = _evaluate("rich_day")
    corrupt = dict(r)
    corrupt["continuity"] = {"resolved": [], "active": [], "expired": []}
    cname, ok, _detail = ev.chk_rich_loops(corrupt)
    assert cname == "loop_resolved_and_surfaced"
    assert ok is False, "the loop invariant should fail when the resolved loop is missing"
