"""Prompt ↔ renderer contract for the FLEX extraction prompt (Sprint 1 CI guard).

Guards from the prompt-overhaul review (docs/plans/roadmap-v2-the-editor.md, Sprint 1):
(a) every data-section header the extraction prompt NAMES must match a header some formatter
    actually emits — the phantom-section bug ("Open Commitments for Key People" vs the real
    "Commitment History for Key People (historical context only)"; "TRACKED OPEN LOOPS", which
    no formatter ever emitted);
(b) the gold-standard example obeys its own rules: contains a Coming Up section, uses zero
    banned phrases, and stays within the em-dash budget (≤2 in the whole narrative example);
(c) the <!-- SYSTEM/USER SPLIT --> seam exists exactly once (compose_brief.py splits the
    template on it: above → Gemini systemInstruction, below → the user turn);
(d) every HTML comment in the prompt is one of: a maintainer comment (stripped at load), the
    split seam, or model-facing tap-marker syntax (<!--id:...--> / <!--meeting:...-->) — so the
    loader's "strip maintainer comments" rule can never eat output-format instructions.

Deliberately file-based (regex over the .md and over render_local.py's source string literals),
not import-based: compose_brief.py's loader is changing in a parallel PR, and this contract is
about the artifacts themselves.
"""
import os
import re

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
PROMPT_PATH = os.path.join(ROOT, "morning-brief", "references", "extraction-prompt.md")
RENDER_PATH = os.path.join(ROOT, "_shared", "lib", "render_local.py")

with open(PROMPT_PATH, encoding="utf-8") as f:
    PROMPT = f.read()
with open(RENDER_PATH, encoding="utf-8") as f:
    RENDER_SRC = f.read()

SEAM = "<!-- SYSTEM/USER SPLIT -->"

# The single banned-phrase list (mirrors Voice & Tone in the prompt / _shared/references/voice.md).
BANNED_PHRASES = [
    "reached out",
    "following up",
    "has been reaching out regarding",
    "multiple emails received",
    "require your immediate attention",
    "needs a confirmation",
    "high-priority tracked open loop",
    "waiting for your response",
]

# Section names the SYSTEM INSTRUCTION quotes when pointing the model at data sections.
# Curated: each must be a substring of a header some formatter emits (or a literal header of the
# user-turn template itself).
PROMPT_REFERENCED_SECTIONS = [
    "Open Commitments (ACTION LEDGER from previous briefs)",
    "Commitment History for Key People (historical context only)",
    "Stale Outbound Threads (PRE-COMPUTED from Gmail — trust these signals)",
    "What You Know About Today's People",
    "Cross-Source Index",
    "Birthdays",
    "Evening Accountability",
]


def _emitted_headers():
    """Headers the _format_* renderers emit, extracted from render_local.py's string literals
    (they all appear as "## …\\n" / "### …\\n" literals), plus the literal ## / ### headers of the
    user-turn template below the seam."""
    headers = re.findall(r'"(#{2,3} [^"]+?)\\n', RENDER_SRC)
    user_turn = PROMPT.split(SEAM, 1)[1]
    headers += re.findall(r"^#{2,3} .+$", user_turn, re.M)
    return [h.strip() for h in headers]


def test_prompt_referenced_sections_are_really_emitted():
    emitted = _emitted_headers()
    assert emitted, "no headers extracted from render_local.py — extraction regex broke"
    for name in PROMPT_REFERENCED_SECTIONS:
        assert name in PROMPT, f"curated section name no longer referenced by the prompt: {name!r}"
        assert any(name in h for h in emitted), (
            f"prompt references section {name!r} but no formatter/user-turn header contains it.\n"
            f"Emitted headers:\n" + "\n".join(emitted))


def test_phantom_section_names_are_gone():
    # The two documented phantoms must never come back.
    assert "TRACKED OPEN LOOPS" not in PROMPT
    assert "Open Commitments for Key People" not in PROMPT
    # ...and render_local no longer instructs against a section that doesn't exist.
    assert "TRACKED OPEN LOOPS" not in RENDER_SRC


def _example_region():
    """The gold-standard example's narrative ("markdown" field) block."""
    start = PROMPT.index('### Example "markdown" field:')
    end = PROMPT.index("### Worked NEGATIVE example")
    return PROMPT[start:end]


def _example_output_section():
    start = PROMPT.index("## Example Output")
    end = PROMPT.index("## Validation Checklist")
    return PROMPT[start:end]


def test_gold_example_contains_coming_up():
    assert "**Coming Up**" in _example_region(), (
        "the gold example must include a Coming Up section (the example day has meetings)")


def test_gold_example_has_no_banned_phrases():
    section = _example_output_section().lower()
    hits = [p for p in BANNED_PHRASES if p in section]
    assert not hits, f"gold example uses banned phrase(s): {hits}"


def test_gold_example_within_em_dash_budget():
    count = _example_region().count("—")
    assert count <= 2, f"gold example narrative uses {count} em dashes (budget: 2)"


def test_gold_example_bans_unsupported_inference_phrases():
    section = _example_output_section().lower()
    for phrase in ("might be urgent", "that's not like him", "that's not like her"):
        assert phrase not in section, f"gold example manufactures urgency: {phrase!r}"


def test_split_seam_exists_exactly_once():
    assert PROMPT.count(SEAM) == 1, (
        f"expected exactly one {SEAM!r} seam (compose_brief splits on it), "
        f"found {PROMPT.count(SEAM)}")


def test_every_html_comment_is_classified():
    """The loader strips ONLY maintainer comments. Everything else in <!-- --> must be the seam
    or model-facing tap-marker syntax — never prose the model needs that a stripper could eat."""
    for comment in re.findall(r"<!--.*?-->", PROMPT, re.S):
        ok = (comment == SEAM
              or comment.startswith("<!-- MAINTAINER:")
              or comment.startswith("<!--id:")
              or comment.startswith("<!--meeting:"))
        assert ok, f"unclassified HTML comment in extraction-prompt.md: {comment[:80]!r}"


def test_banned_list_stated_once_in_system_instruction():
    """One canonical list (Voice & Tone); the checklist references it instead of duplicating."""
    system = PROMPT.split(SEAM, 1)[0]
    assert system.count('"high-priority tracked open loop"') == 1
