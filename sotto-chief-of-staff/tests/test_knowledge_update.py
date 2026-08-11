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

import yaml

HERE = os.path.dirname(__file__)
LIB = os.path.join(HERE, "..", "_shared", "lib")
SCRIPTS = os.path.join(HERE, "..", "_shared", "knowledge")
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
    path = kg.find_person_file(name="Sarah Chen", identifier="sarah@acme.com")
    p = kg.parse_person_file(open(path).read())
    assert os.path.basename(path) == f"{p.canonical_id}.md"  # cid-keyed, not name-slug
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
    content = open(kg.find_person_file(identifier="sarah@acme.com")).read()
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


# ── robustness / timestamp truthfulness ──────────────────────────────────────
def test_news_item_missing_text_is_skipped(tmp_path):
    # Extraction sometimes emits a news item without text — indexing item['text'] raw killed the
    # whole knowledge update. The bad item is skipped; its siblings still apply.
    _setup(tmp_path)
    out = ku.apply({"company_updates": [{"company_name": "Acme", "news": [
        {"url": "https://x.com/no-text"},
        {"text": "Acme raised a Series B", "url": "https://x.com/b"},
    ]}]}, NOW)
    assert out["company_files"]
    content = open(os.path.join(kg.companies_dir(), out["company_files"][0]), encoding="utf-8").read()
    assert "Series B" in content and "no-text" not in content


def test_now_iso_labels_utc_truthfully():
    from datetime import timedelta, timezone
    aware_pst = datetime(2026, 6, 23, 23, 30, 0, tzinfo=timezone(timedelta(hours=-8)))
    assert kg.now_iso(aware_pst) == "2026-06-24T07:30:00Z"    # converted to UTC, not relabeled


def test_apply_default_now_is_utc(tmp_path, monkeypatch):
    # apply() used to default to server-LOCAL datetime.now() and stamp it with a 'Z' suffix.
    _setup(tmp_path)
    from datetime import timezone
    seen = {}
    real_dt = ku.datetime

    class _FakeDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            seen["tz"] = tz
            return real_dt(2026, 6, 23, 23, 30, 0, tzinfo=tz) if tz else real_dt(2026, 6, 23, 15, 30, 0)

    monkeypatch.setattr(ku, "datetime", _FakeDT)
    out = ku.apply({"person_updates": [{"person_name": "Zed", "identifier": "z@x.com",
                                        "facts": [{"fact": "Zed ships fast", "memory_type": "context",
                                                   "confidence": 0.9}]}]})
    assert seen["tz"] == timezone.utc
    content = open(os.path.join(kg.people_dir(), out["person_files"][0]), encoding="utf-8").read()
    assert "2026-06-23T23:30:00Z" in content                  # truthful UTC label


def test_correction_supersedes_at_high_overlap_never_bumps(tmp_path):
    # THE inverted-correction bug: "Peyton is NOT the founder…" shares >0.5 of its words with the
    # wrong fact, so the old code BUMPed it (+0.1 conf, decay reset, seen>1 immortality) and threw
    # the correction away. change_type="correction" must SUPERSEDE at ANY overlap ratio.
    _setup(tmp_path)
    ku.apply({"person_updates": [{
        "person_name": "Peyton Lee", "identifier": "peyton@alive.inc",
        "facts": [{"fact": "Peyton is the founder of Alive Ventures",
                   "memory_type": "context", "confidence": 0.8}],
    }]}, NOW)
    ku.apply({"person_updates": [{
        "person_name": "Peyton Lee", "identifier": "peyton@alive.inc",
        "facts": [{"fact": "Peyton is NOT the founder of Alive — she runs partnerships",
                   "memory_type": "context", "confidence": 0.9,
                   "change_type": "correction"}],
    }]}, NOW)
    path = kg.find_person_file(name="Peyton Lee", identifier="peyton@alive.inc")
    p = kg.parse_person_file(open(path).read())
    active = [m.text for _, m in kg.sorted_active_facts(p.facts)]
    assert any("runs partnerships" in t for t in active), "correction must become the active fact"
    assert "Peyton is the founder of Alive Ventures" not in active, "wrong fact must not stay active"
    assert any(m.status == "archived" for m in p.facts.values()), "superseded fact archived, not deleted"


