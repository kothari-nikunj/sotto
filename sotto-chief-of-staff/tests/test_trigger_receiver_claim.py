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
    real_dump = json.dump
    calls = {"n": 0}

    def flaky_dump(obj, fp, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return real_dump(obj, fp, **kw)

    monkeypatch.setattr(rec.json, "dump", flaky_dump)
    code, r = rec.handle_trigger({"type": "morning_ready", "date": "2026-06-23", "local_data": {}})
    assert code == 500 and "enqueue failed" in r["error"]
    assert spawned == []                                             # skill never enqueued
    assert not os.path.exists(rec.delivered_flag("2026-06-23", "morning"))   # claim released
    # A later trigger (disk recovered) must go through immediately — not wait out CLAIM_STALE_SECS.
    code2, r2 = rec.handle_trigger({"type": "morning_ready", "date": "2026-06-23", "local_data": {}})
    assert code2 == 202 and r2["status"] == "enqueued"
    assert len(spawned) == 1
