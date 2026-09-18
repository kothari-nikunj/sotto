"""test_draft_outcomes.py — the learning loop's left half + matcher (ROADMAP Step 3).

The contract in one sentence: a draft matches the first message the user sent to the same person
within 24 hours of the offer — ≥0.95 similarity is executed (verbatim), ≥0.60 is edited_and_sent,
a 24-hour-old draft with no match is dismissed, and everything lands through log_outcome so
the Learn runner invokes it directly.
"""
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
SCRIPTS = os.path.join(HERE, "..", "_shared", "scripts")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


al = _load("action_links")
do = _load("draft_outcomes")
se = _load("style_extract")

NOW = datetime(2026, 8, 23, 18, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_draft(tmp_path, ts, channel, identifier, text, action_type="reply"):
    p = tmp_path / "events" / "drafts.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _iso(ts), "channel": channel, "identifier": identifier,
                            "text": text, "action_type": action_type}) + "\n")


def _write_signal(tmp_path, ts, handle, text, source="imessage", seen_at=None, rowid=None):
    p = tmp_path / "events" / "queue.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    ev = {"source": source, "is_from_me": True, "text": text, "handle": handle,
          "timestamp": _iso(ts), "rowid": rowid or f"{source}:{handle}:{_iso(ts)}"}
    if source == "whatsapp":
        ev = {"source": source, "is_from_me": True, "text": text, "timestamp": _iso(ts),
              "rowid": rowid or f"{source}:{handle}:{_iso(ts)}",
              "contact_jid": handle + "@s.whatsapp.net"}
        ev.pop("handle", None)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _iso(seen_at or ts), "verdict_class": "signal",
                            "sender": "Someone", "event": ev}) + "\n")


def _outcomes(tmp_path):
    p = tmp_path / "outcomes.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# ── The ledger (left half) ────────────────────────────────────────────────────────────────────────

def test_link_for_records_the_offered_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    url = al.link_for("imessage", "+1 (415) 555-1234", "On my way — 5 min out.")
    assert url.startswith("imessage://")
    rows = [json.loads(l) for l in
            (tmp_path / "events" / "drafts.jsonl").read_text().splitlines()]
    assert rows[0]["channel"] == "imessage"
    assert rows[0]["identifier"] == "+14155551234"      # normalized exactly as the link embeds it
    assert rows[0]["text"] == "On my way — 5 min out."
    # a bare open-the-thread link carries no draft → no row
    al.link_for("imessage", "+14155551234")
    assert len((tmp_path / "events" / "drafts.jsonl").read_text().splitlines()) == 1


