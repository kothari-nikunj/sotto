"""Reverse-MCP relay: initialize/tools-list answered locally, tool calls forwarded to the Bridge."""
import importlib.util
import json
import os
import threading
import time

import pytest
from datetime import datetime

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
    assert resp["result"]["structuredContent"] == {"error_code": "bridge_offline"}
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


def test_queued_send_is_refused_if_permission_changes_before_poll():
    r = relay.Relay()
    r._touch()
    allowed = [True]
    r.validate_request = lambda request: allowed[0]
    replies = []
    thread = threading.Thread(target=lambda: replies.append(r.mcp_call({
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'send_message', 'arguments': {}}}, timeout=2)))
    thread.start()
    deadline = time.monotonic() + 1
    while r._q.empty() and time.monotonic() < deadline:
        time.sleep(.001)
    assert not r._q.empty()
    allowed[0] = False
    assert r.poll(timeout=.05) is None
    thread.join(timeout=1)
    assert replies[0]['error']['code'] == -32002
    assert not thread.is_alive()


def test_revoked_poll_cancels_call_without_requeue_or_timeout():
    r = relay.Relay()
    r._touch()
    replies = []
    thread = threading.Thread(target=lambda: replies.append(r.mcp_call({
        'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
        'params': {'name': 'send_message', 'arguments': {}}}, timeout=2)))
    thread.start()
    request = r.poll(timeout=1)
    assert request is not None
    r.cancel(request, 'Bridge connection was revoked before execution.')
    r.cancel(None, 'empty poll')
    thread.join(timeout=1)
    assert replies[0]['id'] == 9 and replies[0]['error']['code'] == -32002
    assert r._q.empty()


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


def test_source_response_uses_server_request_start_and_returns_it_in_both_payload_lanes(monkeypatch):
    receiver_spec = importlib.util.spec_from_file_location(
        "receiver_request_marker", os.path.join(HERE, "receiver.py"))
    receiver = importlib.util.module_from_spec(receiver_spec)
    receiver_spec.loader.exec_module(receiver)
    recorded = []

    class SourceContext:
        @staticmethod
        def record_bridge_status(payload, data_root=None):
            recorded.append((dict(payload), data_root))

    monkeypatch.setattr(receiver, '_source_context', lambda: SourceContext)
    r = relay.Relay()
    r.on_response = receiver._record_source_response
    validation_markers = []
    r.validate_response = lambda request, result: (
        validation_markers.append((request[relay.BRIDGE_REQUEST_STARTED_AT],
                                   result['structuredContent'][relay.BRIDGE_REQUEST_STARTED_AT]))
        or True)

    caller_marker = '1900-01-01T00:00:00+00:00'
    bridge_marker = '2999-01-01T00:00:00+00:00'
    bridge_request = []

    def bridge():
        req = r.poll(timeout=5)
        bridge_request.append(req)
        payload = {'generated_at': bridge_marker, 'source_status': {'chrome': 'ok'},
                   relay.BRIDGE_REQUEST_STARTED_AT: bridge_marker}
        r.respond({'jsonrpc': '2.0', 'id': req['id'], 'result': {
            'structuredContent': dict(payload),
            'content': [{'type': 'text', 'text': json.dumps(payload)}],
            'isError': False,
        }})

    thread = threading.Thread(target=bridge)
    thread.start()
    time.sleep(.05)
    response = r.mcp_call({
        'jsonrpc': '2.0', 'id': 17, 'method': 'tools/call',
        'params': {'name': 'read_local', 'arguments': {}},
        relay.BRIDGE_REQUEST_STARTED_AT: caller_marker,
    }, timeout=5)
    thread.join()

    assert relay.BRIDGE_REQUEST_STARTED_AT not in bridge_request[0]
    payload = response['result']['structuredContent']
    returned_text_payload = json.loads(response['result']['content'][0]['text'])
    marker = payload[relay.BRIDGE_REQUEST_STARTED_AT]
    assert marker not in (caller_marker, bridge_marker)
    assert datetime.fromisoformat(marker).tzinfo is not None
    assert returned_text_payload == payload == recorded[0][0]
    assert receiver._load_shared_lib('textutil').unwrap_tool_result(response['result']) == payload
    assert validation_markers == [(marker, bridge_marker)]  # validation precedes annotation


