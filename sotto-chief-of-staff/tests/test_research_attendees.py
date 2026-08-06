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
    seen_batches, deep_batches = [], []
    monkeypatch.setattr(ra, "_research_batch",
                        lambda batch, ctx, key, comms=None, known=None: (seen_batches.append(len(batch)) or
                                                 [{"email": a["email"], "company": "C", "summary": "s"} for a in batch]))
    monkeypatch.setattr(ra, "_deep_batch",
                        lambda batch, ctx, key, known: (deep_batches.append(len(batch)) or []))
    people = [{"name": f"P{i}", "email": f"p{i}@x.com"} for i in range(12)] + [{"name": "P0", "email": "P0@x.com"}]
    out = ra.research(people, "ctx")
    assert len(out["attendees"]) == 12               # dupe (case-insensitive) dropped
    assert sorted(seen_batches) == [2, 5, 5]          # 12 → batches of 5,5,2
    assert sorted(deep_batches) == [3, 3, 3, 3]       # Pass B: 12 → batches of 3


def test_context_summary_sorted():
    s = ra._context_summary([{"summary": "B", "start": "2026-06-29T15:00"},
                             {"summary": "A", "start": "2026-06-29T09:00"}])
    assert s.index('"A"') < s.index('"B"')           # soonest first


# ── Pass A upgrades: company fallback + comms context + grounding discipline ──────────────────────

def test_profile_prompt_domain_hint_and_company_fallback():
    p = ra._build_prompt([{"name": "Nelson Rojas", "email": "nelson@cobalt-research.com"},
                          {"name": "sam", "email": "sam@rasyn.ai"},
                          {"name": "Jo Free", "email": "jo@yahoo.com"}], "ctx")
    assert "(corporate domain cobalt-research.com — research the company too)" in p
    assert "(use domain rasyn.ai to identify company)" in p          # no last name → identify company
    # freemail: no company hint (yahoo, because the public-repo secrets guard flags g-mail addresses)
    assert "jo@yahoo.com>" in p and "yahoo.com to identify" not in p
    assert "company_summary" in p and "Degrade person → company → nothing" in p
    assert "ALWAYS attempt this for a corporate" in p


def test_no_unsourced_numbers_rule_verbatim_in_both_prompts():
    # The ported original's citation-backed-numbers rule, restored verbatim into BOTH live prompts.
    a = ra._build_prompt([{"name": "A B", "email": "a@x.com"}], "")
    b = ra._build_deep_prompt([{"name": "A B", "email": "a@x.com"}], "", {}, 90)
    for prompt in (a, b):
        assert ra.NO_UNSOURCED_NUMBERS in prompt
    assert "a $115M exit" in ra.NO_UNSOURCED_NUMBERS and "permanent knowledge graph" in ra.NO_UNSOURCED_NUMBERS


def test_comms_context_threads_into_profile_prompt():
    comms = {"emails": [
        {"headers": {"from": "Dana <dana@acme.com>", "subject": "Pricing thread", "date": "Tue"},
         "snippet": "circling back on the pilot"},
        {"from": "other@y.com", "to": "someone@z.com", "subject": "noise"},
    ]}
    by = ra._comms_by_email(comms, {"dana@acme.com"})
    assert by["dana@acme.com"] == ['email Tue "Pricing thread": circling back on the pilot']
    p = ra._build_prompt([{"name": "Dana Roe", "email": "dana@acme.com"}], "", comms_by_email=by)
    assert 'Recent comms with the user: email Tue "Pricing thread"' in p


def test_comms_by_email_caps_at_three():
    emails = [{"from": "dana@acme.com", "subject": f"s{i}"} for i in range(5)]
    by = ra._comms_by_email(emails, {"dana@acme.com"})
    assert len(by["dana@acme.com"]) == 3


# ── Pass B: recency sweep, novelty, post-filters, knobs ───────────────────────────────────────────

