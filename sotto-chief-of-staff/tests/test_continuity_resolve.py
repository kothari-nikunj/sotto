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

def test_new_action_reopens_terminal_anchor(tmp_path, monkeypatch):
    # The person replied again the day AFTER their loop resolved: the new action used to be
    # absorbed (times_surfaced bump) while step 2 `continue`d on the terminal status — the
    # person vanished for the whole retention window.
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "imessage:follow_up:id:4155550000", status="resolved", resolution="replied",
          resolved_at="2026-06-23", created_at="2026-06-16", summary="old ask", times_surfaced=3)
    out = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"action_type": "reply", "channel": "imessage", "contact_name": "Someone",
         "contact_identifier": "+14155550000", "summary": "they followed up again"}]}, NOW)
    assert len(out["active"]) == 1
    it = out["active"][0]
    assert it["status"] == "open"
    assert it["summary"] == "they followed up again"          # refreshed from the new action
    assert it["created_at"].startswith("2026-06-24 ")         # age clock restarted, to a real instant
    assert it["reopened_at"] == "2026-06-24"
    assert it["prior_resolution"] == "replied"                # history kept
    assert "resolution" not in it and "resolved_at" not in it
    assert it["times_surfaced"] == 4


def test_terminal_anchor_without_new_action_stays_terminal(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "imessage:follow_up:id:4155550000", status="resolved", resolution="replied",
          resolved_at="2026-06-23")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert out["active"] == [] and out["resolved"] == []       # untouched, not re-opened


# ── reply_* type variants (the FLEX schema leaves `type` a free string) ────────

def test_reply_message_variant_resolves_cross_channel(tmp_path, monkeypatch):
    # Gemini emits reply_message/reply_email alongside the documented "reply" — the loop must
    # still self-resolve when the user answers on any channel (it used to fall through the
    # ("reply","follow_up","follow_up_stale") gate and live to its 7-day expiry).
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", action_type="reply_message",
          contact_identifier="+1 (415) 555-2222", created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "imessage": [{"is_from_me": True, "handle": "4155552222",
                      "timestamp": "2026-06-23 20:00:00", "text": "done"}]}}, NOW)
    assert [r["resolution"] for r in out["resolved"]] == ["replied"]
    assert "iMessage" in out["resolved"][0]["resolution_evidence"]


def test_reply_email_variant_resolves_cross_channel(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", action_type="reply_email", channel="gmail", contact_name="Dhruv",
          contact_identifier="dhruv@acme.com", created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "contacts": [{"name": "Dhruv", "emails": ["dhruv@acme.com"], "phones": ["+1 415 555 2222"]}],
        "whatsapp": [{"is_from_me": True, "contact_jid": "14155552222@s.whatsapp.net",
                      "timestamp": "2026-06-23 20:00:00", "text": "sent it"}]}}, NOW)
    assert [r["resolution"] for r in out["resolved"]] == ["replied"]


def test_reply_variants_collapse_to_one_anchor_family(tmp_path, monkeypatch):
    # reply vs reply_message vs reply_email must produce the SAME anchor key, so the same owed
    # reply re-extracted under a variant spelling bumps times_surfaced instead of duplicating.
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
         "contactIdentifier": "+14155551234"}]}, datetime(2026, 6, 23, 9, 0, 0))
    out = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"type": "reply_message", "channel": "imessage", "contactName": "Jo",
         "contactIdentifier": "+14155551234"}]}, NOW)
    assert len(out["active"]) == 1
    assert out["active"][0]["times_surfaced"] == 2


# ── MCP tool-result wrapper around `local` (shared unwrap) ─────────────────────

def test_wrapped_local_still_drives_cross_channel_resolution(tmp_path, monkeypatch):
    # The SKILL says to pass the read_local JSON AS-IS — which may be the raw MCP tool-result
    # wrapper. Resolution must see through it (it used to see {} and never resolve anything).
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "k", contact_identifier="+1 (415) 555-2222", created_at="2026-06-23 08:00:00")
    wrapped = {"content": [{"type": "text", "text":
               '{"imessage": [{"is_from_me": true, "handle": "4155552222", '
               '"timestamp": "2026-06-23 20:00:00", "text": "done"}]}'}]}
    out = cr.resolve({"today": "2026-06-24", "local": wrapped}, NOW)
    assert [r["resolution"] for r in out["resolved"]] == ["replied"]


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
    out = cr.resolve(payload)
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


