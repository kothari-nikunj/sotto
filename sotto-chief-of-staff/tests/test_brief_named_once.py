"""An unchanged overdue loop you owe is named once, then kept in the count line until it changes
(owner, Nov 2026). Fictional people only."""
import datetime
import importlib.util
import json
from pathlib import Path

import brief_marker
import brief_validate as bv
from delivery_effects import finalize, loop_version, stage

spec = importlib.util.spec_from_file_location("compose_brief_named_once",
    Path(__file__).resolve().parents[1] / "_shared/scripts/compose_brief.py")
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)

TODAY = "2026-03-10"


def _owed(**over):
    row = {"anchor_key": "loop:ines", "contact_name": "Ines Okafor", "status": "open",
           "action_type": "reply", "channel": "email", "contact_identifier": "ines@example.test",
           "summary": "the venue contract", "deadline": "2026-03-08", "created_at": "2026-03-01T09:00:00Z"}
    return {**row, **over}


def test_overdue_loop_named_at_same_version_falls_to_the_count_line():
    row = _owed()
    prior = {"loop:ines": loop_version(row)}
    assert bv.is_urgent(row, TODAY) and not bv.is_urgent(row, TODAY, prior)
    assert bv.missing_open_loops("a quiet day", [row], TODAY, prior) == []
    assert bv.unsurfaced_open_loops("a quiet day", [row], TODAY, prior) == [row]


def test_any_change_to_the_loop_makes_it_mandatory_again():
    row = _owed()
    prior = {"loop:ines": loop_version(row)}
    changed = _owed(summary="the venue contract, now with a signed rider")
    assert bv.is_urgent(changed, TODAY, prior)
    assert bv.missing_open_loops("a quiet day", [changed], TODAY, prior) == [changed]


def test_waiting_on_and_due_today_loops_keep_their_line():
    chased = _owed(action_type="waiting_on", chased_count=1)
    due_today = _owed(deadline=TODAY)
    prior = {"loop:ines": loop_version(chased)}
    assert bv.is_urgent(chased, TODAY, prior)
    assert bv.is_urgent(due_today, TODAY, {"loop:ines": loop_version(due_today)})