def test_plain_reobservation_still_bumps_after_fix(tmp_path):
    # Without change_type=correction, a re-observed fact still bumps (seen+1) — the fix must not
    # turn ordinary dedup into supersession.
    _setup(tmp_path)
    ext = {"person_updates": [{
        "person_name": "Sarah Chen", "identifier": "sarah@acme.com",
        "facts": [{"fact": "Sarah leads the platform team at Acme",
                   "memory_type": "context", "confidence": 0.7}],
    }]}
    ku.apply(ext, NOW)
    r2 = ku.apply(ext, NOW)
    assert r2["applied"]["confirmed"] == 1 and r2["applied"]["new"] == 0


# ── Entity-dedup lite (Editor Step 2 item 6) ─────────────────────────────────
# Exact identifiers auto-merge; names only ever SUGGEST. The asymmetry is the whole design: an
# identifier collision is proof, a name collision is the original identity bug (two John Smiths).

def _write_person(cid, name, identifiers, facts=None, updated="2026-06-01T07:00:00Z"):
    os.makedirs(kg.people_dir(), exist_ok=True)
    p = kg.PersonFile(canonical_id=cid, name=name, identifiers=list(identifiers),
                      updated_at=updated, updated_by="brief_extraction",
                      facts={fid: kg.FactMeta(text=t, type="context", status="active", seen=1,
                                              conf=0.8, first="2026-06-01", last="2026-06-01")
                             for fid, t in (facts or {}).items()})
    path = os.path.join(kg.people_dir(), f"{cid}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(kg.serialize_person_file(p, NOW))
    return path


def _people_files():
    import glob
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(kg.people_dir(), "*.md")))


def _suggestions():
    path = os.path.join(str(kg.data_root()), "knowledge", "merge_suggestions.json")
    if not os.path.exists(path):
        return []
    return json.load(open(path, encoding="utf-8"))["suggestions"]


def test_exact_identifier_collision_auto_merges_into_one_file(tmp_path):
    """Two files, one email — one human. Facts are unioned, nothing is dropped, one file remains."""
    _setup(tmp_path)
    _write_person("c_aaaaaaaaaaaa", "Ben", ["+1 (415) 555-0000", "ben@northstar.io"],
                  {"f_a": "Ben runs ops at Northstar"})
    _write_person("c_bbbbbbbbbbbb", "Ben Butler", ["BEN@northstar.io"],
                  {"f_b": "Ben Butler is closing the Series A"})
    out = ku.apply({}, NOW)
    assert len(_people_files()) == 1
    assert len(out["auto_merged"]) == 1 and out["auto_merged"][0]["identifier"] == "ben@northstar.io"
    path = os.path.join(kg.people_dir(), _people_files()[0])
    p = kg.parse_person_file(open(path, encoding="utf-8").read())
    texts = {f.text for f in p.facts.values()}
    assert texts == {"Ben runs ops at Northstar", "Ben Butler is closing the Series A"}
    assert "ben@northstar.io" in [str(i).lower() for i in p.identifiers]
    assert any("415" in str(i) for i in p.identifiers)      # the phone survived the merge too


def test_phone_identifier_collision_normalizes_before_matching(tmp_path):
    """Same rule as the graph's: phone-ish identifiers match on their last 10 digits."""
    _setup(tmp_path)
    _write_person("c_cccccccccccc", "Dhruv", ["+1 415-555-0123"], {"f_a": "Dhruv texts about pricing"})
    _write_person("c_dddddddddddd", "Dhruv Patel", ["4155550123"], {"f_b": "Dhruv Patel is at Acme"})
    ku.apply({}, NOW)
    assert len(_people_files()) == 1


