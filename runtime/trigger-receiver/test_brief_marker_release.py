"""A brief's delivered marker is a send claim: a definite refusal gives the day back, doubt keeps it."""
import os
import time

import test_receiver as harness
from test_receiver import _clock_outside_the_cron_windows, _explicit_google_credential_home  # noqa: F401

rec = harness.rec
_archive, _channel, _marker = harness._archive, harness._channel, harness._marker
_no_backoff, _outbox_rows = harness._no_backoff, harness._outbox_rows


def test_a_brief_refused_on_every_attempt_gives_its_day_back(tmp_path, monkeypatch):
    """The marker is written when the send starts, before the provider answers. A brief whose
    every attempt was definitely refused never reached anyone, so its claim must not keep the
    day reading as delivered: the terminal hook removes it and another run may send."""
    rec.DATA = str(tmp_path)
    _archive(tmp_path, "morning"); _archive(tmp_path, "evening")
    _no_backoff(monkeypatch)
    monkeypatch.setattr(rec.OUTBOX, "MAX_ATTEMPTS", 2)
    _channel(monkeypatch, (False, "gateway refused"))
    assert rec._deliver_text("the evening brief", "brief:sotto-evening-brief",
                             run_id="run-a") is False
    assert _marker(tmp_path, "evening") == "run-a"
    rec.OUTBOX.drain()   # second refusal: gave up
    rec.OUTBOX.drain()   # terminal bookkeeping
    (row,) = _outbox_rows(tmp_path)
    assert row["status"] == "failed" and row["terminal_reconciled"] is True
    assert not os.path.exists(_marker_path(tmp_path, "evening"))
    sent = _channel(monkeypatch)
    assert rec._deliver_text("a recomposed evening brief", "brief:sotto-evening-brief",
                             run_id="run-b") is True
    assert [s["body"] for s in sent] == ["a recomposed evening brief"]
    assert _marker(tmp_path, "evening") == "run-b"


def test_an_uncertain_closed_brief_keeps_its_day(tmp_path, monkeypatch):
    """A send that may have landed keeps the reservation: a second brief is worse than none."""
    rec.DATA = str(tmp_path)
    path = _marker(tmp_path, "evening", "run-a")
    day = rec.DASHBOARD._local_today()
    base = {"id": "row", "status": "failed", "day": day,
            "payload": {"label": "brief:sotto-evening-brief", "run_id": "run-a"}}
    assert rec._release_failed_brief_claim({**base, "acceptance": "unknown"}) is True
    assert rec._release_failed_brief_claim({**base, "acceptance": "rejected",
                                            "acceptance_uncertain": True}) is True
    assert rec._release_failed_brief_claim({**base, "status": "expired", "acceptance": "rejected",
                                            "acceptance_uncertain": True}) is True
    assert rec._release_failed_brief_claim(dict(base)) is True  # legacy row: no acceptance field
    assert rec._release_failed_brief_claim({**base, "acceptance": "rejected",
                                            "payload": {**base["payload"], "run_id": "run-z"}}) is True
    assert os.path.exists(path)
    assert rec._release_failed_brief_claim({**base, "acceptance": "rejected"}) is True
    assert not os.path.exists(path)
    path = _marker(tmp_path, "evening", "run-a")
    assert rec._release_failed_brief_claim({**base, "status": "expired", "acceptance": "rejected"}) is True
    assert not os.path.exists(path)


def test_a_refused_brief_that_outlives_its_validity_gives_its_day_back(tmp_path, monkeypatch):
    """A scheduled brief's `valid_until` (at most two hours) ends a refused send as expired long
    before MAX_ATTEMPTS would fail it; proof of no send, not the closing status, gives the day back."""
    rec.DATA = str(tmp_path)
    _archive(tmp_path, "morning"); _archive(tmp_path, "evening")
    _no_backoff(monkeypatch)
    _channel(monkeypatch, (False, "gateway refused"))
    assert rec._deliver_text("the evening brief", "brief:sotto-evening-brief",
                             run_id="run-a", valid_until=time.time() + 1) is False
    assert _marker(tmp_path, "evening") == "run-a"
    time.sleep(1.1)
    rec.OUTBOX.drain(); rec.OUTBOX.drain()
    (row,) = _outbox_rows(tmp_path)
    assert row["status"] == "expired" and row["acceptance"] == "rejected"
    assert row["terminal_reconciled"] is True
    assert not os.path.exists(_marker_path(tmp_path, "evening"))
    sent = _channel(monkeypatch)
    assert rec._deliver_text("a recomposed evening brief", "brief:sotto-evening-brief",
                             run_id="run-b") is True
    assert [s["body"] for s in sent] == ["a recomposed evening brief"]
    assert _marker(tmp_path, "evening") == "run-b"


def _marker_path(tmp_path, kind):
    return os.path.join(str(tmp_path), "briefs", f"{rec.DASHBOARD._local_today()}.{kind}.delivered")
