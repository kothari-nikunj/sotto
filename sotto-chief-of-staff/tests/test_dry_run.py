"""End-to-end dry-run: fixture bundle -> brief + exhaust, no live LLM (M1 DoD)."""
import importlib.util
import json
import os

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location("dry_run", os.path.join(ROOT, "tools", "dry_run.py"))
dr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dr)


def test_dry_run_full_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    bundle = json.load(open(os.path.join(ROOT, "tools", "fixtures", "brief_bundle.json")))
    # Simulate the separate positive critic verdict, not a self-authored receipt in model JSON.
    cr = dr._load("morning-brief/scripts/continuity_resolve.py", "dry_run_critic_fixture")
    for update in bundle.get("loop_updates", []):
        update["criticReceipt"] = cr.record_critic_approval(update)
    result = dr.run(bundle)

    # brief has all four sections rendered
    md = result["brief_markdown"]
    assert "Needs attention" in md and "Already handled" in md
    assert "signed contract" in md

    # knowledge graph learned + written
    assert result["knowledge_applied"]["applied"]["new"] == 2
    import knowledge as kg
    assert kg.find_person_file(name="Sarah Chen") is not None  # cid-keyed person file written
    assert os.path.exists(os.path.join(str(tmp_path), "knowledge", "companies", "acme.md"))

    # continuity: the replied thread resolved, the rest active
    assert result["continuity"]["resolved"] == 1
    assert result["continuity"]["active"] == 2
