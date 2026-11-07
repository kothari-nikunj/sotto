#!/usr/bin/env python3
"""One offline Kokoro request per process; model installation is a separate operator step."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import math
import multiprocessing
import os
from pathlib import Path, PurePosixPath
import platform
import re
import select
import signal
import stat
import sys
import tempfile
import time
import wave

MAX_TEXT = 6000
MAX_REQUEST_BYTES = 32768
MAX_MANIFEST_BYTES = 2_000_000
MAX_ASSET_BYTES = 4_000_000_000
MAX_ASSET_FILES = 10000
MAX_AUDIO_SECONDS = 600
MAX_TIMEOUT_SECONDS = 180
MAX_INPUT_SECONDS = 30
PROTOCOL_VERSION = 1


class SpeechError(Exception):
    """Stable public error code; never include input text or filesystem details."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SpeechError("invalid_json")
            result[key] = value
        return result

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(SpeechError("invalid_json")),
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SpeechError("invalid_json") from exc


def keys(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or not set(required) <= value.keys()
        or value.keys() - set(required) - set(optional)
    ):
        raise SpeechError("invalid_schema")


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise SpeechError("invalid_asset_path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value or value == ".":
        raise SpeechError("invalid_asset_path")
    return value


def inventory(root):
    """Reject links and special files, and hash every installed asset, including G2P data."""
    if root.is_symlink() or not root.is_dir():
        raise SpeechError("invalid_pack")
    files = {}
    total = 0

    def unreadable(_error):
        raise SpeechError("asset_unreadable")

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=unreadable):
        for name in dirs + names:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise SpeechError("invalid_asset_path")
        for name in names:
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            if rel == "manifest.json":
                continue
            total += path.stat().st_size
            if total > MAX_ASSET_BYTES or len(files) >= MAX_ASSET_FILES:
                raise SpeechError("pack_too_large")
            files[rel] = digest(path)
    if not files:
        raise SpeechError("empty_pack")
    return files


def validate_config(config):
    keys(config, ("version", "id", "engine", "source", "licenses", "roles", "voices"), ("files",))
    if type(config["version"]) is not int or config["version"] != 1 or config["engine"] != "kokoro":
        raise SpeechError("unsupported_pack")
    if not isinstance(config["id"], str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,128}", config["id"]):
        raise SpeechError("invalid_pack_id")
    if (
        not isinstance(config["source"], str)
        or not config["source"].startswith("https://")
        or len(config["source"]) > 2048
    ):
        raise SpeechError("invalid_source")
    if not isinstance(config["licenses"], list) or not 1 <= len(config["licenses"]) <= 20:
        raise SpeechError("missing_license_inventory")
    for license_item in config["licenses"]:
        keys(license_item, ("component", "license", "source"))
        if any(not isinstance(v, str) or not v or len(v) > 2048 for v in license_item.values()):
            raise SpeechError("invalid_license_inventory")
    roles = config["roles"]
    keys(roles, ("model", "voices", "tokens", "data_dir"), ("lexicons",))
    for name in ("model", "voices", "tokens", "data_dir"):
        relative(roles[name])
    if not isinstance(roles.get("lexicons", []), list) or len(roles.get("lexicons", [])) > 20:
        raise SpeechError("invalid_lexicons")
    for value in roles.get("lexicons", []):
        relative(value)
        if "," in value:
            raise SpeechError("invalid_asset_path")
    voices = config["voices"]
    if not isinstance(voices, dict) or not 1 <= len(voices) <= 100:
        raise SpeechError("invalid_voices")
    for name, speaker_id in voices.items():
        if (
            not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name)
            or type(speaker_id) is not int
            or not 0 <= speaker_id < 1000
        ):
            raise SpeechError("invalid_voices")


def validate_pack(root):
    manifest = root / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > MAX_MANIFEST_BYTES:
        raise SpeechError("invalid_manifest")
    config = read_json(manifest.read_bytes())
    validate_config(config)
    expected = config.get("files")
    if not isinstance(expected, dict) or not expected:
        raise SpeechError("missing_checksums")
    for name, sha in expected.items():
        relative(name)
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise SpeechError("invalid_checksum")
    if expected != inventory(root):
        raise SpeechError("asset_integrity_failed")
    roles = config["roles"]
    for value in [roles[k] for k in ("model", "voices", "tokens")] + roles.get("lexicons", []):
        if value not in expected:
            raise SpeechError("missing_asset")
    data_dir = roles["data_dir"] + "/"
    if not any(name.startswith(data_dir) for name in expected):
        raise SpeechError("missing_g2p_data")
    return config


def seal(root, config):
    """Operator provisioning only: checksum existing, trusted files; no downloads."""
    validate_config(config)
    if "files" in config or (root / "manifest.json").exists():
        raise SpeechError("already_sealed")
    result = dict(config, files=inventory(root))
    # O_EXCL prevents replacing an existing manifest or following a pre-planted link.
    with (root / "manifest.json").open("x") as stream:
        stream.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    try:
        validate_pack(root)
    except Exception:
        (root / "manifest.json").unlink()
        raise
    return result


