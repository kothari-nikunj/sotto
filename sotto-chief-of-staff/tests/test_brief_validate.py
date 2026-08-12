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


# --- rule (g): every URGENT open ledger item is named; the rest are not inventoried ---------------
# The contract changed (the owner's evening brief carried 14 "Still open" rows for ~7 real debts):
# a loop earns its own line only when it is overdue, due within 24h, or already chased.

TODAY = "2026-08-10"
LEDGER = [{"contact_name": "Priya Raman", "status": "open", "summary": "Send the diligence memo",
           "deadline": "2026-08-09"},                                   # overdue → urgent
          {"contact_name": "Dana Wu", "status": "waiting", "summary": "Waiting on the signed LOI",
           "chased_count": 1}]                                          # chased, no answer → urgent


def test_urgency_predicate_is_deterministic():
    """One place decides: overdue, due within 24h, or already chased without an answer."""
    assert bv.is_urgent({"deadline": "2026-08-09"}, TODAY)              # overdue
    assert bv.is_urgent({"deadline": TODAY}, TODAY)                     # due today
    assert bv.is_urgent({"deadline": "2026-08-11"}, TODAY)              # due tomorrow (within 24h)
    assert not bv.is_urgent({"deadline": "2026-08-12"}, TODAY)          # due later — the quiet lane
    assert bv.is_urgent({"chased_count": 2}, TODAY)                     # asked twice, no answer
    assert not bv.is_urgent({"chased_count": 0, "summary": "x"}, TODAY)  # a plain open loop
    assert not bv.is_urgent({"summary": "x"}, "")                       # no day → only the chase clock
    assert bv.is_urgent({"chased_count": 1}, "")
    assert not bv.is_urgent("junk", TODAY) and not bv.is_urgent({"deadline": "nonsense"}, TODAY)


def test_dropped_urgent_ledger_name_flagged():
    # The morning bug: open loops older than the 1-day gather window vanished from the brief entirely.
    md = "## Needs Attention Now\n**Dana Wu**<!--id:dana@x.com|ch:email--> owes you the signed LOI."
    v = bv.validate(md, [], SRC, action_ledger=LEDGER, today=TODAY)
    assert any(x.startswith("dropped-open-loop") and "Priya Raman" in x for x in v)
    assert not any("Dana Wu" in x for x in v)


def test_a_non_urgent_loop_is_never_demanded():
    """The contract change, in one assertion: a quiet open loop the brief didn't name is NOT a
    violation — it belongs to the count line and the proactive lane, not to the brief."""
    quiet = [{"contact_name": "Priya Raman", "status": "open", "summary": "Send the diligence memo"}]
    assert bv.validate("## Needs Attention Now\nquiet day", [], SRC,
                       action_ledger=quiet, today=TODAY) == []
    assert [e["summary"] for e in bv.unsurfaced_open_loops("quiet day", quiet, TODAY)] == [
        "Send the diligence memo"]
    # …and once the brief names them, they are not even counted
    assert bv.unsurfaced_open_loops("Priya Raman has the memo.", quiet, TODAY) == []


def test_a_model_written_wall_of_open_loops_is_a_violation():
    """The brief decides, it doesn't inventory: a section listing loops is the bug, one pointer
    line is the contract. (The composer's own urgent backstop is appended AFTER validation.)"""
    wall = ("## Still open\n- **A** — one\n- **B** — two\n- **C** — three\n")
    assert any(x.startswith("open-loop-inventory") for x in bv.validate(wall, [], SRC, today=TODAY))
    pointer = "## Still open\n- 9 other open loops — see /app#loops\n"
    assert not any(x.startswith("open-loop-inventory")
                   for x in bv.validate(pointer, [], SRC, today=TODAY))


def test_named_anywhere_satisfies_the_contract():
    # Already Handled counts, and so does a plain mention — placement is the model's call, presence isn't.
    md = ("## ✅ Already Handled\n**Priya Raman**<!--id:priya@x.com|ch:email--> got the memo.\n"
          "## Needs Attention Now\n**Dana Wu**<!--id:d@x.com|ch:email--> owes the signed LOI")
    assert not any(x.startswith("dropped-open-loop")
                   for x in bv.validate(md, [], SRC, action_ledger=LEDGER, today=TODAY))


def test_resolved_and_nameless_entries():
    # Resolved entries are not the contract's business; a nameless one falls back to a summary word.
    done = [{"contact_name": "Priya Raman", "status": "resolved", "summary": "Send the memo",
             "deadline": "2026-08-09"}]
    assert bv.validate("", [], SRC, action_ledger=done, today=TODAY) == []
    nameless = [{"status": "open", "summary": "Return the Redwood term sheet", "chased_count": 1}]
    assert any("redwood" in x.lower() for x in bv.validate("", [], SRC, action_ledger=nameless,
                                                           today=TODAY))
    assert bv.validate("Chased Redwood yesterday.", [], SRC, action_ledger=nameless,
                       today=TODAY) == []