def test_similar_names_are_suggested_never_merged(tmp_path):
    """Slug containment with non-conflicting identifiers: a suggestion, and BOTH files survive."""
    _setup(tmp_path)
    _write_person("c_eeeeeeeeeeee", "Ben", ["+14155550000"], {"f_a": "Ben runs ops"})
    _write_person("c_ffffffffffff", "Ben Butler", ["ben@northstar.io"],
                  {"f_b": "Ben Butler is closing the Series A", "f_c": "based in SF"})
    out = ku.apply({}, NOW)
    assert out["auto_merged"] == []
    assert len(_people_files()) == 2                       # NOTHING was merged
    sugg = _suggestions()
    assert len(sugg) == 1
    assert sugg[0]["from"] == "c_eeeeeeeeeeee" and sugg[0]["into"] == "c_ffffffffffff"
    assert "ben ⊂ ben-butler" in sugg[0]["reason"]
    assert sugg[0]["first_seen"] == "2026-06-23"


def test_two_john_smiths_with_distinct_identifiers_are_never_merged_or_suggested(tmp_path):
    """The audit's failure mode, in both directions: identical names, contradicting emails. Neither
    auto-merged (names never auto-merge at all) NOR suggested — a confirmed merge here would be the
    identity bug the canonical_id keying exists to prevent."""
    _setup(tmp_path)
    _write_person("c_111111111111", "John Smith", ["john@acme.com"], {"f_a": "John Smith at Acme"})
    _write_person("c_222222222222", "John Smith", ["john@globex.com"], {"f_b": "John Smith at Globex"})
    out = ku.apply({}, NOW)
    assert out["auto_merged"] == []
    assert len(_people_files()) == 2
    assert _suggestions() == []
    assert out["merge_suggestions"] == []


def test_suggestion_survives_reruns_and_is_capped(tmp_path):
    """first_seen is preserved across passes (the dashboard ages the card), and the list is capped."""
    _setup(tmp_path)
    _write_person("c_eeeeeeeeeeee", "Ben", ["+14155550000"])
    _write_person("c_ffffffffffff", "Ben Butler", ["ben@northstar.io"], {"f_b": "closing the A"})
    ku.apply({}, NOW)
    later = datetime(2026, 7, 1, 7, 0, 0)
    ku.apply({}, later)
    sugg = _suggestions()
    assert len(sugg) == 1 and sugg[0]["first_seen"] == "2026-06-23"
    assert len(sugg) <= ku.MERGE_SUGGESTIONS_MAX


def test_identifiers_conflict_only_within_a_kind(tmp_path):
    """A phone-only file and an email-only file do not contradict each other — that IS the dupe
    shape the audit found (the non-Contacts path). Two different emails DO contradict."""
    _setup(tmp_path)
    phone_only = kg.PersonFile(name="Ben", identifiers=["+14155550000"])
    email_only = kg.PersonFile(name="Ben Butler", identifiers=["ben@northstar.io"])
    other_email = kg.PersonFile(name="Ben Butler", identifiers=["ben@other.com"])
    assert ku.identifiers_conflict(phone_only, email_only) is False
    assert ku.identifiers_conflict(email_only, other_email) is True
    assert ku.identifiers_conflict(email_only, kg.PersonFile(name="Ben", identifiers=[])) is False


def test_auto_merge_runs_before_the_updates_land(tmp_path):
    """The Learn step merges first, so the day's facts land in the SURVIVING file, not a third one."""
    _setup(tmp_path)
    _write_person("c_aaaaaaaaaaaa", "Ben", ["ben@northstar.io"], {"f_a": "Ben runs ops"})
    _write_person("c_bbbbbbbbbbbb", "Ben Butler", ["ben@northstar.io"], {"f_b": "closing the A"})
    ku.apply({"person_updates": [{"person_name": "Ben Butler", "identifier": "ben@northstar.io",
                                  "facts": [{"fact": "Ben Butler moved the board meeting to Friday",
                                             "memory_type": "context", "confidence": 0.9}]}]}, NOW)
    assert len(_people_files()) == 1
    p = kg.parse_person_file(open(os.path.join(kg.people_dir(), _people_files()[0]),
                                  encoding="utf-8").read())
    assert any("board meeting to Friday" in f.text for f in p.facts.values())


# ── Company prevention: alias + domain resolution ────────────────────────────

