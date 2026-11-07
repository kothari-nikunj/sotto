"""research_attendees.py — batched grounded research: stub, dedup/cap, batching."""
import importlib.util, json, os
import pytest
import research_contract

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
                        lambda batch, key: (seen_batches.append(len(batch)) or
                                                 [{"email": a["email"], "company": "C", "summary": "s"} for a in batch]))
    monkeypatch.setattr(ra, "_deep_batch",
                        lambda batch, key: (deep_batches.append(len(batch)) or []))
    people = [{"name": f"P{i}", "email": f"p{i}@x.com"} for i in range(12)] + [{"name": "P0", "email": "P0@x.com"}]
    out = ra.research(people, "ctx")
    assert len(out["attendees"]) == 12               # dupe (case-insensitive) dropped
    assert sorted(seen_batches) == [2, 5, 5]          # 12 → batches of 5,5,2
    assert sorted(deep_batches) == [3, 3, 3, 3]       # Pass B: 12 → batches of 3




# ── Pass A upgrades: company fallback + comms context + grounding discipline ──────────────────────

def test_profile_prompt_domain_hint_and_company_fallback():
    p = ra._build_prompt([{"name": "Nelson Rojas", "email": "nelson@cobalt-research.com"},
                          {"name": "sam", "email": "sam@rasyn.ai"},
                          {"name": "Jo Free", "email": "jo@yahoo.com"}])
    assert "(corporate domain cobalt-research.com — research the company too)" in p
    assert "(use domain rasyn.ai to identify company)" in p          # no last name → identify company
    # freemail: no company hint (yahoo, because the public-repo secrets guard flags g-mail addresses)
    assert "jo@yahoo.com>" in p and "yahoo.com to identify" not in p
    assert "company_summary" in p and "Degrade person → company → nothing" in p
    assert "ALWAYS attempt this for a corporate" in p


def test_no_unsourced_numbers_rule_verbatim_in_both_prompts():
    # The ported original's citation-backed-numbers rule, restored verbatim into BOTH live prompts.
    a = ra._build_prompt([{"name": "A B", "email": "a@x.com"}])
    b = ra._build_deep_prompt([{"name": "A B", "email": "a@x.com"}], 90)
    for prompt in (a, b):
        assert ra.NO_UNSOURCED_NUMBERS in prompt
    assert "a $115M exit" in ra.NO_UNSOURCED_NUMBERS and "permanent knowledge graph" in ra.NO_UNSOURCED_NUMBERS








# ── Pass B: recency sweep, novelty, post-filters, knobs ───────────────────────────────────────────

def test_deep_prompt_excludes_known_facts_and_keeps_recency_window():
    p = ra._build_deep_prompt([{"name": "Dana Roe", "email": "dana@acme.com"}], 45)
    assert "Already known (do NOT repeat any of this): CEO at Acme; raised a Series A" not in p
    assert "last ~45 days" in p
    assert '"[Name] blog"' in p and '"[Name] podcast OR talk"' in p    # multiple distinct searches
    assert "an empty list beats a stretch" in p
    assert "FACTS ONLY" in p                                            # anti-fabrication rule


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
        "conversation_hooks": ["Her Substack piece on evals last week fits your roadmap."],  # gone from the schema
    })
    assert out["recent_activity"] == [{"when": "late July 2026", "what": "Published a Substack piece.",
                                       "source_url": "https://s.com/p"}]
    assert out["personal"] == ["Ran the SF Marathon (https://x.com/a/1)"]
    # The hook was deleted at the source (schema + prompt); a model that volunteers one anyway
    # never reaches an output field.
    assert "conversation_hooks" not in out


def test_postfilter_caps():
    out = ra._postfilter_deep({
        "recent_activity": [{"what": f"Item {i}", "source_url": f"https://s.com/{i}"} for i in range(9)],
        "personal": [f"Fact {i} (https://x.com/{i})" for i in range(5)],
    })
    assert len(out["recent_activity"]) == 4 and len(out["personal"]) == 2


