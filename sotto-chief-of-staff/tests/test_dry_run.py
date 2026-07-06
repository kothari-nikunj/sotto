"""End-to-end dry-run: fixture bundle -> brief + exhaust, no live LLM (M1 DoD)."""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))

spec = importlib.util.spec_from_file_location("dry_run", os.path.join(ROOT, "tools", "dry_run.py"))
dr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dr)


def test_dry_run_full_loop(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    bundle = json.load(open(os.path.join(ROOT, "tools", "fixtures", "brief_bundle.json")))
    result = dr.run(bundle)

    # brief has all four sections rendered
    md = result["brief_markdown"]
    assert "Needs attention" in md and "Already handled" in md
    assert "signed contract" in md

    # knowledge graph learned + written
    assert result["knowledge_applied"]["applied"]["new"] == 2
    assert os.path.exists(os.path.join(str(tmp_path), "knowledge", "people", "sarah-chen.md"))
    assert os.path.exists(os.path.join(str(tmp_path), "knowledge", "companies", "acme.md"))

    # continuity: the replied thread resolved, the rest active
    assert result["continuity"]["resolved"] == 1
    assert result["continuity"]["active"] == 2
