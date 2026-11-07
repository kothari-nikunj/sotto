"""The Golden Corpus replay harness — evals/run_golden.py.

Builds a real (synthetic-source) corpus with the real builder, then replays it through the real
pipeline offline and checks the scoring, the refusals, and the two properties that make a harness
trustworthy: it never writes into the repo, and it never scores against labels nobody reviewed.
"""
import importlib.util
import json
import os
import sys

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


def test_allow_unreviewed_is_an_explicit_opt_in(corpus, capsys):
    """The opt-in gets past the review refusal — and then, with no baseline recorded, the gate
    reports itself unarmed rather than inventing a PASS."""
    rc = rg.run("corpus-v1", 0, False, False, True, False, 0.05, corpus["out"])
    out = capsys.readouterr().out
    assert "reviewed: false" not in out
    assert rc == 2
    assert "RESULT: NO BASELINE" in out


def test_a_missing_corpus_says_how_to_build_one(tmp_path, capsys):
    rc = rg.run("corpus-v1", 0, False, False, True, False, 0.05, str(tmp_path / "nope"))
    assert rc == 2
    assert "build_golden_corpus.py" in capsys.readouterr().out


def test_chronological_replay_preserves_afternoon_evidence_and_refuses_sampling(tmp_path, capsys):
    from datetime import datetime
    res = tgc.build_corpus(tmp_path / 'chronological', chronological=True)
    day = res['days']['day-01']
    base = rg.base_for(day, datetime(2026, 8, 9, 12))
    assert base == datetime(2026, 8, 8, 23, 59)
    inputs = rg.ev.resolve_tokens(day['inputs'], base)
    assert inputs['local']['imessage'][0]['timestamp'] == '2026-08-08 18:00:00'
    assert rg.run('corpus-v1', 1, False, False, True, False, 0.05, res['out']) == 2
    assert 'every retained day' in capsys.readouterr().out


def test_live_without_a_key_aborts(corpus, monkeypatch):
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)
    _review(corpus["out"])
    assert rg.run("corpus-v1", 0, True, False, False, False, 0.05, corpus["out"]) == 2


def test_chronological_funnel_never_sees_later_messages_or_day_end_learning(tmp_path, monkeypatch):
    from datetime import datetime
    import personal_context
    import triage_event

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    def message(hour, text, **extra):
        return {'handle': '+15555550123', 'timestamp': f'2026-08-08 {hour}:00:00',
                'text': text, '_gid': text, **extra}
    # Deliberately reverse source order. The sent reply is context, not an interruption.
    day = {'name': 'ordered', 'replay_clock': 'day_end', 'inputs': {
        'type': 'evening', 'google': {'emails': [], 'events': []}, 'local': {'imessage': [
            message('18', 'later question'), message('14', 'sent it', is_from_me=True),
            message('09', 'morning ask')]}}}
    observed = []
    learned = tmp_path / 'learned-at-day-end'
    def compose(*args, **kwargs):
        learned.write_text('future knowledge')
        return {'brief_markdown': 'Scripted brief', 'actions': []}
    def triage(payload, **kwargs):
        event = payload['events'][0]
        assert not learned.exists(), 'earlier event saw day-end learning'
        context = personal_context.current_conversation(
            event, candidates=personal_context.conversation_snapshot())
        observed.append((event['text'], [row['text'] for row in context]))
        return {'verdict': 'queue'}
    monkeypatch.setattr(rg.cb, 'compose', compose)
    monkeypatch.setattr(triage_event, 'triage', triage)
    result = rg.replay_day(day, datetime(2026, 8, 8, 23, 59), str(tmp_path), False)
    assert result['exception'] is None
    assert observed == [('morning ask', ['morning ask']),
                        ('later question', ['morning ask', 'sent it', 'later question'])]
    assert learned.exists()


def test_chronological_funnel_orders_sources_and_refuses_undated_events(tmp_path, monkeypatch):
    from datetime import datetime
    import triage_event
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    observed = []
    monkeypatch.setattr(triage_event, 'triage', lambda payload, **kwargs:
                        observed.append(payload['events'][0]['_gid']) or {'verdict': 'queue'})
    day = {'replay_clock': 'day_end', 'inputs': {'google': {'emails': [
        {'_gid': 'email', 'date': '2026-08-08T09:00:00Z'}]}, 'local': {'imessage': [
        {'_gid': 'text', 'timestamp': '2026-08-08T18:00:00Z'}]}}}
    rg._replay_funnel(day, datetime(2026, 8, 8, 23, 59))
    assert observed == ['email', 'text']
    day['inputs']['google']['emails'][0].pop('date')
    with pytest.raises(ValueError, match='requires an observed timestamp'):
        rg._replay_funnel(day, datetime(2026, 8, 8, 23, 59))
    assert observed == ['email', 'text'], 'invalid chronology must fail before processing events'


# ── The replay ────────────────────────────────────────────────────────────────────────────────────

def test_dry_replay_scores_the_deterministic_half(corpus, tmp_path, monkeypatch, capsys):
    _review(corpus["out"])
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path / "vol"))
    assert rg.run("corpus-v1", 0, False, False, False, True, 0.05, corpus["out"]) == 0
    capsys.readouterr()                                       # the recording run's output
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
    assert ev.main() == 2                                  # forwarded fine; no baseline recorded
    assert "GOLDEN" in capsys.readouterr().out


