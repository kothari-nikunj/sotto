import base64
import importlib.util
import json
import os
import sys
import types
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
LIB = os.path.join(ROOT, "_shared", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import gmail_read
from gmail_read import extract_body, message_fields


def _b64(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def test_nested_multipart_prefers_complete_plain_body_and_ignores_text_attachment():
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("Full nested plain body")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>HTML body</p>")}},
        ]},
        {"mimeType": "text/plain", "filename": "notes.txt",
         "body": {"data": _b64("attachment must not become email body")}},
    ]}
    assert extract_body(payload) == "Full nested plain body"


def test_nested_html_is_read_when_message_has_no_plain_alternative():
    payload = {"mimeType": "multipart/related", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<p>Hello <b>Dinki</b></p><p>Can you review?</p>")}}
        ]}
    ]}
    assert extract_body(payload) == "Hello Dinki\nCan you review?"


def test_html_fallback_drops_script_and_style_content():
    assert extract_body({"mimeType": "text/html", "body": {"data": _b64(
        "<style>.secret{display:none}</style><p>Visible</p><script>steal()</script>")}}) == "Visible"


def test_attachment_only_message_is_a_successful_empty_body():
    full = message_fields({"id": "a", "payload": {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "application/pdf", "filename": "brief.pdf",
         "body": {"attachmentId": "att", "size": 10}}
    ]}})
    assert full["body"] == "" and full["id"] == "a"


def test_missing_nonempty_text_data_is_a_failed_read_not_an_empty_message():
    with pytest.raises(RuntimeError, match="missing its encoded data"):
        extract_body({"mimeType": "text/plain", "body": {"size": 42}})


def test_inline_binary_part_does_not_get_decoded_as_message_text():
    payload = {"mimeType": "multipart/related", "parts": [
        {"mimeType": "text/plain", "body": {"data": _b64("hello")}},
        {"mimeType": "image/png", "body": {"size": 99, "attachmentId": "inline-image"}},
    ]}
    assert extract_body(payload) == "hello"


def test_missing_mime_payload_is_a_failed_full_read():
    with pytest.raises(RuntimeError, match="missing its MIME payload"):
        message_fields({"id": "broken"})


def test_google_client_transport_has_the_same_thirty_second_bound(monkeypatch):
    seen = {}

    class Credentials:
        @staticmethod
        def from_authorized_user_file(path):
            seen["token"] = path
            return "credentials"

    class Http:
        def __init__(self, timeout):
            seen["timeout"] = timeout

    class AuthorizedHttp:
        def __init__(self, credentials, http):
            seen["credentials"] = credentials
            seen["raw_http"] = http
            seen["authorized_http"] = self

    def build(api, version, **kwargs):
        seen.update(api=api, version=version, build_kwargs=kwargs)
        return "service"

    monkeypatch.setattr(gmail_read, "token_path", lambda: "/fake/google_token.json")
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.oauth2", types.ModuleType("google.oauth2"))
    credentials_mod = types.ModuleType("google.oauth2.credentials")
    credentials_mod.Credentials = Credentials
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", credentials_mod)
    discovery_mod = types.ModuleType("googleapiclient.discovery")
    discovery_mod.build = build
    monkeypatch.setitem(sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery_mod)
    auth_http_mod = types.ModuleType("google_auth_httplib2")
    auth_http_mod.AuthorizedHttp = AuthorizedHttp
    monkeypatch.setitem(sys.modules, "google_auth_httplib2", auth_http_mod)
    httplib2_mod = types.ModuleType("httplib2")
    httplib2_mod.Http = Http
    monkeypatch.setitem(sys.modules, "httplib2", httplib2_mod)

    assert gmail_read.google_service("gmail", "v1") == "service"
    assert seen["timeout"] == 30 and seen["credentials"] == "credentials"
    assert seen["build_kwargs"] == {"http": seen["authorized_http"], "cache_discovery": False}