def test_source_response_removes_untrusted_marker_without_pending_request_marker(monkeypatch):
    receiver_spec = importlib.util.spec_from_file_location(
        "receiver_untrusted_marker", os.path.join(HERE, "receiver.py"))
    receiver = importlib.util.module_from_spec(receiver_spec)
    receiver_spec.loader.exec_module(receiver)
    recorded = []

    class SourceContext:
        @staticmethod
        def record_bridge_status(payload, data_root=None):
            recorded.append(dict(payload))

    monkeypatch.setattr(receiver, '_source_context', lambda: SourceContext)
    payload = {'sources': {'chrome': 'ok'},
               relay.BRIDGE_REQUEST_STARTED_AT: '2999-01-01T00:00:00+00:00'}
    result = {'structuredContent': dict(payload),
              'content': [{'type': 'text', 'text': json.dumps(payload)}],
              'isError': False}
    receiver._record_source_response(
        {'method': 'tools/call', 'params': {'name': 'health'}}, result)

    assert relay.BRIDGE_REQUEST_STARTED_AT not in result['structuredContent']
    assert relay.BRIDGE_REQUEST_STARTED_AT not in json.loads(result['content'][0]['text'])
    assert recorded == [{'sources': {'chrome': 'ok'}}]


def test_source_response_wraps_raw_payload_without_self_reference(monkeypatch):
    receiver_spec = importlib.util.spec_from_file_location(
        "receiver_raw_payload", os.path.join(HERE, "receiver.py"))
    receiver = importlib.util.module_from_spec(receiver_spec)
    receiver_spec.loader.exec_module(receiver)
    recorded = []

    class SourceContext:
        @staticmethod
        def record_bridge_status(payload, data_root=None):
            recorded.append(dict(payload))

    monkeypatch.setattr(receiver, '_source_context', lambda: SourceContext)
    request = {'method': 'tools/call', 'params': {'name': 'health'},
               relay.BRIDGE_REQUEST_STARTED_AT: '2026-09-22T12:00:00+00:00'}
    result = {'sources': {'chrome': 'ok'}}
    receiver._record_source_response(request, result)

    assert result['structuredContent'] is not result
    assert json.loads(json.dumps(result))['structuredContent'] == recorded[0]
    assert json.loads(result['content'][0]['text']) == recorded[0]


def test_source_response_leaves_empty_and_malformed_results_untouched(monkeypatch):
    receiver_spec = importlib.util.spec_from_file_location(
        "receiver_malformed_payload", os.path.join(HERE, "receiver.py"))
    receiver = importlib.util.module_from_spec(receiver_spec)
    receiver_spec.loader.exec_module(receiver)

    class SourceContext:
        @staticmethod
        def record_bridge_status(_payload, data_root=None):
            raise AssertionError('malformed payload must not be recorded')

    monkeypatch.setattr(receiver, '_source_context', lambda: SourceContext)
    request = {'method': 'tools/call', 'params': {'name': 'read_local'},
               relay.BRIDGE_REQUEST_STARTED_AT: '2026-09-22T12:00:00+00:00'}
    results = [
        {},
        {'structuredContent': {}},
        {'structuredContent': {'source_status': []}, 'isError': False},
        {'content': [{'type': 'text', 'text': 'not json'}], 'isError': False},
    ]
    originals = [json.loads(json.dumps(result)) for result in results]

    for result in results:
        receiver._record_source_response(request, result)

    assert results == originals


