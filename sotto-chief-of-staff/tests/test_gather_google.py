"""gather_google.py — normalizes google_api.py output to compose_brief's shapes; never crashes."""
import importlib.util, json, os, re

import pytest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("gg", os.path.join(HERE, "..", "_shared", "scripts", "gather_google.py"))
gg = importlib.util.module_from_spec(spec); spec.loader.exec_module(gg)


def test_email_normalization_maps_labels_and_body():
    e = gg.normalize_email(
        {"id": "1", "threadId": "t", "from": "a@b.com", "subject": "Hi", "date": "d",
         "snippet": "sn", "labels": ["INBOX", "SENT", "IMPORTANT"]},
        {"body": "the full body", "to": "me@x.com"})
    assert e["labelIds"] == ["INBOX", "SENT", "IMPORTANT"]   # compose_brief keys flags off labelIds
    assert e["isSent"] is True and e["body"] == "the full body" and e["to"] == "me@x.com"


def test_event_normalization_flattens_start_and_maps_link():
    ev = gg.normalize_event({"id": "e", "summary": "Sync",
                             "start": {"dateTime": "2026-06-26T10:00:00Z"},
                             "end": {"dateTime": "2026-06-26T10:30:00Z"}, "htmlLink": "http://x"})
    assert ev["start"] == "2026-06-26T10:00:00Z" and ev["meetingLink"] == "http://x"
    # plain-string start also passes through
    assert gg.normalize_event({"start": "2026-06-26"})["start"] == "2026-06-26"


def test_as_list_unwraps_common_envelopes():
    assert gg._as_list([1, 2]) == [1, 2]
    assert gg._as_list({"messages": [1]}) == [1]
    assert gg._as_list({"events": [2]}) == [2]
    assert gg._as_list({"nope": 1}) == []


def test_email_normalization_tolerates_mcp_field_names():
    # A Gmail MCP server uses different field names + a {name,email} sender — must still normalize.
    e = gg.normalize_email(
        {"message_id": "9", "thread_id": "t9", "sender": {"name": "Dana", "email": "dana@acme.com"},
         "title": "Re: LOI", "preview": "quick note", "received_at": "2026-06-28",
         "label_ids": ["inbox", "important"]}, {})
    assert e["id"] == "9" and e["threadId"] == "t9"
    assert e["from"] == "Dana <dana@acme.com>" and e["subject"] == "Re: LOI"
    assert e["snippet"] == "quick note" and e["labelIds"] == ["INBOX", "IMPORTANT"]


def test_event_normalization_tolerates_mcp_field_names():
    ev = gg.normalize_event({"event_id": "e9", "title": "Pitch", "start_time": "2026-06-29T09:00:00-07:00",
                             "participants": [{"email": "x@y.com"}], "conferenceLink": "http://meet"})
    assert ev["id"] == "e9" and ev["summary"] == "Pitch"
    assert ev["start"] == "2026-06-29T09:00:00-07:00" and ev["meetingLink"] == "http://meet"
    assert ev["attendees"] == [{"email": "x@y.com"}]


def test_normalize_mcp_path_writes_canonical_files(tmp_path, monkeypatch):
    # The MCP fallback: agent dumps raw MCP results, --from-mcp normalizes to canonical shape, no CLI.
    graw = tmp_path / "graw.json"; craw = tmp_path / "craw.json"
    json.dump({"messages": [{"message_id": "1", "sender": "a@b.com", "title": "Hi"}]}, open(graw, "w"))
    json.dump([{"event_id": "e", "title": "Sync", "start": {"dateTime": "2026-06-29T10:00:00Z"}}], open(craw, "w"))
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    # _find_google_api should NOT be consulted on the MCP path.
    monkeypatch.setattr(gg, "_find_google_api", lambda: (_ for _ in ()).throw(AssertionError("CLI used")))
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--from-mcp-gmail", str(graw),
                                     "--from-mcp-calendar", str(craw), "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    emails, events = json.load(open(g)), json.load(open(c))
    assert emails[0]["id"] == "1" and emails[0]["subject"] == "Hi"
    assert events[0]["summary"] == "Sync" and events[0]["start"] == "2026-06-29T10:00:00Z"


def test_main_writes_empty_files_when_api_missing(tmp_path, monkeypatch):
    # No google_api.py found → write empty files + WARNING, exit 0 (the brief still runs).
    monkeypatch.setattr(gg, "_find_google_api", lambda: None)
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    assert json.load(open(g)) == [] and json.load(open(c)) == []


