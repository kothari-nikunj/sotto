"""The ledger holds DEBTS — not everything that mentions the user.

Built from the owner's live volume (Aug 2026), anonymized. Two defects, one file, because they are
the same disease seen from two ends:

  THE EXPIRED SET said the capture bar is too low. 261 rows: 26 open, 80 resolved, 121 EXPIRED,
  34 dismissed — more debts died of old age than were ever closed. A 30-row sample of the expired
  ones held roughly 5 real commitments; the rest were receipt reminders, benefits enrollment, a
  badge-setup invite, cold pitches from strangers, launch announcements, doc mentions, and two rows
  with no summary at all.

  THE OPEN SET said identity is too fragile. Of 26 open rows about 11 were distinct debts: ONE group
  ask filed as six rows, ONE sync as four, ONE call request as three across two channels, and two
  named/nameless pairs. The anchor is `channel : family : counterpart` and the extractor supplied
  all three — ~19 invented `action_type` values, a channel it guessed differently per run, and a
  label it reworded (or left blank) every time.

WHAT IS CODE AND WHAT IS THE PROMPT'S JOB (stated once, tested below in that order):
  code   — a row with no words; a row that identifies nobody; a counterpart that is a no-reply
           address. None of these need judgment, so none of them belong in a prompt.
  prompt — "is this them ANSWERING him, an FYI, or a cold pitch rather than an ask?" is judgment
           about what a message MEANS. Faking it here with keyword matching would drop real asks,
           and a brief that says "nothing needs you today" is the worse failure. Part 2 pins the
           prompt text that carries it.

Every name, address and company below is invented (the publish guard forbids real personal
addresses in fixtures); only the SHAPES are the owner's.
"""
import importlib.util
import os

import yaml

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location(
    "cr_debt", os.path.join(ROOT, "morning-brief", "scripts", "continuity_resolve.py"))
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)

TODAY = "2026-08-10"
NOW = __import__("datetime").datetime(2026, 8, 10, 9, 0, 0)


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")


def _act(kind, name, summary, ident=None, channel="gmail", ask="", **extra):
    a = {"type": kind, "channel": channel, "contactName": name, "contextSummary": summary}
    if ident:
        a["contactIdentifier"] = ident
    if ask:
        a["contextAsk"] = ask
    a.update(extra)
    return a


# ── The 30 expired rows, anonymized. `code_rejects` is the claim under test. ───────────────────────
# id, action, code_rejects, why-it-is-or-is-not-a-debt
AUTOMATED = [
    (_act("reply", "Spendco", "The 'Investor' card will be locked in 24 hours due to 12 missing "
                              "receipts", "no-reply@spendco.example"), "no-reply sender"),
    (_act("reply", "Benefitly", "Benefits Open Enrollment has started",
          "notifications@benefitly.example"), "notifications sender"),
    (_act("reply", "Badgely", "Invite to set up Badgely Pass", "no-reply@badgely.example"),
     "no-reply sender"),
    (_act("reply", "Finance", "Request for transaction memo for $72.18",
          "receipts@spendco.example"), "receipts sender"),
]
EMPTY = [
    (_act("reply", "Caregiver Update", "", "no-reply@carewell.example"), "no summary"),
    (_act("reply", "Spendco", "", "hello@spendco.example"), "no summary"),
    (_act("follow_up", "", "", channel="imessage"), "no summary and no counterpart"),
]
# These three shapes are the PROMPT's job — a human sender at a human-looking address, so no
# deterministic rule can tell them from a real ask without reading what the message means.
COLD_OUTREACH = [
    _act("reply", "+33750298517", "Checking if you need help or services before July 21st in Paris",
         "+33750298517", channel="whatsapp"),
    _act("reply", "Milo Brandt", "Partnership outreach from Cratewell", "milo@cratewell.example"),
    _act("reply", "Aerix Dynamics", "Seed round pitch", "hello@aerix.example"),
]
FYI_NO_ASK = [
    _act("reply", "Rosa Lindqvist", "Sent over the H1 2026 update", "rosa@northwind.example"),
    _act("reply", "Devesh Rao", "Latest deal sharing", "devesh@harborline.example"),
    _act("reply", "Omar Haddad", "Launched 'Happy Oyster'", "omar@reactorlabs.example"),
    _act("reply", "Usable", "Mentioned in the 'Usable - Term Sheet' doc", "team@usable.example"),
]
TIME_MOOT = [
    _act("reply", "Marguerite Wagner", "Coordinating a sync for tomorrow; hard stop 11:00 AM",
         "marguerite@ridgeline.example"),
    _act("reply", "Tomas Vidal", "Confirmed availability to meet on the 4th",
         "tomas@ridgeline.example"),
]
# The duplicates the sample still carried: the same cold pitch and the same launch note, twice.
DUPLICATES = [
    _act("reply", "Milo Brandt", "Partnership outreach from Cratewell", "milo@cratewell.example"),
    _act("reply", "Omar Haddad", "Launched 'Happy Oyster' this week", "omar@reactorlabs.example"),
    _act("reply", "Fintech for Home Services", "Cold pitch deck for a home-services fintech",
         "founders@homefin.example"),
    _act("reply", "Fintech for Home Services", "Cold pitch deck, second send",
         "founders@homefin.example"),
]
# …and the ~5 that were REAL. Every one names a person, a thing owed, and a request or a promise.
GENUINE = [
    _act("reply", "Gregory Mattison", "Waiting on your reply per the CRM — the thread has been "
                                      "open since Tuesday", "gregory@kelsopartners.example",
         ask="Reply to Gregory on the Kelso follow-up"),
    _act("reply", "Victor Yeung", "Asked you to decide the allocation on the SPV before Friday",
         "victor@yeungcapital.example", ask="Give Victor the allocation decision"),
    _act("reply", "Sana Qureshi", "Asked to catch up and proposed coffee next week",
         "sana@brightfold.example", ask="Pick a time with Sana"),
    _act("reply", "Seth Warren", "Asked whether you are in on the seed extension",
         "seth@warrenlabs.example", ask="Tell Seth yes or no on the extension"),
    _act("waiting_on", "Amara Osei", "Promised to send her read on the new pricing model",
         "amara@osei.example", ask="Check whether Amara sent the pricing note"),
]