def test_response_hook_failure_does_not_discard_authenticated_bridge_result():
    r = relay.Relay()
    r.on_response = lambda _request, _result: (_ for _ in ()).throw(RuntimeError('observer down'))

    def bridge():
        req = r.poll(timeout=5)
        r.respond({"jsonrpc": "2.0", "id": req["id"], "result": {"answer": 42}})

    thread = threading.Thread(target=bridge)
    thread.start()
    time.sleep(0.05)
    response = r.mcp_call({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                           "params": {"name": "read_local"}}, timeout=5)
    thread.join()
    assert response == {"jsonrpc": "2.0", "id": 8, "result": {"answer": 42}}


def test_forward_uses_unique_internal_ids_and_restores_the_external_id():
    r = relay.Relay()
    got = []

    def bridge():
        first = r.poll(timeout=5)
        second = r.poll(timeout=5)
        got.extend([first["id"], second["id"]])
        r.respond({"jsonrpc": "2.0", "id": first["id"], "result": {"n": 1}})
        r.respond({"jsonrpc": "2.0", "id": second["id"], "result": {"n": 2}})

    r._touch()
    bt = threading.Thread(target=bridge)
    bt.start()
    results = []
    calls = [threading.Thread(target=lambda n=n: results.append(r.mcp_call(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": f"t{n}"}},
        timeout=5))) for n in (1, 2)]
    for t in calls:
        t.start()
    for t in calls:
        t.join()
    bt.join()
    assert len(set(got)) == 2 and all(len(v) == 32 for v in got)
    assert [v["id"] for v in results] == [7, 7]
    assert {v["result"]["n"] for v in results} == {1, 2}


def test_forwarded_requests_advertise_accepted_fields_without_touching_the_callers_request():
    r = relay.Relay()
    seen = []

    def bridge():
        for _ in range(2):
            req = r.poll(timeout=5)
            seen.append(req)
            r.respond({"jsonrpc": "2.0", "id": req["id"], "result": {}})

    r._touch()
    bt = threading.Thread(target=bridge)
    bt.start()
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "read_local", "arguments": {"since_hours": 24},
                       "_meta": {"progressToken": "p", relay.BRIDGE_ACCEPTS_META: ["forged"]}}}
    original = json.loads(json.dumps(call))
    r.mcp_call(call, timeout=5)
    r.mcp_call({"jsonrpc": "2.0", "id": 2, "method": "resources/list"}, timeout=5)
    bt.join()
    assert call == original
    assert seen[0]["params"]["_meta"] == {"progressToken": "p", relay.BRIDGE_ACCEPTS_META: ["source_coverage", "source_coverage_v2", "search_metadata_v1"]}
    assert seen[0]["params"]["arguments"] == {"since_hours": 24}
    assert seen[1]["params"] == {"_meta": {relay.BRIDGE_ACCEPTS_META: ["source_coverage", "source_coverage_v2", "search_metadata_v1"]}}


def test_expired_forward_is_never_delivered_after_reconnect():
    r = relay.Relay()
    r._touch()
    assert r._forward({"jsonrpc": "2.0", "id": 9, "method": "tools/call"}, timeout=0.01) is None
    assert r.poll(timeout=0.02) is None


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
    r._last_poll = float("-inf")  # force "disconnected"
    cached = r.mcp_call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert any(x["name"] == "custom_tool" for x in cached["result"]["tools"])


def test_fresh_relay_is_disconnected_even_right_after_host_boot(monkeypatch):
    # time.monotonic() is host uptime on Linux — with _last_poll initialized to 0.0, a receiver
    # started within bridge_timeout seconds of boot reported "Mac linked ✓" before the Bridge ever
    # polled. float("-inf") makes a never-polled relay unconditionally disconnected.
    r = relay.Relay(bridge_timeout=40.0)
    monkeypatch.setattr(relay.time, "monotonic", lambda: 5.0)   # host booted 5s ago
    assert not r.bridge_connected()


