"""brief_marker.py — the atomic deliver-once gate (cron ↔ wake-push coordination)."""
import importlib.util, os

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
spec = importlib.util.spec_from_file_location("bm", os.path.join(ROOT, "_shared", "scripts", "brief_marker.py"))
bm = importlib.util.module_from_spec(spec); spec.loader.exec_module(bm)


def test_first_claim_wins_second_loses(tmp_path, monkeypatch):
    monkeypatch.delenv("SOTTO_DELIVERY_RUN_ID", raising=False)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    assert bm.claim("morning") is True      # cron (or wake-push) wins
    assert bm.claim("morning") is False     # the other path sees it's done → stops
    assert bm.claim("evening") is True      # different kind, independent


def test_check_does_not_claim(tmp_path, monkeypatch):
    monkeypatch.delenv("SOTTO_DELIVERY_RUN_ID", raising=False)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    assert os.path.exists(bm._path("morning")) is False
    # peeking never creates the flag
    assert bm.claim("morning") is True


def test_a_receiver_run_never_writes_the_marker(tmp_path, monkeypatch):
    """The hole this closes (external review, Aug 31): a receiver-spawned run that claimed here and
    then died before its output reached the outbox left the day marked delivered with NOTHING queued
    to deliver it. So under the receiver (SOTTO_DELIVERY_RUN_ID set) this call only answers — the
    durable send seam is the marker's one writer, at the moment of actual delivery."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setenv("SOTTO_DELIVERY_RUN_ID", "abc123")
    assert bm.claim("evening") is True                       # compose on — the seam will gate
    assert not os.path.exists(bm._path("evening"))           # …but NOTHING was written here
    # a marker another lane already owns still stops this run
    os.makedirs(os.path.dirname(bm._path("evening")), exist_ok=True)
    with open(bm._path("evening"), "w", encoding="utf-8") as f:
        f.write("other-run")
    assert bm.claim("evening") is False
    # …and one this run owns (the seam claimed for it) still answers "claimed"
    with open(bm._path("evening"), "w", encoding="utf-8") as f:
        f.write("abc123")
    assert bm.claim("evening") is True


def test_a_receiver_run_does_not_stamp_the_digest_window(tmp_path, monkeypatch):
    """The digest stamp belongs to the claim that DELIVERS — under the receiver that is the send
    seam's claim, not this one."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setenv("SOTTO_DELIVERY_RUN_ID", "abc123")
    stamped = []
    monkeypatch.setattr(bm, "_stamp_digest_window", lambda: stamped.append(1))
    assert bm.claim("morning") is True
    assert stamped == []
    # the local lane (no run id) still stamps — there its claim IS the delivery gate
    monkeypatch.delenv("SOTTO_DELIVERY_RUN_ID")
    assert bm.claim("morning") is True
    assert stamped == [1]


def test_a_claimer_with_no_run_id_says_so(tmp_path, monkeypatch):
    """A local-lane claimer cannot name itself, so it writes `unlabeled` — which reads as ANOTHER
    LANE to the seam, the safe answer when identity is unknown."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.delenv("SOTTO_DELIVERY_RUN_ID", raising=False)
    assert bm.claim("morning") is True
    with open(bm._path("morning"), encoding="utf-8") as f:
        assert f.read() == bm.UNLABELED


def test_claim_fails_closed_when_atomic_create_cannot_be_decided(tmp_path, monkeypatch):
    monkeypatch.delenv("SOTTO_DELIVERY_RUN_ID", raising=False)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setattr(bm.os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
    assert bm.claim("morning") is False
