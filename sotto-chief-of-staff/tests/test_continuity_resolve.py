"""continuity_resolve.py — dedicated edge-case suite for the cross-channel resolution "moat":
reply-on-another-channel matching (phone last-10 / email / WhatsApp JID), anchor_key dedup,
7-day expiry, snoozed_until, deadline grace, and meeting-passed incl. the near-midnight
UTC-offset cases (the strptime(...[:19]) off-by-one this suite pins the fix for)."""
import importlib.util
import os
import re
import sys
from datetime import datetime

import pytest
import yaml

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location(
    "cr_edge", os.path.join(ROOT, "morning-brief", "scripts", "continuity_resolve.py"))
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)

NOW = datetime(2026, 6, 24, 9, 0, 0)   # naive on purpose — callers pass naive datetimes today


def _env(tmp_path, monkeypatch, tz="+00:00"):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", tz)


def _all_fm(tmp_path) -> list:
    """Every ledger file's frontmatter, terminal ones included (what's actually on disk)."""
    d = tmp_path / "knowledge" / "continuity"
    return [yaml.safe_load(p.read_text().split("---\n")[1]) for p in sorted(d.glob("*.md"))]


def _loop(tmp_path, key, **fm):
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True, exist_ok=True)
    fm.setdefault("anchor_key", key)
    fm.setdefault("status", "open")
    fm.setdefault("action_type", "reply")
    fm.setdefault("contact_name", "Someone")
    fm.setdefault("contact_identifier", "+14155550000")
    fm.setdefault("created_at", "2026-06-23")
    # Files are named the way the resolver names them (`_safe`), so a fixture may use any real
    # anchor_key — including one with a "/" in it, which a real `name:` anchor can carry.
    name = key if key.replace("-", "").replace("_", "").isalnum() else cr._safe(key)
    (d / f"{name}.md").write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n")


# ── cross-channel reply matching (phone last-10 / email / JID) ─────────────────







def test_a_briefs_own_action_is_stamped_with_the_brief_that_minted_it(tmp_path, monkeypatch):
    """`source_brief_at` is what the evening's accountability block reads ("this morning's brief
    flagged X — here's what happened"); it had no writer. A SOURCED action (a Granola commitment)
    is not the brief's and is not stamped."""
    _env(tmp_path, monkeypatch)
    out = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"action_type": "reply", "channel": "gmail", "contactName": "Victor",
         "contactIdentifier": "victor@acme.com", "contextSummary": "the allocation decision"},
        {"action_type": "reply", "channel": "gmail", "contactName": "Dana",
         "contactIdentifier": "dana@acme.com", "contextSummary": "the deck she promised",
         "source": "followup_commitment"}]}, NOW, resolve_existing=False)
    rows = {r["contact_name"]: r for r in _all_fm(tmp_path)}
    assert str(rows["Victor"]["source_brief_at"]).startswith("2026-06-24 ")
    assert "source_brief_at" not in rows["Dana"]
    assert len(out["active"]) == 2




def test_promised_dates_are_read_from_plain_words():
    ref = datetime(2026, 6, 24, 9, 0, 0)          # a Wednesday
    p = cr.promised_date
    assert p("I'll send it tomorrow", ref) == "2026-06-25"
    assert p("should have it to you by Friday", ref) == "2026-06-26"
    assert p("on Wednesday at the latest", ref) == "2026-07-01"          # the NEXT Wednesday
    assert p("by end of week", ref) == "2026-06-26"
    assert p("next week for sure", ref) == "2026-07-01"
    assert p("before the 16th of July", ref) == "2026-07-16"
    assert p("by Aug 3", ref) == "2026-08-03"
    assert p("by March 2", ref) == "2027-03-02"                          # already passed → next year
    assert p("legal has it, no date yet", ref) is None and p("", ref) is None


def test_an_rsvp_ask_never_opens_a_ledger_row(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    out = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"type": "rsvp", "channel": "calendar", "contactName": "Coffee with Priya",
         "contactIdentifier": "ev-unanswered", "contextSummary": "You haven't answered the invite"}]},
        NOW, resolve_existing=False)
    assert out["active"] == [] and _all_fm(tmp_path) == []




def test_incoming_or_earlier_messages_do_not_resolve(tmp_path, monkeypatch):
    # Neither an INCOMING message from them nor an outgoing one from BEFORE the loop counts.
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", contact_identifier="+14155552222", created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "imessage": [
            {"is_from_me": False, "handle": "4155552222", "timestamp": "2026-06-23 20:00:00"},
            {"is_from_me": True, "handle": "4155552222", "timestamp": "2026-06-23 07:00:00"}]}}, NOW)
    assert out["resolved"] == [] and len(out["active"]) == 1


def test_outgoing_reply_never_closes_something_the_other_person_owes(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "w", action_type="waiting_on", contact_identifier="+14155552222",
          created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "imessage": [{"is_from_me": True, "handle": "4155552222",
                      "timestamp": "2026-06-23 20:00:00", "text": "checking in"}]}}, NOW)
    assert out["resolved"] == [] and [a["anchor_key"] for a in out["active"]] == ["w"]


def test_source_backed_explicit_commitment_only_closes_explicitly(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "g", resolution_mode="explicit", source="followup_commitment",
          contact_identifier="+14155552222", created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-07-20", "local": {
        "imessage": [{"is_from_me": True, "handle": "4155552222",
                      "timestamp": "2026-06-23 20:00:00", "text": "done"}]}},
        datetime(2026, 7, 20, 9, 0, 0))
    assert out["resolved"] == [] and out["expired"] == []
    assert [a["anchor_key"] for a in out["active"]] == ["g"]


def test_short_or_mismatched_numbers_never_false_positive(tmp_path, monkeypatch):
    # <7-digit identifiers and different last-10s must not match (the _phone_matches guard).
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", contact_identifier="+14155552222", created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "imessage": [{"is_from_me": True, "handle": "555222", "timestamp": "2026-06-23 20:00:00"},
                     {"is_from_me": True, "handle": "+14155559999", "timestamp": "2026-06-23 20:00:00"}]}}, NOW)
    assert out["resolved"] == []


# ── the answer gate, outgoing: a bare pleasantry is contact, not payment ─────────
# Same cutoff as the inbound mirror (`_inbound_cutoff`); a LOWER content bar than the delivery one,
# because direction decides how a debt the user owes closes: any real message is an answer, and only
# a pleasantry on its own ("Happy birthday!", "thanks!", "👍") is not. ANY outgoing message used to
# close the loop.

def test_short_real_replies_still_close_the_debt_and_bare_pleasantries_do_not():
    # The predicate on its own, because the fixtures above and below only cover the two ends.
    for answer in ("sent the LOI", "Thursday works!", "yes", "I'll send it Friday",
                   "Happy birthday! deck coming Friday", "Confirmed, see you Tuesday."):
        assert cr._is_answer(answer), answer
    for contact in ("Happy birthday!", "happy new year", "thanks so much", "Thanks!", "ok", "👍",
                    "🎉🎉", "congrats!!", "lol", "hope you're well", ""):
        assert not cr._is_answer(contact), contact

def test_a_birthday_text_does_not_close_a_document_you_owe(tmp_path, monkeypatch):
    # The replay probe's `birthday_is_not_fulfillment`: "Happy birthday!" paid off the pricing doc.
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", contact_identifier="+14155552222", created_at="2026-06-23 08:00:00",
          summary="Send the pricing document")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "imessage": [{"is_from_me": True, "handle": "4155552222",
                      "timestamp": "2026-06-23 20:00:00", "text": "Happy birthday!"}]}}, NOW)
    assert out["resolved"] == [] and [a["anchor_key"] for a in out["active"]] == ["k"]








# ── anchor_key dedup ───────────────────────────────────────────────────────────

