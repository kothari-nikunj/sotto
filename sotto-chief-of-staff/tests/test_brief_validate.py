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


def test_dropped_open_loop_patch_supplies_exact_saved_task_and_date():
    row = {'anchor_key': 'intro-call', 'contact_name': 'Alex Stone', 'status': 'open',
           'created_at': '2026-08-05 17:30:00', 'deadline': '2026-08-09',
           'summary': 'Alex asked for a Project Cedar call after the investor introduction',
           'ask': 'Reply to Alex with a time for the Project Cedar call'}
    violations = bv.validate('## Needs Attention Now\nNo task selected.', [], SRC,
                             action_ledger=[row], today='2026-08-10')
    patch = next(v for v in violations if v.startswith('dropped-open-loop:'))
    assert 'intro-call' in patch and '2026-08-05' in patch
    assert row['summary'] in patch and row['ask'] in patch


def test_dropped_historical_task_warns_first_revision_against_two_dated_field_sentences():
    row = {'anchor_key': 'group-deck', 'contact_name': 'Research Group', 'status': 'open',
           'created_at': '2026-08-05', 'deadline': '2026-08-09',
           'summary': 'Dana shared a trial-project deck and raised a review tomorrow.',
           'ask': 'Review the deck and ask Dana about the data room tomorrow.'}
    dropped = bv._check_open_ledger_coverage('## Needs Attention Now\nNo task selected.',
                                             [row], '2026-08-10')
    assert len(dropped) == 1
    assert 'one complete canonical ask' in dropped[0]
    assert 'not two separately dated task sentences' in dropped[0]
    current = dict(row, created_at='2026-08-10')
    assert 'not two separately dated task sentences' not in bv._check_open_ledger_coverage(
        '## Needs Attention Now\nNo task selected.', [current], '2026-08-10')[0]


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


def test_marked_open_task_needs_its_saved_summary_ask_and_historical_date():
    old = {'anchor_key': 'phone-relay', 'contact_name': 'Alex Stone', 'status': 'open',
           'created_at': '2026-08-05 17:30:00',
           'summary': 'Alex wants to connect today or tomorrow about Project Cedar.',
           'ask': 'Tell Alex your availability for a Project Cedar call tomorrow.'}
    group = {'anchor_key': 'group-deck', 'contact_name': 'Project Group', 'status': 'open',
             'created_at': '2026-08-10 09:00:00',
             'summary': 'Dana shared the clinical trial deck and flagged a data room review tomorrow.',
             'ask': 'Review the deck and coordinate with Dana on data room next steps.'}
    partial = ('## Needs Attention Now\n**Alex Stone**<!--id:alex@example.test|ch:email-->'
               '<!--loop:phone-relay-->: Alex wants to connect today or tomorrow about Project Cedar. '
               'Reply with availability.\n'
               '**Project Group**<!--loop:group-deck-->: Dana flagged a data room review tomorrow.\n')
    flagged = [v for v in bv.validate(partial, [], SRC, action_ledger=[old, group],
                                       today='2026-08-10') if v.startswith('marked-loop-wording:')]
    assert len(flagged) == 2
    assert 'phone-relay' in flagged[0] and 'Aug 5' in flagged[0]
    assert 'group-deck' in flagged[1] and 'clinical trial deck' in flagged[1]
    complete = ('## Needs Attention Now\n**Alex Stone**<!--id:alex@example.test|ch:email-->'
                '<!--loop:phone-relay-->: From Aug 5: “Alex wants to connect today or tomorrow '
                'about Project Cedar. Tell Alex your availability for a Project Cedar call tomorrow.”\n'
                '**Project Group**<!--loop:group-deck-->: Dana shared the clinical trial deck '
                'and flagged a data room review tomorrow. Review the deck and coordinate with '
                'Dana on data room next steps.\n')
    assert not [v for v in bv.validate(complete, [], SRC, action_ledger=[old, group],
                                       today='2026-08-10') if v.startswith('marked-loop-wording:')]


def test_marked_wording_check_does_not_rewrite_handled_or_backstop_lines():
    row = {'anchor_key': 'deck', 'contact_name': 'Alex Stone', 'status': 'open',
           'summary': 'Send the strategy deck', 'ask': 'Reply with the deck link'}
    markdown = ('## ✅ Already Handled\n**Alex Stone**<!--loop:deck-->: Sent it.\n'
                '## Still open\n- **Alex Stone**: Send the strategy deck <!--loop:deck-->')
    assert not [v for v in bv.validate(markdown, [], SRC, action_ledger=[row])
                if v.startswith('marked-loop-wording:')]