def request(raw, pack):
    if len(raw) > MAX_REQUEST_BYTES:
        raise SpeechError("request_too_large")
    value = read_json(raw)
    keys(value, ("text", "voice"), ("speed",))
    text = value["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise SpeechError("invalid_text")
    if any((ord(c) < 32 and c not in "\n\t") or 0xD800 <= ord(c) <= 0xDFFF for c in text):
        raise SpeechError("invalid_text")
    voice = value["voice"]
    if not isinstance(voice, str) or voice not in pack["voices"]:
        raise SpeechError("unknown_voice")
    speed = value.get("speed", 1.0)
    if type(speed) not in (float, int) or not 0.5 <= speed <= 2.0 or not math.isfinite(speed):
        raise SpeechError("invalid_speed")
    return {"text": text, "voice": voice, "speed": float(speed)}


def runtime_identity():
    lock = Path(__file__).with_name("requirements.lock")
    versions = {}
    for name, version in re.findall(r"^([a-zA-Z0-9_-]+)==([^\s]+)", lock.read_text(), re.MULTILINE):
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise SpeechError("runtime_not_installed") from exc
        if actual != version:
            raise SpeechError("runtime_version_mismatch")
        versions[name] = actual
    return {
        "packages": versions,
        "lock_sha256": digest(lock),
        "worker_sha256": digest(Path(__file__)),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "provider": "cpu",
        "threads": 2,
        "format": "wav-pcm-s16le-mono",
        "protocol": PROTOCOL_VERSION,
    }


def cache_id(pack, value, runtime):
    return hashlib.sha256(canonical({"pack": pack, "request": value, "runtime": runtime})).hexdigest()


def _infer(root, pack, value, output, pipe):
    # Child alone imports native code. The parent can kill it even if inference stops responding.
    try:
        import numpy as np  # noqa: PLC0415 -- native code stays in killable child
        import sherpa_onnx  # noqa: PLC0415 -- parent must run without native dependencies

        roles = pack["roles"]
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(root / roles["model"]),
                    voices=str(root / roles["voices"]),
                    tokens=str(root / roles["tokens"]),
                    data_dir=str(root / roles["data_dir"]),
                    lexicon=",".join(str(root / name) for name in roles.get("lexicons", [])),
                ),
                provider="cpu",
                num_threads=2,
                debug=False,
            ),
            max_num_sentences=1,
        )
        if not config.validate():
            raise SpeechError("invalid_engine_config")
        engine = sherpa_onnx.OfflineTts(config)
        sid = pack["voices"][value["voice"]]
        if sid >= engine.num_speakers:
            raise SpeechError("unknown_voice")
        frames = 0
        too_long = False

        def bounded(samples, progress):
            nonlocal frames, too_long
            frames += len(samples)
            too_long = frames > engine.sample_rate * MAX_AUDIO_SECONDS
            # Kokoro's pinned C++ implementation uses 1=continue, 0=stop;
            # its Python docstring currently states the inverse.
            return int(not too_long)

        audio = engine.generate(value["text"], sid=sid, speed=value["speed"], callback=bounded)
        if too_long or len(audio.samples) > audio.sample_rate * MAX_AUDIO_SECONDS:
            raise SpeechError("audio_too_long")
        samples = np.asarray(audio.samples)
        if not len(samples) or not np.isfinite(samples).all():
            raise SpeechError("invalid_audio")
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(audio.sample_rate)
            wav.writeframes(pcm)
        pipe.send({"ok": True})
    except SpeechError as exc:
        pipe.send({"error": str(exc)})
    except Exception:  # noqa: BLE001 -- native boundary exposes only stable, text-free errors
        pipe.send({"error": "inference_failed"})
    finally:
        pipe.close()