def test_bodies_fetch_preserves_order_and_tolerates_failure(monkeypatch):
    # 4 search hits, --bodies 3: full bodies for the first 3 only; one fetch fails → that email
    # stays snippet-only; output order matches the search order regardless of fetch completion order.
    items = [{"id": f"m{i}", "subject": f"S{i}", "snippet": f"sn{i}"} for i in range(4)]

    def fake_run(api, args, timeout=60):
        if args[:2] == ["gmail", "search"]:
            return items
        mid = args[2]
        if mid == "m1":
            raise RuntimeError("timeout")           # failed body fetch — tolerated
        return {"body": f"full-{mid}"}

    monkeypatch.setattr(gg, "_run", fake_run)
    emails = gg.gather_gmail("/fake/api.py", 25, 3)
    assert [e["id"] for e in emails] == ["m0", "m1", "m2", "m3"]   # search order preserved
    assert emails[0]["body"] == "full-m0" and emails[2]["body"] == "full-m2"
    assert emails[1]["body"] == "" and emails[1]["snippet"] == "sn1"   # failed → snippet-only
    assert emails[3]["body"] == ""                                     # beyond --bodies → not fetched


def test_bodies_fetched_concurrently(monkeypatch):
    # Two body fetches meet at a barrier — only possible if they run in parallel. A sequential
    # implementation deadlocks the barrier (0.5s timeout → BrokenBarrierError → test fails loudly).
    import threading
    barrier = threading.Barrier(2, timeout=5)

    def fake_run(api, args, timeout=60):
        if args[:2] == ["gmail", "search"]:
            return [{"id": "a", "subject": "A"}, {"id": "b", "subject": "B"}]
        barrier.wait(timeout=0.5)
        return {"body": f"full-{args[2]}"}

    monkeypatch.setattr(gg, "_run", fake_run)
    emails = gg.gather_gmail("/fake/api.py", 25, 2)
    assert [e["body"] for e in emails] == ["full-a", "full-b"]


# --- email-window honesty (Sprint 0 #5): cap raised + truncation envelope ------

def _wire_fake_gather(monkeypatch, items):
    def fake_run(api, args, timeout=60):
        if args[:2] == ["gmail", "search"]:
            return items
        return {"body": "b"}
    monkeypatch.setattr(gg, "_run", fake_run)
    monkeypatch.setattr(gg, "_find_google_api", lambda: "/fake/api.py")
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: True)
    monkeypatch.setattr(gg, "gather_calendar", lambda *a: [])


def test_truncation_envelope_written_when_search_hits_cap(tmp_path, monkeypatch, capsys):
    _wire_fake_gather(monkeypatch, [{"id": f"m{i}", "subject": f"S{i}"} for i in range(3)])
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--max", "3", "--bodies", "0",
                                     "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    payload = json.load(open(g))
    assert payload["truncated_at"] == 3                              # exactly at cap → truncated
    assert [e["id"] for e in payload["emails"]] == ["m0", "m1", "m2"]
    assert payload["truncation_note"] == "(inbox window truncated at 3 — more arrived)"
    assert "truncated at 3 — more arrived" in capsys.readouterr().out  # operator line says so too


def test_no_envelope_when_under_cap(tmp_path, monkeypatch):
    _wire_fake_gather(monkeypatch, [{"id": "m0", "subject": "S0"}])
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--max", "3", "--bodies", "0",
                                     "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    payload = json.load(open(g))
    assert isinstance(payload, list) and payload[0]["id"] == "m0"    # bare array — full back-compat


def test_default_max_raised_to_40(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(gg, "_find_google_api", lambda: "/fake/api.py")
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: True)
    monkeypatch.setattr(gg, "gather_gmail", lambda api, mx, bodies, days=1: captured.update(max=mx) or [])
    monkeypatch.setattr(gg, "gather_calendar", lambda *a, **k: [])
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    assert captured["max"] == 40