def test_one_marked_historical_task_does_not_repeat_its_summary_and_ask_as_two_dated_sentences():
    row = {'anchor_key': 'group-deck', 'contact_name': 'Research Group', 'status': 'open',
           'created_at': '2026-08-05 17:30:00',
           'summary': 'From Aug 5: Dana shared a deck for a trial project and raised a review tomorrow.',
           'ask': 'Review the deck and ask Dana whether to examine the data room tomorrow.'}
    doubled = ('## Needs Attention Now\n**Research Group**<!--loop:group-deck--> - '
               'From Aug 5: “Dana shared a deck for a trial project and raised a review tomorrow.” '
               'From Aug 5: “Review the deck and ask Dana whether to examine the data room tomorrow.”')
    violations = bv._check_marked_loop_wording(doubled, [row], '2026-08-10')
    assert len(violations) == 1
    assert 'one complete canonical ask' in violations[0]
    assert 'preserve any separate asks' in violations[0]
    # A single ask with distinct context, or one quotation carrying both fields, is not a pair.
    concise = ('## Needs Attention Now\n**Research Group**<!--loop:group-deck--> - '
               'Dana shared the trial-project deck. From Aug 5: “Review the deck and ask Dana '
               'whether to examine the data room tomorrow.”')
    assert bv._check_marked_loop_wording(concise, [row], '2026-08-10') == []
    combined = doubled.replace('” From Aug 5: “', ' ').replace('tomorrow. Review', 'tomorrow. Review')
    assert bv._check_marked_loop_wording(combined, [row], '2026-08-10') == []
    # Two marked obligations keep their independent wording and dates.
    sibling = dict(row, anchor_key='other-deck', contact_name='Other Person',
                   created_at='2026-08-06', ask='Send a separate signed budget tomorrow.')
    separate = doubled + '<!--loop:other-deck--> From Aug 6: “Send a separate signed budget tomorrow.”'
    assert not [v for v in bv._check_marked_loop_wording(separate, [row, sibling], '2026-08-10')
                if 'one complete canonical ask' in v]


def test_marked_historical_relative_day_requires_year_across_years():
    row = {'anchor_key': 'old-call', 'contact_name': 'Alex Stone', 'status': 'open',
           'created_at': '2025-08-05 17:30:00',
           'summary': 'Alex asked to call tomorrow about Project Cedar',
           'ask': 'Reply to Alex about Project Cedar tomorrow'}
    line = ('## Needs Attention Now\n**Alex Stone**<!--loop:old-call-->: From Aug 5: '
            '“Alex asked to call tomorrow about Project Cedar.” '
            'Reply to Alex about Project Cedar tomorrow.')
    flagged = bv._check_marked_loop_wording(line, [row], '2026-08-10')
    assert len(flagged) == 1 and 'Aug 5, 2025' in flagged[0]
    dated = line.replace('Aug 5:', 'Aug 5, 2025:').replace(
        'Project Cedar.” Reply to Alex about Project Cedar tomorrow.',
        'Project Cedar. Reply to Alex about Project Cedar tomorrow.”')
    assert bv._check_marked_loop_wording(dated, [row], '2026-08-10') == []


def test_historical_relative_ask_outside_dated_quote_is_sent_to_existing_revision():
    row = {'anchor_key': 'relay', 'status': 'open', 'contact_name': 'Alex Stone',
           'created_at': '2026-08-05',
           'summary': 'Alex asked to connect today or tomorrow about Project Cedar.',
           'ask': 'Tell Alex your availability for a call tomorrow about Project Cedar.'}
    line = ('## Needs Attention Now\n**Alex Stone**<!--loop:relay-->: From Aug 5: '
            '“Alex asked to connect today or tomorrow about Project Cedar.” '
            'Tell Alex your availability for a call tomorrow about Project Cedar.')
    flagged = bv._check_marked_loop_wording(line, [row], '2026-08-10')
    assert len(flagged) == 1 and 'outside dated quotation' in flagged[0]
    sibling = line.replace('<!--loop:relay-->', '<!--loop:relay--><!--loop:current-call-->')
    assert not [v for v in bv._check_marked_loop_wording(sibling, [row], '2026-08-10')
                if 'outside dated quotation' in v]
    # The entire historical summary and ask in one dated single-quoted span is safe.
    dated_single = ('## Needs Attention Now\n**Alex Stone**<!--loop:relay-->: From Aug 5: '
                    '‘Alex asked to connect today or tomorrow about Project Cedar. '
                    'Tell Alex your availability for a call tomorrow about Project Cedar.’')
    assert bv._check_marked_loop_wording(dated_single, [row], '2026-08-10') == []


