"""render_chat_text — brief_markdown → the chat-deliverable text.

Regression for the delivered-brief formatting bug: raw <!--id:…|ch:…--> and <!--meeting:…-->
markers showed up verbatim in WhatsApp, and CommonMark (## headings, **bold**) rendered as literal
clutter. Delivery used to rely on the AGENT sed-ing markers out; it skipped the instruction, so the
conversion is now deterministic in compose_brief and exposed as `brief_text`.
"""
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))

import compose_brief as cb  # noqa: E402

# The exact failure SHAPE from a real delivered evening brief (names/emails synthetic — the
# SECRETS publish guard rightly blocks real addresses in shipped files).
SAMPLE = """## Needs Attention Now

**Sarah Chen**<!--id:sarah@acme.com|ch:email--> - Locked in a 15-20 minute session for Monday at 11 AM.

**Team Standup**<!--id:90271639461897@lid|ch:whatsapp--> - Coordination is still open for a call this weekend.

## Coming Up

- **9:30 AM** — Morning Team Video Sync

<!--meeting:event_id:b4mocrfgt5pci6dfkf6pu3pr6s_20260713T163000Z|title:Morning Team Video Sync|start:2026-07-13T09:30:00-07:00|attendees:alice@example.com;bob@example.com-->
<!--meeting:event_id:7oj82qfihkqhq4a0solnlaniri|title:Coffee with Taylor|start:2026-07-13T10:00:00-07:00|attendees:taylor@example.com-->

## ✅ Already Handled

**Sarah Chen**<!--id:sarah@acme.com|ch:email--> - Confirmed the Monday sync.

---

## Filtered

7 promotional emails, 10 automated notifications
"""


def test_markers_are_fully_stripped():
    out = cb.render_chat_text(SAMPLE)
    assert "<!--" not in out and "-->" not in out
    assert "@lid" not in out                                   # id markers gone, incl. the lid one
    assert "meeting:event_id" not in out                       # meeting markers gone entirely
    assert "Sarah Chen" in out                                 # the names themselves survive


def test_headings_and_bold_become_whatsapp_syntax():
    out = cb.render_chat_text(SAMPLE)
    assert "## " not in out and "**" not in out
    assert "*Needs Attention Now*" in out                      # heading → *bold* line
    assert "*✅ Already Handled*" in out
    assert "*Sarah Chen* - Locked in" in out                   # **name** → *name*
    assert "*9:30 AM* — Morning Team Video Sync" in out


def test_rules_dropped_and_blank_runs_collapsed():
    out = cb.render_chat_text(SAMPLE)
    assert "---" not in out
    assert "\n\n\n" not in out                                 # marker lines don't leave gaps
    assert not out.startswith("\n") and not out.endswith("\n")


def test_multiline_marker_and_empty_input():
    assert cb.render_chat_text("a <!--id:x\n|ch:email--> b") == "a  b".replace("  ", " ") or \
        cb.render_chat_text("a <!--id:x\n|ch:email--> b") == "a  b"   # marker spanning lines still dies
    assert "<!--" not in cb.render_chat_text("a <!--id:x\n|ch:email--> b")
    assert cb.render_chat_text("") == ""
    assert cb.render_chat_text(None) == ""


def test_compose_output_carries_brief_text(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    stub = tmp_path / "stub.json"
    stub.write_text(json.dumps({
        "markdown": "## Needs Attention Now\n\n**Sarah Chen**<!--id:sarah@acme.com|ch:email--> - ping.",
        "actionItems": [], "extractedKnowledge": {"person_updates": [], "company_updates": []},
    }))
    monkeypatch.setenv("SOTTO_LLM_STUB", str(stub))
    out = cb.compose({"type": "morning", "google": {"emails": [], "events": []}, "local": {}})
    assert out["brief_text"] == "*Needs Attention Now*\n\n*Sarah Chen* - ping."
    assert "<!--id:" in out["brief_markdown"]                  # markdown keeps the markers for records
