"""persist_prep.py — meeting-prep research persisted to the graph + fresh-profile research skip."""
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pp = _load("persist_prep", "meeting-prep/scripts/persist_prep.py")
import knowledge as kg  # noqa: E402

RESEARCH = {"attendees": [
    {"email": "dana@acme.com", "title": "CEO", "company": "Acme", "summary": "Leads Acme's platform org."},
]}
ATTENDEES_IN = [{"name": "Dana Roe", "email": "dana@acme.com", "meeting_title": "Sync"}]


def _person(tmp_path, name="Dana Roe", email="dana@acme.com"):
    path = kg.find_person_file(name=name, identifier=email)
    with open(path, encoding="utf-8") as f:
        return path, kg.parse_person_file(f.read())


def test_persist_writes_low_confidence_sourced_facts(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    out = pp.persist(RESEARCH, ATTENDEES_IN)
    assert out["persisted"] == 1 and out["people"] == ["Dana Roe"]
    _, p = _person(tmp_path)
    facts = list(p.facts.values())
    assert len(facts) == 1
    f = facts[0]
    # Identity comes from research ONLY, clearly sourced, one combined fact per attendee.
    assert f.text == "Per web search: CEO at Acme — Leads Acme's platform org."
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert f.conf == 0.55                                       # prewarm's low-confidence decay pattern
    assert f.source_ref == f"meeting-prep-research:{today}"
    assert p.company is None and p.title is None               # never authoritative identity fields
    assert "dana@acme.com" in p.identifiers


def test_persist_skips_attendee_with_nothing_grounded(tmp_path, monkeypatch):
    # Research returned no title/company/summary → nothing is invented, nothing is written.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    out = pp.persist({"attendees": [{"email": "ghost@x.com", "title": "", "company": "", "summary": ""}]},
                     [{"name": "Ghost", "email": "ghost@x.com"}])
    assert out["persisted"] == 0
    assert kg.find_person_file(name="Ghost", identifier="ghost@x.com") is None


def test_persist_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(RESEARCH, ATTENDEES_IN)
    pp.persist(RESEARCH, ATTENDEES_IN)                          # same research twice
    _, p = _person(tmp_path)
    assert len(p.facts) == 1                                    # deduped (bumped), not duplicated
    assert all(f.seen == 2 for f in p.facts.values())