def test_bridge_connected_timing():
    r = relay.Relay(bridge_timeout=0.2)
    assert not r.bridge_connected()          # never polled
    r.poll(timeout=0.01)                       # a poll marks it alive
    assert r.bridge_connected()
    time.sleep(0.25)
    assert not r.bridge_connected()            # ages out


def test_http_routes_and_auth_through_the_receiver(tmp_path):
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
    rec.DATA = str(tmp_path)
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


def test_response_validator_uses_pending_request_and_withholds_denied_result():
    r = relay.Relay()
    r._touch()
    allowed = []
    r.validate_response = lambda request, result: allowed.append((request, result)) or False
    result = []
    t = threading.Thread(target=lambda: result.append(r._forward({
        'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
        'params': {'name': 'read_history', 'arguments': {'source': 'imessage'}}}, timeout=.2)))
    t.start()
    req = r.poll(timeout=.1)
    assert r.respond({'id': req['id'], 'result': {'private': 'content'}}) is False
    t.join()
    assert result == [None] and allowed[0][0]['params']['arguments']['source'] == 'imessage'
    assert allowed[0][1] == {'private': 'content'}


def test_response_hooks_run_outside_the_relay_lock():
    """validate_response reads capability state and on_response writes a ledger under a file lock;
    neither may hold `_lock`, which every forward, poll hand-off and unrelated respond waits on."""
    r = relay.Relay()
    r._touch()
    held_during = []

    def hook(_request, _result):
        free = r._lock.acquire(timeout=0)
        if free:
            r._lock.release()
        held_during.append(not free)
        return True

    r.validate_response = hook
    r.on_response = hook
    result = []
    t = threading.Thread(target=lambda: result.append(r._forward({
        'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
        'params': {'name': 'read_local', 'arguments': {}}}, timeout=2)))
    t.start()
    req = r.poll(timeout=1)
    assert r.respond({'id': req['id'], 'result': {'ok': True}}) is True
    t.join()
    assert held_during == [False, False] and result[0]['result'] == {'ok': True}


