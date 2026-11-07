"""Standalone, bounded Browser Harness driver. Not a tenant network sandbox."""

from __future__ import annotations

import argparse
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

MAX_INPUT = 65_536
MAX_ACTIONS = 12
MAX_TEXT = 16_000
DEADLINE = 90
VERSION = "0.1.13"
USER_AGENT = "OpenAI File Downloader, XaiImageApiFetch/1.0"


class InvalidRequestError(ValueError):
    pass


# Chromium parses any host whose last label is a number (127.1, 0x7f.1, 169.254.43518) as IPv4,
# maps full-width digits and percent-escapes first, and reads a backslash as a path separator.
_PUBLIC_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")


def validate_url(value, fixture_origin=None):
    if (not isinstance(value, str) or len(value) > 4096 or "\\" in value
            or any(ord(c) < 33 for c in value)):
        raise InvalidRequestError("invalid_url")
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError()
        port = url.port
        origin = f"{url.scheme}://{url.netloc}"
        if fixture_origin and origin == fixture_origin:
            return value
        host = url.hostname.lower().rstrip(".")
        if (_PUBLIC_HOST.fullmatch(host) is None or _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[1])
                or host.endswith((".localhost", ".local", ".internal"))):
            raise ValueError()
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None or port not in {None, 80, 443}:
            raise ValueError()
    except ValueError as exc:
        raise InvalidRequestError("invalid_url") from exc
    return value


def validate_request(request, *, interactions=False, fixture_origin=None):
    if not isinstance(request, dict) or set(request) != {"actions"}:
        raise InvalidRequestError("expected_actions")
    actions = request["actions"]
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
        raise InvalidRequestError("action_limit")
    for action in actions:
        if not isinstance(action, dict):
            raise InvalidRequestError("invalid_action")
        kind = action.get("type")
        if kind == "navigate" and set(action) == {"type", "url"}:
            validate_url(action["url"], fixture_origin)
        elif kind == "snapshot" and set(action) == {"type"}:
            pass
        elif kind == "click" and interactions and set(action) == {"type", "x", "y"}:
            if any(type(action[k]) is not int or not 0 <= action[k] < limit
                   for k, limit in (("x", 1280), ("y", 720))):
                raise InvalidRequestError("invalid_coordinates")
        elif kind == "scroll" and set(action) == {"type", "dy"}:
            if type(action["dy"]) is not int or not -1440 <= action["dy"] <= 1440:
                raise InvalidRequestError("invalid_scroll")
        else:
            raise InvalidRequestError("unsupported_action")
    if actions[0]["type"] != "navigate":
        raise InvalidRequestError("navigate_first")
    return request