def test_plain_run_evals_still_runs_the_fixture_scorecard(monkeypatch, capsys):
    _ev = importlib.util.spec_from_file_location("run_evals", os.path.join(ROOT, "evals", "run_evals.py"))
    ev = importlib.util.module_from_spec(_ev)
    _ev.loader.exec_module(ev)
    monkeypatch.setattr("sys.argv", ["run_evals.py"])
    assert ev.main() == 0
    assert "SCORECARD" in capsys.readouterr().out


# ── The gate is armed, or it says so (external review #7) ─────────────────────────────────────────

def test_the_judges_scores_reach_the_aggregate_and_the_baseline(corpus, tmp_path, monkeypatch, capsys):
    """Regression: the judge's verdict rode a nested dict that aggregate() silently dropped —
    fabrication/voice/triage never reached the baseline. They land as 0-1 judge_* scalars now,
    and the baseline records the run's shape beside them."""
    _review(corpus["out"])
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path / "vol"))
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "offline-stub")
    import gemini as gem
    monkeypatch.setattr(gem, "model_once", lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("offline test — no network")))
    monkeypatch.setattr(rg, "gemini_call", lambda _p, _i: json.dumps(
        {"brief_markdown": "morning.", "actions": [], "extracted_knowledge": {}}))
    monkeypatch.setattr(rg, "judge_day", lambda _r, _d, _b: {
        "fabrication": 8, "voice": 9, "triage": 7, "notes": "solid"})
    import triage_event
    monkeypatch.setattr(triage_event, '_classify_tier1', lambda *_: ('queue','ambient','scripted verdict'))
    assert rg.run("corpus-v1", 0, True, True, False, True, 0.05, corpus["out"]) == 0
    out = capsys.readouterr().out
    agg = json.loads(out.split("AGGREGATE: ")[1].splitlines()[0])
    assert (agg["judge_fabrication"], agg["judge_voice"], agg["judge_triage"]) == (0.8, 0.9, 0.7)
    with open(tmp_path / "vol" / "evals" / "baselines" / "golden-corpus-v1.json",
              encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["scores"]["judge_voice"] == 0.9
    assert doc["run"] == {"mode": "live+judge", "sample": 0, "critic_policy": "auto",
                          "metric_policy": "active-states-and-absolute-entity-error-v2",
                          "chronological_funnel_policy": "event-time-before-day-end-compose-v1",
                          "compose_model": "/".join(rg.gemini_transport.effective_compose_model()),
                          "judge_model": rg.judge_model()}


def test_aggregate_refuses_a_shape_it_cannot_score():
    """A dict without 'f1' used to fall through both branches and vanish. Vanishing is the bug;
    now it is a loud error at the seam."""
    with pytest.raises(ValueError, match="unaggregatable"):
        rg.aggregate([("day-00", "judge", {"fabrication": 8}, "")])


def test_a_metric_that_vanishes_from_the_run_fails_the_gate(tmp_path, monkeypatch, capsys):
    """The comparison walks the BASELINE's metrics: a dimension the baseline holds that this run
    didn't produce is a FAIL with the recorded run shape in the hint — not a silent skip."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path / "vol"))
    assert rg._baseline_gate("corpus-v1", {"funnel_agreement": 0.9, "judge_fabrication": 0.8},
                             True, 0.05, {"mode": "live+judge", "sample": 5}, _coverage()) == 0
    capsys.readouterr()
    rc = rg._baseline_gate("corpus-v1", {"funnel_agreement": 0.9}, False, 0.05,
                           {"mode": "dry", "sample": 0}, _coverage())
    out = capsys.readouterr().out
    assert rc == 1
    assert "judge_fabrication" in out and "live+judge" in out
    assert "RESULT: FAIL" in out


def test_a_matching_run_with_no_regressions_still_passes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path / "vol"))
    sig = {"mode": "dry", "sample": 0}
    assert rg._baseline_gate("corpus-v1", {"funnel_agreement": 0.9}, True, 0.05, sig, _coverage()) == 0
    capsys.readouterr()
    assert rg._baseline_gate("corpus-v1", {"funnel_agreement": 0.88}, False, 0.05, sig, _coverage()) == 0
    assert "RESULT: PASS" in capsys.readouterr().out


@pytest.mark.parametrize("current", [5.0, -5.0])
def test_entity_dedup_drift_in_either_direction_fails_the_gate(tmp_path, monkeypatch, capsys, current):
    """entity_dedup is a signed distance from 0: split identities (+) regress as much as merges (-)."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path / "vol"))
    sig = {"mode": "dry", "sample": 0}
    assert rg._baseline_gate("corpus-v1", {"entity_dedup": 0.0}, True, 0.05, sig, _coverage()) == 0
    capsys.readouterr()
    assert rg._baseline_gate("corpus-v1", {"entity_dedup": current}, False, 0.05, sig, _coverage()) == 1
    assert "RESULT: FAIL" in capsys.readouterr().out
    assert rg._baseline_gate("corpus-v1", {"entity_dedup": -current / 100}, False, 0.05, sig, _coverage()) == 0


def test_live_baseline_records_effective_critic_policy_and_refuses_mismatch(
        corpus, tmp_path, monkeypatch, capsys):
    _review(corpus['out'])
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path / 'vol'))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'offline-stub')
    monkeypatch.setattr(rg, 'replay_day', lambda *_args: {'exception': None, 'brief': 'scripted'})
    monkeypatch.setattr(rg, 'score_day', lambda *_args: [('metric', .9, 'scripted')])
    monkeypatch.setenv('SOTTO_CRITIC', ' off ')
    assert rg.run('corpus-v1', 0, True, False, False, True, .05, corpus['out']) == 0
    baseline = tmp_path / 'vol/evals/baselines/golden-corpus-v1.json'
    recorded = baseline.read_bytes()
    assert json.loads(recorded)['run']['critic_policy'] == 'off'
    for policy in ('auto', 'always', 'invalid-policy'):
        monkeypatch.setenv('SOTTO_CRITIC', policy)
        capsys.readouterr()
        assert rg.run('corpus-v1', 0, True, False, False, False, .05, corpus['out']) == 1
        assert 'COVERAGE MISMATCH' in capsys.readouterr().out
        assert baseline.read_bytes() == recorded
    monkeypatch.setenv('SOTTO_CRITIC', 'OFF')
    capsys.readouterr()
    assert rg.run('corpus-v1', 0, True, False, False, False, .05, corpus['out']) == 0
    assert 'RESULT: PASS' in capsys.readouterr().out
    legacy = json.loads(recorded)
    del legacy['run']['critic_policy']
    baseline.write_text(json.dumps(legacy))
    capsys.readouterr()
    assert rg.run('corpus-v1', 0, True, False, False, False, .05, corpus['out']) == 1
    assert 'COVERAGE MISMATCH' in capsys.readouterr().out


