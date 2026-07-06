"""Reverse-MCP relay: initialize/tools-list answered locally, tool calls forwarded to the Bridge."""
import importlib.util
import os
import threading
import time

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("relay", os.path.join(HERE, "relay.py"))
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


def test_initialize_answered_locally_without_bridge():
    # The relay must mirror the REAL Bridge's identity (sotto-bridge/core/src/mcp.rs), so Hermes sees
    # one consistent server whether or not the Mac is connected.
    r = relay.Relay()
    resp = r.mcp_call({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp["result"]["serverInfo"]["name"] == "sotto-bridge"
    assert resp["result"]["protocolVersion"] == relay.PROTOCOL_VERSION == "2025-06-18"


def test_tools_list_offline_returns_fallback():
    r = relay.Relay()
    resp = r.mcp_call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "read_local" in names and "health" in names   # toolset visible even with the Mac asleep


def test_tools_call_offline_is_graceful_error_result():
    r = relay.Relay()
    resp = r.mcp_call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "read_local", "arguments": {}}})
    assert resp["result"]["isError"] is True
    assert "offline" in resp["result"]["content"][0]["text"].lower()


def test_health_answered_locally_when_offline_keeps_server_alive():
    # Hermes pings `health` to keep the MCP server alive. Offline it must return a SUCCESSFUL result
    # (not isError) so a sleeping Mac doesn't storm reconnects — but it must truthfully say connected:false.
    r = relay.Relay()
    resp = r.mcp_call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "health", "arguments": {}}})
    assert resp["result"]["isError"] is False
    assert resp["result"]["structuredContent"]["connected"] is False


def test_notification_returns_none():
    r = relay.Relay()
    assert r.mcp_call({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_full_forward_cycle_with_a_bridge():
    r = relay.Relay()

    # A fake Bridge: poll for the request, echo a result back.
    def bridge():
        req = r.poll(timeout=5)
        assert req is not None and req["method"] == "tools/call"
        r.respond({"jsonrpc": "2.0", "id": req["id"],
                   "result": {"content": [{"type": "text", "text": "pong"}]}})

    t = threading.Thread(target=bridge)
    t.start()
    # The Bridge polled (marks connected); now Hermes calls a tool.
    time.sleep(0.05)
    resp = r.mcp_call({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "health", "arguments": {}}}, timeout=5)
    t.join()
    assert resp["result"]["content"][0]["text"] == "pong"
    assert resp["id"] == 7


def test_tools_list_caches_after_a_connected_listing():
    r = relay.Relay()

    def bridge():
        req = r.poll(timeout=5)
        r.respond({"jsonrpc": "2.0", "id": req["id"],
                   "result": {"tools": [{"name": "read_local"}, {"name": "custom_tool"}]}})

    t = threading.Thread(target=bridge)
    t.start()
    time.sleep(0.05)
    live = r.mcp_call({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=5)
    t.join()
    assert any(x["name"] == "custom_tool" for x in live["result"]["tools"])
    # Now simulate the Mac going to sleep: cached list is still served.
    r._last_poll = 0  # force "disconnected"
    cached = r.mcp_call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert any(x["name"] == "custom_tool" for x in cached["result"]["tools"])


def test_bridge_connected_timing():
    r = relay.Relay(bridge_timeout=0.2)
    assert not r.bridge_connected()          # never polled
    r.poll(timeout=0.01)                       # a poll marks it alive
    assert r.bridge_connected()
    time.sleep(0.25)
    assert not r.bridge_connected()            # ages out


def test_http_routes_and_auth_through_the_receiver():
    """The receiver's HTTP wiring: /mcp routes JSON-RPC into the relay, bearer auth is enforced, and
    /health reports bridge connectivity. (The poll→respond forwarding itself is covered by the
    Relay-class tests above; this guards the route+auth layer without thread-timing flakiness.)"""
    import importlib.util as _il
    import json as _json
    import urllib.request as _u
    from http.server import ThreadingHTTPServer

    spec = _il.spec_from_file_location("receiver", os.path.join(HERE, "receiver.py"))
    rec = _il.module_from_spec(spec)
    spec.loader.exec_module(rec)
    rec.MCP_TOKEN = "tok"
    rec.TOKEN = "tok"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), rec.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # initialize is answered locally — no Bridge needed, so the toolset is always reachable.
        req = _u.Request(base + "/mcp", data=_json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode(),
                         headers={"Authorization": "Bearer tok", "Content-Type": "application/json"}, method="POST")
        with _u.urlopen(req, timeout=5) as r:
            body = _json.loads(r.read())
        assert body["result"]["serverInfo"]["name"] == "sotto-bridge"

        # /health reports relay/bridge state.
        with _u.urlopen(base + "/health", timeout=5) as r:
            assert _json.loads(r.read())["bridge_connected"] is False

        # No/!bad bearer on /mcp → 401.
        try:
            _u.urlopen(_u.Request(base + "/mcp", data=b"{}",
                       headers={"Content-Type": "application/json"}, method="POST"), timeout=5)
            assert False, "expected 401"
        except _u.HTTPError as e:
            assert e.code == 401
    finally:
        srv.shutdown()


def test_ping_is_answered_locally_while_bridge_offline():
    # Hermes' MCP keepalive. The relay is the server being probed — it must answer {} even with
    # the Mac asleep, or every probe logs "keepalive failed ... Sotto Bridge offline" (every ~3 min).
    r = relay.Relay()
    resp = r.mcp_call({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    assert resp == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_ping_is_answered_locally_even_when_bridge_connected():
    r = relay.Relay()
    r._touch()  # mark the Mac as connected
    resp = r.mcp_call({"jsonrpc": "2.0", "id": 8, "method": "ping"})
    assert resp == {"jsonrpc": "2.0", "id": 8, "result": {}}
