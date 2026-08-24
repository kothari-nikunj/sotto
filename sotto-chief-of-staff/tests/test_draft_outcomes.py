"""test_draft_outcomes.py — the learning loop's left half + matcher (ROADMAP Step 3).

The contract in one sentence: a draft matches the first message the user sent to the same person
within 24 hours of the offer — ≥0.95 similarity is executed (verbatim), ≥0.60 is edited_and_sent,
a 24-hour-old draft with no match is dismissed, and everything lands through log_outcome so
learn_preferences' existing tally consumes it with zero new wiring.
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

NOW = datetime(2026, 8, 23, 18, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_draft(tmp_path, ts, channel, identifier, text, action_type="reply"):
    p = tmp_path / "events" / "drafts.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _iso(ts), "channel": channel, "identifier": identifier,
                            "text": text, "action_type": action_type}) + "\n")


def _write_signal(tmp_path, ts, handle, text, source="imessage"):
    p = tmp_path / "events" / "queue.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    ev = {"source": source, "is_from_me": True, "text": text, "handle": handle}
    if source == "whatsapp":
        ev = {"source": source, "is_from_me": True, "text": text,
              "contact_jid": handle + "@s.whatsapp.net"}
        ev.pop("handle", None)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _iso(ts), "verdict_class": "signal",
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


def test_dismissal_requires_a_demonstrably_alive_lane(tmp_path, monkeypatch):
    """Dismissed only when the user sent OTHER messages on that lane during the window and still
    didn't use the draft. A silent lane (Mac asleep all day, Gmail poll off) leaves the draft
    ungraded — no verdict beats a false one."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _write_draft(tmp_path, NOW - timedelta(hours=30), "imessage", "+14155551234", "Want to grab lunch?")
    _write_draft(tmp_path, NOW - timedelta(hours=1), "imessage", "+16505550000", "Sounds good!")
    s = do.run(now=NOW)
    assert (s["dismissed"], s["pending"]) == (0, 2)      # silent lane → no verdict at all
    # an unrelated message inside the old draft's window proves the lane was alive → dismissed
    _write_signal(tmp_path, NOW - timedelta(hours=20), "+12125550000", "totally unrelated text")
    s2 = do.run(now=NOW)
    assert (s2["dismissed"], s2["pending"]) == (1, 1)
    assert _outcomes(tmp_path)[0]["outcome"] == "dismissed"


def test_grading_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    t0 = NOW - timedelta(hours=2)
    _write_draft(tmp_path, t0, "imessage", "+14155551234", "On my way.")
    _write_signal(tmp_path, t0 + timedelta(minutes=5), "+14155551234", "On my way.")
    do.run(now=NOW)
    s2 = do.run(now=NOW)
    assert s2["graded"] == 0
    assert len(_outcomes(tmp_path)) == 1


def _write_email_signal(tmp_path, ts, to, text, cc=""):
    p = tmp_path / "events" / "queue.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    ev = {"source": "email", "is_from_me": True, "body": text,
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
    # an old email draft is dismissed only once the email lane shows life in its window
    _write_draft(tmp_path, NOW - timedelta(hours=30), "email", "someoneelse@example.com",
                 "Following up on the memo.")
    assert do.run(now=NOW)["dismissed"] == 0             # lane silent in THAT draft's window
    _write_email_signal(tmp_path, NOW - timedelta(hours=20), "third@example.com", "separate mail")
    assert do.run(now=NOW)["dismissed"] == 1


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


def test_learn_preferences_runs_the_matcher_first(tmp_path, monkeypatch):
    """One Learn invocation does match → tally: the outcomes the matcher just wrote are in the
    same run's tally, so approval_defaults and deprioritization learn from real draft outcomes
    with zero new SKILL steps."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    lp_path = os.path.join(HERE, "..", "approval-tiers", "scripts", "learn_preferences.py")
    spec = importlib.util.spec_from_file_location("lp", lp_path)
    lp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lp)
    t0 = NOW - timedelta(hours=2)
    _write_draft(tmp_path, t0, "imessage", "+14155551234", "On my way.")
    _write_signal(tmp_path, t0 + timedelta(minutes=5), "+14155551234", "On my way.")
    prefs = lp.learn()
    assert isinstance(prefs, dict)
    assert _outcomes(tmp_path)[0]["outcome"] == "executed"   # the matcher ran inside learn()


def test_poll_gmail_sent_lane_marks_is_from_me(tmp_path, monkeypatch):
    """The outcome side of email grading: the poll's second, smaller in:sent search rides the same
    seen-ring and marks its events is_from_me — and a broken sent lane never costs the inbox lane."""
    spec2 = importlib.util.spec_from_file_location(
        "pg2", os.path.join(HERE, "..", "event-triage", "scripts", "poll_gmail.py"))
    pg = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(pg)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(pg, "_find_google_api", lambda: "/fake/google_api.py")

    def fake_run(api, args, timeout=60):
        if args[:2] == ["gmail", "search"]:
            if pg.SENT_QUERY in args:
                return [{"id": "s1", "from": "me@x.example", "subject": "Re: hi"}]
            return [{"id": "i1", "from": "a@x.example", "subject": "hi"}]
        return {"from": "x", "to": [{"email": "y@x.example"}], "body": "b"}
    monkeypatch.setattr(pg, "_run", fake_run)
    flags = {e["rowid"]: bool(e.get("is_from_me")) for e in pg.poll()}
    assert flags == {"i1": False, "s1": True}
    assert not os.path.exists(tmp_path / "events" / "gmail_seen.json")
    assert pg.acknowledge(["i1", "s1", "i1"]) == 2
    assert pg.acknowledge(["i1"]) == 0
    assert json.load(open(tmp_path / "events" / "gmail_seen.json")) == ["i1", "s1"]

    def broken_sent(api, args, timeout=60):
        if args[:2] == ["gmail", "search"] and pg.SENT_QUERY in args:
            raise RuntimeError("sent search down")
        if args[:2] == ["gmail", "search"]:
            return [{"id": "i2", "from": "b@x.example", "subject": "hi2"}]
        return {}
    monkeypatch.setattr(pg, "_run", broken_sent)
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
