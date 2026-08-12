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


def test_filter_fresh_keep_exception_for_the_focused_person(tmp_path, monkeypatch):
    # Focus mode: the named person survives the freshness filter (the deep dive needs a
    # company_deep no earlier sweep run ever produced); everyone else fresh is still skipped.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(RESEARCH, ATTENDEES_IN)                          # Dana is fresh
    pp.persist({"attendees": [{"email": "spencer@commenda.io", "title": "CEO",
                               "company": "Commenda", "summary": "Founder."}]},
               [{"name": "Spencer Kim", "email": "spencer@commenda.io"}])   # Spencer too
    attendees = [{"name": "Dana Roe", "email": "dana@acme.com"},
                 {"name": "Spencer Kim", "email": "spencer@commenda.io"}]
    kept, skipped = pp.filter_fresh(attendees, keep="spencer")
    assert [a["email"] for a in kept] == ["spencer@commenda.io"]
    assert skipped == ["Dana Roe"]
    # Same rule as --focus: an '@' argument is an exact email, and no --keep changes nothing.
    kept, skipped = pp.filter_fresh(attendees, keep="dana@acme.com")
    assert [a["email"] for a in kept] == ["dana@acme.com"] and skipped == ["Spencer Kim"]
    assert pp.filter_fresh(attendees)[0] == []


def test_cli_keep_survives_the_in_place_rewrite(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(RESEARCH, ATTENDEES_IN)
    infile = tmp_path / "research_in.json"
    infile.write_text(json.dumps([{"name": "Dana Roe", "email": "dana@acme.com"},
                                  {"name": "New Person", "email": "new@x.com"}]))
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "meeting-prep", "scripts", "persist_prep.py"),
         "--filter-fresh", str(infile), "--keep", "Dana"],
        env=dict(os.environ, SOTTO_DATA=str(tmp_path)), capture_output=True, text=True)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"kept": 2, "skipped_fresh": []}
    assert [a["email"] for a in json.loads(infile.read_text())] == ["dana@acme.com", "new@x.com"]


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
    assert json.loads(proc.stdout) == {"persisted": 0, "people": [], "companies": []}


# ── Company research compounds into COMPANY files ────────────────────────────────────────────────
# The doctrine: every token spent on research must leave behind a durable, structured, correctable
# fact — and knowledge about an ORGANIZATION belongs to the organization, not to whichever human
# happened to be researched that morning.

FOCUS_RESEARCH = {"attendees": [
    {"email": "spencer@commenda.io", "title": "CEO", "company": "Commenda",
     "summary": "Founder and CEO of Commenda.",
     "company_summary": "Commenda handles global tax filing.",
     "company_deep": {"company": "Commenda",
                      "builds": ["Global tax filing tied to each payment"],
                      "founder_story": "Built after a misfiled quarter in three countries.",
                      "traction": ["Series A led by Nexus, June 2026 (https://tc.com/a)"],
                      "market": "The post-EOR shift; competes with Deel and Remote."}}]}


def _company(tmp_path, slug="commenda"):
    return (tmp_path / "knowledge" / "companies" / f"{slug}.md").read_text()


def test_company_deep_dive_lands_in_the_company_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    out = pp.persist(FOCUS_RESEARCH, [{"name": "Spencer Kim", "email": "spencer@commenda.io"}])
    assert out["companies"] == ["Commenda"]
    body = _company(tmp_path)
    # The DURABLE half is the About: what it builds, who founded it, the market.
    assert "## About" in body
    assert "Global tax filing tied to each payment" in body
    assert "misfiled quarter" in body and "post-EOR shift" in body
    # The dated, source-URLed half is News — the writer's own dedupe key.
    assert "[Series A led by Nexus, June 2026](https://tc.com/a)" in body
    # Provenance: the same two stamps a researched PERSON gets.
    assert "updated_by: web_research" in body and "last_researched:" in body
    # The person still carries their own bio fact — the company write is additive, not a move.
    _, p = _person(tmp_path, "Spencer Kim", "spencer@commenda.io")
    assert any("Per web search" in f.text for f in p.facts.values())


