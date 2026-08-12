"""test_knowledge_apply_lock.py — knowledge_update.apply() is serialized across processes.

THE BUG: four callers write the graph through apply() — the brief's Learn step, meeting-prep
research (persist_prep), the prewarm sweep, and dashboard/chat edits — each doing read → modify →
write over the same person markdown, with no lock. That is the preferences.json race one level up:
two overlapping applies drop one side's facts, because the second parsed the file before the first
wrote it.

These tests run REAL subprocesses (the collision is between processes, not threads) writing facts
about two different people at the same moment: both facts must survive and every file must still
parse.
"""
import importlib.util
import json
import os
import subprocess
import sys
import textwrap

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
KNOW = os.path.join(ROOT, "_shared", "knowledge")
LIB = os.path.join(ROOT, "_shared", "lib")

_spec = importlib.util.spec_from_file_location(
    "knowledge_update", os.path.join(KNOW, "knowledge_update.py"))
ku = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ku)

import jsonstore  # noqa: E402
import knowledge as kg  # noqa: E402


_WRITER = textwrap.dedent("""
    import importlib.util, os, sys, time
    sys.path.insert(0, {know!r}); sys.path.insert(0, {lib!r})
    spec = importlib.util.spec_from_file_location("knowledge_update", os.path.join({know!r}, "knowledge_update.py"))
    ku = importlib.util.module_from_spec(spec); spec.loader.exec_module(ku)
    import knowledge as kg
    name, fact, delay, hold = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
    if hold:
        # Widen the read→write window from the INSIDE, deterministically: serialization is the last
        # step before the file is rewritten, so a slow writer is one that has read and not yet
        # written — exactly the interval a second writer must not be allowed into.
        _real = kg.serialize_person_file
        kg.serialize_person_file = lambda *a, **k: (time.sleep(hold), _real(*a, **k))[1]
    time.sleep(delay)
    ku.apply({{"person_updates": [{{"person_name": name, "identifier": name.lower() + "@x.com",
              "facts": [{{"fact": fact, "memory_type": "milestone", "confidence": 0.9}}]}}]}})
""")


def _run_writer(tmp_path, name, fact, delay="0", hold="0"):
    script = tmp_path / "writer.py"
    script.write_text(_WRITER.format(know=KNOW, lib=LIB))
    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    return subprocess.Popen([sys.executable, str(script), name, fact, delay, hold], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _wait(*procs):
    for p in procs:
        _o, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()[-2000:]


def _people_facts(tmp_path):
    """{person name: [fact text]} read straight off disk."""
    out = {}
    for fn in os.listdir(kg.people_dir()):
        if not fn.endswith(".md"):
            continue
        with open(os.path.join(kg.people_dir(), fn), encoding="utf-8") as f:
            p = kg.parse_person_file(f.read())          # a torn file raises here
        out[p.name] = [m.text for m in p.facts.values()]
    return out


def test_two_concurrent_applies_both_survive(tmp_path, monkeypatch):
    """The spec's case: two applies about two different people, at the same moment."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _wait(_run_writer(tmp_path, "Ada", "Founded Analytical Engines"),
          _run_writer(tmp_path, "Grace", "Wrote the first compiler"))
    facts = _people_facts(tmp_path)
    assert "Founded Analytical Engines" in facts.get("Ada", []), facts
    assert "Wrote the first compiler" in facts.get("Grace", []), facts


def test_a_second_apply_cannot_enter_the_first_ones_read_write_window(tmp_path, monkeypatch):
    """The sharp version, and the one that fails without the lock: writer A has read Ada's file and
    not yet written it when writer B starts. Unlocked, B reads the pre-A file and A's write lands
    last — B's fact is silently gone. The two facts have nothing in common, so dedup (which is
    supposed to collapse restatements) cannot be what saves or loses them."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _wait(_run_writer(tmp_path, "Ada", "Seeded before the race"))       # the file both will read
    slow = _run_writer(tmp_path, "Ada", "Founded the analytical programme", hold="1.5")
    fast = _run_writer(tmp_path, "Ada", "Runs infrastructure at Northwind", delay="0.8")
    _wait(slow, fast)
    facts = sorted(_people_facts(tmp_path).get("Ada", []))
    assert facts == ["Founded the analytical programme", "Runs infrastructure at Northwind",
                     "Seeded before the race"], facts


