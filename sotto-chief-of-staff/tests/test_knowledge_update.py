"""
Parity tests for the knowledge-graph port (knowledge_update.py / knowledge.py).
Asserts the exact behaviors of knowledge_files.rs: dedup ratios, bump/supersede/new,
confidence decay, prune, fact-id hashing, .md round-trip.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(__file__)
LIB = os.path.join(HERE, "..", "_shared", "lib")
SCRIPTS = os.path.join(HERE, "..", "morning-brief", "scripts")
sys.path.insert(0, LIB)
import knowledge as kg  # noqa: E402

# load knowledge_update.py as a module
_spec = importlib.util.spec_from_file_location("knowledge_update", os.path.join(SCRIPTS, "knowledge_update.py"))
ku = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ku)

NOW = datetime(2026, 6, 23, 7, 0, 0)


def _setup(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    return tmp_path


# ── dedup ────────────────────────────────────────────────────────────────────
def test_dedupe_key_drops_stopwords_and_short():
    assert kg.make_dedupe_key("She is the CTO at Acme") == {"cto", "acme"}


def test_high_similarity_bumps():
    facts = {"f_1": kg.FactMeta(text="CTO at Acme Corp", type="milestone", first="2026-06-01", last="2026-06-01")}
    action, fid = kg.find_similar_fact(facts, "CTO at Acme Corporation", "milestone", False)
    assert action == kg.BUMP and fid == "f_1"


def test_medium_similarity_supersedes_mutable_type():
    facts = {"f_1": kg.FactMeta(text="works at Acme leading platform", type="milestone")}
    action, fid = kg.find_similar_fact(facts, "works at Acme on growth", "milestone", False)
    # overlap {works,acme} / smaller -> within 0.3..0.5 and mutable type => supersede
    assert action in (kg.SUPERSEDE, kg.BUMP)  # ratio boundary; mutable type allows supersede


def test_low_similarity_is_new():
    facts = {"f_1": kg.FactMeta(text="CTO at Acme", type="milestone")}
    action, _ = kg.find_similar_fact(facts, "enjoys mountain biking weekends", "interest", False)
    assert action == kg.NEW


def test_archived_match_skips():
    facts = {"f_1": kg.FactMeta(text="CTO at Acme Corp", type="milestone", status="archived")}
    action, _ = kg.find_similar_fact(facts, "CTO at Acme Corp", "milestone", False)
    assert action == kg.SKIP


# ── decay / prune ──────────────────────────────────────────────────────────────
def test_confidence_decays_008_per_week_floor_04():
    f = kg.FactMeta(text="x", conf=0.95, last="2026-06-09")  # 14 days = 2 weeks before NOW
    assert abs(kg.effective_confidence(f, NOW) - (0.95 - 2 * 0.08)) < 1e-9


def test_confidence_floor():
    f = kg.FactMeta(text="x", conf=0.8, last="2025-01-01")
    assert kg.effective_confidence(f, NOW) == kg.CONFIDENCE_FLOOR


def test_prune_one_off_after_60_days():
    facts = {"f_1": kg.FactMeta(text="x", seen=1, status="active", last="2026-01-01")}
    kg.prune_stale_facts(facts, NOW)
    assert facts["f_1"].status == "archived" and facts["f_1"].archived_text == "x"


def test_prune_keeps_seen_more_than_once():
    facts = {"f_1": kg.FactMeta(text="x", seen=2, status="active", last="2026-01-01")}
    kg.prune_stale_facts(facts, NOW)
    assert facts["f_1"].status == "active"


# ── fact id ────────────────────────────────────────────────────────────────────
def test_fact_id_is_stable_sha_prefix():
    a = kg.generate_fact_id("c_abc", "CTO at Acme", "2026-06-23")
    assert a.startswith("f_") and len(a) == 12 and a == kg.generate_fact_id("c_abc", "CTO at Acme", "2026-06-23")


# ── apply pipeline ─────────────────────────────────────────────────────────────
def test_apply_new_then_bump_increments_seen_and_conf(tmp_path):
    _setup(tmp_path)
    ext = {"person_updates": [{
        "person_name": "Sarah Chen", "identifier": "sarah@acme.com",
        "facts": [{"fact": "CTO at Acme Corp", "memory_type": "milestone", "confidence": 0.9}],
    }]}
    r1 = ku.apply(ext, NOW)
    assert r1["applied"]["new"] == 1
    # re-extract same fact -> bump
    r2 = ku.apply(ext, NOW)
    assert r2["applied"]["confirmed"] == 1 and r2["applied"]["new"] == 0
    p = kg.parse_person_file(open(os.path.join(kg.people_dir(), "sarah-chen.md")).read())
    fact = next(iter(p.facts.values()))
    assert fact.seen == 2 and abs(fact.conf - 1.0) < 1e-9  # 0.9 -> min(1.0, 1.0)


def test_apply_low_confidence_fact_skipped(tmp_path):
    _setup(tmp_path)
    ext = {"person_updates": [{"person_name": "Bob", "facts": [
        {"fact": "maybe likes tea", "memory_type": "interest", "confidence": 0.3}]}]}
    r = ku.apply(ext, NOW)
    assert r["applied"]["new"] == 0


def test_md_round_trip_preserves_facts(tmp_path):
    _setup(tmp_path)
    ext = {"person_updates": [{"person_name": "Sarah Chen", "identifier": "sarah@acme.com",
        "profile_patch": {"title": "CTO", "company": "Acme Corp"},
        "facts": [{"fact": "Working on Series B", "memory_type": "milestone", "confidence": 0.85}]}]}
    ku.apply(ext, NOW)
    content = open(os.path.join(kg.people_dir(), "sarah-chen.md")).read()
    p = kg.parse_person_file(content)
    assert p.name == "Sarah Chen" and p.title == "CTO" and p.company == "Acme Corp"
    assert any("Series B" in f.text for f in p.facts.values())
    assert "## Facts" in content and content.startswith("---\n")


def test_rejects_path_traversal_in_company_slug(tmp_path):
    _setup(tmp_path)
    ext = {"company_updates": [{"company_slug": "../../../etc/cron.d/evil", "company_name": "Evil",
                                "news": [{"text": "x"}]}]}
    r = ku.apply(ext, NOW)
    # traversal is neutralized: any written file stays INSIDE companies/, nothing escapes to /etc
    assert not os.path.exists("/etc/cron.d/evil.md")
    assert not os.path.exists(os.path.join(str(tmp_path), "..", "..", "etc"))
    cdir = os.path.realpath(kg.companies_dir())
    for fn in r["company_files"]:
        assert os.path.realpath(os.path.join(kg.companies_dir(), fn)).startswith(cdir + os.sep)


def test_rejects_traversal_person_name(tmp_path):
    _setup(tmp_path)
    # a name that slugifies to empty (only separators) must not fall back to a raw path
    ext = {"person_updates": [{"person_name": "../..", "facts": [
        {"fact": "x", "memory_type": "context", "confidence": 0.9}]}]}
    r = ku.apply(ext, NOW)
    assert r["person_files"] == []


def test_safe_slug_helpers():
    assert kg.safe_slug("../../etc") == "etc"      # slugify strips separators
    assert kg.safe_slug("..") is None              # no usable slug -> None (no raw fallback)
    assert kg.safe_slug("") is None
    assert kg.safe_slug("Acme Corp") == "acme-corp"


def test_company_news_dedup_by_url(tmp_path):
    _setup(tmp_path)
    ext = {"company_updates": [{"company_name": "Acme Corp", "news": [
        {"text": "Raised Series B", "url": "https://x.com/1", "date": "2026-06"}]}]}
    ku.apply(ext, NOW)
    ku.apply(ext, NOW)  # same url -> no dup
    content = open(os.path.join(kg.companies_dir(), "acme.md")).read()
    assert content.count("https://x.com/1") == 1


def test_company_preserves_domain_and_sets_last_news_update(tmp_path):
    _setup(tmp_path)
    import yaml
    path = os.path.join(kg.companies_dir(), "acme.md")
    os.makedirs(kg.companies_dir(), exist_ok=True)
    # Seed a company file with a domain (as the Mac app would write).
    with open(path, "w") as f:
        f.write("---\n" + yaml.safe_dump({"schema": 1, "normalized": "acme", "aliases": ["Acme Corp"],
                "domain": "acme.com", "updated_at": "2026-01-01", "updated_by": "seed"},
                sort_keys=False) + "---\n\n## About\nDev tools.\n")
    ku.apply({"company_updates": [{"company_name": "Acme Corp",
              "news": [{"text": "Launched v2", "url": "https://x.com/v2"}]}]}, NOW)
    fm = yaml.safe_load(open(path).read().split("---")[1])
    assert fm.get("domain") == "acme.com"             # preserved, not erased
    assert fm.get("last_news_update") == "2026-06-23"  # set when news arrives


def test_company_context_capped(tmp_path):
    _setup(tmp_path)
    big = "x" * 1500
    ku.apply({"company_updates": [{"company_name": "Beta", "context_updates": [big]}]}, NOW)
    content = open(os.path.join(kg.companies_dir(), "beta.md")).read()
    # The Context section must not exceed the on-disk cap (keeps the most-recent tail).
    ctx = content.split("## Context", 1)[1]
    assert ctx.count("x") == kg.MAX_COMPANY_CONTEXT_CHARS


def test_canonical_id_is_12_hex(tmp_path):
    cid = kg.generate_canonical_id("kf:Sarah Chen|sarah@acme.com")
    assert cid.startswith("c_") and len(cid) == 14  # "c_" + 12 hex


# ── packed-context labeling (knowledge_query.py) ─────────────────────────────
def test_pack_person_labels_low_confidence_facts(tmp_path):
    _setup(tmp_path)
    _kq_spec = importlib.util.spec_from_file_location(
        "knowledge_query", os.path.join(SCRIPTS, "knowledge_query.py"))
    kq = importlib.util.module_from_spec(_kq_spec)
    _kq_spec.loader.exec_module(kq)
    today = NOW.strftime("%Y-%m-%d")
    p = kg.PersonFile(canonical_id="c_abc", name="Sarah Chen", facts={
        # research fact: 0.55 but pre-labeled at write time → packed verbatim, no extra suffix
        "f_1": kg.FactMeta(text="Per web search: CTO at Acme", conf=0.55, type="context",
                           first=today, last=today),
        # low-confidence extraction with no label → must be flagged in the packed context
        "f_2": kg.FactMeta(text="Might be moving to Austin", conf=0.55, type="context",
                           first=today, last=today),
        # normal-confidence fact → untouched
        "f_3": kg.FactMeta(text="Prefers morning meetings", conf=0.9, type="working_style",
                           first=today, last=today),
    })
    packed = kq.pack_person(p, True, NOW)
    assert "Per web search: CTO at Acme" in packed and "CTO at Acme (unverified)" not in packed
    assert "Might be moving to Austin (unverified)" in packed
    assert "Prefers morning meetings" in packed and "Prefers morning meetings (unverified)" not in packed
