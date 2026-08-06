#!/usr/bin/env python3
"""
Reverse-MCP relay — the tunnel-free transport.

Instead of the cloud reaching IN to the Mac (Cloudflare tunnel → 530s, inbound exposure), the Mac
dials OUT to this relay (which runs beside the agent on the always-on host). The relay presents a
LOCALHOST-stable MCP server to Hermes (`sotto-local` points at it), and forwards tool calls down the
held-open outbound link to whichever Bridge is connected.

Flow:
  - Hermes → POST /mcp  (JSON-RPC)            → Relay.mcp_call(...)
  - Bridge → GET  /bridge/poll  (long-poll)   → Relay.poll(...)        delivers the next tool call
  - Bridge → POST /bridge/respond             → Relay.respond(...)     returns the result to Hermes

Key property: `initialize`, `ping`, and `tools/list` are answered by the relay itself (ping is the
MCP keepalive — the relay IS the server being probed, so it must answer even with the Mac away;
tools/list from a cache primed on the first Bridge connection), so Hermes ALWAYS sees a healthy MCP
server — even while the Mac is asleep. Only `tools/call` needs the Bridge; if it's offline, the call returns a clear error the
skills already handle ("Bridge offline"), and it reconnects automatically when the Mac comes back.
No tunnel, no domain, no inbound port exposing chat.db. Stdlib only.
"""
from __future__ import annotations

import json
import queue
import threading
import time

# Mirror the real Bridge's identity (sotto-bridge/core/src/mcp.rs: PROTOCOL_VERSION + server_info),
# so Hermes sees ONE consistent server whether initialize is answered here or the Mac is connected.
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "sotto-bridge", "version": "1.0"}
# Minimal fallback tool list used only before the Bridge has ever connected (no cache yet).
_FALLBACK_TOOLS = [
    {"name": "read_local", "description": "Local Mac context (messages/calls/contacts/…).",
     "inputSchema": {"type": "object", "properties": {"since_hours": {"type": "number"},
                     "sources": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "get_messages", "description": "A message thread for one contact.",
     "inputSchema": {"type": "object", "properties": {"identifier": {"type": "string"},
                     "limit": {"type": "number"}}, "required": ["identifier"]}},
    {"name": "get_contacts", "description": "Address book contacts.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "health", "description": "Bridge capability + liveness probe.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


class Relay:
    """Thread-safe reverse-RPC bridge between Hermes (/mcp) and the Bridge (poll/respond)."""

    def __init__(self, bridge_timeout: float = 40.0):
        self._q: queue.Queue = queue.Queue()      # tool-call requests awaiting the Bridge's poll
        self._waiters: dict = {}                    # request id -> {"event", "value"}
        self._lock = threading.Lock()
        self._last_poll = 0.0                       # monotonic time of the last Bridge poll/respond
        self._bridge_timeout = bridge_timeout
        self._tools_cache = None                    # last good tools/list result (primed on connect)

    # ---- status -----------------------------------------------------------
    def bridge_connected(self) -> bool:
        return (time.monotonic() - self._last_poll) < self._bridge_timeout

    def _touch(self):
        self._last_poll = time.monotonic()

    # ---- Hermes side (/mcp) ----------------------------------------------
    def mcp_call(self, req: dict, timeout: float = 90.0):
        """Handle one JSON-RPC message from Hermes. Returns a response dict, or None for a
        notification (no reply expected)."""
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            return _ok(rid, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                             "serverInfo": dict(SERVER_INFO)})
        if method in ("notifications/initialized", "notifications/cancelled") or rid is None:
            return None
        if method == "ping":
            # The MCP keepalive. Hermes probes the server every few minutes; the relay IS that
            # server and it's alive, so answer locally (the real Bridge does the same in mcp.rs).
            # Forwarding — or the old fall-through to the offline error — made every probe while
            # the Mac slept log "keepalive failed, triggering reconnect: Sotto Bridge offline"
            # every ~3 minutes, all night. Offline-ness belongs in tool RESULTS (health), not here.
            return _ok(rid, {})
        if method == "tools/list":
            # Answer from cache so the toolset stays visible even when the Mac is asleep.
            if self.bridge_connected():
                resp = self._forward(req, timeout=20.0)
                if resp and "result" in resp:
                    self._tools_cache = resp["result"]
                    return resp
            return _ok(rid, self._tools_cache or {"tools": _FALLBACK_TOOLS})
        if method == "tools/call":
            if not self.bridge_connected():
                name = (req.get("params") or {}).get("name")
                if name == "health":
                    # Answer the keepalive `health` probe LOCALLY when the Mac is offline. Hermes pings
                    # health to keep the MCP server alive; forwarding it to an absent Bridge fails and
                    # storms reconnects every night the Mac sleeps. Returning a SUCCESSFUL result that
                    # truthfully says connected:false keeps the server healthy AND lets the agent report
                    # the real state. (Same spirit as answering initialize/tools-list from cache.)
                    payload = {"connected": False, "link": "relay", "fda": "unknown",
                               "note": "Sotto Bridge offline (Mac asleep or app not running)."}
                    return _ok(rid, {"content": [{"type": "text", "text": json.dumps(payload)}],
                                     "structuredContent": payload, "isError": False})
                # A tool RESULT carrying an error message (not a transport error) so the skill can
                # say "Bridge offline" gracefully rather than the whole MCP looking broken.
                return _ok(rid, {"content": [{"type": "text",
                            "text": "Sotto Bridge offline — your Mac isn't connected right now."}],
                            "isError": True})
            return self._forward(req, timeout) or _err(rid, -32001, "Bridge timed out")
        # Anything else → forward if possible.
        if self.bridge_connected():
            return self._forward(req, timeout) or _err(rid, -32001, "Bridge timed out")
        return _err(rid, -32000, "Sotto Bridge offline")

    def _forward(self, req: dict, timeout: float):
        rid = req.get("id")
        ev = threading.Event()
        with self._lock:
            self._waiters[rid] = {"event": ev, "value": None}
        self._q.put(req)
        if ev.wait(timeout):
            with self._lock:
                slot = self._waiters.pop(rid, None)
            return slot["value"] if slot else None
        with self._lock:
            self._waiters.pop(rid, None)
        return None

    # ---- Bridge side (poll / respond) ------------------------------------
    def poll(self, timeout: float = 25.0):
        """Long-poll: block up to `timeout` for the next request to hand the Bridge. Returns the
        request dict, or None (→ the Bridge re-polls). Marks the Bridge alive."""
        self._touch()
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def respond(self, resp: dict):
        """Deliver the Bridge's result for a request id back to the waiting Hermes call."""
        self._touch()
        rid = resp.get("id")
        with self._lock:
            slot = self._waiters.get(rid)
            if slot:
                slot["value"] = resp
                slot["event"].set()
