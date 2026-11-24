from http import HTTPStatus
"""Bridge upload acknowledgments follow receiver event acceptance."""
import threading

import pytest
from test_proactive_procedure import rec as rec  # noqa: PLC0414 — fixture export


@pytest.mark.parametrize("batch", [[61], [62, 61]])
def test_bridge_retry_cannot_ack_an_event_still_in_flight(rec, monkeypatch, batch):
    def event(rowid):
        return {"source": "imessage", "rowid": rowid}

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def triage(events, catchup):
        calls.append([e["rowid"] for e in events])
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            raise ValueError("fixture triage failure")
        return {"verdict": "queue", "reason": "ok", "bundle": {}}

    monkeypatch.setattr(rec, "run_triage", triage)
    first = []
    thread = threading.Thread(target=lambda: first.append(rec.handle_events({"events": [event(61)]})))
    thread.start()
    try:
        assert entered.wait(5)
        overlapping = rec.handle_events({"events": [event(rowid) for rowid in batch]})
    finally:
        release.set()
        thread.join(5)

    assert first[0][0] == HTTPStatus.INTERNAL_SERVER_ERROR
    assert overlapping[0] == HTTPStatus.SERVICE_UNAVAILABLE
    assert calls == [[61]]
    assert rec.handle_events({"events": [event(rowid) for rowid in batch]})[0] == HTTPStatus.OK
    assert calls == [[61], batch]