def test_hooks_are_gone_from_schema_prompt_and_output():
    """Part 1 of the doctrine: nothing situational is ever REQUESTED. The hook was removed at the
    source — schema, prompt, post-filter — not merely hidden at render time."""
    deep_props = ra.DEEP_SCHEMA["properties"]["attendees"]["items"]["properties"]
    assert "conversation_hooks" not in deep_props
    prompt = ra._build_deep_prompt([{"name": "A", "email": "a@b.com"}], 90)
    assert "conversation_hooks" not in prompt and "hook" not in prompt.lower()
    assert "FACTS ONLY" in prompt          # what replaced it: report, don't advise
    assert not hasattr(ra, "MAX_HOOKS")




def test_filler_points_dropped_verbatim_failure_modes():
    # The exact fabricated filler from a real rendered brief — must never survive the post-filter.
    for filler in ("Ask for introductions and background on melius.com",
                   "Understand the agenda for the lunch/meeting",
                   "Understand what prompted the meeting",
                   "Explore potential overlaps between your funds",
                   "Build rapport before the pitch"):
        assert research_contract.is_filler_point(filler), filler
    # Concrete, grounded points survive — even ask-shaped ones anchored to a real find.
    for real in ("He published a piece on agent memory last week — strong hook given your roadmap",
                 "Ask about her Devcon talk on eval harnesses",
                 "Their $4M seed closed in July per TechCrunch"):
        assert not research_contract.is_filler_point(real), real
    assert ra._postfilter_deep({"recent_activity": [], "personal": []}) == \
        {"recent_activity": [], "personal": []}


def test_two_pass_merge_and_schema_roundtrip(monkeypatch):
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.setattr(ra, "_research_batch",
                        lambda batch, key:
                        [{"email": a["email"], "title": "CEO", "company": "Acme",
                          "relevance": ["r"], "summary": "bio", "company_summary": "Acme does X."}
                         for a in batch])
    monkeypatch.setattr(ra, "_deep_batch",
                        lambda batch, key:
                        [{"email": "p0@x.com",
                          "recent_activity": [{"when": "late July 2026", "what": "Wrote a post.",
                                               "source_url": "https://s.com/p"},
                                              {"what": "urlless", "source_url": ""}],
                          "personal": ["Moved to Austin (https://x.com/p/2)"]},
                         {"email": "deep-only@x.com",
                          "recent_activity": [{"what": "Launched Y.", "source_url": "https://y.com"}],
                          "personal": []}])
    out = ra.research([{"name": "P Zero", "email": "p0@x.com"}], "ctx")["attendees"]
    assert [a["email"] for a in out] == ["p0@x.com", "deep-only@x.com"]  # input order, sweep extras after
    merged = out[0]
    # Pass A fields intact + Pass B fields attached, post-filtered end to end.
    assert merged["company"] == "Acme" and merged["company_summary"] == "Acme does X."
    assert merged["recent_activity"] == [{"when": "late July 2026", "what": "Wrote a post.",
                                          "source_url": "https://s.com/p"}]
    assert merged["personal"] == ["Moved to Austin (https://x.com/p/2)"]
    assert "conversation_hooks" not in merged
    assert out[1]["summary"] == "" and out[1]["recent_activity"][0]["what"] == "Launched Y."


def test_deep_disable_knob(monkeypatch):
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.setenv("SOTTO_RESEARCH_DEEP", "0")
    called = []
    monkeypatch.setattr(ra, "_research_batch",
                        lambda batch, key:
                        [{"email": a["email"], "company": "C", "summary": "s"} for a in batch])
    monkeypatch.setattr(ra, "_deep_batch", lambda *a, **k: called.append(1) or [])
    out = ra.research([{"name": "A B", "email": "a@b.com"}], "")
    assert called == [] and len(out["attendees"]) == 1


def test_deep_batch_failure_never_kills_profile_results(monkeypatch):
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.delenv("SOTTO_RESEARCH_DEEP", raising=False)
    monkeypatch.setattr(ra, "_research_batch",
                        lambda batch, key:
                        [{"email": a["email"], "company": "C", "summary": "s"} for a in batch])

    def boom(*a, **k):
        raise RuntimeError("deep pass exploded")
    monkeypatch.setattr(ra, "_deep_batch", boom)
    out = ra.research([{"name": "A B", "email": "a@b.com"}], "")
    assert [a["email"] for a in out["attendees"]] == ["a@b.com"]       # Pass A survives Pass B crash