def test_anchor_dedup_across_phone_formats_does_not_claim_delivery(tmp_path, monkeypatch):
    # The same owed reply re-extracted next brief with a differently-formatted phone → ONE loop,
    # dedupes without claiming either model proposal was delivered to the user.
    _env(tmp_path, monkeypatch)
    cr.resolve({"today": "2026-06-23", "new_actions": [
        {"type": "reply", "channel": "imessage", "contactName": "Jo",
         "contactIdentifier": "+1 (415) 555-1234", "contextSummary": "Jo asked about Thursday"}]},
        datetime(2026, 6, 23, 9, 0, 0))
    out = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"type": "reply", "channel": "imessage", "contactName": "Jo",
         "contactIdentifier": "4155551234", "contextSummary": "Jo asked about Thursday"}]}, NOW)
    assert len(out["active"]) == 1
    assert "times_surfaced" not in out["active"][0]
    assert "delivery_surface" not in out["active"][0]
    files = list((tmp_path / "knowledge" / "continuity").glob("*.md"))
    assert len(files) == 1


def test_anchor_thread_id_beats_contact_and_family_groups_types(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    # thread id wins regardless of contact fields
    a = cr.compute_anchor_key(cr._normalize_action(
        {"type": "reply", "channel": "gmail", "contactName": "X", "emailThreadId": "T1"}))
    assert a == "thread:T1"
    # reply vs follow_up vs call_back collapse into one follow_up family per person
    k1 = cr.compute_anchor_key({"channel": "imessage", "action_type": "reply",
                                "contact_identifier": "+14155551234"})
    k2 = cr.compute_anchor_key({"channel": "imessage", "action_type": "follow_up",
                                "contact_identifier": "1 (415) 555-1234"})
    assert k1 == k2


# ── 7-day age expiry ───────────────────────────────────────────────────────────



def test_cutoffs_derive_from_payload_today_not_wall_clock(tmp_path, monkeypatch):
    # Regression (the dry_run time-bomb): with now=None the REAL clock may be weeks past the
    # payload's `today`; expiry must still reference `today`, so a replayed fixture is stable.
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", contact_name="Fixture", created_at="2026-06-23")
    out = cr.resolve({"today": "2026-06-24"})            # now=None → wall clock (2026-07+)
    assert out["expired"] == []
    assert [a["contact_name"] for a in out["active"]] == ["Fixture"]


# ── snoozed_until ─────────────────────────────────────────────────────────────

def test_snoozed_loop_hidden_then_resurfaces(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", contact_name="Zoe", created_at="2026-06-23", snoozed_until="2026-06-27")
    hidden = cr.resolve({"today": "2026-06-24"}, NOW)
    assert hidden["active"] == [] and hidden["expired"] == [] and hidden["resolved"] == []
    assert (tmp_path / "knowledge" / "continuity" / "k.md").exists()   # kept on disk
    back = cr.resolve({"today": "2026-06-28"}, datetime(2026, 6, 28, 9, 0, 0))
    assert [a["contact_name"] for a in back["active"]] == ["Zoe"]




# ── deadline grace ─────────────────────────────────────────────────────────────



def test_old_loop_with_a_future_deadline_does_not_age_expire(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "future", contact_name="Future", created_at="2026-06-01",
          deadline="2026-07-01")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert out["expired"] == []
    assert [a["anchor_key"] for a in out["active"]] == ["future"]


# ── meeting passed, incl. near-midnight offsets (the strptime[:19] fix) ────────

def test_meeting_passed_utc_stamp_is_yesterday_in_la(tmp_path, monkeypatch):
    # 2026-06-25T06:30:00Z == 2026-06-24 23:30 in LA. On the user's 06-25 the meeting is PAST.
    # The old code compared the raw "2026-06-25" date part → wrongly still pending.
    _env(tmp_path, monkeypatch, tz="America/Los_Angeles")
    assert cr.meeting_passed("2026-06-25T06:30:00Z", "2026-06-20", "2026-06-25") is True


def test_meeting_passed_utc_stamp_is_tomorrow_in_tokyo(tmp_path, monkeypatch):
    # 2026-06-24T16:00:00Z == 2026-06-25 01:00 in Tokyo. On the user's 06-25 that meeting is TODAY
    # — not passed. The old code took "2026-06-24" and wrongly resolved it a day early.
    _env(tmp_path, monkeypatch, tz="Asia/Tokyo")
    assert cr.meeting_passed("2026-06-24T16:00:00Z", "2026-06-20", "2026-06-25") is False


def test_meeting_passed_explicit_offset_and_naive_forms(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, tz="America/Los_Angeles")
    # explicit -07:00 offset: already LA-local, no shift
    assert cr.meeting_passed("2026-06-24T23:30:00-07:00", "2026-06-20", "2026-06-25") is True
    assert cr.meeting_passed("2026-06-25T08:00:00-07:00", "2026-06-20", "2026-06-25") is False
    # naive forms are treated as user-local (unchanged behavior)
    assert cr.meeting_passed("2026-06-24 10:00", "2026-06-20", "2026-06-25") is True
    assert cr.meeting_passed("2026-06-25", "2026-06-20", "2026-06-25") is False
    # relative forms still compare against created_at
    assert cr.meeting_passed("Tomorrow 3pm", "2026-06-23", "2026-06-25") is True
    assert cr.meeting_passed("Tomorrow 3pm", "2026-06-25", "2026-06-25") is False


def test_meeting_resolves_not_expires_with_offset_time(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, tz="America/Los_Angeles")
    _loop(tmp_path, "m", action_type="meeting_prep", channel="calendar", contact_name="Pitch",
          contact_identifier="ev1", created_at="2026-06-23", meeting_time="2026-06-24T06:30:00Z")
    out = cr.resolve({"today": "2026-06-24"}, NOW)   # 06-24T06:30Z = 06-23 23:30 LA → passed
    assert [(r["resolution"], r["status"]) for r in out["resolved"]] == [("meeting_passed", "resolved")]


def test_legacy_hyphen_meeting_type_still_resolves_when_the_meeting_passes(tmp_path, monkeypatch):
    """Legacy entries spell it "meeting-prep". Comparing the RAW action_type stranded them open
    forever — the resolver normalizes first, like every other action-type test in the funnel."""
    _env(tmp_path, monkeypatch, tz="America/Los_Angeles")
    _loop(tmp_path, "m", action_type="meeting-prep", channel="calendar", contact_name="Pitch",
          contact_identifier="ev1", created_at="2026-06-23", meeting_time="2026-06-24T06:30:00Z")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert [(r["resolution"], r["status"]) for r in out["resolved"]] == [("meeting_passed", "resolved")]


# ── malformed ledger files are never surfaced, never persisted over ───────────

def test_malformed_ledger_file_skipped_and_left_untouched(tmp_path, monkeypatch, capsys):
    # Regression: a broken frontmatter used to parse to {} → treated as a status-less open item →
    # _persist REWROTE the file as '---\n{}\n---', destroying the content. It must be skipped
    # entirely (not active, not resolved, not expired) and the bytes left exactly as they were.
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "good", contact_name="Fine", created_at="2026-06-23")
    d = tmp_path / "knowledge" / "continuity"
    broken = d / "broken.md"
    original = "---\n[broken: yaml\n---\nprecious hand-written notes\n"
    broken.write_text(original)
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert [a["contact_name"] for a in out["active"]] == ["Fine"]
    assert out["resolved"] == [] and out["expired"] == []
    assert broken.read_text() == original                    # file content UNCHANGED
    assert "broken.md" in capsys.readouterr().err            # one-line stderr warning names it


# ── type-safe slicing of raw YAML values (unquoted dates, explicit nulls) ─────

def test_unquoted_yaml_dates_and_nulls_do_not_crash_resolution(tmp_path, monkeypatch):
    # yaml.safe_load yields datetime.date for unquoted dates and None for explicit nulls; slicing
    # those raw killed the whole continuity step with a TypeError.
    _env(tmp_path, monkeypatch)
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True, exist_ok=True)
    (d / "dates.md").write_text(
        "---\nanchor_key: dates\nstatus: open\naction_type: reply\ncontact_name: Datey\n"
        "contact_identifier: '+14155550000'\ncreated_at: 2026-06-23\ndeadline: null\n"
        "snoozed_until: null\n---\n")                        # created_at parses as datetime.date
    (d / "done.md").write_text(
        "---\nanchor_key: done\nstatus: resolved\ncontact_name: Done\n"
        "resolved_at: 2026-06-23\n---\n")                    # terminal + unquoted resolved_at date
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert [a["contact_name"] for a in out["active"]] == ["Datey"]
    assert (d / "done.md").exists()                          # within retention → kept
    # …and the CLI (which json.dumps the result carrying the raw date values) survives too
    import json as _json
    import subprocess
    payload = tmp_path / "cont.json"
    payload.write_text('{"today": "2026-06-24"}')
    env = dict(os.environ, SOTTO_DATA=str(tmp_path), SOTTO_TIMEZONE="+00:00")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "morning-brief", "scripts", "continuity_resolve.py"),
         str(payload)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    cli_out = _json.loads(proc.stdout)
    assert [a["contact_name"] for a in cli_out["active"]] == ["Datey"]
    assert cli_out["active"][0]["created_at"] == "2026-06-23"   # date → ISO string in JSON


