"""continuity_resolve.py — dedicated edge-case suite for the cross-channel resolution "moat":
reply-on-another-channel matching (phone last-10 / email / WhatsApp JID), anchor_key dedup,
7-day expiry, snoozed_until, deadline grace, and meeting-passed incl. the near-midnight
UTC-offset cases (the strptime(...[:19]) off-by-one this suite pins the fix for)."""
import importlib.util
import os
import sys
from datetime import datetime

import yaml

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))

spec = importlib.util.spec_from_file_location(
    "cr_edge", os.path.join(ROOT, "morning-brief", "scripts", "continuity_resolve.py"))
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)

NOW = datetime(2026, 6, 24, 9, 0, 0)   # naive on purpose — callers pass naive datetimes today


def _env(tmp_path, monkeypatch, tz="+00:00"):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", tz)


def _loop(tmp_path, key, **fm):
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True, exist_ok=True)
    fm.setdefault("anchor_key", key)
    fm.setdefault("status", "open")
    fm.setdefault("action_type", "reply")
    fm.setdefault("contact_name", "Someone")
    fm.setdefault("contact_identifier", "+14155550000")
    fm.setdefault("created_at", "2026-06-23")
    (d / f"{key}.md").write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n")


# ── cross-channel reply matching (phone last-10 / email / JID) ─────────────────

def test_reply_resolves_via_phone_last10_format_mismatch(tmp_path, monkeypatch):
    # Loop tracks "+1 (415) 555-2222"; the outgoing iMessage handle is bare "4155552222".
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", contact_identifier="+1 (415) 555-2222", created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "imessage": [{"is_from_me": True, "handle": "4155552222",
                      "timestamp": "2026-06-23 20:00:00", "text": "done"}]}}, NOW)
    assert [r["resolution"] for r in out["resolved"]] == ["replied"]
    assert "iMessage" in out["resolved"][0]["resolution_evidence"]


def test_email_loop_resolves_via_whatsapp_jid_through_contacts(tmp_path, monkeypatch):
    # Loop is an EMAIL reply owed to dhruv@acme.com; the user answered him on WhatsApp. The JID's
    # phone prefix must match the contact's phone (email→name→phone expansion, then last-10 vs JID).
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", channel="gmail", contact_name="Dhruv",
          contact_identifier="dhruv@acme.com", created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "contacts": [{"name": "Dhruv", "emails": ["dhruv@acme.com"], "phones": ["+1 415 555 2222"]}],
        "whatsapp": [{"is_from_me": True, "contact_jid": "14155552222@s.whatsapp.net",
                      "timestamp": "2026-06-23 21:00:00", "text": "sent it"}]}}, NOW)
    assert [r["resolution"] for r in out["resolved"]] == ["replied"]
    assert "WhatsApp" in out["resolved"][0]["resolution_evidence"]


def test_callback_resolves_via_whatsapp_call_jid(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", action_type="call_back", channel="phone",
          contact_identifier="+14155559999", created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "whatsapp_calls": [{"is_outgoing": True, "jid": "14155559999@s.whatsapp.net",
                            "timestamp": "2026-06-23 19:00:00"}]}}, NOW)
    assert [r["resolution"] for r in out["resolved"]] == ["called"]


def test_incoming_or_earlier_messages_do_not_resolve(tmp_path, monkeypatch):
    # Neither an INCOMING message from them nor an outgoing one from BEFORE the loop counts.
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", contact_identifier="+14155552222", created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "imessage": [
            {"is_from_me": False, "handle": "4155552222", "timestamp": "2026-06-23 20:00:00"},
            {"is_from_me": True, "handle": "4155552222", "timestamp": "2026-06-23 07:00:00"}]}}, NOW)
    assert out["resolved"] == [] and len(out["active"]) == 1


def test_short_or_mismatched_numbers_never_false_positive(tmp_path, monkeypatch):
    # <7-digit identifiers and different last-10s must not match (the _phone_matches guard).
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", contact_identifier="+14155552222", created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "imessage": [{"is_from_me": True, "handle": "555222", "timestamp": "2026-06-23 20:00:00"},
                     {"is_from_me": True, "handle": "+14155559999", "timestamp": "2026-06-23 20:00:00"}]}}, NOW)
    assert out["resolved"] == []


