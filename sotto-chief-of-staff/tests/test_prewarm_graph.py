"""prewarm_graph.py — seed the graph at setup so brief #1 isn't cold (safe stubs; opt-in research)."""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "morning-brief", "scripts"))

spec = importlib.util.spec_from_file_location(
    "prewarm_graph", os.path.join(ROOT, "_shared", "scripts", "prewarm_graph.py"))
pw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pw)
import knowledge as kg  # noqa: E402

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


def _msg(name, days_ago, handle):
    ts = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return {"handle": handle, "resolved_name": name, "is_from_me": False,
            "is_group_chat": False, "timestamp": ts, "text": "hi"}


def _local():
    msgs = []
    for d in (10, 8, 6, 4):                    # Dhruv: 4 touches (emailed contact)
        msgs.append(_msg("Dhruv", d, "+1"))
    for d in (9, 7, 5):                         # Sarah: 3 touches (phone-only)
        msgs.append(_msg("Sarah", d, "+2"))
    msgs.append(_msg("Tom", 3, "+3"))          # Tom: 1 touch → below MIN_INTERACTIONS, excluded
    return {
        "contacts": [
            {"name": "Dhruv", "phones": ["+1"], "emails": ["dhruv@acme.com"]},
            {"name": "Sarah", "phones": ["+2"]},
            {"name": "Tom", "phones": ["+3"]},
        ],
        "imessage": msgs,
    }


def test_stubs_only_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_PREWARM_RESEARCH", raising=False)
    out = pw.prewarm(_local())
    assert out["researched"] == 0
    assert set(out["people"]) == {"Dhruv", "Sarah"}        # Tom (one-off) excluded
    dhruv = open(kg.find_person_file(name="Dhruv", identifier="dhruv@acme.com")).read()
    assert "dhruv@acme.com" in dhruv                       # identity stub carries the email
    assert "## Facts" not in dhruv                         # but NO invented facts (no role/company guess)
    assert kg.find_person_file(name="Sarah") is not None


def test_opt_in_research_writes_low_conf_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_PREWARM_RESEARCH", "1")
    stub = tmp_path / "research.json"
    stub.write_text(json.dumps({"attendees": [
        {"email": "dhruv@acme.com", "title": "Eng Lead", "company": "Acme",
         "summary": "Builds infra tools.", "relevance": []}]}))
    monkeypatch.setenv("SOTTO_LLM_STUB", str(stub))
    out = pw.prewarm(_local())
    assert out["researched"] == 1
    dhruv = open(kg.find_person_file(name="Dhruv", identifier="dhruv@acme.com")).read()
    assert "Per web search" in dhruv and "Acme" in dhruv   # clearly-sourced, not authoritative identity
    assert "company: Acme" not in dhruv                    # research never becomes a hard identity field
    # research path stamps last_researched, so persist_prep's fresh filter skips re-researching
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert f"last_researched: '{today}'" in dhruv or f"last_researched: {today}" in dhruv
    ppspec = importlib.util.spec_from_file_location(
        "persist_prep_pw", os.path.join(ROOT, "meeting-prep", "scripts", "persist_prep.py"))
    ppm = importlib.util.module_from_spec(ppspec)
    ppspec.loader.exec_module(ppm)
    assert ppm.profile_is_fresh("Dhruv", "dhruv@acme.com")


def test_empty_input_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    out = pw.prewarm({})
    assert out == {"stubs": 0, "researched": 0, "people": []}