def wait_for_child(child, receive, timeout, cancelled):
    deadline = time.monotonic() + timeout
    while True:
        if cancelled():
            raise SpeechError("cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SpeechError("timeout")
        if receive.poll(min(0.05, remaining)):
            try:
                result = receive.recv()
            except EOFError as exc:
                raise SpeechError("inference_failed") from exc
            if result.get("error"):
                raise SpeechError(result["error"])
            return
        if not child.is_alive():
            raise SpeechError("inference_failed")


def audio_metadata(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 96_000 * MAX_AUDIO_SECONDS + 4096:
        raise SpeechError("invalid_audio")
    try:
        with wave.open(str(path), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
            if (
                wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or not 8000 <= rate <= 48000
                or not 0 < frames <= rate * MAX_AUDIO_SECONDS
            ):
                raise SpeechError("invalid_audio")
            if len(wav.readframes(frames)) != frames * 2:
                raise SpeechError("invalid_audio")
    except (wave.Error, EOFError) as exc:
        raise SpeechError("invalid_audio") from exc
    return {
        "sample_rate": rate,
        "frames": frames,
        "duration_seconds": frames / rate,
        "channels": 1,
        "encoding": "pcm_s16le",
        "bytes": path.stat().st_size,
        "sha256": digest(path),
    }


def synthesize(root, output, raw, *, timeout=MAX_TIMEOUT_SECONDS, cancelled=lambda: False, _worker=_infer):
    """Paths and cancellation come from the operator/caller, never from request JSON."""
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise SpeechError("invalid_timeout")
    pack = validate_pack(root)
    value = request(raw, pack)
    runtime = runtime_identity()
    identity = cache_id(pack, value, runtime)
    if cancelled():
        raise SpeechError("cancelled")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.is_symlink() or output.stat().st_mode & 0o077 or output.stat().st_uid != os.getuid():
        raise SpeechError("output_must_be_private")
    # This private directory has one active writer. No request controls a filename.
    lock_fd = os.open(output / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SpeechError("busy") from exc
        wav_path, meta_path = output / (identity + ".wav"), output / (identity + ".json")
        if wav_path.is_symlink() or meta_path.is_symlink():
            raise SpeechError("invalid_cache")
        if meta_path.exists():
            if meta_path.stat().st_size > 8192:
                raise SpeechError("invalid_cache")
            result = read_json(meta_path.read_bytes())
            if (
                not isinstance(result, dict)
                or set(result) != {"ok", "cache_id", "artifact", "cached", "pack_id", "voice", "audio"}
                or result.get("ok") is not True
                or result.get("artifact") != wav_path.name
                or result.get("voice") != value["voice"]
                or result.get("pack_id") != pack["id"]
                or result.get("cache_id") != identity
                or result.get("audio") != audio_metadata(wav_path)
            ):
                raise SpeechError("invalid_cache")
            return dict(result, cached=True)
        with tempfile.TemporaryDirectory(prefix=".speech-", dir=output) as scratch:
            temporary = Path(scratch) / "audio.wav"
            context = multiprocessing.get_context("spawn")
            receive, send = context.Pipe(duplex=False)
            child = context.Process(target=_worker, args=(root, pack, value, temporary, send), daemon=True)
            child.start()
            send.close()
            try:
                wait_for_child(child, receive, timeout, cancelled)
            finally:
                receive.close()
                if child.is_alive():
                    child.kill()
                child.join()
            if cancelled():
                raise SpeechError("cancelled")
            result = {
                "ok": True,
                "cache_id": identity,
                "artifact": wav_path.name,
                "cached": False,
                "pack_id": pack["id"],
                "voice": value["voice"],
                "audio": audio_metadata(temporary),
            }
            temporary.replace(wav_path)
            metadata = Path(scratch) / "metadata.json"
            metadata.write_bytes(canonical(result) + b"\n")
            metadata.replace(meta_path)
            return result


def read_request(stream, cancelled, timeout=MAX_INPUT_SECONDS):
    """Read one EOF-delimited request without trapping shutdown behind an open pipe."""
    deadline = time.monotonic() + timeout
    data = bytearray()
    while True:
        if cancelled():
            raise SpeechError("cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SpeechError("input_timeout")
        ready, _, _ = select.select([stream.fileno()], [], [], min(remaining, 0.05))
        if not ready:
            continue
        chunk = os.read(stream.fileno(), MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > MAX_REQUEST_BYTES:
            raise SpeechError("request_too_large")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("seal", "check", "synthesize"))
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--config", type=Path, help="Operator pack definition for seal only")
    parser.add_argument("--output", type=Path, help="Private cache directory for synthesize")
    args = parser.parse_args()
    stopped = False

    def cancel(_signum, _frame):
        nonlocal stopped
        stopped = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, cancel)
    os.umask(0o077)
    try:
        if args.command == "seal":
            if args.config is None or args.config.stat().st_size > MAX_MANIFEST_BYTES:
                raise SpeechError("invalid_config")
            pack = seal(args.pack, read_json(args.config.read_bytes()))
            result = {"ok": True, "pack_id": pack["id"], "files": len(pack["files"])}
        elif args.command == "check":
            pack = validate_pack(args.pack)
            result = {
                "ok": True,
                "pack_id": pack["id"],
                "voices": list(pack["voices"]),
                "runtime": runtime_identity(),
            }
        else:
            if args.output is None:
                raise SpeechError("missing_output")
            raw = read_request(sys.stdin.buffer, lambda: stopped)
            result = synthesize(args.pack, args.output, raw, cancelled=lambda: stopped)
        print(json.dumps(result))
        return 0
    except SpeechError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    except (OSError, ValueError):
        print(json.dumps({"ok": False, "error": "io_error"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
