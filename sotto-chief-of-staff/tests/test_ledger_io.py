"""ledger_io.py — the ONE continuity-ledger loader shared by retune_scan / loops_query /
continuity_resolve (no more copy-paste drift between the readers)."""
import importlib.util
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")

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


def test_load_active_hides_a_snoozed_loop_from_every_read_view(tmp_path, monkeypatch):
    """The snooze is ONE rule, and it lives here. The brief used to read the ledger without it, so a
    loop the user had explicitly parked was named as urgent in the brief and counted in the line
    pointing at /app#loops — where sotto-loops showed neither. Hidden is hidden, in every view."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _write(tmp_path, "a.md", "---\nanchor_key: a\nstatus: open\nsnoozed_until: '2099-01-01'\n---\n")
    _write(tmp_path, "b.md", "---\nanchor_key: b\nstatus: open\nsnoozed_until: '2020-01-01'\n---\n")
    _write(tmp_path, "c.md", f"---\nanchor_key: c\nstatus: open\nsnoozed_until: '{today}'\n---\n")
    # the file itself is untouched — a snooze hides a loop, it never closes one
    assert [e.get("anchor_key") for e in li.load_entries()] == ["a", "b", "c"]
    # …and the snooze ends ON its date (`> today`, matching continuity_resolve's own test)
    assert [e.get("anchor_key") for e in li.load_active()] == ["b", "c"]


def test_chase_state_fields_are_named_once(tmp_path, monkeypatch):
    """The chase's whole story on a row, listed in one place: a caller that clears four of the five
    leaves a loop that can never be chased again or one that is never asked about twice."""
    assert li.CHASE_STATE_FIELDS == ("chased_count", "chase_after", "last_chased_at",
                                     "chase_pending", "handoff_asked_at")