def test_lock_is_held_across_the_whole_apply(tmp_path, monkeypatch):
    """A DIFFERENT process holding the graph lock blocks apply() — proof the body really runs
    inside it. The holder must be another process: jsonstore.lock is deliberately reentrant within
    one process (knowledge_edit's locked merge nests inside apply's own lock), so an in-process
    holder no longer blocks — which is the designed behavior, not the property under test."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(jsonstore, "LOCK_TIMEOUT_SECS", 1)
    holder = tmp_path / "holder.py"
    holder.write_text(textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {LIB!r})
        import jsonstore
        with jsonstore.lock({str(tmp_path / 'knowledge' / '.apply')!r}):
            print("HELD", flush=True)
            time.sleep(5)
    """))
    proc = subprocess.Popen([sys.executable, str(holder)], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "HELD"
        with pytest.raises(TimeoutError):
            ku.apply({"person_updates": [{"person_name": "Ada", "identifier": "ada@x.com",
                                          "facts": [{"fact": "blocked", "memory_type": "milestone",
                                                     "confidence": 0.9}]}]})
    finally:
        proc.kill()
        proc.wait()


def test_a_locked_merge_nests_inside_apply_without_deadlocking(tmp_path, monkeypatch):
    """The reason the lock is reentrant: apply() calls the merge machinery from inside its own
    locked body, and knowledge_edit's --op merge takes the same lock at its own entry point. If
    reentrancy ever regresses, apply() deadlocks against itself on the auto-merge path — this
    pins the nesting directly."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr(jsonstore, "LOCK_TIMEOUT_SECS", 2)
    with jsonstore.lock(ku.apply_lock_target()):
        with jsonstore.lock(ku.apply_lock_target()):   # would TimeoutError before reentrancy
            pass


def test_the_lock_file_is_the_documented_one(tmp_path, monkeypatch):
    """One name, so a second caller takes THE graph lock instead of inventing another."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert jsonstore.lock_path(ku.apply_lock_target()) == os.path.join(
        str(tmp_path), "knowledge", ".apply.lock")


def test_lock_contextmanager_is_exclusive_and_times_out(tmp_path, monkeypatch):
    """jsonstore.lock: the transaction's lock half — no read, no write, same suffix and timeout."""
    monkeypatch.setattr(jsonstore, "LOCK_TIMEOUT_SECS", 1)
    target = str(tmp_path / "thing")
    taker = tmp_path / "taker.py"
    taker.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {LIB!r})
        import jsonstore
        jsonstore.LOCK_TIMEOUT_SECS = 1
        try:
            with jsonstore.lock({target!r}):
                print("GOT")
        except TimeoutError:
            print("BLOCKED")
    """))
    with jsonstore.lock(target):
        assert os.path.exists(target + jsonstore.LOCK_SUFFIX)
        assert not os.path.exists(target)                 # it locks; it does not create the file
        proc = subprocess.run([sys.executable, str(taker)], capture_output=True, timeout=120)
        assert b"BLOCKED" in proc.stdout, (proc.stdout, proc.stderr)
    assert b"GOT" in subprocess.run([sys.executable, str(taker)],
                                    capture_output=True, timeout=120).stdout

def test_transaction_still_writes_through_the_same_lock(tmp_path):
    """transaction() is lock() plus a read and an atomic write — one flock implementation."""
    path = str(tmp_path / "prefs.json")
    with jsonstore.transaction(path, default={}) as data:
        data["muted"] = ["sarah"]
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"muted": ["sarah"]}