def test_poll_defers_only_failed_full_reads_and_does_not_mark_them_seen(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "poll_gmail_retry", os.path.join(ROOT, "event-triage", "scripts", "poll_gmail.py"))
    poll_gmail = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(poll_gmail)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(poll_gmail, "_find_google_api", lambda: "/fake/google_api.py")

    class Execute:
        def __init__(self, value): self.value = value
        def execute(self): return self.value
    class Messages:
        def list(self, **kwargs):
            return Execute({"messages": []} if "in:sent" in kwargs["q"] else
                           {"messages": [{"id": "ok", "snippet": "thin"},
                                         {"id": "retry", "snippet": "thin"}]})
    class Users:
        def messages(self): return Messages()
    class Service:
        def users(self): return Users()
        def close(self): pass

    monkeypatch.setattr(poll_gmail, "gmail_service", Service)
    attempts = []

    def fetch(_service, mid):
        attempts.append(mid)
        if mid == "retry":
            raise RuntimeError("transient read failure")
        return {"id": mid, "from": "a@example.com", "body": "complete body"}

    monkeypatch.setattr(poll_gmail, "fetch_message", fetch)
    first = poll_gmail.poll()
    assert [event["rowid"] for event in first] == ["ok"]
    poll_gmail.acknowledge([event["rowid"] for event in first])
    second = poll_gmail.poll()
    assert second == []
    assert attempts == ["ok", "retry", "retry"]
    assert poll_gmail._load_seen() == ["ok"]


def test_poll_page_cursor_waits_for_failed_id_and_resumes_next_page_after_restart(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "poll_gmail_pages", os.path.join(ROOT, "event-triage", "scripts", "poll_gmail.py"))
    pg = importlib.util.module_from_spec(spec); spec.loader.exec_module(pg)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(pg, "_find_google_api", lambda: "/fake/google_api.py")
    pages = {None: ({"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"}),
             "p2": ({"messages": [{"id": "c"}]})}
    class Execute:
        def __init__(self, value): self.value = value
        def execute(self): return self.value
    class Messages:
        def list(self, **kwargs):
            return Execute({"messages": []} if "in:sent" in kwargs["q"]
                           else pages[kwargs.get("pageToken")])
    class Users:
        def messages(self): return Messages()
    class Service:
        def users(self): return Users()
        def close(self): pass
    monkeypatch.setattr(pg, "gmail_service", Service)
    failed = {"b"}
    def fetch(_service, mid):
        if mid in failed:
            raise RuntimeError("temporary")
        return {"id": mid, "from": "a@example.com", "body": "body", "date": "2026-09-15T22:00:00Z"}
    monkeypatch.setattr(pg, "fetch_message", fetch)
    monkeypatch.setattr(pg.time, "time", lambda: 1789516800)
    first = pg.poll()
    assert [e["rowid"] for e in first] == ["a"] and first[0]["_sotto_catchup"] is True
    pg.acknowledge(["a"])
    assert pg._load_state()["lanes"]["inbox"]["page_ids"] == ["a", "b"]
    failed.clear()
    assert [e["rowid"] for e in pg.poll()] == ["b"]
    pg.acknowledge(["b"])
    assert [e["rowid"] for e in pg.poll()] == ["c"]


def test_seen_only_overlap_page_advances_without_receiver_ack(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "poll_gmail_overlap", os.path.join(ROOT, "event-triage", "scripts", "poll_gmail.py"))
    pg = importlib.util.module_from_spec(spec); spec.loader.exec_module(pg)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(pg, "_find_google_api", lambda: "/fake/google_api.py")
    pg._save_state({"seen": ["known"], "lanes": {}})
    class Result:
        def __init__(self, value): self.value = value
        def execute(self): return self.value
    class Messages:
        def list(self, **kwargs):
            return Result({"messages": []} if "in:sent" in kwargs["q"] else
                          {"messages": [{"id": "known"}], "nextPageToken": "p2"})
    class Service:
        def users(self): return type("Users", (), {"messages": lambda self: Messages()})()
        def close(self): pass
    monkeypatch.setattr(pg, "gmail_service", Service)
    assert pg.poll() == []
    lane = pg._load_state()["lanes"]["inbox"]
    assert lane["page_ids"] == [] and lane["page_token"] == "p2"


def test_initial_paginated_query_keeps_fixed_bounds_when_clock_advances(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "poll_gmail_fixed_query", os.path.join(ROOT, "event-triage", "scripts", "poll_gmail.py"))
    pg = importlib.util.module_from_spec(spec); spec.loader.exec_module(pg)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(pg, "_find_google_api", lambda: "/fake/google_api.py")
    clock = {"now": 1789516800}
    monkeypatch.setattr(pg.time, "time", lambda: clock["now"])
    inbox_calls = []

    class Result:
        def __init__(self, value): self.value = value
        def execute(self): return self.value
    class Messages:
        def list(self, **kwargs):
            if "in:sent" in kwargs["q"]:
                return Result({"messages": []})
            inbox_calls.append(kwargs)
            if kwargs.get("pageToken") == "p2":
                return Result({"messages": [{"id": "b"}]})
            return Result({"messages": [{"id": "a"}], "nextPageToken": "p2"})
    class Service:
        def users(self): return type("Users", (), {"messages": lambda self: Messages()})()
        def close(self): pass

    monkeypatch.setattr(pg, "gmail_service", Service)
    monkeypatch.setattr(pg, "fetch_message", lambda _service, mid: {
        "id": mid, "from": "a@example.com", "body": "body"})
    assert [event["rowid"] for event in pg.poll()] == ["a"]
    initial_cursor = clock["now"] - pg.RECOVERY_LOOKBACK_SECONDS
    assert pg._load_state()["lanes"]["inbox"]["cursor"] == initial_cursor
    pg.acknowledge(["a"])
    clock["now"] += 300
    assert [event["rowid"] for event in pg.poll()] == ["b"]
    assert inbox_calls[0]["q"] == inbox_calls[1]["q"]
    assert inbox_calls[1]["pageToken"] == "p2"


