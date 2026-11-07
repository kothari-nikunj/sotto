from datetime import datetime, timedelta, timezone
import hashlib
import shutil

import pytest

import continuity_resolve as cr  # noqa: E402
import knowledge_edit as ke  # noqa: E402
import ledger_io  # noqa: E402

NOW = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    return tmp_path


def test_same_event_replay_after_rollover_returns_original_row(data_root):
    first = ke.op_loop_add_admitted_owner_text(
        "Send the signed deck", owner_text_span="Send the signed deck",
        owner_event_id="evt-rollover", now=NOW,
    )
    replay = ke.op_loop_add_admitted_owner_text(
        "Send the signed deck", owner_text_span="Send the signed deck",
        owner_event_id="evt-rollover", now=NOW + timedelta(days=1),
    )
    assert replay == {"ok": True, "anchor_key": first["anchor_key"], "created": False}
    rows = ledger_io.load_entries()
    assert len([row for row in rows if row.get("owner_event_id") == "evt-rollover"]) == 1


def test_same_event_with_different_text_conflicts(data_root):
    ke.op_loop_add_admitted_owner_text(
        "Send the signed deck", owner_text_span="Send the signed deck",
        owner_event_id="evt-conflict", now=NOW,
    )
    with pytest.raises(ke.EditError, match="conflicts"):
        ke.op_loop_add_admitted_owner_text(
            "Send the signed invoice", owner_text_span="Send the signed invoice",
            owner_event_id="evt-conflict", now=NOW + timedelta(days=1),
        )


def test_malformed_ledger_file_fails_closed_before_new_event(data_root):
    ledger_dir = data_root / "knowledge" / "continuity"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / "broken.md").write_text("---\nthis: [is malformed\n", encoding="utf-8")
    with pytest.raises(ke.EditError, match="ledger is unavailable"):
        ke.op_loop_add_admitted_owner_text(
            "Send the signed deck", owner_text_span="Send the signed deck",
            owner_event_id="evt-malformed", now=NOW,
        )
    assert list(ledger_dir.glob("*.md")) == [ledger_dir / "broken.md"]


def test_duplicate_yaml_event_key_fails_strict_scan_while_ordinary_parser_is_unchanged(data_root):
    first = ke.op_loop_add_admitted_owner_text(
        "Send the signed deck", owner_text_span="Send the signed deck",
        owner_event_id="evt-duplicate-key", now=NOW,
    )
    path = next(data_root.glob('knowledge/continuity/*.md'))
    content = path.read_text(encoding='utf-8').replace(
        'owner_event_id: evt-duplicate-key\n',
        'owner_event_id: evt-duplicate-key\nowner_event_id: evt-other\n',
    )
    assert first['anchor_key']
    # Existing non-protected readers retain their historical parser behavior.
    assert ledger_io.parse_frontmatter(content)['owner_event_id'] == 'evt-other'
    path.write_text(content, encoding='utf-8')
    with pytest.raises(ke.EditError, match='ledger is unavailable'):
        ke.op_loop_add_admitted_owner_text(
            "Send the signed deck", owner_text_span="Send the signed deck",
            owner_event_id="evt-duplicate-key", now=NOW + timedelta(days=1),
        )
    assert len(list(data_root.glob('knowledge/continuity/*.md'))) == 1


def test_shared_manual_action_keeps_legacy_anchor_and_row_fields(data_root):
    text = 'Send the deck'
    contact = 'Ada'
    identifier = 'ada@example.com'
    day = NOW.strftime('%Y-%m-%d')
    seed = hashlib.sha256(f'them|{contact}|{text}|{day}'.encode()).hexdigest()[:12]
    legacy_action = cr._normalize_action({
        'action_type': 'waiting_on', 'channel': 'manual', 'contact_name': contact,
        'contact_identifier': identifier, 'source_thread_id': f'manual:{seed}',
        'summary': text, 'deadline': '2026-08-15',
        'created_at': NOW.strftime('%Y-%m-%d %H:%M:%S'),
    })
    expected_anchor = cr.compute_anchor_key(legacy_action)
    action, anchor = ke._manual_loop_action(text, contact, identifier, '2026-08-15',
                                            'them', NOW, cr)
    assert action == legacy_action and anchor == expected_anchor
    created = ke.op_loop_add(text, contact=contact, identifier=identifier,
                             deadline='2026-08-15', direction='them', now=NOW)
    row = next(row for row in ledger_io.load_entries() if row['anchor_key'] == anchor)
    assert created == {'ok': True, 'anchor_key': expected_anchor, 'created': True}
    assert row['source_thread_id'] == f'manual:{seed}'
    assert row['summary'] == text and row['deadline'] == '2026-08-15'
    assert row['action_type'] == 'waiting_on' and row['contact_name'] == contact


def test_symlink_ledger_file_fails_closed(data_root, tmp_path):
    ledger_dir = data_root / "knowledge" / "continuity"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside.md"
    target.write_text("---\nanchor_key: outside\n---\n", encoding="utf-8")
    (ledger_dir / "linked.md").symlink_to(target)
    with pytest.raises(ke.EditError, match="ledger is unavailable"):
        ke.op_loop_add_admitted_owner_text(
            "Send the signed deck", owner_text_span="Send the signed deck",
            owner_event_id="evt-symlink", now=NOW,
        )


