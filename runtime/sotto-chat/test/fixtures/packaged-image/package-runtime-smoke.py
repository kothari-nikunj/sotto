"""Credential-free checks against files shipped in the selected runtime image."""
import grp
import importlib.util
import hashlib
import os
import pwd
import sys
import tempfile
from pathlib import Path


def require(condition, message):
    if not condition:
        raise AssertionError(message)


require(os.geteuid() == 10000, "smoke must run as packaged Node UID")
require(os.getegid() == 10000, "smoke must run with packaged Node GID")
require(pwd.getpwuid(10000).pw_name == "sotto-runtime", "UID 10000 account mismatch")
require(pwd.getpwnam("sotto").pw_uid == 10001, "receiver UID account mismatch")
require(grp.getgrnam("sotto").gr_gid == 10001, "receiver GID group mismatch")

for path in ("/app/trigger-receiver", "/app/adapters/sotto"):
    require(Path(path).is_dir(), f"missing packaged directory: {path}")

sys.path[:0] = ["/app/trigger-receiver", "/app/adapters/sotto"]
import control_vault  # noqa: E402
import managed_exec  # noqa: E402
import model_lease  # noqa: E402
import tenant_lifecycle  # noqa: E402
import runtime_lock  # noqa: E402
import photon_owner  # noqa: E402

require(control_vault, "control vault module import failed")
require(managed_exec, "managed executor module import failed")
require(model_lease, "model lease module import failed")
require(tenant_lifecycle, "tenant lifecycle module import failed")
require(runtime_lock, "runtime lock module import failed")
require(photon_owner, "Sotto Photon module import failed")

receiver_path = Path("/app/trigger-receiver/receiver.py")
expected_hash = os.environ["SOTTO_EXPECTED_RECEIVER_SHA256"]
require(len(expected_hash) == 64 and all(char in "0123456789abcdef" for char in expected_hash),
        "expected receiver hash must be lowercase SHA-256 hex")
actual_hash = hashlib.sha256(receiver_path.read_bytes()).hexdigest()
require(actual_hash == expected_hash, "packaged receiver does not match the corrected source snapshot")
spec = importlib.util.spec_from_file_location("packaged_sotto_receiver", receiver_path)
require(spec is not None and spec.loader is not None, "packaged receiver cannot be loaded")
receiver = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = receiver
spec.loader.exec_module(receiver)

require(receiver._runtime_adapter().__name__ == "sotto_runtime_api",
        "receiver did not load the packaged Sotto runtime adapter")
jobs = receiver._sotto_cron_jobs()
job_names = {job[0] for job in jobs}
require(job_names == {"sotto-morning-brief", "sotto-evening-brief",
                      "sotto-relationship-pulse", "sotto-proactive", "sotto-midday-digest"},
        "packaged Sotto system cron list did not resolve the five expected entries")

require(receiver._require_sotto_procedure([sys.executable, str(receiver_path.parent / "brief_runner.py")]) is None,
        "packaged brief runner was not accepted")
require(receiver._require_sotto_procedure([sys.executable, str(receiver_path.parent / "procedure_runner.py")]) is None,
        "packaged procedure runner was not accepted")
memory_script = receiver._find_sotto_script("_shared", "scripts", "memory_cycle.py")
require(memory_script is not None and memory_script.startswith("/app/sotto-skills/"),
        "shared memory-cycle script was not resolved from packaged skills")
require(receiver._require_sotto_procedure([sys.executable, memory_script]) is None,
        "packaged memory-cycle runner was not accepted")
# Exercise the packaged admission path, not just the low-level runner predicate.
# Both spellings must fail before a fallback probes an executable or creates a queue.
for legacy_runner in (["hermes", "-z"], ["/usr/local/bin/hermes", "-z"]):
    try:
        receiver._spawn_and_deliver(legacy_runner, "Synthetic retired runner", "cron:sotto-morning-brief")
    except RuntimeError as exc:
        require("unattended skill procedure is unsupported" in str(exc),
                "legacy runner failed for an unrelated reason")
    else:
        raise AssertionError("legacy Hermes runner unexpectedly reached Sotto queue admission")

require(not Path(os.environ["SOTTO_DATA"]).exists(), "receiver import created persistent state")

# A managed Bridge grant remains usable when Google is not configured. Exercise
# the packaged Sotto adapter and the receiver's real source-authority composition
# with synthetic state and a local fake Relay; never read ambient HOME credentials.
import managed  # noqa: E402

class FakeRelay:
    def __init__(self):
        self.calls = 0

    def bridge_connected(self):
        return True

    def mcp_call(self, request, timeout):
        self.calls += 1
        require(timeout <= 2.0, "receiver exceeded the bounded health probe")
        require(request["params"]["name"] == "health", "unexpected Relay tool call")
        return {"result": {"connected": True, "source_policy": {
            "version": 1, "bridge_revision": "bridge-fixture-1",
            "cloud_revision": "cloud-fixture-1", "managed": True,
            "allowed_sources": ["imessage"]}}}


with tempfile.TemporaryDirectory(prefix="sotto-bridge-only-") as scratch:
    root = Path(scratch)
    home = root / "synthetic-home"
    stale_hermes = home / ".hermes"
    stale_hermes.mkdir(parents=True)
    (stale_hermes / "google_token.json").write_text(
        '{"synthetic_poison":"must-not-be-read"}', encoding="utf-8")
    os.environ["HOME"] = str(home)
    os.environ.pop("SOTTO_HOME", None)
    os.environ["SOTTO_TENANT_ID"] = "tenant-fixture"
    os.environ["SOTTO_DEPLOYMENT_MODE"] = "managed"
    receiver.DATA = str(root / "data")
    Path(receiver.DATA).mkdir()
    adapter = receiver._runtime_adapter()
    try:
        adapter.home_path("google_token.json")
    except ValueError as exc:
        require("explicit SOTTO_HOME" in str(exc), "missing-root rejection was not explicit")
    else:
        raise AssertionError("selected adapter accepted ambient Google home")
    require(receiver._google_home_path_or_none("google_token.json") is None,
            "receiver unexpectedly discovered ambient Google credentials")
    managed.record_bridge_consent(receiver.DATA, ["imessage"], ["imessage"], allow_send=False)
    relay = FakeRelay()
    receiver.RELAY = relay
    authority = receiver._runtime_source_authority()
    require(authority.get("complete") is True, "Bridge-only source authority did not complete")
    require(authority.get("bridge_sources") == ["imessage"], "synthetic Bridge grant was not preserved")
    require(authority.get("google_sources") == [], "missing Google root unexpectedly granted Google")
    require(relay.calls == 1, "synthetic Relay health was not queried exactly once")

print("packaged paths, imports, UIDs, and Sotto mode selection: PASS", flush=True)
print("selected adapter Bridge-only authority without SOTTO_HOME: PASS", flush=True)