# ── anchor_key dedup ───────────────────────────────────────────────────────────

def test_anchor_dedup_across_phone_formats_bumps_times_surfaced(tmp_path, monkeypatch):
    # The same owed reply re-extracted next brief with a differently-formatted phone → ONE loop,
    # times_surfaced bumped (not a duplicate file).
    _env(tmp_path, monkeypatch)
    cr.resolve({"today": "2026-06-23", "new_actions": [
        {"type": "reply", "channel": "imessage", "contactName": "Jo",
         "contactIdentifier": "+1 (415) 555-1234"}]}, datetime(2026, 6, 23, 9, 0, 0))
    out = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"type": "reply", "channel": "imessage", "contactName": "Jo",
         "contactIdentifier": "4155551234"}]}, NOW)
    assert len(out["active"]) == 1
    assert out["active"][0]["times_surfaced"] == 2
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

def test_age_expiry_boundaries(tmp_path, monkeypatch):
    # today 2026-06-24 → cutoff 2026-06-17: created BEFORE it expires; ON it survives.
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "old8", contact_name="Old", created_at="2026-06-16")     # 8 days → expired
    _loop(tmp_path, "edge7", contact_name="Edge", created_at="2026-06-17")   # exactly 7 → survives
    _loop(tmp_path, "new6", contact_name="New", created_at="2026-06-18")     # 6 days → active
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert [e["contact_name"] for e in out["expired"]] == ["Old"]
    assert {a["contact_name"] for a in out["active"]} == {"Edge", "New"}
    assert out["expired"][0]["resolution"] == "expired"


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


def test_snooze_does_not_shield_from_resolution(tmp_path, monkeypatch):
    # A snoozed loop the user then actually answers still resolves (resolution runs before snooze).
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", contact_identifier="+14155552222", created_at="2026-06-23 08:00:00",
          snoozed_until="2026-07-15")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "imessage": [{"is_from_me": True, "handle": "4155552222",
                      "timestamp": "2026-06-23 20:00:00"}]}}, NOW)
    assert [r["resolution"] for r in out["resolved"]] == ["replied"]


# ── deadline grace ─────────────────────────────────────────────────────────────

def test_deadline_two_day_grace_boundaries(tmp_path, monkeypatch):
    # today 2026-06-24 → deadline cutoff 2026-06-22: a deadline 3 days ago expires; the 2-day-old
    # one is still within grace.
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "past", contact_name="Past", created_at="2026-06-23", deadline="2026-06-21")
    _loop(tmp_path, "grace", contact_name="Grace", created_at="2026-06-23", deadline="2026-06-22")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert [(e["contact_name"], e["resolution"]) for e in out["expired"]] == [("Past", "deadline_passed")]
    assert {a["contact_name"] for a in out["active"]} == {"Grace"}


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

def test_scheduled_meeting_resolves_with_offset_start_and_naive_now(tmp_path, monkeypatch):
    # The gathered event carries a Z offset while resolve() got a NAIVE now — the old
    # strptime/naive-compare path could both misparse and crash-compare. Must resolve cleanly.
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", contact_name="Dana", contact_identifier="dana@x.com",
          created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "calendar_events": [{"summary": "Coffee", "start": "2026-06-25T06:30:00Z",
                             "attendees": [{"email": "dana@x.com"}]}]}}, NOW)
    assert [r["resolution"] for r in out["resolved"]] == ["scheduled_meeting"]
    assert "Coffee" in out["resolved"][0]["resolution_evidence"]


def test_calendar_event_outside_14d_window_does_not_resolve(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", contact_name="Dana", contact_identifier="dana@x.com",
          created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "calendar_events": [{"summary": "Far", "start": "2026-08-30T10:00:00Z",
                             "attendees": [{"email": "dana@x.com"}]}]}}, NOW)
    assert out["resolved"] == [] and len(out["active"]) == 1
