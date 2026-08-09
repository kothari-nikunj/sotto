"""google_action.py — the WRITE side (send email / create calendar event), arg-building + guards."""
import importlib.util, json, os
import pytest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("google_action", os.path.join(HERE, "..", "_shared", "scripts", "google_action.py"))
ga = importlib.util.module_from_spec(spec); spec.loader.exec_module(ga)


def test_run_errors_clearly_when_cli_missing(monkeypatch):
    monkeypatch.setattr(ga, "_find_google_api", lambda: None)
    out = ga._run(["gmail", "send", "--to", "a@b.com", "--body", "hi"])
    assert out["status"] == "error" and "not found" in out["error"]


def test_gmail_reply_builds_exact_cli(monkeypatch, capsys):
    cap = {}
    monkeypatch.setattr(ga, "_run", lambda args: cap.__setitem__("args", args) or {"status": "sent", "threadId": "t"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "gmail-reply", "--message-id", "M1", "--body", "works for me"])
    ga.main()
    assert cap["args"] == ["gmail", "reply", "M1", "--body", "works for me"]
    assert json.loads(capsys.readouterr().out)["status"] == "sent"


def test_calendar_create_builds_exact_cli(monkeypatch, capsys):
    cap = {}
    monkeypatch.delenv("SOTTO_USER_EMAIL", raising=False)   # without it, attendees pass through untouched
    monkeypatch.setattr(ga, "_run", lambda args: cap.__setitem__("args", args) or {"status": "created", "htmlLink": "x"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "calendar-create", "--summary", "Sync",
                                     "--start", "2026-06-27T14:00:00-07:00", "--end", "2026-06-27T14:30:00-07:00",
                                     "--attendees", "a@x.com,b@y.com"])
    ga.main()
    assert cap["args"] == ["calendar", "create", "--summary", "Sync",
                           "--start", "2026-06-27T14:00:00-07:00", "--end", "2026-06-27T14:30:00-07:00",
                           "--attendees", "a@x.com,b@y.com"]


def test_calendar_create_adds_the_organizer_to_the_guest_list(monkeypatch, capsys):
    """An invite Sotto sends must look like one the user sent themselves: the native Calendar UI
    always lists the creator as a guest, so calendar-create appends the user's own address —
    otherwise the event renders '1 guest, 1 awaiting' with the organizer missing from the list."""
    cap = {}
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@nk-example.com")
    monkeypatch.setattr(ga, "_run", lambda args: cap.__setitem__("args", args) or {"status": "created"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "calendar-create", "--summary", "Sync",
                                     "--start", "2026-06-27T14:00:00-07:00", "--end", "2026-06-27T14:30:00-07:00",
                                     "--attendees", "ron@antimlabs.com"])
    ga.main()
    assert cap["args"][cap["args"].index("--attendees") + 1] == "ron@antimlabs.com,me@nk-example.com"


def test_calendar_create_uses_the_derived_google_account_email(tmp_path, monkeypatch, capsys):
    """The guest-list self-append reads the SAME chain the brief does: SOTTO_USER_EMAIL is an
    override, and with it unset the address the Google connect derived into settings.json answers."""
    cap = {}
    monkeypatch.delenv("SOTTO_USER_EMAIL", raising=False)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.json").write_text('{"google_account_email": "me@nk-example.com"}')
    assert ga._settings_email() == "me@nk-example.com"
    monkeypatch.setattr(ga, "_run", lambda args: cap.__setitem__("args", args) or {"status": "created"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "calendar-create", "--summary", "Sync",
                                     "--start", "2026-06-27T14:00:00-07:00", "--end", "2026-06-27T14:30:00-07:00",
                                     "--attendees", "ron@antimlabs.com"])
    ga.main()
    assert cap["args"][cap["args"].index("--attendees") + 1] == "ron@antimlabs.com,me@nk-example.com"
    assert json.loads(capsys.readouterr().out)["organizer_email"] == "me@nk-example.com"
    # env still wins over the derived setting
    monkeypatch.setenv("SOTTO_USER_EMAIL", "other@nk-example.com")
    assert ga._settings_email() == "other@nk-example.com"


