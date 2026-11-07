"""One-shot intentions: durable, idempotent, conditional, and serviced by the existing heartbeat."""
import importlib.util
import json
import os
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *parts))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sw = _load("schedule_wakeup_test", "_shared", "scripts", "schedule_wakeup.py")
ps = _load("proactive_intentions_test", "proactive", "scripts", "proactive_scan.py")


def test_create_is_idempotent_and_cancel_is_a_transition(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    first = sw.create("2026-08-27T15:00:00-07:00", "Check on the deck", "Board deck")
    duplicate = sw.create("2026-08-27T15:00:00-07:00", "Check on the deck", "Board deck")
    assert duplicate["id"] == first["id"] and duplicate["duplicate"] is True
    assert len(sw.current()) == 1
    assert sw.transition(first["id"], "canceled")["status"] == "canceled"
    assert sw.due(datetime(2026, 8, 28, tzinfo=timezone.utc)) == []


def test_due_compares_real_instants_across_offsets(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    item = sw.create("2026-08-27T15:00:00-07:00", "Check on Sarah")
    assert sw.due(datetime(2026, 8, 27, 21, 59, tzinfo=timezone.utc)) == []
    assert [row["id"] for row in sw.due(datetime(2026, 8, 27, 22, 0, tzinfo=timezone.utc))] == [item["id"]]


def test_create_rejects_an_ambiguous_naive_time(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    with pytest.raises(ValueError, match="timezone"):
        sw.create("2026-08-27T15:00:00", "Check on Sarah")


def test_dedupe_keeps_distinct_loop_conditions(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    first = sw.create("2026-08-27T15:00:00-07:00", "Check for a reply", anchor_key="loop_a")
    second = sw.create("2026-08-27T15:00:00-07:00", "Check for a reply", anchor_key="loop_b")
    assert first["id"] != second["id"]


def test_corrupt_log_fails_closed_instead_of_forgetting_existing_intentions(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    path = tmp_path / "intentions.jsonl"
    path.write_text(json.dumps({"id": "int_existing", "status": "scheduled"}) + "\nnot-json\n")
    try:
        sw.create("2026-08-27T15:00:00-07:00", "Create a duplicate by mistake")
        assert False, "corrupt logs must stop writes"
    except ValueError:
        pass
    assert path.read_text().splitlines()[-1] == "not-json"


def test_proactive_scan_turns_due_intention_into_one_nudge():
    now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
    item = {"id": "int_1", "action": "Review the deck", "context": "Before the partner meeting"}
    out = ps.scan([], [], {}, "", now, intentions=[item])
    assert out["nudges"] == [{"kind": "intention", "key": "intention:int_1",
                               "intention_id": "int_1", "title": "Review the deck",
                               "detail": "Before the partner meeting"}]


def test_conditional_intention_auto_cancels_when_loop_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    item = sw.create("2026-08-20T15:00:00+00:00", "Check whether Sarah replied",
                     anchor_key="loop_sarah")
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    assert ps._intention_candidates(now, [{"anchor_key": "loop_sarah", "status": "resolved"}]) == []
    assert next(row for row in sw.current() if row["id"] == item["id"])["status"] == "canceled"


def test_conditional_intention_survives_while_loop_is_open(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    item = sw.create("2026-08-20T15:00:00+00:00", "Check whether Sarah replied",
                     anchor_key="loop_sarah")
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    out = ps._intention_candidates(now, [{"anchor_key": "loop_sarah"}])
    assert [row["id"] for row in out] == [item["id"]]
    assert sw.current()[0]["status"] == "scheduled"


def test_production_anchor_view_includes_things_the_user_is_waiting_on(tmp_path, monkeypatch):
    import source_context
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    row, cr = _source_loop()
    item = sw.create("2026-08-20T15:00:00+00:00", "Check whether Sarah replied", anchor_key=row['anchor_key'])
    monkeypatch.setattr(source_context, 'allowed', lambda source: True)
    out = ps._intention_candidates(datetime(2026, 8, 27, tzinfo=timezone.utc))
    assert [row["id"] for row in out] == [item["id"]]


def test_conditional_intention_waits_when_ledger_is_unavailable(tmp_path, monkeypatch):
    import protected_ledger_io
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    item = sw.create("2026-08-20T15:00:00+00:00", "Check whether Sarah replied",
                     anchor_key="email:waiting_on:id:sarah")
    def unavailable(*_args, **_kwargs):
        raise protected_ledger_io.ProtectedLedgerUnavailableError("volume unavailable")
    monkeypatch.setattr(protected_ledger_io, "load_protected_scope", unavailable)
    assert ps._intention_candidates(datetime(2026, 8, 27, tzinfo=timezone.utc)) == []
    assert sw.current()[0]["id"] == item["id"]
    assert sw.current()[0]["status"] == "scheduled"


# ── PR #817 review: ordinary anchored reminders keep main's rule (deliver while the loop is
#    open, cancel once it is closed or gone), whatever wrote the loop. Every case here matches
#    origin/main, where open anchors came from `loops_query.query()`. ──────────────────────────

NOW_817 = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)


def test_reminder_on_a_hand_added_loop_is_delivered(tmp_path, monkeypatch):
    import knowledge_edit as ke
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    anchor = ke.op_loop_add("Send Dana the deck", contact="Dana",
                            now=datetime(2026, 8, 26, tzinfo=timezone.utc))["anchor_key"]
    item = sw.create("2026-08-27T09:00:00+00:00", "Check you sent Dana the deck", anchor_key=anchor)
    assert [row["id"] for row in ps._intention_candidates(NOW_817)] == [item["id"]]


def test_reminder_on_a_brief_captured_imessage_loop_is_delivered(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    cr = _load("cr_intentions_817", "morning-brief", "scripts", "continuity_resolve.py")
    out = cr.resolve({"today": "2026-08-25", "new_actions": [
        {"type": "waiting_on", "channel": "imessage", "contactName": "Jo",
         "contactIdentifier": "+14155551234", "threadId": "iMessage;-;+14155551234",
         "contextSummary": "Jo to send the signed lease",
         "sourceRefs": [{"sourceType": "imessage", "sourceId": "guid-1"}]}]}, datetime(2026, 8, 25, 9))
    anchor = out["active"][0]["anchor_key"]
    item = sw.create("2026-08-27T09:00:00+00:00", "Check whether Jo sent the lease", anchor_key=anchor)
    assert [row["id"] for row in ps._intention_candidates(NOW_817)] == [item["id"]]


def test_reminder_on_a_pruned_loop_is_canceled(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    _source_loop()                                  # some other loop exists; this one is gone
    sw.create("2026-08-20T15:00:00+00:00", "Check whether Sarah replied",
              anchor_key="email:waiting_on:id:sarah-gone")
    assert ps._intention_candidates(NOW_817) == []
    assert sw.current()[0]["status"] == "canceled"


def test_one_malformed_ledger_file_holds_no_other_reminder(tmp_path, monkeypatch):
    import knowledge_edit as ke
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    anchor = ke.op_loop_add("Send Dana the deck", contact="Dana", direction="them",
                            now=datetime(2026, 8, 26, tzinfo=timezone.utc))["anchor_key"]
    ledger = tmp_path / "knowledge" / "continuity"
    (ledger / "junk.md").write_text("not frontmatter")
    (ledger / "half.md").write_text("---\nanchor_key: [unclosed\n---\n")
    item = sw.create("2026-08-27T09:00:00+00:00", "Check Dana", anchor_key=anchor)
    gone = sw.create("2026-08-27T09:00:00+00:00", "Check Sam", anchor_key="email:waiting_on:id:sam")
    assert [row["id"] for row in ps._intention_candidates(NOW_817)] == [item["id"]]
    # A malformed file is no loop anywhere in Sotto (the canonical parser skips it), so a
    # reminder whose loop is gone still cancels, as on main.
    assert {row["id"]: row["status"] for row in sw.current()}[gone["id"]] == "canceled"


def test_an_untrusted_ledger_row_stops_absence_from_canceling(tmp_path, monkeypatch):
    import knowledge_edit as ke
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    anchor = ke.op_loop_add("Send Dana the deck", contact="Dana", direction="them",
                            now=datetime(2026, 8, 26, tzinfo=timezone.utc))["anchor_key"]
    # Every other reader would see this row (last duplicate key wins); the protected reader
    # will not trust it, so it cannot prove Sam's loop is gone either.
    (tmp_path / "knowledge" / "continuity" / "dup.md").write_text(
        "---\nanchor_key: email:waiting_on:id:other\nanchor_key: email:waiting_on:id:sam\n"
        "status: open\n---\n")
    item = sw.create("2026-08-27T09:00:00+00:00", "Check Dana", anchor_key=anchor)
    held = sw.create("2026-08-27T09:00:00+00:00", "Check Sam", anchor_key="email:waiting_on:id:sam")
    assert [row["id"] for row in ps._intention_candidates(NOW_817)] == [item["id"]]
    assert {row["id"]: row["status"] for row in sw.current()}[held["id"]] == "scheduled"


def test_reminder_on_a_closed_or_snoozed_loop_is_canceled_as_on_main(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    row, cr = _source_loop()
    first = sw.create("2026-08-20T15:00:00+00:00", "Check Dana replied", anchor_key=row["anchor_key"])
    row["snoozed_until"] = "2099-01-01"
    cr._persist(row)
    assert ps._intention_candidates(NOW_817) == []
    assert sw.current()[0]["status"] == "canceled" and sw.current()[0]["id"] == first["id"]


def test_reminder_on_an_owner_proved_loop_waits_while_snoozed_then_fires(tmp_path, monkeypatch):
    import knowledge_edit as ke
    cr = _load("cr_intentions_owner_817", "morning-brief", "scripts", "continuity_resolve.py")
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    anchor = ke.op_loop_add_admitted_owner_text("Send the signed deck", owner_text_span="Send the signed deck",
                                                owner_event_id="evt-1", now=datetime(2026, 8, 26, tzinfo=timezone.utc))["anchor_key"]
    item = sw.create("2026-08-27T09:00:00+00:00", "Check the deck went", anchor_key=anchor)
    with cr._ledger_lock():
        row = cr._load_items()[anchor]
        row["snoozed_until"] = "2026-08-29"
        cr._persist(row)                          # bookkeeping: the row stays proved
    assert ps._intention_candidates(NOW_817) == []
    assert sw.current()[0]["status"] == "scheduled"
    assert [entry["id"] for entry in ps._intention_candidates(datetime(2026, 8, 30, 12, tzinfo=timezone.utc))] == [item["id"]]


def _source_loop():
    cr = _load('conditional_actual_continuity', 'morning-brief', 'scripts', 'continuity_resolve.py')
    row = {'anchor_key': 'gmail:waiting_on:cedar', 'status': 'open', 'source': 'gmail',
           'source_message_id': 'cedar-message', 'source_thread_id': 'cedar-thread',
           'source_refs': [{'sourceType': 'gmail', 'sourceId': 'cedar-message'}],
           'contact_name': 'Dana', 'contact_identifier': 'dana@example.com',
           'action_type': 'waiting_on', 'summary': 'Send Cedar deck', 'created_at': '2026-08-20'}
    cr._persist(row)
    return row, cr


def test_new_conditional_due_identity_current_grants_and_one_accepted_effect(tmp_path, monkeypatch):
    from delivery_effects import loop_identity, valid, finalize
    import ask_research_pipeline
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    row, cr = _source_loop()
    binding = {'anchor_key': row['anchor_key'], 'loop_identity': loop_identity(row), 'sources': ['gmail']}
    item = sw.create('2026-08-20T15:00:00Z', 'Check Dana reply', anchor_key=row['anchor_key'])
    item['condition_binding'] = binding
    sw._write_row(item)
    authority = {'complete': True, 'revision': 'e'*64, 'google_sources': ['gmail'], 'bridge_sources': [], 'connector_sources': []}
    monkeypatch.setattr(ask_research_pipeline, 'current_authority', lambda: authority)
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    candidates = ps._intention_candidates(now)
    assert [entry['id'] for entry in candidates] == [item['id']]
    output = ps.scan([], [], {}, '', now, intentions=candidates)['nudges']
    assert len(output) == 1 and output[0]['intention_id'] == item['id']
    effect = [{'kind': 'eligibility', 'intention_id': item['id']}]
    assert valid(effect, now.timestamp())
    # Grant loss blocks both the due candidate and already prepared output.
    authority['google_sources'] = []
    assert ps._intention_candidates(now) == [] and not valid(effect, now.timestamp())
    assert sw.current()[0]['status'] == 'scheduled'
    authority['google_sources'] = ['gmail']
    row['snoozed_until'] = '2026-08-30'; cr._persist(row)
    assert ps._intention_candidates(now) == [] and sw.current()[0]['status'] == 'scheduled'
    row.pop('snoozed_until'); row['summary'] = 'Send the updated Cedar deck'; cr._persist(row)
    assert [entry['id'] for entry in ps._intention_candidates(now)] == [item['id']]
    assert finalize([{'kind': 'intention', 'id': item['id']}], {'accepted_at': now.isoformat()})
    assert sw.current()[0]['status'] == 'fired'
    assert ps._intention_candidates(now) == []
    # Already accepted effects replay without any second provider contact.
    assert finalize([{'kind': 'intention', 'id': item['id']}], {'accepted_at': now.isoformat()})


def test_new_condition_terminal_only_missing_ledger_and_ordinary_independence(tmp_path, monkeypatch):
    from delivery_effects import loop_identity
    import ask_research_pipeline
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    row, cr = _source_loop()
    item = sw.create('2026-08-20T15:00:00Z', 'Check Dana reply', anchor_key=row['anchor_key'])
    item['condition_binding'] = {'anchor_key': row['anchor_key'], 'loop_identity': loop_identity(row), 'sources': ['gmail']}
    sw._write_row(item)
    ordinary = sw.create('2026-08-20T15:00:00Z', 'Review ordinary deck')
    authority = {'complete': True, 'revision': 'e'*64, 'google_sources': ['gmail']}
    monkeypatch.setattr(ask_research_pipeline, 'current_authority', lambda: authority)
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    path = next((tmp_path/'knowledge/continuity').glob('*.md'))
    path.unlink()
    assert [entry['id'] for entry in ps._intention_candidates(now)] == [ordinary['id']]
    assert next(entry for entry in sw.current() if entry['id']==item['id'])['status'] == 'scheduled'
    cr._persist(row)
    row['status'] = 'resolved'; cr._persist(row)
    authority['complete'] = False
    assert [entry['id'] for entry in ps._intention_candidates(now)] == [ordinary['id']]
    assert next(entry for entry in sw.current() if entry['id']==item['id'])['status'] == 'scheduled'
    authority['complete'] = True
    assert [entry['id'] for entry in ps._intention_candidates(now)] == [ordinary['id']]
    assert next(entry for entry in sw.current() if entry['id']==item['id'])['status'] == 'canceled'
    path.write_text('broken ledger')
    assert [entry['id'] for entry in ps._intention_candidates(now)] == [ordinary['id']]


def test_an_unrelated_bad_ledger_file_holds_no_bound_condition(tmp_path, monkeypatch):
    """PR #817 review: the strict whole-ledger read failed on any one malformed file, so every
    owner-bound reminder stopped firing and every prepared one failed its delivery check."""
    from delivery_effects import loop_identity, valid
    import ask_research_pipeline
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    row, _cr = _source_loop()
    item = sw.create('2026-08-20T15:00:00Z', 'Check Dana reply', anchor_key=row['anchor_key'])
    item['condition_binding'] = {'anchor_key': row['anchor_key'], 'loop_identity': loop_identity(row), 'sources': ['gmail']}
    sw._write_row(item)
    authority = {'complete': True, 'revision': 'e'*64, 'google_sources': ['gmail'], 'bridge_sources': [], 'connector_sources': []}
    monkeypatch.setattr(ask_research_pipeline, 'current_authority', lambda: authority)
    ledger = tmp_path / 'knowledge' / 'continuity'
    (ledger / 'junk.md').write_text('not frontmatter')
    (ledger / 'dup.md').write_text('---\nanchor_key: other\nanchor_key: other-2\n---\n')
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    assert [entry['id'] for entry in ps._intention_candidates(now)] == [item['id']]
    assert valid([{'kind': 'eligibility', 'intention_id': item['id']}], now.timestamp())


def test_conditional_full_union_generation_bookend_and_local_snooze(tmp_path, monkeypatch):
    from delivery_effects import loop_identity
    import ask_research_pipeline
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    row, cr = _source_loop()
    row['source_refs'].append({'sourceType':'granola','sourceId':'meeting-cedar'})
    cr._persist(row)
    binding = {'anchor_key': row['anchor_key'], 'loop_identity': loop_identity(row), 'sources': ['gmail', 'granola']}
    authority = {'complete': True, 'revision': 'e'*64, 'google_sources': ['gmail'], 'connector_sources': ['granola']}
    monkeypatch.setattr(ask_research_pipeline, 'current_authority', lambda: authority)
    now = datetime(2026, 8, 27, 1, tzinfo=timezone.utc)  # still Aug 26 in owner zone
    row['snoozed_until'] = '2026-08-27'; cr._persist(row)
    assert sw.conditional_current_state(binding, now=now)[0] == 'held'
    row.pop('snoozed_until'); row['status'] = 'parked'; cr._persist(row)
    assert sw.conditional_current_state(binding, now=now)[0] == 'held'
    row['status'] = 'open'; cr._persist(row)
    authority['connector_sources'] = []
    with pytest.raises(PermissionError):
        sw.conditional_current_state(binding, now=now)
    authority['connector_sources'] = ['granola']
    # A new generation/revision between read bookends must not authorize a use.
    captures = iter([dict(authority), {**authority, 'revision': 'f'*64}])
    monkeypatch.setattr(ask_research_pipeline, 'current_authority', lambda: next(captures))
    with pytest.raises(PermissionError):
        sw.conditional_current_state(binding, now=now)