def test_relay_loop_action_cannot_borrow_email_recipient_from_distinct_current_task():
    relay = {'anchor_key': 'follow_up:cid:relay', 'status': 'open',
             'contact_name': 'Alex Stone', 'contact_identifier': '+15550001000',
             'summary': 'A relay mentioned Alex wanted a call',
             'ask': 'Ask Alex about the old call'}
    current = {'anchor_key': 'follow_up:cid:current', 'status': 'open',
               'contact_name': 'Alex Stone', 'contact_identifier': 'alex@example.test',
               'summary': 'Alex replied after the introduction',
               'ask': 'Answer Alex with a call time'}
    legacy = {'anchor_key': 'thread:original-thread', 'status': 'open',
              'contact_name': 'Alex Stone', 'contact_identifier': '',
              'summary': 'Alex asked for a call', 'ask': 'Reply with a time'}
    action = {'loopId': relay['anchor_key'], 'contactIdentifier': 'alex@example.test'}
    flagged = bv._check_action_loop_recipients([action], [relay, current, legacy])
    assert len(flagged) == 1 and 'loop-recipient-mismatch:' in flagged[0]
    assert relay['summary'] in flagged[0] and relay['ask'] in flagged[0]
    assert any(v.startswith('loop-recipient-mismatch:') for v in bv.validate(
        '## Needs Attention Now\n**Alex Stone**<!--id:alex@example.test|ch:email-->'
        '<!--loop:follow_up:cid:relay-->: old call.', [action],
        'alex@example.test +15550001000', action_ledger=[relay, current, legacy],
        allowed_identifiers={'alex@example.test', '5550001000'}))
    assert bv._check_action_loop_recipients(
        [{**action, 'loopId': current['anchor_key']}], [relay, current, legacy]) == []
    assert bv._check_action_loop_recipients(
        [{**action, 'loopId': legacy['anchor_key'], 'emailThreadId': 'original-thread'}],
        [relay, current, legacy]) == []  # An exact thread is not proof of its sender.


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

# --- rule (h): never tell you twice ------------------------------------------

def test_an_already_nudged_person_may_not_open_the_brief_or_take_more_than_two_lines():
    """The prompt asks for one compressed line; this is the measurement. A 3pm ask that tapped the
    user at 3:05 is not the evening's headline, and it is not three paragraphs."""
    opener = ("Sarah Chen is still waiting on the deck — she asked again this afternoon.\n"
              "## Needs Attention Now\n- **Ben**<!--id:ben@x.com|ch:email--> wants Thursday\n")
    v = bv.validate(opener, [], SRC, already_nudged=["Sarah Chen"])
    assert [x for x in v if x.startswith("already-nudged")] == [
        "already-nudged: 'Sarah Chen' opens the brief — Sotto already nudged the user about them "
        "today; it compresses to one matter-of-fact line, never the opener"]
    retold = ("Good evening.\n## Needs Attention Now\n"
              "- **Sarah Chen**<!--id:sarah@acme.com|ch:email--> asked for the deck\n"
              "- Sarah Chen followed with a second note about timing\n"
              "- and Sarah Chen's board wants it Friday\n")
    v = bv.validate(retold, [], SRC, already_nudged=["Sarah Chen"])
    assert any(x.startswith("already-nudged: 'Sarah Chen' takes 3 lines") for x in v)
    fine = ("Good evening.\n## Needs Attention Now\n"
            "- nudged you at 3:05 — Sarah Chen's deck ask; still open unless you handled it\n"
            "## Coming Up\n- 10:00 Sarah Chen — pipeline review\n")
    assert not [x for x in bv.validate(fine, [], SRC, already_nudged=["Sarah Chen"])
                if x.startswith("already-nudged")]
    # full names only: a single token is a common word as often as a person
    common = "Will send the redline.\n## Needs Attention Now\n- it will be ready\n- will do\n"
    assert not bv.validate(common, [], SRC, already_nudged=["Will"])
    assert not bv.validate(common, [], SRC, already_nudged=["still no word from Maya on the deck"])
    # …and the rule is off when nothing was nudged
    assert not [x for x in bv.validate(retold, [], SRC) if x.startswith("already-nudged")]


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


def test_coming_up_counts_visible_schedule_rows_only():
    rows = '\n'.join(f'- 9:0{i} AM — Meeting {i}' for i in range(5))
    assert bv._check_coming_up_length(f'**Coming Up**\n{rows}\n<!--meeting:event_id:abc-->\n') == []
    assert bv._check_coming_up_length(f'**Coming Up**\n{rows}\n\n**Already Handled**\n- **A**: done\n- **B**: done\n') == []
    assert bv._check_coming_up_length(f'**Coming Up**\n{rows}\n\nA newer Sotto is available.\nSay yes to keep that rule.\n') == []
    assert bv._check_coming_up_length(f'**Coming Up**\n{rows}\n- 3:00 PM — Sixth\n')  # a real overflow