def test_a_decline_is_recorded_but_still_never_linked(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    url = al.link_for("imessage", "+14155551234", "Can't make it, sorry.", action_type="decline")
    assert url == ""                                     # the never-pre-link-a-decline rule holds
    rows = [json.loads(l) for l in
            (tmp_path / "events" / "drafts.jsonl").read_text().splitlines()]
    assert rows[0]["action_type"] == "decline"


# ── The matcher ───────────────────────────────────────────────────────────────────────────────────

def test_verbatim_send_is_executed_and_edit_is_edited_and_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    t0 = NOW - timedelta(hours=2)
    _write_draft(tmp_path, t0, "imessage", "+14155551234", "On my way — 5 min out.")
    _write_draft(tmp_path, t0, "whatsapp", "16505550000",
                 "Great chat today — I'll send the deck by Thursday.")
    _write_signal(tmp_path, t0 + timedelta(minutes=9), "+1 (415) 555-1234",
                  "On my way — 5 min out.")             # verbatim, formatting differences only
    _write_signal(tmp_path, t0 + timedelta(minutes=30), "16505550000",
                  "Great chat today — I'll send the deck by Thursday, and I'll loop in Priya.",
                  source="whatsapp")                    # the draft plus the user's own addition
    s = do.run(now=NOW)
    assert (s["executed"], s["edited_and_sent"], s["dismissed"]) == (1, 1, 0)
    by_contact = {r["contact"]: r for r in _outcomes(tmp_path)}
    assert by_contact["+14155551234"]["outcome"] == "executed"
    assert by_contact["+14155551234"]["tier"] == "one_tap"
    assert by_contact["16505550000"]["outcome"] == "edited_and_sent"
    assert "similarity" in by_contact["16505550000"]["edits"]


def test_absence_of_a_send_stays_unknown_even_when_lane_is_alive(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _write_draft(tmp_path, NOW - timedelta(hours=30), "imessage", "+14155551234", "Want to grab lunch?")
    _write_draft(tmp_path, NOW - timedelta(hours=1), "imessage", "+16505550000", "Sounds good!")
    s = do.run(now=NOW)
    assert (s["dismissed"], s["pending"]) == (0, 2)      # silent lane → no verdict at all
    # Unrelated activity is telemetry about the lane, not evidence that this draft was rejected.
    _write_signal(tmp_path, NOW - timedelta(hours=20), "+12125550000", "totally unrelated text")
    s2 = do.run(now=NOW)
    assert (s2["dismissed"], s2["pending"]) == (0, 2)
    assert _outcomes(tmp_path) == []


def test_grading_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    t0 = NOW - timedelta(hours=2)
    _write_draft(tmp_path, t0, "imessage", "+14155551234", "On my way.")
    _write_signal(tmp_path, t0 + timedelta(minutes=5), "+14155551234", "On my way.")
    do.run(now=NOW)
    s2 = do.run(now=NOW)
    assert s2["graded"] == 0
    assert len(_outcomes(tmp_path)) == 1


def test_inferred_non_use_cannot_become_a_mute_offer(tmp_path, monkeypatch):
    """Exercise matcher -> tune-up with the real persisted history.
    A busy user sending unrelated mail must not be accused of rejecting this person.
    """
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    for hours in (30, 29, 28):
        _write_draft(tmp_path, NOW - timedelta(hours=hours), "imessage", "+14155551234",
                     f"Follow-up draft offered {hours} hours ago")
    _write_signal(tmp_path, NOW - timedelta(hours=20), "+12125550000", "An unrelated reply")
    assert do.run(now=NOW)["dismissed"] == 0
    assert not (tmp_path / "preferences.json").exists()
    import retune_scan
    assert retune_scan.scan()["mute_suggestions"] == []
    assert not (tmp_path / "proactive" / "pending_offer.json").exists()
    # A direct user instruction still works through the existing preference command.
    import subprocess
    import sys
    result = subprocess.run([sys.executable, os.path.join(SCRIPTS, "preferences.py"),
                             "mute-person", "Maya"], capture_output=True, text=True,
                            env=os.environ, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "preferences.json").read_text())["explicit"]["mute_people"] == ["Maya"]


def _write_email_signal(tmp_path, ts, to, text, cc=""):
    p = tmp_path / "events" / "queue.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    ev = {"source": "email", "is_from_me": True, "body": text, "date": _iso(ts),
          "id": f"email:{to}:{_iso(ts)}",
          "from": "Me <me@fpv.example.com>", "to": to, "cc": cc}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _iso(ts), "verdict_class": "signal",
                            "sender": "Me", "event": ev}) + "\n")


