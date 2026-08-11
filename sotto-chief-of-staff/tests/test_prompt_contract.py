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
    loader's "strip maintainer comments" rule can never eat output-format instructions;
(e) the artifacts Step 2.7 added stay intact: Commitment Detection's three blocks with the
    delegation block's strict bar (ROADMAP 2.7.4), and the decline register that voice.md states
    once and the no-draft skills only point at.

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

# The single banned-phrase list, imported — never restated. voice.md states it for humans and the
# prompts; brief_validate.BANNED_PHRASES is its one code form, and this contract test checks the
# prompt's gold example against that same tuple (a copy here is how the lists drifted before).
from brief_validate import BANNED_PHRASES  # noqa: E402  (conftest puts _shared/lib on sys.path)

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


def _quoted_banned_list(text: str, marker: str) -> list:
    """The phrases quoted on the ONE line that follows `marker` — voice.md and the prompt both state
    the list as a single sentence of double-quoted phrases."""
    line = text[text.index(marker):].split("\n", 1)[0]
    return [p.lower() for p in re.findall(r'"([^"]+)"', line)]


def test_banned_phrase_lists_agree_everywhere():
    """voice.md states the list, the prompt distills it, brief_validate enforces it — one list.
    A phrase added in voice.md and forgotten in code is exactly the drift this catches."""
    with open(os.path.join(ROOT, "_shared", "references", "voice.md"), encoding="utf-8") as f:
        voice = f.read()
    canonical = _quoted_banned_list(voice, "**Banned phrases (the single list)")
    assert canonical == list(BANNED_PHRASES), (
        "voice.md and brief_validate.BANNED_PHRASES disagree — voice.md is canonical, "
        "brief_validate is its one code form")
    assert _quoted_banned_list(PROMPT, "**Banned phrases (the single list") == canonical, (
        "extraction-prompt.md's distilled Voice & Tone list drifted from voice.md")
    # The two secondary prompts are static templates (no runtime file load), so they carry the list
    # in full rather than pointing at it — same eight, same order, no third variant.
    for rel in (("followup", "references", "followup-prompt.md"),
                ("meeting-prep", "references", "meeting-prep-prompt.md")):
        with open(os.path.join(ROOT, *rel), encoding="utf-8") as f:
            assert _quoted_banned_list(f.read(), "**Banned phrases (the single list") == canonical, rel


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


# ── Commitment Detection: three blocks, and the delegation block's strictness (ROADMAP 2.7.4) ────
# "Can you handle the vendor call" must become a `waiting_on` THEY own — but the ledger's whole
# credibility is that everything in it is real, so the block carries the strictest bar in the prompt
# until the labeled corpus lets us tune it.

def _commitment_section():
    start = PROMPT.index("## Commitment Detection")
    end = PROMPT.index("## Knowledge Extraction")
    return PROMPT[start:end]


def test_commitment_detection_has_three_blocks():
    section = _commitment_section()
    for block in ('**Outgoing (user promised something) → "follow_up":**',
                  '**Inbound (someone promised user) → "waiting_on":**',
                  '**User delegated (the user ASKED someone else to do something) → "waiting_on"'):
        assert block in section, f"Commitment Detection lost a block: {block!r}"
    # the enumerating line must count the blocks that actually exist
    assert "For all three:" in section
    assert "For both:" not in section, "the two-block wording survived a third block being added"


def test_delegation_block_is_strict():
    """Confidence floor, required evidence, and the named exclusions — the false-positive guards."""
    start = _commitment_section().index("**User delegated")
    block = _commitment_section()[start:]
    block = block[:block.index("\nFor all three:")]
    assert "confidence ≥ 0.8" in block, "delegation block lost its 0.8 confidence floor"
    assert "**evidence: REQUIRED**" in block, "delegation must require evidence, same as Inbound"
    assert "**NOT a delegation**" in block, "delegation block lost its exclusion list"
    for excluded in ("can you believe", "😂", "quoted"):
        assert excluded in block, f"delegation exclusions no longer name {excluded!r}"
    assert "ROADMAP 2.7.4" in block, "the threshold's post-corpus tuning note went missing"


def test_evidence_required_in_both_waiting_on_blocks():
    # Inbound and delegated both mint a `waiting_on`; neither may do it without evidence.
    assert _commitment_section().count("**evidence: REQUIRED**") == 2


# ── The decline register lives in voice.md, and only there ───────────────────────────────────────

def _voice():
    with open(os.path.join(ROOT, "_shared", "references", "voice.md"), encoding="utf-8") as f:
        return f.read()


def _decline_register():
    voice = _voice()
    start = voice.index("**The decline register")
    end = voice.index("**Banned phrases (the single list)")
    return voice[start:end]


def test_voice_has_a_decline_register():
    """The no-draft's voice rules are stated ONCE, here — the skills reference this file."""
    reg = _decline_register()
    assert "≤2 sentences" in reg
    assert "slammed right now" in reg, "the fake-busyness ban must name its own worked example"
    assert reg.count("✅") == 2 and reg.count("❌") == 1, (
        "the decline register needs 2 gold examples and 1 anti-example")


def test_no_draft_skills_point_at_the_register_instead_of_restating_it():
    """draft-reply, triage, and event-triage all cite voice.md — none of them re-states the rules."""
    for rel in (("draft-reply", "SKILL.md"), ("triage", "SKILL.md"), ("event-triage", "SKILL.md")):
        with open(os.path.join(ROOT, *rel), encoding="utf-8") as f:
            text = f.read()
        assert "decline register" in text, f"{rel} does not invoke the decline register"
        assert "_shared/references/voice.md" in text, f"{rel} does not cite the voice source"
        assert 'action_type: "decline"' in text, (
            f"{rel} must type a no so approval-tiers' never-relax guard can key on it")


def test_banned_list_stated_once_in_system_instruction():
    """One canonical list (Voice & Tone); the checklist references it instead of duplicating."""
    system = PROMPT.split(SEAM, 1)[0]
    assert system.count('"high-priority tracked open loop"') == 1