def test_calendar_create_reports_a_missing_organizer_honestly(tmp_path, monkeypatch, capsys):
    """A silent miss is how '1 guest, 1 awaiting' invites ship — when Sotto knows no address at all
    (no env var, no connected Google) the result must say organizer_listed:false plus a plain note
    the skill surfaces to the user."""
    monkeypatch.delenv("SOTTO_USER_EMAIL", raising=False)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))   # no settings.json → nothing derived yet
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "created"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "calendar-create", "--summary", "Sync",
                                     "--start", "2026-06-27T14:00:00-07:00", "--end", "2026-06-27T14:30:00-07:00",
                                     "--attendees", "ron@antimlabs.com"])
    ga.main()
    out = json.loads(capsys.readouterr().out)
    assert out["organizer_listed"] is False
    assert "connect Google" in out["note"] and "SOTTO_USER_EMAIL" in out["note"]


def test_calendar_create_reports_the_appended_organizer(monkeypatch, capsys):
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@nk-example.com")
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "created"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "calendar-create", "--summary", "Sync",
                                     "--start", "2026-06-27T14:00:00-07:00", "--end", "2026-06-27T14:30:00-07:00",
                                     "--attendees", "ron@antimlabs.com"])
    ga.main()
    out = json.loads(capsys.readouterr().out)
    assert out["organizer_listed"] is True and out["organizer_email"] == "me@nk-example.com"


def test_calendar_create_never_duplicates_the_organizer(monkeypatch, capsys):
    cap = {}
    monkeypatch.setenv("SOTTO_USER_EMAIL", "Me@NK-Example.com")   # case-insensitive match
    monkeypatch.setattr(ga, "_run", lambda args: cap.__setitem__("args", args) or {"status": "created"})
    monkeypatch.setattr("sys.argv", ["google_action.py", "calendar-create", "--summary", "Sync",
                                     "--start", "2026-06-27T14:00:00-07:00", "--end", "2026-06-27T14:30:00-07:00",
                                     "--attendees", "ron@antimlabs.com,me@nk-example.com"])
    ga.main()
    assert cap["args"][cap["args"].index("--attendees") + 1] == "ron@antimlabs.com,me@nk-example.com"


# ── calendar-rsvp ────────────────────────────────────────────────────────────

def _event():
    """A board dinner where 'me' is a plain (self) attendee, plus an organizer and a peer."""
    return {
        "id": "EV1",
        "summary": "Board Dinner",
        "status": "confirmed",  # a real event's own status — must NOT be read as an error
        "start": {"dateTime": "2026-07-07T18:00:00-07:00"},
        "organizer": {"email": "chair@corp.com"},
        "attendees": [
            {"email": "chair@corp.com", "organizer": True, "responseStatus": "accepted"},
            {"email": "me@example.com", "self": True, "responseStatus": "needsAction"},
            {"email": "peer@corp.com", "displayName": "Peer", "responseStatus": "tentative"},
        ],
    }


def _self_and_others(patch_args):
    aj = patch_args[patch_args.index("--attendees-json") + 1]
    patched = json.loads(aj)
    self_e = [a for a in patched if a.get("self")][0]
    others = [a for a in patched if not a.get("self")]
    return self_e, others


@pytest.mark.parametrize("response", ["accepted", "declined", "tentative"])
def test_calendar_rsvp_sets_status_and_preserves_others(monkeypatch, response):
    cap = {}

    def fake_run(args):
        if args[1] == "get":
            return _event()
        cap["patch"] = args
        return {"status": "patched"}

    monkeypatch.setattr(ga, "_run", fake_run)
    out = ga._rsvp("EV1", response, "primary", "")
    assert out == {"status": "rsvped", "event_id": "EV1", "response": response,
                   "summary": "Board Dinner", "start": "2026-07-07T18:00:00-07:00"}
    p = cap["patch"]
    # sendUpdates=all so the organizer is notified
    assert "--send-updates" in p and p[p.index("--send-updates") + 1] == "all"
    self_e, others = _self_and_others(p)
    assert self_e["responseStatus"] == response
    # every other attendee is byte-identical to the fetched event
    assert others == [a for a in _event()["attendees"] if not a.get("self")]


