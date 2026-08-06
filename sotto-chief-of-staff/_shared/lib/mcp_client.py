#!/usr/bin/env python3
"""
mcp_client.py — a minimal Streamable-HTTP MCP client for DETERMINISTIC gathers.

WHY: pipelines must never depend on agent-driven fetching (the #1 historical failure mode —
"use the Granola MCP" steps got skipped and briefs went thin). This client is just enough of
the MCP spec (2025-03-26 Streamable HTTP transport) for a script to initialize a session, list
tools, and call one: no SDK dependency, stdlib only, injectable transport for tests.

Protocol notes (the parts we implement):
- Every request is an HTTP POST of a single JSON-RPC message to the server URL, with
  `Accept: application/json, text/event-stream` — the server may answer either way:
    * plain `application/json` body containing the JSON-RPC response, or
    * a `text/event-stream` body: SSE events whose `data:` lines each carry a JSON-RPC message;
      we read until the response whose `id` matches ours (the parser is deliberately tiny).
- `initialize` → capture the `Mcp-Session-Id` response header and echo it on every later call;
  then fire the `notifications/initialized` notification (no id — no response expected).
- 401/403 → ConnectorAuthError, so the caller can refresh the token once and retry.

Tool results come back as {content:[{type:"text", text:"…"}…]}; call_tool() returns the first
text item json-decoded when possible (mirroring the Mac's granola-mcp.ts), else the raw text.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from connector_tokens import ConnectorAuthError  # noqa: E402

PROTOCOL_VERSION = "2025-03-26"


class McpError(Exception):
    pass


def _default_transport(url: str, body: bytes, headers: dict, timeout: int):
    """POST one JSON-RPC message. Returns (status, response_headers_dict, body_bytes).
    Injectable for tests (and for any host that needs a proxy-aware transport)."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _sse_extract(body_text: str, want_id):
    """Tiny SSE parser: walk the stream's events, JSON-decode each event's `data:` payload, and
    return the JSON-RPC message whose id matches `want_id` (server may interleave notifications)."""
    data_lines = []
    for raw in body_text.splitlines() + [""]:          # trailing "" flushes the final event
        line = raw.rstrip("\r")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "" and data_lines:                # blank line = event boundary
            payload = "\n".join(data_lines)
            data_lines = []
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
    return None


class McpClient:
    def __init__(self, url: str, bearer: str, transport=None, client_name: str = "sotto"):
        self.url = url
        self.bearer = bearer
        self.transport = transport or _default_transport
        self.client_name = client_name
        self.session_id = None
        self._next_id = 0

    # -- plumbing ---------------------------------------------------------

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "Authorization": f"Bearer {self.bearer}",
             "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, message: dict, timeout: int):
        status, headers, body = self.transport(self.url, json.dumps(message).encode(),
                                               self._headers(), timeout)
        if status in (401, 403):
            raise ConnectorAuthError(f"MCP server rejected the token (HTTP {status})")
        if status >= 400:
            raise McpError(f"MCP HTTP {status}: {body[:300]!r}")
        # Session id may be (re)issued on any response; capture case-insensitively.
        for k, v in (headers or {}).items():
            if str(k).lower() == "mcp-session-id" and v:
                self.session_id = v
        return headers or {}, body or b""

    def _request(self, method: str, params: dict | None, timeout: int = 60) -> dict:
        self._next_id += 1
        rid = self._next_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        headers, body = self._post(msg, timeout)
        ctype = ""
        for k, v in headers.items():
            if str(k).lower() == "content-type":
                ctype = str(v)
        text = body.decode("utf-8", "replace")
        if "text/event-stream" in ctype or (not ctype and text.lstrip().startswith(("event:", "data:"))):
            resp = _sse_extract(text, rid)
            if resp is None:
                raise McpError(f"MCP SSE stream ended without a response for id {rid} ({method})")
        else:
            try:
                resp = json.loads(text)
            except ValueError as e:
                raise McpError(f"MCP returned non-JSON for {method}: {e}") from e
        if isinstance(resp, dict) and resp.get("error"):
            err = resp["error"]
            raise McpError(f"MCP {method} error {err.get('code')}: {err.get('message')}")
        return resp.get("result") if isinstance(resp, dict) else {}

    def _notify(self, method: str, timeout: int = 30) -> None:
        """Fire a JSON-RPC notification (no id → no response; servers answer 202/empty)."""
        self._post({"jsonrpc": "2.0", "method": method}, timeout)

    # -- the three calls gathers need -------------------------------------

    def initialize(self) -> dict:
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": self.client_name, "version": "1.0.0"},
        })
        self._notify("notifications/initialized")
        return result or {}

    def list_tools(self) -> list:
        result = self._request("tools/list", {}) or {}
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict | None = None, timeout: int = 60):
        """Call a tool; return its first text content item JSON-decoded when possible (else the
        raw text, else the raw content list) — the same extraction the Mac's granola-mcp.ts does."""
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}},
                               timeout=timeout) or {}
        if result.get("isError"):
            raise McpError(f"tool {name} returned an error: {json.dumps(result.get('content'))[:300]}")
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                    try:
                        return json.loads(item["text"])
                    except ValueError:
                        return item["text"]
        return content