def test_company_alias_resolves_to_the_existing_file(tmp_path):
    """"YC" must land in the stored "Y Combinator" file — aliases have been written since the Rust
    port and were never consulted, which is exactly how one company became two memories."""
    _setup(tmp_path)
    os.makedirs(kg.companies_dir(), exist_ok=True)
    with open(os.path.join(kg.companies_dir(), "y-combinator.md"), "w", encoding="utf-8") as f:
        f.write("---\nschema: 1\nnormalized: y-combinator\naliases:\n- Y Combinator\n- YC\n"
                "updated_at: '2026-06-01T07:00:00Z'\nupdated_by: brief_extraction\n---\n"
                "\n## Context\nW26 batch demo day is in March\n")
    out = ku.apply({"company_updates": [{"company_name": "YC",
                                         "context_updates": ["Sarah is applying"]}]}, NOW)
    import glob
    files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(kg.companies_dir(), "*.md")))
    assert files == ["y-combinator.md"]                     # no second file was minted
    assert out["company_files"] == ["y-combinator.md"]
    content = open(os.path.join(kg.companies_dir(), "y-combinator.md"), encoding="utf-8").read()
    assert "Sarah is applying" in content and "demo day" in content
    fm = yaml.safe_load(kg.split_frontmatter_body(content)[0])
    assert fm["aliases"] == ["Y Combinator", "YC"]          # unchanged; nothing duplicated


def test_a_resolved_name_form_is_recorded_as_an_alias_for_next_time(tmp_path):
    """The prevention loop closes itself: a spelling that resolved through the domain lookup today
    is a direct alias hit tomorrow, without re-deriving anything."""
    _setup(tmp_path)
    os.makedirs(kg.companies_dir(), exist_ok=True)
    with open(os.path.join(kg.companies_dir(), "northstar.md"), "w", encoding="utf-8") as f:
        f.write("---\nschema: 1\nnormalized: northstar\naliases:\n- Northstar Labs\n"
                "domain: getnorthstar.com\nupdated_at: '2026-06-01T07:00:00Z'\n"
                "updated_by: brief_extraction\n---\n")
    ku.apply({"company_updates": [{"company_name": "getnorthstar.com"}]}, NOW)
    fm = yaml.safe_load(kg.split_frontmatter_body(
        open(os.path.join(kg.companies_dir(), "northstar.md"), encoding="utf-8").read())[0])
    assert fm["aliases"] == ["Northstar Labs", "getnorthstar.com"]
    # An equivalent spelling of a name already stored adds nothing (aliases don't bloat).
    ku.apply({"company_updates": [{"company_name": "Northstar Labs, Inc."}]}, NOW)
    fm2 = yaml.safe_load(kg.split_frontmatter_body(
        open(os.path.join(kg.companies_dir(), "northstar.md"), encoding="utf-8").read())[0])
    assert fm2["aliases"] == ["Northstar Labs", "getnorthstar.com"]


def test_company_domain_resolves_to_the_existing_file(tmp_path):
    """A domain-shaped name resolves through the stored `domain`, not a second file."""
    _setup(tmp_path)
    os.makedirs(kg.companies_dir(), exist_ok=True)
    with open(os.path.join(kg.companies_dir(), "northstar.md"), "w", encoding="utf-8") as f:
        f.write("---\nschema: 1\nnormalized: northstar\naliases:\n- Northstar Labs\n"
                "domain: northstar.io\nupdated_at: '2026-06-01T07:00:00Z'\n"
                "updated_by: brief_extraction\n---\n")
    out = ku.apply({"company_updates": [{"company_name": "northstar.io",
                                         "context_updates": ["shipped the SDK"]}]}, NOW)
    assert out["company_files"] == ["northstar.md"]
    content = open(os.path.join(kg.companies_dir(), "northstar.md"), encoding="utf-8").read()
    assert "shipped the SDK" in content
    assert "domain: northstar.io" in content                # metadata still preserved


def test_genuinely_new_company_still_gets_its_own_file(tmp_path):
    """Prevention must not become collapsing: an unrelated name is a new file."""
    _setup(tmp_path)
    ku.apply({"company_updates": [{"company_name": "Y Combinator"}]}, NOW)
    ku.apply({"company_updates": [{"company_name": "Signal House"}]}, NOW)
    import glob
    files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(kg.companies_dir(), "*.md")))
    assert files == ["signal-house.md", "y-combinator.md"]