def test_inbound_delivery_with_a_link_resolves_a_waiting_on(tmp_path, monkeypatch):
    # They actually sent it — the mirrored branch of the outgoing check, same identifier machinery.
    # created_at is DATE-ONLY on purpose: that is what legacy ledger files carry.
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, contact_identifier="+1 (415) 555-2222", created_at="2026-06-22")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "imessage": [{"is_from_me": False, "handle": "4155552222",
                      "timestamp": "2026-06-23 20:00:00",
                      "text": "here you go https://drive.example.com/contract"}]}}, NOW)
    assert [r["resolution"] for r in out["resolved"]] == ["delivered"]
    assert "Inbound iMessage" in out["resolved"][0]["resolution_evidence"]


def test_inbound_delivery_via_whatsapp_jid_through_contacts(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, channel="gmail", contact_name="Dhruv",
             contact_identifier="dhruv@acme.com", created_at="2026-06-23 08:00:00")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "contacts": [{"name": "Dhruv", "emails": ["dhruv@acme.com"], "phones": ["+1 415 555 2222"]}],
        "whatsapp": [{"is_from_me": False, "contact_jid": "14155552222@s.whatsapp.net",
                      "timestamp": "2026-06-23 21:00:00",
                      "text": "attached the revised deck — final_deck.pdf"}]}}, NOW)
    assert [r["resolution"] for r in out["resolved"]] == ["delivered"]
    assert "Inbound WhatsApp" in out["resolved"][0]["resolution_evidence"]


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
         "contactIdentifier": "+14155552222", "contextSummary": "the deck"}],
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


def test_substance_gate_shape():
    assert cr._is_delivery("here's the deck https://x.co/d") is True
    assert cr._is_delivery("revenue was 4.2M, margin 61%, and the board deck is attached") is True
    assert cr._is_delivery("ok") is False
    assert cr._is_delivery("") is False
    assert cr._is_delivery("I'll get you the numbers by EOD, promise, they are nearly done") is False


def test_waiting_on_never_age_expires_while_reply_and_follow_up_still_do(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "r", action_type="reply", contact_name="Owed", created_at="2026-06-10")
    _loop(tmp_path, "f", action_type="follow_up", contact_name="Chase", created_at="2026-06-10")
    _waiting(tmp_path, "w", contact_name="Acme", created_at="2026-06-10")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert {e["contact_name"] for e in out["expired"]} == {"Owed", "Chase"}
    assert [a["contact_name"] for a in out["active"]] == ["Acme"]


def test_a_deadline_never_kills_a_waiting_on(tmp_path, monkeypatch):
    # A due date makes a debt owed to the USER more protected, not less: the passed deadline makes
    # it chase-eligible immediately, and sotto-loops §B is still the only exit.
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, "w", created_at="2026-06-23", deadline="2026-06-20")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert out["expired"] == []
    assert out["active"][0]["chase_pending"] == "2026-06-24"
    # …while what the USER owes keeps the 2-day deadline grace exactly as before.
    _loop(tmp_path, "owed", action_type="reply", contact_name="Owed",
          created_at="2026-06-23", deadline="2026-06-20")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert [(e["contact_name"], e["resolution"]) for e in out["expired"]] == [("Owed", "deadline_passed")]


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


