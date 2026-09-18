"""Gmail completion proposals across the real brief seam.

These tests prove the deterministic path from rendered Gmail identity through compose normalization
and evidence-bound resolution. They deliberately stub the model: whether a live model proposes the
right update often enough is a proposal-rate question for a paid live eval, not something an offline
unit test can establish.
"""
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
for directory in ("_shared/scripts", "_shared/lib"):
    sys.path.insert(0, str(ROOT / directory))

import compose_brief as cb  # noqa: E402
from delivery_effects import loop_version  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "email_proposal_resolver", ROOT / "morning-brief/scripts/continuity_resolve.py")
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)

NOW = datetime(2026, 6, 24, 9)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "UTC")


def _loop(tmp_path, action_type: str, summary: str) -> dict:
    row = {"anchor_key": "email-loi", "status": "open", "action_type": action_type,
           "channel": "gmail", "contact_name": "Dana", "contact_identifier": "dana@example.com",
           "created_at": "2026-06-23 08:00:00", "summary": summary}
    directory = tmp_path / "knowledge/continuity"
    directory.mkdir(parents=True)
    (directory / (cr._safe(row["anchor_key"]) + ".md")).write_text(
        "---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    return row


def _compose_with_proposal(row: dict, email: dict, source_id: str, snippet: str) -> dict:
    version = loop_version(row)

    def fake_llm(prompt, inputs, system=None, schema=None):
        assert f"loop_id: {row['anchor_key']}" in prompt
        assert f"loop_version: {version}" in prompt
        assert f"MessageId: {email['id']}" in prompt
        assert snippet in prompt
        return json.dumps({
            "markdown": "# Brief\n\nNothing else needs attention.",
            "actionItems": [],
            "loopUpdates": [{"loopId": row["anchor_key"], "loopVersion": version,
                             "status": "resolved", "evidence": [
                                 {"sourceType": "email", "sourceId": source_id,
                                  "snippet": snippet}]}],
        })

    return cb.compose({"type": "morning", "now": "2026-06-24T09:00:00Z",
                       "google": {"emails": [email]},
                       "local": {"action_ledger": [row]}}, llm=fake_llm)


@pytest.mark.parametrize(("action_type", "summary", "email", "resolution"), [
    ("waiting_on", "Dana owes the signed LOI",
     {"id": "gmail-inbound-loi", "threadId": "thread-in", "labelIds": ["INBOX"],
      "from": "Dana <dana@example.com>", "to": "Me <me@example.com>", "subject": "LOI",
      "date": "2026-06-23T22:00:00Z", "body": "I sent the LOI."}, "delivered"),
    ("reply", "Send Dana the signed LOI",
     {"id": "gmail-sent-loi", "threadId": "thread-out", "labelIds": ["SENT"],
      "from": "Me <me@example.com>", "to": "Dana <dana@example.com>", "subject": "LOI",
      "date": "2026-06-23T22:00:00Z", "body": "I sent the LOI."}, "replied"),
])
def test_compose_email_proposal_resolves_only_matching_obligation(
        tmp_path, action_type, summary, email, resolution):
    row = _loop(tmp_path, action_type, summary)
    composed = _compose_with_proposal(row, email, email["id"], "sent the LOI")
    assert composed["loop_updates"][0]["evidence"][0]["sourceId"] == email["id"]

    out = cr.resolve({"today": "2026-06-24", "local": {"emails": [email]},
                      "loop_updates": composed["loop_updates"]}, NOW)
    assert [(item["anchor_key"], item["resolution"]) for item in out["resolved"]] == [
        (row["anchor_key"], resolution)]


def test_compose_unsupported_email_proposal_leaves_obligation_unchanged(tmp_path):
    row = _loop(tmp_path, "reply", "Send Dana the signed LOI")
    email = {"id": "gmail-sent-loi", "threadId": "thread-out", "labelIds": ["SENT"],
             "from": "Me <me@example.com>", "to": "Dana <dana@example.com>", "subject": "LOI",
             "date": "2026-06-23T22:00:00Z", "body": "I sent the LOI."}
    composed = _compose_with_proposal(row, email, "unsupported-message", "sent the LOI")
    out = cr.resolve({"today": "2026-06-24", "local": {"emails": [email]},
                      "loop_updates": composed["loop_updates"]}, NOW)
    assert out["resolved"] == []
    active, = out["active"]
    assert active["anchor_key"] == row["anchor_key"] and active["status"] == "open"
