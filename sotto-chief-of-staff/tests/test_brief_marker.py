"""brief_marker.py — the atomic deliver-once gate (cron ↔ wake-push coordination)."""
import importlib.util, os, sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))
spec = importlib.util.spec_from_file_location("bm", os.path.join(ROOT, "_shared", "scripts", "brief_marker.py"))
bm = importlib.util.module_from_spec(spec); spec.loader.exec_module(bm)


def test_first_claim_wins_second_loses(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    assert bm.claim("morning") is True      # cron (or wake-push) wins
    assert bm.claim("morning") is False     # the other path sees it's done → stops
    assert bm.claim("evening") is True      # different kind, independent


def test_check_does_not_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    assert os.path.exists(bm._path("morning")) is False
    # peeking never creates the flag
    assert bm.claim("morning") is True
