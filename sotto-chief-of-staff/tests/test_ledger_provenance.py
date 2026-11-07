"""Offline provenance fixtures; no grant or ledger writer is changed."""
from __future__ import annotations

from datetime import date
import unittest

import yaml

import ledger_io
import ledger_provenance as projection


def parsed(row):
    """Exercise the actual ledger frontmatter parser and PyYAML scalar behavior."""
    return ledger_io.parse_frontmatter("---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")


def with_provenance(row, kind="sources", sources=()):
    # A test-only stand-in for a future audited writer. The classifier
    # cannot create this marker or backfill it onto existing records.
    row = parsed(row)
    row["content_provenance"] = {"version": 1, "kind": kind,
                                 "sources": sorted(sources),
                                 "digest": projection.digest_frontmatter(row)}
    return parsed(row)


def base(**extra):
    return {"anchor_key": "thread:one", "status": "open", "action_type": "reply",
            "channel": "email", "contact_name": "Owner", "summary": "Send Q3 deck",
            "created_at": "2026-09-01", **extra}


class ProjectionTests(unittest.TestCase):
    def visible(self, rows, allowed):
        return projection.project_entries(rows, lambda source: source in allowed)

    def test_every_legacy_row_is_withheld_including_explicit_source_ref(self):
        imessage = parsed(base(channel="imessage", source_refs={"sourceType": "imessage", "sourceId": "m1"}))
        manual = parsed(base(channel="manual", source="user_added", source_thread_id="manual:abc"))
        gmail = parsed(base(source_refs={"sourceType": "email", "sourceId": "m2"}, source_thread_id="t1"))
        self.assertEqual(self.visible([imessage, manual, gmail], {"imessage", "gmail"}), [])

    def test_versioned_imessage_and_snake_case_whatsapp_require_every_grant(self):
        row = with_provenance(base(channel="imessage", source_refs=[
            {"sourceType": "imessage", "sourceId": "m1"},
            {"source_type": "whatsapp", "source_id": "m2"}]),
            sources=("imessage", "whatsapp"))
        self.assertEqual(self.visible([row], {"imessage"}), [])
        self.assertEqual(self.visible([row], {"imessage", "whatsapp"}), [row])

    def test_email_alias_and_thread_are_gmail_not_channel_authority(self):
        row = with_provenance(base(source_refs={"sourceType": "apple_mail", "sourceId": "m1"},
                          source_thread_id="opaque-thread", source_message_id="m1"), sources=("gmail",))
        self.assertEqual(self.visible([row], {"gmail"}), [row])
        self.assertEqual(self.visible([row], set()), [])
        channel_only = with_provenance(base(source_thread_id="opaque-thread"), sources=("gmail",))
        self.assertEqual(self.visible([channel_only], {"gmail"}), [])

    def test_granola_commitment_not_misclassified_as_email(self):
        row = with_provenance(base(source="followup_commitment", source_thread_id="granola:meeting-1:hash",
                          source_refs=[{"source": "granola", "source_id": "meeting-1", "snippet": "I'll send it"}]),
                     sources=("granola",))
        self.assertEqual(self.visible([row], {"gmail"}), [])
        self.assertEqual(self.visible([row], {"granola"}), [row])
        legacy = with_provenance(base(source="followup_commitment", source_thread_id="commitment:hash"), sources=("granola",))
        self.assertEqual(self.visible([legacy], {"granola"}), [])

    def test_calendar_and_completion_refs_union(self):
        row = with_provenance(base(channel="calendar", source_refs={"sourceType": "calendar", "sourceId": "event-1"},
                          resolution_source_refs={"sourceType": "email", "sourceId": "sent-1"}),
                     sources=("calendar", "gmail"))
        self.assertEqual(self.visible([row], {"calendar"}), [])
        self.assertEqual(self.visible([row], {"calendar", "gmail"}), [row])

    def test_email_thread_keeps_original_authority_when_closed_on_another_channel(self):
        row = with_provenance(base(source_thread_id='gmail-thread', source_message_id='original-mail',
            source_refs=[{'sourceType': 'email', 'sourceId': 'original-mail'}],
            resolution_source_refs=[{'sourceType': 'imessage', 'sourceId': 'later-text'}]),
            sources=('gmail', 'imessage'))
        self.assertEqual(self.visible([row], {'gmail', 'imessage'}), [row])
        self.assertEqual(self.visible([row], {'gmail'}), [])
        self.assertEqual(self.visible([row], {'imessage'}), [])

    def test_closing_email_cannot_authorize_unbound_original_thread_or_message(self):
        for identity in ({'source_thread_id': 'unbound-thread'}, {'source_message_id': 'unbound-message'}):
            row = with_provenance(base(channel='imessage', **identity,
                source_refs=[{'sourceType': 'imessage', 'sourceId': 'original-text'}],
                resolution_source_refs=[{'sourceType': 'email', 'sourceId': 'later-mail'}]),
                sources=('gmail', 'imessage'))
            self.assertEqual(self.visible([row], {'gmail', 'imessage'}), [])

    def test_unknown_malformed_and_conflicting_refs_fail_closed(self):
        rows = [
            with_provenance(base(source_refs=[{"sourceType": "imessage", "sourceId": "m1"},
                                     {"sourceType": "mystery", "sourceId": "m2"}]), sources=("imessage",)),
            with_provenance(base(source_refs=["not-a-dict"]), sources=("imessage",)),
            with_provenance(base(source_refs={"sourceType": "imessage", "source": "whatsapp", "sourceId": "m1"}), sources=("imessage",)),
            with_provenance(base(source_refs={"sourceType": "imessage", "sourceId": ""}), sources=("imessage",)),
        ]
        self.assertEqual(self.visible(rows, {"imessage", "whatsapp"}), [])

    def test_digest_rejects_changed_displayed_field_and_parser_normalizes_dates(self):
        row = with_provenance(base(channel="imessage", source_refs={"sourceType": "imessage", "sourceId": "m1"},
                          created_at=date(2026, 9, 1)), sources=("imessage",))
        self.assertEqual(self.visible([row], {"imessage"}), [row])
        edited = {**row, "summary": "text from newly revoked Gmail"}
        self.assertEqual(self.visible([edited], {"imessage", "gmail"}), [])
        changed_status = {**row, "status": "parked"}
        self.assertEqual(self.visible([changed_status], {"imessage"}), [])
        self.assertEqual(projection.digest_frontmatter({**row, "_path": "/untrusted/a.md"}),
                         projection.digest_frontmatter(row))
        self.assertEqual(projection.digest_frontmatter(dict(reversed(list(row.items())))),
                         projection.digest_frontmatter(row))

    def test_typed_scalar_cannot_collide_with_ordinary_marker_shaped_dict(self):
        typed = parsed(base(created_at=date(2026, 9, 1),
                            source_refs={"sourceType": "imessage", "sourceId": "m1"}))
        marker = {**typed, "created_at": {"$date": "2026-09-01"}}
        self.assertNotEqual(projection.digest_frontmatter(typed), projection.digest_frontmatter(marker))
        typed_float = {**typed, "confidence": 0.7}
        marker_float = {**typed, "confidence": {"$float": "0.7"}}
        self.assertNotEqual(projection.digest_frontmatter(typed_float), projection.digest_frontmatter(marker_float))
        typed_datetime = {**typed, "observed_at": __import__("datetime").datetime(2026, 9, 1, 12)}
        marker_datetime = {**typed, "observed_at": {"$datetime": "2026-09-01T12:00:00"}}
        self.assertNotEqual(projection.digest_frontmatter(typed_datetime), projection.digest_frontmatter(marker_datetime))

    def test_nested_global_budget_rejects_before_full_encoding(self):
        # Each branch is shallow and locally bounded; aggregate bytes exceed 64 KiB.
        oversized = {"rows": [{"note": "x" * 200} for _ in range(600)]}
        with self.assertRaises(projection.UnknownProvenanceError):
            projection.digest_frontmatter(oversized)
        too_many_nodes = {"rows": [{"small": i} for i in range(1024)]}
        with self.assertRaises(projection.UnknownProvenanceError):
            projection.digest_frontmatter(too_many_nodes)

    def test_top_level_and_scalar_bounds_precede_copy_or_encoding(self):
        for oversized in ({str(i): i for i in range(1028)},
                          {"summary": "x" * 8193}, {"x" * 8193: "value"}):
            with self.assertRaises(projection.UnknownProvenanceError):
                projection.digest_frontmatter(oversized)

    def test_claimed_source_types_fail_as_unknown_provenance(self):
        row = with_provenance(base(channel="imessage", source_refs={"sourceType": "imessage", "sourceId": "m1"}),
                     sources=("imessage",))
        for malformed in ([{}], [42], ["imessage", {}], ["imessage", "imessage"], ["imessage"] * 6):
            bad = {**row, "content_provenance": {**row["content_provenance"], "sources": malformed}}
            with self.subTest(malformed=malformed), self.assertRaises(projection.UnknownProvenanceError):
                projection.classify_ledger_sources(bad)
            self.assertEqual(self.visible([bad], {"imessage"}), [])

    def test_versioned_owner_only_is_narrow_and_a_source_merge_invalidates_it(self):
        owner = with_provenance(base(channel="manual", source="user_added", source_thread_id="manual:abc"),
                       kind="owner")
        self.assertEqual(self.visible([owner], set()), [owner])
        merged = {**owner, "source_refs": [{"source": "granola", "source_id": "m1"}]}
        self.assertEqual(self.visible([merged], {"granola"}), [])
        recaptured = {**owner, "source_brief_at": "2026-09-30"}
        self.assertEqual(self.visible([recaptured], set()), [])
        not_manual = with_provenance(base(source="user_added", source_thread_id="manual:abc"), kind="owner")
        self.assertEqual(self.visible([not_manual], set()), [])

    def test_authority_failure_is_not_a_partial_response(self):
        row = with_provenance(base(channel="imessage", source_refs={"sourceType": "imessage", "sourceId": "m1"}),
                     sources=("imessage",))
        def unavailable(_source):
            raise RuntimeError("authority unavailable")
        with self.assertRaises(RuntimeError):
            projection.project_entries([row], unavailable)

    def test_value_error_authority_failure_after_visible_rows_still_aborts_whole_projection(self):
        first = with_provenance(base(channel="imessage", source_refs={"sourceType": "imessage", "sourceId": "m1"}),
                                sources=("imessage",))
        owner = with_provenance(base(channel="manual", source="user_added", source_thread_id="manual:abc"),
                                kind="owner")
        second = with_provenance(base(channel="whatsapp", source_refs={"sourceType": "whatsapp", "sourceId": "m2"}),
                                 sources=("whatsapp",))
        calls = []
        def permit(source):
            calls.append(source)
            if source == "whatsapp":
                raise ValueError("authority changed")
            return True
        with self.assertRaises(ValueError):
            projection.project_entries([first, owner, second], permit)
        self.assertEqual(calls, ["imessage", "whatsapp"])


if __name__ == "__main__":
    unittest.main()
