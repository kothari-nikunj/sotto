"""The Golden Corpus replay harness — evals/run_golden.py.

Builds a real (synthetic-source) corpus with the real builder, then replays it through the real
pipeline offline and checks the scoring, the refusals, and the two properties that make a harness
trustworthy: it never writes into the repo, and it never scores against labels nobody reviewed.
"""
import importlib.util
import json
import os

import pytest
import yaml

import test_golden_corpus as tgc

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))

_spec = importlib.util.spec_from_file_location("run_golden", os.path.join(ROOT, "evals", "run_golden.py"))
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    res = tgc.build_corpus(tmp_path_factory.mktemp("replay"))
    assert res["rc"] == 0
    return res


def _review(corpus_dir: str) -> None:
    """Flip the draft to reviewed, as the owner does at the end of the labeling hour."""
    path = os.path.join(corpus_dir, "labels.yaml")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace("reviewed: false", "reviewed: true"))


# ── The refusals ──────────────────────────────────────────────────────────────────────────────────

def test_refuses_to_score_against_an_unreviewed_draft(corpus, capsys):
    """An LLM's guesses are not a baseline. The refusal is the feature."""
    rc = rg.run("corpus-v1", 0, False, False, False, False, 0.05, corpus["out"])
    assert rc == 2
    assert "reviewed: false" in capsys.readouterr().out


def test_allow_unreviewed_is_an_explicit_opt_in(corpus):
    assert rg.run("corpus-v1", 0, False, False, True, False, 0.05, corpus["out"]) == 0


def test_a_missing_corpus_says_how_to_build_one(tmp_path, capsys):
    rc = rg.run("corpus-v1", 0, False, False, True, False, 0.05, str(tmp_path / "nope"))
    assert rc == 2
    assert "build_golden_corpus.py" in capsys.readouterr().out


def test_live_without_a_key_aborts(corpus, monkeypatch):
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)
    _review(corpus["out"])
    assert rg.run("corpus-v1", 0, True, False, False, False, 0.05, corpus["out"]) == 2


# ── The replay ────────────────────────────────────────────────────────────────────────────────────

