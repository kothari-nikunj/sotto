"""/mcp and /chat/brief serve only the co-located chat runtime, like every /runtime/* route: a valid
read bearer from a non-loopback peer is refused. /client-mcp is the internet-facing lane and stays
reachable."""
from http import HTTPStatus
import importlib.util
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _receiver(tmp_path):
    spec = importlib.util.spec_from_file_location("receiver_loopback", os.path.join(HERE, "receiver.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DATA = str(tmp_path)
    module.RELAY_TOKEN = module.TOKEN = "root-tok"
    module.MCP_TOKEN = module.derive_mcp_token("root-tok")
    return module


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _post(base, path, token):
    request = urllib.request.Request(base + path, data=b"{}", method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def _remote_handler(receiver):
    class Remote(receiver.Handler):
        def setup(self):
            super().setup()
            self.client_address = ("203.0.113.7", 40000)
    return Remote


@pytest.mark.parametrize("path", ["/mcp", "/chat/brief"])
def test_a_valid_read_bearer_from_a_remote_peer_is_refused(tmp_path, path):
    receiver = _receiver(tmp_path)
    server, base = _serve(_remote_handler(receiver))
    try:
        assert _post(base, path, receiver.MCP_TOKEN) == HTTPStatus.UNAUTHORIZED
    finally:
        server.shutdown()


@pytest.mark.parametrize("path", ["/mcp", "/chat/brief"])
def test_the_loopback_runtime_still_authenticates(tmp_path, path):
    receiver = _receiver(tmp_path)
    server, base = _serve(receiver.Handler)
    try:
        assert _post(base, path, receiver.MCP_TOKEN) != HTTPStatus.UNAUTHORIZED
    finally:
        server.shutdown()


def test_the_internet_facing_client_mcp_lane_is_not_loopback_gated(tmp_path):
    receiver = _receiver(tmp_path)
    server, base = _serve(_remote_handler(receiver))
    try:
        status = _post(base, "/client-mcp", "not-a-client-token")
        assert status != HTTPStatus.NOT_FOUND and status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)
    finally:
        server.shutdown()