def _active_names(tmp_path, monkeypatch, actions):
    _env(tmp_path, monkeypatch)
    out = cr.resolve({"today": TODAY, "new_actions": list(actions)}, NOW)
    return [a["contact_name"] for a in out["active"]]


# ── Part 1: the deterministic half ────────────────────────────────────────────────────────────────

def test_an_automated_counterpart_never_opens_a_debt(tmp_path, monkeypatch):
    """Nobody is waiting at a no-reply address. The predicate is textutil's `_is_likely_automated` —
    the SAME one event triage drops automated senders with, so the ledger is not re-deciding a
    question that already has an owner."""
    for action, why in AUTOMATED:
        assert cr.not_a_debt(cr._normalize_action(action)), why
    assert _active_names(tmp_path, monkeypatch, [a for a, _ in AUTOMATED]) == []


def test_a_row_with_no_words_is_never_a_debt(tmp_path, monkeypatch):
    """"Caregiver Update" with an empty summary is a row nobody can act on. A debt has to SAY what
    is owed — the gate reads summary and ask together, so a terse summary with a real ask survives."""
    for action, why in EMPTY:
        assert cr.not_a_debt(cr._normalize_action(action)), why
    assert _active_names(tmp_path, monkeypatch, [a for a, _ in EMPTY]) == []
    terse_but_real = _act("reply", "Sana Qureshi", "the deck", "sana@brightfold.example",
                          ask="Send Sana the deck")
    assert cr.not_a_debt(cr._normalize_action(terse_but_real)) == ""


def test_a_row_that_identifies_nobody_never_opens_a_debt(tmp_path, monkeypatch):
    """The nameless half of every pair in the open set. Such a row can never be resolved (every
    cross-channel check needs an identifier) or tapped, and worse: they ALL collapse onto the single
    anchor ending `name:`, so two unrelated asks become one debt."""
    nameless = _act("reply", "", "Team members in the group chat asked you to call Pegah about the "
                                 "allocation", channel="imessage")
    assert "no counterpart" in cr.not_a_debt(cr._normalize_action(nameless))
    assert _active_names(tmp_path, monkeypatch, [nameless]) == []
    # a synthetic anchor is the ONE nameless shape that is still a debt: the thread IS the identity
    commitment = {"action_type": "follow_up", "contact_name": "", "channel": "followup",
                  "source_thread_id": "commitment:abc123", "summary": "send the revised model"}
    assert cr.not_a_debt(commitment) == ""