def test_s_stringifies_dates_as_iso():
    from datetime import date as _date
    assert cr._s(_date(2026, 6, 23)) == "2026-06-23"
    assert cr._s(datetime(2026, 6, 23, 10, 0, 0))[:10] == "2026-06-23"
    assert cr._s(None) == "" and cr._s("x") == "x"


# ── calendar-event scheduling resolution (offset starts, naive now) ────────────



def test_calendar_event_outside_14d_window_does_not_resolve(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", contact_name="Dana", contact_identifier="dana@x.com",
          created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "calendar_events": [{"summary": "Far", "start": "2026-08-30T10:00:00Z",
                             "attendees": [{"email": "dana@x.com"}]}]}}, NOW)
    assert out["resolved"] == [] and len(out["active"]) == 1


# ── bare/empty ledger files are never adopted or destroyed ─────────────────────

def test_bare_md_file_is_left_untouched_and_never_surfaced(tmp_path, monkeypatch):
    # A frontmatter-less .md in the continuity dir used to load as {}, surface as a content-free
    # open loop, and get rewritten by _persist as '---\n{}\n---' — destroying its contents.
    _env(tmp_path, monkeypatch)
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True, exist_ok=True)
    content = "# my precious notes\n\nnot a ledger entry at all\n"
    (d / "notes.md").write_text(content)
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert out["active"] == [] and out["resolved"] == [] and out["expired"] == []
    assert (d / "notes.md").read_text() == content            # byte-for-byte untouched


def test_empty_frontmatter_file_is_not_adopted(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True, exist_ok=True)
    (d / "empty.md").write_text("---\n{}\n---\nbody text survives\n")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert out["active"] == []                                # no content-free {} loop
    assert "body text survives" in (d / "empty.md").read_text()


# ── terminal anchors re-open when the person comes back ────────────────────────



def test_terminal_anchor_without_new_action_stays_terminal(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "imessage:follow_up:id:4155550000", status="resolved", resolution="replied",
          resolved_at="2026-06-23")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert out["active"] == [] and out["resolved"] == []       # untouched, not re-opened






# ── reply_* type variants (the FLEX schema leaves `type` a free string) ────────





def test_reply_variants_collapse_to_one_anchor_family(tmp_path, monkeypatch):
    # reply vs reply_message vs reply_email must produce the SAME anchor key, so the same owed
    # reply re-extracted under a variant spelling dedupes without counting model proposals.
    base = {"channel": "imessage", "contact_identifier": "+14155551234", "contact_name": "Jo"}
    keys = {cr.compute_anchor_key({**base, "action_type": t})
            for t in ("reply", "reply_message", "reply_email", "call-back", "followup")}
    assert len(keys) == 1
    # NOTE (not migrated by design): a volume written BEFORE this fix can hold both-family
    # duplicates for the same person (e.g. …:reply_message:… next to …:follow_up:…). They are
    # left as-is — the variant file stops being re-surfaced under its old key and ages out via
    # the 7-day expiry, while all NEW extractions dedupe into the canonical family key.
    _env(tmp_path, monkeypatch)
    cr.resolve({"today": "2026-06-23", "new_actions": [
        {"type": "reply", "channel": "imessage", "contactName": "Jo",
         "contactIdentifier": "+14155551234", "contextSummary": "Jo asked about Thursday"}]},
        datetime(2026, 6, 23, 9, 0, 0))
    out = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"type": "reply_message", "channel": "imessage", "contactName": "Jo",
         "contactIdentifier": "+14155551234", "contextSummary": "Jo asked about Thursday"}]}, NOW)
    assert len(out["active"]) == 1
    assert "times_surfaced" not in out["active"][0]
    assert "delivery_surface" not in out["active"][0]


# ── MCP tool-result wrapper around `local` (shared unwrap) ─────────────────────



def test_meeting_actions_never_create_ledger_entries(tmp_path, monkeypatch):
    # The ledger holds communication debts; meeting prep/info are calendar shadows (owner-reported
    # pollution: the Loops view mirrored tomorrow's schedule). Upsert must skip them entirely.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    payload = {
        "today": "2026-08-06",
        "new_actions": [
            {"type": "meeting_prep", "channel": "calendar", "contactName": "Ronak Trivedi",
             "contactIdentifier": "evt_1", "contextSummary": "Sync with Ronak tomorrow"},
            {"type": "meeting_info", "channel": "calendar", "contactName": "Ryan Walker",
             "contactIdentifier": "evt_2", "contextSummary": "Intro call tomorrow"},
            {"type": "reply", "channel": "email", "contactName": "Sarah Chen",
             "contactIdentifier": "sarah@acme.com", "contextSummary": "Reply about the deck"},
        ],
        "local": {}, "events": [],
    }
    cr.resolve(payload)
    cont_dir = tmp_path / "knowledge" / "continuity"
    files = list(cont_dir.glob("*.md")) if cont_dir.exists() else []
    texts = "\n".join(f.read_text() for f in files)
    assert "Sarah Chen" in texts                      # the real debt persists
    assert "Ronak Trivedi" not in texts               # calendar shadows never become loops
    assert "Ryan Walker" not in texts


# ── the waiting-on chase: inbound delivery closes it, aging never does ─────────
# One sentence: what you owe expires quietly; what you're owed gets chased.

def _waiting(tmp_path, key="w", **fm):
    fm.setdefault("action_type", "waiting_on")
    fm.setdefault("contact_name", "Acme")
    fm.setdefault("summary", "the signed contract")
    _loop(tmp_path, key, **fm)








def test_email_delivery_requires_counterpart_thread_substance_and_inbound_direction(tmp_path, monkeypatch):
    cases = [
        {"from": "Dana <dana@acme.com>", "threadId": "wrong", "date": "2026-06-23T20:00:00-07:00",
         "body": "Here is the signed agreement https://x.example/a.pdf"},
        {"from": "Other <other@acme.com>", "threadId": "thread-1", "date": "2026-06-23T20:00:00-07:00",
         "body": "Here is the signed agreement https://x.example/a.pdf"},
        {"from": "Dana <dana@acme.com>", "threadId": "thread-1", "date": "2026-06-23T20:00:00-07:00",
         "body": "I'll send it tomorrow"},
        {"from": "Me <me@mine.com>", "to": "dana@acme.com", "threadId": "thread-1",
         "date": "2026-06-23T20:00:00-07:00", "body": "Here is the agreement https://x.example/a.pdf",
         "isSent": True},
    ]
    for email in cases:
        _env(tmp_path, monkeypatch)
        for f in (tmp_path / "knowledge" / "continuity").glob("*.md"):
            f.unlink()
        _waiting(tmp_path, channel="email", contact_name="Dana", contact_identifier="dana@acme.com",
                 source_thread_id="thread-1", created_at="2026-06-23 08:00:00")
        out = cr.resolve({"today": "2026-06-24", "emails": [email]}, NOW)
        assert out["resolved"] == [], email


