"""test_knowledge_journal.py — a graph update that is interrupted finishes; it never half-applies.

THE BUG (external review finding #6, Aug 31): the knowledge graph is Sotto's durable memory, and
its mutations were neither atomic per file nor transactional across files.

  * Per file: every person and company write was `open(path, "w")`, which TRUNCATES before it
    writes. A crash in that window (OOM, container restart, a killed `hermes -z`) left an EMPTY
    person file — everything Sotto knew about someone, gone, with no error anywhere.
  * Across files: a relation is stored on BOTH people and a merge writes the survivor, deletes the
    loser and repoints everyone who pointed at it. A crash between those writes left a one-sided
    edge or a set of edges naming a slug with no file, and nothing noticed or repaired it.

The fix is two mechanisms, and these tests pin both: one temp→`os.replace` primitive for every
graph file, and a `knowledge/.journal.json` write-ahead list for the batches that span files, which
every writer entry point replays before it reads anything.

Fixtures are synthetic (example.com).
"""
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(__file__)
KNOW = os.path.join(HERE, "..", "_shared", "knowledge")

import knowledge as kg  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "knowledge_update", os.path.join(KNOW, "knowledge_update.py"))
ku = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ku)

_rt_spec = importlib.util.spec_from_file_location(
    "kj_retention", os.path.join(HERE, "..", "..", "runtime", "trigger-receiver", "retention.py"))
rt = importlib.util.module_from_spec(_rt_spec)
_rt_spec.loader.exec_module(rt)

ANNA = "c_" + "a" * 12
BEN = "c_" + "b" * 12
CARA = "c_" + "c" * 12


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    os.makedirs(kg.people_dir(), exist_ok=True)
    return tmp_path


def _person(cid, name, email):
    kg.write_person_file(
        kg.safe_path(kg.people_dir(), cid),
        kg.PersonFile(canonical_id=cid, name=name, identifiers=[email],
                      updated_at="2026-08-31T09:00:00Z", updated_by="test"))


def _read(cid):
    with open(kg.safe_path(kg.people_dir(), cid), encoding="utf-8") as f:
        return kg.parse_person_file(f.read())


def _bytes(cid):
    with open(kg.safe_path(kg.people_dir(), cid), "rb") as f:
        return f.read()


def _journal():
    with open(kg.journal_path(), encoding="utf-8") as f:
        return json.load(f)


# ── per-file atomicity ───────────────────────────────────────────────────────────────────────────

def test_a_failed_person_write_cannot_damage_the_file_it_was_replacing(data, monkeypatch):
    """The truncation window does not exist: the write goes to a temp and the rename is the only
    thing that touches the real file, so a writer that dies mid-write leaves the previous
    memory byte-for-byte intact."""
    _person(ANNA, "Anna Reyes", "anna@example.com")
    before = _bytes(ANNA)

    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("killed")))
    p = _read(ANNA)
    p.name = "Anna Reyes-Whitfield"
    with pytest.raises(OSError):
        kg.write_person_file(kg.safe_path(kg.people_dir(), ANNA), p)

    assert _bytes(ANNA) == before


def test_a_failed_write_leaves_no_temp_file_behind(data, monkeypatch):
    """A temp that survives its writer is litter in the directory every reader globs."""
    _person(ANNA, "Anna Reyes", "anna@example.com")
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("killed")))
    with pytest.raises(OSError):
        kg.write_person_file(kg.safe_path(kg.people_dir(), ANNA), _read(ANNA))
    assert sorted(os.listdir(kg.people_dir())) == [f"{ANNA}.md"]


def _kill_after_first_write(monkeypatch):
    """Make the SECOND person-file write of a batch die, the way a container restart does. Returns
    the restore callable — `monkeypatch.undo()` would also undo the fixture's SOTTO_DATA."""
    real = kg.write_person_file
    calls = []

    def die_on_second(path, p, now=None):
        calls.append(path)
        if len(calls) > 1:
            raise RuntimeError("container restart")
        real(path, p, now)

    monkeypatch.setattr(kg, "write_person_file", die_on_second)
    return lambda: monkeypatch.setattr(kg, "write_person_file", real)


# ── the journal: a batch that spans files ────────────────────────────────────────────────────────

