"""research_attendees.py — batched grounded research (port of gemini-research.ts): stub, dedup/cap, batching."""
import importlib.util, json, os, sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
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
    # People to research and nothing to ask: empty AND a warning the brief turns into an
    # unavailable source (compose_brief._normalize_local) — never a silent {"attendees":[]}.
    assert ra.research([{"name": "A", "email": "a@b.com"}], "") == {
        "attendees": [], "warnings": [ra.wr.NO_PROVIDER]}
    # Nobody to research is not a broken source — no warning.
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


def test_comms_by_email_accepts_per_attendee_shape():
    # gather_google --attendee-comms output: already keyed per attendee, with direction —
    # the meeting-prep path (30d per-attendee threads, not the 24h global gmail file).
    comms = {"Dana@acme.com": [
        {"date": "Tue", "subject": "Pricing", "snippet": "circling back on the pilot",
         "from_me": False},
        {"date": "Wed", "subject": "Re: Pricing", "snippet": "sent the revised SOW",
         "from_me": True},
    ] + [{"date": f"d{i}", "subject": f"s{i}", "snippet": "x", "from_me": False} for i in range(3)],
        "other@x.com": [{"date": "d", "subject": "s", "snippet": "not an attendee",
                         "from_me": False}]}
    by = ra._comms_by_email(comms, {"dana@acme.com"})
    assert by["dana@acme.com"][0] == 'email Tue "Pricing" (them→you): circling back on the pilot'
    assert by["dana@acme.com"][1] == 'email Wed "Re: Pricing" (you→them): sent the revised SOW'
    assert len(by["dana@acme.com"]) == 3                   # same ≤3 cap as the raw-gmail path
    assert "other@x.com" not in by                         # non-attendees dropped
    p = ra._build_prompt([{"name": "Dana Roe", "email": "dana@acme.com"}], "", comms_by_email=by)
    assert 'Recent comms with the user: email Tue "Pricing" (them→you)' in p


# ── Pass B: recency sweep, novelty, post-filters, knobs ───────────────────────────────────────────

def test_deep_prompt_novelty_includes_known_facts_and_recency_window():
    p = ra._build_deep_prompt([{"name": "Dana Roe", "email": "dana@acme.com"}], "ctx",
                              {"dana@acme.com": "CEO at Acme; raised a Series A"}, 45)
    assert "Already known (do NOT repeat any of this): CEO at Acme; raised a Series A" in p
    assert "last ~45 days" in p
    assert '"[Name] blog"' in p and '"[Name] podcast OR talk"' in p    # multiple distinct searches
    assert "an empty list beats a stretch" in p
    assert "NEVER write generic process points" in p                    # anti-fabrication rule


def test_recency_days_is_a_named_constant(monkeypatch):
    """Not a knob — the window is DEFAULT_RECENCY_DAYS, and the prompt builder reads it."""
    assert ra._recency_days() == ra.DEFAULT_RECENCY_DAYS == 90
    monkeypatch.setattr(ra, "DEFAULT_RECENCY_DAYS", 30)
    assert ra._recency_days() == 30


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


def test_main_writes_out_file_and_truncates_stale(tmp_path, monkeypatch, capsys):
    # --out (default /tmp/sotto_research.json) is always truncated+rewritten — a skipped/empty
    # research run must overwrite the morning's file, not leave stale bios for the afternoon prep.
    stub = tmp_path / "s.json"
    stub.write_text(json.dumps({"attendees": [{"email": "a@b.com", "summary": "fresh bio"}]}))
    monkeypatch.setenv("SOTTO_LLM_STUB", str(stub))
    att = tmp_path / "in.json"
    att.write_text(json.dumps([{"name": "A", "email": "a@b.com"}]))
    out = tmp_path / "research.json"
    out.write_text(json.dumps({"attendees": [{"email": "stale@old.com", "summary": "MORNING'S"}]}))
    monkeypatch.setattr("sys.argv", ["research_attendees.py", "--attendees", str(att),
                                     "--out", str(out)])
    ra.main()
    payload = json.loads(out.read_text())
    assert payload == {"attendees": [{"email": "a@b.com", "summary": "fresh bio"}]}
    assert json.loads(capsys.readouterr().out) == payload   # stdout kept for compatibility

    # Empty attendee list → the file still gets rewritten, to {"attendees": []}.
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)
    att.write_text("[]")
    monkeypatch.setattr("sys.argv", ["research_attendees.py", "--attendees", str(att),
                                     "--out", str(out)])
    ra.main()
    assert json.loads(out.read_text()) == {"attendees": []}


# ── Focus pass (--focus): ONE person, ONE extra grounded call, company_deep ───────────────────────