def test_ensure_deps_fast_path_skips_subprocess(monkeypatch):
    # When googleapiclient imports cleanly, NO subprocess work happens (the 240s pip install
    # stays out of the brief's hot path; it remains the backstop when the import fails).
    import sys as _sys
    import types
    monkeypatch.setitem(_sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setattr(gg.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess used on fast path")))
    assert gg._ensure_google_deps() is True


def test_ensure_deps_cli_mode_only_heals(monkeypatch, capsys):
    # `--ensure-deps` runs ONLY the self-heal (setup-time), no CLI discovery / gather / file writes.
    called = {"heal": 0}
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: called.__setitem__("heal", called["heal"] + 1) or True)
    monkeypatch.setattr(gg, "_find_google_api", lambda: (_ for _ in ()).throw(AssertionError("gather ran")))
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--ensure-deps"])
    gg.main()
    assert called["heal"] == 1
    assert "googleapiclient OK" in capsys.readouterr().out


# --- --attendee-comms mode (meeting-prep: per-attendee Gmail threads) ------------------------

def test_attendee_comms_normalizes_and_derives_direction(tmp_path, monkeypatch):
    # Per-attendee search → {"<email>": [{date,subject,snippet,from_me}]}. from_me: SENT label
    # wins; else derived from whether the From header carries the ATTENDEE's address.
    att = tmp_path / "att.json"
    json.dump([{"name": "Dana Roe", "email": "Dana@acme.com"}], open(att, "w"))

    def fake_run(api, args, timeout=60):
        assert args[:2] == ["gmail", "search"]
        assert args[2] == "from:dana@acme.com OR to:dana@acme.com newer_than:30d"
        return [
            {"from": "Dana Roe <dana@acme.com>", "subject": "Pricing", "date": "Tue, 04 Aug",
             "snippet": "circling back on the pilot", "labels": ["INBOX"]},
            {"from": "Me <me@myco.com>", "to": "dana@acme.com", "subject": "Re: Pricing",
             "date": "Wed, 05 Aug", "snippet": "sending the revised SOW", "labels": ["SENT"]},
            {"from": "Me <me@myco.com>", "subject": "no labels at all", "date": "d", "snippet": "s"},
        ]

    monkeypatch.setattr(gg, "_find_google_api", lambda: "/fake/api.py")
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: True)
    monkeypatch.setattr(gg, "_run", fake_run)
    out = tmp_path / "comms.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--attendee-comms", str(att),
                                     "--comms-out", str(out)])
    gg.main()
    comms = json.load(open(out))
    rows = comms["dana@acme.com"]                      # keyed by LOWERCASED attendee email
    assert rows[0] == {"date": "Tue, 04 Aug", "subject": "Pricing",
                       "snippet": "circling back on the pilot", "from_me": False}
    assert rows[1]["from_me"] is True                  # SENT label
    assert rows[2]["from_me"] is True                  # no labels → From lacks attendee's address


def test_attendee_comms_fail_empty_when_api_missing(tmp_path, monkeypatch, capsys):
    # No google_api.py → empty {} + WARNING, exit 0 (the prep still runs, just without threads).
    att = tmp_path / "att.json"
    json.dump([{"name": "Dana", "email": "dana@acme.com"}], open(att, "w"))
    monkeypatch.setattr(gg, "_find_google_api", lambda: None)
    out = tmp_path / "comms.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--attendee-comms", str(att),
                                     "--comms-out", str(out)])
    gg.main()
    assert json.load(open(out)) == {}
    assert "WARNING" in capsys.readouterr().out


def test_attendee_comms_one_failed_search_never_fails_the_rest(tmp_path, monkeypatch):
    att = tmp_path / "att.json"
    json.dump([{"email": "a@x.com"}, {"email": "b@y.com"}], open(att, "w"))

    def fake_run(api, args, timeout=60):
        if "a@x.com" in args[2]:
            raise RuntimeError("timeout")              # one attendee's search dies — tolerated
        return [{"from": "b@y.com", "subject": "S", "date": "d", "snippet": "sn"}]

    monkeypatch.setattr(gg, "_find_google_api", lambda: "/fake/api.py")
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: True)
    monkeypatch.setattr(gg, "_run", fake_run)
    out = tmp_path / "comms.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--attendee-comms", str(att),
                                     "--comms-out", str(out)])
    gg.main()
    comms = json.load(open(out))
    assert "a@x.com" not in comms and comms["b@y.com"][0]["subject"] == "S"


def test_attendee_emails_accepts_both_shapes_dedupes_and_caps():
    # research_in shape [{name,email}] + {"attendees":[…]} envelope
    import json as _json, tempfile, os as _os
    def _emails(data):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            _json.dump(data, f)
        try:
            return gg._attendee_emails_from_file(f.name)
        finally:
            _os.unlink(f.name)

    assert _emails([{"name": "A", "email": "A@x.com"}, {"email": "a@x.com"}]) == ["a@x.com"]
    assert _emails({"attendees": [{"email": "b@y.com"}]}) == ["b@y.com"]
    # calendar-derived list: events carrying attendees[] (dicts or bare strings)
    assert _emails([{"summary": "Sync", "attendees": [{"email": "c@z.com"}, "d@z.com"]}]) == \
        ["c@z.com", "d@z.com"]
    # cap at 15 unique emails
    many = [{"email": f"p{i}@x.com"} for i in range(20)]
    assert len(_emails(many)) == 15
    # unreadable file → []
    assert gg._attendee_emails_from_file("/nonexistent/path.json") == []


# ── the stale sent lane: an email you sent that nobody answered is a debt owed to you ────────────

