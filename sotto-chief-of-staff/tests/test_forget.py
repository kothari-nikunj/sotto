"""tools/forget.py — the retention tool docs/DATA-FLOW.md points at.

Two claims are worth a test, and they are the two a user is trusting:

  1. each verb removes EXACTLY its files and nothing else — a "delete the caches" that also took
     the delivery ledger would be a worse bug than not shipping the tool;
  2. the knowledge graph survives `--all` — memory is not exhaust, and the line between them is
     the whole design of the tool.
"""
import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.abspath(os.path.join(HERE, ".."))
SCRIPT = os.path.join(PACK, "tools", "forget.py")


def _load():
    spec = importlib.util.spec_from_file_location("forget_tool", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fg = _load()

LOG_SPEC = importlib.util.spec_from_file_location(
    "sotto_log_forget_test", os.path.join(PACK, "_shared", "lib", "sotto_log.py"))
sotto_log = importlib.util.module_from_spec(LOG_SPEC)
LOG_SPEC.loader.exec_module(sotto_log)

# name → (relative path, contents). One byte count per file so a summary can be checked by size.
EXHAUST = {
    "snapshot": ("knowledge/last_local_snapshot.json", '{"messages": []}'),
    "research": ("cache/research_2026-08-12.json", '{"cards": 1}'),
    "research2": ("cache/research_2026-08-11.json", '{"cards": 2}'),
    "calendar": ("cache/calendar_today.json", '{"events": 0}'),
    "log": ("logs/compose_brief.log", "line one\nline two\n"),
    "delivery": ("events/delivery.jsonl", '{"status": "delivered"}\n'),
    "sends": ("events/sends.jsonl", '{"result": "sent"}\n'),
}

# …and everything that must be there afterwards, whatever was asked for.
MEMORY = {
    "knowledge/people/vishnu-sharma.md": "# Vishnu Sharma\n",
    "knowledge/companies/acme.md": "# Acme\n",
    "knowledge/continuity/loop-1.md": "---\nanchor_key: x\n---\n",
    "preferences.json": '{"explicit": {}}',
    "style.json": '{"buckets": {}}',
    "outcomes.jsonl": '{"action": "accepted"}\n',
    "events/queue.jsonl": '{"verdict": "hold"}\n',
    "events/surfaced.jsonl": '{"verdict": "nudge"}\n',
    "knowledge/relationship_state.json": '{"queue": []}',
    "briefs/2026-08-12_morning.json": "{}",
}

VERB_FILES = {
    "snapshot": ["knowledge/last_local_snapshot.json"],
    "caches": ["cache/calendar_today.json", "cache/research_2026-08-11.json",
               "cache/research_2026-08-12.json"],
    "logs": ["logs/compose_brief.log"],
    "receipts": ["events/delivery.jsonl", "events/sends.jsonl"],
}


def _write(root, rel, body):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


def _seed(root):
    for rel, body in [v for v in EXHAUST.values()]:
        _write(root, rel, body)
    for rel, body in MEMORY.items():
        _write(root, rel, body)


def _locked_receipt_rewrite(path, writer_has_lock, let_writer_finish):
    """Spawn-safe model of retention's read/replace transaction on a receipt ledger."""
    with fg.jsonstore.lock(path):
        with open(path, encoding="utf-8") as stream:
            old = stream.read()
        writer_has_lock.set()
        if not let_writer_finish.wait(timeout=5):
            raise TimeoutError("test did not release receipt writer")
        fg.jsonstore.write_atomic(path, {"preserved": old})


def _present(root):
    out = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            out.add(os.path.relpath(full, root).replace(os.sep, "/"))
    return out


@pytest.fixture()
def vol(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setenv("SOTTO_DATA", root)
    _seed(root)
    return root


ALL_EXHAUST = {rel for rel, _ in EXHAUST.values()}


@pytest.mark.parametrize("verb", sorted(VERB_FILES))
def test_each_verb_removes_exactly_its_own_files(vol, verb):
    """The contract per verb: its files go, and NOTHING else on the volume changes."""
    before = _present(vol)
    summary = fg.forget({verb})

    assert sorted(r["path"] for r in summary["removed"]) == VERB_FILES[verb]
    assert summary["errors"] == []

    after = _present(vol)
    if verb == "logs":
        # Truncate, not unlink. The shared sidecar lock is durable by protocol; deleting a lock
        # inode after release could split future lockers across two different inodes.
        assert after == before | {"logs/compose_brief.log.lock"}
        assert os.path.getsize(os.path.join(vol, "logs", "compose_brief.log")) == 0
    else:
        assert before - after == set(VERB_FILES[verb])
    # every OTHER exhaust file is untouched, byte for byte
    for rel in ALL_EXHAUST - set(VERB_FILES[verb]):
        assert os.path.exists(os.path.join(vol, *rel.split("/")))


def test_byte_counts_are_the_sizes_that_were_actually_freed(vol):
    sizes = {rel: os.path.getsize(os.path.join(vol, *rel.split("/")))
             for rel in VERB_FILES["caches"]}
    summary = fg.forget({"caches"})
    assert {r["path"]: r["bytes"] for r in summary["removed"]} == sizes
    assert summary["bytes"] == sum(sizes.values())
    assert summary["count"] == len(sizes)


def test_log_forget_and_rotation_cannot_restore_erased_history(vol):
    path = os.path.join(vol, "logs", "compose_brief.log")
    old = "PRIVATE OLD DIAGNOSTIC\n" * 500
    _write(vol, "logs/compose_brief.log", old)
    start = threading.Barrier(3)

    def rotate_and_append():
        start.wait(timeout=5)
        sotto_log.bounded_append(path, "NEW DIAGNOSTIC", max_bytes=100, keep_lines=2)

    def erase():
        start.wait(timeout=5)
        fg.forget({"logs"})

    # Queue both operations behind the real sidecar lock, then release them together. Either legal
    # order may win; neither may rewrite the old tail after the user's erase.
    with sotto_log._log_lock(path):
        threads = [threading.Thread(target=rotate_and_append), threading.Thread(target=erase)]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        time.sleep(0.05)
        assert all(thread.is_alive() for thread in threads)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    body = open(path, encoding="utf-8").read()
    assert "PRIVATE OLD DIAGNOSTIC" not in body
    assert body in ("", "NEW DIAGNOSTIC\n")


@pytest.mark.parametrize("name", ("delivery.jsonl", "sends.jsonl"))
def test_receipt_forget_waits_for_locked_rewrite_then_removes_it(vol, name):
    path = os.path.join(vol, "events", name)
    process_context = multiprocessing.get_context("spawn")
    writer_has_lock = process_context.Event()
    let_writer_finish = process_context.Event()
    # Use a separate process to exercise the real cross-process flock contract. Spawn avoids
    # forking pytest after it has started background threads.
    writer = process_context.Process(
        target=_locked_receipt_rewrite,
        args=(path, writer_has_lock, let_writer_finish),
    )
    eraser = None
    writer.start()
    try:
        assert writer_has_lock.wait(timeout=5)
        result = {}
        eraser = threading.Thread(target=lambda: result.update(fg.forget({"receipts"})))
        eraser.start()
        time.sleep(0.05)
        assert eraser.is_alive(), "erasure bypassed the receipt ledger lock"
        let_writer_finish.set()
        writer.join(timeout=5)
        eraser.join(timeout=5)
        assert not writer.is_alive() and not eraser.is_alive()
        assert writer.exitcode == 0
        assert not os.path.exists(path)
        assert any(row["path"] == f"events/{name}" for row in result["removed"])
    finally:
        let_writer_finish.set()
        writer.join(timeout=5)
        if writer.is_alive():
            writer.terminate()
            writer.join(timeout=5)
        if eraser is not None:
            eraser.join(timeout=12)


def test_all_removes_every_exhaust_file_and_no_memory_file(vol):
    """`--all` is the widest thing this tool does, so it is where the memory line matters most."""
    summary = fg.forget(set(fg.VERBS))
    assert sorted(r["path"] for r in summary["removed"]) == sorted(ALL_EXHAUST)

    for rel, body in MEMORY.items():
        path = os.path.join(vol, *rel.split("/"))
        assert os.path.exists(path), f"--all deleted {rel}, which is memory, not exhaust"
        with open(path, encoding="utf-8") as f:
            assert f.read() == body, f"--all rewrote {rel}"

    # the graph directories themselves survive too — an empty people/ would be its own kind of lie
    for protected in fg.PROTECTED:
        assert os.path.isdir(os.path.join(vol, *protected.split("/")))


def test_nothing_to_do_is_a_success_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    summary = fg.forget(set(fg.VERBS))
    assert summary == {
        "data_root": str(tmp_path), "verbs": sorted(fg.VERBS), "removed": [], "count": 0,
        "bytes": 0, "errors": [], "kept": list(fg.PROTECTED)}
    assert fg.main(["--all"]) == 0


def test_the_protected_guard_refuses_a_memory_path_whatever_asks(vol):
    """The guard is per PATH, not per verb, so a future verb (or a typo'd glob) can't reach the
    graph by being added to the wrong list."""
    for rel in ("knowledge/people/vishnu-sharma.md", "knowledge/companies/acme.md",
                "knowledge/continuity/loop-1.md", "knowledge/people", "knowledge/continuity"):
        assert fg._protected(rel), rel
    for rel in ("knowledge/last_local_snapshot.json", "knowledge/relationship_state.json",
                "cache/calendar_today.json", "events/sends.jsonl"):
        assert not fg._protected(rel), rel


def test_no_verb_is_a_usage_error_not_a_silent_success(capsys):
    assert fg.main([]) == 2
    assert "at least one verb" in capsys.readouterr().err


def test_cli_prints_one_json_object_and_exits_zero(vol):
    """The summary is meant to be piped, so it must be exactly one JSON document on stdout."""
    env = dict(os.environ, SOTTO_DATA=vol)
    proc = subprocess.run([sys.executable, SCRIPT, "--snapshot", "--receipts"],
                          capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert sorted(r["path"] for r in summary["removed"]) == [
        "events/delivery.jsonl", "events/sends.jsonl", "knowledge/last_local_snapshot.json"]


def test_an_unknown_verb_is_rejected_rather_than_ignored(vol):
    with pytest.raises(ValueError):
        fg.forget({"everything"})


def test_data_flow_points_at_this_tool_by_its_real_path_and_verbs():
    """docs/DATA-FLOW.md used to point at `tools/forget.sh`, which never existed — a retention
    promise with no implementation behind it. Keep the reference honest."""
    docs = os.path.join(os.path.dirname(PACK), "docs", "DATA-FLOW.md")
    with open(docs, encoding="utf-8") as f:
        body = f.read()
    assert "tools/forget.sh" not in body, "DATA-FLOW.md still names a script that does not exist"
    assert "sotto-chief-of-staff/tools/forget.py" in body
    for verb in fg.VERBS:
        assert f"--{verb}" in body, f"DATA-FLOW.md doesn't mention forget.py's --{verb}"


def test_caches_forgets_card_images_and_source_manifests(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    _write(str(tmp_path), 'cache/visual-briefs/deck/01.png', 'private image')
    _write(str(tmp_path), 'cache/visual-briefs/deck/manifest.json', 'private source text')
    fg.forget({'caches'})
    assert not _present(str(tmp_path))