def test_established_cursor_covers_a_full_day_per_slice_without_skipping_older_gap(tmp_path,
                                                                                   monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "poll_gmail_long_gap", os.path.join(ROOT, "event-triage", "scripts", "poll_gmail.py"))
    pg = importlib.util.module_from_spec(spec); spec.loader.exec_module(pg)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(pg, "_find_google_api", lambda: "/fake/google_api.py")
    cursor = 1789000000
    now = cursor + 3 * 24 * 3600
    pg._save_state({"seen": [], "lanes": {"inbox": {"cursor": cursor},
                                            "sent": {"cursor": now}}})
    calls = []
    class Result:
        def __init__(self, value): self.value = value
        def execute(self): return self.value
    class Messages:
        def list(self, **kwargs):
            calls.append(kwargs)
            return Result({"messages": [{"id": "old"}]} if "in:inbox" in kwargs["q"] else
                          {"messages": []})
    class Service:
        def users(self): return type("Users", (), {"messages": lambda self: Messages()})()
        def close(self): pass
    monkeypatch.setattr(pg, "gmail_service", Service)
    monkeypatch.setattr(pg.time, "time", lambda: now)
    monkeypatch.setattr(pg, "fetch_message", lambda _service, mid: {
        "id": mid, "from": "a@example.com", "body": "body", "date": str(now * 1000)})

    events = pg.poll()
    assert [event["rowid"] for event in events] == ["old"]
    assert events[0]["_sotto_catchup"] is True
    assert calls[0]["q"] == f"in:inbox after:{cursor} before:{cursor + 24 * 3600 + 1}"
    pg.acknowledge(["old"])
    assert pg._load_state()["lanes"]["inbox"]["cursor"] == cursor + 24 * 3600


def test_client_without_bounded_pagination_fails_without_advancing_cursor(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "poll_gmail_no_legacy_fallback", os.path.join(ROOT, "event-triage", "scripts", "poll_gmail.py"))
    pg = importlib.util.module_from_spec(spec); spec.loader.exec_module(pg)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(pg, "_find_google_api", lambda: "/fake/google_api.py")
    pg._save_state({"seen": [], "lanes": {"inbox": {"cursor": 123}, "sent": {}}})
    monkeypatch.setattr(pg, "gmail_service", lambda: object())

    with pytest.raises(RuntimeError, match="bounded paginated"):
        pg.poll()
    assert pg._load_state()["lanes"]["inbox"] == {"cursor": 123}


def test_acknowledge_propagates_failed_atomic_save(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "poll_gmail_save_failure", os.path.join(ROOT, "event-triage", "scripts", "poll_gmail.py"))
    pg = importlib.util.module_from_spec(spec); spec.loader.exec_module(pg)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    pg._save_state({"seen": ["old"], "lanes": {}})
    monkeypatch.setattr(pg.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        pg.acknowledge(["new"])
    assert pg._load_seen() == ["old"]


@pytest.mark.parametrize("contents", [
    "{not json",
    json.dumps({"seen": {}, "lanes": {}}),
    json.dumps({"seen": [], "lanes": {"inbox": []}}),
    json.dumps({"seen": [], "lanes": {"inbox": {"page_ids": "a"}}}),
])
def test_malformed_persisted_state_fails_closed(tmp_path, monkeypatch, contents):
    spec = importlib.util.spec_from_file_location(
        "poll_gmail_bad_state", os.path.join(ROOT, "event-triage", "scripts", "poll_gmail.py"))
    pg = importlib.util.module_from_spec(spec); spec.loader.exec_module(pg)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    path = tmp_path / "events" / "gmail_seen.json"
    path.parent.mkdir()
    path.write_text(contents)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        pg._load_state()
