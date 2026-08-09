"""compose_meeting_prep.py — the standalone meeting-prep skill: external-attendee selection,
research/knowledge/granola join, and the single-message render. Offline (stubbed Gemini)."""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location(
    "compose_meeting_prep", os.path.join(ROOT, "meeting-prep", "scripts", "compose_meeting_prep.py"))
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)


def _soon(hours=6):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _event(summary, start, attendees, **extra):
    return {"id": summary.lower().replace(" ", "-"), "summary": summary, "start": start,
            "attendees": attendees, **extra}


def test_external_attendee_filter_drops_user_and_colleagues():
    inputs = {"google": {"userEmail": "me@myco.com",
                         "events": [_event("Pitch", _soon(), [
                             {"email": "me@myco.com", "displayName": "Me"},
                             {"email": "colleague@myco.com", "displayName": "Coworker"},
                             {"email": "vc@fund.com", "displayName": "Taylor VC"}])]}}
    ctx, meetings = mp.build_context(inputs)
    assert len(meetings) == 1
    names = [a["name"] for a in meetings[0]["attendees"]]
    assert names == ["Taylor VC"]            # only the external attendee survives
    assert "vc@fund.com" in ctx
    assert "colleague@myco.com" not in ctx


def test_internal_only_meeting_produces_no_prep():
    inputs = {"google": {"userEmail": "me@myco.com",
                         "events": [_event("Standup", _soon(), [
                             {"email": "me@myco.com"}, {"email": "colleague@myco.com"}])]}}
    ctx, meetings = mp.build_context(inputs)
    assert ctx == "" and meetings == []
    out = mp.compose(inputs)               # short-circuits, no LLM call
    assert out["meetings"] == []
    assert "internal" in out["prep_markdown"].lower()