def test_deep_prompt_novelty_includes_known_facts_and_recency_window():
    p = ra._build_deep_prompt([{"name": "Dana Roe", "email": "dana@acme.com"}], "ctx",
                              {"dana@acme.com": "CEO at Acme; raised a Series A"}, 45)
    assert "Already known (do NOT repeat any of this): CEO at Acme; raised a Series A" in p
    assert "last ~45 days" in p
    assert '"[Name] blog"' in p and '"[Name] podcast OR talk"' in p    # multiple distinct searches
    assert "an empty list beats a stretch" in p
    assert "NEVER write generic process points" in p                    # anti-fabrication rule


def test_recency_days_env_knob(monkeypatch):
    monkeypatch.setenv("SOTTO_RESEARCH_RECENCY_DAYS", "30")
    assert ra._recency_days() == 30
    monkeypatch.setenv("SOTTO_RESEARCH_RECENCY_DAYS", "junk")
    assert ra._recency_days() == 90                                    # default on garbage


def test_postfilter_drops_urlless_activity_and_unsourced_personal():
    out = ra._postfilter_deep({
        "recent_activity": [
            {"when": "late July 2026", "what": "Published a Substack piece.", "source_url": "https://s.com/p"},
            {"when": "August 2026", "what": "Allegedly gave a talk.", "source_url": ""},       # no URL → dropped
            {"when": "", "what": "Launched v2.", "source_url": "not-a-url"},                    # bad URL → dropped
        ],
        "personal": ["Ran the SF Marathon (https://x.com/a/1)", "Has two kids"],                # unsourced → dropped
        "conversation_hooks": ["Her Substack piece on evals last week fits your roadmap."],
    })
    assert out["recent_activity"] == [{"when": "late July 2026", "what": "Published a Substack piece.",
                                       "source_url": "https://s.com/p"}]
    assert out["personal"] == ["Ran the SF Marathon (https://x.com/a/1)"]
    assert len(out["conversation_hooks"]) == 1


def test_postfilter_caps():
    out = ra._postfilter_deep({
        "recent_activity": [{"what": f"Item {i}", "source_url": f"https://s.com/{i}"} for i in range(9)],
        "personal": [f"Fact {i} (https://x.com/{i})" for i in range(5)],
        "conversation_hooks": [f"Her post {i} on Acme is relevant." for i in range(5)],
    })
    assert len(out["recent_activity"]) == 4 and len(out["personal"]) == 2
    assert len(out["conversation_hooks"]) == 2


def test_filler_hooks_dropped_verbatim_failure_modes():
    # The exact fabricated filler from a real rendered brief — must never survive the post-filter.
    for filler in ("Ask for introductions and background on melius.com",
                   "Understand the agenda for the lunch/meeting",
                   "Understand what prompted the meeting",
                   "Explore potential overlaps between your funds",
                   "Build rapport before the pitch"):
        assert ra.is_filler_point(filler), filler
    # Concrete, grounded points survive — even ask-shaped ones anchored to a real find.
    for real in ("He published a piece on agent memory last week — strong hook given your roadmap",
                 "Ask about her Devcon talk on eval harnesses",
                 "Their $4M seed closed in July per TechCrunch"):
        assert not ra.is_filler_point(real), real
    out = ra._postfilter_deep({"recent_activity": [], "personal": [],
                               "conversation_hooks": ["Build rapport before the pitch"]})
    assert out["conversation_hooks"] == []