def test_email_drafts_are_graded_via_the_sent_mail_lane(tmp_path, monkeypatch):
    """The poll's in:sent lane queues the user's outbound mail as signals — an email draft sent
    verbatim grades `executed` even though the reply body drags the whole quoted thread below the
    new words (the quote tail is stripped before similarity)."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    t0 = NOW - timedelta(hours=3)
    body = "Thanks Ron — Monday 11:30 at Mendocino Farms works. See you there."
    _write_draft(tmp_path, t0, "email", "ron@example.com", body)
    _write_email_signal(tmp_path, t0 + timedelta(minutes=40),
                        "Ron B <ron@example.com>",
                        body + "\n\nOn Fri, Aug 21, 2026 at 9:02 AM Ron wrote:\n> any of those work")
    s = do.run(now=NOW)
    assert (s["executed"], s["dismissed"]) == (1, 0)
    assert _outcomes(tmp_path)[0]["channel"] == "email"
    # An old email draft remains unknown even when unrelated sent-mail telemetry exists.
    _write_draft(tmp_path, NOW - timedelta(hours=30), "email", "someoneelse@example.com",
                 "Following up on the memo.")
    assert do.run(now=NOW)["dismissed"] == 0             # lane silent in THAT draft's window
    _write_email_signal(tmp_path, NOW - timedelta(hours=20), "third@example.com", "separate mail")
    assert do.run(now=NOW)["dismissed"] == 0


def test_native_timestamp_and_cross_channel_counterpart_drive_matching(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    offered = NOW - timedelta(hours=3)
    text = "Can you send the signed agreement this week?"
    _write_draft(tmp_path, offered, "imessage", "+14155551234", text)
    _write_signal(tmp_path, offered + timedelta(hours=1), "14155551234", text,
                  source="whatsapp", seen_at=NOW + timedelta(days=2), rowid="late-cross")
    assert do.run(now=NOW + timedelta(days=2))["executed"] == 1
    row = _outcomes(tmp_path)[0]
    assert row["source_event_id"] == "whatsapp:late-cross"
    assert row["source_event_ts"] == _iso(offered + timedelta(hours=1))


def test_cross_country_numbers_with_same_final_ten_digits_do_not_match(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    offered = NOW - timedelta(hours=2)
    text = "I'll send the signed agreement tonight."
    _write_draft(tmp_path, offered, "imessage", "+442079460958", text)
    _write_signal(tmp_path, offered + timedelta(hours=1), "12079460958", text,
                  source="whatsapp", rowid="different-country")
    assert do.run(now=NOW)["executed"] == 0
    assert _outcomes(tmp_path) == []


def test_full_uk_number_matches_whatsapp_jid_across_channels(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    offered = NOW - timedelta(hours=2)
    text = "I'll send the signed agreement tonight."
    _write_draft(tmp_path, offered, "imessage", "+442079460958", text)
    _write_signal(tmp_path, offered + timedelta(hours=1), "442079460958", text,
                  source="whatsapp", rowid="same-uk-number")
    assert do.run(now=NOW)["executed"] == 1
    assert _outcomes(tmp_path)[0]["source_event_id"] == "whatsapp:same-uk-number"


def test_one_send_credits_the_closest_supported_draft_once_across_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    text = "Can you send the updated supplier contract this week?"
    older, newer = NOW - timedelta(hours=4), NOW - timedelta(hours=2)
    _write_draft(tmp_path, older, "imessage", "+14155551234", text)
    _write_draft(tmp_path, newer, "imessage", "+14155551234", text)
    _write_signal(tmp_path, NOW - timedelta(hours=1), "+14155551234", text, rowid="one-send")
    assert do.run(now=NOW)["executed"] == 1
    expected = {"ts": _iso(newer), "channel": "imessage", "identifier": "+14155551234",
                "text": text, "action_type": "reply"}
    assert _outcomes(tmp_path)[0]["action_id"] == do._draft_key(expected)
    assert do.run(now=NOW)["graded"] == 0
    assert len(_outcomes(tmp_path)) == 1


def test_preoffer_send_and_absent_native_timestamp_are_never_credited(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    text = "The implementation review is complete."
    offered = NOW - timedelta(hours=2)
    _write_draft(tmp_path, offered, "imessage", "+14155551234", text)
    _write_signal(tmp_path, offered - timedelta(minutes=1), "+14155551234", text,
                  seen_at=NOW, rowid="preoffer")
    queue = tmp_path / "events" / "queue.jsonl"
    row = json.loads(queue.read_text().splitlines()[0])
    row["event"].pop("timestamp")
    with open(queue, "a", encoding="utf-8") as f:
        f.write(json.dumps(row | {"ts": _iso(NOW)}) + "\n")
    assert do.run(now=NOW)["pending"] == 1
    assert _outcomes(tmp_path) == []


def test_late_send_repairs_legacy_inferred_nonuse_but_not_explicit_dismissal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    offered = NOW - timedelta(hours=3)
    text = "Can you send the plan?"
    _write_draft(tmp_path, offered, "imessage", "+14155551234", text)
    draft = json.loads((tmp_path / "events" / "drafts.jsonl").read_text())
    key = do._draft_key(draft)
    do.log_outcome.log({"action_id": key, "outcome": "dismissed", "channel": "imessage",
                        "contact": "+14155551234", "action_type": "reply"})
    _write_signal(tmp_path, offered + timedelta(hours=1), "+14155551234", text, rowid="late")
    assert do.run(now=NOW)["executed"] == 1
    assert [r["outcome"] for r in _outcomes(tmp_path)] == ["dismissed", "executed"]

    other = "Please send the budget."
    _write_draft(tmp_path, offered, "imessage", "+16505550000", other)
    explicit = json.loads((tmp_path / "events" / "drafts.jsonl").read_text().splitlines()[-1])
    do.log_outcome.log({"action_id": do._draft_key(explicit), "outcome": "dismissed",
                        "channel": "imessage", "contact": "+16505550000", "action_type": "reply",
                        "feedback_source": "explicit_user"})
    _write_signal(tmp_path, offered + timedelta(hours=1), "+16505550000", other, rowid="explicit")
    assert do.run(now=NOW)["executed"] == 0


def test_verbatim_send_confirms_the_style_sample(tmp_path, monkeypatch):
    """The voice register finally gets graded: a draft the user shipped verbatim confirms the
    matching observed sample into style.json's `confirmed` bucket (floored quality, TTL-immune,
    quoted first by style_apply)."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    sent = "On my way — 5 min out."
    sample = {"channel": "imessage", "date": "2026-08-23", "recipient": "+14155551234",
              "text": sent, "bucket": "personal_chat", "source": "observed"}
    (tmp_path / "style.json").write_text(json.dumps(
        {"recent": [sample], "canonical": {}, "confirmed": []}))
    t0 = NOW - timedelta(hours=2)
    _write_draft(tmp_path, t0, "imessage", "+14155551234", sent)
    _write_signal(tmp_path, t0 + timedelta(minutes=5), "+14155551234", sent)
    s = do.run(now=NOW)
    assert s["style_confirmed"] == 1
    style = json.loads((tmp_path / "style.json").read_text())
    assert len(style["confirmed"]) == 1 and style["confirmed"][0]["source"] == "confirmed"