def test_dry_replay_scores_the_deterministic_half(corpus, capsys):
    _review(corpus["out"])
    rc = rg.run("corpus-v1", 0, False, False, False, False, 0.05, corpus["out"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RESULT: PASS" in out
    agg = json.loads(out.split("AGGREGATE: ")[1].splitlines()[0])
    assert {"entity_dedup", "funnel_agreement"} <= set(agg)


def test_identity_dedup_is_measured_against_the_maps_ground_truth(corpus):
    """The dedup test the corpus exists for: the pipeline must resolve the day to exactly as many
    humans as the identity map says were really there. A split identity reads as a positive delta."""
    labels = rg.load_labels(corpus["out"])
    day = next(d for d in rg.load_corpus("corpus-v1", corpus["out"])["days"] if d["name"] == "day-00")
    base = rg.frozen_base()
    resolved = rg._distinct_entities(rg.ev.resolve_tokens(day, base)["inputs"])
    assert resolved == labels["days"]["day-00"]["entity_count"]


def test_funnel_replay_ignores_the_users_own_outbound(corpus):
    """A message you sent is not an interruption — it never reaches the funnel."""
    day = next(d for d in rg.load_corpus("corpus-v1", corpus["out"])["days"] if d["name"] == "day-01")
    assert any(m.get("is_from_me") for m in day["inputs"]["local"]["imessage"])
    assert all(not e.get("is_from_me") for e in rg.funnel_events(day))


def test_every_funnel_event_gets_a_verdict_from_the_real_funnel(corpus):
    day = next(d for d in rg.load_corpus("corpus-v1", corpus["out"])["days"] if d["name"] == "day-00")
    base = rg.frozen_base()
    sandbox = rg._seed_sandbox(rg.load_corpus("corpus-v1", corpus["out"]))
    saved = os.environ.get("SOTTO_DATA")
    os.environ["SOTTO_DATA"] = sandbox
    try:
        res = rg.replay_day(day, base, sandbox, live=False)
    finally:
        if saved is None:
            os.environ.pop("SOTTO_DATA", None)
        else:
            os.environ["SOTTO_DATA"] = saved
    assert res["exception"] is None
    assert set(res["verdicts"]) == {e["_gid"] for e in rg.funnel_events(day)}
    assert set(res["verdicts"].values()) <= {"nudge", "queue", "drop"}


def test_seed_ledger_is_seeded_once_and_days_accumulate_in_order(corpus):
    """Days replay oldest-first into ONE sandbox — that ordering is what makes the ledger and the
    interrupt budget accumulate the way six real weeks would."""
    loaded = rg.load_corpus("corpus-v1", corpus["out"])
    assert [d["name"] for d in loaded["days"]] == ["day-01", "day-00"]
    frozen = rg.frozen_base("2026-06-24")
    sandbox = rg._seed_sandbox(loaded, rg.oldest_base(loaded, frozen))
    anchors = rg._open_anchors(sandbox)
    assert anchors, "the pseudonymized ledger seeds the replay's day 1"
    # The seed ledger rides the SAME clock as the days: re-based onto the oldest day, never left at
    # its absolute build-time date (which reads as future-dated for most of a backwards replay).
    import yaml as _yaml
    for path in sorted(__import__("glob").glob(
            os.path.join(sandbox, "knowledge", "continuity", "*.md"))):
        with open(path, encoding="utf-8") as f:
            fm = _yaml.safe_load(f.read().split("---")[1])
        created = str(fm.get("created_at") or "")[:10]
        assert not created or created <= "2026-06-23", created


# ── Sampling, baselines, and not touching the repo ────────────────────────────────────────────────

def test_sampling_is_evenly_spaced_and_reproducible():
    days = [{"name": f"day-{i:02d}"} for i in range(42)]
    picked = rg.sample_days(days, 5)
    assert picked == rg.sample_days(days, 5)
    assert [d["name"] for d in picked] == ["day-00", "day-10", "day-20", "day-31", "day-41"]
    assert rg.sample_days(days, 0) == days                 # 0 = the whole corpus


def test_baseline_lands_beside_the_fixture_baselines(corpus, tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path / "vol"))
    _review(corpus["out"])
    assert rg.run("corpus-v1", 0, False, False, False, True, 0.05, corpus["out"]) == 0
    assert os.path.exists(tmp_path / "vol" / "evals" / "baselines" / "golden-corpus-v1.json")


def test_the_replay_never_writes_into_the_repo(corpus):
    before = {p: os.stat(os.path.join(dp, p)).st_mtime
              for dp, _dn, fns in os.walk(os.path.join(ROOT, "evals")) for p in fns}
    _review(corpus["out"])
    rg.run("corpus-v1", 0, False, False, False, False, 0.05, corpus["out"])
    after = {p: os.stat(os.path.join(dp, p)).st_mtime
             for dp, _dn, fns in os.walk(os.path.join(ROOT, "evals")) for p in fns}
    assert before == after


# ── The fixture harness delegates to this one ─────────────────────────────────────────────────────

def test_run_evals_golden_forwards_everything_after_the_flag(corpus, monkeypatch, capsys):
    """`run_evals.py --golden <args…>` is the single entry point; the flags stay defined once, in
    run_golden's own parser."""
    _ev = importlib.util.spec_from_file_location("run_evals", os.path.join(ROOT, "evals", "run_evals.py"))
    ev = importlib.util.module_from_spec(_ev)
    _ev.loader.exec_module(ev)
    monkeypatch.setattr("sys.argv", ["run_evals.py", "--golden", "--corpus-dir", corpus["out"],
                                     "--allow-unreviewed", "--sample", "1"])
    assert ev.main() == 0
    assert "GOLDEN" in capsys.readouterr().out


def test_plain_run_evals_still_runs_the_fixture_scorecard(monkeypatch, capsys):
    _ev = importlib.util.spec_from_file_location("run_evals", os.path.join(ROOT, "evals", "run_evals.py"))
    ev = importlib.util.module_from_spec(_ev)
    _ev.loader.exec_module(ev)
    monkeypatch.setattr("sys.argv", ["run_evals.py"])
    assert ev.main() == 0
    assert "SCORECARD" in capsys.readouterr().out


# ── Labels contract ───────────────────────────────────────────────────────────────────────────────

def test_unlabeled_fields_are_unscored_not_guessed(corpus):
    """`open_loops_after: null` means the owner had no opinion — the harness must produce no metric
    for it rather than invent one."""
    loaded = rg.load_corpus("corpus-v1", corpus["out"])
    day = loaded["days"][-1]
    labels = yaml.safe_load(yaml.safe_dump(loaded["labels"]))
    labels["days"][day["name"]]["open_loops_after"] = None
    res = {"exception": None, "verdicts": {}, "open_loops": set(), "entities": 3, "actions": []}
    metrics = {m for m, _v, _d in rg.score_day(day, res, labels, live=False)}
    assert "open_loops" not in metrics