def _thread(tid, msgs):
    return {"id": tid, "messages": [
        {"id": f"{tid}-{i}", "internalDate": str(int(ts.timestamp() * 1000)),
         "labelIds": ["SENT"] if mine else ["INBOX"], "snippet": snip,
         "payload": {"headers": [{"name": "To", "value": to}, {"name": "Subject", "value": subj}]}}
        for i, (ts, mine, to, subj, snip) in enumerate(msgs)]}


def test_stale_is_the_last_word_being_yours_for_three_to_fourteen_days():
    import datetime as dt
    now = dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.timezone.utc)
    d = lambda days, h=9: now - dt.timedelta(days=days, hours=12 - h)
    threads = {
        # yours, 4 days silent → stale
        "t-stale": _thread("t-stale", [(d(6), False, "me@example.com", "Deck", "here you go"),
                                       (d(4), True, "Victor Yeung <victor@acme.com>", "Re: Deck",
                                        "sending the revised deck, let me know on allocation")]),
        # they answered after you → not stale
        "t-answered": _thread("t-answered", [(d(5), True, "dana@acme.com", "Q", "quick question"),
                                             (d(2), False, "me@example.com", "Re: Q", "answer")]),
        # yours, but only 2 days → not yet
        "t-fresh": _thread("t-fresh", [(d(2), True, "ben@acme.com", "Thu?", "does thursday work")]),
        # yours, 20 days → dead, not a debt
        "t-dead": _thread("t-dead", [(d(20), True, "old@acme.com", "Ping", "any thoughts here")]),
    }
    cands = [{"threadId": t, "to": "x", "subject": "s", "snippet": "sn"} for t in
             ("t-stale", "t-answered", "t-fresh", "t-dead", "t-missing")]
    rows = gg.stale_from_threads(cands, threads, now)
    assert [r["threadId"] for r in rows] == ["t-stale"]
    r = rows[0]
    assert r["toEmail"] == "victor@acme.com" and r["subject"] == "Re: Deck"
    assert r["daysSinceSent"] == 4 and r["sentDate"].startswith("2026-08-31")
    assert "revised deck" in r["snippet"]


def test_gather_stale_sent_searches_once_and_reads_each_thread_once(monkeypatch):
    import datetime as dt
    now = dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.timezone.utc)
    searches, gets = [], []

    def fake_run(api, args, timeout=60):
        searches.append(args)
        return [{"id": "m1", "threadId": "t1", "to": "victor@acme.com", "subject": "Deck"},
                {"id": "m2", "threadId": "t1", "to": "victor@acme.com", "subject": "Deck"},
                {"id": "m3", "threadId": "t2", "to": "dana@acme.com", "subject": "Q"}]
    monkeypatch.setattr(gg, "_run", fake_run)

    class _Get:
        def __init__(self, tid): self.tid = tid
        def execute(self):
            gets.append(self.tid)
            if self.tid == "t2":
                raise RuntimeError("gone")
            return _thread("t1", [(now - dt.timedelta(days=5), True, "victor@acme.com", "Deck",
                                   "the revised deck is attached, thoughts on allocation")])

    class _Svc:
        def users(self): return self
        def threads(self): return self
        def get(self, userId, id, format, metadataHeaders): return _Get(id)

    rows = gg.gather_stale_sent("/fake/api.py", service=_Svc(), now=now)
    assert len(searches) == 1 and searches[0][2] == "in:sent older_than:3d newer_than:14d -in:chats"
    assert gets == ["t1", "t2"]                              # one read per thread, once
    assert [r["threadId"] for r in rows] == ["t1"]           # the unreadable thread costs itself only


def test_stale_rows_ride_the_gmail_envelope(tmp_path, monkeypatch):
    _wire_fake_gather(monkeypatch, [{"id": "m0", "subject": "S0"}])
    monkeypatch.setattr(gg, "gather_stale_sent", lambda api: [{"threadId": "t1", "toEmail": "v@acme.com"}])
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--max", "3", "--bodies", "0",
                                     "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    payload = json.load(open(g))
    assert payload["stale_threads"] == [{"threadId": "t1", "toEmail": "v@acme.com"}]
    assert [e["id"] for e in payload["emails"]] == ["m0"] and "truncated_at" not in payload
    # --skip-stale, or a backfill window, never runs the lane
    monkeypatch.setattr(gg, "gather_stale_sent", lambda api: (_ for _ in ()).throw(AssertionError("ran")))
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--max", "3", "--bodies", "0", "--skip-stale",
                                     "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    assert isinstance(json.load(open(g)), list)


def test_event_normalization_reads_your_own_rsvp():
    ev = gg.normalize_event({"id": "e", "summary": "Pitch", "start": "2026-09-05T10:00:00Z",
                             "attendees": [{"email": "vc@fund.com", "responseStatus": "accepted"},
                                           {"email": "me@example.com", "self": True,
                                            "responseStatus": "declined"}]})
    assert ev["my_response"] == "declined"
    assert gg.normalize_event({"id": "e2", "attendees": [{"email": "a@b.com"}]})["my_response"] == ""


