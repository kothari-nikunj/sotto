"""brief_validate.py — the deterministic post-hoc brief validator (Sprint 1 #6): each
machine-checkable rule, positive and negative."""
import importlib.util
import os

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location(
    "brief_validate", os.path.join(ROOT, "_shared", "lib", "brief_validate.py"))
bv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bv)

# A rendered source containing: a real identifier, a group header, and an event id.
SRC = """### Ops War Room [GROUP - no deep link]
[THEY SENT — Nadia] shipping is late
### Sarah Chen
identifier: sarah@acme.com
SenderEmail: dana@x.com
event_id: evt123
"""


def _v(md, actions=None, src=SRC):
    return bv.validate(md, actions or [], src)


# --- rule (a): marker on every bold name in the action sections ----------------

def test_missing_marker_flagged():
    md = "## Needs Attention Now\n**Sarah Chen** needs the deal answer."
    v = _v(md)
    assert any(x.startswith("missing-marker") and "Sarah Chen" in x for x in v)


def test_marker_present_passes():
    md = "## Needs Attention Now\n**Sarah Chen**<!--id:sarah@acme.com|ch:email--> needs the deal answer."
    assert _v(md) == []


def test_group_name_exempt_from_marker_rule():
    md = "## Should Handle Today\n**Ops War Room** is deciding logistics tonight."
    assert not any(x.startswith("missing-marker") for x in _v(md))


def test_birthday_line_exempt():
    md = "## Should Handle Today\n🎂 **Mira Patel** — birthday today, send a quick wish."
    assert not any(x.startswith("missing-marker") for x in _v(md))


def test_coming_up_lines_exempt():
    md = ("## Should Handle Today\n**Coming Up**\n- **9:00am** — Board sync (Dana)\n"
          "- **2:00pm** — 1:1\n")
    assert not any(x.startswith("missing-marker") for x in _v(md))


def test_non_action_sections_not_scanned():
    md = "## Filtered\n**Newsletter Bot** and 12 others were filtered."
    assert _v(md) == []


# --- rule (b): one person = one entry ------------------------------------------

def test_duplicate_entry_across_sections_flagged():
    md = ("## Needs Attention Now\n**Sarah Chen**<!--id:sarah@acme.com|ch:email--> wants the deal.\n"
          "## ✅ Already Handled\n**Sarah Chen**<!--id:sarah@acme.com|ch:email--> got your reply.")
    v = _v(md)
    assert any(x.startswith("duplicate-entry") and "Sarah Chen" in x for x in v)


def test_single_entry_not_flagged():
    md = "## Needs Attention Now\n**Sarah Chen**<!--id:sarah@acme.com|ch:email--> wants the deal."
    assert not any(x.startswith("duplicate-entry") for x in _v(md))


# --- rule (c): banned phrases --------------------------------------------------

def test_banned_phrases_flagged():
    md = ("## Needs Attention Now\nShe reached out and is waiting for your response — "
          "this requires your immediate attention.")
    v = _v(md)
    hits = [x for x in v if x.startswith("banned-phrase")]
    assert "banned-phrase: 'reached out'" in hits
    assert "banned-phrase: 'waiting for your response'" in hits
    assert any("immediate attention" in h for h in hits)


def test_clean_prose_passes_banned_scan():
    md = "## Needs Attention Now\nWants your read on the Harbor deal before Friday."
    assert not any(x.startswith("banned-phrase") for x in _v(md))


# --- rule (d): Coming Up ≤ 5 content lines -------------------------------------

def test_coming_up_overflow_flagged():
    lines = "\n".join(f"- **{h}:00** — Meeting {h}" for h in range(6))
    md = f"## Should Handle Today\nstuff\n**Coming Up**\n{lines}\n## ✅ Already Handled\ndone"
    assert any(x.startswith("coming-up-overflow") for x in _v(md))


def test_coming_up_five_lines_ok():
    lines = "\n".join(f"- **{h}:00** — Meeting {h}" for h in range(5))
    md = f"**Coming Up**\n{lines}"
    assert not any(x.startswith("coming-up-overflow") for x in _v(md))


def test_coming_up_heading_form_also_counted():
    lines = "\n".join(f"- line {i}" for i in range(7))
    md = f"## Coming Up\n{lines}"
    assert any(x.startswith("coming-up-overflow") for x in _v(md))


# --- rule (e): marker identifiers verbatim in source ---------------------------

def test_fabricated_identifier_flagged():
    md = "## Needs Attention Now\n**Sarah Chen**<!--id:invented@nowhere.com|ch:email--> pinged you."
    v = _v(md)
    assert any(x.startswith("fabricated-identifier") and "invented@nowhere.com" in x for x in v)


def test_meeting_marker_event_id_checked():
    ok = "<!--meeting:event_id:evt123|title:Sync|start:X|attendees:-->"
    bad = "<!--meeting:event_id:evt999|title:Sync|start:X|attendees:-->"
    assert not any(x.startswith("fabricated-identifier") for x in _v(ok))
    assert any("evt999" in x for x in _v(bad))


# --- rule (f): contextSummary / contextAsk / prose pairwise distinct ------------

def test_repetitive_action_fields_flagged():
    a = {"id": "a1", "contextSummary": "Send the deck to Dana at Acme",
         "contextAsk": "Send the deck to Dana at Acme",
         "prose": "Their board meets Thursday and the deck is the last input."}
    v = _v("", [a])
    assert any(x.startswith("repetitive-action") and "a1" in x for x in v)


def test_distinct_action_fields_pass():
    a = {"id": "a1", "contextSummary": "Dana asked for the revised deck after Tuesday's sync",
         "contextAsk": "Send Dana the updated slides before Thursday",
         "prose": "Their board meets Thursday morning — this is the last input they're waiting on."}
    assert _v("", [a]) == []


def test_empty_fields_skip_distinctness():
    assert _v("", [{"id": "a1", "contextSummary": "Send the deck", "contextAsk": "", "prose": ""}]) == []


# --- robustness ----------------------------------------------------------------

def test_validate_never_raises_on_garbage():
    assert bv.validate(None, None, None) == []
    assert bv.validate(123, [{"bad": object()}], "") == []


def test_coming_up_first_run_offer_line_allowed():
    # The sim's day-one case: a full 5-line Coming Up plus the onboarding note's MANDATED trailing
    # offer line. first_run=True allows that one extra line — otherwise the critic was told the
    # first brief a user ever sees has a "real defect" and deleted a schedule line or the offer.
    lines = "\n".join(f"- **{h}:00** — Meeting {h}" for h in range(5))
    md = f"## Should Handle Today\nstuff\n**Coming Up**\n{lines}\n" \
         "Want more? Ask me to reply to a message, prep a meeting, or list what you're waiting on."
    assert not any(x.startswith("coming-up-overflow")
                   for x in bv.validate(md, [], SRC, first_run=True))
    # The SAME brief on any later day is still an overflow (first_run tolerance is one-time only).
    assert any(x.startswith("coming-up-overflow") for x in bv.validate(md, [], SRC))


def test_coming_up_first_run_does_not_hide_real_overflow():
    lines = "\n".join(f"- line {i}" for i in range(7))    # 7 > 5 + the one allowed offer line
    md = f"## Coming Up\n{lines}"
    assert any(x.startswith("coming-up-overflow") for x in bv.validate(md, [], SRC, first_run=True))