def test_a_link_interrupted_after_the_first_file_leaves_a_journal(data, monkeypatch):
    """A relation is one fact about two people. Kill the process between the two file writes and
    the graph is one-sided — but the journal on disk says what was owed."""
    _person(ANNA, "Anna Reyes", "anna@example.com")
    _person(BEN, "Ben Okafor", "ben@example.com")

    _kill_after_first_write(monkeypatch)
    with pytest.raises(RuntimeError):
        ku.link_relation(ANNA, BEN, "introduced_by", name_a="Anna Reyes", name_b="Ben Okafor")

    assert [r.type for r in _read(ANNA).relations] == ["introduced_by"]
    assert _read(BEN).relations == []                       # the other end never landed
    assert [op["op"] for op in _journal()["ops"]] == ["edge", "edge"]


def test_the_next_thing_to_touch_the_graph_finishes_the_link(data, monkeypatch, capsys):
    """The rule, end to end: the next writer replays the journal, both ends agree again, and the
    journal is gone. The repair says one line — it is the only thing here worth saying."""
    _person(ANNA, "Anna Reyes", "anna@example.com")
    _person(BEN, "Ben Okafor", "ben@example.com")
    _person(CARA, "Cara Lindqvist", "cara@example.com")

    restore = _kill_after_first_write(monkeypatch)
    with pytest.raises(RuntimeError):
        ku.link_relation(ANNA, BEN, "introduced_by", name_a="Anna Reyes", name_b="Ben Okafor")
    restore()

    # Any writer entry point will do — this one is about a third person entirely.
    ku.apply({"person_updates": [{"person_name": "Cara Lindqvist", "identifier": "cara@example.com",
                                  "canonical_id": CARA, "facts": []}]})

    assert [(r.type, r.slug) for r in _read(ANNA).relations] == [("introduced_by", BEN)]
    assert [(r.type, r.slug) for r in _read(BEN).relations] == [("introduced", ANNA)]
    assert not os.path.exists(kg.journal_path())
    assert "knowledge journal: finished 1 interrupted op(s)" in capsys.readouterr().err


def test_replaying_a_finished_batch_changes_no_bytes(data):
    """Replay re-runs the whole op list without knowing how far the crash got, so every op has to
    be idempotent. Run it twice over a completed link: both files must be byte-stable."""
    _person(ANNA, "Anna Reyes", "anna@example.com")
    _person(BEN, "Ben Okafor", "ben@example.com")
    ops = [kg.edge_op(ANNA, "Anna Reyes",
                      kg.Relation(type="works_with", slug=BEN, name="Ben Okafor", source="test")),
           kg.edge_op(BEN, "Ben Okafor",
                      kg.Relation(type="works_with", slug=ANNA, name="Anna Reyes", source="test"))]
    kg.run_journaled(ops)
    after = (_bytes(ANNA), _bytes(BEN))

    for _ in range(2):
        kg.write_text_atomic(kg.journal_path(), json.dumps({"ts": "2026-08-31T09:00:00Z",
                                                            "ops": ops}))
        assert kg.replay_journal() == 0            # nothing left to finish
        assert (_bytes(ANNA), _bytes(BEN)) == after


def test_a_clean_link_leaves_no_journal(data):
    """The journal is a crash artifact, not a log: it exists only between the first write and the
    last."""
    _person(ANNA, "Anna Reyes", "anna@example.com")
    _person(BEN, "Ben Okafor", "ben@example.com")
    assert ku.link_relation(ANNA, BEN, "works_with")
    assert not os.path.exists(kg.journal_path())


def test_an_interrupted_merge_finishes_and_leaves_nobody_pointing_at_the_deleted_file(data,
                                                                                     monkeypatch):
    """A merge is a write, a delete and N repoints. Interrupt it after the survivor is written and
    the graph holds two files for one person plus edges naming a slug about to vanish; the next
    writer completes all three."""
    _person(ANNA, "Anna Reyes", "anna@example.com")
    _person(BEN, "Ben Okafor", "ben@example.com")
    _person(CARA, "Cara Lindqvist", "cara@example.com")
    ku.link_relation(BEN, CARA, "works_with", name_a="Ben Okafor", name_b="Cara Lindqvist")

    real_remove = os.remove
    monkeypatch.setattr(os, "remove", lambda *a, **k: (_ for _ in ()).throw(OSError("killed")))
    with pytest.raises(OSError):
        ku.merge_person_files(kg.safe_path(kg.people_dir(), ANNA),
                              kg.safe_path(kg.people_dir(), BEN))
    monkeypatch.setattr(os, "remove", real_remove)

    assert os.path.exists(kg.safe_path(kg.people_dir(), BEN))          # half-applied
    assert kg.replay_journal() == 1
    assert not os.path.exists(kg.safe_path(kg.people_dir(), BEN))
    assert "ben@example.com" in _read(ANNA).identifiers
    assert [(r.type, r.slug) for r in _read(CARA).relations] == [("works_with", ANNA)]
    assert not os.path.exists(kg.journal_path())