def test_skip_gmail_calendar_only(tmp_path, monkeypatch):
    # meeting-prep uses --skip-gmail: gmail not even attempted; both files still written.
    called = {"gmail": False, "cal": False}
    monkeypatch.setattr(gg, "_find_google_api", lambda: "/fake/google_api.py")
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: True)   # don't shell out to pip in tests
    monkeypatch.setattr(gg, "gather_gmail", lambda *a, **k: called.__setitem__("gmail", True) or [{"id": "x"}])
    monkeypatch.setattr(gg, "gather_calendar", lambda *a, **k: called.__setitem__("cal", True) or [{"id": "e"}])
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--skip-gmail", "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    assert called["gmail"] is False and called["cal"] is True
    import json
    assert json.load(open(g)) == [] and len(json.load(open(c))) == 1


# ── Sent lane (roadmap Step 2 item 0) ─────────────────────────────────────────────────────────────

def _sent_row(mid="s1", to="Sarah Chen <sarah@acme.com>", body="Thanks — locking Thursday 3pm.",
              labels=None):
    row = {"id": mid, "threadId": "t" + mid, "from": "me@mine.com", "to": to,
           "subject": "Re: Contract", "date": "2026-08-06T09:00:00Z", "snippet": body[:20]}
    if labels is not None:
        row["labels"] = labels
    return row


def test_mark_sent_sets_both_signals_even_without_labels():
    """The `in:sent` QUERY is the ground truth; some hosts return search rows with no labels at all.
    Set isSent AND the SENT label so the two downstream readers can never disagree."""
    e = gg.mark_sent(gg.normalize_email(_sent_row(), {}))
    assert e["isSent"] is True and "SENT" in e["labelIds"]
    # already-labelled rows aren't duplicated
    e2 = gg.mark_sent(gg.normalize_email(_sent_row(labels=["SENT"]), {}))
    assert e2["labelIds"].count("SENT") == 1


def test_merge_sent_dedupes_by_id_and_upgrades_the_inbox_row():
    """`newer_than:1d` has no in: operator, so Gmail already returns some sent mail in the inbox
    lane — merging must not double-count it (that would hand style_extract the same sample twice)."""
    inbox = [gg.normalize_email({"id": "a", "from": "x@y.com"}, {}),
             gg.normalize_email(_sent_row("s1"), {})]          # same message, no SENT label
    sent = [gg.mark_sent(gg.normalize_email(_sent_row("s1"), {"body": "full body"})),
            gg.mark_sent(gg.normalize_email(_sent_row("s2"), {}))]
    merged = gg.merge_sent(inbox, sent)
    assert [e["id"] for e in merged] == ["a", "s1", "s2"]      # no duplicate s1
    dup = next(e for e in merged if e["id"] == "s1")
    assert dup["isSent"] is True and "SENT" in dup["labelIds"] # the inbox copy GAINS the markers
    assert dup["body"] == "full body"                          # and the body the sent lane fetched
    assert merged[0]["isSent"] is False                        # inbound rows untouched


def test_merge_sent_tolerates_missing_ids_and_junk():
    assert gg.merge_sent([], []) == []
    merged = gg.merge_sent([{"id": None}], [{"id": None, "isSent": True}, "junk"])
    assert len(merged) == 2                                    # id-less sent row still appended


def test_sent_lane_shape_is_what_style_extract_consumes():
    """The contract that matters: gather_google's sent rows must survive style_extract's --gmail
    adapter untouched (it keys off isSent/SENT, the To header, and body-or-snippet)."""
    import style_extract as se
    rows = gg.merge_sent(
        [gg.normalize_email({"id": "in1", "from": "dana@acme.com", "subject": "hi",
                             "snippet": "inbound"}, {})],
        [gg.mark_sent(gg.normalize_email(_sent_row("s1"),
                                         {"body": "Thanks — locking Thursday 3pm."})),
         gg.mark_sent(gg.normalize_email(_sent_row("s2", to="mom@yahoo.com"),
                                         {"body": "call you tonight"}))])
    samples = se._adapt_gmail(rows)
    assert len(samples) == 2                                   # the inbound row is NOT a sample
    assert [s["channel"] for s in samples] == ["email", "email"]
    assert samples[0]["recipient"] == "Sarah Chen"
    # the full body, not the 20-char snippet — bodies are why the lane fetches them
    assert samples[0]["text"] == "Thanks — locking Thursday 3pm."
    assert samples[0]["work"] is True                          # acme.com → work_email bucket
    assert samples[1]["work"] is False                         # yahoo.com → personal
    assert se.bucket_for_sample(samples[0]) == "work_email"
    # the file-shaped envelope compose_brief writes on a truncated window works too
    assert len(se._adapt_gmail({"emails": rows, "truncated_at": 40})) == 2