def test_coming_up_stops_at_unheaded_marked_tasks_after_calendar():
    rows = '\n'.join(f'- Wed Oct 14 {hour}:00 AM: Meeting {hour}' for hour in range(8, 13))
    tasks = '\n'.join(f'- Reply to Person {i} <!--loop:task-{i}-->' for i in range(5))
    markdown = f'**Coming Up**\n{rows}\n\n<!--meeting:event_id:one|start:2026-10-14-->\n\n{tasks}\n'
    assert bv._check_coming_up_length(markdown) == []
    assert bv._check_coming_up_length(f'**Coming Up**\n{rows}\n- Wed Oct 14 1:00 PM: Sixth\n')
    assert bv._check_coming_up_length(
        f'**Coming Up**\n{rows}\n\n- Wed Oct 14 1:00 PM: Sixth <!--loop:unexpected-->\n')


def test_marked_loop_wording_accepts_a_line_showing_the_whole_ask():
    row = {'anchor_key': 'deck', 'status': 'open', 'contact_name': 'Alex Stone',
           'summary': 'Alex asked whether you could review the Q4 board deck', 'ask': 'Review the Q4 board deck',
           'created_at': '2026-10-09'}
    shown = '## Needs Attention Now\n- **Alex Stone**: Review the Q4 board deck. <!--loop:deck-->\n'
    assert bv._check_marked_loop_wording(shown, [row], '2026-10-10') == []
    partial = '## Needs Attention Now\n- **Alex Stone**: The Q4 deck. <!--loop:deck-->\n'
    assert bv._check_marked_loop_wording(partial, [row], '2026-10-10')


def test_backstop_sections_are_not_model_inventory_or_dropped_loops():
    row = {'anchor_key': 'memo', 'status': 'open', 'contact_name': 'Priya Raman', 'chased_count': 1,
           'contact_identifier': 'p@x.com', 'summary': 'Send the diligence memo', 'created_at': '2026-10-08'}
    other = {**row, 'anchor_key': 'lease', 'contact_identifier': 'd@x.com', 'contact_name': 'Dana Wu',
             'summary': 'Return the signed lease'}
    md = ('## Still open\n- **Priya Raman**: Send the diligence memo <!--loop:memo-->\n'
          '- **Dana Wu**: Return the signed lease <!--loop:lease-->\n- 2 other open loops.\n')
    flagged = bv.validate(md, [], '', action_ledger=[row, other], today='2026-10-10')
    assert any(v.startswith('open-loop-inventory') for v in flagged)  # model-written: still flagged
    final = bv.validate(md, [], '', action_ledger=[row, other], today='2026-10-10',
                        deterministic_sections=('still open',))
    assert not [v for v in final if v.startswith(('open-loop-inventory', 'dropped-open-loop'))]


def test_coming_up_still_counts_real_overflow_the_composer_did_not_rewrite():
    rows = '\n'.join(f'- 9:0{i} AM: Meeting {i}' for i in range(7))
    assert bv._check_coming_up_length(f'**Coming Up**\n**Today**\n{rows}\n')
    spaced = '- a\n\n' + '\n'.join(f'- {c}' for c in 'bcdef')
    assert bv._check_coming_up_length(f'**Coming Up**\n{spaced}\n')


def test_only_the_composers_last_block_is_exempt_from_the_inventory_rule():
    model = '## Still open\n- **A Person**: one\n- **B Person**: two\n'
    composer = '## Still open\n- **C Person**: three <!--loop:c-->\n- **D Person**: four <!--loop:d-->\n'
    flagged = bv._check_no_open_loop_inventory(model + composer, ('still open',))
    assert len(flagged) == 1  # the model-written list is still inventory
    assert bv._check_no_open_loop_inventory(composer, ('still open',)) == []


def test_composed_action_prose_may_repeat_its_distinct_summary_and_next_step():
    action = {'id': 'one', 'contextSummary': 'A partner supplied the clinical dataset deck.',
              'contextAsk': 'Review the data room and discuss pricing.',
              'prose': 'A partner supplied the clinical dataset deck. Review the data room and discuss pricing.'}
    assert not bv._check_action_field_distinctness([action])
    repeated = dict(action, contextAsk=action['contextSummary'])
    assert bv._check_action_field_distinctness([repeated])