def test_identifier_less_waiting_on_expires_as_unreachable_after_its_chases(tmp_path, monkeypatch):
    # A commitment with no to_email can neither self-resolve (no inbound to match) nor be nudged
    # (no channel). After its chase quota it leaves the ledger with a resolution that says why.
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, "w", contact_identifier=None, created_at="2026-06-14", chased_count=1)
    assert cr.resolve({"today": "2026-06-24"}, NOW)["expired"] == []      # one chase left
    _waiting(tmp_path, "w", contact_identifier=None, created_at="2026-06-14", chased_count=2)
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert [e["resolution"] for e in out["expired"]] == ["unreachable"]
    # …an identifier-ful one is never expired this way
    _waiting(tmp_path, "k", contact_identifier="+14155552222", created_at="2026-06-14", chased_count=2)
    assert [e["anchor_key"] for e in cr.resolve({"today": "2026-06-25"},
                                                datetime(2026, 6, 25, 9, 0, 0))["expired"]] == []


def test_delivery_resolves_a_chased_waiting_on_and_stops_the_chase(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, "w", contact_identifier="+14155552222", created_at="2026-06-16",
             chased_count=1, last_chased_at="2026-06-20", chase_after="2026-06-23")
    out = cr.resolve({"today": "2026-06-24", "local": {
        "imessage": [{"is_from_me": False, "handle": "4155552222", "timestamp": "2026-06-23 20:00:00",
                      "text": "sorry for the wait — signed and sent: https://x.co/signed"}]}}, NOW)
    assert [r["resolution"] for r in out["resolved"]] == ["delivered"]
    assert out["active"] == []


def test_chase_knob_is_read_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("SOTTO_CHASE_AFTER_DAYS", raising=False)
    assert cr.chase_after_days() == 3
    monkeypatch.setenv("SOTTO_CHASE_AFTER_DAYS", "5")
    assert cr.chase_after_days() == 5
    monkeypatch.setenv("SOTTO_CHASE_AFTER_DAYS", "junk")
    assert cr.chase_after_days() == 3


def test_reopened_waiting_on_gets_a_fresh_chase_clock(tmp_path, monkeypatch):
    # They came back with a NEW promise after the old one closed — chasing it "for the third time"
    # would be counting last month's silence against this week's ask.
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, "imessage:follow_up:id:4155550000", status="resolved", resolution="delivered",
             resolved_at="2026-06-20", created_at="2026-06-10", chased_count=2,
             last_chased_at="2026-06-18", chase_after="2026-06-21")
    out = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"action_type": "waiting_on", "channel": "imessage", "contact_name": "Acme",
         "contact_identifier": "+14155550000", "summary": "the new quote"}]}, NOW)
    it = out["active"][0]
    assert it["status"] == "open" and it["created_at"].startswith("2026-06-24 ")
    assert all(k not in it for k in ("chased_count", "chase_after", "last_chased_at", "chase_pending"))


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
    assert by_type["waiting_on"]["anchor_key"] == "imessage:waiting_on:id:4155551234"
    assert by_type["reply"]["anchor_key"] == "imessage:follow_up:id:4155551234"
    # a thread carries one debt per direction too
    assert cr.compute_anchor_key({"source_thread_id": "T", "action_type": "reply"}) == "thread:T"
    assert cr.compute_anchor_key({"source_thread_id": "T",
                                  "action_type": "waiting_on"}) == "thread:T:waiting_on"


def test_a_live_anchor_refreshes_its_ask_but_never_its_direction(tmp_path, monkeypatch):
    """Re-capturing a live loop updates what it's ABOUT (the brief was describing Monday's ask on
    Friday) — but not its type: a genuine change of direction forks its own anchor instead."""
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, "imessage:waiting_on:id:4155551234", contact_identifier="+14155551234",
             summary="old ask", created_at="2026-06-20")
    out = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"type": "waiting_on", "channel": "imessage", "contactName": "Acme",
         "contactIdentifier": "+14155551234", "contextSummary": "the signed contract",
         "contextAsk": "chase it", "deadlineDate": "2026-06-30"}]}, NOW)
    it = out["active"][0]
    assert it["summary"] == "the signed contract" and it["ask"] == "chase it"
    assert it["deadline"] == "2026-06-30" and it["action_type"] == "waiting_on"
    assert it["times_surfaced"] == 2 and it["created_at"] == "2026-06-20"   # age clock unchanged


# ── the two passes: resolve before the brief, merge after it ───────────────────