def test_normalize_mcp_merges_a_sent_dump(tmp_path):
    (tmp_path / "gm.json").write_text(json.dumps([{"id": "a", "from": "x@y.com"}]))
    (tmp_path / "sent.json").write_text(json.dumps([_sent_row("s1")]))
    emails, events = gg.normalize_mcp(str(tmp_path / "gm.json"), None, str(tmp_path / "sent.json"))
    assert [e["id"] for e in emails] == ["a", "s1"]
    assert emails[1]["isSent"] is True and "SENT" in emails[1]["labelIds"]
    assert events == []
    # no sent dump → the CLI-parity path still works (the documented MCP gap)
    emails2, _ = gg.normalize_mcp(str(tmp_path / "gm.json"), None)
    assert [e["isSent"] for e in emails2] == [False]


def test_gather_sent_uses_the_in_sent_query(monkeypatch):
    calls = []

    def fake_run(api, args, timeout=60):
        calls.append(args)
        if args[:2] == ["gmail", "search"]:
            return [_sent_row("s1")]
        return {"body": "the full sent body", "to": "Sarah Chen <sarah@acme.com>"}

    monkeypatch.setattr(gg, "_run", fake_run)
    rows = gg.gather_sent("api.py", 15, 10)
    assert calls[0] == ["gmail", "search", "in:sent newer_than:1d", "--max", "15"]
    assert len(rows) == 1 and rows[0]["isSent"] is True
    assert rows[0]["body"] == "the full sent body"             # bodies fetched: snippets can't teach voice


def test_window_days_slices_the_backfill_past_the_page_caps(monkeypatch):
    """One Gmail search clamps at ~500 results and one calendar list pages at ~25 events — the
    owner's 42-day backfill came back ~5 days deep. A wide window becomes date-bounded slices
    (after:/before: for Gmail, short --start/--end spans for calendar), deduped by id; the daily
    1-day gather keeps its single query and tight 60s leash."""
    import datetime
    calls, touts = [], []

    def fake_run(api, args, timeout=60):
        calls.append(args)
        touts.append(timeout)
        return [{"id": "same-row"}]                    # every slice returns the same id

    monkeypatch.setattr(gg, "_run", fake_run)
    rows = gg.gather_gmail("api.py", 500, 0, days=42)
    gmail_calls = list(calls)
    assert len(gmail_calls) >= 6                       # ~weekly slices cover the window
    assert all(c[0] == "gmail" and "after:" in c[2] and "before:" in c[2] for c in gmail_calls)
    assert len(rows) == 1                              # boundary overlap deduped by id

    calls.clear()
    sent = gg.gather_sent("api.py", 15, 0, days=42)
    assert all(c[2].startswith("in:sent after:") for c in calls)
    assert len(sent) == 1 and sent[0]["isSent"] is True

    calls.clear()
    evs = gg.gather_calendar("api.py", back_days=41)
    assert len(calls) >= 20                            # ~2-day spans beat the 25-event page
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    first = datetime.datetime.strptime(calls[0][calls[0].index("--start") + 1], fmt)
    last = datetime.datetime.strptime(calls[-1][calls[-1].index("--end") + 1], fmt)
    assert (last - first).days == 44                   # 41 back + 3 forward, covered end to end
    assert len(evs) == 1                               # deduped by event id
    assert all(t == gg.BACKFILL_TIMEOUT for t in touts)

    calls.clear()
    touts.clear()
    gg.gather_gmail("api.py", 40, 0)
    assert calls == [["gmail", "search", "newer_than:1d", "--max", "40"]]
    assert touts == [60]                               # the daily brief is untouched


# --- the attachment lane -------------------------------------------------------------------
# An attachment Sotto can read becomes text under its email; one it can't is named, never guessed.
# The Gmail service is faked here because the lane talks to the Gmail API DIRECTLY (the host's
# google_api.py has no attachments verb and its `gmail get` discards the MIME part tree) — the same
# direct-to-Google precedent google_action.py's gmail-draft set.


class _FakeGmail:
    """The three chained calls the lane makes, and nothing else. `parts` is the payload's part list;
    `blobs` maps attachmentId → base64url data for the ones Gmail didn't inline."""

    def __init__(self, parts, blobs=None, boom=False):
        self._parts, self._blobs, self._boom = parts, blobs or {}, boom
        self.attachment_gets = []

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, userId=None, id=None, format=None):
        if self._boom:
            raise RuntimeError("Gmail said no")
        return _Exec({"id": id, "payload": {"parts": self._parts}})

    def attachments(self):
        return _Attachments(self)