def test_two_pass_merge_and_schema_roundtrip(monkeypatch):
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.setattr(ra, "_research_batch",
                        lambda batch, ctx, key, comms=None, known=None:
                        [{"email": a["email"], "title": "CEO", "company": "Acme",
                          "relevance": ["r"], "summary": "bio", "company_summary": "Acme does X."}
                         for a in batch])
    monkeypatch.setattr(ra, "_deep_batch",
                        lambda batch, ctx, key, known:
                        [{"email": "p0@x.com",
                          "recent_activity": [{"when": "late July 2026", "what": "Wrote a post.",
                                               "source_url": "https://s.com/p"},
                                              {"what": "urlless", "source_url": ""}],
                          "personal": ["Moved to Austin (https://x.com/p/2)"],
                          "conversation_hooks": ["Her post last week maps to your Acme thread.",
                                                 "Build rapport"]},
                         {"email": "deep-only@x.com",
                          "recent_activity": [{"what": "Launched Y.", "source_url": "https://y.com"}],
                          "personal": [], "conversation_hooks": []}])
    out = ra.research([{"name": "P Zero", "email": "p0@x.com"}], "ctx")["attendees"]
    assert [a["email"] for a in out] == ["p0@x.com", "deep-only@x.com"]  # input order, sweep extras after
    merged = out[0]
    # Pass A fields intact + Pass B fields attached, post-filtered end to end.
    assert merged["company"] == "Acme" and merged["company_summary"] == "Acme does X."
    assert merged["recent_activity"] == [{"when": "late July 2026", "what": "Wrote a post.",
                                          "source_url": "https://s.com/p"}]
    assert merged["personal"] == ["Moved to Austin (https://x.com/p/2)"]
    assert merged["conversation_hooks"] == ["Her post last week maps to your Acme thread."]
    assert out[1]["summary"] == "" and out[1]["recent_activity"][0]["what"] == "Launched Y."


def test_deep_disable_knob(monkeypatch):
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.setenv("SOTTO_RESEARCH_DEEP", "0")
    called = []
    monkeypatch.setattr(ra, "_research_batch",
                        lambda batch, ctx, key, comms=None, known=None:
                        [{"email": a["email"], "company": "C", "summary": "s"} for a in batch])
    monkeypatch.setattr(ra, "_deep_batch", lambda *a, **k: called.append(1) or [])
    out = ra.research([{"name": "A B", "email": "a@b.com"}], "")
    assert called == [] and len(out["attendees"]) == 1


def test_deep_batch_failure_never_kills_profile_results(monkeypatch):
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.delenv("SOTTO_RESEARCH_DEEP", raising=False)
    monkeypatch.setattr(ra, "_research_batch",
                        lambda batch, ctx, key, comms=None, known=None:
                        [{"email": a["email"], "company": "C", "summary": "s"} for a in batch])

    def boom(*a, **k):
        raise RuntimeError("deep pass exploded")
    monkeypatch.setattr(ra, "_deep_batch", boom)
    out = ra.research([{"name": "A B", "email": "a@b.com"}], "")
    assert [a["email"] for a in out["attendees"]] == ["a@b.com"]       # Pass A survives Pass B crash


def test_deep_horizon_gate():
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    far = (datetime.now(timezone.utc) + timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    assert ra._within_research_horizon({"meeting_start": soon})
    assert not ra._within_research_horizon({"meeting_start": far})     # beyond 72h → no deep spend
    assert ra._within_research_horizon({})                              # no start → trust the selector


def test_known_facts_prefers_explicit_known_and_reads_graph(tmp_path, monkeypatch):
    assert ra._known_facts({"known": "CEO at Acme", "email": "x@y.com"}) == "CEO at Acme"
    # Graph fallback: a persisted profile's title/company + facts pack into the novelty anchor.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    people = os.path.join(str(tmp_path), "knowledge", "people")
    os.makedirs(people, exist_ok=True)
    with open(os.path.join(people, "c_dana.md"), "w") as f:
        f.write("---\nschema: 1\ncanonical_id: c_dana\nname: Dana Roe\ntitle: CEO\ncompany: Acme\n"
                "identifiers: [dana@acme.com]\nfacts:\n  f1:\n    text: Raised a Series A\n"
                "    type: context\n    status: active\n    seen: 1\n    conf: 0.8\n    source: ''\n"
                "    source_ref: ''\n    first: '2026-08-01'\n    last: '2026-08-01'\n---\n")
    known = ra._known_facts({"name": "Dana Roe", "email": "dana@acme.com"})
    assert "CEO at Acme" in known and "Raised a Series A" in known