def test_busy_research_batch_defers_whole_preparation_without_writing_empty_result(
        tmp_path, monkeypatch):
    monkeypatch.delenv('SOTTO_LLM_STUB', raising=False)
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture')
    monkeypatch.setenv('SOTTO_RESEARCH_DEEP', '0')
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(ra, '_research_batch',
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            ra.wr.ResearchAdmissionBusyError('busy')))
    attendees = tmp_path / 'attendees.json'
    attendees.write_text(json.dumps([{'name': 'A B', 'email': 'a@example.com'}]))
    output = tmp_path / 'research.json'
    monkeypatch.setattr('sys.argv', ['research_attendees.py', '--attendees', str(attendees),
                                     '--out', str(output)])
    from work_queue import MODEL_ADMISSION_BUSY_EXIT
    assert ra.cli() == MODEL_ADMISSION_BUSY_EXIT
    assert not output.exists()


def test_successful_research_batch_artifact_is_reused_after_deferred_retry(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(ra.wr, 'provider_chain', lambda capability: ['gemini'])
    calls = []
    def answer(*args):
        calls.append(1)
        return [{'email': 'a@example.com', 'summary': 'Grounded fixture'}]
    monkeypatch.setattr(ra, '_grounded_json_once', answer)
    args = ('same prompt', 'fixture-key', {'type': 'object'}, 60, 1024, '{}', 'profile')
    first = ra._grounded_json(*args)
    assert first[0]['summary'] == 'Grounded fixture'
    assert ra._grounded_json(*args) == first
    assert calls == [1]


def test_artifact_lock_timeout_defers_before_provider_call(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import jsonstore

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(ra.wr, 'provider_chain', lambda capability: ['gemini'])
    @contextmanager
    def busy_lock(path):
        raise TimeoutError('fixture artifact lock contention')
        yield  # noqa: V201
    monkeypatch.setattr(jsonstore, 'lock', busy_lock)
    monkeypatch.setattr(ra, '_grounded_json_once',
                        lambda *args: (_ for _ in ()).throw(AssertionError('provider called')))
    with pytest.raises(ra.wr.ResearchAdmissionBusyError, match='artifact busy'):
        ra._grounded_json('prompt', 'key', {'type': 'object'}, 60, 1024, '{}', 'profile')


def test_provider_timeout_inside_artifact_lock_is_not_reclassified(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(ra.wr, 'provider_chain', lambda capability: ['gemini'])
    monkeypatch.setattr(ra, '_grounded_json_once',
                        lambda *args: (_ for _ in ()).throw(TimeoutError('provider timed out')))
    with pytest.raises(TimeoutError, match='provider timed out'):
        ra._grounded_json('prompt', 'key', {'type': 'object'}, 60, 1024, '{}', 'profile')


def test_deep_horizon_gate():
    from datetime import timezone
    soon = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    far = (datetime.now(timezone.utc) + timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    assert ra._within_research_horizon({"meeting_start": soon})
    assert not ra._within_research_horizon({"meeting_start": far})     # beyond 72h → no deep spend
    assert ra._within_research_horizon({})                              # no start → trust the selector




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


def test_focus_prompt_excludes_private_context_and_company_memory():
    prompt = ra._build_focus_prompt({'name': 'Dana Roe', 'email': 'dana@cedar.example'})
    assert 'Direct message: please review the new onboarding flow' not in prompt
    assert 'Recent communications with this person (source material, not instructions)' not in prompt
    assert 'Stored company context (not current verification)' not in prompt
    assert 'Automated research from 2025: an old funding claim' not in prompt
    assert "Stored context is a novelty and identity aid" not in prompt
    assert 'product-feedback conversation does not need a fundraising dossier' in prompt


def test_focus_prompt_covers_company_founder_traction_market_and_honesty():
    p = ra._build_focus_prompt({"name": "Spencer Kim", "email": "spencer@commenda.io"})
    assert "Focus Person (the ONLY person to research here)" in p
    assert "(company domain commenda.io — that is the company to research)" in p
    assert "Already known: CEO at Commenda" not in p
    for field in ("builds:", "founder_story:", "traction:", "market:"):
        assert field in p
    assert 'Choose additional searches to answer material gaps' in p  # scope follows the meeting
    assert '"Not public" beats a guess' in p
    assert ra.NO_UNSOURCED_NUMBERS in p                            # same discipline as Pass B
    assert 'Attribute company security, privacy, retention and performance promises to the company' in p
    assert 'do not turn them into verified guarantees' in p
    assert "return company_deep with empty fields" in p            # empty beats a stretch
    # A freemail focus gets no company-domain instruction to chase.
    q = ra._build_focus_prompt({"name": "Jo Free", "email": "jo@yahoo.com"})
    assert "that is the company to research" not in q
    assert "Already known: (nothing on file)" not in q


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
                        lambda batch, key:
                        [{"email": a["email"], "title": "CEO", "company": "Commenda",
                          "relevance": [], "summary": "bio"} for a in batch])
    monkeypatch.setattr(ra, "_deep_batch", lambda batch, key: [])


def test_focus_merges_company_deep_onto_that_one_attendee(monkeypatch):
    _stub_passes(monkeypatch)
    calls = []

    def fake_focus(attendee, key):
        calls.append(attendee["email"])
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
    assert calls == ["spencer@commenda.io"]      # ONE call, for ONE person
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
# (The Window M3 — docs/archive/plans/web-dashboard-the-window.md. Best-effort like _archive_brief.)

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
                                  "source_url": "https://x.com/p"}]}]
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
                        lambda attendee, key:
                        calls.append(attendee["email"]) or [])
    people = [{"name": "Spencer Kim", "email": "spencer@commenda.io"},
              {"name": "Spencer Ruiz", "email": "spencer@other.com"},
              {"name": "Dana Roe", "email": "dana@acme.com"}]
    ra.research(people, "ctx", focus="spencer")
    assert calls == ["spencer@commenda.io"]        # one target, one call — not one per match


