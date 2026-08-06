"""compose_meeting_prep.py — the standalone meeting-prep skill: external-attendee selection,
research/knowledge/granola join, and the single-message render. Offline (stubbed Gemini)."""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))

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
