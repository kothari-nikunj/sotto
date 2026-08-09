"""connector_tokens.py — the read/refresh side of the receiver's MCP connector token files.

Contract under test (shared with the receiver, which writes these files on OAuth):
$SOTTO_DATA/connectors/<service>.json with {service, access_token, refresh_token, expires_at,
token_endpoint, client_id, resource, mcp_url, obtained_at}. Refresh is OAuth 2.1 public-client
(client_id, NO secret), atomic write-back (0600), refresh-token rotation honored."""
import json
import os
import stat
import sys
import urllib.parse

HERE = os.path.dirname(__file__)

import connector_tokens as ct  # noqa: E402

NOW = 1_754_000_000.0


def _write_token(tmp_path, monkeypatch, **overrides):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    rec = {"service": "granola", "access_token": "tok-old", "refresh_token": "rt-1",
           "expires_at": NOW + 3600, "token_endpoint": "https://mcp-auth.granola.ai/oauth/token",
           "client_id": "dcr-client-1", "resource": "https://mcp.granola.ai/mcp",
           "mcp_url": "https://mcp.granola.ai/mcp", "obtained_at": NOW - 100}
    rec.update(overrides)
    path = tmp_path / "connectors" / "granola.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec))
    return path, rec


def _no_http(*a, **k):
    raise AssertionError("HTTP must not be called for a fresh token")


def test_fresh_token_returned_without_refresh(tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch)
    tok, url = ct.get_access_token("granola", http=_no_http, _now=NOW)
    assert tok == "tok-old" and url == "https://mcp.granola.ai/mcp"


def test_missing_file_raises_connector_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    try:
        ct.get_access_token("granola", http=_no_http)
        raise AssertionError("expected ConnectorMissing")
    except ct.ConnectorMissing as e:
        assert "granola" in str(e)


def test_expiring_token_refreshes_and_rewrites_file(tmp_path, monkeypatch):
    # expires within the 120s window → POST token_endpoint with grant_type=refresh_token +
    # client_id and NO client secret; file rewritten atomically, 0600, schema preserved.
    path, _ = _write_token(tmp_path, monkeypatch, expires_at=NOW + 60)
    calls = []

    def http(url, data, headers, **k):
        calls.append((url, dict(urllib.parse.parse_qsl(data.decode())), headers))
        return 200, json.dumps({"access_token": "tok-new", "refresh_token": "rt-2",
                                "expires_in": 3600}).encode()

    tok, url = ct.get_access_token("granola", http=http, _now=NOW)
    assert tok == "tok-new" and url == "https://mcp.granola.ai/mcp"
    endpoint, form, _headers = calls[0]
    assert endpoint == "https://mcp-auth.granola.ai/oauth/token"
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "rt-1"
    assert form["client_id"] == "dcr-client-1"
    assert "client_secret" not in form                      # OAuth 2.1 public client — no secret
    saved = json.loads(path.read_text())
    assert saved["access_token"] == "tok-new"
    assert saved["refresh_token"] == "rt-2"                 # rotation honored
    assert saved["expires_at"] == NOW + 3600
    # Full schema preserved (the receiver reads these back).
    for k in ("service", "token_endpoint", "client_id", "resource", "mcp_url", "obtained_at"):
        assert k in saved
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_refresh_without_rotation_keeps_old_refresh_token(tmp_path, monkeypatch):
    path, _ = _write_token(tmp_path, monkeypatch, expires_at=NOW - 5)   # already expired
    http = lambda *a, **k: (200, json.dumps({"access_token": "tok-new", "expires_in": 100}).encode())  # noqa: E731
    tok, _ = ct.get_access_token("granola", http=http, _now=NOW)
    assert tok == "tok-new"
    assert json.loads(path.read_text())["refresh_token"] == "rt-1"      # not clobbered to null


def test_refresh_rejected_raises_auth_error(tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch, expires_at=NOW + 30)
    http = lambda *a, **k: (400, b'{"error":"invalid_grant"}')  # noqa: E731
    try:
        ct.get_access_token("granola", http=http, _now=NOW)
        raise AssertionError("expected ConnectorAuthError")
    except ct.ConnectorAuthError as e:
        assert "reconnect" in str(e)


def test_expired_without_refresh_token_raises_auth_error(tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch, expires_at=NOW - 10, refresh_token=None)
    try:
        ct.get_access_token("granola", http=_no_http, _now=NOW)
        raise AssertionError("expected ConnectorAuthError")
    except ct.ConnectorAuthError:
        pass


def test_force_refresh_refreshes_even_when_fresh(tmp_path, monkeypatch):
    # The 401-retry path: the caller got 401 mid-session and asks for one forced refresh.
    _write_token(tmp_path, monkeypatch, expires_at=NOW + 3600)
    http = lambda *a, **k: (200, json.dumps({"access_token": "tok-forced", "expires_in": 60}).encode())  # noqa: E731
    tok, _ = ct.get_access_token("granola", http=http, force_refresh=True, _now=NOW)
    assert tok == "tok-forced"


def test_no_access_token_in_file_raises_auth_error(tmp_path, monkeypatch):
    _write_token(tmp_path, monkeypatch, access_token="")
    try:
        ct.get_access_token("granola", http=_no_http, _now=NOW)
        raise AssertionError("expected ConnectorAuthError")
    except ct.ConnectorAuthError:
        pass