def test_calendar_rsvp_main_dispatch(monkeypatch, capsys):
    def fake_run(args):
        return _event() if args[1] == "get" else {"status": "patched"}

    monkeypatch.setattr(ga, "_run", fake_run)
    monkeypatch.setattr("sys.argv", ["google_action.py", "calendar-rsvp",
                                     "--event-id", "EV1", "--response", "declined"])
    ga.main()
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "rsvped" and out["response"] == "declined" and out["event_id"] == "EV1"


def test_calendar_rsvp_comment_and_nonprimary_calendar(monkeypatch):
    calls = []

    def fake_run(args):
        calls.append(args)
        return _event() if args[1] == "get" else {"status": "patched"}

    monkeypatch.setattr(ga, "_run", fake_run)
    ga._rsvp("EV1", "tentative", "work@corp.com", "running late")
    get_args, patch_args = calls[0], calls[1]
    assert get_args[get_args.index("--calendar") + 1] == "work@corp.com"
    assert "--calendar" in patch_args
    self_e, _ = _self_and_others(patch_args)
    assert self_e["comment"] == "running late"


def test_calendar_rsvp_self_via_organizer_email(monkeypatch):
    """No attendee flagged self:true — fall back to the account's email (a self organizer)."""
    ev = {
        "id": "EV2", "summary": "Sync", "start": {"dateTime": "2026-07-08T09:00:00-07:00"},
        "organizer": {"self": True, "email": "me@example.com"},
        "attendees": [
            {"email": "me@example.com", "organizer": True, "responseStatus": "needsAction"},
            {"email": "x@corp.com", "responseStatus": "needsAction"},
        ],
    }
    cap = {}

    def fake_run(args):
        if args[1] == "get":
            return ev
        cap["patch"] = args
        return {"status": "patched"}

    monkeypatch.setattr(ga, "_run", fake_run)
    out = ga._rsvp("EV2", "accepted", "primary", "")
    assert out["status"] == "rsvped"
    aj = json.loads(cap["patch"][cap["patch"].index("--attendees-json") + 1])
    me = [a for a in aj if a["email"] == "me@example.com"][0]
    assert me["responseStatus"] == "accepted"


def test_calendar_rsvp_organizer_no_attendees_errors(monkeypatch):
    monkeypatch.setattr(ga, "_run", lambda args: {
        "id": "EV3", "summary": "1:1", "start": {"dateTime": "2026-07-07T10:00:00-07:00"},
        "organizer": {"self": True, "email": "me@example.com"}, "attendees": []})
    out = ga._rsvp("EV3", "accepted", "primary", "")
    assert out["status"] == "error" and "organizer" in out["error"].lower()


def test_calendar_rsvp_organizer_not_among_attendees_errors(monkeypatch):
    monkeypatch.setattr(ga, "_run", lambda args: {
        "id": "EV4", "summary": "S", "start": {"date": "2026-07-09"},
        "organizer": {"self": True, "email": "me@example.com"},
        "attendees": [{"email": "a@corp.com", "responseStatus": "accepted"},
                      {"email": "b@corp.com", "responseStatus": "needsAction"}]})
    out = ga._rsvp("EV4", "declined", "primary", "")
    assert out["status"] == "error" and "organizer" in out["error"].lower()


def test_calendar_rsvp_event_not_found_errors(monkeypatch):
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "error", "error": "404 not found"})
    out = ga._rsvp("NOPE", "accepted", "primary", "")
    assert out["status"] == "error" and "not found" in out["error"].lower()