def test_resolve_only_and_merge_only_split_the_pass(tmp_path, monkeypatch):
    """The brief must reason about a ledger resolved as of THIS run, and only the merge needs the
    brief's output — so --resolve-only runs before compose and --merge-only after."""
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "old", action_type="reply", contact_name="Ancient", created_at="2026-06-01")
    pre = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"type": "reply", "channel": "imessage", "contactName": "New",
         "contactIdentifier": "+14155559999"}]}, NOW, merge=False)
    assert [e["contact_name"] for e in pre["expired"]] == ["Ancient"]   # resolution ran…
    assert pre["active"] == [] and "New" not in str(pre)                # …the new action did not
    post = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"type": "reply", "channel": "imessage", "contactName": "New",
         "contactIdentifier": "+14155559999"}]}, NOW, resolve_existing=True, merge=True)
    assert [a["contact_name"] for a in post["active"]] == ["New"]


def test_merge_only_writes_the_new_action_and_resolves_nothing(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "old", action_type="reply", contact_name="Ancient", created_at="2026-06-01")
    out = cr.resolve({"today": "2026-06-24", "new_actions": [
        {"type": "reply", "channel": "imessage", "contactName": "New",
         "contactIdentifier": "+14155559999"}]}, NOW, resolve_existing=False)
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


def test_a_group_ask_anchors_on_the_group_id_whatever_the_model_called_it(tmp_path, monkeypatch):
    """Two captures of ONE group ask — one carrying the group's id under a made-up label, one
    carrying the right label and no id — are one debt, keyed `gid:<chat_guid>`. The row also takes
    the group's OWN name, so the brief prints what the thread is actually called."""
    _env(tmp_path, monkeypatch)
    out = cr.resolve({"today": "2026-06-24", "local": GROUP_LOCAL, "new_actions": [
        _group_ask("Intro Group", "asked about Insight Partners", identifier=GROUP_GUID),
        _group_ask("FPV / Piston", "asked for contact info")]}, NOW)
    assert len(out["active"]) == 1
    it = out["active"][0]
    assert it["anchor_key"] == f"imessage:waiting_on:gid:{GROUP_GUID.lower()}"
    assert it["contact_name"] == "FPV / Piston"          # the group's name, not the invented label
    assert it["group_id"] == GROUP_GUID.lower()
    assert it["times_surfaced"] == 2


def test_a_group_id_counts_only_when_the_snapshot_actually_contains_it(tmp_path, monkeypatch):
    """Source-verified, never model-asserted: an id this brief's data never showed is not identity,
    so the action keeps today's behavior instead of anchoring on something invented."""
    _env(tmp_path, monkeypatch)
    out = cr.resolve({"today": "2026-06-24", "local": GROUP_LOCAL, "new_actions": [
        _group_ask("Some Group", "asked the group", identifier="iMessage;+;chatMADEUP")]}, NOW)
    assert out["active"][0]["anchor_key"] == "imessage:waiting_on:id:imessage;+;chatmadeup"
    assert not out["active"][0].get("group_id")


def test_a_thread_id_never_outranks_a_verified_group_id(tmp_path, monkeypatch):
    """The same group ask reached us with a thread id one day and without it the next — which is
    how one debt became two rows. A verified group is the counterpart, so it anchors on the group."""
    _env(tmp_path, monkeypatch)
    out = cr.resolve({"today": "2026-06-24", "local": GROUP_LOCAL, "new_actions": [
        _group_ask("FPV / Piston", "asked for intros", emailThreadId=GROUP_GUID),
        _group_ask("FPV / Piston", "asked for contact info")]}, NOW)
    assert len(out["active"]) == 1
    assert out["active"][0]["anchor_key"] == f"imessage:waiting_on:gid:{GROUP_GUID.lower()}"