def test_an_unlink_is_journaled_as_one_batch_too(data, monkeypatch):
    """Removing an edge from one file and not the other is the same half-applied graph as adding
    one, so the undo takes the same route."""
    _person(ANNA, "Anna Reyes", "anna@example.com")
    _person(BEN, "Ben Okafor", "ben@example.com")
    ku.link_relation(ANNA, BEN, "works_with")

    restore = _kill_after_first_write(monkeypatch)
    with pytest.raises(RuntimeError):
        ku.unlink_relation(ANNA, BEN, "works_with")
    restore()

    assert _read(BEN).relations                     # still there — the batch didn't finish
    assert kg.replay_journal() == 1
    assert _read(ANNA).relations == [] and _read(BEN).relations == []


# ── a broken journal must not brick the graph ────────────────────────────────────────────────────

def test_an_unparseable_journal_is_cleared_and_the_write_still_happens(data, capsys):
    """A journal nobody can read would otherwise be re-read, and re-fail, on every future graph
    touch — every write in Sotto's memory blocked by one corrupt file."""
    _person(ANNA, "Anna Reyes", "anna@example.com")
    kg.write_text_atomic(kg.journal_path(), "{not json at all")

    ku.apply({"person_updates": [{"person_name": "Anna Reyes", "identifier": "anna@example.com",
                                  "canonical_id": ANNA,
                                  "facts": [{"fact": "Runs the Helsinki office", "confidence": 0.9,
                                             "memory_type": "context"}]}]})

    assert not os.path.exists(kg.journal_path())
    assert any(f.text == "Runs the Helsinki office" for f in _read(ANNA).facts.values())
    assert "knowledge journal: unreadable" in capsys.readouterr().err


def test_an_op_that_can_never_apply_is_dropped_rather_than_retried_forever(data):
    """Both files gone, a type outside the closed vocabulary, a slug that isn't a canonical_id: the
    journal is cleared anyway, silently. A repair that cannot happen is not an emergency."""
    kg.write_text_atomic(kg.journal_path(), json.dumps({"ts": "2026-08-31T09:00:00Z", "ops": [
        {"op": "merge", "dst": "c_deadbeef0001", "src": "c_deadbeef0002"},
        {"op": "edge", "slug": ANNA, "name": "Anna", "rel": {"type": "nemesis_of", "slug": BEN}},
        {"op": "nonsense"},
    ]}))
    assert kg.replay_journal() == 0
    assert not os.path.exists(kg.journal_path())


# ── the journal lives in the graph, which the sweep may never touch ──────────────────────────────

def test_the_retention_sweep_can_never_reach_the_journal():
    """`knowledge/` is in retention's NEVER set per path, not per rule — the journal inherits that,
    and it must, because deleting a journal is deleting a half-finished memory."""
    assert rt.protected("knowledge/" + kg.JOURNAL_FILE)


# ── the second reviewer's holes (Aug 31): the delete-then-crash window, and raised ops ───────────