def test_missing_open_loops_returns_the_urgent_entries():
    """compose_brief's backstop renders exactly what this rule flags — same helper, one source."""
    assert bv.missing_open_loops("nothing here", LEDGER, TODAY) == LEDGER
    assert bv.missing_open_loops("nothing here", LEDGER) == [LEDGER[1]]   # no day → chase clock only
    assert bv.missing_open_loops("", None) == []


def test_ledger_omitted_means_rule_does_not_run():
    assert bv.validate("## Needs Attention Now\nquiet day", [], SRC) == []


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


def test_machine_named_ledger_entries_never_drive_a_permanent_still_open_line():
    """apply_commitments writes a MEETING TITLE or an email prefix into contact_name when there is
    no recipient. The brief writes the loop under the person's name, so searching for "Board sync"
    never matches — and nothing about the item ever changes, so the backstop fired every day
    forever. A machine label is not evidence; a rare summary word is."""
    entries = [{"contact_name": "Board sync", "status": "open", "chased_count": 1,
                "summary": "Priya owes the redlines"},
               {"contact_name": "nikunj.k17", "status": "open", "chased_count": 1,
                "summary": "the pricing model"},
               {"contact_name": "Board sync", "status": "open", "chased_count": 1, "summary": "ok"}]
    md = "Priya owes you the redlines, and the pricing model is still out."
    assert bv.missing_open_loops(md, entries, TODAY) == []
    # the summary token still catches a genuinely dropped one…
    assert [e["summary"] for e in bv.missing_open_loops("quiet day", entries, TODAY)] == [
        "Priya owes the redlines", "the pricing model"]
    # …and an entry with neither a name nor a distinctive word is never reported at all
    assert bv.missing_open_loops("quiet day", [entries[2]], TODAY) == []


def test_a_group_label_the_brief_printed_counts_as_naming_it():
    """The FPV/Piston row that repeated verbatim under "Still open" after the narrative had already
    covered it: the label is not name-shaped, so only a summary word could match. When the brief
    prints the label itself, that IS the evidence — the ask is not told twice."""
    entries = [{"contact_name": "FPV / Piston", "status": "open", "chased_count": 1,
                "summary": "asked for an intro to Insight Partners"}]
    md = "## Should Handle Today\n**FPV / Piston** wants the Insight intro."
    assert bv.missing_open_loops(md, entries, TODAY) == []
    assert bv.missing_open_loops("quiet day", entries, TODAY) == entries


def test_a_name_must_match_as_a_whole_word():
    """Prose evidence is whole words — "Sam Ito" is not surfaced by "samples" — and a bare first
    name is not evidence at all, because it is shared (see the Sam-Patel case below)."""
    entries = [{"contact_name": "Sam Ito", "status": "open", "summary": "the samples",
                "chased_count": 1}]
    assert bv.missing_open_loops("We shipped the samples on Tuesday.", entries, TODAY) == entries
    assert bv.missing_open_loops("Sam Ito still owes you a call.", entries, TODAY) == []


# ── "the brief said it" is proven by IDENTITY, not by name tokens ─────────────────────────────────
# Name tokens were wrong in both directions at once: one person's line suppressed another person's
# urgent loop (silently gone), and a narrative that said "Maya" did not prove "Maya Chen" was told
# (printed twice). An `<!--id:…-->` marker is the brief's own machine-readable claim about who a
# line is about — neither ambiguous nor paraphrasable.

def test_a_marker_carrying_the_entrys_identifier_proves_the_brief_named_it():
    """The double-tell, closed: the narrative writes "Maya" but taps maya@x.com, and the ledger row
    for maya@x.com is not printed a second time under Still open."""
    entry = {"status": "open", "contact_name": "Maya Chen", "contact_identifier": "maya@x.com",
             "chased_count": 1, "summary": "the signed contract"}
    md = ("## Needs Attention Now\n"
          "- **Maya**<!--id:maya@x.com|ch:email--> has not sent the signed contract.\n")
    assert bv.is_urgent(entry, TODAY)
    assert bv.missing_open_loops(md, [entry], TODAY) == []
    assert bv.unsurfaced_open_loops(md, [entry], TODAY) == []      # named is named, not counted
    # …and a brief that never tapped her still owes her a line
    assert bv.missing_open_loops("## Needs Attention Now\n- a quiet morning.\n",
                                 [entry], TODAY) == [entry]


def test_another_persons_line_never_silences_a_shared_first_name():
    """Two Sams. The brief's only line is about Sam Patel; the ledger row is the OTHER Sam's twice-
    chased wire details. It used to be treated as already told and vanish from BOTH the printed
    lines and the count — the one failure mode this contract can never have."""
    entry = {"status": "open", "contact_name": "Sam", "contact_identifier": "+14155550001",
             "chased_count": 2, "summary": "the wire details"}
    md = "## Needs Attention Now\n- **Sam Patel**<!--id:sam.patel@x.com|ch:email--> sent the deck.\n"
    assert bv.missing_open_loops(md, [entry], TODAY) == [entry]
    # …the same is true when the row has no identifier at all: a lone first name is not evidence.
    bare = {"status": "open", "contact_name": "Sam", "chased_count": 2,
            "summary": "the wire details"}
    assert bv.missing_open_loops(md, [bare], TODAY) == [bare]


