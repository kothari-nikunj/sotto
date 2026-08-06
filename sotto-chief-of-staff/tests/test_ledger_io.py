"""ledger_io.py — the ONE continuity-ledger loader shared by retune_scan / loops_query /
continuity_resolve (no more copy-paste drift between the readers)."""
import importlib.util
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))

spec = importlib.util.spec_from_file_location("li", os.path.join(ROOT, "_shared", "scripts", "ledger_io.py"))
li = importlib.util.module_from_spec(spec)
spec.loader.exec_module(li)


def _write(tmp_path, name, text):
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text)


def test_parse_frontmatter_shapes():
    assert li.parse_frontmatter("no frontmatter here") is None          # bare file
    assert li.parse_frontmatter("---\nstatus: open\n---\nbody") == {"status": "open"}
    assert li.parse_frontmatter("---\n{}\n---\nbody") == {}             # valid EMPTY mapping
    # Malformed metadata is None (never a dict) — a broken file must never look like a valid
    # status-less entry, or continuity_resolve would persist '---\n{}\n---' over the content.
    assert li.parse_frontmatter("---\n[broken: yaml\n---\n") is None    # YAML error, never raises
    assert li.parse_frontmatter("---\nnever closed") is None            # unclosed fence
    assert li.parse_frontmatter("---\n- a\n- b\n---\n") is None         # non-dict yaml


def test_load_entries_and_active(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _write(tmp_path, "a.md", "---\nanchor_key: a\nstatus: open\n---\n")
    _write(tmp_path, "b.md", "---\nanchor_key: b\nstatus: resolved\n---\n")
    _write(tmp_path, "c.md", "---\nanchor_key: c\n---\n")               # no status → open
    _write(tmp_path, "d.md", "just a note, no frontmatter\n")           # bare
    entries = li.load_entries()
    assert [e.get("anchor_key") for e in entries] == ["a", "b", "c"]    # sorted; bare skipped
    assert "_path" not in entries[0]
    with_bare = li.load_entries(with_path=True, include_bare=True)
    assert len(with_bare) == 4 and all(e["_path"].endswith(".md") for e in with_bare)
    assert {e.get("anchor_key") for e in li.load_active()} == {"a", "c"}   # terminal filtered


def test_load_entries_flags_malformed_and_read_views_exclude_them(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _write(tmp_path, "a.md", "---\nanchor_key: a\nstatus: open\n---\n")
    _write(tmp_path, "broken.md", "---\n[broken: yaml\n---\nprecious body\n")
    _write(tmp_path, "bare.md", "just a note\n")
    # read views: malformed silently excluded (matches the readers' old per-file try/except)
    assert [e.get("anchor_key") for e in li.load_entries()] == ["a"]
    assert [e.get("anchor_key") for e in li.load_active()] == ["a"]
    # writers (continuity_resolve) get them flagged so they can skip — never persist over them
    with_bare = li.load_entries(with_path=True, include_bare=True)
    by_name = {e["_path"].rsplit("/", 1)[-1]: e for e in with_bare}
    assert by_name["broken.md"].get("_malformed") is True
    assert "_malformed" not in by_name["bare.md"]                       # bare ≠ malformed
    assert "_malformed" not in by_name["a.md"]


def test_load_entries_empty_when_no_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert li.load_entries() == [] and li.load_active() == []


def test_age_days():
    today = datetime(2026, 6, 24, 9, 0, 0, tzinfo=timezone.utc)
    assert li.age_days("2026-06-20", today) == 4
    assert li.age_days("2026-06-23 08:00:00", today) == 1
    assert li.age_days("2026-06-30", today) == 0                        # future clamps to 0
    assert li.age_days("garbage", today) is None
    assert li.age_days(None, today) is None