def test_the_genuine_asks_all_survive_the_deterministic_gate(tmp_path, monkeypatch):
    """The opposite failure — an empty brief — is the worse one. Every real commitment in the
    sample passes every code gate, and each one still opens exactly one ledger row."""
    for action in GENUINE:
        assert cr.not_a_debt(cr._normalize_action(action)) == "", action["contactName"]
    names = _active_names(tmp_path, monkeypatch, GENUINE)
    assert sorted(names) == sorted(a["contactName"] for a in GENUINE)


def test_the_whole_sample_end_to_end(tmp_path, monkeypatch):
    """All 30 rows through one merge. The code half removes the automated and the wordless; the
    duplicates collapse by identity; nothing genuine is lost."""
    sample = ([a for a, _ in AUTOMATED] + [a for a, _ in EMPTY] + COLD_OUTREACH + FYI_NO_ASK
              + TIME_MOOT + DUPLICATES + GENUINE)
    assert len(sample) == 25          # the rows the owner enumerated verbatim, of 30 sampled
    names = _active_names(tmp_path, monkeypatch, sample)
    # nothing automated, nothing wordless
    assert not {"Benefitly", "Badgely", "Finance", "Caregiver Update"} & set(names)
    assert "Spendco" not in names          # both Spendco rows were junk (no-reply, then no summary)
    # the duplicates are one row each, not two
    assert names.count("Milo Brandt") == 1 and names.count("Omar Haddad") == 1
    assert names.count("Fintech for Home Services") == 1
    # every genuine ask is still there
    assert {a["contactName"] for a in GENUINE} <= set(names)
    # …and the rows only the PROMPT can judge are still captured today, deliberately (see Part 2)
    assert "Rosa Lindqvist" in names


# ── Part 2: the half that needs judgment lives in the prompt, and is pinned there ──────────────────

PROMPT = open(os.path.join(ROOT, "morning-brief", "references", "extraction-prompt.md"),
              encoding="utf-8").read()


def test_the_prompt_states_the_debt_test_and_its_negative_examples():
    """What code must not fake, the prompt must say — and say concretely. These are the owner's own
    expired rows, anonymized, written into the prompt as negative examples."""
    low = PROMPT.lower()
    assert "## the debt test" in low
    for clause in ("a specific thing owed by a specific person",
                   "a request or a promise behind it"):
        assert clause in low, clause
    # one negative example per shape the sample was full of
    for shape in ("open enrollment",        # a transactional notice
                  "cold",                   # a pitch from a stranger
                  "mention",                # named in a doc
                  "launched",               # a launch announcement
                  "confirmed"):             # them ANSWERING him
        assert shape in low, shape


# ── Part 3: the OPEN set — one debt, one row, whatever the extractor called it ─────────────────────

def _row(tmp_path, key, filename=None, **fm):
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True, exist_ok=True)
    fm.setdefault("anchor_key", key)
    fm.setdefault("status", "open")
    fm.setdefault("created_at", "2026-08-05")
    fm.setdefault("times_surfaced", 1)
    name = filename or cr._safe(key)
    (d / f"{name}.md").write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n")