def test_matches_focus_one_rule():
    # '@' in the argument → exact email; otherwise a case-insensitive substring of the name or of
    # the email's local part.
    assert ra.matches_focus("spencer", "Spencer Kim", "spencer.kim@commenda.io")
    assert ra.matches_focus("KIM", "Spencer Kim", "s.kim@commenda.io")
    assert ra.matches_focus("s.kim", "Someone Else", "s.kim@commenda.io")     # local part
    assert ra.matches_focus("spencer.kim@commenda.io", "Spencer Kim", "spencer.kim@commenda.io")
    assert not ra.matches_focus("spencer@other.com", "Spencer Kim", "spencer.kim@commenda.io")
    assert not ra.matches_focus("dana", "Spencer Kim", "spencer.kim@commenda.io")
    assert not ra.matches_focus("", "Spencer Kim", "spencer.kim@commenda.io")
    assert not ra.matches_focus("commenda.io", "Spencer Kim", "spencer.kim@commenda.io")  # domain ≠ local


def test_focus_prompt_covers_company_founder_traction_market_and_honesty():
    p = ra._build_focus_prompt({"name": "Spencer Kim", "email": "spencer@commenda.io"}, "ctx",
                               "CEO at Commenda")
    assert "Focus Person (the ONLY person to research here)" in p
    assert "(company domain commenda.io — that is the company to research)" in p
    assert "Already known: CEO at Commenda" in p
    for field in ("builds:", "founder_story:", "traction:", "market:"):
        assert field in p
    assert '"[Company] competitors OR alternatives"' in p          # multiple distinct searches
    assert '"Not public" beats a guess' in p
    assert ra.NO_UNSOURCED_NUMBERS in p                            # same discipline as Pass B
    assert "return company_deep with empty fields" in p            # empty beats a stretch
    # A freemail focus gets no company-domain instruction to chase.
    q = ra._build_focus_prompt({"name": "Jo Free", "email": "jo@yahoo.com"}, "", "")
    assert "that is the company to research" not in q
    assert "Already known: (nothing on file)" in q


def test_postfilter_focus_drops_unsourced_traction_and_empty_fields():
    out = ra._postfilter_focus({"company_deep": {
        "company": "Commenda",
        "builds": [f"Product area {i}" for i in range(7)] + ["", "  "],
        "founder_story": "  ",
        "traction": ["Series A led by Ridgeline, June 2026 (https://tc.com/a)",
                     "Allegedly at $4M ARR",                       # no URL → dropped
                     "Launched the filing API (https://c.io/blog)"],
        "market": "Displaces the EOR incumbents.",
    }})["company_deep"]
    assert out["builds"] == [f"Product area {i}" for i in range(5)]         # capped at 5, blanks gone
    assert "founder_story" not in out                                       # empty field never renders
    assert out["traction"] == ["Series A led by Ridgeline, June 2026 (https://tc.com/a)",
                               "Launched the filing API (https://c.io/blog)"]
    assert out["market"] == "Displaces the EOR incumbents."
    assert out["company"] == "Commenda"


def test_postfilter_focus_company_name_alone_is_not_a_deep_dive():
    assert ra._postfilter_focus({"company_deep": {"company": "Commenda"}}) == {}
    assert ra._postfilter_focus({"company_deep": {}}) == {}
    assert ra._postfilter_focus({}) == {}


def _stub_passes(monkeypatch):
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.setattr(ra, "_research_batch",
                        lambda batch, ctx, key, comms=None, known=None:
                        [{"email": a["email"], "title": "CEO", "company": "Commenda",
                          "relevance": [], "summary": "bio"} for a in batch])
    monkeypatch.setattr(ra, "_deep_batch", lambda batch, ctx, key, known: [])


def test_focus_merges_company_deep_onto_that_one_attendee(monkeypatch):
    _stub_passes(monkeypatch)
    calls = []

    def fake_focus(attendee, ctx, key, known):
        calls.append((attendee["email"], known))
        return [{"email": "WRONG@echo.com",                # model echoed a different key…
                 "company_deep": {"company": "Commenda", "builds": ["Global tax filing"],
                                  "founder_story": "Built it after a misfiled quarter.",
                                  "traction": ["Series A, June 2026 (https://tc.com/a)",
                                               "unsourced claim"],
                                  "market": "Post-EOR shift."}}]
    monkeypatch.setattr(ra, "_focus_batch", fake_focus)
    people = [{"name": "Spencer Kim", "email": "spencer@commenda.io", "known": "CEO at Commenda"},
              {"name": "Dana Roe", "email": "dana@acme.com"}]
    out = ra.research(people, "ctx", focus="spencer")["attendees"]
    assert calls == [("spencer@commenda.io", "CEO at Commenda")]      # ONE call, for ONE person
    spencer, dana = out
    # …but the join key is the TARGET's email, so a deep dive can never land on someone else.
    assert spencer["email"] == "spencer@commenda.io" and spencer["company"] == "Commenda"
    assert spencer["company_deep"]["builds"] == ["Global tax filing"]
    assert spencer["company_deep"]["traction"] == ["Series A, June 2026 (https://tc.com/a)"]
    assert "company_deep" not in dana                                  # nobody else gets one
    assert not any(a["email"] == "wrong@echo.com" for a in out)