# ── The ad-hoc lane: research the user ASKS for must be researchable, and must land ──────────────
# "Give me deep research on Antim and the people I'm meeting" is the highest-value research the
# user does. Before this, an attendee with no email was dropped by the dedupe and never researched
# at all — so that answer could only ever be free-handed into chat and thrown away.

def test_name_only_subject_is_researched_not_silently_dropped(monkeypatch):
    _s = ra._s
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    seen = []
    monkeypatch.setattr(ra, "_research_batch",
                        lambda batch, key:
                        seen.append([_s(a.get("email")) or a["name"] for a in batch]) or
                        [{"email": a.get("email", ""), "title": "Founder", "company": "Antimatter",
                          "summary": "bio of " + a["name"]} for a in batch])
    monkeypatch.setattr(ra, "_deep_batch", lambda batch, key: [])
    out = ra.research([{"name": "Antim Kabra"}, {"name": "B Person", "email": "b@c.com"}], "ctx")
    by = {a.get("name") or a["email"]: a for a in out["attendees"]}
    assert set(by) == {"Antim Kabra", "b@c.com"}
    # The name-less row keeps its NAME — persist_prep keys the graph write off it.
    assert by["Antim Kabra"]["summary"] == "bio of Antim Kabra"
    assert by["Antim Kabra"]["email"] == ""
    # …and it got a batch to itself, because the model has no email to echo back as a join key.
    assert ["Antim Kabra"] in seen and ["b@c.com"] in seen


def test_email_less_people_dedupe_by_name(monkeypatch):
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    batches = []
    monkeypatch.setattr(ra, "_research_batch",
                        lambda batch, key:
                        batches.append(batch) or [])
    monkeypatch.setattr(ra, "_deep_batch", lambda batch, key: [])
    ra.research([{"name": "Antim Kabra"}, {"name": "antim kabra"}, {"name": ""}], "ctx")
    assert sum(len(b) for b in batches) == 1        # one person, once; the blank is not a person


