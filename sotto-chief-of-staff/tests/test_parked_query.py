"""Parked obligations have an explicit, opt-in supported read path."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "_shared" / "scripts" / "loops_query.py"
spec = importlib.util.spec_from_file_location("lq_parked_query", SCRIPT)
lq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lq)


def _write(directory, name, **fields):
    fields.setdefault("anchor_key", name)
    fields.setdefault("created_at", "2026-09-01")
    (directory / f"{name}.md").write_text(
        "---\n" + yaml.safe_dump(fields, sort_keys=False) + "---\n"
    )


def _seed(tmp_path):
    directory = tmp_path / "knowledge" / "continuity"
    directory.mkdir(parents=True)
    _write(directory, "open", status="open", action_type="reply", contact_name="Open")
    _write(directory, "later", status="parked", action_type="reply", contact_name="Later",
           created_at="2026-09-05")
    _write(directory, "urgent", status="parked", action_type="reply", contact_name="Urgent",
           created_at="2026-09-10", deadline="2026-09-01")
    _write(directory, "older", status="parked", action_type="waiting_on", contact_name="Older",
           created_at="2026-08-01")
    _write(directory, "shadow", status="parked", action_type="meeting_prep", contact_name="Shadow",
           created_at="2026-07-01")


def test_parked_rows_are_opt_in_filtered_and_stably_sorted(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    _seed(tmp_path)

    normal = lq.query()
    assert normal["counts"] == {"you_owe": 1, "waiting_on_them": 0}
    assert "parked" not in normal

    expanded = lq.query(include_parked=True)
    assert expanded["counts"] == {"you_owe": 1, "waiting_on_them": 0, "parked": 3}
    assert [row["anchor_key"] for row in expanded["parked"]] == ["urgent", "older", "later"]
    assert all(row["anchor_key"] != "shadow" for row in expanded["parked"])


def test_cli_parked_flag_preserves_default_shape(tmp_path):
    _seed(tmp_path)
    env = {**os.environ, "SOTTO_DATA": str(tmp_path), "SOTTO_TIMEZONE": "+00:00"}
    default = json.loads(subprocess.check_output([os.sys.executable, str(SCRIPT)], env=env))
    expanded = json.loads(subprocess.check_output([os.sys.executable, str(SCRIPT), "--parked"], env=env))
    assert "parked" not in default and "parked" not in default["counts"]
    assert [row["anchor_key"] for row in expanded["parked"]] == ["urgent", "older", "later"]
    assert expanded["counts"]["parked"] == 3