def test_calendar_rsvp_invalid_response_errors(monkeypatch):
    monkeypatch.setattr(ga, "_run", lambda args: pytest.fail("should not fetch on a bad response"))
    out = ga._rsvp("EV1", "maybe", "primary", "")
    assert out["status"] == "error" and "accepted|declined|tentative" in out["error"]


def test_calendar_rsvp_unsupported_host_cli_falls_back(monkeypatch):
    """A host google_api.py without `calendar get`/`patch` makes argparse reject the subcommand with an
    'invalid choice' / 'usage:' error. That must surface as a DISTINCT capability error + deep_link
    fallback, not a confusing 'event not found: usage:...'."""
    usage_err = ("argument command: invalid choice: 'get' (choose from 'send', 'reply')\n"
                 "usage: google_api.py calendar [-h] ...")
    monkeypatch.setattr(ga, "_run", lambda args: {"status": "error", "error": usage_err})
    out = ga._rsvp("EV1", "accepted", "primary", "")
    assert out["status"] == "error"
    assert out["fallback"] == "deep_link"
    assert "RSVP by API unavailable" in out["error"]
    assert "usage:" not in out["error"]                  # no raw usage dump leaks to the user


def test_looks_unsupported_subcommand_detection():
    assert ga._looks_unsupported_subcommand("error: argument command: invalid choice: 'get'")
    assert ga._looks_unsupported_subcommand("usage: google_api.py [-h] ...")
    assert not ga._looks_unsupported_subcommand("404 not found")
    assert not ga._looks_unsupported_subcommand("")


def _cap_run(monkeypatch, responses):
    """Capture _run invocations; pop canned responses in order."""
    calls = []
    def fake(args):
        calls.append(list(args))
        return responses.pop(0) if responses else {"status": "created", "id": "E1"}
    monkeypatch.setattr(ga, "_run", fake)
    return calls


def test_calendar_create_forwards_location_and_description(monkeypatch, capsys):
    calls = _cap_run(monkeypatch, [{"status": "created", "id": "E1", "htmlLink": "http://x"}])
    monkeypatch.setattr("sys.argv", ["google_action.py", "calendar-create", "--summary", "Deck review",
                                     "--start", "2026-08-08T14:00:00-07:00", "--end", "2026-08-08T14:30:00-07:00",
                                     "--attendees", "alex@acme.com", "--location", "Blue Bottle, Ferry Building"])
    ga.main()
    out = json.loads(capsys.readouterr().out)
    assert calls[0][-2:] == ["--location", "Blue Bottle, Ferry Building"]
    assert out["location_attached"] is True


def test_calendar_create_location_falls_back_to_patch_on_old_host_cli(monkeypatch, capsys):
    # Old host CLI rejects the flag with an argparse usage dump → bare create, then patch.
    calls = _cap_run(monkeypatch, [
        {"status": "error", "error": "usage: google_api.py calendar create ... unrecognized arguments: --location"},
        {"status": "created", "id": "E9", "htmlLink": "http://x"},
        {"status": "patched"},
    ])
    monkeypatch.setattr("sys.argv", ["google_action.py", "calendar-create", "--summary", "S",
                                     "--start", "A", "--end", "B", "--location", "HQ"])
    ga.main()
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "E9" and out["location_attached"] is True
    assert calls[1] == ["calendar", "create", "--summary", "S", "--start", "A", "--end", "B"]
    assert calls[2][:3] == ["calendar", "patch", "E9"] and "--location" in calls[2]


def test_calendar_create_location_patch_failure_is_honest(monkeypatch, capsys):
    calls = _cap_run(monkeypatch, [
        {"status": "error", "error": "usage: ... unrecognized arguments"},
        {"status": "created", "id": "E9"},
        {"status": "error", "error": "usage: ... invalid choice: 'patch'"},
    ])
    monkeypatch.setattr("sys.argv", ["google_action.py", "calendar-create", "--summary", "S",
                                     "--start", "A", "--end", "B", "--location", "HQ"])
    ga.main()
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "E9" and out["location_attached"] is False and "note" in out