def test_more_than_confirmed_cap_is_one_shot_per_action(tmp_path, monkeypatch):
    """Rotating prompt samples out must not make old sends look newly confirmed next Learn run."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    sent_messages = []
    for i in range(21):
        handle = f"+1415555{i:04d}"
        text = f"Verbatim shipped message number {i} with enough distinctive words."
        sent_messages.append({"channel": "imessage", "date": "2026-08-23",
                              "recipient": handle, "text": text, "work": False})
        _write_draft(tmp_path, NOW - timedelta(hours=2), "imessage", handle, text)
        _write_signal(tmp_path, NOW - timedelta(hours=1), handle, text)
    se.extract({"sent_messages": sent_messages}, now=NOW)

    first = do.run(now=NOW)
    after_first = json.loads((tmp_path / "style.json").read_text())
    first_texts = [s["text"] for s in after_first["confirmed"]]
    assert first["style_confirmed"] == 21
    assert len(after_first["confirmed"]) == 20
    assert len(after_first["confirmed_actions"]) == 21
    evicted = {s["text"] for s in sent_messages} - set(first_texts)
    assert len(evicted) == 1

    # Real Learn ordering extracts again before grading. The extraction must preserve the action
    # markers even though one confirmed prompt sample has rotated out.
    se.extract({"sent_messages": sent_messages}, now=NOW + timedelta(minutes=1))
    second = do.run(now=NOW)
    after_second = json.loads((tmp_path / "style.json").read_text())
    assert second["style_confirmed"] == 0
    assert [s["text"] for s in after_second["confirmed"]] == first_texts
    assert evicted.isdisjoint(s["text"] for s in after_second["confirmed"])
    assert len(_outcomes(tmp_path)) == 21

    # The existing ledger lifecycle bounds the markers too; no separate forever-growing state.
    (tmp_path / "events" / "drafts.jsonl").unlink()
    se.extract({"sent_messages": sent_messages}, now=NOW + timedelta(minutes=2))
    assert json.loads((tmp_path / "style.json").read_text())["confirmed_actions"] == {}


def test_the_ledger_row_id_has_exactly_one_implementation():
    """The grader keys outcomes by it and style_extract prunes markers by it. They used to compute
    the same sha256 in two places; now both names ARE keys.draft_key, so drift cannot compile."""
    import keys
    assert do._draft_key is keys.draft_key
    assert se.draft_key is keys.draft_key
    row = {"ts": _iso(NOW), "channel": "imessage", "identifier": "+14155550000",
           "text": "Verbatim shipped message.", "action_type": "reply"}
    assert keys.draft_key(row) == do._draft_key(row) == se.draft_key(row)


def test_the_offered_drafts_ledger_has_exactly_one_path_owner(tmp_path, monkeypatch):
    """action_links writes it; the grader and the marker pruner both ask it where it is."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert do.action_links_drafts_path() == al.drafts_path()
    _write_draft(tmp_path, NOW - timedelta(hours=2), "imessage", "+14155550000", "Hi there.")
    assert se._retained_draft_actions() == {
        do._draft_key(json.loads((tmp_path / "events" / "drafts.jsonl").read_text().strip()))}