def test_invalidated_event_proof_is_held_without_rewrite(data_root):
    first = ke.op_loop_add_admitted_owner_text(
        "Send the signed deck", owner_text_span="Send the signed deck",
        owner_event_id="evt-tampered", now=NOW,
    )
    row = next(row for row in ledger_io.load_entries()
               if row.get("anchor_key") == first["anchor_key"])
    row["summary"] = "Altered after original owner event"
    cr._persist(row)
    with pytest.raises(ke.EditError, match="invalid provenance"):
        ke.op_loop_add_admitted_owner_text(
            "Send the signed deck", owner_text_span="Send the signed deck",
            owner_event_id="evt-tampered", now=NOW + timedelta(days=1),
        )
    saved = next(row for row in ledger_io.load_entries()
                 if row.get("owner_event_id") == "evt-tampered")
    assert saved["summary"] == "Altered after original owner event"
    assert "content_provenance" in saved


def test_resolved_event_is_not_recreated_after_proof_becomes_stale(data_root):
    first = ke.op_loop_add_admitted_owner_text(
        "Send the signed deck", owner_text_span="Send the signed deck",
        owner_event_id="evt-resolved", now=NOW,
    )
    row = next(row for row in ledger_io.load_entries()
               if row.get("anchor_key") == first["anchor_key"])
    cr._terminate(row, "resolved", "user_resolved", NOW.strftime("%Y-%m-%d"))
    cr._persist(row)
    with pytest.raises(ke.EditError, match="invalid provenance"):
        ke.op_loop_add_admitted_owner_text(
            "Send the signed deck", owner_text_span="Send the signed deck",
            owner_event_id="evt-resolved", now=NOW + timedelta(days=2),
        )
    rows = [row for row in ledger_io.load_entries()
            if row.get("owner_event_id") == "evt-resolved"]
    assert len(rows) == 1 and rows[0]["status"] == "resolved"


def test_shadowed_duplicate_event_is_ambiguous_and_not_rewritten(data_root):
    first = ke.op_loop_add_admitted_owner_text(
        "Send the signed deck", owner_text_span="Send the signed deck",
        owner_event_id="evt-shadow", now=NOW,
    )
    original = next(row for row in ledger_io.load_entries()
                    if row.get("anchor_key") == first["anchor_key"])
    # The row remains a second persisted record with the same event identity; the strict scan
    # must see both even though _load_items would classify one as shadowed/ambiguous.
    source_path = next(row["_path"] for row in ledger_io.load_entries(with_path=True)
                       if row.get("anchor_key") == original["anchor_key"])
    shadow_path = data_root / "knowledge" / "continuity" / "zz-shadow-copy.md"
    shutil.copyfile(source_path, shadow_path)
    with pytest.raises(ke.EditError, match="ambiguous"):
        ke.op_loop_add_admitted_owner_text(
            "Send the signed deck", owner_text_span="Send the signed deck",
            owner_event_id="evt-shadow", now=NOW + timedelta(days=1),
        )


def test_independent_event_cannot_backfill_existing_unproved_anchor(data_root):
    generic = ke.op_loop_add("Send the signed deck", now=NOW)
    with pytest.raises(ke.EditError, match="anchor already"):
        ke.op_loop_add_admitted_owner_text(
            "Send the signed deck", owner_text_span="Send the signed deck",
            owner_event_id="evt-independent", now=NOW,
        )
    row = next(row for row in ledger_io.load_entries()
               if row.get("anchor_key") == generic["anchor_key"])
    assert "owner_event_id" not in row
    assert "content_provenance" not in row


def test_independent_event_cannot_stamp_source_backed_anchor(data_root):
    text = "Send the signed deck"
    day = NOW.strftime("%Y-%m-%d")
    seed = hashlib.sha256(f"you||{text}|{day}".encode()).hexdigest()[:12]
    action = cr._normalize_action({
        "action_type": "follow_up", "channel": "manual", "contact_name": "",
        "contact_identifier": None, "source_thread_id": f"manual:{seed}",
        "summary": text, "deadline": None, "created_at": NOW.strftime("%Y-%m-%d %H:%M:%S"),
    })
    row = {
        "anchor_key": cr.compute_anchor_key(action), "action_type": action["action_type"],
        "channel": "manual", "contact_name": "", "status": "open", "summary": text,
        "source": "gmail", "source_thread_id": f"manual:{seed}",
        "source_refs": [{"sourceType": "gmail", "sourceId": "message-1"}],
    }
    cr._persist(row)
    with pytest.raises(ke.EditError, match="anchor already"):
        ke.op_loop_add_admitted_owner_text(
            text, owner_text_span=text, owner_event_id="evt-independent-source",
            now=NOW,
        )
    saved = next(entry for entry in ledger_io.load_entries()
                 if entry.get("anchor_key") == row["anchor_key"])
    assert saved["source"] == "gmail"
    assert "owner_event_id" not in saved
    assert "content_provenance" not in saved


def test_independent_event_after_rollover_gets_new_intent(data_root):
    first = ke.op_loop_add_admitted_owner_text(
        "Send the signed deck", owner_text_span="Send the signed deck",
        owner_event_id="evt-first", now=NOW,
    )
    second = ke.op_loop_add_admitted_owner_text(
        "Send the signed deck", owner_text_span="Send the signed deck",
        owner_event_id="evt-second", now=NOW + timedelta(days=1),
    )
    assert second["created"] is True
    assert second["anchor_key"] != first["anchor_key"]
    assert {row.get("owner_event_id") for row in ledger_io.load_entries()
            if row.get("owner_event_id")} == {"evt-first", "evt-second"}