# The owner's open ledger, anonymized: the 24 rows he enumerated out of 26, in the exact shapes his
# extractor produced them. `_seed` writes each one under the anchor TODAY'S code computes for it,
# which is how they came to exist.
OPEN_ROWS = [
    # ONE group ask → six rows (invented labels, a reworded label, and a guessed `slack` channel)
    ("reply", "imessage", "Intro Group", "Asked in the Ridge / Anvil group thread if anyone knows "
                                         "contacts at Crescent Partners"),
    ("reply", "imessage", "Ridge / Anvil", "Asked 1 day ago in the Ridge / Anvil group thread if "
                                           "anyone knows contacts."),
    ("follow_up", "imessage", "Intro Group", "Asked in the Ridge / Anvil group thread if anyone "
                                             "knows contact info"),
    ("reply", "slack", "", "Group asked in Ridge / Anvil thread if anyone knows contacts"),
    ("reply", "imessage", "Ridge / Anvil Group", "Team members in Ridge / Anvil chat asked if "
                                                 "anyone knows contacts at Crescent Partners"),
    ("reply", "imessage", "Ridge / Anvil Group", "Team members in the Ridge / Anvil group thread "
                                                 "asked if anyone knows contacts at Crescent"),
    # ONE sync → four rows, two of them calendar shadows the old MEETING_TYPES check let through
    ("meeting", "calendar", "You and Jonas Getty", "Sync with Jonas Getty (Gutter Capital) on "
                                                   "Monday morning."),
    ("calendar", "calendar", "", "Sync with Jonas Getty scheduled for Monday"),
    ("schedule", "email", "", "Jonas Getty from Gutter Capital requested a sync Monday morning"),
    ("follow_up", "gmail", "Jonas Getty", "Jonas requested a morning sync ahead of your 2:00 PM "
                                          "meeting today"),
    # ONE call request → three rows, across two channels
    ("reply", "imessage", "", "Team members in the FLASH group chat asked you to call Pegah about "
                              "the allocation"),
    ("call", "whatsapp", "", "Team requested in the FLASH group chat to call Pegah about the "
                             "Railside investment"),
    ("call", "whatsapp", "Pegah Ebrahim", "Pegah asked for a call about the Railside SAFE "
                                          "allocation before Monday"),
    # the named/nameless pairs
    ("reply", "imessage", "Farid Mirzayev", "Farid asked for the intro to Kimia"),
    ("reply", "imessage", "", "Asked for the intro to Kimia"),
    ("reply", "email", "Amy Wooten", "Amy asked where the Forkable renewal landed"),
    ("reply", "email", "", "Asked where the Forkable renewal landed"),
    # …and the seven single genuine debts, each already one row
    ("reply", "email", "Justin Leigh", "Justin asked for the Fund II list of allocations"),
    ("follow_up", "email", "Spencer Sneed", "You owe Spencer the intro to the Onshore team"),
    ("reply", "imessage", "Animesh Bhatt", "Animesh asked whether you can join the Thursday review"),
    ("reply", "imessage", "Eshanth Rao", "Eshanth asked for a written reference by Friday"),
    ("waiting_on", "email", "Tanmayi Kolla", "Tanmayi owes you the pre-read for Monday's board"),
    ("reply", "whatsapp", "Yum Gong", "Yum asked whether the Tuesday dinner is still on"),
    ("reply", "email", "Gaurav Menon", "Gaurav asked you to review the revised model"),
]

# The eleven distinct debts those 24 rows describe, by the survivor's contact_name (the group ask
# survives under whichever label the extractor used most recently).
GENUINE_OPEN = {"Jonas Getty", "Pegah Ebrahim", "Farid Mirzayev", "Amy Wooten", "Justin Leigh",
                "Spencer Sneed", "Animesh Bhatt", "Eshanth Rao", "Tanmayi Kolla", "Yum Gong",
                "Gaurav Menon"}


def _seed_open_ledger(tmp_path):
    for i, (kind, channel, name, summary) in enumerate(OPEN_ROWS):
        identity = {"action_type": kind, "channel": channel, "contact_name": name}
        # One FILE per row, under the anchor today's code computes — collisions included, because
        # colliding files are exactly what his volume holds.
        _row(tmp_path, cr.compute_anchor_key(identity), filename=f"row{i:02d}",
             action_type=kind, channel=channel, contact_name=name, summary=summary)


def test_an_invented_action_type_can_no_longer_fork_a_row():
    """`action_family` is a CLOSED set with a default. The extractor's ~19 invented type words all
    land in the owed-by-you family instead of minting one apiece — the direction split survives."""
    invented = ("action", "task", "review", "read", "info", "reminder", "email_follow",
                "document_mention", "action_required", "follow_up_stalled", "call", "reply_email")
    fams = {cr.action_family(t) for t in invented}
    assert fams == {"follow_up"}
    assert cr.action_family("waiting_on") == "waiting_on"          # direction never collapses
    assert cr.action_family("meeting") == cr.action_family("calendar") == "meeting"
    assert cr.action_family("propose_time") == "scheduling"


def test_a_verified_identity_keys_the_debt_without_the_channel():
    """One person, one debt — the channel is not part of who. The extractor guessed `imessage` one
    run and `whatsapp` the next for the same ask; only the guessed `name:` fallback still carries
    the channel, to keep two same-named strangers apart."""
    ims = cr.compute_anchor_key({"channel": "imessage", "action_type": "reply",
                                 "contact_identifier": "+14155551234"})
    wa = cr.compute_anchor_key({"channel": "whatsapp", "action_type": "call",
                                "contact_identifier": "4155551234"})
    assert ims == wa == "follow_up:id:4155551234"
    named = cr.compute_anchor_key({"channel": "imessage", "action_type": "reply",
                                   "contact_name": "Ridge / Anvil"})
    assert named == "imessage:follow_up:name:ridge /"