def test_poll_gmail_sent_lane_marks_is_from_me(tmp_path, monkeypatch):
    """The outcome side of email grading: the poll's second, smaller in:sent search rides the same
    seen-ring and marks its events is_from_me — and a broken sent lane never costs the inbox lane."""
    spec2 = importlib.util.spec_from_file_location(
        "pg2", os.path.join(HERE, "..", "event-triage", "scripts", "poll_gmail.py"))
    pg = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(pg)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(pg, "_find_google_api", lambda: "/fake/google_api.py")

    broken_sent = {"value": False}
    class Result:
        def __init__(self, value): self.value = value
        def execute(self): return self.value
    class Messages:
        def list(self, **kwargs):
            if "in:sent" in kwargs["q"]:
                if broken_sent["value"]:
                    raise RuntimeError("sent search down")
                return Result({"messages": [{"id": "s1", "from": "me@x.example",
                                              "subject": "Re: hi"}]})
            mid = "i2" if broken_sent["value"] else "i1"
            return Result({"messages": [{"id": mid, "from": "a@x.example", "subject": "hi"}]})
    class Service:
        def users(self):
            return type("Users", (), {"messages": lambda self: Messages()})()
        def close(self):
            pass
    monkeypatch.setattr(pg, "gmail_service", Service)
    monkeypatch.setattr(pg, "fetch_message", lambda _service, mid: {
        "id": mid, "from": "x", "to": [{"email": "y@x.example"}], "body": "b"})
    flags = {e["rowid"]: bool(e.get("is_from_me")) for e in pg.poll()}
    assert flags == {"i1": False, "s1": True}
    pending = json.load(open(tmp_path / "events" / "gmail_seen.json"))
    assert pending["seen"] == [] and pending["lanes"]["inbox"]["page_ids"] == ["i1"]
    assert pg.acknowledge(["i1", "s1", "i1"]) == 2
    assert pg.acknowledge(["i1"]) == 0
    assert json.load(open(tmp_path / "events" / "gmail_seen.json"))["seen"] == ["i1", "s1"]

    broken_sent["value"] = True
    assert [e["rowid"] for e in pg.poll()] == ["i2"]      # inbox lane survives alone


def test_gmail_draft_records_the_offered_draft(tmp_path, monkeypatch):
    """A Gmail draft is a draft OFFERED — google_action's create path leaves the same ledger row a
    tap link does, so email drafts enter the matcher's left half."""
    spec3 = importlib.util.spec_from_file_location(
        "ga2", os.path.join(SCRIPTS, "google_action.py"))
    ga = importlib.util.module_from_spec(spec3)
    spec3.loader.exec_module(ga)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))

    class _Exec:
        def execute(self):
            return {"id": "d1", "message": {"threadId": ""}}

    class _Drafts:
        def create(self, userId=None, body=None):
            return _Exec()

    class _Users:
        def drafts(self):
            return _Drafts()

    class _Svc:
        def users(self):
            return _Users()
    monkeypatch.setattr(ga, "_gmail_service", lambda: _Svc())
    out = ga._gmail_draft("ron@example.com", "Thanks Ron — Monday works.")
    assert out["status"] == "drafted"
    rows = [json.loads(l) for l in
            (tmp_path / "events" / "drafts.jsonl").read_text().splitlines()]
    assert rows[0]["channel"] == "email"
    assert rows[0]["identifier"] == "ron@example.com"
    assert rows[0]["action_type"] == "send"               # no thread yet → a fresh send