def test_told_before_reads_the_nearest_accepted_brief_across_a_missed_day(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    early = dict(kind="brief_named", day="2026-03-08", edition="morning",
                 anchor_keys=["loop:ines"], loop_versions={"loop:ines": "v1"})
    recent = dict(early, edition="evening", loop_versions={"loop:ines": "v2"})
    assert brief_marker.told_before(TODAY, "morning") == {}
    assert finalize([early], {"accepted_at": 1})
    assert brief_marker.told_before(TODAY, "morning") == {"loop:ines": "v1"}
    assert finalize([recent], {"accepted_at": 2})
    assert brief_marker.told_before(TODAY, "morning") == {"loop:ines": "v2"}


def test_only_overdue_loops_are_recorded_as_told():
    overdue, due_today = _owed(), _owed(anchor_key="loop:tomas", deadline=TODAY)
    assert brief_marker.told_versions([overdue, due_today], [overdue, due_today], TODAY, {}) == {
        "loop:ines": loop_version(overdue)}
    assert brief_marker.told_versions([overdue], [], TODAY, {"loop:ines": loop_version(overdue)}) == {
        "loop:ines": loop_version(overdue)}                         # carried while unchanged
    assert brief_marker.told_versions([_owed(summary="new")], [], TODAY, {"loop:ines": loop_version(overdue)}) == {}


def _compose(monkeypatch, tmp_path, row, prose="## Needs Attention Now\nQuiet.\n"):
    import ledger_provenance
    import loops_query
    monkeypatch.setattr(loops_query, "permitted_rows", lambda _root, _sources: [row])
    monkeypatch.setattr(ledger_provenance, "source_loop_sources", lambda _row: frozenset({"calls"}))

    def fake_llm(prompt, inputs, system=None, schema=None):
        return json.dumps({"brief_markdown": prose, "actions": []})

    return cb.compose({"type": "morning", "google": {}, "local": {"action_ledger": [row]}},
                      llm=fake_llm, critic=True)


def test_the_next_brief_counts_an_unchanged_overdue_loop_instead_of_naming_it(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    today = cb._user_local_date("+00:00")
    yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    row = _owed(deadline=(datetime.date.fromisoformat(today) - datetime.timedelta(days=3)).isoformat())

    first = _compose(monkeypatch, tmp_path, row)
    assert "Ines Okafor" in first["brief_markdown"]
    effect = first["_brief_named"]
    assert effect["loop_versions"] == {"loop:ines": loop_version(row)}
    assert not brief_marker.accepted_named(today, "morning")

    assert finalize([{**effect, "day": yesterday, "edition": "evening"}], {"accepted_at": 1})
    second = _compose(monkeypatch, tmp_path, row)
    assert "Ines Okafor" not in second["brief_markdown"]
    assert "- 1 other open loop. Ask me what's still open." in second["brief_markdown"]
    assert second["_brief_named"]["anchor_keys"] == []
    assert second["_brief_named"]["loop_versions"] == {"loop:ines": loop_version(row)}

    changed = _owed(deadline=row["deadline"], summary="the venue contract and the deposit")
    assert "Ines Okafor" in _compose(monkeypatch, tmp_path, changed)["brief_markdown"]


def test_a_claim_without_provider_acceptance_cannot_suppress_anything(tmp_path, monkeypatch):
    from proactive_scan import _brief_named_keys, _recent_brief_delivered, _brief_delivered_today

    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    now = datetime.datetime.now(datetime.timezone.utc)
    day = now.date().isoformat()
    (briefs / f"{day}.morning.delivered").write_text("a" * 32)
    (briefs / f"{day}.morning.named.json").write_text(json.dumps({
        "anchor_keys": ["loop:ines"], "loop_versions": {"loop:ines": "v1"}}))
    assert _brief_named_keys(now) == set()
    assert not _brief_delivered_today(now)
    assert not _recent_brief_delivered(now)
    assert brief_marker.told_before(day, "evening") == {}


def test_final_brief_drops_an_unchanged_overdue_ask_the_model_repeats(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setenv("SOTTO_CRITIC", "off")
    today = datetime.date.fromisoformat(cb._user_local_date("+00:00"))
    yesterday = today - datetime.timedelta(days=1)
    row = _owed(deadline=str(today - datetime.timedelta(days=3)))
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / f"{yesterday}.evening.delivered").touch()
    (briefs / f"{yesterday}.evening.named.json").write_text(json.dumps({
        "anchor_keys": ["loop:ines"], "loop_versions": {"loop:ines": loop_version(row)},
        "accepted_at": datetime.datetime.now(datetime.timezone.utc).timestamp()}))
    md = _compose(monkeypatch, tmp_path, row,
                  "## Needs Attention Now\n- Reply to Ines Okafor about the venue contract (email).\n")["brief_markdown"]
    assert "Ines Okafor" not in md
    assert "1 other open loop" in md

def test_only_the_latest_composition_of_the_accepted_run_counts(monkeypatch, tmp_path):
    from delivery_effects import _path

    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    accepted_run, other_run = "a" * 32, "b" * 32
    first = dict(kind="brief_named", day=TODAY, edition="morning", anchor_keys=["first"], loop_versions={})
    winner = dict(first, anchor_keys=["second"])
    assert stage([first], run_id=accepted_run)
    assert stage([winner], run_id=accepted_run)
    assert stage([dict(first, anchor_keys=["other"])], run_id=other_run)
    assert not brief_marker.accepted_named(TODAY, "morning")
    effects = json.loads(Path(_path(accepted_run)).read_text())["effects"]
    assert not finalize(effects, {})
    assert finalize(effects, {"accepted_at": 2})
    assert finalize([first], {"accepted_at": 1})
    assert brief_marker.accepted_named(TODAY, "morning")["anchor_keys"] == ["second"]