def test_a_date_only_created_at_never_closes_on_its_own_day(tmp_path, monkeypatch):
    # The 09:00 chat that PRECEDED the 16:00 promise used to close the debt on the day it was
    # recorded: "2026-06-23" sorts before every timestamp of that day. A date-only created_at could
    # be any hour of it, so nothing from that day counts as delivery.
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, contact_identifier="+14155552222", created_at="2026-06-23")
    payload = {"today": "2026-06-24", "local": {"imessage": [
        {"is_from_me": False, "handle": "4155552222", "timestamp": "2026-06-23 09:00:00",
         "text": "the offsite moved to Thursday, can you confirm with the venue?"}]}}
    out = cr.resolve(payload, NOW)
    assert out["resolved"] == [] and len(out["active"]) == 1
    # …and a NEW item stamps a full local timestamp, so it is its own honest cutoff.
    _env(tmp_path, monkeypatch)
    for f in (tmp_path / "knowledge" / "continuity").glob("*.md"):
        f.unlink()
    out = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"action_type": "waiting_on", "channel": "imessage", "contactName": "Acme",
         "contactIdentifier": "+14155552222", "contextSummary": "the deck they promised"}],
        "local": {"imessage": [
            {"is_from_me": False, "handle": "4155552222", "timestamp": "2026-06-24 08:00:00",
             "text": "the offsite moved to Thursday, can you confirm with the venue?"}]}}, NOW)
    assert out["active"][0]["created_at"] == "2026-06-24 09:00:00"
    assert out["resolved"] == []                # 08:00 predates the 09:00 promise — still open


def test_bare_ack_or_promise_never_closes_a_waiting_on(tmp_path, monkeypatch):
    # "ok" is not a deliverable, and neither is "I'll send it tomorrow" — the whole point of the
    # conservative gate: a stale chase candidate beats a debt closed behind the user's back.
    _env(tmp_path, monkeypatch)
    for text in ("ok", "👍", "will do!", "I'll send the deck tomorrow, sorry for the delay"):
        _env(tmp_path, monkeypatch)
        for f in (tmp_path / "knowledge" / "continuity").glob("*.md"):
            f.unlink()
        _waiting(tmp_path, contact_identifier="+14155552222", created_at="2026-06-23 08:00:00")
        out = cr.resolve({"today": "2026-06-24", "local": {
            "imessage": [{"is_from_me": False, "handle": "4155552222",
                          "timestamp": "2026-06-23 20:00:00", "text": text}]}}, NOW)
        assert out["resolved"] == [], text
        assert len(out["active"]) == 1, text


def test_inbound_from_the_wrong_person_or_before_creation_never_closes(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, contact_identifier="+14155552222", created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {"imessage": [
        # right substance, wrong number
        {"is_from_me": False, "handle": "+14155559999", "timestamp": "2026-06-23 20:00:00",
         "text": "here is the signed contract, all done https://x.co/c"},
        # right person, but from BEFORE the loop was created
        {"is_from_me": False, "handle": "4155552222", "timestamp": "2026-06-23 07:00:00",
         "text": "here is the signed contract, all done https://x.co/c"},
        # and the user's own outgoing message is not a delivery to them
        {"is_from_me": True, "handle": "4155552222", "timestamp": "2026-06-23 21:00:00",
         "text": "here is the signed contract, all done https://x.co/c"}]}}, NOW)
    assert out["resolved"] == [] and len(out["active"]) == 1








def test_chase_stamp_ripens_at_the_knob_and_stops_after_two(tmp_path, monkeypatch):
    # Two phases: resolve() PROPOSES (chase_pending), --finalize-chase COUNTS it once delivered.
    _env(tmp_path, monkeypatch)
    monkeypatch.setenv("SOTTO_CHASE_AFTER_DAYS", "3")
    _waiting(tmp_path, "w", created_at="2026-06-21")           # 2 days old on 06-23
    out = cr.resolve({"today": "2026-06-23"}, datetime(2026, 6, 23, 9, 0, 0))
    assert "chase_pending" not in out["active"][0]             # too young — no chase yet
    out = cr.resolve({"today": "2026-06-24"}, NOW)             # 3 days old → chase #1 proposed
    it = out["active"][0]
    assert it["chase_pending"] == "2026-06-24" and "chased_count" not in it
    assert cr.finalize_chase("w", NOW)["chased_count"] == 1    # …and delivered
    it = cr.resolve({"today": "2026-06-24"}, NOW)["active"][0]
    assert it["chased_count"] == 1 and it["last_chased_at"] == "2026-06-24"
    assert it["chase_after"] == "2026-06-27" and "chase_pending" not in it
    # same day, a second pass (evening brief) must not chase again
    assert cr.resolve({"today": "2026-06-24"}, NOW)["active"][0]["chased_count"] == 1
    # …nor before chase_after ripens
    assert "chase_pending" not in cr.resolve({"today": "2026-06-26"},
                                             datetime(2026, 6, 26, 9, 0, 0))["active"][0]
    two = cr.resolve({"today": "2026-06-27"}, datetime(2026, 6, 27, 9, 0, 0))["active"][0]
    assert two["chase_pending"] == "2026-06-27" and two["chased_count"] == 1
    assert cr.finalize_chase("w", datetime(2026, 6, 27, 9, 0, 0))["chased_count"] == 2
    # after CHASE_MAX the item stays open but is never chased again — sotto-loops' cleanup lane now
    handed_off = cr.resolve({"today": "2026-07-05"}, datetime(2026, 7, 5, 9, 0, 0))["active"][0]
    assert handed_off["chased_count"] == 2 and handed_off["status"] == "open"
    assert "chase_pending" not in handed_off
    assert cr.chase_due(handed_off, "2026-07-05", datetime(2026, 7, 5)) is False


def test_an_undelivered_chase_expires_without_being_counted(tmp_path, monkeypatch):
    # The nudge lane was down (quiet hours / budget spent / cron off): the proposal simply lapses.
    # A chase the user never saw must never be one of the two they get.
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, "w", created_at="2026-06-14")
    assert cr.resolve({"today": "2026-06-24"}, NOW)["active"][0]["chase_pending"] == "2026-06-24"
    nxt = cr.resolve({"today": "2026-06-25"}, datetime(2026, 6, 25, 9, 0, 0))["active"][0]
    assert nxt["chase_pending"] == "2026-06-25"        # re-proposed today…
    assert "chased_count" not in nxt                   # …and yesterday's burnt nothing
    # finalize refuses to count a chase that was never pending today
    assert cr.finalize_chase("w", datetime(2026, 6, 26, 9, 0, 0))["ok"] is False


def test_finalize_chase_is_idempotent_within_the_day(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, "w", created_at="2026-06-14")
    cr.resolve({"today": "2026-06-24"}, NOW)
    assert cr.finalize_chase("w", NOW)["chased_count"] == 1
    again = cr.finalize_chase("w", NOW)
    assert again["ok"] is True and again["chased_count"] == 1
    assert cr.finalize_chase("nope", NOW)["ok"] is False


def test_only_one_waiting_on_is_stamped_per_day_most_overdue_first(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, "old", contact_name="Older", created_at="2026-06-14")
    _waiting(tmp_path, "late", contact_name="Overdue", created_at="2026-06-20",
             deadline="2026-06-22")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    chased = [a["contact_name"] for a in out["active"] if a.get("chase_pending")]
    assert chased == ["Overdue"]                               # overdue outranks merely old


def test_post_deadline_waiting_on_escalates_immediately(tmp_path, monkeypatch):
    # Inside the 2-day deadline grace: too young for the 3-day clock, but already late.
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, "w", created_at="2026-06-23", deadline="2026-06-23")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert out["active"][0]["chase_pending"] == "2026-06-24"






def test_chase_knob_is_read_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("SOTTO_CHASE_AFTER_DAYS", raising=False)
    assert cr.chase_after_days() == 3
    monkeypatch.setenv("SOTTO_CHASE_AFTER_DAYS", "5")
    assert cr.chase_after_days() == 5
    monkeypatch.setenv("SOTTO_CHASE_AFTER_DAYS", "junk")
    assert cr.chase_after_days() == 3




# ── direction is load-bearing, so direction gets its own anchor ────────────────