class _Attachments:
    def __init__(self, gmail):
        self._g = gmail

    def get(self, userId=None, messageId=None, id=None):
        self._g.attachment_gets.append(id)
        return _Exec({"data": self._g._blobs.get(id, "")})


class _Exec:
    def __init__(self, payload):
        self._p = payload

    def execute(self):
        return self._p


def _b64(raw):
    import base64
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _wire_attachment_gather(monkeypatch, parts, blobs=None, boom=False):
    """The inbox search + body fetch stay on the fake CLI; the attachment lane gets a fake Gmail."""
    svc = _FakeGmail(parts, blobs, boom)

    def fake_run(api, args, timeout=60):
        if args[:2] == ["gmail", "search"]:
            return [{"id": "m1", "subject": "Q3 numbers", "labels": ["INBOX"]}]
        return {"body": "see attached"}

    monkeypatch.setattr(gg, "_run", fake_run)
    monkeypatch.setattr(gg, "_token_path", lambda: "/fake/google_token.json")
    monkeypatch.setattr(gg, "_gmail_service", lambda: svc)
    return svc


def test_inbox_attachment_is_converted_and_hung_on_the_email(monkeypatch):
    """The end-to-end claim: a CSV attached to an inbox email in the bodies cohort arrives on the
    normalized row as converted Markdown."""
    parts = [{"filename": "", "mimeType": "text/plain", "body": {"size": 4}},
             {"filename": "numbers.csv", "mimeType": "text/csv",
              "body": {"size": 8, "attachmentId": "att-1"}}]
    _wire_attachment_gather(monkeypatch, parts, {"att-1": _b64(b"a,b\n1,2\n")})
    emails = gg.gather_gmail("/fake/api.py", 25, 12)
    atts = emails[0]["attachments"]
    assert len(atts) == 1 and atts[0]["filename"] == "numbers.csv"
    assert "| a | b |" in atts[0]["text"]


def test_gmail_inlined_bytes_need_no_second_api_call(monkeypatch):
    """Gmail inlines small attachment bodies. When it has, the lane uses them — an attachments().get
    for an attachment we already hold is a call spent for nothing."""
    parts = [{"filename": "numbers.csv", "mimeType": "text/csv",
              "body": {"size": 8, "data": _b64(b"a,b\n1,2\n")}}]
    svc = _wire_attachment_gather(monkeypatch, parts)
    emails = gg.gather_gmail("/fake/api.py", 25, 12)
    assert "| 1 | 2 |" in emails[0]["attachments"][0]["text"]
    assert svc.attachment_gets == []                 # nothing was downloaded twice


def test_an_email_with_no_attachments_makes_no_attachment_call(monkeypatch):
    """A part with no filename is not an attachment. No filename anywhere → no attachments().get,
    and no `attachments` key on the row at all."""
    svc = _wire_attachment_gather(monkeypatch, [{"filename": "", "mimeType": "text/html",
                                                 "body": {"size": 20}}])
    emails = gg.gather_gmail("/fake/api.py", 25, 12)
    assert "attachments" not in emails[0]
    assert svc.attachment_gets == []


def test_the_sent_lane_never_fetches_attachments(monkeypatch):
    """v1 is the inbox. The sent lane is style exhaust — converting the files the user attached to
    their own mail would spend API calls and prompt budget telling them what they already know."""
    monkeypatch.setattr(gg, "_token_path",
                        lambda: pytest.fail("attachment lane touched on the sent path"))

    def fake_run(api, args, timeout=60):
        if args[:2] == ["gmail", "search"]:
            return [{"id": "s1", "subject": "my reply"}]
        return {"body": "sent body"}

    monkeypatch.setattr(gg, "_run", fake_run)
    sent = gg.gather_sent("/fake/api.py", 15, 10)
    assert sent[0]["isSent"] is True and "attachments" not in sent[0]


def test_an_attachment_failure_never_drops_the_email(monkeypatch):
    """Fail toward silence: Gmail erroring on the metadata call costs that email its attachments,
    never its place in the brief."""
    _wire_attachment_gather(monkeypatch, [], boom=True)
    emails = gg.gather_gmail("/fake/api.py", 25, 12)
    assert len(emails) == 1 and emails[0]["subject"] == "Q3 numbers"
    assert "attachments" not in emails[0]