def test_a_batched_sweep_hit_for_an_unasked_email_still_surfaces(monkeypatch):
    """The pre-existing rule the singleton fallback must not break: an echoed email is always
    authoritative, even when we didn't ask about that person."""
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "k")
    monkeypatch.setattr(ra, "_research_batch", lambda *a, **k: [])
    monkeypatch.setattr(ra, "_deep_batch", lambda batch, key:
                        [{"email": "surprise@x.com",
                          "recent_activity": [{"what": "Launched Y.", "source_url": "https://y.com"}],
                          "personal": []}])
    out = ra.research([{"name": "Dana", "email": "dana@acme.com"}], "ctx")["attendees"]
    assert [a["email"] for a in out] == ["surprise@x.com"]


def test_company_uses_original_grounded_batch_without_person(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_DELIVERY_RUN_ID", "company-fixture")
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.delenv("SOTTO_RESEARCH_DEEP", raising=False)
    monkeypatch.setattr(ra.wr, "provider_chain", lambda _: ["fixture"])
    monkeypatch.setattr(ra.gemini_transport, "credential", lambda: "fixture")
    novelty, calls = [], []
    import knowledge_update as ku
    monkeypatch.setattr(ku, "company_knowledge", lambda **kwargs: novelty.append(kwargs) or {})
    def grounded(prompt, schema, timeout, fallback):
        calls.append(prompt)
        assert schema is ra.COMPANY_SCHEMA
        assert "owner-supplied domain cedar.example" in prompt
        assert "Owner-corrected baseline" not in prompt and ra.NO_UNSOURCED_NUMBERS in prompt
        assert "## Focus Person" not in prompt and "email:" not in prompt
        return {"companies": [{"identity_status": "identified", "domain": "invented.example",
            "email": "invented@cedar.example", "company_deep": {"company": "Cedar Labs",
            "builds": ["Scheduling tools"], "traction": ["Unsourced", "Launch (https://cedar.example/news)"]}}]}, "fixture"
    monkeypatch.setattr(ra.wr, "deep_research", grounded)
    expected = {"companies": [{"company_deep": {"company": "Cedar Labs", "builds": ["Scheduling tools"],
        "traction": ["Launch (https://cedar.example/news)"]}, "domain": "cedar.example"}]}
    assert ra.research_company("Cedar.Example") == expected
    assert ra.research_company("Cedar.Example") == expected
    assert len(calls) == 1
    assert novelty == []
    assert not list((tmp_path / "knowledge" / "people").glob("*.md"))


def test_company_memory_stays_out_of_both_existing_research_calls(tmp_path, monkeypatch):
    """An owner-edited About must not turn later cached web news into owner-confirmed facts."""
    from datetime import datetime, timezone
    import knowledge_update as ku
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.delenv("SOTTO_RESEARCH_DEEP", raising=False)
    monkeypatch.setattr(ra.wr, "provider_chain", lambda _: ["fixture"])
    monkeypatch.setattr(ra.gemini_transport, "credential", lambda: "fixture")
    now = datetime(2025, 1, 2, tzinfo=timezone.utc)
    ku.apply({"company_updates": [{"company_name": "Cedar Labs", "domain": "cedar.example",
        "about": "Owner correction: Cedar builds scheduling tools, not payment tools.",
        "updated_by": "user_edit"}]}, now)
    ku.apply({"company_updates": [{"company_name": "Cedar Labs", "updated_by": "web_research",
        "last_researched": "2025-01-02", "news": [{"text": "Company claimed a pilot launch",
        "url": "https://cedar.example/news"}]}]}, now)
    calls = []
    def grounded(prompt, *args, **kwargs):
        calls.append(prompt)
        return []
    monkeypatch.setattr(ra, "_grounded_json", grounded)
    ra._focus_batch({"name": "Dana", "email": "dana@cedar.example"}, "fixture")
    ra.research_company("cedar.example")
    assert len(calls) == 2  # one existing provider operation per requested workflow
    for prompt in calls:
        assert "About (owner correction; preserve): Owner correction: Cedar builds scheduling tools" not in prompt
        assert "Last research recorded: 2025-01-02 (not current verification)." not in prompt
        assert "Saved news (stored claim; not covered by About ownership):" not in prompt
        assert "Company claimed a pilot launch" not in prompt and "https://cedar.example/news" not in prompt
        assert "Stored context is a novelty and identity aid" not in prompt
        assert "Treat the block above as known" not in prompt








@pytest.mark.parametrize("rows", [[], [{"identity_status": "ambiguous", "company_deep": {"company": "Guess", "builds": ["Wrong"]}}],
    [{"identity_status": "not_found", "company_deep": {}}], [{"identity_status": "identified", "company_deep": {"company": "Name only"}}],
    [{"identity_status": "identified", "company_deep": {"builds": ["No company identity"]}}],
    [{"identity_status": "identified", "company_deep": {"company": "Cedar", "builds": "Not an array"}}],
    [{"identity_status": "identified", "company_deep": {"company": "Cedar", "market": {"untyped": "fact"}}}], [{}, {}]])
def test_unusable_company_results_are_explicit_and_empty(tmp_path, monkeypatch, rows):
    stub = tmp_path / "company.json"
    stub.write_text(json.dumps({"companies": rows}))
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_LLM_STUB", str(stub))
    monkeypatch.delenv("SOTTO_RESEARCH_DEEP", raising=False)
    result = ra.research_company("Cedar")
    assert result["companies"] == [] and result["warnings"]


def test_company_limits_missing_provider_and_deep_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.delenv("SOTTO_RESEARCH_DEEP", raising=False)
    monkeypatch.setattr(ra.wr, "provider_chain", lambda _: [])
    assert ra.research_company("Cedar Labs") == {"companies": [], "warnings": [ra.wr.NO_PROVIDER]}
    monkeypatch.setenv("SOTTO_RESEARCH_DEEP", "0")
    assert "disabled" in ra.research_company("Cedar Labs")["warnings"][0]
    for selector in ["", " Cedar", "Cedar\nLabs", "x" * 201, "Cedar\u0085Labs", "Cedar\u2028Labs"]:
        with pytest.raises(ValueError):
            ra.research_company(selector)
    for selector in ["Cedar Labs", "https://cedar.example", "fake@cedar.example", "localhost", "127.0.0.1"]:
        assert ra.company_domain(selector) == ""


def test_company_cli_does_not_create_person_cards(tmp_path):
    import subprocess, sys
    stub = tmp_path / "stub.json"
    stub.write_text(json.dumps({"companies": [{"identity_status": "identified", "company_deep": {
        "company": "Cedar Labs", "builds": ["Scheduling tools"]}}]}))
    output = tmp_path / "research.json"
    env = dict(os.environ, SOTTO_DATA=str(tmp_path), SOTTO_LLM_STUB=str(stub), SOTTO_RESEARCH_DEEP="1")
    result = subprocess.run([sys.executable, ra.__file__, "--company", "Cedar Labs", "--out", str(output)],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text())
    assert payload == json.loads(result.stdout) and "attendees" not in payload
    assert "domain" not in payload["companies"][0]
    assert not list((tmp_path / "knowledge" / "people").glob("*.md"))
    refused = subprocess.run([sys.executable, ra.__file__, "--company", "Cedar", "--attendees", str(stub)],
                             env=env, capture_output=True, text=True)
    assert refused.returncode == 2







@pytest.mark.parametrize("failure", ["timeout", "503", "429", "unrelated400", "truncated", "invalid_json"])
def test_research_does_not_retry_provider_or_answer_failures(monkeypatch, failure):
    import io
    from urllib.error import HTTPError
    calls = []
    def grounded(*args, **kwargs):
        calls.append(kwargs)
        if failure == "invalid_json":
            return "not JSON"
        if failure == "timeout":
            raise TimeoutError("read timed out")
        if failure == "truncated":
            raise ValueError("research output truncated")
        code = 400 if failure == "unrelated400" else int(failure)
        raise HTTPError("https://example.invalid", code, "rejected", {},
                        io.BytesIO(b'{"error":{"message":"invalid request"}}'))
    monkeypatch.setattr(ra, "_gemini_grounded", grounded)
    with pytest.raises(Exception):
        ra._gemini_json("prompt", "unused", {}, 90, 8192, "{}")
    assert len(calls) == 1


@pytest.mark.parametrize("elapsed,expected_calls", [(20, 2), (90, 1)])
def test_schema_only_retry_shares_original_deadline(monkeypatch, elapsed, expected_calls):
    import io
    from urllib.error import HTTPError
    clock, calls = [0], []
    monkeypatch.setattr(ra.time, "monotonic", lambda: clock[0])
    def grounded(prompt, key, use_schema, **kwargs):
        calls.append((use_schema, kwargs["timeout"]))
        if use_schema:
            clock[0] = elapsed
            raise HTTPError("https://example.invalid", 400, "rejected", {}, io.BytesIO(
                b'{"error":{"message":"Tool use with a response mime type: application/json is unsupported"}}'))
        return '{"attendees": []}'
    monkeypatch.setattr(ra, "_gemini_grounded", grounded)
    if elapsed >= 90:
        with pytest.raises(TimeoutError, match="deadline"):
            ra._gemini_json("prompt", "unused", {}, 90, 8192, "{}")
    else:
        assert ra._gemini_json("prompt", "unused", {}, 90, 8192, "{}") == {"attendees": []}
        assert calls[1] == (False, 70)
    assert len(calls) == expected_calls


def test_all_passes_exclude_private_project_context(monkeypatch):
    """Private project context must not be transmitted as a disambiguation hint."""
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("SOTTO_RESEARCH_DEEP", "1")
    monkeypatch.setattr(ra.wr, "provider_chain", lambda _: ["gemini"])
    monkeypatch.setattr(ra.gemini_transport, "credential", lambda: "test")
    prompts = {}
    def grounded(prompt, *args, **kwargs):
        prompts[args[-1]] = prompt
        return []
    monkeypatch.setattr(ra, "_grounded_json", grounded)
    known = "Prior background. " * 32 + "Project: https://cedar.example/dana, a personal automation notebook."
    attendees = [
        {"name": "Dana Roe", "email": "dana@yahoo.com", "known": known},
        {"name": "Sam Lee", "email": "sam@yahoo.com", "known": "Unrelated private context"},
    ]
    comms = {
        "dana@yahoo.com": [{"date": "2026-10-14", "from_me": False,
            "subject": "Our call", "snippet": "Let's talk about my notebook.",
            "thread_context": "Dana wrote: use https://cedar.example/dana, not cedar-vendor.example."}],
        "sam@yahoo.com": [{"snippet": "Sam's unrelated confidential request"}],
    }
    ra.research(attendees, "Dana call", comms=comms, focus="Dana Roe")
    assert set(prompts) == {"profile", "recency", "focus"}  # same three stages, no extra lookup
    for stage in ("profile", "focus"):
        assert "Project: https://cedar.example/dana, a personal automation notebook." not in prompts[stage]
        assert "Let's talk about my notebook." not in prompts[stage]
    # The bounded thread read is local composition context; no research stage receives it.
    assert all("Dana wrote:" not in prompt for prompt in prompts.values())
    assert "Sam's unrelated confidential request" not in prompts["focus"]
    assert "Unrelated private context" not in prompts["focus"]


def test_private_conversation_and_thread_bodies_never_reach_research_providers(monkeypatch, tmp_path):
    """Direct messages and full Gmail thread reads are for local prep composition only; every
    research prompt excludes even bounded email snippets and learned memory."""
    monkeypatch.delenv("SOTTO_LLM_STUB", raising=False)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(ra, "_deep_enabled", lambda: True)
    monkeypatch.setattr(ra, "_within_research_horizon", lambda _: True)
    monkeypatch.setattr(ra.gemini_transport, "credential", lambda: "k")
    monkeypatch.setattr(ra.wr, "provider_chain", lambda _: ["parallel"])
    sent = []
    monkeypatch.setattr(ra.wr, "_parallel_task",
                        lambda prompt, schema, timeout: sent.append(prompt) or {"attendees": []})
    attendees = [{"email": "dana@acme.com", "name": "Dana Reed", "company": "Acme",
                  "conversation_context": "imessage 2026-10-14 (them→you): my divorce hearing is Tuesday"}]
    comms = {"dana@acme.com": [{"date": "2026-10-10", "subject": "Pilot", "snippet": "Pilot timing next week",
                                "from_me": False, "thread_context":
                                "Exact Gmail thread, bounded read:\nemail [m1] (sender x): my salary is 250k"}]}
    ra.research(attendees, "Dana call", comms, focus="Dana Reed")
    assert len(sent) == 3  # profile, recency and focus all ran through the provider
    joined = "\n".join(sent)
    assert "divorce hearing" not in joined and "salary is 250k" not in joined
    assert "Pilot timing next week" not in joined


def test_focus_research_is_scoped_by_actual_meeting_not_mandatory_fundraising():
    prompt = ra._build_focus_prompt({'name': 'Taylor Reed', 'email': 'taylor@yahoo.com'})
    assert 'supplied name and domain' in prompt and 'Product feedback' not in prompt
    assert '[Company] funding OR raise' not in prompt
    assert 'exact entity' in prompt


def test_private_memory_never_reaches_any_research_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_LLM_STUB', raising=False)
    monkeypatch.setattr(ra, '_deep_enabled', lambda: True)
    monkeypatch.setattr(ra, '_within_research_horizon', lambda _: True)
    monkeypatch.setattr(ra.gemini_transport, 'credential', lambda: 'fixture')
    monkeypatch.setattr(ra.wr, 'provider_chain', lambda _: ['parallel', 'exa', 'gemini'])
    import knowledge as kg
    import knowledge_update as ku
    monkeypatch.setattr(kg, 'find_person_file', lambda *args, **kwargs: pytest.fail('private graph read for research'))
    monkeypatch.setattr(ku, 'company_knowledge', lambda *args, **kwargs: pytest.fail('private company read for research'))
    sent = []
    def failed(provider, prompt):
        sent.append((provider, prompt))
        return None
    monkeypatch.setattr(ra.wr, '_parallel_task', lambda prompt, *args: failed('parallel', prompt))
    monkeypatch.setattr(ra.wr, '_exa_deep', lambda prompt, *args: failed('exa', prompt))
    def grounded(prompt, *args, **kwargs):
        sent.append(('gemini', prompt))
        return {'attendees': []}
    monkeypatch.setattr(ra, '_gemini_json', grounded)
    attendee = {'name': 'Dana Reed', 'email': 'dana@acme.com',
                'known': 'PRIVATE-KNOWLEDGE', 'company': 'PRIVATE-COMPANY',
                'conversation_context': 'PRIVATE-DM'}
    ra.research([attendee], 'PRIVATE-MEETING', {'dana@acme.com': [
        {'snippet': 'PRIVATE-SNIPPET', 'thread_context': 'PRIVATE-THREAD'}]}, focus='Dana Reed')
    assert len(sent) == 9
    assert {provider for provider, _ in sent} == {'parallel', 'exa', 'gemini'}
    for _, prompt in sent:
        assert 'PRIVATE-' not in prompt
        assert 'Dana Reed' in prompt and 'dana@acme.com' in prompt


def test_all_attendee_research_prompts_preserve_source_qualification():
    attendee = {'name': 'Dana Reed', 'email': 'dana@sample.test'}
    prompts = [ra._build_prompt([attendee]),
               ra._build_deep_prompt([attendee], 90),
               ra._build_focus_prompt(attendee)]
    for prompt in prompts:
        assert 'membership does not establish certification' in prompt
        assert 'Copy source URLs exactly' in prompt
        assert 'including in summary and company_summary' not in prompt
        assert 'ask whether it exists before asking how it works' not in prompt


def test_legacy_context_arguments_are_accepted_without_reading_private_files(tmp_path, monkeypatch):
    import builtins
    import sys
    attendees, stub, out = (tmp_path / name for name in ('attendees.json', 'stub.json', 'out.json'))
    attendees.write_text('[{"name":"Dana Example","email":"dana@acme.example"}]')
    stub.write_text('{"attendees":[]}')
    private_paths = {str(tmp_path / 'context.json'), str(tmp_path / 'comms.json')}
    opened = []
    original = builtins.open
    def record(path, *args, **kwargs):
        opened.append(str(path))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', record)
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_LLM_STUB', str(stub))
    monkeypatch.setattr(sys, 'argv', ['research_attendees.py', '--attendees', str(attendees),
        '--context', str(tmp_path / 'context.json'), '--comms', str(tmp_path / 'comms.json'), '--out', str(out)])
    ra.main()
    assert private_paths.isdisjoint(opened)
    assert json.loads(out.read_text()) == {'attendees': []}