def test_dry_baseline_critic_policy_is_disabled_even_if_env_changes(
        corpus, tmp_path, monkeypatch):
    _review(corpus['out'])
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path / 'vol'))
    monkeypatch.setenv('SOTTO_CRITIC', 'always')
    assert rg.run('corpus-v1', 0, False, False, False, True, .05, corpus['out']) == 0
    baseline = tmp_path / 'vol/evals/baselines/golden-corpus-v1.json'
    assert json.loads(baseline.read_text())['run']['critic_policy'] == 'disabled'
    monkeypatch.setenv('SOTTO_CRITIC', 'off')
    assert rg.run('corpus-v1', 0, False, False, False, False, .05, corpus['out']) == 0


@pytest.mark.parametrize('threshold', [float('nan'), float('inf'), -1, 1.1])
@pytest.mark.parametrize('update', [False, True])
def test_invalid_threshold_never_passes_or_replaces_baseline(
        tmp_path, monkeypatch, capsys, threshold, update):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path / 'vol'))
    sig = {'mode': 'dry', 'sample': 0}
    assert rg._baseline_gate('probe', {'metric': .9}, True, .05, sig, _coverage()) == 0
    baseline = tmp_path / 'vol/evals/baselines/golden-probe.json'
    before = baseline.read_bytes()
    capsys.readouterr()
    assert rg._baseline_gate('probe', {'metric': .5}, update, threshold, sig, _coverage()) == 1
    assert 'RESULT: PASS' not in capsys.readouterr().out
    assert baseline.read_bytes() == before


@pytest.mark.parametrize('threshold,should_pass', [(0, False), (1, True)])
def test_finite_threshold_endpoints_keep_normal_regression_comparison(
        tmp_path, monkeypatch, threshold, should_pass):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path / 'vol'))
    sig = {'mode': 'dry', 'sample': 0}
    assert rg._baseline_gate('probe', {'metric': .9}, True, .05, sig, _coverage()) == 0
    assert (rg._baseline_gate('probe', {'metric': .5}, False, threshold, sig, _coverage()) == 0) is should_pass


def test_cli_rejects_nonfinite_threshold_before_replay_or_baseline_write(
        corpus, tmp_path, monkeypatch, capsys):
    _review(corpus['out'])
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path / 'vol'))
    argv = ['run_golden.py', '--corpus-dir', corpus['out'], '--version', 'corpus-v1',
            '--sample', '0', '--update-baseline']
    monkeypatch.setattr(sys, 'argv', argv)
    assert rg.main() == 0
    baseline = tmp_path / 'vol/evals/baselines/golden-corpus-v1.json'
    before = baseline.read_bytes()
    capsys.readouterr()
    monkeypatch.setattr(sys, 'argv', argv[:-1] + ['--threshold', 'nan'])
    assert rg.main() != 0
    assert 'RESULT: PASS' not in capsys.readouterr().out
    assert baseline.read_bytes() == before


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


def _coverage():
    return {'expected':['one'], 'executed':['one'], 'failed':[], 'unlabeled':[], 'scored':{}}


