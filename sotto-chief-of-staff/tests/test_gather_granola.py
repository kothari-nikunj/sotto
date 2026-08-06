"""gather_granola.py — deterministic Granola gather (MCP lane + REST break-glass), mapped to the
EXISTING granola_meetings shape the pipeline consumes; fail-empty, exit 0, like gather_google."""
import datetime
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "_shared", "lib"))

spec = importlib.util.spec_from_file_location(
    "gather_granola", os.path.join(HERE, "..", "_shared", "scripts", "gather_granola.py"))
gg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gg)

import render_local as rl  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _iso_hours_ago(h):
    return (NOW - datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeMcp:
    """Injectable stand-in for McpClient: scripted tools + meetings + per-id transcripts."""
    def __init__(self, tools, meetings, transcripts=None):
        self.tools = tools
        self.meetings = meetings
        self.transcripts = transcripts or {}
        self.calls = []

    def initialize(self):
        return {}

    def list_tools(self):
        return self.tools

    def call_tool(self, name, arguments=None, timeout=60):
        self.calls.append((name, arguments))
        if "transcript" in name.lower():
            return self.transcripts.get((arguments or {}).get("meeting_id"))
        return self.meetings


GRANOLA_TOOLS = [{"name": "list_meetings"}, {"name": "get_meetings"},
                 {"name": "get_meeting_transcript"}, {"name": "list_meeting_folders"},
                 {"name": "get_account_info"}, {"name": "query_granola_meetings"}]

RAW_MEETINGS = [
    {"id": "m1", "title": "Acme Sync", "start_time": _iso_hours_ago(2),
     "attendees": [{"name": "Sarah", "email": "sarah@acme.com"}, "dev@acme.com"],
     "notes_markdown": "my typed notes", "summary": "Discussed the pilot rollout."},
    {"id": "m2", "title": "Old 1:1", "start_time": _iso_hours_ago(24 * 10),
     "attendee_emails": ["jane@x.com"], "ai_summary": "Roadmap chat."},
    {"id": "m3", "title": "Ancient", "start_time": _iso_hours_ago(24 * 20)},   # outside --days 14
]


def test_mcp_happy_path_maps_to_existing_granola_shape():
    client = FakeMcp(GRANOLA_TOOLS, RAW_MEETINGS, {"m1": {"transcript": "you: I'll send the deck"}})
    meetings, warnings = gg.gather_mcp(days=14, since_hours=36, client=client, now=NOW)
    assert [m["title"] for m in meetings] == ["Acme Sync", "Old 1:1"]   # m3 outside the window
    m1, m2 = meetings
    # EXACT existing shape: render_local._format_granola_meetings + compose_followup contract.
    assert set(m1) == {"title", "date", "time", "attendee_emails", "your_notes", "ai_summary", "transcript"}
    assert m1["date"] == "2026-08-06" and m1["time"] == "10:00"
    assert m1["attendee_emails"] == ["sarah@acme.com", "dev@acme.com"]  # {email} dicts AND bare strings
    assert m1["your_notes"] == "my typed notes"                         # notes_markdown variant mapped
    assert m1["ai_summary"] == "Discussed the pilot rollout."           # summary variant mapped
    assert m1["transcript"] == "you: I'll send the deck"                # recent → transcript fetched
    assert "transcript" not in m2                                       # 10 days old → no transcript
    assert m2["ai_summary"] == "Roadmap chat."
    # The list tool got the Mac client's canonical name + args.
    assert client.calls[0] == ("list_meetings", {"limit": 50})
    assert ("get_meeting_transcript", {"meeting_id": "m1"}) in client.calls


def test_mcp_output_renders_through_render_local_smoke():
    client = FakeMcp(GRANOLA_TOOLS, RAW_MEETINGS, {"m1": "raw transcript"})
    meetings, _ = gg.gather_mcp(days=14, since_hours=36, client=client, now=NOW)
    rendered = rl._format_granola_meetings({"granola_meetings": meetings})
    assert "Acme Sync" in rendered and "Discussed the pilot rollout." in rendered
    assert "sarah@acme.com" in rendered


def test_tool_name_tolerance_exact_alternates():
    client = FakeMcp([{"name": "get_meetings"}, {"name": "get_transcript"}],
                     [RAW_MEETINGS[0]], {"m1": "t"})
    meetings, _ = gg.gather_mcp(days=14, since_hours=36, client=client, now=NOW)
    assert meetings[0]["transcript"] == "t"
    assert client.calls[0][0] == "get_meetings"
    assert client.calls[1][0] == "get_transcript"


def test_tool_name_tolerance_fuzzy_fallback():
    # Unknown-but-recognizable names still match; folder/account tools are never mistaken for the list.
    client = FakeMcp([{"name": "list_meeting_folders"}, {"name": "recent_meetings"},
                      {"name": "fetch_meeting_transcript"}], [RAW_MEETINGS[0]], {"m1": "t"})
    meetings, _ = gg.gather_mcp(days=14, since_hours=36, client=client, now=NOW)
    assert client.calls[0][0] == "recent_meetings"
    assert meetings[0]["transcript"] == "t"


def test_no_transcript_tool_still_returns_notes_with_warning():
    client = FakeMcp([{"name": "list_meetings"}], RAW_MEETINGS[:2])
    meetings, warnings = gg.gather_mcp(days=14, since_hours=36, client=client, now=NOW)
    assert len(meetings) == 2 and all("transcript" not in m for m in meetings)
    assert any("no transcript tool" in w for w in warnings)


def test_transcripts_window_respected():
    # 50h-old meeting with a 36h window → transcript NOT fetched even though the tool exists.
    old = dict(RAW_MEETINGS[0], id="m9", start_time=_iso_hours_ago(50))
    client = FakeMcp(GRANOLA_TOOLS, [old], {"m9": "should not be fetched"})
    meetings, _ = gg.gather_mcp(days=14, since_hours=36, client=client, now=NOW)
    assert "transcript" not in meetings[0]
    assert all(name != "get_meeting_transcript" for name, _ in client.calls)


def test_meetings_envelope_dict_unwrapped():
    client = FakeMcp([{"name": "list_meetings"}], None)
    client.meetings = {"meetings": RAW_MEETINGS[:1]}
    meetings, _ = gg.gather_mcp(days=14, since_hours=0, client=client, now=NOW)
    assert meetings[0]["title"] == "Acme Sync"


def test_mcp_401_mid_session_refreshes_once_and_retries():
    from connector_tokens import ConnectorAuthError

    class Stale(FakeMcp):
        def call_tool(self, name, arguments=None, timeout=60):
            raise ConnectorAuthError("401")

    made = []

    def factory(force_refresh=False):
        made.append(force_refresh)
        if force_refresh:
            return FakeMcp(GRANOLA_TOOLS, RAW_MEETINGS[:1], {"m1": "t"})
        return Stale(GRANOLA_TOOLS, [])

    meetings, _ = gg.gather_mcp_with_retry(days=14, since_hours=36, client_factory=factory, now=NOW)
    assert made == [False, True]                    # exactly one forced refresh, then success
    assert meetings[0]["transcript"] == "t"


# --- REST break-glass lane ---------------------------------------------------

def test_rest_mode_same_shape_with_transcript(monkeypatch):
    requests = []

    def http_get(url, headers, timeout=30):
        requests.append((url, headers))
        if "/notes/n1" in url:
            return 200, json.dumps({"id": "n1", "transcript": "full transcript",
                                    "notes": "detail notes"}).encode()
        return 200, json.dumps({"notes": [
            {"id": "n1", "title": "Board Prep", "created_at": _iso_hours_ago(3),
             "attendees": ["a@b.com"], "summary": "prep points"},
            {"id": "n2", "title": "Last Week", "created_at": _iso_hours_ago(24 * 5),
             "summary": "old summary"}]}).encode()

    throttle = gg._Throttle(sleep=lambda s: None, monotonic=lambda: 0.0)
    meetings, warnings = gg.gather_rest("grn_key", days=14, since_hours=36,
                                        http_get=http_get, throttle=throttle, now=NOW)
    assert requests[0][1]["Authorization"] == "Bearer grn_key"
    assert "created_after=" in requests[0][0] and requests[0][0].startswith(gg.REST_BASE + "/notes?")
    b1, b2 = meetings
    assert set(b1) == {"title", "date", "time", "attendee_emails", "your_notes", "ai_summary", "transcript"}
    assert b1["transcript"] == "full transcript"
    assert b1["your_notes"] == "detail notes"       # detail backfills notes missing from the list item
    assert b1["ai_summary"] == "prep points"
    assert "transcript" not in b2                   # outside the transcript window → list data only
    assert not warnings


def test_rest_pagination_follows_cursor():
    pages = [({"notes": [{"id": "n1", "title": "A", "created_at": _iso_hours_ago(200)}],
               "next_cursor": "c2"}),
             ({"notes": [{"id": "n2", "title": "B", "created_at": _iso_hours_ago(200)}],
               "next_cursor": None})]
    urls = []

    def http_get(url, headers, timeout=30):
        urls.append(url)
        return 200, json.dumps(pages[len(urls) - 1]).encode()

    throttle = gg._Throttle(sleep=lambda s: None, monotonic=lambda: 0.0)
    meetings, _ = gg.gather_rest("k", days=14, since_hours=0, http_get=http_get,
                                 throttle=throttle, now=NOW)
    assert [m["title"] for m in meetings] == ["A", "B"]
    assert "cursor=c2" in urls[1]


def test_rest_throttle_spaces_requests_at_5rps():
    sleeps = []
    clock = iter([0.0, 0.0, 0.05, 0.05])
    t = gg._Throttle(sleep=sleeps.append, monotonic=lambda: next(clock))
    t.wait()                                        # first request — no sleep
    t.wait()                                        # 0.05s later → sleep the remaining 0.15s
    assert sleeps == [0.15000000000000002] or abs(sleeps[0] - 0.15) < 1e-9


# --- fail-empty main ---------------------------------------------------------

def test_main_no_connector_no_key_writes_empty_and_exits_0(tmp_path, monkeypatch, capsys):
    from connector_tokens import ConnectorMissing
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("GRANOLA_API_KEY", raising=False)
    monkeypatch.setattr(gg, "gather_mcp_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectorMissing("no token")))
    out = tmp_path / "granola.json"
    monkeypatch.setattr("sys.argv", ["gather_granola.py", "--out", str(out)])
    gg.main()                                       # must not raise (exit-0-fail-empty)
    assert json.load(open(out)) == {"meetings": []}
    printed = capsys.readouterr().out
    assert "[gather_granola] 0 meetings" in printed and "connect Granola" in printed


def test_main_auth_error_fails_empty_with_reconnect_warning(tmp_path, monkeypatch, capsys):
    from connector_tokens import ConnectorAuthError
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.delenv("GRANOLA_API_KEY", raising=False)
    monkeypatch.setattr(gg, "gather_mcp_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectorAuthError("revoked")))
    out = tmp_path / "granola.json"
    monkeypatch.setattr("sys.argv", ["gather_granola.py", "--out", str(out)])
    gg.main()
    assert json.load(open(out)) == {"meetings": []}
    assert "reconnect" in capsys.readouterr().out


def test_main_writes_meetings_envelope_and_counts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    sample = [{"title": "Acme Sync", "date": "2026-08-06", "time": "10:00",
               "attendee_emails": ["sarah@acme.com"], "your_notes": None,
               "ai_summary": "Pilot rollout.", "transcript": "t"},
              {"title": "Notes Only", "date": "2026-08-05", "time": "09:00",
               "attendee_emails": [], "your_notes": "jotted", "ai_summary": None}]
    monkeypatch.setattr(gg, "gather_mcp_with_retry", lambda *a, **k: (list(sample), []))
    out = tmp_path / "granola.json"
    monkeypatch.setattr("sys.argv", ["gather_granola.py", "--out", str(out)])
    gg.main()
    payload = json.load(open(out))
    assert payload == {"meetings": sample}          # compose_brief's fold-in accepts {meetings:[…]}
    printed = capsys.readouterr().out
    assert "2 meetings (1 with transcripts, 2 with notes)" in printed


def test_main_falls_back_to_rest_when_key_present(tmp_path, monkeypatch):
    from connector_tokens import ConnectorMissing
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("GRANOLA_API_KEY", "grn_test")
    monkeypatch.setattr(gg, "gather_mcp_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectorMissing("no token")))
    called = {}
    monkeypatch.setattr(gg, "gather_rest",
                        lambda key, days, since, **k: called.update(key=key, days=days) or ([], []))
    out = tmp_path / "granola.json"
    monkeypatch.setattr("sys.argv", ["gather_granola.py", "--out", str(out), "--days", "7"])
    gg.main()
    assert called == {"key": "grn_test", "days": 7}
    assert json.load(open(out)) == {"meetings": []}
