import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone

import pytest


HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("calendar_receiver", os.path.join(HERE, "receiver.py"))
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)


NOW = datetime(2026, 8, 17, 17, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def synthetic_calendar_authority(monkeypatch):
    # These queue/checkpoint tests install already-authorized in-memory snapshots.
    # Source-revision changes are exercised in test_source_calendar.py.
    monkeypatch.setitem(rec.CALCACHE.HOOKS, 'source_revision', lambda: 'a' * 64)
    monkeypatch.setitem(rec.CALCACHE._CAL_CACHE, 'source_revision', 'a' * 64)


def _calendar_env(tmp_path, monkeypatch):
    cc = rec.CALCACHE
    monkeypatch.setattr(rec, "DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    coverage = {"since": NOW.isoformat(), "until": (NOW + timedelta(days=3)).isoformat()}
    base = {"id": "base", "summary": "Coffee", "start": "2026-08-17T18:00:00+00:00",
            "end": "2026-08-17T18:30:00+00:00",
            "attendees": [{"email": "other@example.com", "displayName": "Other"}]}
    cc._LAST_RAW.update(events=[base], valid=True, coverage=coverage, observed_at=NOW.isoformat())
    cc._CHANGE_BASELINE.update(events=None, source=None, account="", loaded=False,
                               acknowledged=set())
    assert cc.change_tick(NOW) == 0
    return cc, base


def test_calendar_refresh_batches_once_and_replays_only_unacknowledged(tmp_path, monkeypatch):
    cc, base = _calendar_env(tmp_path, monkeypatch)
    additions = [dict(base, id=f"new-{i}", summary=f"New {i}",
                      start=f"2026-08-17T18:{10 + i}:00+00:00") for i in range(3)]
    cc._LAST_RAW["events"] = [base, *additions]
    calls = []

    def partial(events):
        calls.append(events)
        return {events[0]["rowid"], events[2]["rowid"]}

    monkeypatch.setitem(cc.HOOKS, "calendar_change_batch", partial)
    assert cc.change_tick(NOW) == 2
    assert len(calls) == 1 and len(calls[0]) == 3
    state = json.load(open(cc.change_state_path(), encoding="utf-8"))
    assert len(state["acknowledged"]) == 2

    # Simulate a receiver restart: the durable partial acknowledgement admits only the failed row.
    cc._CHANGE_BASELINE.update(events=None, source=None, account="", loaded=False,
                               acknowledged=set())
    calls.clear()
    monkeypatch.setitem(cc.HOOKS, "calendar_change_batch",
                        lambda events: (calls.append(events), {events[0]["rowid"]})[1])
    assert cc.change_tick(NOW) == 1
    assert len(calls) == 1 and len(calls[0]) == 1
    assert json.load(open(cc.change_state_path(), encoding="utf-8"))["acknowledged"] == []


def test_calendar_change_retry_keeps_identity_but_later_repeat_is_fresh(tmp_path, monkeypatch):
    cc, base = _calendar_env(tmp_path, monkeypatch)
    invited = dict(base, id="repeat", summary="Retry me",
                   start="2026-08-17T18:20:00+00:00")
    cc._LAST_RAW.update(events=[base, invited], observed_at="2026-08-17T17:01:00+00:00")
    attempts = []
    monkeypatch.setitem(cc.HOOKS, "calendar_change_batch",
                        lambda events: attempts.extend(events) or set())
    assert cc.change_tick(NOW) == 0
    first = attempts[-1]

    # A restart and later gather must retain the timestamp hashed into the durable item key.
    cc._CHANGE_BASELINE.update(events=None, source=None, account="", loaded=False,
                               acknowledged=set(), detected_at={})
    cc._LAST_RAW["observed_at"] = "2026-08-17T17:02:00+00:00"
    assert cc.change_tick(NOW) == 0
    assert attempts[-1]["timestamp"] == first["timestamp"]

    monkeypatch.setitem(cc.HOOKS, "calendar_change_batch",
                        lambda events: {event["rowid"] for event in events})
    assert cc.change_tick(NOW) == 1

    # Once the baseline advances, a later occurrence of the same candidate key is a new change.
    cc._LAST_RAW.update(events=[base], observed_at="2026-08-17T17:03:00+00:00")
    assert cc.change_tick(NOW) == 1
    cc._LAST_RAW.update(events=[base, invited], observed_at="2026-08-17T17:04:00+00:00")
    later = []
    monkeypatch.setitem(cc.HOOKS, "calendar_change_batch",
                        lambda events: later.extend(events) or set())
    assert cc.change_tick(NOW) == 0
    assert later[0]["timestamp"] != first["timestamp"]


def test_invalidated_baseline_writes_the_current_state_version(tmp_path, monkeypatch):
    """`_invalidate_change_baseline` must write the SAME format number as `_write_change_state` —
    one on-disk version, not a stale one the reader merely tolerates."""
    cc, base = _calendar_env(tmp_path, monkeypatch)
    monkeypatch.delenv("SOTTO_USER_EMAIL")
    cc._CHANGE_BASELINE.update(events=None, source=None, account="", loaded=False,
                               acknowledged=set(), detected_at={})
    assert cc.change_tick(NOW) == 0
    state = json.load(open(cc.change_state_path(), encoding="utf-8"))
    assert state["source"]["status"] == "identity_unknown"
    assert state["version"] == 2


@pytest.mark.parametrize("receipt_verdict", ("queue", "drop"))
def test_calendar_change_receipt_closes_post_triage_checkpoint_crash_gap(
        tmp_path, monkeypatch, receipt_verdict):
    """Real triage has already committed queue/drop by the time change_tick checkpoints its ack.
    A crash in that gap must reconcile the stable receipt instead of writing it a second time."""
    cc, base = _calendar_env(tmp_path, monkeypatch)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "UTC")
    monkeypatch.setenv("SOTTO_QUIET_START", "0")
    monkeypatch.setenv("SOTTO_QUIET_END", "24")
    monkeypatch.setitem(cc.HOOKS, "event_handled", rec._synthetic_event_handled)

    if receipt_verdict == "queue":
        changed = dict(base, id="queued-change", summary="Queued change",
                       start="2026-08-17T18:20:00+00:00")
        cc._LAST_RAW.update(events=[base, changed], observed_at="2026-08-17T17:01:00+00:00")
    else:
        old = dict(base, organizer={"email": "me@example.com"}, attendees=[
            {"email": "me@example.com", "displayName": "Me", "responseStatus": "accepted"},
            {"email": "other@example.com", "displayName": "Other",
             "responseStatus": "accepted"},
        ])
        cc._write_change_state([old])
        cc._CHANGE_BASELINE.update(events=[old], source=cc._change_observation(),
                                   account="me@example.com", acknowledged=set(), detected_at={})
        declined = {**old, "attendees": [old["attendees"][0],
                                          {**old["attendees"][1],
                                           "responseStatus": "declined"}]}
        cc._LAST_RAW.update(events=[declined], observed_at="2026-08-17T17:01:00+00:00")
        (tmp_path / "preferences.json").write_text(
            json.dumps({"explicit": {"mute_people": ["Other"]}}), encoding="utf-8")

    triage_calls = []

    def real_triage_batch(events):
        triage_calls.append(events)
        verdict = rec.run_triage(events, False)
        assert verdict["verdict"] == receipt_verdict
        return {event["rowid"] for event in events}

    monkeypatch.setitem(cc.HOOKS, "calendar_change_batch", real_triage_batch)
    real_write = rec.CONNECTORS.write_json

    def fail_acknowledgement(path, obj, *args, **kwargs):
        if path == cc.change_state_path() and obj.get("acknowledged"):
            raise OSError("crash after triage receipt")
        return real_write(path, obj, *args, **kwargs)

    monkeypatch.setitem(cc.HOOKS, "write_json", fail_acknowledgement)
    with pytest.raises(OSError, match="crash after triage receipt"):
        cc.change_tick(NOW)

    def ledger_rows(name):
        path = tmp_path / "events" / name
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    before = {name: len(ledger_rows(name)) for name in ("queue.jsonl", "surfaced.jsonl")}
    assert before["surfaced.jsonl"] == 1
    assert before["queue.jsonl"] == (1 if receipt_verdict == "queue" else 0)

    # A receiver restart reloads the pre-dispatch timestamp claim. Receipt recovery acknowledges
    # it without counting a new dispatch or calling triage again.
    monkeypatch.setitem(cc.HOOKS, "write_json", real_write)
    cc._CHANGE_BASELINE.update(events=None, source=None, account="", loaded=False,
                               acknowledged=set(), detected_at={})
    assert cc.change_tick(NOW) == 0
    assert len(triage_calls) == 1
    assert {name: len(ledger_rows(name))
            for name in ("queue.jsonl", "surfaced.jsonl")} == before


@pytest.mark.parametrize("lookup_failure", (None, OSError("receipt store unavailable")))
def test_calendar_change_uncertain_ownership_pauses_whole_batch(
        tmp_path, monkeypatch, lookup_failure):
    cc, base = _calendar_env(tmp_path, monkeypatch)
    additions = [dict(base, id=f"uncertain-{i}", summary=f"Uncertain {i}",
                      start=f"2026-08-17T18:{20 + i}:00+00:00") for i in range(2)]
    cc._LAST_RAW.update(events=[base, *additions], observed_at="2026-08-17T17:01:00+00:00")
    lookups = 0

    def uncertain_on_second(_event):
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            return False
        if lookup_failure is not None:
            raise lookup_failure
        return None

    monkeypatch.setitem(cc.HOOKS, "event_handled", uncertain_on_second)
    dispatched = []
    monkeypatch.setitem(cc.HOOKS, "calendar_change_batch",
                        lambda events: dispatched.extend(events) or set())

    assert cc.change_tick(NOW) == 0
    assert dispatched == []
    state = json.load(open(cc.change_state_path(), encoding="utf-8"))
    assert state["acknowledged"] == []
    assert set(state["detected_at"]) == {f"uncertain-{i}:invited:2026-08-17T18:{20 + i}:00+00:00"
                                         for i in range(2)}


def _meeting(summary, end):
    return {"summary": summary, "start": "2026-08-06T09:00:00+00:00", "end": end,
            "attendees": [{"name": "Me", "email": "me@example.com"},
                          {"name": "Other", "email": "other@example.com"}]}


def test_tap_checkpoint_failure_never_dispatches_an_unowned_slot(tmp_path, monkeypatch):
    cc = rec.CALCACHE
    monkeypatch.setattr(rec, "DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    monkeypatch.setenv("SOTTO_TAP_MAX_PER_DAY", "2")
    monkeypatch.setitem(cc.HOOKS, "local_today", lambda: "2026-08-06")
    events = [_meeting("One", "2026-08-06T10:00:00+00:00"),
              _meeting("Two", "2026-08-06T10:01:00+00:00")]
    monkeypatch.setattr(cc, "_CAL_CACHE", {"ts": rec.time.time(), "source_revision": "a" * 64, "value": {
        "events": events, "generated_at": "2026-08-06T10:05:00Z", "status": "ok"}})
    sent = []
    monkeypatch.setitem(cc.HOOKS, "meeting_tap", lambda event: (sent.append(event), True)[1])
    monkeypatch.setitem(cc.HOOKS, "write_json", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk")))
    now = datetime(2026, 8, 6, 10, 7, tzinfo=timezone.utc)
    assert cc.tap_tick(now) == 0
    assert sent == []


def test_tap_pending_checkpoint_survives_restart_and_holds_the_cap(tmp_path, monkeypatch):
    cc = rec.CALCACHE
    monkeypatch.setattr(rec, "DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    monkeypatch.setenv("SOTTO_TAP_MAX_PER_DAY", "1")
    monkeypatch.setitem(cc.HOOKS, "local_today", lambda: "2026-08-06")
    first = _meeting("One", "2026-08-06T10:00:00+00:00")
    second = _meeting("Two", "2026-08-06T10:01:00+00:00")
    monkeypatch.setattr(cc, "_CAL_CACHE", {"ts": rec.time.time(), "source_revision": "a" * 64, "value": {
        "events": [first, second], "generated_at": "2026-08-06T10:05:00Z", "status": "ok"}})
    real_write = rec.CONNECTORS.write_json
    writes = 0

    def fail_completion(path, obj, *args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk")
        return real_write(path, obj, *args, **kwargs)

    monkeypatch.setitem(cc.HOOKS, "write_json", fail_completion)
    sent = []
    monkeypatch.setitem(cc.HOOKS, "meeting_tap", lambda event: (sent.append(event), True)[1])
    now = datetime(2026, 8, 6, 10, 7, tzinfo=timezone.utc)
    assert cc.tap_tick(now) == 0
    assert [event["summary"] for event in sent] == ["One"]
    state = json.load(open(cc.tap_state_path(), encoding="utf-8"))
    assert state["pending"] and state["fired"] == []

    # A fresh process would load the same pending reservation. Retrying it may be deduped by
    # triage's durable ownership; it is finalized before the distinct second meeting can enter.
    monkeypatch.setitem(cc.HOOKS, "write_json", real_write)
    assert cc.tap_tick(now) == 1
    assert [event["summary"] for event in sent] == ["One", "One"]
    assert cc.tap_tick(now) == 0
    state = json.load(open(cc.tap_state_path(), encoding="utf-8"))
    assert len(state["fired"]) == 1 and state["pending"] == []


def test_tap_pending_reconciles_owned_work_without_second_triage(tmp_path, monkeypatch):
    cc = rec.CALCACHE
    monkeypatch.setattr(rec, "DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    monkeypatch.setenv("SOTTO_TAP_MAX_PER_DAY", "1")
    monkeypatch.setitem(cc.HOOKS, "local_today", lambda: "2026-08-06")
    meeting = _meeting("Owned", "2026-08-06T10:00:00+00:00")
    monkeypatch.setattr(cc, "_CAL_CACHE", {"ts": rec.time.time(), "source_revision": "a" * 64, "value": {
        "events": [meeting], "generated_at": "2026-08-06T10:05:00Z", "status": "ok"}})
    candidate = cc.ended_meetings([meeting], datetime(2026, 8, 6, 10, 7,
                                                       tzinfo=timezone.utc), "2026-08-06")[0]
    candidate["calendar_observed_at"] = "2026-08-06T10:05:00Z"
    event = cc.tap_event(candidate)
    rec.CONNECTORS.write_json(cc.tap_state_path(), {
        "version": 2, "date": "2026-08-06", "fired": [], "pending": [candidate["key"]],
    })
    item_key = rec.WORK_QUEUE.event_item_key(event)
    rec.WORK_QUEUE.enqueue(str(tmp_path), "event", {"bundle": {"events": [event]}},
                           item_keys=[item_key])
    monkeypatch.setitem(cc.HOOKS, "write_json", rec.CONNECTORS.write_json)
    triaged = []
    monkeypatch.setitem(cc.HOOKS, "meeting_tap", lambda row: triaged.append(row) or True)

    assert cc.tap_tick(datetime(2026, 8, 6, 10, 7, tzinfo=timezone.utc)) == 0
    assert triaged == []
    state = json.load(open(cc.tap_state_path(), encoding="utf-8"))
    assert state["fired"] == [candidate["key"]] and state["pending"] == []


def test_tap_false_after_receipt_keeps_slot_and_does_not_replay(tmp_path, monkeypatch):
    cc = rec.CALCACHE
    monkeypatch.setattr(rec, "DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    monkeypatch.setitem(cc.HOOKS, "local_today", lambda: "2026-08-06")
    meeting = _meeting("Queued before timeout", "2026-08-06T10:00:00+00:00")
    monkeypatch.setattr(cc, "_CAL_CACHE", {"ts": rec.time.time(), "source_revision": "a" * 64, "value": {
        "events": [meeting], "generated_at": "2026-08-06T10:05:00Z", "status": "ok"}})
    triaged = []
    monkeypatch.setitem(cc.HOOKS, "meeting_tap", lambda event: triaged.append(event) or False)
    monkeypatch.setitem(cc.HOOKS, "event_handled", lambda event: True)
    now = datetime(2026, 8, 6, 10, 7, tzinfo=timezone.utc)

    assert cc.tap_tick(now) == 0
    assert len(triaged) == 1
    state = json.load(open(cc.tap_state_path(), encoding="utf-8"))
    assert len(state["fired"]) == 1 and state["pending"] == []
    assert cc.tap_tick(now) == 0 and len(triaged) == 1


def test_tap_ownership_lookup_failure_retains_pending_and_pauses(tmp_path, monkeypatch):
    cc = rec.CALCACHE
    monkeypatch.setattr(rec, "DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    monkeypatch.setitem(cc.HOOKS, "local_today", lambda: "2026-08-06")
    meeting = _meeting("Unknown", "2026-08-06T10:00:00+00:00")
    monkeypatch.setattr(cc, "_CAL_CACHE", {"ts": rec.time.time(), "source_revision": "a" * 64, "value": {
        "events": [meeting], "generated_at": "2026-08-06T10:05:00Z", "status": "ok"}})
    candidate = cc.ended_meetings([meeting], datetime(2026, 8, 6, 10, 7,
                                                       tzinfo=timezone.utc), "2026-08-06")[0]
    rec.CONNECTORS.write_json(cc.tap_state_path(), {
        "version": 2, "date": "2026-08-06", "fired": [], "pending": [candidate["key"]],
    })
    monkeypatch.setitem(cc.HOOKS, "event_handled",
                        lambda _row: (_ for _ in ()).throw(OSError("database unavailable")))
    triaged = []
    monkeypatch.setitem(cc.HOOKS, "meeting_tap", lambda row: triaged.append(row) or True)

    assert cc.tap_tick(datetime(2026, 8, 6, 10, 7, tzinfo=timezone.utc)) == 0
    assert triaged == []
    state = json.load(open(cc.tap_state_path(), encoding="utf-8"))
    assert state["fired"] == [] and state["pending"] == [candidate["key"]]


def test_tap_pending_beyond_lookback_conservatively_holds_cap_until_rollover(tmp_path, monkeypatch):
    cc = rec.CALCACHE
    monkeypatch.setattr(rec, "DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    monkeypatch.setenv("SOTTO_TAP_MAX_PER_DAY", "1")
    monkeypatch.setitem(cc.HOOKS, "local_today", lambda: "2026-08-06")
    old = _meeting("Old uncertain", "2026-08-06T08:00:00+00:00")
    new = _meeting("New", "2026-08-06T10:00:00+00:00")
    old_key = cc._tap_key(old)
    rec.CONNECTORS.write_json(cc.tap_state_path(), {
        "version": 2, "date": "2026-08-06", "fired": [], "pending": [old_key],
    })
    monkeypatch.setattr(cc, "_CAL_CACHE", {"ts": rec.time.time(), "source_revision": "a" * 64, "value": {
        "events": [old, new], "generated_at": "2026-08-06T10:05:00Z", "status": "ok"}})
    triaged = []
    monkeypatch.setitem(cc.HOOKS, "meeting_tap", lambda row: triaged.append(row) or True)

    assert cc.tap_tick(datetime(2026, 8, 6, 10, 7, tzinfo=timezone.utc)) == 0
    assert triaged == []
    assert json.load(open(cc.tap_state_path(), encoding="utf-8"))["pending"] == [old_key]


@pytest.mark.parametrize("verdict", ["queue", "drop"])
def test_tap_pending_reconciles_nonagent_receipt_without_second_triage(
        tmp_path, monkeypatch, verdict):
    cc = rec.CALCACHE
    monkeypatch.setattr(rec, "DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    monkeypatch.setitem(cc.HOOKS, "local_today", lambda: "2026-08-06")
    meeting = _meeting(verdict.title(), "2026-08-06T10:00:00+00:00")
    monkeypatch.setattr(cc, "_CAL_CACHE", {"ts": rec.time.time(), "source_revision": "a" * 64, "value": {
        "events": [meeting], "generated_at": "2026-08-06T10:05:00Z", "status": "ok"}})
    now = datetime(2026, 8, 6, 10, 7, tzinfo=timezone.utc)
    candidate = cc.ended_meetings([meeting], now, "2026-08-06")[0]
    candidate["calendar_observed_at"] = "2026-08-06T10:05:00Z"
    event = cc.tap_event(candidate)
    rec.CONNECTORS.write_json(cc.tap_state_path(), {
        "version": 2, "date": "2026-08-06", "fired": [], "pending": [candidate["key"]],
    })
    os.makedirs(os.path.join(str(tmp_path), "events"), exist_ok=True)
    with open(os.path.join(str(tmp_path), "events", "surfaced.jsonl"), "w",
              encoding="utf-8") as stream:
        stream.write(json.dumps({"verdict": verdict,
                                 "item_key": rec.WORK_QUEUE.event_item_key(event)}) + "\n")
    triaged = []
    monkeypatch.setitem(cc.HOOKS, "meeting_tap", lambda row: triaged.append(row) or True)

    assert cc.tap_tick(now) == 0
    assert triaged == []
    state = json.load(open(cc.tap_state_path(), encoding="utf-8"))
    assert state["fired"] == [candidate["key"]] and state["pending"] == []


def test_corrupt_current_day_tap_checkpoint_fails_closed(tmp_path, monkeypatch):
    cc = rec.CALCACHE
    monkeypatch.setattr(rec, "DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    monkeypatch.setitem(cc.HOOKS, "local_today", lambda: "2026-08-06")
    os.makedirs(os.path.dirname(cc.tap_state_path()), exist_ok=True)
    with open(cc.tap_state_path(), "w", encoding="utf-8") as stream:
        stream.write("not json")
    monkeypatch.setattr(cc, "_CAL_CACHE", {"ts": rec.time.time(), "source_revision": "a" * 64, "value": {
        "events": [_meeting("One", "2026-08-06T10:00:00+00:00")],
        "generated_at": "2026-08-06T10:05:00Z", "status": "ok"}})
    sent = []
    monkeypatch.setitem(cc.HOOKS, "meeting_tap", lambda event: sent.append(event) or True)
    assert cc.tap_tick(datetime(2026, 8, 6, 10, 7, tzinfo=timezone.utc)) == 0
    assert sent == []


TAP_AT = datetime(2026, 8, 6, 10, 7, tzinfo=timezone.utc)


def _tap_fixture(tmp_path, monkeypatch, events, state=None):
    cc = rec.CALCACHE
    monkeypatch.setattr(rec, "DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@example.com")
    monkeypatch.setitem(cc.HOOKS, "local_today", lambda: "2026-08-06")
    monkeypatch.setitem(cc.HOOKS, "write_json", rec.CONNECTORS.write_json)
    monkeypatch.setattr(cc, "_CAL_CACHE", {"ts": rec.time.time(), "source_revision": "a" * 64, "value": {
        "events": events, "generated_at": "2026-08-06T10:05:00Z", "status": "ok"}})
    if state is not None:
        rec.CONNECTORS.write_json(cc.tap_state_path(), {"version": 2, "date": "2026-08-06", **state})
    sent = []
    monkeypatch.setitem(cc.HOOKS, "meeting_tap", lambda event: sent.append(event) or True)
    return cc, sent


def _scoped(meeting, n, uid=""):
    """A cache row as R14 writes it: calendar-scoped `_id`, plus the invite's iCalUID when known."""
    return {**meeting, "_id": "gcal_" + str(n) * 64, **({"_uid": uid} if uid else {})}


def _tap_state(cc):
    return json.load(open(cc.tap_state_path(), encoding="utf-8"))


@pytest.mark.parametrize("uids", [("one@google.com", "two@google.com"), ("", "")])
def test_tap_distinct_meetings_sharing_slot_and_title_each_fire(tmp_path, monkeypatch, uids):
    base = _meeting("Intro call", "2026-08-06T10:00:00+00:00")
    one = _scoped(base, 1, uids[0])
    two = _scoped({**base, "attendees": [{"name": "Me", "email": "me@example.com"},
                                         {"name": "Ana", "email": "ana@elsewhere.com"}]}, 2, uids[1])
    cc, sent = _tap_fixture(tmp_path, monkeypatch, [one, two])

    assert cc.tap_tick(TAP_AT) == 2
    assert [event["attendees"][0]["name"] for event in sent] == ["Other", "Ana"]
    assert len({event["rowid"] for event in sent}) == 2
    fired = _tap_state(cc)["fired"]
    assert len(fired) == 2 and cc._legacy_tap_key(base) not in fired
    assert cc.tap_tick(TAP_AT) == 0 and len(sent) == 2


def test_tap_same_invite_on_two_calendars_fires_once(tmp_path, monkeypatch):
    cc = rec.CALCACHE
    raw = {"id": "gcal_" + "1" * 64, "iCalUID": "invite@google.com", "summary": "Intro call",
           "start": "2026-08-06T09:00:00+00:00", "end": "2026-08-06T10:00:00+00:00",
           "attendees": [{"email": "me@example.com"}, {"email": "other@example.com"}]}
    primary, shared = cc._norm_cal_event(raw), cc._norm_cal_event({**raw, "id": "gcal_" + "2" * 64})
    assert primary["_id"] != shared["_id"] and primary["_uid"] == shared["_uid"] == "invite@google.com"
    cc, sent = _tap_fixture(tmp_path, monkeypatch, [primary, shared])

    assert len(cc.ended_meetings([primary, shared], TAP_AT, "2026-08-06")) == 1
    assert cc.tap_tick(TAP_AT) == 1
    assert len(sent) == 1 and len(_tap_state(cc)["fired"]) == 1
    # The record names the meeting, not the copy that happened to speak for it.
    cc._CAL_CACHE["value"]["events"] = [shared]
    assert cc.tap_tick(TAP_AT) == 0 and len(sent) == 1


def test_tap_series_instance_and_moved_meeting_stay_separate_ends(tmp_path, monkeypatch):
    first = _scoped(_meeting("Weekly", "2026-08-06T09:40:00+00:00"), 1, "series@google.com")
    second = _scoped(_meeting("Weekly", "2026-08-06T10:00:00+00:00"), 2, "series@google.com")
    cc, sent = _tap_fixture(tmp_path, monkeypatch, [first, second])

    assert cc.tap_tick(TAP_AT) == 2
    cc._CAL_CACHE["value"]["events"] = [first, {**second, "end": "2026-08-06T10:01:00+00:00"}]
    assert cc.tap_tick(TAP_AT) == 1 and len(sent) == 3


def test_tap_fired_under_the_legacy_key_does_not_fire_again_after_upgrade(tmp_path, monkeypatch):
    meeting = _meeting("Intro call", "2026-08-06T10:00:00+00:00")
    legacy = "2026-08-06T09:00:00+00:00|2026-08-06T10:00:00+00:00|Intro call"
    cc, sent = _tap_fixture(tmp_path, monkeypatch, [_scoped(meeting, 1, "invite@google.com")],
                            {"fired": [legacy], "pending": []})

    assert cc._legacy_tap_key(meeting) == legacy
    assert cc.tap_tick(TAP_AT) == 0 and sent == []
    assert _tap_state(cc) == {"version": 2, "date": "2026-08-06", "fired": [legacy], "pending": []}


def test_tap_pending_under_the_legacy_key_keeps_its_proof_after_upgrade(tmp_path, monkeypatch):
    meeting = _meeting("Intro call", "2026-08-06T10:00:00+00:00")
    legacy = "2026-08-06T09:00:00+00:00|2026-08-06T10:00:00+00:00|Intro call"
    cc, sent = _tap_fixture(tmp_path, monkeypatch, [_scoped(meeting, 1, "invite@google.com")],
                            {"fired": [], "pending": [legacy]})
    looked_up = []
    monkeypatch.setitem(cc.HOOKS, "event_handled", lambda event: looked_up.append(event["rowid"]))

    # Unknown effect: the hold stays exactly as recorded and nothing is sent under a new key.
    assert cc.tap_tick(TAP_AT) == 0 and sent == []
    assert looked_up == [legacy]
    assert _tap_state(cc)["pending"] == [legacy] and _tap_state(cc)["fired"] == []

    # The pre-upgrade process accepted the work under the old rowid; that receipt still settles it.
    old_event = cc.tap_event({**cc.ended_meetings([meeting], TAP_AT, "2026-08-06")[0], "key": legacy})
    rec.WORK_QUEUE.enqueue(str(tmp_path), "event", {"bundle": {"events": [old_event]}},
                           item_keys=[rec.WORK_QUEUE.event_item_key(old_event)])
    monkeypatch.setitem(cc.HOOKS, "event_handled", rec._synthetic_event_handled)
    assert cc.tap_tick(TAP_AT) == 0 and sent == []
    assert _tap_state(cc)["fired"] == [legacy] and _tap_state(cc)["pending"] == []


def test_tap_row_without_an_id_still_fires_once_under_the_legacy_key(tmp_path, monkeypatch):
    meeting = _meeting("Intro call", "2026-08-06T10:00:00+00:00")
    cc, sent = _tap_fixture(tmp_path, monkeypatch, [meeting])

    assert cc.tap_tick(TAP_AT) == 1 and "calendar_event_id" not in sent[0]
    assert _tap_state(cc)["fired"] == ["2026-08-06T09:00:00+00:00|2026-08-06T10:00:00+00:00|Intro call"]
    assert cc.tap_tick(TAP_AT) == 0 and len(sent) == 1


def test_uid_tap_keeps_pre_uid_scoped_pending_identity(tmp_path, monkeypatch):
    meeting = _scoped(_meeting('Intro', '2026-08-06T10:00:00+00:00'), 1, 'invite')
    old = f"id:{meeting['_id']}|{meeting['start']}|{meeting['end']}"
    cc, sent = _tap_fixture(tmp_path, monkeypatch, [meeting], state={'fired': [], 'pending': [old]})
    inspected = []
    monkeypatch.setitem(cc.HOOKS, 'event_handled', lambda row: inspected.append(row['rowid']) or None)
    assert cc.tap_tick(TAP_AT) == 0 and sent == [] and inspected == [old]
    assert _tap_state(cc)['pending'] == [old]
    monkeypatch.setitem(cc.HOOKS, 'event_handled', lambda row: True)
    assert cc.tap_tick(TAP_AT) == 0 and sent == []
    assert _tap_state(cc)['fired'] == [old] and _tap_state(cc)['pending'] == []


@pytest.mark.parametrize('changes, expected', [
    ({'busy': False}, 0),
    ({'transparency': 'transparent'}, 0),
    ({'transparency': 'transparent', 'attendees': [{'self': True, 'responseStatus': 'accepted', 'email': 'me@example.com'}, {'email': 'other@example.com'}]}, 1),
    ({'busy': False, 'attendees': [{'self': True, 'responseStatus': 'tentative', 'email': 'me@example.com'}, {'email': 'other@example.com'}]}, 1),
    ({'busy': False, 'organizer': {'self': True}, 'attendees': [{'self': True, 'responseStatus': 'accepted', 'email': 'me@example.com'}, {'email': 'other@example.com'}]}, 0),
    ({'busy': False, 'attendees': [{'self': True, 'responseStatus': 'needsAction', 'email': 'me@example.com'}, {'email': 'other@example.com'}]}, 0),
    ({'busy': False, 'status': 'cancelled'}, 0),
    ({'busy': False, 'my_response': 'declined'}, 0),
    ({'busy': False, 'calendar_id': 'colleague@example.com', 'attendees': [
        {'email': 'colleague@example.com', 'self': True}, {'email': 'other@example.com'}]}, 0),
    ({'busy': False, 'eventType': 'workingLocation'}, 0),
])
@pytest.mark.parametrize('normalized', [False, True])
def test_free_meetings_use_attendance_for_followup_not_availability(tmp_path, monkeypatch, changes, expected, normalized):
    meeting = {**_meeting('Intro call', '2026-08-06T10:00:00+00:00'),
               'calendar_id': 'primary', **changes}
    cc, sent = _tap_fixture(tmp_path, monkeypatch, [meeting])
    if normalized:
        meeting = cc._norm_cal_event(meeting)
        cc._CAL_CACHE['value']['events'] = [meeting]
    assert not cc.blocks_time(meeting, 'me@example.com')
    assert cc.tap_tick(TAP_AT) == expected
    assert len(sent) == expected
    assert cc.tap_tick(TAP_AT) == 0