@pytest.mark.parametrize('update',[False,True])
def test_failed_day_never_passes_or_replaces_complete_baseline(corpus,tmp_path,monkeypatch,capsys,update):
    monkeypatch.setenv('SOTTO_DATA',str(tmp_path/'vol'))
    _review(corpus['out'])
    assert rg.run('corpus-v1',0,False,False,False,True,.05,corpus['out'])==0
    baseline=tmp_path/'vol/evals/baselines/golden-corpus-v1.json'
    before=baseline.read_bytes();capsys.readouterr()
    original=rg.replay_day
    def failed(day,*args,**kwargs):
        if day['name']=='day-00':
            return {'exception':'synthetic execution failure','brief':''}
        return original(day,*args,**kwargs)
    monkeypatch.setattr(rg,'replay_day',failed)
    assert rg.run('corpus-v1',0,False,False,False,update,.05,corpus['out'])==1
    output=capsys.readouterr().out
    assert 'RESULT: PASS' not in output and 'day-00' in output
    coverage=json.loads(output.split('COVERAGE: ')[1].splitlines()[0])
    assert coverage['expected']==['day-01','day-00']
    assert coverage['executed']==['day-01'] and coverage['failed']==['day-00']
    assert baseline.read_bytes()==before
    monkeypatch.setattr(rg,'replay_day',original)
    assert rg.run('corpus-v1',0,False,False,False,False,.05,corpus['out'])==0


