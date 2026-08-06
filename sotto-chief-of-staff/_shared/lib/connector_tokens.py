#!/usr/bin/env python3
"""
connector_tokens.py — read (and refresh) the per-service OAuth tokens the receiver's generic
MCP connect surface stores on the volume.

CONTRACT (shared with the receiver, which WRITES these files on OAuth):
    $SOTTO_DATA/connectors/<service>.json
    { "service", "access_token", "refresh_token" (or null), "expires_at" (epoch seconds or null),
      "token_endpoint", "client_id", "resource", "mcp_url", "obtained_at" }

This module is the READ side used by deterministic gathers (gather_granola.py first): load the
file, refresh centrally when the token is expiring (OAuth 2.1 public-client refresh — DCR'd
client_id, PKCE flow, NO client secret), and hand back (access_token, mcp_url). The receiver
owns the initial OAuth dance; scripts never do interactive auth.

Refresh is written back atomically (tmp + os.replace, 0600) preserving the schema, rotating
refresh_token when the token endpoint returns a new one (OAuth 2.1 servers may one-time-use them).

Errors:
    ConnectorMissing   — no token file for the service (never connected). Gathers fail-empty.
    ConnectorAuthError — token expired/revoked and unrefreshable (or the refresh was rejected).
                         The user must reconnect on /setup.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REFRESH_WINDOW_SECS = 120   # refresh when the token expires within this window


class ConnectorError(Exception):
    pass


class ConnectorMissing(ConnectorError):
    """No token file — the service was never connected on /setup."""


class ConnectorAuthError(ConnectorError):
    """Token expired/revoked and could not be refreshed — reconnect on /setup."""


def connectors_dir() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "connectors")


def token_path(service: str) -> str:
    return os.path.join(connectors_dir(), f"{service}.json")


def _default_http(url: str, data: bytes, headers: dict, timeout: int = 30):
    """POST `data` to `url`; returns (status_code, body_bytes). Injectable for tests."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _load(service: str) -> dict:
    path = token_path(service)
    if not os.path.exists(path):
        raise ConnectorMissing(f"no connector token for '{service}' ({path}) — connect it on /setup")
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError) as e:
        raise ConnectorAuthError(f"connector token for '{service}' unreadable: {e}") from e
    if not isinstance(rec, dict) or not rec.get("access_token"):
        raise ConnectorAuthError(f"connector token for '{service}' has no access_token — reconnect on /setup")
    return rec


def _write_atomic(service: str, rec: dict) -> None:
    """Atomic replace + 0600 — the receiver may read/rewrite this file concurrently."""
    path = token_path(service)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=f".{service}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _needs_refresh(rec: dict, now: float) -> bool:
    exp = rec.get("expires_at")
    return isinstance(exp, (int, float)) and exp - now <= REFRESH_WINDOW_SECS


def _refresh(service: str, rec: dict, http, now: float) -> dict:
    """OAuth 2.1 public-client refresh: POST grant_type=refresh_token with the DCR'd client_id,
    NO client secret. Updates the token file atomically; rotates refresh_token if one is returned."""
    endpoint = rec.get("token_endpoint")
    if not endpoint:
        raise ConnectorAuthError(f"'{service}' token expiring and no token_endpoint to refresh at — reconnect on /setup")
    form = {"grant_type": "refresh_token",
            "refresh_token": rec["refresh_token"],
            "client_id": rec.get("client_id") or ""}
    if rec.get("resource"):
        form["resource"] = rec["resource"]   # RFC 8707 — MCP auth spec binds tokens to the resource
    status, body = http(endpoint, urllib.parse.urlencode(form).encode(),
                        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    if status != 200:
        raise ConnectorAuthError(
            f"'{service}' token refresh rejected (HTTP {status}) — reconnect on /setup: {body[:200]!r}")
    try:
        tok = json.loads(body)
    except ValueError as e:
        raise ConnectorAuthError(f"'{service}' token endpoint returned non-JSON: {e}") from e
    if not tok.get("access_token"):
        raise ConnectorAuthError(f"'{service}' token refresh response had no access_token")

    updated = dict(rec)   # preserve the full schema (service, client_id, mcp_url, resource, …)
    updated["access_token"] = tok["access_token"]
    if tok.get("refresh_token"):                    # rotation: some servers one-time-use refresh tokens
        updated["refresh_token"] = tok["refresh_token"]
    expires_in = tok.get("expires_in")
    updated["expires_at"] = (now + float(expires_in)) if isinstance(expires_in, (int, float)) else None
    updated["obtained_at"] = now
    _write_atomic(service, updated)
    return updated


def get_access_token(service: str, http=None, force_refresh: bool = False, _now=None):
    """Return (access_token, mcp_url) for `service`, refreshing first if the token expires within
    ~2 minutes (or `force_refresh` — the 401-retry path: a caller that got 401 mid-session refreshes
    once and retries). `http(url, data, headers) -> (status, body_bytes)` is injectable for tests.

    Raises ConnectorMissing (never connected) or ConnectorAuthError (needs reconnect)."""
    http = http or _default_http
    now = _now if _now is not None else time.time()
    rec = _load(service)
    if force_refresh or _needs_refresh(rec, now):
        if rec.get("refresh_token"):
            rec = _refresh(service, rec, http, now)
        elif force_refresh or (isinstance(rec.get("expires_at"), (int, float)) and rec["expires_at"] <= now):
            # Actually expired (or the caller just got a 401) and nothing to refresh with.
            raise ConnectorAuthError(f"'{service}' access token expired and no refresh_token — reconnect on /setup")
        # else: inside the refresh window but not yet expired, with no refresh_token —
        # return it as-is and let the caller's 401 handling surface the reconnect.
    return rec["access_token"], rec.get("mcp_url") or ""
