"""compose_followup.py — recent-ended filter + stubbed extraction contract."""
import importlib.util, json, os, sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
spec = importlib.util.spec_from_file_location("cf", os.path.join(ROOT, "followup", "scripts", "compose_followup.py"))
cf = importlib.util.module_from_spec(spec); spec.loader.exec_module(cf)
SKILL = open(os.path.join(ROOT, "followup", "SKILL.md"), encoding="utf-8").read()


def _iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_granola_backlog_route_does_not_reuse_the_36_hour_followup_window():
    assert "--days 14 --transcripts-since-hours 0" in SKILL
    assert "--since-hours 336 --reconcile-open-loops" in SKILL
    assert "answer\n   “what is open” from `open_loops`" in SKILL


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


def test_explicit_future_end_keeps_an_in_progress_meeting_out():
    now = datetime.now(timezone.utc)
    meeting = {"title": "Still running", "start": _iso(1), "end": _iso(-1),
               "ai_summary": "draft live notes"}
    assert cf._recent_ended([meeting], 36, now) == []


def test_context_layers_notes_summary_then_transcript():
    # Structure over volume: the transcript used to REPLACE notes+summary, drowning the two most
    # distilled signals (the user's own notes; Granola's summary, which already lists action items)
    # exactly when a transcript existed. All three layers render, most-distilled first, under the
    # exact markers the prompt's "distilled first, verified always" rule names.
    meetings = [{"title": "Acme Sync", "date": _iso(2),
                 "your_notes": "push on pricing",
                 "ai_summary": "Action items: send deck to Sarah.",
                 "transcript": "hello " * 10 + "I'll send the deck tomorrow"}]
    ctx, ended = cf.build_context({"granola": meetings, "local": {}}, 36)
    assert len(ended) == 1
    ni, si, ti = ctx.index("[your notes]:"), ctx.index("[summary]:"), ctx.index("[transcript]:")
    assert ni < si < ti                                   # distilled first
    assert "push on pricing" in ctx and "send deck to Sarah" in ctx
    assert "I'll send the deck tomorrow" in ctx           # transcript still present, last
    # A long transcript keeps its TAIL — commitments cluster at the end.
    meetings[0]["transcript"] = ("early filler. " * 20000) + "THE ENDING COMMITMENT"
    ctx, _ = cf.build_context({"granola": meetings, "local": {}}, 36)
    assert "THE ENDING COMMITMENT" in ctx
    assert len(ctx) < cf.TRANSCRIPT_CHAR_CAP + 5000       # capped, not the whole 260K chars


def test_compose_no_meetings_short_circuits():
    out = cf.compose({"granola": [], "local": {}, "google": {"events": []}}, since_hours=36)
    assert out["commitments"] == [] and out["drafts"] == []
    assert cf._normalize({})["procedural_candidates"] == []   # the key always exists downstream
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
                           "commitments": [{"meeting_id": "m-sync", "owner": "you",
                                            "owner_is_user": True, "what": "send deck",
                                            "source_snippet": "you: I'll send the deck"}],
                           "drafts": []})
    inputs = {"granola": [{"meeting_id": "m-sync", "title": "Sync", "date": _iso(2),
                            "transcript": "you: I'll send the deck"}],
              "local": {}, "google": {"events": []}}
    out = cf.compose_for_brief(inputs, since_hours=12, llm=fake_llm)
    assert out["followup_markdown"].startswith("*Sync*")   # chat-formatted (to_chat) on the way out
    assert out["commitments"][0]["what"] == "send deck"


def test_compose_with_injected_llm():
    captured = {}

    def fake_llm(prompt, inputs):
        captured["prompt"] = prompt
        return json.dumps({"followup_markdown": "**Sync** — yesterday\nYou committed to send the deck.",
                           "commitments": [{"meeting": "Sync", "meeting_id": "m-sync",
                                            "owner": "you", "owner_is_user": True,
                                            "what": "send deck", "due": None,
                                            "source_snippet": "you: I'll send the deck"}],
                           "drafts": [{"to_name": "Dana", "to_email": "dana@acme.com", "channel": "email",
                                       "subject": "The deck", "body": "Here's the deck I mentioned."}]})
    inputs = {"granola": [{"meeting_id": "m-sync", "title": "Sync", "date": _iso(18),
                            "transcript": "you: I'll send the deck",
                           "attendee_emails": ["dana@acme.com"]}],
              "local": {}, "google": {"events": [], "userEmail": "me@x.com"}, "user_email": "me@x.com"}
    out = cf.compose(inputs, since_hours=36, llm=fake_llm)
    assert out["drafts"][0]["to_email"] == "dana@acme.com"
    assert out["commitments"][0]["what"] == "send deck"
    assert "Sync" in captured["prompt"] and "deck" in captured["prompt"]   # transcript reached the prompt