def test_unlabeled_completed_case_has_coverage_not_failure(corpus,tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('SOTTO_DATA',str(tmp_path/'vol'))
    loaded=rg.load_corpus('corpus-v1',corpus['out'])
    loaded['labels']={'reviewed':True,'days':{'day-01':{'entity_count':0}}}
    monkeypatch.setattr(rg,'load_corpus',lambda *a:loaded)
    assert rg.run('corpus-v1',0,False,False,False,True,.05,corpus['out'])==0
    coverage=json.loads(capsys.readouterr().out.split('COVERAGE: ')[1].splitlines()[0])
    assert coverage['executed']==coverage['expected']
    assert coverage['failed']==[] and coverage['unlabeled']==['day-00']
    loaded['labels']['days']['day-01']={}
    assert rg.run('corpus-v1',0,False,False,False,True,.05,corpus['out'])==1


def test_changed_case_coverage_cannot_pass_surviving_equal_scores(tmp_path,monkeypatch):
    monkeypatch.setenv('SOTTO_DATA',str(tmp_path))
    full=dict(_coverage(),expected=['one','two'],executed=['one','two'],scored={'metric':['one','two']})
    assert rg._baseline_gate('fixture',{'metric':1},True,.05,{'mode':'dry'},full)==0
    assert rg._baseline_gate('fixture',{'metric':1},False,.05,{'mode':'dry'},_coverage())==1


@pytest.mark.parametrize('case',['approved','denied','missing-evidence','revoked'])
def test_golden_archived_gmail_completion_uses_real_gather_critic_and_resolver(tmp_path,monkeypatch,case):
    import copy
    import ledger_io
    import source_context
    import delivery_effects
    import loop_context
    import gather_google
    from test_loop_context import Service,raw,NOW
    monkeypatch.setenv('SOTTO_DATA',str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE','+00:00')
    monkeypatch.setenv('SOTTO_CRITIC','auto')
    monkeypatch.setattr(source_context,'allowed',lambda source: source!='gmail' or case!='revoked')
    # Exact original Gmail thread, absent from INBOX; a second obligation belongs to the
    # same contact but has its own stable anchor and must never close by name.
    first=dict(anchor_key='task:pricing',status='open',action_type='follow_up',
        source='gmail',channel='email',source_thread_id='original-thread',contact_name='Dana Reed',
        contact_identifier='peer@example.com',created_at='2026-09-28',summary='Send pricing document',ask='Send pricing document')
    second=dict(first,anchor_key='task:budget',source_thread_id='second-thread',summary='Review supplier budget',ask='Review supplier budget')
    for row in (first,second):
        rg.cr._persist(row)
    effect=[{'kind':'eligibility','source':'gmail','anchor_key':first['anchor_key']}]
    if case!='revoked':
        assert delivery_effects.valid(effect,now=NOW.timestamp())
    service=Service([raw(body='The pricing document is sent.')])
    emails,receipt=loop_context.refresh([first],{},service,gather_google.normalize_email,
        allowed=source_context.allowed,now=NOW,aliases=lambda _:())
    assert not any('INBOX' in e.get('labelIds',[]) for e in emails)
    if case=='missing-evidence':
        emails=[]
    update={'loopId':first['anchor_key'],'loopVersion':delivery_effects.loop_version(first),
        'status':'resolved','evidence':[{'sourceType':'email','sourceId':'reply','snippet':'pricing document is sent'}]}
    seen=[]
    def model(prompt,inputs):
        seen.append(('critic' if inputs.get('_critic') else 'extract',prompt))
        if inputs.get('_critic'):
            return json.dumps({'patches':[],'score':95,'summary':'Synthetic reviewed verdict.',
                'loop_verdicts':[{'proposal_id':rg.cr.critic_review_receipt(update),'verdict':'approve'}] if case=='approved' else []})
        return json.dumps({'markdown':'# Brief\n\nThe pricing document is sent.', 'actionItems':[], 'loopUpdates':[update]})
    monkeypatch.setattr(rg,'gemini_call',model)
    day={'name':'completion','inputs':{'type':'morning','google':{'emails':emails,'events':[],
        'userEmail':'owner@example.com'},'local':{}},'signals':{}}
    result=rg.replay_day(copy.deepcopy(day),NOW,str(tmp_path),live=True)
    assert result['exception'] is None,result
    after={row['anchor_key']:row for row in ledger_io.load_entries()}
    assert after[second['anchor_key']]['status']=='open'
    assert after[first['anchor_key']]['status']==('resolved' if case=='approved' else 'open')
    assert delivery_effects.valid(effect,now=NOW.timestamp())==(case not in ('approved','revoked'))
    if case=='approved':
        assert service.calls and any(kind=='critic' for kind,_ in seen)
        assert any('pricing document is sent' in prompt for kind,prompt in seen if kind=='extract')
        settled=copy.deepcopy(after[first['anchor_key']])
        repeated=rg.replay_day(copy.deepcopy(day),NOW,str(tmp_path),live=True)
        assert repeated['exception'] is None
        assert next(r for r in ledger_io.load_entries() if r['anchor_key']==first['anchor_key'])==settled
    if case=='revoked':
        assert emails==[] and service.calls==[]
        assert not any('Send pricing document' in prompt for _,prompt in seen)


def _resolve_commitment_in_fresh_worker(data_root):
    import subprocess
    # Rebuild only the fixture's production import roots. The full suite adds paths
    # repeatedly; forwarding the parent's sys.path can exceed the OS argument limit.
    script = ('import json,sys\nfrom pathlib import Path\npack=Path(sys.argv[1])\n'
              'roots=[pack/"tests",pack/"_shared/lib",pack/"_shared/scripts",'
              'pack/"_shared/knowledge",*sorted(pack.glob("*/scripts"))]\n'
              'sys.path[:0]=list(dict.fromkeys(str(path) for path in roots))\n'
              'from commitment_journey_fixture import run_chronological_stage\n'
              'print(json.dumps(run_chronological_stage(sys.argv[2],"resolve","restart")))\n')
    completed = subprocess.run([sys.executable, '-c', script, ROOT, str(data_root)],
                               capture_output=True, text=True, timeout=30, check=True)
    return json.loads(completed.stdout.splitlines()[-1])


@pytest.mark.parametrize('scenario', [
    'email-to-text', 'siblings', 'partial', 'acknowledgment', 'noncompletion',
    'ambiguous-group', 'missing-access', 'partial-access', 'revoked-access',
    'restart', 'new-request', 'later-explanation',
])
def test_chronological_commitment_matrix_uses_production_golden_path(tmp_path, scenario):
    from copy import deepcopy
    from commitment_journey_fixture import run_chronological_stage
    run = lambda stage: run_chronological_stage(tmp_path, stage, scenario)
    request = run('request')
    assert len(request['state']) == 2
    assert all(row['status'] == 'open' for row in request['state'])
    assert request['closing_source'] not in json.dumps(request['evidence'])
    assert [call['kind'] for call in request['model_calls']] == ['extract', 'critic']
    before = deepcopy(request['state'])
    collected = run('fulfillment')
    assert collected['state'] == before, 'collection has no canonical writer effect'
    if scenario == 'revoked-access':
        assert any(read['source'] == 'gmail' for read in collected['source_reads'])
        assert not any(read['source'] == 'imessage' for read in collected['source_reads'])
    if scenario in ('missing-access', 'partial-access', 'revoked-access'):
        assert collected['gather_receipt']['complete'] is False
    if scenario == 'restart':
        resolved = _resolve_commitment_in_fresh_worker(tmp_path)
    else:
        resolved = run('resolve')
    held = scenario in ('partial', 'acknowledgment', 'noncompletion', 'ambiguous-group',
                        'missing-access', 'partial-access', 'revoked-access')
    if scenario == 'revoked-access':
        assert any(read['source'] == 'gmail' for read in resolved['source_reads'])
        assert not any(read['source'] == 'imessage' for read in resolved['source_reads'])
    if scenario in ('missing-access', 'partial-access', 'revoked-access'):
        assert resolved['gather_receipt']['complete'] is False
    closed = [row for row in resolved['state'] if row['status'] == 'resolved']
    assert len(closed) == (0 if held else 1)
    assert [call['kind'] for call in resolved['model_calls']] == ['extract', 'critic']
    pricing = next(row for row in resolved['state'] if row['summary'] == 'Send the pricing document')
    assert pricing['status'] == ('open' if held else 'resolved')
    if held:
        assert pricing['anchor_key'] in resolved['brief_eligible']
    if scenario in ('partial', 'acknowledgment', 'noncompletion'):
        assert any(call['kind'] == 'extract' and call['has_closing_source'] for call in resolved['model_calls'])
        assert any(read.get('source') == 'imessage' and read.get('identifier') == '+14155550193'
                   for read in resolved['source_reads'])
    sibling = next(row for row in resolved['state'] if row['summary'] == 'Send the launch checklist')
    assert sibling['status'] == 'open'
    if closed:
        row, = closed
        assert row['summary'] == 'Send the pricing document'
        assert row['source_refs'][0]['sourceId'] == request['original_source']
        assert row['resolution_source_refs'][0]['sourceId'] == request['closing_source']
        assert row['anchor_key'] not in resolved['brief_eligible']
        assert row['anchor_key'] not in resolved['reminder_eligible']
    restarted = run('restart')
    assert restarted['state'] == resolved['state']
    assert restarted['model_calls'] == restarted['source_reads'] == []
    if not held:
        item, = restarted['history']['items']
        assert item['status'] == 'resolved'
        assert item['source_refs'][0]['sourceId'] == request['original_source']
        assert item['resolution_source_refs'][0]['sourceId'] == request['closing_source']
        # Replay source and model proposal cannot duplicate or revive the closure.
        assert run('resolve')['state'] == resolved['state']
    if scenario == 'new-request':
        new = run('new-request')
        assert len(new['state']) == 3
        assert next(row for row in new['state'] if row['anchor_key'] == closed[0]['anchor_key']) == closed[0]
        fresh = next(row for row in new['state'] if row['summary'] == 'Send revised pricing document')
        assert fresh['status'] == 'open'
        assert fresh['source_refs'][0]['sourceId'] == 'journey-new-pricing-request'
        assert fresh['canonical_id'] == 'c_abc193'
        assert fresh['anchor_key'] in new['brief_eligible']


@pytest.mark.parametrize('stage', ['request', 'restart'])
def test_chronological_commitment_rejects_lost_due_date(tmp_path, monkeypatch, stage):
    import commitment_journey_fixture as journey
    if stage == 'request':
        original = journey.capture_payload
        def omitted_deadline():
            payload = original()
            for action in payload['new_actions']:
                action.pop('contextDeadline')
            return payload
        monkeypatch.setattr(journey, 'capture_payload', omitted_deadline)
    else:
        journey.run_chronological_stage(tmp_path, 'request', 'partial')
        monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
        writer = journey._golden_runner().cr
        for row in writer._load_items().values():
            row['deadline'] = None
            writer._persist(row)
    with pytest.raises(AssertionError, match='original obligation deadline'):
        journey.run_chronological_stage(tmp_path, stage, 'partial')


def test_chronological_commitment_rejects_unexpected_read_even_when_gather_catches_it(tmp_path, monkeypatch):
    import loop_context
    from commitment_journey_fixture import run_chronological_stage
    run_chronological_stage(tmp_path, 'request')
    monkeypatch.setattr(loop_context, 'graph_aliases', lambda: lambda *_: ['+14155550999'])
    with pytest.raises(AssertionError, match='unexpected native source read'):
        run_chronological_stage(tmp_path, 'fulfillment')


def test_commitment_restart_worker_does_not_forward_an_inflated_parent_import_path(tmp_path, monkeypatch):
    import subprocess
    from commitment_journey_fixture import run_chronological_stage
    run_chronological_stage(tmp_path, 'request', 'restart')
    collected = run_chronological_stage(tmp_path, 'fulfillment', 'restart')
    assert all(row['status'] == 'open' for row in collected['state'])
    # This exceeds Linux's per-argument cap if serialized as one argv item.
    monkeypatch.setattr(sys, 'path', [*sys.path, *(['/duplicate-fixture-import-root'] * 10000)])
    assert len(json.dumps(sys.path)) > 131072
    original = subprocess.run
    calls = []
    def bounded_run(argv, **kwargs):
        calls.append(argv)
        assert sum(len(value) for value in argv) < 8192
        return original(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', bounded_run)
    resolved = _resolve_commitment_in_fresh_worker(tmp_path)
    assert len(calls) == 1
    assert [row['summary'] for row in resolved['state'] if row['status'] == 'resolved'] == ['Send the pricing document']


def test_unreviewed_numbers_cannot_replace_reviewed_baseline(corpus,tmp_path,monkeypatch):
    import copy
    value=copy.deepcopy(rg.load_corpus('corpus-v1',corpus['out']))
    value['labels']['reviewed']=False
    monkeypatch.setattr(rg,'load_corpus',lambda *_:value)
    monkeypatch.setenv('SOTTO_DATA',str(tmp_path))
    baseline=tmp_path/'evals/baselines/golden-corpus-v1.json'
    baseline.parent.mkdir(parents=True)
    baseline.write_text('existing reviewed baseline')
    assert rg.run('corpus-v1',0,False,False,True,True,.05,corpus['out'])==2
    assert baseline.read_text()=='existing reviewed baseline'


def test_replay_revocation_filters_ledger_funnel_and_judge(tmp_path,monkeypatch):
    from datetime import datetime
    import source_context
    monkeypatch.setenv('SOTTO_DATA',str(tmp_path))
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE',raising=False)
    actual_allowed=source_context.allowed
    monkeypatch.setattr(source_context,'allowed',lambda s:s!='gmail' and actual_allowed(s))
    rg.cr._persist(dict(anchor_key='task:disabled',status='open',action_type='follow_up',
        source='imessage',channel='imessage',contact_name='Dana Reed',contact_identifier='+15550001111',
        created_at='2026-10-12',summary='PRIVATE_LEDGER',ask='PRIVATE_LEDGER'))
    day={'name':'revoked','inputs':{'type':'morning','local':{
        'source_status':{'imessage':'disabled'},'imessage':[{'_gid':'text','handle':'+15550001111',
            'text':'PRIVATE_TEXT','timestamp':'2026-10-13T10:00:00Z'}]},
        'google':{'emails':[{'_gid':'mail','id':'mail','from':'Dana <dana@example.com>',
            'subject':'PRIVATE_EMAIL','snippet':'PRIVATE_EMAIL'}],'events':[]}}}
    prompts=[]
    def model(prompt,_inputs):
        prompts.append(prompt)
        if 'INDEPENDENT judge' in prompt:
            return json.dumps(dict(fabrication=10,voice=10,triage=10))
        return json.dumps({'brief_markdown':'Nothing new.','actions':[],'extracted_knowledge':{}})
    monkeypatch.setattr(rg,'gemini_call',model)
    base=datetime(2026,10,13,12)
    res=rg.replay_day(day,base,str(tmp_path),True)
    assert res['exception'] is None
    assert res['verdicts']=={}
    assert rg.judge_day(res,day,base)['fabrication']==10
    assert len(prompts)>=2
    assert not any(token in p for p in prompts for token in ('PRIVATE_TEXT','PRIVATE_EMAIL','PRIVATE_LEDGER'))
    count=len(prompts)
    res['_source_permissions']=['gmail']
    assert 'error' in rg.judge_day(res,day,base)
    assert len(prompts)==count


def test_failed_judge_does_not_update_baseline(corpus,tmp_path,monkeypatch,capsys):
    _review(corpus['out'])
    monkeypatch.setenv('SOTTO_DATA',str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY','offline')
    monkeypatch.setattr(rg,'gemini_call',rg._stub_llm)
    monkeypatch.setattr(rg,'_replay_funnel',lambda *_a,**_k:{})
    monkeypatch.setattr(rg,'judge_day',lambda *_:{'error':'unavailable'})
    assert rg.run('corpus-v1',0,True,True,False,True,.05,corpus['out'])==1
    assert not (tmp_path/'evals/baselines/golden-corpus-v1.json').exists()
    output=capsys.readouterr().out
    coverage=json.loads(output.split('COVERAGE: ')[1].splitlines()[0])
    assert coverage['failed']==coverage['expected'] and coverage['executed']==[]


@pytest.mark.parametrize('live',[False,True])
def test_funnel_model_failure_is_execution_failure_only_when_live(corpus,tmp_path,monkeypatch,live):
    import triage_event
    import shutil
    value=rg.load_corpus('corpus-v1',corpus['out'])
    base=rg.frozen_base()
    sandbox=rg._seed_sandbox(value,rg.oldest_base(value,base))
    monkeypatch.setenv('SOTTO_DATA',sandbox)
    monkeypatch.setenv('GOOGLE_AI_API_KEY','offline-never-use')
    calls=[]
    def fail(*args):
        calls.append(True)
        raise RuntimeError('scripted classifier failure')
    monkeypatch.setattr(triage_event,'_classify_tier1',fail)
    monkeypatch.setattr(rg,'gemini_call',rg._stub_llm)
    try:
        results=[rg.replay_day(day,rg.base_for(day,base),sandbox,live) for day in value['days']]
    finally:
        shutil.rmtree(sandbox)
    if live:
        assert calls
        assert any(res['exception']=='RuntimeError: funnel execution failed' for res in results)
    else:
        assert not calls
        assert all(res['exception'] is None for res in results)


@pytest.mark.parametrize('raw_score', ['NaN', '1e999', '-1e999', '-1', '11'])
@pytest.mark.parametrize('update', [False, True])
def test_invalid_judge_score_cannot_pass_or_replace_baseline(
        corpus, tmp_path, monkeypatch, capsys, raw_score, update):
    _review(corpus['out'])
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'offline-never-use')
    monkeypatch.setattr(rg, '_replay_funnel', lambda *_a, **_k: {})
    score = '9'

    def scripted_model(prompt, inputs):
        if 'INDEPENDENT judge' in prompt:
            return '{"fabrication":9,"voice":' + score + ',"triage":9}'
        return rg._stub_llm(prompt, inputs)

    monkeypatch.setattr(rg, 'gemini_call', scripted_model)
    assert rg.run('corpus-v1', 0, True, True, False, True, .05, corpus['out']) == 0
    baseline = tmp_path / 'evals/baselines/golden-corpus-v1.json'
    before = baseline.read_bytes()
    capsys.readouterr()
    score = raw_score
    assert rg.run('corpus-v1', 0, True, True, False, update, .05, corpus['out']) == 1
    output = capsys.readouterr().out
    coverage = json.loads(output.split('COVERAGE: ')[1].splitlines()[0])
    assert coverage['failed'] == coverage['expected'] and not coverage['executed']
    assert 'RESULT: PASS' not in output and baseline.read_bytes() == before


@pytest.mark.parametrize('metric,value', [
    ('metric', float('nan')), ('metric', float('inf')), ('metric', -float('inf')),
    ('judge_voice', -.1), ('judge_voice', 1.1),
])
def test_invalid_numeric_score_is_rejected_by_aggregate_and_gate(
        tmp_path, monkeypatch, capsys, metric, value):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    with pytest.raises(ValueError, match='invalid score'):
        rg.aggregate([('day', metric, value, '')])
    sig = {'mode': 'live+judge', 'sample': 1}
    assert rg._baseline_gate('probe', {metric: .9}, True, .05, sig, _coverage()) == 0
    baseline = tmp_path / 'evals/baselines/golden-probe.json'
    before = baseline.read_bytes()
    for update in (False, True):
        capsys.readouterr()
        assert rg._baseline_gate('probe', {metric: value}, update, .05, sig, _coverage()) == 1
        assert 'RESULT: PASS' not in capsys.readouterr().out
        assert baseline.read_bytes() == before
    # An older invalid baseline must not make subsequent valid runs pass either.
    doc = json.loads(before)
    doc['scores'][metric] = value
    baseline.write_text(json.dumps(doc))
    invalid_before = baseline.read_bytes()
    assert rg._baseline_gate('probe', {metric: .9}, False, .05, sig, _coverage()) == 1
    assert baseline.read_bytes() == invalid_before


def test_valid_judge_endpoints_and_signed_entity_delta_still_score():
    assert rg.aggregate([
        ('day', 'judge_voice', 0, ''), ('day', 'judge_triage', 1, ''),
        ('day', 'entity_dedup', -2, ''),
    ]) == {'judge_voice': 0, 'judge_triage': 1, 'entity_dedup': 2}


def test_nonfinite_prf_aggregate_is_rejected():
    with pytest.raises(ValueError, match='invalid aggregate'):
        rg.aggregate([('day', 'open_loops', {
            'f1': float('nan'), 'tp': float('nan'), 'expected': 1, 'actual': 1,
        }, '')])


def test_opposite_entity_errors_cannot_cancel_across_days():
    assert rg.aggregate([('day1', 'entity_dedup', -2, ''),
                         ('day2', 'entity_dedup', 2, '')])['entity_dedup'] == 2


def test_the_judge_model_wins_over_compose_overrides_and_fallback(monkeypatch):
    import gemini_transport
    monkeypatch.setenv('SOTTO_BRIEF_MODEL', 'anthropic/claude-sonnet-4-6')
    monkeypatch.setenv('SOTTO_FALLBACK_MODEL', 'anthropic/claude-haiku')
    monkeypatch.delenv('SOTTO_JUDGE_MODEL', raising=False)
    seen = []
    def model(prompt, _inputs):
        seen.append((gemini_transport.effective_compose_model(), os.environ.get('SOTTO_FALLBACK_MODEL')))
        return json.dumps(dict(fabrication=9, voice=9, triage=9))
    monkeypatch.setattr(rg, 'gemini_call', model)
    monkeypatch.setattr(rg.ev, 'resolve_tokens', lambda day, base: {'inputs': {}})
    res = {'brief': 'Nothing new.', '_judge_inputs': {}}
    assert rg.judge_day(res, {}, rg.datetime(2026, 10, 13, 12))['voice'] == 9
    assert seen == [(('gemini', rg.JUDGE_MODEL), '')]
    assert os.environ['SOTTO_BRIEF_MODEL'] == 'anthropic/claude-sonnet-4-6'
    assert os.environ['SOTTO_FALLBACK_MODEL'] == 'anthropic/claude-haiku'


def test_baseline_signature_names_the_models_it_was_measured_with(monkeypatch):
    monkeypatch.setenv('SOTTO_BRIEF_MODEL', 'gemini/gemini-3.8-flash')
    monkeypatch.setenv('SOTTO_JUDGE_MODEL', 'gemini-judge-x')
    live = rg.run_signature(True, True, 5, 'enabled')
    assert (live['compose_model'], live['judge_model']) == ('gemini/gemini-3.8-flash', 'gemini-judge-x')
    monkeypatch.setenv('SOTTO_BRIEF_MODEL', 'gemini/another-model')
    assert rg.run_signature(True, True, 5, 'enabled') != live
    assert 'compose_model' not in rg.run_signature(False, False, 5, 'disabled')


def test_a_blank_judge_override_means_the_pinned_default(monkeypatch):
    monkeypatch.setenv('SOTTO_JUDGE_MODEL', '  ')
    assert rg.judge_model() == rg.JUDGE_MODEL
    monkeypatch.setenv('SOTTO_JUDGE_MODEL', ' gemini-judge-x ')
    assert rg.judge_model() == 'gemini-judge-x'


def test_a_signature_mismatch_names_each_field_that_differs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path / 'vol'))
    recorded = {'mode': 'live', 'sample': 5, 'compose_model': 'gemini/gemini-3.8-flash'}
    assert rg._baseline_gate('corpus-v1', {'metric': 0.9}, True, 0.05, recorded, _coverage()) == 0
    capsys.readouterr()
    current = dict(recorded, compose_model='gemini/gemini-3-flash-preview')
    assert rg._baseline_gate('corpus-v1', {'metric': 0.9}, False, 0.05, current, _coverage()) == 1
    out = capsys.readouterr().out
    assert '- compose_model: "gemini/gemini-3.8-flash" → "gemini/gemini-3-flash-preview"' in out
    assert 'sample' not in out.split('COVERAGE MISMATCH')[1]


def test_live_compose_runs_without_a_fallback_model(corpus, tmp_path, monkeypatch):
    # The signature names one compose model; a fallback would score another model's briefs.
    _review(corpus['out'])
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path / 'vol'))
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'offline-stub')
    monkeypatch.setenv('SOTTO_FALLBACK_MODEL', 'gemini-other')
    seen = []
    def replay(*_args):
        seen.append(os.environ.get('SOTTO_FALLBACK_MODEL'))
        return {'exception': None, 'brief': 'scripted'}
    monkeypatch.setattr(rg, 'replay_day', replay)
    monkeypatch.setattr(rg, 'score_day', lambda *_args: [('metric', .9, 'scripted')])
    rg.run('corpus-v1', 0, True, False, False, True, .05, corpus['out'])
    assert seen and set(seen) == {''}
    assert os.environ['SOTTO_FALLBACK_MODEL'] == 'gemini-other'