def test_a_delegation_and_a_reply_owed_to_the_same_person_are_two_debts(tmp_path, monkeypatch):
    """A reply you owe them and a deliverable they owe you are two debts, even on one thread. They
    used to share the follow_up family, so whichever landed first fixed the direction forever —
    and Sotto would chase the person who was waiting on the USER."""
    _env(tmp_path, monkeypatch)
    out = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"type": "reply", "channel": "imessage", "contactName": "Sarah",
         "contactIdentifier": "+14155551234", "contextSummary": "she asked about Thursday"},
        {"type": "waiting_on", "channel": "imessage", "contactName": "Sarah",
         "contactIdentifier": "+14155551234", "contextSummary": "Sarah owes the contract"}]}, NOW)
    by_type = {a["action_type"]: a for a in out["active"]}
    assert set(by_type) == {"reply", "waiting_on"}
    assert by_type["waiting_on"]["anchor_key"] == "waiting_on:id:4155551234"
    assert by_type["reply"]["anchor_key"] == "follow_up:id:4155551234"
    # a thread carries one debt per direction too
    assert cr.compute_anchor_key({"source_thread_id": "T", "action_type": "reply"}) == "thread:T"
    assert cr.compute_anchor_key({"source_thread_id": "T",
                                  "action_type": "waiting_on"}) == "thread:T:waiting_on"




# ── the two passes: resolve before the brief, merge after it ───────────────────



def test_merge_only_writes_the_new_action_and_resolves_nothing(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "old", action_type="reply", contact_name="Ancient", created_at="2026-06-01")
    out = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"type": "reply", "channel": "imessage", "contactName": "New",
         "contactIdentifier": "+14155559999", "contextSummary": "New asked for the memo"}]}, NOW, resolve_existing=False)
    assert out["resolved"] == [] and out["expired"] == []               # no resolution pass
    assert {a["contact_name"] for a in out["active"]} == {"Ancient", "New"}
    # the merged item really landed on disk; the untouched one was NOT rewritten
    files = sorted(p.name for p in (tmp_path / "knowledge" / "continuity").glob("*.md"))
    assert len(files) == 2


# ── a group ask is keyed by the GROUP'S OWN ID ────────────────────────────────
# One sentence: a group ask is keyed by the group's ID and a person ask by the person — never by
# what the extractor typed that day. iMessage gives every group a `chat_guid` and WhatsApp gives it
# a `…@g.us` JID; before this, a group was the one counterpart with no identifier at all, so its
# anchor fell back to `name:<the label the model invented>` and one debt became two open rows.

GROUP_GUID = "iMessage;+;chat9911"
GROUP_LOCAL = {"imessage": [
    {"handle": "+14155551111", "is_from_me": False, "timestamp": "2026-06-23 10:00:00",
     "text": "anyone know people at Insight Partners?", "is_group_chat": True,
     "chat_guid": GROUP_GUID, "group_name": "FPV / Piston",
     "group_participants": ["+14155551111", "+14155552222"]}]}


def _group_ask(name, summary, identifier=None, **extra):
    a = {"type": "waiting_on", "channel": "imessage", "contactName": name,
         "contextSummary": summary}
    if identifier:
        a["contactIdentifier"] = identifier
    a.update(extra)
    return a




def test_a_group_id_counts_only_when_the_snapshot_actually_contains_it(tmp_path, monkeypatch):
    """Source-verified, never model-asserted: an id this brief's data never showed is not identity,
    so the action keeps today's behavior instead of anchoring on something invented."""
    _env(tmp_path, monkeypatch)
    out = cr.resolve({"today": "2026-06-24", "local": GROUP_LOCAL, "new_actions": [
        _group_ask("Some Group", "asked the group", identifier="iMessage;+;chatMADEUP")]}, NOW)
    assert out["active"][0]["anchor_key"] == "waiting_on:id:imessage;+;chatmadeup"
    assert not out["active"][0].get("group_id")




def test_a_person_ask_is_untouched_by_group_identity(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    out = cr.resolve({"today": "2026-06-24", "local": GROUP_LOCAL, "new_actions": [
        {"type": "waiting_on", "channel": "imessage", "contactName": "Sarah",
         "contactIdentifier": "+14155551234", "contextSummary": "the contract Sarah owes"},
        {"type": "reply", "channel": "gmail", "contactName": "Morgan",
         "contactIdentifier": "m@x.com", "emailThreadId": "thr_1", "contextSummary": "the deck Morgan sent"}]}, NOW)
    # Morgan's thread id no longer outranks Morgan: a person-directed debt is keyed by the person.
    assert {a["anchor_key"] for a in out["active"]} == {
        "waiting_on:id:4155551234", "follow_up:id:m@x.com"}


# ── the migration: one idempotent heal, and a dedupe is never a receipt ────────



def test_the_migration_is_idempotent(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, "imessage:waiting_on:name:fpv /", channel="imessage",
             contact_name="FPV / Piston", contact_identifier=None, summary="asked about Insight",
             created_at="2026-06-21")
    first = cr.resolve({"today": "2026-06-24", "local": GROUP_LOCAL}, NOW)
    second = cr.resolve({"today": "2026-06-24", "local": GROUP_LOCAL}, NOW)
    assert [a["anchor_key"] for a in first["active"]] == [a["anchor_key"] for a in second["active"]]
    assert first["active"][0]["created_at"] == second["active"][0]["created_at"]
    assert len(_all_fm(tmp_path)) == 1                   # no second file, no second fold


def test_the_migration_never_resurrects_a_closed_loop(tmp_path, monkeypatch):
    """The user already closed this group's debt. A label-keyed row must not be folded into it (that
    would rewrite a terminal loop) — the duplicate retires and the original closure stays intact."""
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, f"imessage:waiting_on:gid:{GROUP_GUID.lower()}", channel="imessage",
             contact_name="FPV / Piston", group_id=GROUP_GUID.lower(), status="resolved",
             resolution="delivered", resolved_at="2026-06-23", created_at="2026-06-20")
    _waiting(tmp_path, "imessage:waiting_on:name:fpv /", channel="imessage",
             contact_name="FPV / Piston", contact_identifier=None, created_at="2026-06-22")
    out = cr.resolve({"today": "2026-06-24", "local": GROUP_LOCAL}, NOW)
    assert out["active"] == []
    entries = _all_fm(tmp_path)
    original = next(f for f in entries if f.get("anchor_key") == f"imessage:waiting_on:gid:{GROUP_GUID.lower()}")
    duplicate = next(f for f in entries if f.get("anchor_key") == "imessage:waiting_on:name:fpv /")
    assert original["status"] == "resolved" and original["resolution"] == "delivered"
    assert duplicate["status"] == "dismissed" and duplicate["resolution"] == "merged_duplicate"
    assert duplicate["merged_into"] == original["anchor_key"]


def test_no_snapshot_means_no_migration(tmp_path, monkeypatch):
    """An on-demand run with no `local` payload has nothing to verify an id against, so it changes
    no identity at all — the migration is a join, never a guess."""
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, "imessage:waiting_on:name:fpv /", channel="imessage",
             contact_name="FPV / Piston", contact_identifier=None, created_at="2026-06-21")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert [a["anchor_key"] for a in out["active"]] == ["imessage:waiting_on:name:fpv /"]


# ── one person, one debt: the knowledge graph resolves WHO before the anchor keys it ──────────────
# The owner's evening brief carried Spencer Schneier three times (three email threads), Farid twice
# (once under his name, once under an id), Amy Wu twice, the Intro Group twice — 14 rows for ~7
# debts. Identity is fixed here, at the root, and the migration heals the rows a label already minted.

def _person_file(tmp_path, cid, name, identifiers=()):
    d = tmp_path / "knowledge" / "people"
    d.mkdir(parents=True, exist_ok=True)
    ids = "".join(f"  - {i}\n" for i in identifiers)
    (d / f"{cid}.md").write_text(
        f"---\nname: {name}\ncanonical_id: {cid}\n"
        + (f"identifiers:\n{ids}" if ids else "") + "---\n\n# " + name + "\n")


SPENCER = "c_5pencer01"