def test_connected_tools_list_uses_primed_cache_without_waiting_for_mac(monkeypatch):
    r = relay.Relay()
    r._touch()
    catalog = {'tools': [{'name': 'read_local'}, {'name': 'get_contacts'}]}
    r._store_tools(catalog)
    forwarded = []
    monkeypatch.setattr(r, '_forward',
                        lambda request, timeout: forwarded.append((request['method'], timeout)))
    allowed = {'read_local', 'get_contacts'}
    r.filter_tools = lambda value: {**value, 'tools': [tool for tool in value['tools']
                                                     if tool['name'] in allowed]}

    first = r.mcp_call({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    allowed.remove('get_contacts')
    second = r.mcp_call({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})

    assert forwarded == [], 'warm discovery must not wait through the live 20-second forward'
    assert first['result']['tools'] == catalog['tools']
    assert second['result']['tools'] == [{'name': 'read_local'}]
    assert r._tools_cache == catalog, 'cache schemas, never a consent-filtered grant'
    # A cached advertised schema cannot authorize a later revoked tool call.
    r.validate_request = lambda request: False
    denied = r.mcp_call({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                         'params': {'name': 'get_contacts', 'arguments': {}}})
    assert denied['error']['code'] == -32002
    assert forwarded == []


def test_connected_empty_catalog_is_cached_without_fallback_or_probe(monkeypatch):
    r = relay.Relay()
    r._touch()
    r._store_tools({'tools': []})
    forwarded = []
    monkeypatch.setattr(r, '_forward',
                        lambda request, timeout: forwarded.append((request['method'], timeout)))
    result = r.mcp_call({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    assert result['result'] == {'tools': []}
    assert forwarded == []


def _join_refresh(r):
    if r._tools_refresh is not None:
        r._tools_refresh.join(5)


def test_bridge_reconnect_relists_in_background_and_keeps_the_last_catalog(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(relay.time, 'monotonic', lambda: clock[0])
    r = relay.Relay(bridge_timeout=40.0)
    r.poll(timeout=0.001)
    old = {'tools': [{'name': 'old_schema'}]}
    r._store_tools(old)
    clock[0] = 11.0
    r.poll(timeout=0.001)
    assert r._tools_listed_at == 10.0, 'an active poll is not a reconnect'

    clock[0] = 51.0
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
    assert r.mcp_call(request)['result'] == old, 'sleeping Mac retains its last catalog'
    assert r._tools_refresh is None, 'an offline Mac is never asked to re-list'
    r.poll(timeout=0.001)
    assert r._tools_cache == old, 'a reconnect keeps the last schemas'
    new = {'tools': [{'name': 'upgraded_schema'}]}
    forwarded = []

    def forward(request, timeout):
        forwarded.append((request['method'], timeout))
        return {'result': new}

    monkeypatch.setattr(r, '_forward', forward)
    assert r.mcp_call(request)['result'] == old, 'the stale catalog answers without waiting'
    _join_refresh(r)
    assert r.mcp_call({**request, 'id': 2})['result'] == new
    assert r.mcp_call({**request, 'id': 3})['result'] == new
    assert forwarded == [('tools/list', 20.0)], 'one background re-list per stale catalog'


def test_a_long_tool_response_is_not_a_reconnect(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(relay.time, 'monotonic', lambda: clock[0])
    r = relay.Relay(bridge_timeout=40.0)
    r.poll(timeout=0.001)
    r._store_tools({'tools': [{'name': 'read_history'}]})
    clock[0] = 55.0  # a 45-second tools/call returning through respond()
    r._touch(polling=False)
    assert r._tools_listed_at == 10.0 and r._tools_cache == {'tools': [{'name': 'read_history'}]}


def test_failed_background_relist_keeps_the_last_catalog_not_the_fallback(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(relay.time, 'monotonic', lambda: clock[0])
    r = relay.Relay(bridge_timeout=40.0)
    r.poll(timeout=0.001)
    last = {'tools': [{'name': 'read_history'}, {'name': 'send_message'}]}
    r._store_tools(last)
    clock[0] = 60.0
    r.poll(timeout=0.001)
    monkeypatch.setattr(r, '_forward', lambda request, timeout: None)
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
    assert r.mcp_call(request)['result'] == last
    _join_refresh(r)
    assert r.mcp_call({**request, 'id': 2})['result'] == last


def test_connected_catalog_relists_after_the_relist_interval_once_at_a_time(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(relay.time, 'monotonic', lambda: clock[0])
    r = relay.Relay(bridge_timeout=40.0)
    r.poll(timeout=0.001)
    r._store_tools({'tools': [{'name': 'read_local'}]})
    release = threading.Event()
    forwarded = []

    def forward(request, timeout):
        forwarded.append(request['method'])
        release.wait(5)
        return {'result': {'tools': [{'name': 'read_local'}, {'name': 'send_message'}]}}

    monkeypatch.setattr(r, '_forward', forward)
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
    while clock[0] + 30 < 10.0 + relay.TOOLS_RELIST_SECONDS:  # a live Bridge polls continuously
        clock[0] += 30
        r._touch()
    r.mcp_call(request)
    assert r._tools_refresh is None, 'a fresh catalog is not re-listed'
    clock[0] = 10.0 + relay.TOOLS_RELIST_SECONDS
    r._touch()
    for index in range(3):
        assert r.mcp_call({**request, 'id': index})['result'] == {'tools': [{'name': 'read_local'}]}
    release.set()
    _join_refresh(r)
    assert forwarded == ['tools/list'], 'at most one background re-list is in flight'
    assert r.mcp_call(request)['result']['tools'][-1] == {'name': 'send_message'}


def test_lost_ack_retry_is_content_free_bounded_and_observed_once(monkeypatch):
    r = relay.Relay()
    seen = []
    r.on_response = lambda request, result: seen.append(result.copy())
    replies = []
    t = threading.Thread(target=lambda: replies.append(r._forward({'id': 9}, timeout=2)))
    t.start()
    req = r.poll(timeout=1)
    response = {'id': req['id'], 'result': {'private': 'unique-secret'}}
    assert r.respond(response)
    t.join(timeout=1)
    assert len(replies) == 1
    # Receiver accepted, but the HTTP acknowledgement was lost. Retry after waiter removal.
    assert r.respond(response)
    assert len(seen) == 1
    assert 'unique-secret' not in repr(r._completed)
    assert not r.respond(dict(response, result={'private': 'different'}))
    restarted = relay.Relay()
    assert not restarted.respond(response)  # no waiter/receipt after restart, never execute a tool
    expiry = r._completed[req['id']][1]
    monkeypatch.setattr(relay.time, 'monotonic', lambda: expiry + 1)
    assert not r.respond(response)
    assert not r._completed


def test_concurrent_response_retries_do_not_repeat_slow_observation():
    r = relay.Relay()
    entered, release = threading.Event(), threading.Event()
    seen = []
    def observe(request, result):
        seen.append(result)
        entered.set()
        assert release.wait(2)
    r.on_response = observe
    caller = threading.Thread(target=lambda: r._forward({'id': 1}, timeout=3))
    caller.start()
    req = r.poll(timeout=1)
    response = {'id': req['id'], 'result': {'ok': True}}
    accepted = []
    first = threading.Thread(target=lambda: accepted.append(r.respond(response)))
    second = threading.Thread(target=lambda: accepted.append(r.respond(response)))
    first.start()
    assert entered.wait(1)
    second.start()
    release.set()
    first.join(2); second.join(2); caller.join(2)
    assert accepted == [True, True] and len(seen) == 1


def test_expired_result_is_not_observed_and_receipt_cache_has_a_hard_cap():
    r = relay.Relay()
    r.on_response = lambda *_: (_ for _ in ()).throw(AssertionError('expired observation'))
    caller = threading.Thread(target=lambda: r._forward({'id': 1}, timeout=.1))
    caller.start()
    req = r.poll(timeout=1)
    with r._lock:
        r._waiters[req['id']]['deadline'] = 0
    assert not r.respond({'id': req['id'], 'result': {}})
    caller.join(1)
    r.on_response = lambda *_: None
    for n in range(256):
        r._completed[str(n)] = ('hash', time.monotonic()+120)
    caller = threading.Thread(target=lambda: r._forward({'id': 2}, timeout=2))
    caller.start()
    req = r.poll(timeout=1)
    assert r.respond({'id': req['id'], 'result': {}})
    caller.join(1)
    assert len(r._completed) == 256 and '0' not in r._completed


@pytest.mark.parametrize('name,accepted,expected', [
    ('get_note', ['note_page_v1'], True),
    ('get_note', [], False),
    ('read_local', ['note_page_v1'], False),
])
def test_note_page_negotiation_is_per_request(name, accepted, expected):
    r = relay.Relay()
    seen = []
    def bridge():
        req = r.poll(timeout=5)
        seen.append(req)
        r.respond({'jsonrpc': '2.0', 'id': req['id'], 'result': {}})
    r._touch()
    worker = threading.Thread(target=bridge)
    worker.start()
    r.mcp_call({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': name, 'arguments': {'id': 1},
                           '_meta': {'sotto/accepts': accepted, 'sotto/note_cursor': 'first'}}}, timeout=5)
    worker.join()
    meta = seen[0]['params']['_meta']
    assert ('note_page_v1' in meta['sotto/accepts']) is expected
    assert meta['sotto/note_cursor'] == 'first'