def read_request():
    chunks = bytearray()
    deadline = time.monotonic() + 5
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([sys.stdin.buffer], [], [], remaining)[0]:
            raise InvalidRequestError("input_timeout")
        chunk = os.read(sys.stdin.fileno(), min(8192, MAX_INPUT + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_INPUT:
            raise InvalidRequestError("input_limit")
    try:
        return json.loads(chunks)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidRequestError("invalid_json") from exc


def signal_group(process, signum):
    # Darwin can report EPERM for a group containing only a newly exited zombie.
    # Reap our leader and retry briefly; never reinterpret persistent EPERM as success.
    deadline = time.monotonic() + 0.2
    while True:
        process.poll()
        try:
            os.killpg(process.pid, signum)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def stop(process, *, grace_seconds=3):
    if process is None:
        return
    try:
        signal_group(process, signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        # Reaping only the group leader is insufficient: Chromium or another owned child
        # may ignore SIGTERM after that leader exits. Keep checking the whole group.
        while True:
            if not signal_group(process, 0):
                break
            if time.monotonic() >= deadline:
                signal_group(process, signal.SIGKILL)
                break
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        process.wait(timeout=3)
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def stop_all(processes):
    failed = False
    for process in processes:
        try:
            stop(process)
        except (OSError, subprocess.TimeoutExpired):
            failed = True
    if failed:
        raise InvalidRequestError("browser_cleanup_failed")


def child_env(root):
    return {
        "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "TMPDIR": str(root),
        "BH_HOME": str(root / "harness"), "BH_AGENT_WORKSPACE": str(root / "workspace"),
        "BH_RUNTIME_DIR": str(root / "ipc"), "BH_TMP_DIR": str(root / "tmp"),
        "BH_TELEMETRY": "0", "BROWSER_HARNESS_TELEMETRY": "0",
        "ANONYMIZED_TELEMETRY": "false", "BH_DOMAIN_SKILLS": "0", "BH_TAB_MARKER": "0",
    }


def collect_worker(process, request):
    """Bound captured output before allocating it all in the supervisor."""
    process.stdin.write(json.dumps(request, ensure_ascii=False).encode())
    process.stdin.close()
    output = bytearray()
    deadline = time.monotonic() + DEADLINE
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise InvalidRequestError("browser_timeout")
        chunk = os.read(process.stdout.fileno(), 65_536)
        if not chunk:
            break
        output.extend(chunk)
        if len(output) > 2_000_000:
            raise InvalidRequestError("output_limit")
    if process.wait(timeout=max(0.1, deadline - time.monotonic())):
        raise InvalidRequestError("browser_operation_failed")
    try:
        return json.loads(output)
    except ValueError as exc:
        raise InvalidRequestError("invalid_worker_response") from exc


def run_browser(request, chromium, *, interactions=False, fixture_origin=None):
    if importlib.metadata.version("browser-harness") != VERSION:
        raise InvalidRequestError("driver_version_mismatch")
    validate_request(request, interactions=interactions, fixture_origin=fixture_origin)
    browser = daemon = worker = None
    # Short path also respects Unix domain socket path limits on macOS.
    with tempfile.TemporaryDirectory(prefix="sotto-bh-", dir="/tmp") as directory:
        root = Path(directory)
        env = child_env(root)
        for name in ("profile", "harness", "workspace", "ipc", "tmp"):
            (root / name).mkdir(mode=0o700)
        try:
            browser = subprocess.Popen([
                chromium, "--headless=new", "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0", f"--user-data-dir={root / 'profile'}",
                "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                "--disable-sync", "--disable-extensions", "--disable-component-update",
                "--disable-breakpad", "--window-size=1280,720", f"--user-agent={USER_AGENT}",
                "about:blank",
            ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            portfile = root / "profile" / "DevToolsActivePort"
            deadline = time.monotonic() + 15
            while not portfile.exists():
                if browser.poll() is not None or time.monotonic() > deadline:
                    raise InvalidRequestError("browser_start_failed")
                time.sleep(0.05)
            port, websocket = portfile.read_text().splitlines()[:2]
            if not port.isdigit() or not websocket.startswith("/devtools/browser/"):
                raise InvalidRequestError("invalid_debug_endpoint")
            env["BU_CDP_WS"] = f"ws://127.0.0.1:{port}{websocket}"
            daemon = subprocess.Popen([sys.executable, "-I", "-m", "browser_harness.daemon"],
                                      env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True)
            command = [sys.executable, "-I", str(Path(__file__).resolve()), "--worker"]
            if interactions:
                command.append("--allow-interactions")
            if fixture_origin:
                command.extend(["--fixture-origin", fixture_origin])
            worker = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
            return collect_worker(worker, request)
        except subprocess.TimeoutExpired as exc:
            raise InvalidRequestError("browser_timeout") from exc
        finally:
            stop_all((worker, daemon, browser))


def perform(request):
    # Import only inside the fresh worker, with private workspace and telemetry disabled.
    from browser_harness import helpers as driver  # noqa: PLC0415 — private child environment required

    deadline = time.monotonic() + 15
    while True:
        try:
            driver.page_info()
            break
        except (OSError, RuntimeError):
            if time.monotonic() > deadline:
                raise InvalidRequestError("driver_start_failed")
            time.sleep(0.05)
    # Downloads never become host artifacts. Chromium still runs page JavaScript.
    driver.cdp("Browser.setDownloadBehavior", behavior="deny")
    results = []
    for action in request["actions"]:
        kind = action["type"]
        if kind == "navigate":
            response = driver.goto_url(action["url"])
            if response.get("errorText"):
                raise InvalidRequestError("navigation_failed")
            driver.wait_for_load(timeout=10)
        elif kind == "click":
            driver.click_at_xy(action["x"], action["y"])
        elif kind == "scroll":
            driver.scroll(640, 360, dy=action["dy"])
        # Fixed source only. No requested expression/selector is evaluated as code.
        snapshot = driver.js("""JSON.stringify((() => {
          const text = document.body?.innerText || '';
          return {title: document.title.slice(0,512), text: text.slice(0,16000),
            truncated: text.length > 16000};
        })())""")
        # Page script can replace the built-ins above, so bounds are enforced here and the URL
        # comes from the browser's navigation entry, never from the page.
        try:
            page = json.loads(snapshot)
            history = driver.cdp("Page.getNavigationHistory")
            url = history["entries"][history["currentIndex"]]["url"]
            title, text = page["title"], page["text"]
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise InvalidRequestError("invalid_snapshot") from exc
        if not all(isinstance(value, str) for value in (url, title, text)):
            raise InvalidRequestError("invalid_snapshot")
        results.append({"action": kind, "url": url[:4096], "title": title[:512],
                        "text": text[:MAX_TEXT], "truncated": len(text) > MAX_TEXT or page.get("truncated") is True})
    return {"ok": True, "driver": f"browser-harness/{VERSION}", "results": results,
            "coverage": "rendered_page_only", "networkSandbox": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", help="Operator-provisioned absolute Chromium executable")
    parser.add_argument("--allow-interactions", action="store_true", help="Permit coordinate clicks")
    parser.add_argument("--fixture-origin", help="Operator-only loopback origin for synthetic tests")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.fixture_origin:
        parsed = urlsplit(args.fixture_origin)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path:
            parser.error("fixture origin must be http://127.0.0.1:PORT")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        request = validate_request(read_request(), interactions=args.allow_interactions,
                                   fixture_origin=args.fixture_origin)
        if args.worker:
            result = perform(request)
        else:
            if not args.chromium or not Path(args.chromium).is_absolute():
                raise InvalidRequestError("chromium_required")
            result = run_browser(request, args.chromium, interactions=args.allow_interactions,
                                 fixture_origin=args.fixture_origin)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (InvalidRequestError, OSError, importlib.metadata.PackageNotFoundError) as exc:
        code = str(exc) if isinstance(exc, InvalidRequestError) else "browser_unavailable"
        print(json.dumps({"ok": False, "error": code}))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "cancelled"}))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