def test_focus_no_match_and_deep_disabled_fire_no_extra_call(monkeypatch):
    _stub_passes(monkeypatch)
    calls = []
    monkeypatch.setattr(ra, "_focus_batch", lambda *a, **k: calls.append(1) or [])
    ra.research([{"name": "Dana Roe", "email": "dana@acme.com"}], "", focus="spencer")
    assert calls == []                                                # no match → sweep only
    monkeypatch.setenv("SOTTO_RESEARCH_DEEP", "0")
    ra.research([{"name": "Spencer Kim", "email": "spencer@commenda.io"}], "", focus="spencer")
    assert calls == []                                                # the knob kills it too


def test_focus_call_failure_never_kills_the_rest(monkeypatch):
    _stub_passes(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("focus pass exploded")
    monkeypatch.setattr(ra, "_focus_batch", boom)
    out = ra.research([{"name": "Spencer Kim", "email": "spencer@commenda.io"}], "", focus="spencer")
    assert [a["email"] for a in out["attendees"]] == ["spencer@commenda.io"]
    assert "company_deep" not in out["attendees"][0]


def test_cli_focus_flag_reaches_research(tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(ra, "research",
                        lambda att, ctx, comms=None, focus="": seen.update(focus=focus) or
                        {"attendees": []})
    att = tmp_path / "in.json"
    att.write_text(json.dumps([{"name": "Spencer Kim", "email": "spencer@commenda.io"}]))
    monkeypatch.setattr("sys.argv", ["research_attendees.py", "--attendees", str(att),
                                     "--out", str(tmp_path / "o.json"), "--focus", "Spencer"])
    ra.main()
    capsys.readouterr()
    assert seen["focus"] == "Spencer"


# ── Dashboard persistence hook: $SOTTO_DATA/cache/research_<local-date>.json ─────────────────────
# (The Window M3 — docs/plans/web-dashboard-the-window.md. Best-effort like _archive_brief.)

from datetime import datetime, timedelta  # noqa: E402


def _run_main(tmp_path, monkeypatch, attendees):
    """Drive main() with a stubbed LLM returning `attendees`; returns the --out path."""
    stub = tmp_path / "stub.json"
    stub.write_text(json.dumps({"attendees": attendees}))
    monkeypatch.setenv("SOTTO_LLM_STUB", str(stub))
    att = tmp_path / "in.json"
    att.write_text(json.dumps([{"name": "A", "email": "a@b.com"}]))
    out = tmp_path / "research.json"
    monkeypatch.setattr("sys.argv", ["research_attendees.py", "--attendees", str(att),
                                     "--out", str(out)])
    ra.main()
    return out


def _local_date(days_ago=0):
    return (ra._now_local(ra.configured_tz()) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def test_cache_written_with_local_date_name(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setenv("SOTTO_DATA", str(data))
    rows = [{"email": "a@b.com", "summary": "bio", "company": "Acme",
             "recent_activity": [{"when": "last week", "what": "wrote a post",
                                  "source_url": "https://x.com/p"}],
             "conversation_hooks": ["He published a piece on Acme's launch last week."]}]
    _run_main(tmp_path, monkeypatch, rows)
    capsys.readouterr()
    cache = data / "cache" / f"research_{_local_date()}.json"
    payload = json.loads(cache.read_text())
    assert payload["attendees"] == rows            # research output verbatim
    assert payload["written_at"].endswith("Z")
    assert not list((data / "cache").glob(".research_*"))   # tmp file replaced away


def test_cache_empty_run_never_clobbers_todays_earlier_cache(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setenv("SOTTO_DATA", str(data))
    cache = data / "cache" / f"research_{_local_date()}.json"
    cache.parent.mkdir(parents=True)
    morning = {"attendees": [{"email": "a@b.com", "summary": "MORNING"}], "written_at": "x"}
    cache.write_text(json.dumps(morning))
    out = _run_main(tmp_path, monkeypatch, [])     # empty run
    capsys.readouterr()
    assert json.loads(out.read_text()) == {"attendees": []}   # --out IS truncated, by design
    assert json.loads(cache.read_text()) == morning           # cache untouched


def test_cache_prunes_files_older_than_seven_days(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setenv("SOTTO_DATA", str(data))
    d = data / "cache"
    d.mkdir(parents=True)
    old = d / f"research_{_local_date(10)}.json"
    recent = d / f"research_{_local_date(3)}.json"
    unrelated = d / "companies.json"
    for p in (old, recent, unrelated):
        p.write_text("{}")
    _run_main(tmp_path, monkeypatch, [{"email": "a@b.com", "summary": "bio"}])
    capsys.readouterr()
    assert not old.exists()                        # >7d pruned
    assert recent.exists() and unrelated.exists()  # <=7d + non-matching names kept
    assert (d / f"research_{_local_date()}.json").exists()


def test_cache_failure_never_fails_research(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("SOTTO_DATA", str(blocker / "data"))   # makedirs will raise
    out = _run_main(tmp_path, monkeypatch, [{"email": "a@b.com", "summary": "bio"}])
    payload = json.loads(out.read_text())          # main() completed: --out + stdout intact
    assert payload["attendees"][0]["summary"] == "bio"
    assert json.loads(capsys.readouterr().out) == payload


def test_no_key_degrade_is_narrated(tmp_path, monkeypatch, capsys):
    # {"attendees":[]} with zero signal read as "research ran and found nothing" — the no-provider
    # degrade must say so on stderr so the agent narrates honestly, and it must name every key that
    # would fix it (not just Gemini's).
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    for k in ("GOOGLE_AI_API_KEY", "EXA_API_KEY", "PARALLEL_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert ra.research([{"name": "A", "email": "a@b.com"}], "") == {
        "attendees": [], "warnings": [ra.wr.NO_PROVIDER]}
    err = capsys.readouterr().err
    assert "[research] skipped — no search provider connected" in err
    assert "EXA_API_KEY" in err and "PARALLEL_API_KEY" in err and "GOOGLE_AI_API_KEY" in err
    # An intentionally empty attendee list is NOT narrated (nothing was skipped).
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    assert ra.research([], "") == {"attendees": []}
    assert "skipped" not in capsys.readouterr().err


def test_research_runs_on_exa_alone(tmp_path, monkeypatch):
    """The hole docs/MODELS.md named: no GOOGLE_AI_API_KEY used to mean no attendee research at
    all. With Exa connected the same batch runs, the same shape comes back, and the Gemini rung is
    never reached (it has no key to spend)."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    monkeypatch.setenv("EXA_API_KEY", "e")
    monkeypatch.setattr(ra.wr, "_exa_deep", lambda prompt, schema, timeout: {
        "attendees": [{"email": "a@b.com", "company": "Acme", "summary": "bio"}]})

    def _no_gemini(*a, **k):
        raise AssertionError("the Gemini rung must not run without GOOGLE_AI_API_KEY")
    monkeypatch.setattr(ra, "_gemini_grounded", _no_gemini)
    out = ra.research([{"name": "A", "email": "a@b.com"}], "")
    assert out["attendees"][0]["company"] == "Acme"


def test_parallel_wins_the_deep_research_ladder(tmp_path, monkeypatch):
    """Precedence at the real call site: Parallel first, Exa second, Gemini last."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    for k, v in (("GOOGLE_AI_API_KEY", "g"), ("EXA_API_KEY", "e"), ("PARALLEL_API_KEY", "p")):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ra.wr, "_parallel_task", lambda prompt, schema, timeout: {
        "attendees": [{"email": "a@b.com", "company": "FromParallel", "summary": "bio"}]})
    monkeypatch.setattr(ra.wr, "_exa_deep", lambda *a: (_ for _ in ()).throw(
        AssertionError("Exa must not run while Parallel answers")))
    monkeypatch.setattr(ra, "_gemini_grounded", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("Gemini must not run while Parallel answers")))
    out = ra.research([{"name": "A", "email": "a@b.com"}], "")
    assert out["attendees"][0]["company"] == "FromParallel"


def test_deep_by_default_never_fans_out_across_matching_people(monkeypatch):
    """Depth follows focus, and focus is ONE person: even when the name the user said matches
    several attendees on the calendar, the focus pass fires exactly ONE extra grounded call.
    A sweep across eight meetings can never become eight deep dives."""
    _stub_passes(monkeypatch)
    calls = []
    monkeypatch.setattr(ra, "_focus_batch",
                        lambda attendee, ctx, key, known: calls.append(attendee["email"]) or [])
    people = [{"name": "Spencer Kim", "email": "spencer@commenda.io"},
              {"name": "Spencer Ruiz", "email": "spencer@other.com"},
              {"name": "Dana Roe", "email": "dana@acme.com"}]
    ra.research(people, "ctx", focus="spencer")
    assert calls == ["spencer@commenda.io"]        # one target, one call — not one per match
