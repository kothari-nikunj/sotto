"""gather_google.py — normalizes google_api.py output to compose_brief's shapes; never crashes."""
import importlib.util, json, os

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
    monkeypatch.setattr(gg, "gather_gmail", lambda api, mx, bodies: captured.update(max=mx) or [])
    monkeypatch.setattr(gg, "gather_calendar", lambda *a: [])
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


def test_skip_gmail_calendar_only(tmp_path, monkeypatch):
    # meeting-prep uses --skip-gmail: gmail not even attempted; both files still written.
    called = {"gmail": False, "cal": False}
    monkeypatch.setattr(gg, "_find_google_api", lambda: "/fake/google_api.py")
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: True)   # don't shell out to pip in tests
    monkeypatch.setattr(gg, "gather_gmail", lambda *a: called.__setitem__("gmail", True) or [{"id": "x"}])
    monkeypatch.setattr(gg, "gather_calendar", lambda *a: called.__setitem__("cal", True) or [{"id": "e"}])
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--skip-gmail", "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    assert called["gmail"] is False and called["cal"] is True
    import json
    assert json.load(open(g)) == [] and len(json.load(open(c))) == 1