def test_a_merge_interrupted_after_the_delete_still_repoints(data, monkeypatch):
    """The reviewer's repro: crash AFTER the loser is deleted but BEFORE the repointing. "The loser
    is gone" used to read as "this merge finished" — the dangling edge survived, replay reported 0,
    and the journal was cleared. Now the merge op finishes the repointing it still owes."""
    _person(ANNA, "Anna Reyes", "anna@example.com")
    _person(BEN, "Ben Okafor", "ben@example.com")
    _person(CARA, "Cara Lindqvist", "cara@example.com")
    ku.link_relation(BEN, CARA, "works_with", name_a="Ben Okafor", name_b="Cara Lindqvist")

    real_repoint = kg.repoint_relations
    monkeypatch.setattr(kg, "repoint_relations",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("killed")))
    with pytest.raises(OSError):
        ku.merge_person_files(kg.safe_path(kg.people_dir(), ANNA),
                              kg.safe_path(kg.people_dir(), BEN))
    monkeypatch.setattr(kg, "repoint_relations", real_repoint)

    assert not os.path.exists(kg.safe_path(kg.people_dir(), BEN))       # the delete happened…
    assert any(r.slug == BEN for r in _read(CARA).relations)            # …the repointing did not
    assert kg.replay_journal() == 1
    assert [(r.type, r.slug) for r in _read(CARA).relations] == [("works_with", ANNA)]
    assert not os.path.exists(kg.journal_path())


def test_an_op_that_raises_is_kept_for_the_next_replay(data, monkeypatch, capsys):
    """Clearing an op that RAISED declares victory over a half-applied graph. It stays journaled
    (attempts counted) until a replay applies it — or gives up loudly at the cap."""
    _person(ANNA, "Anna Reyes", "anna@example.com")
    _person(BEN, "Ben Okafor", "ben@example.com")
    rel = kg.Relation(type="works_with", slug=BEN, name="Ben Okafor", date="2026-08-31",
                      source="test")
    kg.write_text_atomic(kg.journal_path(), json.dumps(
        {"ts": "2026-08-31T09:00:00Z", "ops": [kg.edge_op(ANNA, "Anna Reyes", rel)]}))

    real_write = kg.write_person_file
    monkeypatch.setattr(kg, "write_person_file",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert kg.replay_journal() == 0
    assert _journal()["ops"][0]["attempts"] == 1                        # kept, not cleared
    monkeypatch.setattr(kg, "write_person_file", real_write)
    assert kg.replay_journal() == 1                                     # the next replay finishes
    assert not os.path.exists(kg.journal_path())
    assert any(r.slug == BEN for r in _read(ANNA).relations)


def test_a_forever_broken_op_is_dropped_at_the_attempt_cap(data, monkeypatch, capsys):
    """A permanently broken op must not tax every graph write forever — at JOURNAL_MAX_ATTEMPTS it
    is dropped with a line saying so."""
    _person(ANNA, "Anna Reyes", "anna@example.com")
    rel = kg.Relation(type="works_with", slug=BEN, name="Ben Okafor", date="2026-08-31",
                      source="test")
    kg.write_text_atomic(kg.journal_path(), json.dumps(
        {"ts": "2026-08-31T09:00:00Z", "ops": [kg.edge_op(ANNA, "Anna Reyes", rel)]}))
    monkeypatch.setattr(kg, "write_person_file",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("poisoned")))
    for _ in range(kg.JOURNAL_MAX_ATTEMPTS):
        kg.replay_journal()
    assert not os.path.exists(kg.journal_path())
    assert "gave up" in capsys.readouterr().err


def test_a_new_batch_does_not_clobber_an_op_still_retrying(data, monkeypatch):
    """run_journaled used to overwrite the journal with its own batch, so an op a replay had kept
    for retry was discarded after ONE attempt and the graph declared finished half-applied — the
    exact hole JOURNAL_MAX_ATTEMPTS exists to close (Day-7 simulation, Sep 2026). The retrying op
    rides along, untouched, and the next replay finishes it."""
    _person(ANNA, "Anna Reyes", "anna@example.com")
    _person(BEN, "Ben Okafor", "ben@example.com")
    rel = kg.Relation(type="works_with", slug=BEN, name="Ben Okafor", date="2026-08-31",
                      source="test")
    stuck = dict(kg.edge_op(ANNA, "Anna Reyes", rel), attempts=2)
    kg.write_text_atomic(kg.journal_path(), json.dumps(
        {"ts": "2026-08-31T09:00:00Z", "ops": [stuck]}))
    applied = []
    monkeypatch.setattr(kg, "_apply_journal_op", lambda op, now: applied.append(op.get("op")) or True)
    kg.run_journaled([{"op": "unedge", "slug": ANNA, "rel_key": "nobody"}])
    assert applied == ["unedge"], "the new batch applies; the stuck op is replay's to retry"
    assert _journal()["ops"] == [stuck], "the retrying op survives with its attempts intact"
