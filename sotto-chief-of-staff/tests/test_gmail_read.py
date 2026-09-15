import base64
import importlib.util
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

    def run(_api, args, timeout=60):
        if args[2] == poll_gmail.SEARCH_QUERY:
            return [{"id": "ok", "snippet": "thin"}, {"id": "retry", "snippet": "thin"}]
        return []

    class Service:
        def close(self): pass

    monkeypatch.setattr(poll_gmail, "_run", run)
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
