"""Contracts do not open a browser or contact a provider."""

import importlib.util
import os
from pathlib import Path
import select
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sotto_browser_adapter", ROOT / "adapter.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class BrowserHarnessContracts(unittest.TestCase):
    def test_public_url_and_read(self):
        request = {"actions": [{"type": "navigate", "url": "https://example.com/path"},
                               {"type": "snapshot"}]}
        self.assertEqual(adapter.validate_request(request), request)

    def test_dangerous_and_non_public_schemes_rejected(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "http://127.0.0.1/",
                    "http://[::1]", "http://localhost", "https://x.internal", "http://2130706433",
                    "https://user:secret@example.com", "https://example.com:1234", "https://x.com\nx"):
            with self.subTest(url=url), self.assertRaises(adapter.InvalidRequestError):
                adapter.validate_url(url)

    def test_chromium_ipv4_shorthand_and_backslash_hosts_rejected(self):
        for url in ("http://127.1/", "http://0x7f.1/", "http://0177.0.0.1/", "http://10.1/",
                    "http://169.254.43518/latest/meta-data/", "http://127.0.0.1\\x.example.com/",
                    "http://127.0.0%2e1/", "http://\uff11\uff12\uff17.\uff10.\uff10.\uff11/"):
            with self.subTest(url=url), self.assertRaises(adapter.InvalidRequestError):
                adapter.validate_url(url)
        self.assertEqual(adapter.validate_url("https://xn--bcher-kva.example/"), "https://xn--bcher-kva.example/")

    def test_snapshot_url_comes_from_browser_and_page_output_is_bounded(self):
        driver = Mock()
        driver.page_info.return_value = {}
        driver.goto_url.return_value = {}
        driver.js.return_value = ('{"url": "https://docsend.com/view/legit", "title": "%s", "text": "%s"}'
                                  % ("t" * 600, "x" * 20_000))
        driver.cdp.side_effect = lambda method, **_: (
            {"currentIndex": 0, "entries": [{"url": "http://127.0.0.1:9/"}]}
            if method == "Page.getNavigationHistory" else {})
        package = Mock(helpers=driver)
        with patch.dict(sys.modules, {"browser_harness": package, "browser_harness.helpers": driver}):
            result = adapter.perform({"actions": [{"type": "navigate", "url": "https://example.com"}]})
            row = result["results"][0]
            self.assertEqual(row["url"], "http://127.0.0.1:9/")
            self.assertEqual((len(row["title"]), len(row["text"]), row["truncated"]), (512, adapter.MAX_TEXT, True))
            driver.js.return_value = '["not", "a", "snapshot"]'
            with self.assertRaises(adapter.InvalidRequestError):
                adapter.perform({"actions": [{"type": "navigate", "url": "https://example.com"}]})

    def test_fixture_origin_does_not_enable_other_private_hosts(self):
        self.assertEqual(adapter.validate_url("http://127.0.0.1:8123/x", "http://127.0.0.1:8123"),
                         "http://127.0.0.1:8123/x")
        with self.assertRaises(adapter.InvalidRequestError):
            adapter.validate_url("http://127.0.0.1:8124/x", "http://127.0.0.1:8123")

    def test_input_has_no_code_or_launch_options(self):
        for action in ({"type": "evaluate", "code": "anything"},
                       {"type": "snapshot", "expression": "process.env"},
                       {"type": "navigate", "url": "https://example.com", "profile": "/tmp"}):
            with self.subTest(action=action), self.assertRaises(adapter.InvalidRequestError):
                adapter.validate_request({"actions": [action]})

    def test_click_requires_operator_opt_in(self):
        request = {"actions": [{"type": "navigate", "url": "https://example.com"},
                               {"type": "click", "x": 100, "y": 100}]}
        with self.assertRaises(adapter.InvalidRequestError):
            adapter.validate_request(request)
        adapter.validate_request(request, interactions=True)
        request["actions"][1]["x"] = True
        with self.assertRaises(adapter.InvalidRequestError):
            adapter.validate_request(request, interactions=True)

    def test_action_bound_and_initial_navigation(self):
        for actions in ([], [{"type": "snapshot"}], [{"type": "snapshot"}] * 13):
            with self.assertRaises(adapter.InvalidRequestError):
                adapter.validate_request({"actions": actions})

    def test_children_do_not_inherit_credentials_or_profiles(self):
        env = adapter.child_env(Path("/tmp/test-sotto"))
        self.assertNotIn("GOOGLE_API_KEY", env)
        self.assertNotIn("HOME", env)
        self.assertNotIn("BU_CDP_WS", env)
        self.assertEqual(env["BH_TELEMETRY"], "0")
        self.assertEqual(env["BH_AGENT_WORKSPACE"], "/tmp/test-sotto/workspace")

    def test_cli_rejects_invalid_json_without_driver_installed(self):
        result = subprocess.run([sys.executable, str(ROOT / "adapter.py")], input="bad",
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('"error": "invalid_json"', result.stdout)

    def test_input_limit(self):
        result = subprocess.run([sys.executable, str(ROOT / "adapter.py")], input="x" * 65537,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('"error": "input_limit"', result.stdout)

    def test_worker_output_limit(self):
        worker = subprocess.Popen([sys.executable, "-c", "import sys;sys.stdin.read();print('x'*2100000)"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            with self.assertRaisesRegex(adapter.InvalidRequestError, "output_limit"):
                adapter.collect_worker(worker, {})
        finally:
            adapter.stop(worker)
            worker.stdout.close()

    def test_cleanup_kills_descendant_that_outlives_group_leader(self):
        read_fd, write_fd = os.pipe()
        child = (
            "import os,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            f"os.write({write_fd}, b'ready');time.sleep(60)"
        )
        leader = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c',sys.argv[1]],pass_fds=(int(sys.argv[2]),));"
            "time.sleep(60)"
        )
        process = subprocess.Popen([sys.executable, "-c", leader, child, str(write_fd)],
                                   pass_fds=(write_fd,), start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(write_fd)
        try:
            self.assertTrue(select.select([read_fd], [], [], 5)[0], "child did not start")
            self.assertEqual(os.read(read_fd, 5), b"ready")
            adapter.stop(process, grace_seconds=0.1)
            self.assertIsNotNone(process.returncode)
            self.assertTrue(select.select([read_fd], [], [], 5)[0], "descendant retained its pipe")
            self.assertEqual(os.read(read_fd, 1), b"", "descendant survived cleanup")
        finally:
            adapter.stop(process, grace_seconds=0)
            os.close(read_fd)

    def test_cleanup_attempts_all_groups_when_one_fails(self):
        processes = [object(), object(), object()]
        with patch.object(adapter, "stop", side_effect=[OSError("failed"), None, None]) as stop:
            with self.assertRaisesRegex(adapter.InvalidRequestError, "browser_cleanup_failed"):
                adapter.stop_all(processes)
        self.assertEqual([call.args[0] for call in stop.call_args_list], processes)

    def test_zombie_group_permission_race_requires_confirmed_disappearance(self):
        process = Mock(pid=123)
        with patch.object(adapter.os, "killpg", side_effect=[PermissionError(), ProcessLookupError()]), \
                patch.object(adapter.time, "sleep"):
            self.assertFalse(adapter.signal_group(process, 0))
        self.assertEqual(process.poll.call_count, 2)

    def test_persistent_group_permission_failure_is_not_ignored(self):
        process = Mock(pid=123)
        with patch.object(adapter.os, "killpg", side_effect=PermissionError()), \
                patch.object(adapter.time, "monotonic", side_effect=[0, 1]):
            with self.assertRaises(PermissionError):
                adapter.signal_group(process, 0)


if __name__ == "__main__":
    unittest.main()
