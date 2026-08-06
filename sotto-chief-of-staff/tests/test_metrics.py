"""metrics.py — the per-run cost/latency accumulator + its wiring into compose_brief.

Observability must never block delivery, stay stdlib-only, and add ~nothing to the hot path. These
tests lock: aggregation, cost estimation (priced vs unpriced → n/a), the grep-friendly line format,
the jsonl record + rotation, thread-safe accumulation, no-op safety without a run, and one stubbed
end-to-end compose run that proves the [brief-cost] line + jsonl record actually get written.
"""
import importlib.util
import json
import os
import sys
import threading

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))

import metrics  # noqa: E402

spec = importlib.util.spec_from_file_location("compose_brief", os.path.join(ROOT, "_shared", "scripts", "compose_brief.py"))
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)

MODEL = "gemini-3-flash-preview"


def test_record_and_summary_aggregates_per_phase():
    metrics.start_run()
    metrics.record("extraction", 41.2, 98000, 3200, MODEL)
    metrics.record("critic", 22.0, 101000, 1400, MODEL)
    metrics.record("research", 2.0, 6000, 200, MODEL)
    metrics.record("research", 3.2, 6000, 200, MODEL)   # two research batches → one 'research' phase
    s = metrics.summary()
    assert s["calls"] == 4
    assert s["total_wall_s"] == 68.4                    # 41.2 + 22.0 + 2.0 + 3.2
    assert s["prompt_tokens"] == 211000 and s["output_tokens"] == 5000
    assert s["phases"]["research"]["calls"] == 2
    assert s["phases"]["research"]["prompt_tokens"] == 12000
    assert s["phases"]["research"]["wall_s"] == 5.2


def test_cost_priced_model_and_unpriced_is_none():
    metrics.start_run()
    metrics.record("extraction", 1.0, 1_000_000, 1_000_000, MODEL)
    rate = metrics.PRICE_TABLE[MODEL]
    assert metrics.summary()["est_cost_usd"] == round(rate["in"] + rate["out"], 4)
    # an unknown model anywhere in the run → total cost cannot be trusted → None (n/a), never a guess
    metrics.record("critic", 1.0, 1000, 1000, "some-unknown-model")
    assert metrics.summary()["est_cost_usd"] is None


def test_stub_shaped_call_is_unpriced_na():
    # empty model (how the stub path records) is unpriced → est n/a even though tokens are 0
    metrics.start_run()
    metrics.record("extraction", 0.0, 0, 0, "")
    assert metrics.summary()["est_cost_usd"] is None


def test_human_line_format_is_grep_friendly():
    metrics.start_run()
    metrics.record("extraction", 41.2, 98000, 3200, MODEL)
    metrics.record("critic", 22.0, 101000, 1400, MODEL)
    line = metrics._human_line("2026-07-06", "morning", metrics.summary(), ["revise"])
    assert line.startswith("[brief-cost] kind=morning date=2026-07-06 ")
    assert "total=63.2s" in line and "calls=2" in line
    assert "extract=41.2s/98k/3.2k" in line             # extraction renders as 'extract'
    assert "critic=22.0s/101k/1.4k" in line
    assert "est=$" in line                               # priced model → a dollar figure
    assert "skipped=revise" in line
    assert "revise=" not in line.split("skipped=")[0]    # a skipped phase has no data segment


def test_human_line_unpriced_shows_na():
    metrics.start_run()
    metrics.record("extraction", 0.0, 0, 0, "")
    assert "est=n/a" in metrics._human_line("2026-07-06", "evening", metrics.summary(), [])


def test_fmt_k_thresholds():
    assert metrics._fmt_k(98000) == "98k"
    assert metrics._fmt_k(9100) == "9.1k"
    assert metrics._fmt_k(400) == "0.4k"
    assert metrics._fmt_k(0) == "0.0k"
    assert metrics._fmt_k(12000) == "12k"


def test_emit_writes_line_and_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    metrics.start_run()
    metrics.record("extraction", 5.0, 1000, 200, MODEL)
    line = metrics.emit("2026-07-06", "morning", ["critic", "revise"])
    assert line.startswith("[brief-cost] kind=morning")
    # human line went to the shared brief log
    assert "[brief-cost]" in (tmp_path / "logs" / "compose_brief.log").read_text()
    # structured record appended to brief_metrics.jsonl
    rows = (tmp_path / "logs" / "brief_metrics.jsonl").read_text().strip().splitlines()
    rec = json.loads(rows[-1])
    assert rec["kind"] == "morning" and rec["date"] == "2026-07-06"
    assert rec["calls"] == 1 and rec["prompt_tokens"] == 1000
    assert rec["phases"]["extraction"]["output_tokens"] == 200
    assert rec["skipped"] == ["critic", "revise"] and "ts" in rec


def test_jsonl_rotation_keeps_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    logdir = tmp_path / "logs"; logdir.mkdir()
    path = logdir / "brief_metrics.jsonl"
    path.write_text("".join(f'{{"n":{i}}}\n' for i in range(4000)))   # > 2MB? force via monkeypatch
    monkeypatch.setattr(metrics, "_MAX_BYTES", 1)                     # force a rotation on next append
    metrics.start_run()
    metrics.record("extraction", 1.0, 10, 10, MODEL)
    metrics.emit("2026-07-06", "morning")
    lines = path.read_text().strip().splitlines()
    assert len(lines) <= metrics._KEEP_LINES + 1                     # trimmed to the tail + our record
    assert json.loads(lines[-1])["kind"] == "morning"                # newest record survived


def test_record_without_run_is_noop_safe():
    metrics.start_run()
    metrics._run = None                      # simulate "no active run"
    metrics.record("extraction", 1.0, 100, 100, MODEL)   # must not raise
    assert metrics.summary()["calls"] == 0


def test_record_is_thread_safe():
    metrics.start_run()

    def worker():
        for _ in range(200):
            metrics.record("research", 0.01, 10, 5, MODEL)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert metrics.summary()["calls"] == 8 * 200          # no lost updates under contention


def test_stubbed_compose_emits_brief_cost_line(tmp_path, monkeypatch):
    # End-to-end plumbing WITHOUT a live key: SOTTO_LLM_STUB → compose records a 0-token extraction
    # call, then emits the [brief-cost] line + a jsonl record.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    resp = tmp_path / "resp.json"
    resp.write_text(json.dumps({"brief_markdown": "# Brief", "actions": []}))
    monkeypatch.setenv("SOTTO_LLM_STUB", str(resp))
    out = cb.compose({"type": "morning", "google": {}, "local": {}})
    assert out["brief_markdown"] == "# Brief"
    log = (tmp_path / "logs" / "compose_brief.log").read_text()
    assert "[brief-cost] kind=morning" in log
    assert "est=n/a" in log                               # stub is unpriced
    assert "extract=" in log and "skipped=critic,revise" in log
    rec = json.loads((tmp_path / "logs" / "brief_metrics.jsonl").read_text().strip().splitlines()[-1])
    assert rec["kind"] == "morning" and rec["calls"] == 1
    assert rec["est_cost_usd"] is None