def test_past_meeting_beyond_horizon_excluded():
    long_off = (datetime.now(timezone.utc) + timedelta(hours=200)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    inputs = {"google": {"userEmail": "me@myco.com",
                         "events": [_event("Far Future", long_off, [{"email": "vc@fund.com"}])]}}
    _, meetings = mp.build_context(inputs)
    assert meetings == []


def test_research_and_knowledge_join_into_context():
    inputs = {"google": {"userEmail": "me@myco.com",
                         "events": [_event("Coffee with Taylor", _soon(), [
                             {"email": "taylor@startup.com", "displayName": "Taylor Reed"}])]},
              "attendee_research": [{"email": "taylor@startup.com", "title": "CEO",
                                     "company": "Startup Inc", "relevance": ["Raising a Series A"],
                                     "summary": "Co-founder and CEO of Startup Inc, a dev-tools company."}],
              "prior_knowledge": {"taylor-reed": "Taylor Reed (taylor-reed) | CEO @ Startup Inc | taylor@startup.com\n= met at a conference"}}
    ctx, meetings = mp.build_context(inputs)
    assert "CEO at Startup Inc" in ctx
    assert "Series A" in ctx
    assert "dev-tools company" in ctx
    assert "met at a conference" in ctx     # knowledge graph folded in
    assert meetings[0]["attendees"][0]["role"] == "CEO"


def test_granola_history_joins_by_attendee_email():
    inputs = {"google": {"userEmail": "me@myco.com",
                         "events": [_event("Sync with Devon", _soon(), [
                             {"email": "devon@partner.com", "displayName": "Devon"}])]},
              "local": {"granola_meetings": [
                  {"title": "Devon intro", "date": "2026-06-01",
                   "ai_summary": "Discussed the integration timeline.",
                   "attendee_emails": ["devon@partner.com"]}]}}
    ctx, _ = mp.build_context(inputs)
    assert "past meetings" in ctx
    assert "integration timeline" in ctx


def test_unknown_attendee_marked_not_invented():
    inputs = {"google": {"userEmail": "me@myco.com",
                         "events": [_event("Intro call", _soon(), [
                             {"email": "stranger@unknown.com", "displayName": "Stranger"}])]}}
    ctx, _ = mp.build_context(inputs)
    assert "no public profile or prior knowledge found" in ctx


def test_compose_renders_single_message_with_stub(tmp_path):
    stub = tmp_path / "resp.json"
    stub.write_text(json.dumps({
        "prep_markdown": "**Coffee with Taylor** — today\n- Taylor Reed, CEO @ Startup Inc\nTalking points:\n- Ask about the Series A",
        "meetings": [{"event_id": "coffee-with-taylor", "title": "Coffee with Taylor",
                      "start": "x", "attendees": [{"name": "Taylor Reed", "role": "CEO", "company": "Startup Inc"}],
                      "talking_points": ["Ask about the Series A"]}]}))
    os.environ["SOTTO_LLM_STUB"] = str(stub)
    try:
        inputs = {"google": {"userEmail": "me@myco.com",
                             "events": [_event("Coffee with Taylor", _soon(), [
                                 {"email": "taylor@startup.com", "displayName": "Taylor Reed"}])]}}
        out = mp.compose(inputs)
        assert "Series A" in out["prep_markdown"]
        assert out["meetings"][0]["talking_points"] == ["Ask about the Series A"]
    finally:
        del os.environ["SOTTO_LLM_STUB"]


def test_prep_markdown_is_chat_formatted(tmp_path):
    """Sprint 0 §3: the skill delivers prep_markdown verbatim, so compose converts the LLM's
    markdown (## headings, **bold**, any stray markers) to chat syntax deterministically. The
    JSON contract keys are unchanged — only the text inside is chat-ready."""
    stub = tmp_path / "resp.json"
    stub.write_text(json.dumps({
        "prep_markdown": "## Today's meetings\n\n**Coffee with Taylor**<!--id:x--> — 10am\n"
                         "- Ask about the **Series A**",
        "meetings": [{"event_id": "coffee-with-taylor", "title": "Coffee with Taylor",
                      "start": "x", "attendees": [], "talking_points": ["Ask about the Series A"]}]}))
    os.environ["SOTTO_LLM_STUB"] = str(stub)
    try:
        inputs = {"google": {"userEmail": "me@myco.com",
                             "events": [_event("Coffee with Taylor", _soon(), [
                                 {"email": "taylor@startup.com", "displayName": "Taylor Reed"}])]}}
        out = mp.compose(inputs)
        md = out["prep_markdown"]
        assert "**" not in md and "## " not in md and "<!--" not in md
        assert "*Today's meetings*" in md               # heading → chat bold
        assert "*Coffee with Taylor* — 10am" in md      # bold name → single asterisk, marker gone
        assert out["meetings"][0]["talking_points"] == ["Ask about the Series A"]   # keys untouched
    finally:
        del os.environ["SOTTO_LLM_STUB"]


def test_meetings_in_time_order():
    inputs = {"google": {"userEmail": "me@myco.com", "events": [
        _event("Later", _soon(20), [{"email": "b@x.com", "displayName": "B"}]),
        _event("Sooner", _soon(2), [{"email": "a@x.com", "displayName": "A"}])]}}
    _, meetings = mp.build_context(inputs)
    assert [m["title"] for m in meetings] == ["Sooner", "Later"]


def test_recency_sweep_and_company_fallback_join_into_context():
    inputs = {"google": {"userEmail": "me@myco.com",
                         "events": [_event("Coffee with Taylor", _soon(), [
                             {"email": "taylor@startup.com", "displayName": "Taylor Reed"},
                             {"email": "nelson@cobalt-research.com", "displayName": "Nelson"}])]},
              "attendee_research": [
                  {"email": "taylor@startup.com", "title": "CEO", "company": "Startup Inc",
                   "relevance": [], "summary": "CEO of Startup Inc.",
                   "recent_activity": [{"when": "late July 2026", "what": "Published an agent-memory piece.",
                                        "source_url": "https://t.substack.com/p/mem"}],
                   "personal": ["Ran the SF Marathon (https://x.com/t/1)"],
                   "conversation_hooks": ["His agent-memory piece last week fits your roadmap."]},
                  {"email": "nelson@cobalt-research.com", "title": None, "company": "Cobalt Research",
                   "relevance": [], "summary": "No public profile found.",
                   "company_summary": "Cobalt Research does battery-materials analytics."}]}
    ctx, _ = mp.build_context(inputs)
    assert "recent: Published an agent-memory piece. (late July 2026) — https://t.substack.com/p/mem" in ctx
    assert "personal: Ran the SF Marathon" in ctx
    assert "hook: His agent-memory piece last week fits your roadmap." in ctx
    # Person → company degradation: no sentinel echo, company description instead of "no background".
    assert "No public profile found." not in ctx
    assert "company: Cobalt Research does battery-materials analytics." in ctx
    assert "no public profile or prior knowledge found" not in ctx


# ── Private context: thread:/text:/loop:/granola: lines (marry web research with the user's
#    own emails, texts, continuity loops, and past meetings) ────────────────────────────────────

def _taylor_inputs(**extra):
    base = {"google": {"userEmail": "me@myco.com",
                       "events": [_event("Coffee with Taylor", _soon(), [
                           {"email": "taylor@startup.com", "displayName": "Taylor Reed"}])]}}
    base.update(extra)
    return base


def test_thread_lines_from_attendee_comms_with_direction_and_cap(monkeypatch):
    monkeypatch.setattr(mp, "_active_loops", lambda: [])
    rows = [{"date": "Tue, 04 Aug", "subject": "Deck feedback",
             "snippet": "can you send the updated deck before Thursday", "from_me": False},
            {"date": "Mon, 03 Aug", "subject": "Re: Deck", "snippet": "will do", "from_me": True}]
    rows += [{"date": f"d{i}", "subject": f"s{i}", "snippet": "x", "from_me": False} for i in range(4)]
    inputs = _taylor_inputs(attendee_comms={"Taylor@Startup.com": rows})   # case-insensitive key
    ctx, _ = mp.build_context(inputs)
    assert ('thread: email Tue, 04 Aug "Deck feedback" (them→you): '
            "can you send the updated deck before Thursday") in ctx
    assert 'thread: email Mon, 03 Aug "Re: Deck" (you→them): will do' in ctx
    assert ctx.count("thread:") == 3                       # capped at 3 thread lines


def test_text_lines_matched_via_contact_index_phone_bridge(monkeypatch):
    # The attendee's email hits a contact_index entry that ALSO carries their phone — 1:1
    # iMessage/WhatsApp messages on that phone surface as text: lines (most recent first, ≤2).
    monkeypatch.setattr(mp, "_active_loops", lambda: [])
    inputs = _taylor_inputs(
        prior_knowledge={"person_knowledge": {},
                         "contact_index": [{"canonical_id": "c1", "display_name": "Taylor Reed",
                                            "identifiers": ["taylor@startup.com", "+14155551234"]}]},
        local={"imessage": [
            {"handle": "+14155551234", "text": "let's push our coffee to 3pm?",
             "timestamp": "2026-08-05T09:00:00Z", "is_from_me": False},
            {"handle": "+14155551234", "text": "sure, works", "timestamp": "2026-08-05T09:05:00Z",
             "is_from_me": True},
            {"handle": "+14155551234", "text": "old one", "timestamp": "2026-08-01T09:00:00Z",
             "is_from_me": False},
            {"handle": "+19998887777", "text": "someone else entirely",
             "timestamp": "2026-08-05T10:00:00Z", "is_from_me": False}]})
    ctx, _ = mp.build_context(inputs)
    assert "text: imessage 2026-08-05 (them→you): let's push our coffee to 3pm?" in ctx
    assert "text: imessage 2026-08-05 (you→them): sure, works" in ctx
    assert ctx.count("text:") == 2                         # capped at 2; the old one dropped
    assert "someone else entirely" not in ctx              # exact-identifier match only


def test_group_chats_and_unmatched_attendees_degrade_silently(monkeypatch):
    monkeypatch.setattr(mp, "_active_loops", lambda: [
        {"contact_identifier": "other@person.com", "contact_name": "Someone Else",
         "status": "open", "summary": "unrelated loop"}])
    inputs = _taylor_inputs(local={"imessage": [
        {"handle": "+14155551234", "text": "group noise", "timestamp": "2026-08-05T09:00:00Z",
         "is_from_me": False, "is_group_chat": True, "chat_guid": "g1"}]})
    ctx, _ = mp.build_context(inputs)
    # No comms file, no contact_index match, foreign loop, group-only texts → zero private lines.
    for marker in ("thread:", "text:", "loop:", "granola:"):
        assert marker not in ctx


def test_loop_line_from_continuity_ledger(monkeypatch):
    monkeypatch.setattr(mp, "_active_loops", lambda: [
        {"contact_identifier": "taylor@startup.com", "contact_name": "Taylor Reed",
         "status": "open", "summary": "you said you'd send the deck", "times_surfaced": 2,
         "action_type": "follow_up"},
        {"contact_identifier": "other@x.com", "status": "open", "summary": "unrelated"}])
    ctx, _ = mp.build_context(_taylor_inputs())
    assert "loop: open — you said you'd send the deck (surfaced 2x)" in ctx
    assert "unrelated" not in ctx


def test_granola_line_is_per_attendee_most_recent(monkeypatch):
    # signals.ts matchGranolaToCalendar port: THE MOST RECENT meeting with THIS person, ≤140 chars.
    monkeypatch.setattr(mp, "_active_loops", lambda: [])
    inputs = _taylor_inputs(local={"granola_meetings": [
        {"title": "Old sync", "date": "2026-06-01", "ai_summary": "Early roadmap chat.",
         "attendee_emails": ["taylor@startup.com"]},
        {"title": "Recent sync", "date": "2026-07-20", "ai_summary": "Discussed term sheets. " + "x" * 200,
         "attendee_emails": ["taylor@startup.com"]},
        {"title": "Other person", "date": "2026-08-01", "ai_summary": "Not Taylor's meeting.",
         "attendee_emails": ["other@z.com"]}]})
    ctx, _ = mp.build_context(inputs)
    assert "granola: last met 2026-07-20: Discussed term sheets." in ctx
    assert "Not Taylor's meeting" not in ctx
    granola_line = next(l for l in ctx.split("\n") if "granola:" in l)
    assert len(granola_line) < 180                          # summary capped at 140


def test_private_block_hard_cap_six_lines(monkeypatch):
    # thread(3) + loop(1) + granola(1) + text(2) would be 7 — the hard cap keeps 6, dropping the
    # lowest-signal tail (the 2nd text line), never the thread/loop/granola lines.
    monkeypatch.setattr(mp, "_active_loops", lambda: [
        {"contact_identifier": "taylor@startup.com", "status": "open", "summary": "send the deck"}])
    rows = [{"date": f"d{i}", "subject": f"s{i}", "snippet": "x", "from_me": False} for i in range(3)]
    inputs = _taylor_inputs(
        attendee_comms={"taylor@startup.com": rows},
        prior_knowledge={"person_knowledge": {},
                         "contact_index": [{"canonical_id": "c1", "display_name": "Taylor Reed",
                                            "identifiers": ["taylor@startup.com", "+14155551234"]}]},
        local={"granola_meetings": [{"title": "Sync", "date": "2026-07-20", "ai_summary": "notes",
                                     "attendee_emails": ["taylor@startup.com"]}],
               "imessage": [
                   {"handle": "+14155551234", "text": f"msg {i}",
                    "timestamp": f"2026-08-0{i+1}T09:00:00Z", "is_from_me": False}
                   for i in range(2)]})
    ctx, _ = mp.build_context(inputs)
    private = [l for l in ctx.split("\n")
               if any(m in l for m in ("thread:", "text:", "loop:", "granola:"))]
    assert len(private) == 6
    assert sum("thread:" in l for l in private) == 3
    assert any("loop:" in l for l in private) and any("granola:" in l for l in private)
    assert sum("text:" in l for l in private) == 1          # the tail got the cap


# ── Focus mode: one named person → one meeting, the deep dive ──────────────────────────────────

def _two_meetings(**extra):
    """A Spencer meeting in 2h, a Dana meeting in 6h, a second Spencer meeting in 20h."""
    base = {"google": {"userEmail": "me@myco.com", "events": [
        _event("Coffee with Spencer", _soon(2), [
            {"email": "spencer@commenda.io", "displayName": "Spencer Kim"}]),
        _event("Dana sync", _soon(6), [{"email": "dana@acme.com", "displayName": "Dana Roe"}]),
        _event("Spencer dinner", _soon(20), [
            {"email": "spencer@commenda.io", "displayName": "Spencer Kim"}])]}}
    base.update(extra)
    return base


def test_focus_by_name_selects_that_persons_soonest_meeting(monkeypatch):
    monkeypatch.setattr(mp, "_active_loops", lambda: [])
    ctx, meetings = mp.build_context(_two_meetings(focus="spencer"))
    assert [m["title"] for m in meetings] == ["Coffee with Spencer"]     # one meeting, the soonest
    assert "Dana sync" not in ctx
    assert ctx.startswith("(FOCUS: deep dive on Spencer Kim <spencer@commenda.io> — "
                          "the user named this person.")
    assert "matches 2 upcoming meetings; this is the soonest" in ctx     # ambiguity is stated
    assert mp.focus_mode(_two_meetings(focus="spencer"))


def test_focus_by_email_and_unambiguous_match_says_nothing_extra(monkeypatch):
    monkeypatch.setattr(mp, "_active_loops", lambda: [])
    ctx, meetings = mp.build_context(_two_meetings(focus="dana@acme.com"))
    assert [m["title"] for m in meetings] == ["Dana sync"]
    assert "(FOCUS: deep dive on Dana Roe <dana@acme.com>" in ctx
    assert "matches" not in ctx                                          # only one meeting matched


def test_focus_no_match_falls_back_to_the_sweep(monkeypatch):
    monkeypatch.setattr(mp, "_active_loops", lambda: [])
    inputs = _two_meetings(focus="nobody")
    ctx, meetings = mp.build_context(inputs)
    sweep_ctx, sweep_meetings = mp.build_context(_two_meetings())
    assert ctx == sweep_ctx and meetings == sweep_meetings               # byte-identical to sweep
    assert "(FOCUS:" not in ctx
    assert not mp.focus_mode(inputs)


def test_focus_ignores_people_outside_the_window(monkeypatch):
    monkeypatch.setattr(mp, "_active_loops", lambda: [])
    far = (datetime.now(timezone.utc) + timedelta(hours=200)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    inputs = {"google": {"userEmail": "me@myco.com", "events": [
        _event("Dana sync", _soon(6), [{"email": "dana@acme.com", "displayName": "Dana Roe"}]),
        _event("Spencer next week", far, [
            {"email": "spencer@commenda.io", "displayName": "Spencer Kim"}])]},
        "focus": "spencer"}
    ctx, meetings = mp.build_context(inputs)
    assert not mp.focus_mode(inputs)                                     # beyond 72h → not focusable
    assert [m["title"] for m in meetings] == ["Dana sync"] and "(FOCUS:" not in ctx


def test_company_deep_renders_only_the_fields_that_exist(monkeypatch):
    monkeypatch.setattr(mp, "_active_loops", lambda: [])
    inputs = _two_meetings(focus="spencer", attendee_research=[
        {"email": "spencer@commenda.io", "title": "CEO", "company": "Commenda",
         "relevance": [], "summary": "Founder of Commenda.",
         "company_deep": {"company": "Commenda",
                          "builds": ["Global tax filing tied to each payment",
                                     "A compliance dashboard for misclassification"],
                          "traction": ["Series A led by Ridgeline, June 2026 (https://tc.com/a)"],
                          "market": "Displaces the EOR incumbents after the 2025 reporting change."}}])
    ctx, _ = mp.build_context(inputs)
    assert "    builds: Global tax filing tied to each payment" in ctx
    assert "    builds: A compliance dashboard for misclassification" in ctx
    assert "    traction: Series A led by Ridgeline, June 2026 (https://tc.com/a)" in ctx
    assert "    market: Displaces the EOR incumbents after the 2025 reporting change." in ctx
    assert "founder:" not in ctx                                         # absent field → no line


def test_sweep_context_never_carries_company_deep_lines(monkeypatch):
    monkeypatch.setattr(mp, "_active_loops", lambda: [])
    ctx, _ = mp.build_context(_two_meetings())
    for marker in ("builds:", "founder:", "traction:", "market:", "(FOCUS:"):
        assert marker not in ctx


def test_compose_picks_the_focused_prompt_variant_only_in_focus_mode(monkeypatch):
    monkeypatch.setattr(mp, "_active_loops", lambda: [])
    seen = {}

    def fake_llm(prompt, inputs):
        seen["prompt"] = prompt
        return json.dumps({"prep_markdown": "x", "meetings": []})

    mp.compose(_two_meetings(focus="spencer"), llm=fake_llm)
    focused = seen["prompt"]
    assert "## Output — FOCUSED PREP (one person, one meeting)" in focused
    assert "Return JSON with exactly these keys" not in focused          # sweep spec replaced
    mp.compose(_two_meetings(), llm=fake_llm)
    sweep = seen["prompt"]
    assert "## Output — FOCUSED PREP" not in sweep
    assert "Return JSON with exactly these keys" in sweep


def test_prompt_variant_markers_never_reach_the_model():
    for text in (mp._load_prompt(), mp._load_prompt(focus=True)):
        for marker in (mp.SWEEP_BEGIN, mp.SWEEP_END, mp.FOCUS_BEGIN, mp.FOCUS_END):
            assert marker not in text
    # Both variants keep the shared hard rules + private-context documentation.
    for text in (mp._load_prompt(), mp._load_prompt(focus=True)):
        assert "**Never invent facts.**" in text and "`granola:`" in text


# ── Focused prompt contract: thread first, angles last, empty sections omitted ─────────────────

with open(os.path.join(ROOT, "meeting-prep", "references", "meeting-prep-prompt.md"),
          encoding="utf-8") as _f2:
    FOCUSED = _f2.read().split("<!-- FOCUSED PREP — the --focus variant -->", 1)[1]


def test_focused_prompt_mandates_thread_first_and_angles_last():
    assert "**Your thread** — ALWAYS FIRST, ALWAYS PRESENT" in FOCUSED
    assert "**Angles** — ALWAYS LAST" in FOCUSED
    assert FOCUSED.index("**Your thread**") < FOCUSED.index("**What <company> builds**")
    assert FOCUSED.index("**What <company> builds**") < FOCUSED.index("**Angles**")
    # The private half never contains web research — that is why it leads.
    assert "no web research belongs in it" in FOCUSED
    assert "the half no search can produce" in FOCUSED


def test_focused_prompt_documents_company_deep_lines_and_no_line_no_section():
    for marker in ("`builds:`", "`founder:`", "`traction:`", "`market:`"):
        assert marker in FOCUSED
    assert "No line, no section." in FOCUSED
    assert "OMIT any section with nothing" in FOCUSED
    assert "a casual coffee does not get a forced market thesis" in FOCUSED   # scale to the meeting


def test_focused_prompt_angle_fusion_rule():
    assert "Every angle fuses a private fact with a researched fact where both exist" in FOCUSED
    assert "never fabricate the missing half" in FOCUSED
    assert "never restate a fact as an angle" in FOCUSED
    assert "never ask a generic process question" in FOCUSED
    assert "`meetings[0].talking_points`" in FOCUSED


def test_focused_prompt_keeps_the_identity_guards_and_the_focus_line():
    assert "Every **Hard rule** above still holds here" in FOCUSED
    assert "Depth is not licence to guess." in FOCUSED
    assert "(FOCUS: …)" in FOCUSED                                # the composer's focus line
    assert "say in one line which one you are prepping" in FOCUSED   # ambiguity surfaces to the user


def test_focused_example_is_fictional_and_obeys_the_voice_rules():
    example = FOCUSED.split("### Example — focused prep", 1)[1]
    assert "fictional people and company" in example
    assert "Larkfield" in example and "Priya Raman" in example
    body = example.split("```")[1]
    for section in ("**Your thread**", "**What Larkfield builds**", "**The founder**",
                    "**Traction & signals**", "**The space & why now**", "**Angles**"):
        assert section in body
    assert body.index("**Your thread**") < body.index("**Angles**")
    from brief_validate import BANNED_PHRASES        # the ONE list — never a local copy
    for banned in BANNED_PHRASES:
        assert banned not in body.lower()
    for line in body.splitlines():
        assert line.count("—") <= 1, line                      # voice: ≤1 em dash per line


# ── Prompt contract: private context is REQUIRED-when-present, forbidden otherwise ─────────────

PROMPT_PATH = os.path.join(ROOT, "meeting-prep", "references", "meeting-prep-prompt.md")
with open(PROMPT_PATH, encoding="utf-8") as _f:
    PROMPT = _f.read()


def test_prompt_documents_private_context_lines_as_authoritative():
    for marker in ("`thread:`", "`text:`", "`loop:`", "`granola:`"):
        assert marker in PROMPT, f"prompt no longer documents {marker} context lines"
    assert "pre-computed" in PROMPT and "authoritative" in PROMPT
    assert "do not re-verify, second-guess, or re-derive" in PROMPT


def test_prompt_requires_your_thread_and_recently_when_present():
    assert "`Your thread:`" in PROMPT
    assert "REQUIRED when the attendee has any `thread:`/`text:`/`loop:` line" in PROMPT
    assert "FORBIDDEN when they don't" in PROMPT            # no fabricated stand-ins
    assert "`Recently:`" in PROMPT
    assert "REQUIRED when the attendee has a `recent:` line" in PROMPT
    assert "citing its date" in PROMPT


def test_prompt_thread_direction_and_grounding_rules():
    # gemini-flex.ts threadSnippet rule: never present the user's own outbound as needing response.
    assert "NEVER present the user's own outbound words" in PROMPT
    # Talking points grounded in research ∪ thread ∪ graph; thread context must be built on.
    assert "research ∪ thread ∪ graph" in PROMPT
    assert "at least one talking point must build on it" in PROMPT


def test_prompt_anti_fabrication_rules_intact():
    assert "**Never invent facts.**" in PROMPT
    assert "Never assert a title/role without a source." in PROMPT
    assert "EMPTY `talking_points`" in PROMPT
    assert "NEVER write generic process points" in PROMPT
    assert "No background found — worth a quick intro question" in PROMPT


def test_filler_talking_points_stripped_and_empty_list_respected(tmp_path):
    # The LLM back-filled generic process points (the exact ones from a real brief) — the
    # deterministic post-filter must strip them, leaving an honest EMPTY list, and a meeting
    # the model already left empty stays empty (no back-fill from the deterministic skeleton).
    stub = tmp_path / "resp.json"
    stub.write_text(json.dumps({
        "prep_markdown": "**Lunch** — today\n- Nelson, Cobalt Research",
        "meetings": [
            {"event_id": "lunch", "title": "Lunch", "start": "x", "attendees": [],
             "talking_points": ["Ask for introductions and background on melius.com",
                                "Understand the agenda for the lunch/meeting",
                                "Understand what prompted the meeting",
                                "Ask about her Devcon talk on eval harnesses"]},
            {"event_id": "intro", "title": "Intro", "start": "x", "attendees": [],
             "talking_points": []}]}))
    os.environ["SOTTO_LLM_STUB"] = str(stub)
    try:
        inputs = {"google": {"userEmail": "me@myco.com",
                             "events": [_event("Lunch", _soon(), [
                                 {"email": "nelson@cobalt-research.com", "displayName": "Nelson"}])]}}
        out = mp.compose(inputs)
        assert out["meetings"][0]["talking_points"] == ["Ask about her Devcon talk on eval harnesses"]
        assert out["meetings"][1]["talking_points"] == []       # empty stays empty
    finally:
        del os.environ["SOTTO_LLM_STUB"]
