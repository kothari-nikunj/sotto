"""Per-process reuse of parsed ledger rows and the people index must never serve a stale file."""
import os
import time

import pytest

import knowledge as kg
import ledger_io
import statmemo

OLD = time.time() - 3600


def _settle(path, when=OLD):
    os.utime(path, (when, when))


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    (tmp_path / "knowledge" / "continuity").mkdir(parents=True)
    (tmp_path / "knowledge" / "people").mkdir(parents=True)
    return tmp_path


def _row(data, name, status):
    path = data / "knowledge" / "continuity" / name
    path.write_text(f"---\nanchor_key: k:{name}\nstatus: {status}\nnested: {{n: [1]}}\n---\n")
    return path


def _counting(monkeypatch, module, name):
    calls, real = [], getattr(module, name)

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(module, name, counted)
    return calls


def test_settled_ledger_rows_parse_once_and_callers_get_private_copies(data, monkeypatch):
    for i in range(3):
        _settle(_row(data, f"{i}.md", "open"))
    parses = _counting(monkeypatch, ledger_io, "parse_frontmatter")
    first = ledger_io.load_entries(with_path=True)
    first[0]["status"] = "mutated"
    first[0]["nested"]["n"].append(2)
    second = ledger_io.load_entries()
    assert len(parses) == len(first)
    assert [r["status"] for r in second] == ["open"] * 3 and second[0]["nested"] == {"n": [1]}
    assert "_path" not in second[0]


def test_a_ledger_row_changed_between_loads_is_read_fresh(data):
    path = _row(data, "a.md", "open")
    _settle(path)
    assert ledger_io.load_entries()[0]["status"] == "open"
    _row(data, "a.md", "done")                        # same size, written now: never trusted
    assert ledger_io.load_entries()[0]["status"] == "done"
    _row(data, "a.md", "held")                        # again inside the same second
    assert ledger_io.load_entries()[0]["status"] == "held"
    _settle(path, OLD + 60)                           # settled again, at a different time
    assert ledger_io.load_entries()[0]["status"] == "held"
    path.write_text("---\nstatus: [unclosed\n---\n")
    _settle(path, OLD + 120)
    assert ledger_io.load_entries(include_bare=True) == [{"_malformed": True}]
    path.unlink()
    assert ledger_io.load_entries(include_bare=True) == []


def test_one_anchor_lookup_sees_a_row_persisted_between_calls(data):
    import continuity_resolve as cr
    path = _row(data, "a.md", "open")
    _settle(path)
    assert cr._load_items()["k:a.md"]["status"] == "open"
    assert cr._load_items()["k:a.md"]["status"] == "open"
    _row(data, "a.md", "done")
    assert cr._load_items().get("k:a.md")["status"] == "done"


def _person(data, cid, name, email):
    path = str(data / "knowledge" / "people" / f"{cid}.md")
    kg.write_person_file(path, kg.PersonFile(canonical_id=cid, name=name, identifiers=[email]))
    return path


def test_people_index_is_built_once_while_settled_and_rebuilt_on_any_change(data, monkeypatch):
    a = _person(data, "c_a", "Ada Lovelace", "ada@example.com")
    b = _person(data, "c_b", "Bo Diaz", "bo@example.com")
    _settle(a)
    _settle(b)
    parses = _counting(monkeypatch, kg, "parse_person_file")
    assert kg.find_person_file(identifier="ada@example.com") == a
    index = kg.build_people_index()
    index["by_identifier"].clear()                    # a caller's copy is its own
    assert kg.find_person_file(name="Bo Diaz") == b
    assert len(parses) == len([a, b])
    # An identifier moves to a new person: the very next lookup must see it.
    c = _person(data, "c_c", "Cy Ng", "cy@example.com")
    assert kg.find_person_file(identifier="cy@example.com") == c
    _person(data, "c_a", "Ada Lovelace", "ada@new.example")
    assert kg.find_person_file(identifier="ada@example.com") is None
    assert kg.find_person_file(identifier="ada@new.example") == a
    os.unlink(b)
    assert kg.find_person_file(name="Bo Diaz") is None
    _settle(a, OLD + 60)
    _settle(c, OLD + 60)
    os.rename(c, c.replace("c_c", "c_d"))            # same inode, size and mtime under a new name
    assert kg.find_person_file(identifier="cy@example.com") == c.replace("c_c", "c_d")


def test_recently_written_and_future_dated_files_are_never_reused():
    cache, built = {}, []

    class Stat:
        st_ino, st_size, st_ctime_ns = 1, 1, 1

        def __init__(self, mtime_ns):
            self.st_mtime_ns = mtime_ns

    def build():
        built.append(1)
        return {"n": len(built)}
    now = time.time_ns()
    for mtime in (now, now + 10**12, now - statmemo.SETTLE_NS + 10**8):
        assert statmemo.reuse(cache, "x", [("x", Stat(mtime))], build) != \
            statmemo.reuse(cache, "x", [("x", Stat(mtime))], build)
    settled = [("x", Stat(now - 2 * statmemo.SETTLE_NS))]
    assert statmemo.reuse(cache, "x", settled, build) == statmemo.reuse(cache, "x", settled, build)


def test_rows_for_deleted_ledger_files_are_dropped_from_the_memo(data):
    for i in range(20):
        _settle(_row(data, f"{i}.md", "open"))
    ledger_io.load_entries()
    for i in range(1, 20):
        (data / "knowledge" / "continuity" / f"{i}.md").unlink()
    assert len(ledger_io.load_entries()) == 1
    folder = str(data / "knowledge" / "continuity")
    assert [p for p in ledger_io._PARSED if p.startswith(folder)] == [os.path.join(folder, "0.md")]


def test_same_size_ledger_edit_with_preserved_mtime_is_read_fresh(data):
    path = _row(data, "preserved.md", "open")
    _settle(path)
    original = path.stat()
    assert ledger_io.load_entries()[0]["status"] == "open"
    _row(data, "preserved.md", "done")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    updated = path.stat()
    assert (updated.st_ino, updated.st_size, updated.st_mtime_ns) == (
        original.st_ino, original.st_size, original.st_mtime_ns)
    assert ledger_io.load_entries()[0]["status"] == "done"