def test_a_name_only_capture_and_an_email_capture_are_the_same_person(tmp_path, monkeypatch):
    """The `name:` vs `id:` fork, closed: both captures resolve to the person's canonical_id."""
    _env(tmp_path, monkeypatch)
    _person_file(tmp_path, "c_farid001", "Farid Mirmohseni", ["farid@x.com"])
    a = cr.canonicalize_counterpart(cr._normalize_action(
        {"type": "reply", "channel": "gmail", "contactName": "Farid Mirmohseni"}), {},
        cr.person_identity())
    b = cr.canonicalize_counterpart(cr._normalize_action(
        {"type": "reply", "channel": "gmail", "contactIdentifier": "farid@x.com"}), {},
        cr.person_identity())
    assert cr.compute_anchor_key(a) == cr.compute_anchor_key(b) == "follow_up:cid:c_farid001"


def test_an_unresolvable_counterpart_keeps_todays_behavior(tmp_path, monkeypatch):
    """Resolution is a join, never a guess: nobody in the graph, nothing changes. And a machine
    label ("Board sync") is never resolved to a person at all."""
    _env(tmp_path, monkeypatch)
    _person_file(tmp_path, "c_farid001", "Farid Mirmohseni", ["farid@x.com"])
    idx = cr.person_identity()
    unknown = cr.canonicalize_counterpart(cr._normalize_action(
        {"type": "reply", "channel": "gmail", "contactName": "Nobody Here"}), {}, idx)
    assert not unknown.get("canonical_id")
    assert cr.compute_anchor_key(unknown) == "email:follow_up:name:nobody here"
    assert cr.resolve_canonical_id("Board sync", "", idx) == ""      # a label is not a person




def test_a_synthetic_commitment_anchor_never_collapses_onto_its_contact(tmp_path, monkeypatch):
    """The one place a thread id still IS the identity: apply_commitments' no-recipient anchors are
    content hashes, and two commitments from one meeting must stay two debts (and stay put through
    the migration, which never rewrites an anchor it did not compose)."""
    _env(tmp_path, monkeypatch)
    keys = [cr.compute_anchor_key(cr._normalize_action(
        {"action_type": "follow_up", "channel": "followup", "contact_name": "Acme Sync",
         "source_thread_id": f"commitment:{h}"})) for h in ("aaa111", "bbb222")]
    assert keys == ["thread:commitment:aaa111", "thread:commitment:bbb222"]
    _loop(tmp_path, "email:follow_up:id:dana@acme.com:c:deadbeef", channel="gmail",
          contact_name="Dana", contact_identifier="dana@acme.com", summary="send the deck")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert [a["anchor_key"] for a in out["active"]] == ["email:follow_up:id:dana@acme.com:c:deadbeef"]


# ── identity: a name may never override an unknown identifier ─────────────────────────────────────
# The coherence review's worst find: `resolve_relation_target` fell through identifier → name → a
# dedup-lite CONTAINMENT match, so a debt carrying an address the graph had never seen resolved onto
# whoever happened to share the display name, folded into their debt, and closed as
# `merged_duplicate` — a stranger's ask, deleted, invisible in the brief and on /app#loops.

def test_an_unknown_identifier_never_resolves_by_name(tmp_path, monkeypatch):
    """The graph knows ONE Alex Kim, at alex.kim@startup.io. A debt from a DIFFERENT Alex Kim, at an
    address the graph has never seen, resolves to NOBODY — an identifier is an identity, and the
    name is not consulted once one is present."""
    _env(tmp_path, monkeypatch)
    _person_file(tmp_path, "c_alexkim01", "Alex Kim", ["alex.kim@startup.io"])
    idx = cr.person_identity()
    assert cr.resolve_canonical_id("Alex Kim", "alex.kim@startup.io", idx) == "c_alexkim01"
    assert cr.resolve_canonical_id("Alex Kim", "alex@fund.com", idx) == ""
    assert cr.resolve_canonical_id("", "alex@fund.com", idx) == ""


def test_a_first_name_never_adopts_a_full_name_persons_identity(tmp_path, monkeypatch):
    """"Sam" (an iMessage contact saved by first name) is not Sam Patel. Only a name the graph knows
    EXACTLY resolves; the fuzzy containment match belongs to merge suggestions a human confirms."""
    _env(tmp_path, monkeypatch)
    _person_file(tmp_path, "c_sampatel1", "Sam Patel", ["sam.patel@acme.com"])
    idx = cr.person_identity()
    assert cr.resolve_canonical_id("Sam", "", idx) == ""
    assert cr.resolve_canonical_id("Sam Patel", "", idx) == "c_sampatel1"


def test_two_people_sharing_a_name_stay_two_debts(tmp_path, monkeypatch):
    """End to end: the known Alex's diligence pack and the stranger Alex's LP letter both survive
    the migration. Neither row is folded, neither is terminated, and the stranger keeps his ask."""
    _env(tmp_path, monkeypatch)
    _person_file(tmp_path, "c_alexkim01", "Alex Kim", ["alex.kim@startup.io"])
    _loop(tmp_path, "email:waiting_on:cid:c_alexkim01", action_type="waiting_on", channel="email",
          canonical_id="c_alexkim01", contact_name="Alex Kim",
          contact_identifier="alex.kim@startup.io", summary="the diligence pack",
          created_at="2026-06-20 09:00:00")
    _loop(tmp_path, "email:waiting_on:id:alex@fund.com", action_type="waiting_on", channel="email",
          contact_name="Alex Kim", contact_identifier="alex@fund.com", summary="the LP letter",
          created_at="2026-06-15 09:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {}}, NOW, merge=False)
    assert sorted(a["summary"] for a in out["active"]) == ["the LP letter", "the diligence pack"]
    assert [f.get("resolution") for f in _all_fm(tmp_path)] == [None, None]


# ── the fold folds chase state by VALUE, never by which row is older ──────────────────────────────

def _fold_pair(tmp_path, keeper_fm: dict, loser_fm: dict):
    """The canonical fold fixture: a person-anchored keeper and an older thread-anchored twin of the
    same debt, which `_migrate_identity` re-anchors onto the keeper."""
    _loop(tmp_path, "email:waiting_on:id:maya@x.com", action_type="waiting_on", channel="email",
          contact_name="Maya Chen", contact_identifier="maya@x.com",
          summary="the signed contract", created_at="2026-06-20 09:00:00", **keeper_fm)
    _loop(tmp_path, "thread:T-OLD:waiting_on", action_type="waiting_on", channel="email",
          contact_name="Maya Chen", contact_identifier="maya@x.com", source_thread_id="T-OLD",
          summary="contract signature", created_at="2026-06-01 09:00:00", **loser_fm)


def _kept(tmp_path) -> dict:
    return next(f for f in _all_fm(tmp_path) if f.get("resolution") != "merged_duplicate")




def test_the_fold_never_rewinds_the_chase_clock(tmp_path, monkeypatch):
    """Both rows were chased; the keeper YESTERDAY, the twin weeks ago. Every chase field folds by
    value — the most chases, the latest clock — so a merge cannot make a debt chase-due again the
    day after it was chased."""
    _env(tmp_path, monkeypatch)
    _fold_pair(tmp_path, {"chased_count": 1, "last_chased_at": "2026-06-23",
                          "chase_after": "2026-06-26"},
               {"chased_count": 1, "last_chased_at": "2026-06-05", "chase_after": "2026-06-08"})
    cr.resolve({"today": "2026-06-24", "local": {}}, NOW, merge=False)
    kept = _kept(tmp_path)
    assert kept["chased_count"] == 1
    assert kept["last_chased_at"] == "2026-06-23"     # yesterday's chase is remembered
    assert kept["chase_after"] == "2026-06-26"        # …and its clock still stands
    assert "chase_pending" not in kept




# ── phase two never writes to a dead row ──────────────────────────────────────────────────────────



def test_finalize_never_writes_to_a_terminal_row(tmp_path, monkeypatch):
    """A row the user closed (no `merged_into` to follow) takes neither a chase nor a hand-off."""
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "email:waiting_on:id:zed@x.com", action_type="waiting_on", channel="email",
          contact_name="Zed Ray", contact_identifier="zed@x.com", summary="the invoice",
          status="dismissed", resolution="user_dismissed", chase_pending="2026-06-24")
    assert cr.finalize_chase("email:waiting_on:id:zed@x.com", NOW)["ok"] is False
    assert cr.finalize_handoff("email:waiting_on:id:zed@x.com", NOW)["ok"] is False
    assert _all_fm(tmp_path)[0].get("handoff_asked_at") is None


