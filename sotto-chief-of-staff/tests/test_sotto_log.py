"""Concurrency and permission contracts for the shared diagnostic log writer."""
import concurrent.futures
import fcntl
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest

import jsonstore


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "sotto_log_test", HERE.parent / "_shared" / "lib" / "sotto_log.py"
)
sotto_log = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sotto_log)


def test_concurrent_bounded_appends_keep_every_complete_record(tmp_path):
    path = tmp_path / "logs" / "compose_brief.log"
    records = [f"child-{i}-" + str(i) * 2_000 for i in range(24)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(sotto_log.bounded_append, str(path), record, 8_000, 100)
                   for record in records]
        for future in futures:
            future.result(timeout=10)

    lines = path.read_text().splitlines()
    assert sorted(lines) == sorted(records)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(os.stat(str(path) + ".lock").st_mode) == 0o600


def test_triage_prelocked_queue_append_does_not_deadlock_and_keeps_concurrent_rows(tmp_path):
    """Triage owns a wider queue transaction lock. Its bounded writer must reuse that ownership,
    while separate triage processes still serialize and retain every complete queue row."""
    script_dir = HERE.parent / "event-triage" / "scripts"
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import triage_event as t; "
        "[t._append_queue('ambient', sys.argv[2], "
        "{'source':'imessage','rowid':f'{sys.argv[2]}-{i}'}) for i in range(20)]"
    )
    env = {**os.environ, "SOTTO_DATA": str(tmp_path)}
    processes = [subprocess.Popen([sys.executable, "-c", code, str(script_dir), tag], env=env)
                 for tag in ("child-a", "child-b")]
    for process in processes:
        process.communicate(timeout=10)
        assert process.returncode == 0

    rows = (tmp_path / "events" / "queue.jsonl").read_text().splitlines()
    assert len(rows) == 40
    assert sum('child-a' in row for row in rows) == 20
    assert sum('child-b' in row for row in rows) == 20


def test_contended_log_lock_times_out_instead_of_hanging(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "compose_brief.log"
    path.parent.mkdir()
    held_fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held_fd, fcntl.LOCK_EX)
    monkeypatch.setattr(jsonstore, "LOCK_TIMEOUT_SECS", 0.08)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="could not lock"):
            sotto_log.bounded_append(str(path), "never written", 100, 10)
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)
    assert time.monotonic() - started < 1
    assert not path.exists()


def test_diag_swallows_a_contended_lock_timeout(tmp_path, monkeypatch, capsys):
    log = tmp_path / "logs" / "compose_brief.log"
    log.parent.mkdir()
    held_fd = os.open(str(log) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held_fd, fcntl.LOCK_EX)
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(jsonstore, "LOCK_TIMEOUT_SECS", 0.04)
    try:
        sotto_log.diag("best effort still returns")
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)
    assert "best effort still returns" in capsys.readouterr().err
    assert not log.exists()