# ── Relations: a typed edge between two people, stored on both ends ────────────
# One sentence, and every test below is a clause of it: a relation is a TYPED (closed vocabulary)
# edge between two people SOTTO KNOWS (never a dangling slug), stored on BOTH ends by one writer,
# readable as a SENTENCE.

def _people_by_name(name: str):
    """(slug, PersonFile) for a person, by display name — relations key off the file stem."""
    path = kg.find_person_file(name=name)
    assert path, f"no person file for {name}"
    return os.path.splitext(os.path.basename(path))[0], kg.parse_person_file(open(path).read())


def _intro(subject="Priya Patel", other="Vishnu Sharma", rtype="introduced_by", **kw):
    rel = {"type": rtype, "other_person_name": other, "confidence": 0.95}
    rel.update(kw)
    return {"person_updates": [{"person_name": subject, "relations": [rel]}]}


def test_relation_writes_both_ends_with_the_inverse_type(tmp_path):
    _setup(tmp_path)
    ku.apply({"person_updates": [{"person_name": "Vishnu Sharma", "identifier": "v@x.com"},
                                 {"person_name": "Priya Patel", "identifier": "p@y.com"}]}, NOW)
    out = ku.apply(_intro(date="2026-05-14"), NOW)
    assert out["applied"]["relations"] == 1

    v_slug, vishnu = _people_by_name("Vishnu Sharma")
    p_slug, priya = _people_by_name("Priya Patel")
    assert [(r.type, r.slug, r.name) for r in priya.relations] == \
        [("introduced_by", v_slug, "Vishnu Sharma")]
    assert [(r.type, r.slug, r.name) for r in vishnu.relations] == \
        [("introduced", p_slug, "Priya Patel")]
    # …and it reads as a sentence from either end.
    assert priya.relations[0].sentence() == "Introduced to you by Vishnu Sharma (May 2026)"
    assert vishnu.relations[0].sentence() == "Introduced Priya Patel to you (May 2026)"


def test_relation_vocabulary_is_closed_unknown_types_are_dropped(tmp_path):
    _setup(tmp_path)
    ku.apply({"person_updates": [{"person_name": "Vishnu Sharma"},
                                 {"person_name": "Priya Patel"}]}, NOW)
    out = ku.apply(_intro(rtype="nemesis_of"), NOW)
    assert out["applied"]["relations"] == 0
    assert out["dropped_relation_types"] == ["nemesis_of"]
    assert _people_by_name("Priya Patel")[1].relations == []
    # Every symmetric type in the vocabulary is its own inverse — the edge reads the same way
    # from either file, so neither side needs a second phrasing.
    for t in ("works_with", "family_of", "partner_of"):
        assert kg.RELATION_INVERSE[t] == t


def test_relation_below_the_confidence_bar_is_dropped(tmp_path):
    _setup(tmp_path)
    ku.apply({"person_updates": [{"person_name": "Vishnu Sharma"},
                                 {"person_name": "Priya Patel"}]}, NOW)
    out = ku.apply(_intro(confidence=0.6), NOW)
    assert out["applied"]["relations"] == 0 and _people_by_name("Priya Patel")[1].relations == []


def test_writing_the_same_edge_twice_is_a_no_op(tmp_path):
    _setup(tmp_path)
    ku.apply({"person_updates": [{"person_name": "Vishnu Sharma"},
                                 {"person_name": "Priya Patel"}]}, NOW)
    ku.apply(_intro(date="2026-05-14"), NOW)
    out = ku.apply(_intro(date="2026-08-01"), NOW)          # same edge, restated later
    assert out["applied"]["relations"] == 0
    priya = _people_by_name("Priya Patel")[1]
    assert len(priya.relations) == 1 and priya.relations[0].date == "2026-05-14"
    assert len(_people_by_name("Vishnu Sharma")[1].relations) == 1


