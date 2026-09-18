"""trigger-receiver: a failed payload write must release the trigger claim.

The payload json.dump sat OUTSIDE the try that releases the .claim flag — an OSError (full or
read-only volume) left the claim held, silently blocking the day's brief for CLAIM_STALE_SECS.
"""
import importlib.util
import json
import os

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "receiver_claim", os.path.join(HERE, "..", "..", "runtime", "trigger-receiver", "receiver.py"))
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)


def test_payload_write_failure_releases_claim_and_retry_succeeds(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    spawned = []
    monkeypatch.setattr(rec, "run_skill", lambda *a: spawned.append(a))
    # This test exercises the normal trigger enqueue path regardless of the wall-clock cron slot.
    monkeypatch.setattr(rec, "_in_brief_cron_window", lambda _skill: False)
    real_dump = json.dump
    payload_path = os.path.join(
        str(tmp_path), "briefs", "2026-06-23.morning_ready.payload.json")
    failed_payload_write = {"value": False}

    def flaky_dump(obj, fp, **kw):
        # receiver.py has daemon workers that can write unrelated JSON during the test suite. Fail
        # the brief payload write itself rather than whichever process-wide json.dump runs first.
        if (not failed_payload_write["value"]
                and os.path.abspath(fp.name) == os.path.abspath(payload_path)):
            failed_payload_write["value"] = True
            raise OSError("disk full")
        return real_dump(obj, fp, **kw)

    monkeypatch.setattr(rec.json, "dump", flaky_dump)
    code, r = rec.handle_trigger({"type": "morning_ready", "date": "2026-06-23", "local_data": {}})
    assert failed_payload_write["value"], (
        f"the injected failure must hit the brief payload write; got {code}: {r}")
    assert code == 500 and "enqueue failed" in r["error"]
    assert spawned == []                                             # skill never enqueued
    assert not os.path.exists(rec.delivered_flag("2026-06-23", "morning"))   # claim released
    # A later trigger (disk recovered) must go through immediately — not wait out CLAIM_STALE_SECS.
    code2, r2 = rec.handle_trigger({"type": "morning_ready", "date": "2026-06-23", "local_data": {}})
    assert code2 == 202 and r2["status"] == "enqueued"
    assert len(spawned) == 1