def test_company_deep_dive_is_read_back_as_known_on_the_next_dig(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(FOCUS_RESEARCH, [])
    ra = _load("ra_known", "_shared/scripts/research_attendees.py")
    known = ra._known_company({"email": "dana@commenda.io"}, "Commenda")
    assert "Global tax filing tied to each payment" in known
    assert "Series A led by Nexus" in known           # news rides along as already-known
    prompt = ra._build_focus_prompt({"name": "Dana", "email": "dana@commenda.io"},
                                    "ctx", "", known)
    assert "Already on file about the company (do NOT re-derive it)" in prompt
    assert "Global tax filing tied to each payment" in prompt
    # Nothing on file → the dig runs cold, exactly as before.
    assert ra._known_company({"email": "x@nobody-here.com"}, "Nobody Here") == ""
    assert "Already on file about the company" not in ra._build_focus_prompt(
        {"name": "X", "email": "x@nobody-here.com"}, "ctx", "", "")


def test_same_research_run_twice_does_not_double_write(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(FOCUS_RESEARCH, [{"name": "Spencer Kim", "email": "spencer@commenda.io"}])
    first = _company(tmp_path)
    pp.persist(FOCUS_RESEARCH, [{"name": "Spencer Kim", "email": "spencer@commenda.io"}])
    second = _company(tmp_path)
    assert second.count("Series A led by Nexus") == 1     # news dedupes on its URL
    assert second.count("## About") == 1
    assert second.count("misfiled quarter") == 1          # About is REPLACED, never appended
    # Only updated_at moves between the two writes.
    assert [l for l in first.splitlines() if not l.startswith("updated_at")] == \
           [l for l in second.splitlines() if not l.startswith("updated_at")]


def test_two_people_one_company_write_one_file_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    research = {"attendees": FOCUS_RESEARCH["attendees"] + [
        {"email": "dana@commenda.io", "company": "Commenda", "summary": "CFO.",
         "company_summary": "Commenda, a tax startup."}]}
    out = pp.persist(research, [])
    assert out["companies"] == ["Commenda"]               # one update, not two
    assert len(list((tmp_path / "knowledge" / "companies").glob("*.md"))) == 1


def test_a_sweep_one_liner_never_clobbers_a_deep_dive(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(FOCUS_RESEARCH, [])
    pp.persist({"attendees": [{"email": "dana@commenda.io", "company": "Commenda",
                               "company_summary": "Commenda, a tax startup."}]}, [])
    body = _company(tmp_path)
    assert "misfiled quarter" in body and "a tax startup" not in body
    # …but a company with NOTHING on file gets seeded by the one-liner rather than staying blank.
    pp.persist({"attendees": [{"email": "n@northwind.com", "company": "Northwind",
                               "company_summary": "Northwind builds anvils."}]}, [])
    assert "Northwind builds anvils." in _company(tmp_path, "northwind")


def test_user_correction_to_a_company_survives_the_next_research_run(tmp_path, monkeypatch):
    """Correctability, the doctrine's other half: a company About is editable through the SAME
    apply() lane research writes through, and the correction outranks the next search."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pp.persist(FOCUS_RESEARCH, [])
    ke = _load("knowledge_edit", "_shared/knowledge/knowledge_edit.py")
    res = ke.run(["--slug", "commenda", "--op", "company-about",
                  "--text", "Commenda files taxes for cross-border payroll."])
    assert res["ok"] and res["about"] == "Commenda files taxes for cross-border payroll."
    grown = dict(FOCUS_RESEARCH["attendees"][0])
    grown["company_deep"] = dict(grown["company_deep"],
                                 traction=["Series B, Dec 2026 (https://tc.com/b)"])
    pp.persist({"attendees": [grown]}, [])
    body = _company(tmp_path)
    assert "Commenda files taxes for cross-border payroll." in body   # the human's words stand
    assert "misfiled quarter" not in body
    assert "updated_by: user_edit" in body                            # ownership of About sticks
    assert "https://tc.com/b" in body                                 # news still accumulates
    try:
        ke.run(["--slug", "nobody", "--op", "company-about", "--text", "x"])
        raise AssertionError("expected EditError for a company with no file")
    except ke.EditError:
        pass


def test_persistence_failure_never_costs_the_run(tmp_path, monkeypatch):
    """Fail toward silence: memory is not the deliverable. An unwritable graph loses one run's
    learning and says so — it never raises into the prep the research was gathered for."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))

    def boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(pp.ku, "apply", boom)
    out = pp.persist(FOCUS_RESEARCH, [{"name": "Spencer Kim", "email": "spencer@commenda.io"}])
    assert out["persisted"] == 0 and out["people"] == [] and out["companies"] == []
    assert "read-only file system" in out["error"]
    monkeypatch.setattr(pp.ku, "company_knowledge", boom)
    assert pp.persist(FOCUS_RESEARCH, [])["companies"] == []