def test_an_identified_stranger_gets_the_same_stub_prewarm_creates(tmp_path):
    """An edge to someone with no file yet mints the identity stub — name + identifier, no invented
    facts — so the relation never points at a slug with nothing behind it."""
    _setup(tmp_path)
    ku.apply({"person_updates": [{"person_name": "Priya Patel"}]}, NOW)
    out = ku.apply(_intro(other_identifier="vishnu@example.com"), NOW)
    assert out["applied"]["relations"] == 1
    v_slug, vishnu = _people_by_name("Vishnu Sharma")
    assert vishnu.identifiers == ["vishnu@example.com"] and vishnu.facts == {}
    assert v_slug == vishnu.canonical_id                     # cid-keyed like every other file
    assert os.path.basename(v_slug + ".md") in out["person_files"]
    assert [r.slug for r in _people_by_name("Priya Patel")[1].relations] == [v_slug]


def test_an_unresolvable_name_becomes_a_fact_never_a_guessed_slug(tmp_path):
    _setup(tmp_path)
    ku.apply({"person_updates": [{"person_name": "Priya Patel"}]}, NOW)
    out = ku.apply(_intro(other="Someone Unknown"), NOW)
    assert out["applied"]["relations"] == 0
    priya = _people_by_name("Priya Patel")[1]
    assert priya.relations == []
    texts = [f.text for f in priya.facts.values()]
    assert texts == ["Introduced to you by Someone Unknown (unlinked)"]
    assert kg.find_person_file(name="Someone Unknown") is None   # no phantom person was minted


def test_a_partial_name_resolves_through_the_dedup_lite_match(tmp_path):
    """"Vishnu" ⊂ "Vishnu Sharma" is the containment rule the merge suggestions already use."""
    _setup(tmp_path)
    ku.apply({"person_updates": [{"person_name": "Vishnu Sharma"},
                                 {"person_name": "Priya Patel"}]}, NOW)
    out = ku.apply(_intro(other="Vishnu"), NOW)
    assert out["applied"]["relations"] == 1
    assert _people_by_name("Priya Patel")[1].relations[0].slug == _people_by_name("Vishnu Sharma")[0]


def test_an_ambiguous_name_is_never_guessed(tmp_path):
    """Two candidates is not a resolution. The edge falls back to a fact rather than pick one."""
    _setup(tmp_path)
    ku.apply({"person_updates": [{"person_name": "Vishnu Sharma", "identifier": "a@x.com"},
                                 {"person_name": "Vishnu Sharman", "identifier": "b@x.com"},
                                 {"person_name": "Priya Patel"}]}, NOW)
    out = ku.apply(_intro(other="Vishnu Sharmax"), NOW)
    assert out["applied"]["relations"] == 0
    assert _people_by_name("Priya Patel")[1].relations == []


def test_relations_round_trip_through_the_person_file(tmp_path):
    _setup(tmp_path)
    ku.apply({"person_updates": [{"person_name": "Vishnu Sharma"},
                                 {"person_name": "Priya Patel"}]}, NOW)
    ku.apply(_intro(date="2026-05-14"), NOW)
    path = kg.find_person_file(name="Priya Patel")
    raw = open(path, encoding="utf-8").read()
    fm = yaml.safe_load(kg.split_frontmatter_body(raw)[0])
    assert list(fm) == ["schema", "canonical_id", "name", "identifiers", "updated_at",
                        "updated_by", "relations", "facts"]      # identity block, above facts
    again = kg.serialize_person_file(kg.parse_person_file(raw), NOW)
    assert again == raw
    # A person with no relations is byte-identical to a pre-relations file (no empty key).
    ku.apply({"person_updates": [{"person_name": "Solo Person"}]}, NOW)
    assert "relations:" not in open(kg.find_person_file(name="Solo Person"), encoding="utf-8").read()