def test_a_marker_matches_a_phone_however_it_is_formatted():
    """Identity, not string equality: the ledger's "+1 (415) 555-0001" and the marker's
    "4155550001" are the same person (textutil's one normalizer decides)."""
    entry = {"status": "open", "contact_name": "Sam", "contact_identifier": "+1 (415) 555-0001",
             "chased_count": 2, "summary": "the wire details"}
    md = "## Needs Attention Now\n- **Sam**<!--id:4155550001|ch:imessage--> owes the wire details.\n"
    assert bv.missing_open_loops(md, [entry], TODAY) == []


def test_a_group_is_still_proven_by_its_label():
    """A group has no deep link by design, so the brief never renders a marker for it — its label is
    the only evidence there can be, and it stays evidence."""
    entry = {"status": "open", "contact_name": "FPV / Piston", "group_id": "chat:99",
             "contact_identifier": "chat:99", "chased_count": 1, "summary": "the term sheet redline"}
    assert bv.missing_open_loops("**FPV / Piston** want the redline.", [entry], TODAY) == []
    assert bv.missing_open_loops("quiet day", [entry], TODAY) == [entry]


# ── the hand-off ends urgency ─────────────────────────────────────────────────────────────────────

def test_a_delivered_hand_off_question_ends_the_named_line():
    """A `waiting_on` never expires and a chased one is urgent forever, so a chased-out debt held a
    named line in EVERY brief — the wall, rebuilding one row at a time. Once Sotto has asked the
    user (the question was DELIVERED, so the row carries `handoff_asked_at`) it is their move: the
    loop drops to the count line and waits there."""
    row = {"status": "open", "contact_name": "Maya Chen", "contact_identifier": "maya@x.com",
           "chased_count": 2, "summary": "the signed contract"}
    assert bv.is_urgent(row, TODAY) and bv.missing_open_loops("a quiet day", [row], TODAY) == [row]
    asked = {**row, "handoff_asked_at": "2026-08-09"}
    assert not bv.is_urgent(asked, TODAY)
    assert bv.missing_open_loops("a quiet day", [asked], TODAY) == []
    assert bv.unsurfaced_open_loops("a quiet day", [asked], TODAY) == [asked]   # counted, not gone
    assert bv.validate("a quiet day", [], SRC, action_ledger=[asked], today=TODAY) == []


# ── the count line's arithmetic: every open row lands in exactly one place ────────────────────────

def test_an_urgent_loop_with_no_printable_token_is_counted_not_lost():
    """Chased twice, a machine label for a name, no distinctive summary word: urgent, so the old
    count line skipped it; no token, so the backstop skipped it too. It was shown nowhere and
    counted nowhere. Whatever makes a row unprintable, it falls into the count."""
    nameless = {"status": "open", "contact_name": "nikunj.k17", "chased_count": 2,
                "summary": "the ask"}
    assert bv.is_urgent(nameless, TODAY) and bv._ledger_token(nameless) == ""
    assert bv.missing_open_loops("quiet day", [nameless], TODAY) == []
    assert bv.unsurfaced_open_loops("quiet day", [nameless], TODAY) == [nameless]


def test_printed_counted_and_surfaced_partition_the_open_rows():
    """The two lists are complementary by construction: no open row is in both, and none is in
    neither."""
    rows = [{"status": "open", "contact_name": "Maya Chen", "contact_identifier": "maya@x.com",
             "chased_count": 1, "summary": "the signed contract"},          # urgent, printable
            {"status": "open", "contact_name": "Ron Diaz", "contact_identifier": "ron@x.com",
             "summary": "his intro request"},                               # quiet
            {"status": "open", "contact_name": "nikunj.k17", "chased_count": 2,
             "summary": "the ask"},                                         # urgent, unprintable
            {"status": "open", "contact_name": "Ana Ruiz", "contact_identifier": "ana@x.com",
             "summary": "the invoice"}]                                     # named by the brief
    md = "## Needs Attention Now\n- **Ana Ruiz**<!--id:ana@x.com|ch:email--> sent the invoice.\n"
    missed = bv.missing_open_loops(md, rows, TODAY)
    counted = bv.unsurfaced_open_loops(md, rows, TODAY)
    assert [e["contact_name"] for e in missed] == ["Maya Chen"]
    assert [e["contact_name"] for e in counted] == ["Ron Diaz", "nikunj.k17"]
    assert len(missed) + len(counted) + 1 == len(bv.open_entries(rows))     # +1 = Ana, surfaced