def test_the_hand_off_is_stamped_on_delivery_and_only_once(tmp_path, monkeypatch):
    """The chase's two-phase rule applied to the question that ends the lane: `handoff_asked_at` is
    written when the question actually went out, and a second call is a no-op."""
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "email:waiting_on:id:maya@x.com", action_type="waiting_on", channel="email",
          contact_name="Maya Chen", contact_identifier="maya@x.com", summary="the contract",
          chased_count=2, last_chased_at="2026-06-20")
    first = cr.finalize_handoff("email:waiting_on:id:maya@x.com", NOW)
    assert first["ok"] is True and first["handoff_asked_at"] == "2026-06-24"
    assert _all_fm(tmp_path)[0]["handoff_asked_at"] == "2026-06-24"
    again = cr.finalize_handoff("email:waiting_on:id:maya@x.com",
                                datetime(2026, 7, 4, 9, 0, 0))
    assert again["detail"] == "already asked" and again["handoff_asked_at"] == "2026-06-24"


def test_keep_waiting_clears_the_whole_chase_state(tmp_path, monkeypatch):
    """"I still care" restarts the loop's chase story — chases, clock, pending stamp AND the
    hand-off question — so the one field list lives in ledger_io and every caller reads it."""
    _env(tmp_path, monkeypatch)
    import ledger_io
    import retune_apply
    _loop(tmp_path, "email:waiting_on:id:maya@x.com", action_type="waiting_on", channel="email",
          contact_name="Maya Chen", contact_identifier="maya@x.com", summary="the contract",
          chased_count=2, last_chased_at="2026-06-20", chase_after="2026-06-23",
          handoff_asked_at="2026-06-23")
    assert retune_apply.apply("keep", "email:waiting_on:id:maya@x.com")["ok"] is True
    kept = _all_fm(tmp_path)[0]
    assert not any(k in kept for k in ledger_io.CHASE_STATE_FIELDS)


def test_a_stalled_chase_rotates_to_the_back_of_the_lane(tmp_path, monkeypatch):
    """_stamp_chase gave the day's ONE stamp to the oldest eligible row and returned. A row the
    nudge lane then declined (quiet hours at the tick, a snooze, a dead channel) took the stamp
    again tomorrow, and every other chase-eligible loop on the volume got nothing — one stuck row
    silenced the whole lane (Day-7 simulation, Sep 2026). A stall is counted, and stalled rows
    rank last."""
    _env(tmp_path, monkeypatch)
    monkeypatch.setenv("SOTTO_CHASE_AFTER_DAYS", "3")
    _waiting(tmp_path, "old", created_at="2026-06-10")
    _waiting(tmp_path, "young", created_at="2026-06-20")
    day1 = cr.resolve({"today": "2026-06-24"}, datetime(2026, 6, 24, 9, 0, 0))["active"]
    pending = {it["anchor_key"]: it.get("chase_pending") for it in day1}
    assert [k for k, v in pending.items() if v] == [next(k for k in pending if "old" in k)]
    # nobody delivered it; the next morning the stall is counted and the OTHER row gets the stamp
    day2 = cr.resolve({"today": "2026-06-25"}, datetime(2026, 6, 25, 9, 0, 0))["active"]
    by_key = {it["anchor_key"]: it for it in day2}
    old = next(v for k, v in by_key.items() if "old" in k)
    young = next(v for k, v in by_key.items() if "young" in k)
    assert old.get("chase_stalls") == 1 and "chase_pending" not in old
    assert young.get("chase_pending") == "2026-06-25"
    # …and a stall is a property of the days the lane was down, not of the loop: once a chase on
    # the stalled row actually lands, the penalty is gone (review, Sep 3)
    day3 = cr.resolve({"today": "2026-06-26"}, datetime(2026, 6, 26, 9, 0, 0))["active"]
    stamped = next(it for it in day3 if it.get("chase_pending") == "2026-06-26")
    assert cr.finalize_chase(stamped["anchor_key"], datetime(2026, 6, 26, 9, 0, 0))["ok"]
    after = {it["anchor_key"]: it for it in cr.resolve({"today": "2026-06-26"},
                                                       datetime(2026, 6, 26, 9, 0, 0))["active"]}
    assert "chase_stalls" not in after[stamped["anchor_key"]]
    import ledger_io
    assert "chase_stalls" in ledger_io.CHASE_STATE_FIELDS       # "keep waiting" clears it too


def test_user_closure_does_not_mute_new_requests_but_replays_stay_closed(tmp_path, monkeypatch):
    import knowledge_edit as ke
    _env(tmp_path, monkeypatch)
    old = {"action_type": "reply", "channel": "gmail", "contactName": "Dana",
           "contactIdentifier": "dana@acme.com", "contextSummary": "Send the pricing document",
           "emailThreadId": "pricing-thread", "emailMessageId": "old-mail", "created_at": "2026-06-22 09:00:00"}
    first = cr.resolve({"today": "2026-06-22", "new_actions": [old]}, NOW, resolve_existing=False)
    anchor = first["active"][0]["anchor_key"]
    ke.op_loop(anchor, "resolved", "2026-06-23")
    # A paraphrase and a fresh extraction timestamp do not constitute a new incoming request.
    replay = {**old, "contextSummary": "Dana is still waiting for the pricing doc", "created_at": "2026-06-24 09:00:00"}
    assert cr.resolve({"today": "2026-06-24", "new_actions": [replay]}, NOW, resolve_existing=False)["active"] == []
    new = {**old, "contextSummary": "Confirm the address for tomorrow lunch", "emailThreadId": "lunch-thread", "emailMessageId": "new-mail"}
    message = {"id": "new-mail", "threadId": "lunch-thread", "from": "dana@acme.com",
               "body": "Can you confirm the address for our lunch tomorrow?", "date": "2026-06-24T08:00:00Z"}
    # Old evidence, a sent mail, or another person's message cannot overturn the user's closure.
    for bad in ({**message, "date": "2026-06-22T08:00:00Z"}, {**message, "isSent": True},
                {**message, "from": "other@acme.com"}):
        assert cr.resolve({"today": "2026-06-24", "new_actions": [new], "emails": [bad]}, NOW, resolve_existing=False)["active"] == []
    row, = cr.resolve({"today": "2026-06-24", "new_actions": [new], "emails": [message]}, NOW, resolve_existing=False)["active"]
    assert row["summary"] == new["contextSummary"] and row["source_thread_id"] == "lunch-thread"
    assert row["source_message_id"] == "new-mail" and row["created_at"] == "2026-06-24 08:00:00"
    assert "closed_at" not in row and "resolution" not in row


def test_new_imessage_evidence_can_reopen_after_same_day_user_closure(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "follow_up:id:4155550000", status="dismissed", resolution="user_dismissed",
          resolved_at="2026-06-24", closed_at="2026-06-24T07:30:00Z", summary="Send the pricing doc",
          source_refs=[{"sourceType": "imessage", "sourceId": "11"}])
    action = {"action_type": "reply", "channel": "imessage", "contact_identifier": "+14155550000",
              "summary": "Confirm lunch address", "evidence": [{"sourceType": "imessage", "sourceId": "12"}]}
    message = {"rowid": 12, "handle": "+14155550000", "timestamp": "2026-06-24T08:00:00Z", "text": "What's the address for lunch?"}
    row, = cr.resolve({"today": "2026-06-24", "new_actions": [action], "local": {"imessage": [message]}}, NOW, resolve_existing=False)["active"]
    assert row["summary"] == "Confirm lunch address"


