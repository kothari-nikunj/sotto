"""Wrong `/setup?code=` guesses are counted per socket peer (never a client-supplied header): after
SETUP_FAIL_FREE of them, each further wrong guess waits SETUP_FAIL_DELAY_SECS. A right code is
always compared and always works, so junk traffic cannot lock the owner out."""
from http import HTTPStatus
import importlib.util
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DELAY = 0.3


def _serve(tmp_path):
    spec = importlib.util.spec_from_file_location("receiver_setup_backoff", os.path.join(HERE, "receiver.py"))
    receiver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(receiver)
    receiver.DATA = str(tmp_path)
    receiver.SETTINGS_FILE = os.path.join(str(tmp_path), "config", "settings.json")
    receiver.SETUP_CODE = "right-code-123"
    receiver.MCP_TOKEN = receiver.RELAY_TOKEN = receiver.TOKEN = "bearer-tok"
    receiver.SETUP_FAIL_DELAY_SECS = DELAY
    server = ThreadingHTTPServer(("127.0.0.1", 0), receiver.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return receiver, server, f"http://127.0.0.1:{server.server_address[1]}"


def _get(base, path, headers=None):
    started = time.monotonic()
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers or {}), timeout=10) as r:
            return r.status, time.monotonic() - started
    except urllib.error.HTTPError as error:
        return error.code, time.monotonic() - started


def test_junk_codes_slow_wrong_guesses_but_never_lock_out_the_right_code(tmp_path):
    receiver, server, base = _serve(tmp_path)
    try:
        for _ in range(receiver.SETUP_FAIL_FREE):
            status, took = _get(base, "/setup?code=wrong")
            assert status == HTTPStatus.FORBIDDEN and took < DELAY
        for forwarded in ("203.0.113.1", "198.51.100.2"):   # a forged header does not reset the count
            status, took = _get(base, "/setup?code=wrong", {"X-Forwarded-For": forwarded})
            assert status == HTTPStatus.FORBIDDEN and took >= DELAY
        status, took = _get(base, "/setup?code=right-code-123")
        assert status == HTTPStatus.OK and took < DELAY
        status, took = _get(base, "/setup?code=wrong")
        assert status == HTTPStatus.FORBIDDEN and took < DELAY, "a right code resets the count"
    finally:
        server.shutdown()


def test_an_existing_setup_cookie_works_after_many_wrong_codes(tmp_path):
    receiver, server, base = _serve(tmp_path)
    try:
        for _ in range(receiver.SETUP_FAIL_FREE + 1):
            _get(base, "/setup?code=wrong")
        assert _get(base, "/setup", {"Cookie": "sotto_setup=right-code-123"})[0] == HTTPStatus.OK
    finally:
        server.shutdown()