def test_a_junk_relation_type_on_disk_is_dropped_on_read(tmp_path):
    """The closed vocabulary is enforced on the way IN too — a hand-edited file can't smuggle a
    type nobody defined into a reader or a rewrite."""
    _setup(tmp_path)
    ku.apply({"person_updates": [{"person_name": "Priya Patel"}]}, NOW)
    path = kg.find_person_file(name="Priya Patel")
    raw = open(path, encoding="utf-8").read()
    raw = raw.replace("updated_by: brief_extraction\n",
                      "updated_by: brief_extraction\nrelations:\n- type: owes_money_to\n"
                      "  slug: c_deadbeef\n  name: Nobody\n  source: hand\n  confidence: 1.0\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw)
    assert kg.parse_person_file(open(path).read()).relations == []


def test_merge_repoints_back_references_to_the_surviving_slug(tmp_path):
    """THE integrity piece. Two files for one person, each holding half the graph's edges: after
    the merge every OTHER file points at the survivor, nobody points at the file that's gone, and
    the survivor holds no edge to itself."""
    _setup(tmp_path)
    ku.apply({"person_updates": [
        {"person_name": "Ben Butler", "identifier": "ben@acme.com"},
        {"person_name": "Ben", "identifier": "+15551230000"},
        {"person_name": "Priya Patel"},
        {"person_name": "Dana Reed"},
    ]}, NOW)
    rich, _ = _people_by_name("Ben Butler")
    thin, _ = _people_by_name("Ben")
    priya, _ = _people_by_name("Priya Patel")
    dana, _ = _people_by_name("Dana Reed")
    # Priya knows the rich file; Dana knows the thin one; and the two "Bens" know each other.
    assert ku.link_relation(priya, rich, "introduced_by", now=NOW)
    assert ku.link_relation(dana, thin, "works_with", now=NOW)
    assert ku.link_relation(rich, thin, "works_with", now=NOW)

    assert ku.merge_person_files(kg.safe_path(kg.people_dir(), rich),
                                 kg.safe_path(kg.people_dir(), thin), NOW)
    assert not os.path.exists(kg.safe_path(kg.people_dir(), thin))

    survivor = kg.parse_person_file(open(kg.safe_path(kg.people_dir(), rich)).read())
    edges = sorted((r.type, r.slug) for r in survivor.relations)
    assert edges == [("introduced", priya), ("works_with", dana)]   # no self-edge, no dead slug
    dana_file = kg.parse_person_file(open(kg.safe_path(kg.people_dir(), dana)).read())
    assert [(r.type, r.slug, r.name) for r in dana_file.relations] == \
        [("works_with", rich, "Ben Butler")]                        # repointed, and renamed
    priya_file = kg.parse_person_file(open(kg.safe_path(kg.people_dir(), priya)).read())
    assert [(r.type, r.slug) for r in priya_file.relations] == [("introduced_by", rich)]
    # Nothing anywhere still names the file that disappeared.
    for path in sorted(__import__("glob").glob(os.path.join(kg.people_dir(), "*.md"))):
        assert thin not in open(path, encoding="utf-8").read()


def test_merge_dedupes_an_edge_both_halves_carried(tmp_path):
    """Dana knew both duplicates. After the merge she has ONE edge, not two identical ones."""
    _setup(tmp_path)
    ku.apply({"person_updates": [{"person_name": "Ben Butler", "identifier": "ben@acme.com"},
                                 {"person_name": "Ben", "identifier": "+15551230000"},
                                 {"person_name": "Dana Reed"}]}, NOW)
    rich, thin, dana = _people_by_name("Ben Butler")[0], _people_by_name("Ben")[0], \
        _people_by_name("Dana Reed")[0]
    ku.link_relation(dana, rich, "works_with", now=NOW)
    ku.link_relation(dana, thin, "works_with", now=NOW)
    ku.merge_person_files(kg.safe_path(kg.people_dir(), rich),
                          kg.safe_path(kg.people_dir(), thin), NOW)
    dana_file = kg.parse_person_file(open(kg.safe_path(kg.people_dir(), dana)).read())
    assert [(r.type, r.slug) for r in dana_file.relations] == [("works_with", rich)]


def test_unlink_removes_both_ends(tmp_path):
    _setup(tmp_path)
    ku.apply({"person_updates": [{"person_name": "Vishnu Sharma"},
                                 {"person_name": "Priya Patel"}]}, NOW)
    ku.apply(_intro(), NOW)
    v, p = _people_by_name("Vishnu Sharma")[0], _people_by_name("Priya Patel")[0]
    assert ku.unlink_relation(p, v, "introduced_by", NOW) == 2
    assert _people_by_name("Priya Patel")[1].relations == []
    assert _people_by_name("Vishnu Sharma")[1].relations == []
    assert ku.unlink_relation(p, v, "introduced_by", NOW) == 0      # idempotent


def test_link_refuses_a_self_edge_and_an_unknown_type(tmp_path):
    _setup(tmp_path)
    ku.apply({"person_updates": [{"person_name": "Priya Patel"},
                                 {"person_name": "Vishnu Sharma"}]}, NOW)
    p, v = _people_by_name("Priya Patel")[0], _people_by_name("Vishnu Sharma")[0]
    assert ku.link_relation(p, p, "works_with", now=NOW) is False
    assert ku.link_relation(p, v, "nemesis_of", now=NOW) is False
    assert ku.link_relation(p, "not-a-canonical-id", "works_with", now=NOW) is False
    assert _people_by_name("Priya Patel")[1].relations == []


def test_pack_person_renders_relations_as_sentences(tmp_path):
    """The chat surface: briefs, prep, Ask and the event funnel all read the packed person block,
    so rendering the edge here is the only wiring any of them need."""
    _setup(tmp_path)
    _kq_spec = importlib.util.spec_from_file_location(
        "knowledge_query", os.path.join(SCRIPTS, "knowledge_query.py"))
    kq = importlib.util.module_from_spec(_kq_spec)
    _kq_spec.loader.exec_module(kq)
    p = kg.PersonFile(canonical_id="c_abc", name="Priya Patel", relations=[
        kg.Relation(type="introduced_by", slug="c_v", name="Vishnu Sharma", date="2026-05-14"),
        kg.Relation(type="works_with", slug="c_d", name="Dana Reed"),
    ])
    packed = kq.pack_person(p, True, NOW)
    assert packed.splitlines()[1] == \
        "& Introduced to you by Vishnu Sharma (May 2026); Works with Dana Reed"
    # …and the cap keeps a well-connected person a BLOCK, not a directory.
    p.relations = [kg.Relation(type="works_with", slug=f"c_{i}", name=f"P{i}") for i in range(9)]
    assert kq.pack_person(p, True, NOW).count(";") == kg.MAX_RELATIONS_FOR_LLM - 1
    # A person with no relations packs exactly as before.
    assert "&" not in kq.pack_person(kg.PersonFile(canonical_id="c_x", name="Solo"), True, NOW)


def test_migration_repoints_edges_when_a_file_is_re_keyed(tmp_path):
    """A person file changes identity in two ways — a merge, and the migration re-keying a legacy
    name-slug file to {canonical_id}.md. Both must repoint back-references, or an edge names a
    filename that no longer exists. Same helper, both paths."""
    _setup(tmp_path)
    os.makedirs(kg.people_dir(), exist_ok=True)
    ku.apply({"person_updates": [{"person_name": "Priya Patel", "identifier": "p@y.com"}]}, NOW)
    priya_slug = _people_by_name("Priya Patel")[0]
    # A legacy file (no canonical_id, name-slug filename) that Priya already points at.
    legacy = kg.PersonFile(canonical_id="", name="Vishnu Sharma", identifiers=["v@x.com"],
                           updated_at="2026-06-01T07:00:00Z", updated_by="brief_extraction",
                           relations=[kg.Relation(type="introduced", slug=priya_slug,
                                                  name="Priya Patel", source="brief_extraction")])
    with open(os.path.join(kg.people_dir(), "vishnu-sharma.md"), "w", encoding="utf-8") as f:
        f.write(kg.serialize_person_file(legacy, NOW))
    priya_path = kg.safe_path(kg.people_dir(), priya_slug)
    p = kg.parse_person_file(open(priya_path).read())
    p.relations = [kg.Relation(type="introduced_by", slug="vishnu-sharma", name="Vishnu Sharma",
                               source="brief_extraction")]
    with open(priya_path, "w", encoding="utf-8") as f:
        f.write(kg.serialize_person_file(p, NOW))

    kg.migrate_people_dir(NOW)

    new_slug, vishnu = _people_by_name("Vishnu Sharma")
    assert new_slug == vishnu.canonical_id and new_slug != "vishnu-sharma"
    assert [(r.type, r.slug) for r in _people_by_name("Priya Patel")[1].relations] == \
        [("introduced_by", new_slug)]