def test_a_calendar_shadow_never_becomes_a_debt(tmp_path, monkeypatch):
    """The meeting skip is keyed on the FAMILY, not on two literal spellings. `meeting` and
    `calendar` walked straight past the old set — four rows for one sync."""
    _env(tmp_path, monkeypatch)
    out = cr.resolve({"today": TODAY, "new_actions": [
        _act("meeting", "You and Jonas Getty", "Sync with Jonas Getty on Monday", channel="calendar"),
        _act("calendar", "Jonas Getty", "Sync with Jonas Getty scheduled for Monday",
             "jonas@guttercap.example", channel="calendar"),
        _act("meeting_prep", "Board sync", "Prep the board sync", channel="calendar"),
    ]}, NOW)
    assert out["active"] == []


def test_the_owners_open_ledger_collapses_to_its_real_debts(tmp_path, monkeypatch):
    """The acceptance test, on his volume. 24 rows describing 11 debts (plus the group ask) become
    13 — and EVERY genuine debt is still open afterwards. The two residual rows are the group ask
    under two labels the extractor invented ("Intro Group" vs the thread's real name): nothing in
    the snapshot links them, and the code refuses to guess. They fold the moment a capture carries
    the group's `chat_guid`, which `_canonicalize_group` already verifies."""
    _env(tmp_path, monkeypatch)
    _seed_open_ledger(tmp_path)
    assert len(list((tmp_path / "knowledge" / "continuity").glob("*.md"))) == 24

    out = cr.resolve({"today": TODAY}, NOW)
    active = out["active"]
    assert len(active) == 13

    # every real debt survived — this is the "never an empty brief" half of the bargain
    assert GENUINE_OPEN <= {a["contact_name"] for a in active}
    # the group ask is down to its two invented labels, from six rows
    group = [a for a in active if "Ridge / Anvil" in a["summary"]]
    assert len(group) == 2
    # one sync, one row; one call request, one row
    assert len([a for a in active if a["contact_name"] == "Jonas Getty"]) == 1
    assert len([a for a in active if a["contact_name"] == "Pegah Ebrahim"]) == 1
    # the nameless rows are closed, not silently folded into each other
    closed = [f for f in _all_rows(tmp_path) if f.get("resolution") == "anchorless"]
    assert len(closed) == 7
    assert all(f["status"] in cr.TERMINAL for f in closed)
    # …and running it again changes nothing more
    again = cr.resolve({"today": TODAY}, NOW)
    assert {a["anchor_key"] for a in again["active"]} == {a["anchor_key"] for a in active}


def test_two_files_under_one_anchor_key_are_folded_not_hidden(tmp_path, monkeypatch):
    """The duplicate nobody could see. A migration re-anchors a row in place, so two FILES can end
    up carrying one anchor_key — and the resolver's dict kept whichever sorted last and dropped the
    other. The dropped file was never resolved, never expired, never pruned, but every read view
    reads FILES, so the brief went on showing it forever."""
    _env(tmp_path, monkeypatch)
    key = "follow_up:id:dana@acme.example"
    _row(tmp_path, key, action_type="reply", channel="gmail", contact_name="Dana Reyes",
         contact_identifier="dana@acme.example", summary="the older ask", created_at="2026-08-03",
         chased_count=1, times_surfaced=4)
    d = tmp_path / "knowledge" / "continuity"
    (d / "a-second-file.md").write_text("---\n" + yaml.safe_dump(
        {"anchor_key": key, "status": "open", "action_type": "reply", "channel": "gmail",
         "contact_name": "Dana Reyes", "contact_identifier": "dana@acme.example",
         "summary": "the newer ask", "created_at": "2026-08-07", "times_surfaced": 1},
        sort_keys=False) + "---\n")

    out = cr.resolve({"today": TODAY}, NOW)
    assert len(out["active"]) == 1
    it = out["active"][0]
    assert it["created_at"] == "2026-08-03"     # the older row is the debt's real age
    assert it["summary"] == "the newer ask"     # the newer words are the live ask
    assert it["chased_count"] == 1              # a delivered nudge is never un-sent by a fold
    loser = [f for f in _all_rows(tmp_path) if f.get("resolution") == "merged_duplicate"]
    assert len(loser) == 1 and loser[0]["merged_into"] == key


def _all_rows(tmp_path) -> list:
    d = tmp_path / "knowledge" / "continuity"
    return [yaml.safe_load(p.read_text().split("---\n")[1]) for p in sorted(d.glob("*.md"))]
