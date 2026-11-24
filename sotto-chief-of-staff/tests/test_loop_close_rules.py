"""Loop-close rules that a prompt, a merge or a group could silently bend (workstream A, Nov 2026)."""
import importlib.util
import os
import re
from datetime import datetime

import pytest
import yaml
from delivery_effects import loop_version
from render_local import call_evidence

HERE = os.path.dirname(__file__)
PROMPT_PATH = os.path.join(HERE, "..", "morning-brief", "references", "extraction-prompt.md")
_spec = importlib.util.spec_from_file_location(
    "cr_close_rules", os.path.join(HERE, "..", "morning-brief", "scripts", "continuity_resolve.py"))
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)


def _row(tmp_path, monkeypatch, **fields):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    row = {"status": "open", "action_type": "reply", "channel": "imessage", "contact_name": "Dana",
           "contact_identifier": "+12025550173", "created_at": "2026-06-23 08:00:00",
           "summary": "Send Dana the pricing document", **fields}
    row["anchor_key"] = cr.compute_anchor_key(row)
    directory = tmp_path / "knowledge/continuity"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (cr._safe(row["anchor_key"]) + ".md")).write_text(
        "---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    return row


def test_the_prompt_never_offers_a_whatsapp_call_as_callback_completion():
    """Code refuses `whatsapp_calls` completion evidence (its schema cannot prove a connection),
    so the prompt must not ask the model to cite one; a WhatsApp callback needs a relevant message."""
    prompt = open(PROMPT_PATH, encoding="utf-8").read()
    for line in prompt.splitlines():
        if re.search(r"callback", line, re.I) and "connected call" in line:
            assert "whatsapp_calls" not in line, line
    assert "a WhatsApp callback needs a relevant message" in prompt


def _callback_merged_onto_reply(tmp_path, monkeypatch, connected_at, **fields):
    row = _row(tmp_path, monkeypatch, **{"summary": "Call Dana back", "fulfillment": "callback", **fields})
    missed = {"phone": "+12025550173", "timestamp": "2026-06-24 06:00:00", "is_outgoing": False,
              "is_answered": False, "call_type": "phone", "duration_seconds": 0}
    connected = {"phone": "+12025550173", "timestamp": connected_at, "is_outgoing": True,
                 "is_answered": False, "call_type": "phone", "duration_seconds": 125}
    missed_id, missed_quote = call_evidence(missed, "calls")
    action = {"type": "call_back", "channel": "phone", "contactName": "Dana",
              "contactIdentifier": "+12025550173", "contextSummary": "Return Dana's missed call",
              "evidence": [{"sourceType": "calls", "sourceId": missed_id, "snippet": missed_quote}]}
    local = {"calls": [missed, connected]}
    cr.resolve({"today": "2026-06-24", "local": local, "new_actions": [action]},
               datetime(2026, 6, 24, 9), resolve_existing=False)
    merged = cr._load_items()[row["anchor_key"]]
    assert merged["action_type"] == "reply"            # one debt per counterpart: still one row
    call_id, call_quote = call_evidence(connected, "calls")
    update = {"loopId": merged["anchor_key"], "loopVersion": loop_version(merged), "status": "resolved",
              "evidence": [{"sourceType": "calls", "sourceId": call_id, "snippet": call_quote}]}
    update["criticReceipt"] = cr.record_critic_approval(update)
    return cr.resolve({"today": "2026-06-24", "local": local, "loop_updates": [update]},
                      datetime(2026, 6, 24, 9))


def test_a_callback_merged_onto_an_open_callback_reply_can_be_settled_by_a_later_connected_call(tmp_path, monkeypatch):
    result = _callback_merged_onto_reply(tmp_path, monkeypatch, "2026-06-24 08:00:00")
    assert len(result["resolved"]) == 1


def test_a_callback_merged_onto_a_deliverable_never_lets_a_call_settle_it(tmp_path, monkeypatch):
    result = _callback_merged_onto_reply(tmp_path, monkeypatch, "2026-06-24 08:00:00",
                                         summary="Send Dana the pricing document", fulfillment=None)
    assert result["resolved"] == []
    row = next(iter(cr._load_items().values()))
    assert row["status"] == "open" and "callback_since" not in row and not row.get("fulfillment")


def test_explicit_none_keeps_critic_reviewed_callback_available(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, action_type="call_back", channel="phone",
               summary="Call Dana back", fulfillment="none")
    missed, action = _missed_call_capture(loopId=row["anchor_key"])
    connected = {**missed, "timestamp": "2026-06-24 08:00:00", "is_outgoing": True,
                 "duration_seconds": 125}
    local = {"calls": [missed, connected]}
    now = datetime(2026, 6, 24, 9)
    cr.resolve({"today": "2026-06-24", "local": local, "new_actions": [action]}, now, resolve_existing=False)
    saved = cr._load_items()[row["anchor_key"]]
    call_id, quote = call_evidence(connected, "calls")
    update = {"loopId": saved["anchor_key"], "loopVersion": loop_version(saved), "status": "resolved",
              "evidence": [{"sourceType": "calls", "sourceId": call_id, "snippet": quote}]}
    update["criticReceipt"] = cr.record_critic_approval(update)
    result = cr.resolve({"today": "2026-06-24", "local": local, "loop_updates": [update]}, now)
    assert len(result["resolved"]) == 1
    assert saved["callback_since"] == missed["timestamp"]


def test_a_call_before_the_merged_callback_request_does_not_settle_it(tmp_path, monkeypatch):
    result = _callback_merged_onto_reply(tmp_path, monkeypatch, "2026-06-24 05:00:00")
    assert result["resolved"] == []


@pytest.mark.parametrize("text,reopens", [("thanks!!", False), ("Can you send the updated pricing doc?", True)])
def test_after_your_closure_only_a_real_new_ask_reopens(tmp_path, monkeypatch, text, reopens):
    row = _row(tmp_path, monkeypatch, status="resolved", resolution="user_resolved",
               closed_at="2026-06-23 10:00:00", source="imessage",
               source_refs=[{"sourceType": "imessage", "sourceId": "old", "snippet": "Send the pricing doc"}])
    inbound = {"id": "fresh", "handle": "+12025550173", "timestamp": "2026-06-24 08:00:00",
               "is_from_me": False, "text": text}
    action = {"type": "reply", "channel": "imessage", "contactName": "Dana",
              "contactIdentifier": "+12025550173", "contextSummary": "Send the pricing document",
              "evidence": [{"sourceType": "imessage", "sourceId": "fresh", "snippet": text}]}
    cr.resolve({"today": "2026-06-24", "local": {"imessage": [inbound]}, "new_actions": [action]},
               datetime(2026, 6, 24, 9), resolve_existing=False)
    assert (cr._load_items()[row["anchor_key"]]["status"] == "open") is reopens


def test_a_parked_task_comes_back_when_a_brief_captures_it_again(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, status="parked", parked_at="2026-06-20")
    action = {"type": "reply", "channel": "imessage", "contactName": "Dana",
              "contactIdentifier": "+12025550173", "contextSummary": "Send Dana the pricing document"}
    cr.resolve({"today": "2026-06-24", "local": {}, "new_actions": [action]},
               datetime(2026, 6, 24, 9), resolve_existing=False)
    item = cr._load_items()[row["anchor_key"]]
    assert item["status"] == "open" and "parked_at" not in item


def _missed_call_capture(**extra):
    missed = {"phone": "+12025550173", "timestamp": "2026-06-24 06:00:00", "is_outgoing": False,
              "is_answered": False, "call_type": "phone", "duration_seconds": 0}
    missed_id, missed_quote = call_evidence(missed, "calls")
    return missed, {"type": "call_back", "channel": "phone", "contactName": "Dana",
                    "contactIdentifier": "+12025550173", "contextSummary": "Return Dana's missed call",
                    "evidence": [{"sourceType": "calls", "sourceId": missed_id, "snippet": missed_quote}],
                    **extra}


def test_a_callback_never_marks_a_task_they_owe_you(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, action_type="waiting_on", status="waiting",
               summary="Dana sends the signed contract")
    missed, action = _missed_call_capture(loopId=row["anchor_key"])
    cr.resolve({"today": "2026-06-24", "local": {"calls": [missed]}, "new_actions": [action]},
               datetime(2026, 6, 24, 9), resolve_existing=False)
    assert "callback_since" not in cr._load_items()[row["anchor_key"]]


def test_a_new_ordinary_ask_that_reopens_a_task_drops_an_old_callback(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, status="resolved", resolution="replied",
               closed_at="2026-06-23 10:00:00", callback_since="2026-06-23 07:00:00")
    inbound = {"id": "fresh", "handle": "+12025550173", "timestamp": "2026-06-24 08:00:00",
               "is_from_me": False, "text": "Can you send the updated pricing doc?"}
    action = {"type": "reply", "channel": "imessage", "contactName": "Dana",
              "contactIdentifier": "+12025550173", "contextSummary": "Send the updated pricing doc",
              "evidence": [{"sourceType": "imessage", "sourceId": "fresh", "snippet": inbound["text"]}]}
    cr.resolve({"today": "2026-06-24", "local": {"imessage": [inbound]}, "new_actions": [action]},
               datetime(2026, 6, 24, 9), resolve_existing=False)
    item = cr._load_items()[row["anchor_key"]]
    assert item["status"] == "open" and "callback_since" not in item


def _capture_missed_call(row, local, call):
    call_id, quote = call_evidence(call, "calls")
    action = {"type": "call_back", "channel": "phone", "contactName": "Dana", "contactIdentifier": "+12025550173",
              "contextSummary": "Return Dana's missed call",
              "evidence": [{"sourceType": "calls", "sourceId": call_id, "snippet": quote}]}
    cr.resolve({"today": "2026-06-24", "local": local, "new_actions": [action]},
               datetime(2026, 6, 24, 9), resolve_existing=False)
    return cr._load_items()[row["anchor_key"]]


def test_recapturing_an_older_missed_call_never_moves_the_callback_back(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, summary="Call Dana back", fulfillment="callback")
    call = lambda ts, **k: {"phone": "+12025550173", "timestamp": ts, "call_type": "phone", **k}
    early = call("2026-06-24 06:00:00", is_outgoing=False, is_answered=False, duration_seconds=0)
    late = call("2026-06-24 08:00:00", is_outgoing=False, is_answered=False, duration_seconds=0)
    local = {"calls": [early, late]}
    latest = _capture_missed_call(row, local, late)["callback_since"]
    assert _capture_missed_call(row, local, early)["callback_since"] == latest


def test_a_call_never_settles_a_promised_deliverable(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, action_type="follow_up", summary="Send Dana the signed term sheet")
    missed = {"phone": "+12025550173", "timestamp": "2026-06-24 06:00:00", "call_type": "phone",
              "is_outgoing": False, "is_answered": False, "duration_seconds": 0}
    assert "callback_since" not in _capture_missed_call(row, {"calls": [missed]}, missed)
