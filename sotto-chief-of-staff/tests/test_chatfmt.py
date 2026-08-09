"""chatfmt.to_chat — the ONE shared markdown→chat transformation (Sprint 0 §3).

Replicates compose_brief.render_chat_text's behavior for every non-brief surface (pulse,
meeting-prep, followup): marker strip, ## → *bold*, ** → *, hrule drop, blank-run collapse —
and, critically, IDEMPOTENCE: routing already-converted text through again must not mangle it.
"""
import importlib.util
import os

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location(
    "chatfmt", os.path.join(ROOT, "_shared", "lib", "chatfmt.py"))
cf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cf)

SAMPLE = """## Needs Attention Now

**Sarah Chen**<!--id:sarah@acme.com|ch:email--> - Locked in Monday at 11 AM.

## Coming Up

- **9:30 AM** — Morning Team Video Sync

<!--meeting:event_id:abc123|title:Morning Team Video Sync|start:2026-07-13T09:30:00-07:00-->

---

Reply here: sms:+14155551234
More: https://example.com/thread/42
"""


def test_headings_become_bold_lines():
    out = cf.to_chat(SAMPLE)
    assert "## " not in out
    assert "*Needs Attention Now*" in out
    assert "*Coming Up*" in out


def test_double_asterisk_bold_becomes_single():
    out = cf.to_chat(SAMPLE)
    assert "**" not in out
    assert "*Sarah Chen* - Locked in" in out
    assert "- *9:30 AM* — Morning Team Video Sync" in out


def test_markers_stripped_and_rules_dropped():
    out = cf.to_chat(SAMPLE)
    assert "<!--" not in out and "-->" not in out
    assert "meeting:event_id" not in out
    assert "---" not in out
    assert "\n\n\n" not in out                                     # marker lines leave no gaps
    assert not out.startswith("\n") and not out.endswith("\n")


def test_deep_link_lines_survive_verbatim():
    out = cf.to_chat(SAMPLE)
    assert "sms:+14155551234" in out
    assert "https://example.com/thread/42" in out


def test_idempotent_running_twice_changes_nothing():
    once = cf.to_chat(SAMPLE)
    assert cf.to_chat(once) == once
    # already-chat-formatted producer output (single-asterisk headers/bold) also passes untouched
    chat = "*Waiting on you*\n- *Sarah Chen (Acme)* — waiting 5 days for reply\nsms:+14155551234"
    assert cf.to_chat(chat) == chat


def test_multiline_marker_and_degenerate_inputs():
    assert "<!--" not in cf.to_chat("a <!--id:x\n|ch:email--> b")   # marker spanning lines dies
    assert cf.to_chat("") == ""
    assert cf.to_chat(None) == ""
    assert cf.to_chat(42) == "42"


def test_behavior_matches_render_chat_text_contract():
    # The exact failure shape render_chat_text was built for (test_render_chat_text.py's sample):
    # a bolded name with an inline id marker must come out as a clean single-asterisk name.
    src = "**Sarah Chen**<!--id:sarah@acme.com|ch:email--> - ping."
    assert cf.to_chat(src) == "*Sarah Chen* - ping."