def test_filter_fresh_skips_recently_persisted_person(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(RESEARCH, ATTENDEES_IN)                          # Dana now has a fresh research profile
    attendees = [{"name": "Dana Roe", "email": "dana@acme.com"},
                 {"name": "New Person", "email": "new@x.com"}]
    kept, skipped = pp.filter_fresh(attendees)
    assert skipped == ["Dana Roe"]
    assert [a["email"] for a in kept] == ["new@x.com"]


def test_filter_fresh_matches_by_email_identifier(tmp_path, monkeypatch):
    # Calendar shows a different display name — the email identifier still matches the fresh profile.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(RESEARCH, ATTENDEES_IN)
    kept, skipped = pp.filter_fresh([{"name": "D. Roe", "email": "dana@acme.com"}])
    assert kept == [] and skipped == ["D. Roe"]


def test_persist_stamps_last_researched(tmp_path, monkeypatch):
    # persist() must stamp WHEN the research happened — profile_is_fresh keys off this, not mtime.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(RESEARCH, ATTENDEES_IN)
    _, p = _person(tmp_path)
    assert p.last_researched == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_stale_research_is_re_researched(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(RESEARCH, ATTENDEES_IN)
    # 40 days later the research stamp is stale — even though the FILE keeps getting rewritten
    # (mtime = now), the person must be researched again.
    later = datetime.now(timezone.utc) + timedelta(days=40)
    kept, skipped = pp.filter_fresh([{"name": "Dana Roe", "email": "dana@acme.com"}], now=later)
    assert skipped == [] and len(kept) == 1                     # stale → research again


def test_recent_mtime_or_company_alone_is_not_fresh(tmp_path, monkeypatch):
    # A just-written profile with a company/title but NO last_researched stamp (every legacy
    # profile, or one only touched by brief rewrites) is NOT fresh — one re-research is correct.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    people = os.path.join(str(tmp_path), "knowledge", "people")
    os.makedirs(people, exist_ok=True)
    with open(os.path.join(people, "legacy.md"), "w") as f:
        f.write("---\nschema: 1\ncanonical_id: c_l\nname: Legacy\ncompany: Acme\ntitle: CEO\n"
                "identifiers: [legacy@x.com]\nfacts: {}\n---\n")
    assert os.path.getmtime(os.path.join(people, "legacy.md")) > time.time() - 60  # mtime is fresh
    assert not pp.profile_is_fresh("Legacy", "legacy@x.com")


def test_profile_without_identity_signal_is_not_fresh(tmp_path, monkeypatch):
    # A bare stub (no research stamp) doesn't block research even if recent.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    people = os.path.join(str(tmp_path), "knowledge", "people")
    os.makedirs(people, exist_ok=True)
    with open(os.path.join(people, "thin-stub.md"), "w") as f:
        f.write("---\nschema: 1\ncanonical_id: c_t\nname: Thin Stub\n"
                "identifiers: [thin@x.com]\nfacts: {}\n---\n")
    assert not pp.profile_is_fresh("Thin Stub", "thin@x.com")


def test_cli_filter_rewrites_file_in_place(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(RESEARCH, ATTENDEES_IN)
    infile = tmp_path / "research_in.json"
    infile.write_text(json.dumps([{"name": "Dana Roe", "email": "dana@acme.com"},
                                  {"name": "New Person", "email": "new@x.com"}]))
    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "meeting-prep", "scripts", "persist_prep.py"),
         "--filter-fresh", str(infile)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"kept": 1, "skipped_fresh": ["Dana Roe"]}
    assert [a["email"] for a in json.loads(infile.read_text())] == ["new@x.com"]


def test_persist_recency_facts_carry_web_research_provenance(tmp_path, monkeypatch):
    # Pass-B items persist as separate facts: source="web_research" (NOT the extraction label),
    # source_ref = the item's actual URL, confidence 0.6 for dated+sourced items. Hooks never persist.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    research = {"attendees": [dict(RESEARCH["attendees"][0],
        recent_activity=[{"when": "late July 2026", "what": "Published an agent-memory piece.",
                          "source_url": "https://s.com/mem"}],
        personal=["Ran the SF Marathon (https://x.com/d/1)"],
        conversation_hooks=["Her piece last week fits your roadmap."])]}
    out = pp.persist(research, ATTENDEES_IN)
    assert out["persisted"] == 1
    _, p = _person(tmp_path)
    by_text = {f.text: f for f in p.facts.values()}
    assert len(by_text) == 3                                   # bio + activity + personal
    bio = by_text["Per web search: CEO at Acme — Leads Acme's platform org."]
    assert bio.source == "web_research" and bio.conf == 0.55
    act = by_text["Per web search (late July 2026): Published an agent-memory piece."]
    assert act.source == "web_research" and act.source_ref == "https://s.com/mem" and act.conf == 0.6
    per = by_text["Per web search: Ran the SF Marathon (https://x.com/d/1)"]
    assert per.source == "web_research" and per.source_ref == "https://x.com/d/1"
    assert per.type == "personal" and per.conf == 0.6
    assert not any("roadmap" in t for t in by_text)             # hooks are ephemeral


def test_persist_drops_unsourced_recency_items(tmp_path, monkeypatch):
    # The graph's last line of defense: url-less activity / unsourced personal never persist,
    # even if an upstream filter missed them.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    research = {"attendees": [dict(RESEARCH["attendees"][0],
        recent_activity=[{"when": "August", "what": "Allegedly did a thing.", "source_url": ""}],
        personal=["Has two kids"])]}
    pp.persist(research, ATTENDEES_IN)
    _, p = _person(tmp_path)
    texts = [f.text for f in p.facts.values()]
    assert texts == ["Per web search: CEO at Acme — Leads Acme's platform org."]


def test_persist_company_fallback_and_sentinel_suppressed(tmp_path, monkeypatch):
    # Person → company degradation: the sentinel "No public profile found." is a research MISS and
    # never becomes a fact; the company_summary DOES persist (with the research provenance label).
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    research = {"attendees": [{"email": "nelson@cobalt-research.com", "title": None,
                               "company": "Cobalt Research", "summary": "No public profile found.",
                               "company_summary": "Cobalt Research does battery-materials analytics."}]}
    out = pp.persist(research, [{"name": "Nelson", "email": "nelson@cobalt-research.com"}])
    assert out["persisted"] == 1
    path = kg.find_person_file(name="Nelson", identifier="nelson@cobalt-research.com")
    with open(path, encoding="utf-8") as f:
        p = kg.parse_person_file(f.read())
    (fact,) = p.facts.values()
    assert fact.text == "Per web search: Cobalt Research — Cobalt Research does battery-materials analytics."
    assert "No public profile found" not in fact.text
    assert fact.source == "web_research"


def test_cli_persist_noop_on_missing_input(tmp_path):
    import subprocess
    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "meeting-prep", "scripts", "persist_prep.py"),
         "--research", str(tmp_path / "nope.json")], env=env, capture_output=True, text=True)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"persisted": 0, "people": []}
