"""research_attendees.py — batched grounded research (port of gemini-research.ts): stub, dedup/cap, batching."""
import importlib.util, json, os, sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))
spec = importlib.util.spec_from_file_location("ra", os.path.join(ROOT, "_shared", "scripts", "research_attendees.py"))
ra = importlib.util.module_from_spec(spec); spec.loader.exec_module(ra)


def test_stub_returns_attendees(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"attendees": [{"email": "a@b.com", "company": "Acme", "summary": "bio"}]}))
    monkeypatch.setenv("SOTTO_LLM_STUB", str(p))
    out = ra.research([{"name": "A", "email": "a@b.com"}], "")
    assert out["attendees"][0]["company"] == "Acme"


def test_no_key_or_empty_degrades(monkeypatch):
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)
    assert ra.research([{"name": "A", "email": "a@b.com"}], "") == {"attendees": []}
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    assert ra.research([], "") == {"attendees": []}


def test_dedup_cap_and_batching(monkeypatch):
    # 12 unique + 1 dupe → 12 researched, batched by 5 → 3 batches. Capture batches via a fake.
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    seen_batches = []
    monkeypatch.setattr(ra, "_research_batch",
                        lambda batch, ctx, key: (seen_batches.append(len(batch)) or
                                                 [{"email": a["email"], "company": "C", "summary": "s"} for a in batch]))
    people = [{"name": f"P{i}", "email": f"p{i}@x.com"} for i in range(12)] + [{"name": "P0", "email": "P0@x.com"}]
    out = ra.research(people, "ctx")
    assert len(out["attendees"]) == 12               # dupe (case-insensitive) dropped
    assert sorted(seen_batches) == [2, 5, 5]          # 12 → batches of 5,5,2


def test_context_summary_sorted():
    s = ra._context_summary([{"summary": "B", "start": "2026-06-29T15:00"},
                             {"summary": "A", "start": "2026-06-29T09:00"}])
    assert s.index('"A"') < s.index('"B"')           # soonest first
