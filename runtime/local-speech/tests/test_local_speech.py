"""Contract tests need no native runtime or model; smoke test is separately opt-in."""

import fcntl
import hashlib
import json
import os
import select
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import speech


def fake_infer(root, pack, value, output, pipe):
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x01" * 2400)
    pipe.send({"ok": True})
    pipe.close()


def stalled_infer(root, pack, value, output, pipe):
    output.write_bytes(b"partial")
    time.sleep(10)


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "pack"
        self.root.mkdir()
        (self.root / "data").mkdir()
        for name in ["model.onnx", "voices.bin", "tokens.txt", "data/phonemes"]:
            (self.root / name).write_bytes(name.encode())
        self.config = {
            "version": 1,
            "id": "test",
            "engine": "kokoro",
            "source": "https://example.com/model",
            "licenses": [{"component": "test", "license": "test", "source": "test"}],
            "roles": {
                "model": "model.onnx",
                "voices": "voices.bin",
                "tokens": "tokens.txt",
                "data_dir": "data",
            },
            "voices": {"default": 0},
        }
        self.pack = speech.seal(self.root, self.config)
        self.output = Path(self.temp.name) / "audio"
        self.raw = b'{"text":"Hello world.","voice":"default"}'
        self.runtime = {"test_runtime": "1"}

    def tearDown(self):
        self.temp.cleanup()

    def run_synthesis(self, **kwargs):
        with patch.object(speech, "runtime_identity", return_value=self.runtime):
            return speech.synthesize(self.root, self.output, self.raw, **kwargs)

    def assert_error(self, code, callback):
        with self.assertRaisesRegex(speech.SpeechError, "^" + code + "$"):
            callback()

    def test_unfinished_input_timeout_and_signal_cancellation(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b'{"text":')
            with os.fdopen(read_fd, "rb") as stream:
                self.assert_error(
                    "input_timeout", lambda: speech.read_request(stream, lambda: False, timeout=0.1)
                )
        finally:
            os.close(write_fd)
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys\n"
                "sys.path.insert(0, sys.argv.pop(1))\n"
                "import speech\n"
                "read = speech.read_request\n"
                "def ready(*args, **kwargs):\n"
                "    sys.stderr.write('ready\\n')\n"
                "    sys.stderr.flush()\n"
                "    return read(*args, **kwargs)\n"
                "speech.read_request = ready\n"
                "raise SystemExit(speech.main())\n",
                str(Path(speech.__file__).parent),
                "synthesize",
                "--pack",
                str(self.root),
                "--output",
                str(self.output),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            child.stdin.write(b'{"text":')
            child.stdin.flush()
            # Signal only after main installs its handlers and enters input reading.
            # A startup sleep races slow CI/interpreter initialization.
            self.assertTrue(select.select([child.stderr], [], [], 5)[0], "worker did not reach input")
            self.assertEqual(child.stderr.readline(), b"ready\n")
            child.terminate()
            child.wait(timeout=3)
            self.assertEqual(json.loads(child.stdout.read()), {"ok": False, "error": "cancelled"})
            self.assertEqual(child.returncode, 1)
            self.assertFalse(self.output.exists())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()

    def test_pack_integrity_covers_phonemizer_and_extra_files(self):
        self.assertEqual(speech.validate_pack(self.root), self.pack)
        (self.root / "data/phonemes").write_bytes(b"corrupt")
        self.assert_error("asset_integrity_failed", lambda: speech.validate_pack(self.root))
        (self.root / "data/phonemes").write_bytes(b"data/phonemes")
        (self.root / "unexpected").write_text("extra")
        self.assert_error("asset_integrity_failed", lambda: speech.validate_pack(self.root))

    def test_symlink_asset_and_manifest_rejected(self):
        (self.root / "data/escape").symlink_to(self.root / "tokens.txt")
        self.assert_error("invalid_asset_path", lambda: speech.validate_pack(self.root))
        (self.root / "data/escape").unlink()
        manifest = self.root / "manifest.json"
        outside = self.root.parent / "manifest.json"
        manifest.rename(outside)
        manifest.symlink_to(outside)
        self.assert_error("invalid_manifest", lambda: speech.validate_pack(self.root))

    def test_roles_require_installed_assets(self):
        self.pack["roles"]["model"] = "missing.onnx"
        (self.root / "manifest.json").write_text(json.dumps(self.pack))
        self.assert_error("missing_asset", lambda: speech.validate_pack(self.root))

    def test_malformed_manifest_duplicate_keys_and_unknown_fields(self):
        for data in [b'{"version":1,"version":2}', b"{", b"[]", b"NaN"]:
            (self.root / "manifest.json").write_bytes(data)
            with self.assertRaises(speech.SpeechError):
                speech.validate_pack(self.root)
        self.pack["endpoint"] = "https://remote"
        (self.root / "manifest.json").write_text(json.dumps(self.pack))
        self.assert_error("invalid_schema", lambda: speech.validate_pack(self.root))

    def test_path_traversal_rejected(self):
        for value in ["../evil", "/tmp/evil", "x/../../evil", "x\\evil", "x/./evil", ""]:
            with self.subTest(value=value):
                self.assert_error("invalid_asset_path", lambda: speech.relative(value))

    def test_request_limits_and_no_model_controlled_path(self):
        cases = [
            ({"text": "x", "voice": "default", "output": "../../evil"}, "invalid_schema"),
            ({"text": "x" * 6001, "voice": "default"}, "invalid_text"),
            ({"text": "\x00", "voice": "default"}, "invalid_text"),
            ({"text": "\ud800", "voice": "default"}, "invalid_text"),
            ({"text": " ", "voice": "default"}, "invalid_text"),
            ({"text": "x", "voice": "other"}, "unknown_voice"),
            ({"text": "x", "voice": "default", "speed": True}, "invalid_speed"),
            ({"text": "x", "voice": "default", "speed": 0.4}, "invalid_speed"),
        ]
        for value, error in cases:
            with self.subTest(value=value):
                self.assert_error(error, lambda: speech.request(json.dumps(value).encode(), self.pack))
        self.assert_error("request_too_large", lambda: speech.request(b" " * 32769, self.pack))
        huge_speed = json.dumps({"text": "x", "voice": "default", "speed": 10**1000}).encode()
        self.assert_error("invalid_speed", lambda: speech.request(huge_speed, self.pack))
        self.assert_error(
            "invalid_json", lambda: speech.request(b'{"text":"x","voice":"default","speed":NaN}', self.pack)
        )

    def test_identity_changes_for_all_assets_runtime_and_settings(self):
        value = speech.request(self.raw, self.pack)
        original = speech.cache_id(self.pack, value, self.runtime)
        self.assertNotEqual(original, speech.cache_id(self.pack, dict(value, speed=1.2), self.runtime))
        self.assertNotEqual(original, speech.cache_id(self.pack, value, {"test_runtime": "2"}))
        self.pack["files"]["data/phonemes"] = hashlib.sha256(b"new").hexdigest()
        self.assertNotEqual(original, speech.cache_id(self.pack, value, self.runtime))

    def test_cache_metadata_cannot_return_arbitrary_paths(self):
        first = self.run_synthesis(_worker=fake_infer)
        metadata = self.output / (first["cache_id"] + ".json")
        value = json.loads(metadata.read_bytes())
        value["artifact"] = "../../outside.wav"
        metadata.write_text(json.dumps(value))
        self.assert_error("invalid_cache", lambda: self.run_synthesis(_worker=fake_infer))

    def test_one_active_writer_returns_busy(self):
        self.output.mkdir(mode=0o700)
        with (self.output / ".lock").open("w") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_error("busy", lambda: self.run_synthesis(_worker=fake_infer))

    def test_synthesis_cache_metadata_and_corruption(self):
        first = self.run_synthesis(_worker=fake_infer)
        self.assertFalse(first["cached"])
        self.assertEqual(first["audio"]["sample_rate"], 24000)
        self.assertEqual(first["audio"]["duration_seconds"], 0.1)
        self.assertNotIn("Hello", json.dumps(first))
        cached = self.run_synthesis(_worker=stalled_infer)
        self.assertTrue(cached["cached"])
        wav = self.output / first["artifact"]
        data = bytearray(wav.read_bytes())
        data[-1] ^= 1
        wav.write_bytes(data)
        self.assert_error("invalid_cache", lambda: self.run_synthesis(_worker=fake_infer))

    def test_output_directory_and_cached_symlink_safety(self):
        self.output.mkdir(mode=0o755)
        self.output.chmod(0o755)
        self.assert_error("output_must_be_private", lambda: self.run_synthesis(_worker=fake_infer))
        self.output.chmod(0o700)
        identity = speech.cache_id(self.pack, speech.request(self.raw, self.pack), self.runtime)
        outside = Path(self.temp.name) / "outside"
        outside.write_text("untouched")
        (self.output / (identity + ".wav")).symlink_to(outside)
        self.assert_error("invalid_cache", lambda: self.run_synthesis(_worker=fake_infer))
        self.assertEqual(outside.read_text(), "untouched")

    def test_timeout_kills_child_and_removes_partial_artifacts(self):
        start = time.monotonic()
        self.assert_error("timeout", lambda: self.run_synthesis(_worker=stalled_infer, timeout=0.2))
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual([p.name for p in self.output.iterdir()], [".lock"])

    def test_cancellation_kills_child_and_removes_partial_artifacts(self):
        start = time.monotonic()
        self.assert_error(
            "cancelled",
            lambda: self.run_synthesis(
                _worker=stalled_infer, cancelled=lambda: time.monotonic() - start > 0.2
            ),
        )
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual([p.name for p in self.output.iterdir()], [".lock"])

    def test_cancel_before_work_and_private_lock_symlink(self):
        self.assert_error("cancelled", lambda: self.run_synthesis(cancelled=lambda: True))
        self.assertFalse(self.output.exists())
        self.output.mkdir(mode=0o700)
        outside = Path(self.temp.name) / "outside"
        outside.write_text("safe")
        (self.output / ".lock").symlink_to(outside)
        with self.assertRaises(OSError):
            self.run_synthesis(_worker=fake_infer)
        self.assertEqual(outside.read_text(), "safe")


if __name__ == "__main__":
    unittest.main()
