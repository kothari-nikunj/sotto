"""compose_followup.py — recent-ended filter + stubbed extraction contract."""
import importlib.util, json, os, sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
spec = importlib.util.spec_from_file_location("cf", os.path.join(ROOT, "followup", "scripts", "compose_followup.py"))
cf = importlib.util.module_from_spec(spec); spec.loader.exec_module(cf)


def _iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_recent_ended_filters_window_and_requires_body():
    now = datetime.now(timezone.utc)
    meetings = [
        {"title": "Yesterday", "date": _iso(20), "transcript": "we agreed X"},     # in window + transcript
        {"title": "Last week", "date": _iso(200), "transcript": "old"},            # too old
        {"title": "No notes", "date": _iso(5)},                                    # no transcript/notes
        {"title": "Future", "date": _iso(-5), "transcript": "later"},              # future
        {"title": "Notes only", "date": _iso(10), "ai_summary": "discussed Y"},    # notes ok
    ]
    ended = cf._recent_ended(meetings, 36, now)
    titles = {m["title"] for m in ended}
    assert titles == {"Yesterday", "Notes only"}


def test_compose_no_meetings_short_circuits():
    out = cf.compose({"granola": [], "local": {}, "google": {"events": []}}, since_hours=36)
    assert out["commitments"] == [] and out["drafts"] == []
    assert "Nothing to follow up" in out["followup_markdown"]


def test_compose_for_brief_empty_when_nothing_ended():
    # the evening-merge entry point returns {} (NOT the human-facing "Nothing to follow up" line)
    # and never touches the llm when no recently-ended meeting has notes
    def exploding_llm(prompt, inputs):
        raise AssertionError("llm must not be called")
    assert cf.compose_for_brief({"granola": [], "local": {}, "google": {"events": []}},
                                llm=exploding_llm) == {}
    # old meetings outside the window also short-circuit
    old = {"granola": [{"title": "Old", "date": _iso(30), "transcript": "t"}],
           "local": {}, "google": {"events": []}}
    assert cf.compose_for_brief(old, since_hours=12, llm=exploding_llm) == {}


def test_compose_for_brief_composes_when_meeting_ended():
    def fake_llm(prompt, inputs):
        assert "Sync" in prompt
        return json.dumps({"followup_markdown": "**Sync** — send the deck.",
                           "commitments": [{"owner": "you", "what": "send deck"}], "drafts": []})
    inputs = {"granola": [{"title": "Sync", "date": _iso(2), "transcript": "you: I'll send the deck"}],
              "local": {}, "google": {"events": []}}
    out = cf.compose_for_brief(inputs, since_hours=12, llm=fake_llm)
    assert out["followup_markdown"].startswith("*Sync*")   # chat-formatted (to_chat) on the way out
    assert out["commitments"][0]["what"] == "send deck"


def test_compose_with_injected_llm():
    captured = {}

    def fake_llm(prompt, inputs):
        captured["prompt"] = prompt
        return json.dumps({"followup_markdown": "**Sync** — yesterday\nYou committed to send the deck.",
                           "commitments": [{"meeting": "Sync", "owner": "you", "what": "send deck", "due": None}],
                           "drafts": [{"to_name": "Dana", "to_email": "dana@acme.com", "channel": "email",
                                       "subject": "The deck", "body": "Here's the deck I mentioned."}]})
    inputs = {"granola": [{"title": "Sync", "date": _iso(18), "transcript": "you: I'll send the deck",
                           "attendee_emails": ["dana@acme.com"]}],
              "local": {}, "google": {"events": [], "userEmail": "me@x.com"}, "user_email": "me@x.com"}
    out = cf.compose(inputs, since_hours=36, llm=fake_llm)
    assert out["drafts"][0]["to_email"] == "dana@acme.com"
    assert out["commitments"][0]["what"] == "send deck"
    assert "Sync" in captured["prompt"] and "deck" in captured["prompt"]   # transcript reached the prompt

def test_followup_markdown_is_chat_ready():
    # SKILL.md delivers followup_markdown verbatim on chat channels — the raw-markdown extraction
    # output must pass through chatfmt.to_chat (markers stripped, **bold** → *bold*).
    def fake_llm(prompt, inputs):
        return json.dumps({"followup_markdown":
                           "<!--id:1-->## Follow-Ups\n**Sync** — you committed to send the deck.",
                           "commitments": [], "drafts": []})
    inputs = {"granola": [{"title": "Sync", "date": _iso(18), "transcript": "you: I'll send the deck"}],
              "local": {}, "google": {"events": []}}
    out = cf.compose(inputs, since_hours=36, llm=fake_llm)
    md = out["followup_markdown"]
    assert "<!--" not in md and "**" not in md and "##" not in md
    assert "*Follow-Ups*" in md and "*Sync*" in md
    # Idempotent: the evening merge may re-chat-format the whole brief downstream.
    from chatfmt import to_chat
    assert to_chat(md) == md