def test_apply_ledger_persists_on_demand_commitments(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    sources = [
        {"meeting_id": "m-user", "transcript": "you: I'll send Dana the deck"},
        {"meeting_id": "m-dana", "transcript": "Dana: I'll return the pricing model"},
    ]
    out = {"commitments": [
        {"meeting": "Sync", "meeting_id": "m-user", "owner": "you", "owner_is_user": True,
         "what": "Send Dana the deck", "source_snippet": "you: I'll send Dana the deck"},
        {"meeting": "Sync", "meeting_id": "m-dana", "owner": "Dana", "owner_is_user": False,
         "what": "Return the pricing model", "source_snippet": "Dana: I'll return the pricing model"},
    ]}
    receipt = cf._apply_ledger(out, "me@example.com", sources)
    assert receipt["written"] == 2 and receipt["deduped"] == 0
    continuity = tmp_path / "knowledge" / "continuity"
    frontmatters = [p.read_text().split("---", 2)[1] for p in continuity.glob("*.md")]
    assert any("action_type: follow_up" in fm for fm in frontmatters)
    assert any("action_type: waiting_on" in fm for fm in frontmatters)
    assert all("source: followup_commitment" in fm for fm in frontmatters)


def test_cli_applies_commitments_before_it_prints_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    granola = tmp_path / "granola.json"
    granola.write_text(json.dumps({"meetings": [{
        "meeting_id": "m-sync", "title": "Sync", "date": _iso(2),
        "transcript": "you: I'll send the deck",
    }]}))
    monkeypatch.setattr(cf, "compose", lambda inputs, since_hours: {
        "followup_markdown": "*Sync* — send the deck.",
        "commitments": [{"meeting": "Sync", "meeting_id": "m-sync", "owner": "you",
                         "owner_is_user": True, "what": "Send the deck",
                         "source_snippet": "you: I'll send the deck"}],
        "drafts": [],
    })
    monkeypatch.setattr(sys, "argv", ["compose_followup.py", "--granola", str(granola),
                                      "--user-email", "me@example.com",
                                      "--reconcile-open-loops"])
    cf.main()
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["ledger"]["written"] == 1
    assert emitted["open_loops"]["counts"] == {"you_owe": 1, "waiting_on_them": 0}
    assert emitted["open_loops"]["you_owe"][0]["what"].startswith("You committed to: Send the deck")
    assert list((tmp_path / "knowledge" / "continuity").glob("*.md"))


def test_compose_rejects_fabricated_quote_wrong_direction_and_wrong_object():
    meeting = {"meeting_id": "m-sync", "title": "Sync", "date": _iso(2),
               "transcript": ("Dana: I'll send the deck tomorrow.\n"
                              "Dana said Nikunj will send the model.\n"
                              "Dana: I'll review the memo.\n"
                              "Dana: I will not send the report.")}

    def fake_llm(prompt, inputs):
        return json.dumps({"followup_markdown": "Claims", "drafts": [], "commitments": [
            {"meeting_id": "m-sync", "owner": "Dana", "owner_is_user": False,
             "what": "send deck", "source_snippet": "Dana: I'll send the deck tomorrow."},
            {"meeting_id": "m-sync", "owner": "you", "owner_is_user": True,
             "what": "send deck", "source_snippet": "Dana: I'll send the deck tomorrow."},
            {"meeting_id": "m-sync", "owner": "Dana", "owner_is_user": False,
             "what": "send contract", "source_snippet": "Dana: I'll send the deck tomorrow."},
            {"meeting_id": "m-sync", "owner": "Dana", "owner_is_user": False,
             "what": "send deck", "source_snippet": "Dana: I'll wire the money tomorrow."},
            {"meeting_id": "m-sync", "owner": "Dana", "owner_is_user": False,
             "what": "send model", "source_snippet": "Dana said Nikunj will send the model."},
            {"meeting_id": "m-sync", "owner": "Dana", "owner_is_user": False,
             "what": "send memo", "source_snippet": "Dana: I'll review the memo."},
            {"meeting_id": "m-sync", "owner": "Dana", "owner_is_user": False,
             "what": "send report", "source_snippet": "Dana: I will not send the report."},
        ]})

    out = cf.compose({"granola": [meeting], "local": {}, "google": {"events": []}}, llm=fake_llm)
    assert [c["what"] for c in out["commitments"]] == ["send deck"]
    assert out["commitment_grounding"] == {
        "accepted": 1, "rejected": 6,
        "reasons": {"meeting": 0, "snippet": 1, "owner": 3, "deliverable": 2},
    }


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