def test_a_person_ask_is_untouched_by_group_identity(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    out = cr.resolve({"today": "2026-06-24", "local": GROUP_LOCAL, "new_actions": [
        {"type": "waiting_on", "channel": "imessage", "contactName": "Sarah",
         "contactIdentifier": "+14155551234", "contextSummary": "the contract"},
        {"type": "reply", "channel": "gmail", "contactName": "Morgan",
         "contactIdentifier": "m@x.com", "emailThreadId": "thr_1", "contextSummary": "the deck"}]}, NOW)
    assert {a["anchor_key"] for a in out["active"]} == {
        "imessage:waiting_on:id:4155551234", "thread:thr_1"}


# ── the migration: one idempotent heal, and a dedupe is never a receipt ────────

def test_migration_reanchors_a_label_keyed_group_row_and_folds_the_duplicate(tmp_path, monkeypatch):
    """The volume already carries rows minted under a label. On the next resolve the joinable ones
    re-anchor onto the group's id; two that turn out to be one debt become one — the OLDER row's
    age and chase clock survive, the NEWER words win, and the loser closes as bookkeeping."""
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, "imessage:waiting_on:name:fpv /", channel="imessage",
             contact_name="FPV / Piston", contact_identifier=None, summary="asked about Insight",
             created_at="2026-06-21", chased_count=1, last_chased_at="2026-06-22",
             times_surfaced=3)
    _waiting(tmp_path, f"imessage:waiting_on:gid:{GROUP_GUID.lower()}", channel="imessage",
             contact_name="FPV / Piston", contact_identifier=None, group_id=GROUP_GUID.lower(),
             summary="asked for contact info", created_at="2026-06-23", times_surfaced=1)
    out = cr.resolve({"today": "2026-06-24", "local": GROUP_LOCAL}, NOW)
    assert len(out["active"]) == 1
    it = out["active"][0]
    assert it["anchor_key"] == f"imessage:waiting_on:gid:{GROUP_GUID.lower()}"
    assert it["created_at"] == "2026-06-21"              # the older row's age is the debt's age
    assert it["summary"] == "asked for contact info"     # the newer words are the live ask
    assert it["chased_count"] == 1 and it["last_chased_at"] == "2026-06-22"   # older chase clock
    assert it["times_surfaced"] == 3
    # the loser is terminal, says why, and points at the survivor
    fms = _all_fm(tmp_path)
    loser = [f for f in fms if f.get("resolution") == "merged_duplicate"]
    assert len(loser) == 1
    assert loser[0]["status"] in cr.TERMINAL and loser[0]["resolved_at"] == "2026-06-24"
    assert loser[0]["merged_into"] == f"imessage:waiting_on:gid:{GROUP_GUID.lower()}"
    # a dedupe is bookkeeping, not an outcome that moved: it is never a "resolved" receipt
    assert loser[0]["status"] != "resolved"
    assert [r["resolution"] for r in out["resolved"]] == []


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
    would rewrite a terminal loop) — it is left exactly where it is."""
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, f"imessage:waiting_on:gid:{GROUP_GUID.lower()}", channel="imessage",
             contact_name="FPV / Piston", group_id=GROUP_GUID.lower(), status="resolved",
             resolution="delivered", resolved_at="2026-06-23", created_at="2026-06-20")
    _waiting(tmp_path, "imessage:waiting_on:name:fpv /", channel="imessage",
             contact_name="FPV / Piston", contact_identifier=None, created_at="2026-06-22")
    out = cr.resolve({"today": "2026-06-24", "local": GROUP_LOCAL}, NOW)
    assert [a["anchor_key"] for a in out["active"]] == ["imessage:waiting_on:name:fpv /"]
    assert not any(f.get("resolution") == "merged_duplicate" for f in _all_fm(tmp_path))


def test_no_snapshot_means_no_migration(tmp_path, monkeypatch):
    """An on-demand run with no `local` payload has nothing to verify an id against, so it changes
    no identity at all — the migration is a join, never a guess."""
    _env(tmp_path, monkeypatch)
    _waiting(tmp_path, "imessage:waiting_on:name:fpv /", channel="imessage",
             contact_name="FPV / Piston", contact_identifier=None, created_at="2026-06-21")
    out = cr.resolve({"today": "2026-06-24"}, NOW)
    assert [a["anchor_key"] for a in out["active"]] == ["imessage:waiting_on:name:fpv /"]
