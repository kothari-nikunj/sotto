"""mcp_client.py — the minimal Streamable-HTTP MCP client the deterministic gathers use.

Covers both server response styles (plain JSON body and SSE stream), the Mcp-Session-Id
capture/echo, tool-result text extraction (json-decoded when possible), and 401 → ConnectorAuthError."""
import json
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "_shared", "lib"))

import mcp_client as mc  # noqa: E402
from connector_tokens import ConnectorAuthError  # noqa: E402


class FakeTransport:
    """Scripted transport: pops (status, headers, body) per POST; records every request."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []          # (url, parsed_body_or_None, headers)

    def __call__(self, url, body, headers, timeout):
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        self.requests.append((url, parsed, headers))
        return self.responses.pop(0)


def _json_resp(rid, result, extra_headers=None):
    h = {"Content-Type": "application/json"}
    h.update(extra_headers or {})
    return 200, h, json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()


def test_initialize_captures_session_id_and_echoes_it():
    t = FakeTransport([
        _json_resp(1, {"protocolVersion": "2025-03-26"}, {"Mcp-Session-Id": "sess-42"}),
        (202, {}, b""),                                         # notifications/initialized
        _json_resp(2, {"tools": [{"name": "list_meetings"}]}),
    ])
    c = mc.McpClient("https://mcp.example/mcp", "tok", transport=t)
    c.initialize()
    assert c.session_id == "sess-42"
    tools = c.list_tools()
    assert tools == [{"name": "list_meetings"}]
    # request 1: initialize — right method, bearer, dual Accept, no session id yet
    _, msg0, h0 = t.requests[0]
    assert msg0["method"] == "initialize" and msg0["params"]["protocolVersion"]
    assert h0["Authorization"] == "Bearer tok"
    assert "application/json" in h0["Accept"] and "text/event-stream" in h0["Accept"]
    assert "Mcp-Session-Id" not in h0
    # request 2: the initialized NOTIFICATION — no id
    _, msg1, h1 = t.requests[1]
    assert msg1["method"] == "notifications/initialized" and "id" not in msg1
    assert h1["Mcp-Session-Id"] == "sess-42"                    # echoed from initialize on
    # request 3: tools/list also carries it
    assert t.requests[2][2]["Mcp-Session-Id"] == "sess-42"


def test_call_tool_plain_json_text_content_is_json_decoded():
    t = FakeTransport([_json_resp(1, {"content": [
        {"type": "text", "text": json.dumps([{"title": "Sync", "id": "m1"}])}]})])
    c = mc.McpClient("u", "tok", transport=t)
    out = c.call_tool("list_meetings", {"limit": 50})
    assert out == [{"title": "Sync", "id": "m1"}]
    assert t.requests[0][1]["params"] == {"name": "list_meetings", "arguments": {"limit": 50}}


def test_call_tool_non_json_text_stays_raw():
    t = FakeTransport([_json_resp(1, {"content": [{"type": "text", "text": "you said: ship it"}]})])
    out = mc.McpClient("u", "t", transport=t).call_tool("get_meeting_transcript", {"meeting_id": "m1"})
    assert out == "you said: ship it"


def test_call_tool_sse_response_parsed_to_matching_id():
    # Server answers as an SSE stream: a notification first, then the response for OUR id.
    sse = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n'
        "\n"
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\\"ok\\":true}"}]}}\n'
        "\n")
    t = FakeTransport([(200, {"Content-Type": "text/event-stream"}, sse.encode())])
    out = mc.McpClient("u", "t", transport=t).call_tool("x")
    assert out == {"ok": True}


def test_sse_multiline_data_joined():
    sse = ('data: {"jsonrpc":"2.0","id":1,\n'
           'data:  "result":{"content":[{"type":"text","text":"hi"}]}}\n\n')
    t = FakeTransport([(200, {"Content-Type": "text/event-stream"}, sse.encode())])
    assert mc.McpClient("u", "t", transport=t).call_tool("x") == "hi"


def test_401_raises_connector_auth_error():
    t = FakeTransport([(401, {}, b"Unauthorized")])
    try:
        mc.McpClient("u", "stale", transport=t).call_tool("list_meetings")
        raise AssertionError("expected ConnectorAuthError")
    except ConnectorAuthError:
        pass


def test_jsonrpc_error_raises_mcp_error():
    t = FakeTransport([(200, {"Content-Type": "application/json"},
                        json.dumps({"jsonrpc": "2.0", "id": 1,
                                    "error": {"code": -32601, "message": "no such tool"}}).encode())])
    try:
        mc.McpClient("u", "t", transport=t).call_tool("nope")
        raise AssertionError("expected McpError")
    except mc.McpError as e:
        assert "no such tool" in str(e)


def test_tool_is_error_result_raises():
    t = FakeTransport([_json_resp(1, {"isError": True,
                                      "content": [{"type": "text", "text": "boom"}]})])
    try:
        mc.McpClient("u", "t", transport=t).call_tool("x")
        raise AssertionError("expected McpError")
    except mc.McpError:
        pass