def test_google_not_connected_costs_the_lane_nothing(monkeypatch):
    """No token file → the lane returns immediately, without building a client or importing the
    Google libs. The gather is otherwise untouched."""
    def fake_run(api, args, timeout=60):
        if args[:2] == ["gmail", "search"]:
            return [{"id": "m1", "subject": "Q3"}]
        return {"body": "b"}

    monkeypatch.setattr(gg, "_run", fake_run)
    monkeypatch.setattr(gg, "_token_path", lambda: "")
    monkeypatch.setattr(gg, "_gmail_service",
                        lambda: pytest.fail("client built without a token"))
    emails = gg.gather_gmail("/fake/api.py", 25, 12)
    assert len(emails) == 1 and "attachments" not in emails[0]


def test_an_oversized_attachment_is_named_and_never_downloaded(monkeypatch):
    """The byte cap is enforced BEFORE the download, so a 9MB file costs one metadata read and a
    name — not 9MB of the brief's wall clock."""
    parts = [{"filename": "video.mov", "mimeType": "video/quicktime",
              "body": {"size": gg.MAX_ATTACHMENT_BYTES + 1, "attachmentId": "att-big"}}]
    svc = _wire_attachment_gather(monkeypatch, parts, {"att-big": _b64(b"x" * 10)})
    atts = gg.gather_gmail("/fake/api.py", 25, 12)[0]["attachments"]
    assert atts == [{"filename": "video.mov", "unreadable": "too large to read"}]
    assert svc.attachment_gets == []                 # never fetched


def test_every_filename_is_listed_even_past_the_per_email_cap(monkeypatch):
    """ALL filenames are named; only the CONVERTED ones are capped. "There were two more files" is
    itself information the brief must not lose."""
    parts = [{"filename": f"f{i}.csv", "mimeType": "text/csv",
              "body": {"size": 8, "data": _b64(b"a,b\n1,2\n")}} for i in range(5)]
    _wire_attachment_gather(monkeypatch, parts)
    atts = gg.gather_gmail("/fake/api.py", 25, 12)[0]["attachments"]
    assert len(atts) == 5                                             # every file named
    assert sum(1 for a in atts if "text" in a) == gg.MAX_ATTACHMENTS_PER_EMAIL
    assert all(f"over the {gg.MAX_ATTACHMENTS_PER_EMAIL}-attachment limit" == a["unreadable"]
               for a in atts[gg.MAX_ATTACHMENTS_PER_EMAIL:])


def test_attachment_parts_walks_nested_multipart():
    """multipart/mixed wrapping multipart/alternative is the ordinary shape of a real email — a flat
    scan of payload["parts"] misses everything one level down."""
    payload = {"parts": [
        {"mimeType": "multipart/alternative", "filename": "",
         "parts": [{"filename": "", "mimeType": "text/plain", "body": {"size": 4}},
                   {"filename": "inner.pdf", "mimeType": "application/pdf",
                    "body": {"size": 100, "attachmentId": "deep"}}]},
        {"filename": "outer.csv", "mimeType": "text/csv", "body": {"size": 9}}]}
    names = [p["filename"] for p in gg._attachment_parts(payload)]
    assert names == ["inner.pdf", "outer.csv"]                # depth-first, message order


def test_a_download_failure_names_that_one_attachment_only(monkeypatch):
    """One attachment failing to download names IT — the others on the same email still convert."""
    parts = [{"filename": "good.csv", "mimeType": "text/csv",
              "body": {"size": 8, "data": _b64(b"a,b\n1,2\n")}},
             {"filename": "bad.csv", "mimeType": "text/csv",
              "body": {"size": 8, "attachmentId": "nope"}}]
    svc = _wire_attachment_gather(monkeypatch, parts)

    def _boom(*a, **k):
        raise RuntimeError("download failed")

    monkeypatch.setattr(gg, "_attachment_bytes",
                        lambda s, m, p: _boom() if p["filename"] == "bad.csv" else b"a,b\n1,2\n")
    atts = gg.gather_gmail("/fake/api.py", 25, 12)[0]["attachments"]
    assert "text" in atts[0] and atts[1]["unreadable"] == "could not be downloaded"
    assert svc is not None


def test_the_caps_have_one_owner():
    """gather_google IMPORTS the caps from attachments.py rather than keeping its own copy — the
    fetch side and the render side cannot state two different numbers. Asserted on the import
    statement itself, because equal-but-separately-declared constants is exactly the drift this
    guards against."""
    src = open(os.path.join(HERE, "..", "_shared", "scripts", "gather_google.py"),
               encoding="utf-8").read()
    assert "from attachments import" in src
    assert "MAX_ATTACHMENTS_PER_EMAIL" in src and "MAX_ATTACHMENT_BYTES" in src
    # …and nowhere does it declare one of its own
    assert not re.search(r"^MAX_ATTACHMENT\w*\s*=", src, re.M)
    assert (gg.MAX_ATTACHMENTS_PER_EMAIL, gg.MAX_ATTACHMENT_BYTES) == (3, 8_000_000)