@pytest.mark.parametrize("channel", ["imessage", "whatsapp"])
@pytest.mark.parametrize("group", [False, True])
def test_real_bridge_message_reaches_prompt_and_reopens_only_a_new_request(tmp_path, monkeypatch, channel, group):
    """Bridge's LocalData has no rowid/guid. Exercise the actual prompt and ledger boundaries,
    copying the rendered evidence as the extractor must, without inventing a database ID."""
    import compose_brief as cb
    import knowledge_edit as ke
    _env(tmp_path, monkeypatch)
    phone = "+14155550000"
    group_id = "iMessage;+;chat12345678" if channel == "imessage" else "12345678@g.us"
    counterpart = phone if channel == "imessage" else "14155550000@s.whatsapp.net"
    message = {"text": "Can you confirm the lunch address?", "timestamp": "2026-06-24T08:00:00Z",
               "is_from_me": False, "is_group_chat": group, "chat_guid": group_id if group else None,
               "group_name": "Lunch friends" if group else None, "group_participants": []}
    if channel == "imessage":
        message["handle"] = counterpart
    else:
        message.update(contact_jid=group_id if group else counterpart, partner_name="Dana",
                       sender_jid=counterpart if group else None)
    local = {channel: [message], "contacts": [{"name": "Dana", "phones": [phone]}]}
    prompt = cb.build_prompt(cb._load_prompt(), {"type": "morning", "google": {"events": []}, "local": local})
    ref, = re.findall(r"source_id: (msg:v1:[a-f0-9]+) \| timestamp: 2026-06-24T08:00:00Z", prompt)
    action = {"action_type": "reply", "channel": channel, "contactName": "Lunch friends" if group else "Dana",
              "contactIdentifier": group_id if group else counterpart,
              "contextSummary": "Confirm the lunch address", "evidence": [{"sourceType": channel, "sourceId": ref}]}
    # Seed a closed request with the handle-style evidence older extractions really wrote.
    old = {**action, "contextSummary": "Send pricing doc", "evidence": [{"sourceType": channel, "sourceId": counterpart}]}
    first, = cr.resolve({"today": "2026-06-24", "local": local, "new_actions": [old]}, NOW,
                        resolve_existing=False)["active"]
    ke.op_loop(first["anchor_key"], "dismissed", "2026-06-24T07:30:00Z")
    payload = {"today": "2026-06-24", "local": local, "new_actions": [action]}
    # A rendered old message, another sender/group, outgoing traffic or the wrong source is no proof.
    changes = [{"timestamp": "2026-06-24T07:00:00Z"}, {"is_from_me": True}]
    changes.append({"chat_guid": "other-group"} if group else
                   {"handle" if channel == "imessage" else "contact_jid": "+14155559999"})
    for change in changes:
        bad_local = {**local, channel: [{**message, **change}]}
        if group:
            # Keep the real group in the snapshot's identity index while replacing its evidence.
            bad_local[channel].append({**message, "timestamp": "2026-06-24T07:00:00Z"})
        assert cr.resolve({**payload, "local": bad_local}, NOW, resolve_existing=False)["active"] == []
    old_message = {**message, "timestamp": "2026-06-24T07:00:00Z"}
    old_ref = cr._render_local.message_evidence_id(old_message, channel)
    replay = {**action, "evidence": [{"sourceType": channel, "sourceId": old_ref}]}
    assert cr.resolve({**payload, "local": {**local, channel: [old_message]}, "new_actions": [replay]},
                      NOW, resolve_existing=False)["active"] == []
    for evidence in ([], [{"sourceType": "email", "sourceId": ref}]):
        assert cr.resolve({**payload, "new_actions": [{**action, "evidence": evidence}]}, NOW,
                          resolve_existing=False)["active"] == []
    opened, = cr.resolve(payload, NOW, resolve_existing=False)["active"]
    assert opened["summary"] == "Confirm the lunch address"
    assert opened["created_at"] == "2026-06-24 08:00:00"
    # Closing again must not turn the same extracted ask into another request.
    ke.op_loop(opened["anchor_key"], "dismissed", "2026-06-24T08:30:00Z")
    assert cr.resolve(payload, NOW, resolve_existing=False)["active"] == []


def test_local_message_evidence_is_stable_and_conversation_specific():
    from render_local import message_evidence_id
    message = {"handle": "+14155550000", "contact_jid": "14155550000@s.whatsapp.net",
               "timestamp": "2026-06-24T08:00:00Z", "text": "Confirm lunch?", "is_from_me": False}
    ref = message_evidence_id(message, "imessage")
    assert ref.startswith("msg:v1:")
    assert message_evidence_id({**message, "resolved_name": "Different name", "sender_name": "Dana"}, "imessage") == ref
    for changes in ({"text": "Confirm dinner?"}, {"timestamp": "2026-06-24T08:00:01Z"},
                    {"handle": "+14155559999"}, {"is_from_me": True}, {"chat_guid": "another-group"}):
        assert message_evidence_id({**message, **changes}, "imessage") != ref
    assert message_evidence_id(message, "whatsapp") != ref
    assert message_evidence_id({**message, "rowid": 12}, "imessage") == "12"
    assert message_evidence_id({**message, "source_id": "native-12"}, "imessage") == "native-12"
    wa_group = {**message, "chat_guid": "123@g.us", "sender_jid": "111@s.whatsapp.net"}
    assert message_evidence_id(wa_group, "whatsapp") != message_evidence_id(
        {**wa_group, "sender_jid": "222@s.whatsapp.net"}, "whatsapp")
    for missing in ("handle", "timestamp", "text", "is_from_me"):
        assert message_evidence_id({k: v for k, v in message.items() if k != missing}, "imessage") == ""


def test_replayed_evidence_does_not_reopen_even_if_its_timestamp_changes(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    refs = [{"sourceType": "imessage", "sourceId": "11"}]
    _loop(tmp_path, "follow_up:id:4155550000", status="dismissed", resolution="user_dismissed",
          resolved_at="2026-06-23", summary="Old ask", source_refs=refs)
    action = {"action_type": "reply", "channel": "imessage", "contact_identifier": "+14155550000",
              "summary": "A paraphrase of the old ask", "evidence": refs}
    assert cr.resolve({"today": "2026-06-24", "new_actions": [action], "local": {"imessage": [
        {"rowid": 11, "handle": "+14155550000", "timestamp": "2026-06-24 08:00:00", "text": "Can you send the doc?"}]}}, NOW, resolve_existing=False)["active"] == []


def test_group_reopen_binds_the_group_not_the_individual_sender(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    group = GROUP_GUID.lower()
    _loop(tmp_path, f"follow_up:gid:{group}", status="dismissed", resolution="user_dismissed",
          resolved_at="2026-06-23", group_id=group, contact_name="FPV / Piston", contact_identifier=None,
          summary="The old group request")
    action = {"action_type": "reply", "channel": "imessage", "group_id": group,
              "contact_name": "FPV / Piston", "summary": "Confirm next week's dinner address",
              "evidence": [{"sourceType": "imessage", "sourceId": "12"}]}
    message = {"rowid": 12, "handle": "+14155551111", "timestamp": "2026-06-24 08:00:00",
               "text": "Can you confirm the dinner address?", "chat_guid": GROUP_GUID, "is_group_chat": True}
    wrong = {**message, "chat_guid": "iMessage;+;another-group"}
    assert cr.resolve({"today": "2026-06-24", "new_actions": [action], "local": {"imessage": [wrong]}}, NOW, resolve_existing=False)["active"] == []
    row, = cr.resolve({"today": "2026-06-24", "new_actions": [action], "local": {"imessage": [message]}}, NOW, resolve_existing=False)["active"]
    assert row["summary"] == action["summary"] and row["group_id"] == group


def test_group_evidence_cannot_reopen_a_persons_one_to_one_request(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "follow_up:id:4155550000", status="dismissed", resolution="user_dismissed",
          resolved_at="2026-06-23", summary="Old direct ask")
    action = {"action_type": "reply", "channel": "imessage", "contact_identifier": "+14155550000",
              "summary": "An unrelated group request", "evidence": [{"sourceType": "imessage", "sourceId": "12"}]}
    message = {"rowid": 12, "handle": "+14155550000", "timestamp": "2026-06-24 08:00:00",
               "text": "Can someone send that doc?", "chat_guid": GROUP_GUID, "is_group_chat": True}
    assert cr.resolve({"today": "2026-06-24", "new_actions": [action], "local": {"imessage": [message]}}, NOW, resolve_existing=False)["active"] == []
