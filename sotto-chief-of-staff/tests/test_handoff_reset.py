"""A fresh counterpart promise or a snooze lets a handed-off waiting loop be named again."""
from datetime import datetime

import brief_validate
import retune_apply
from render_local import message_evidence_id
from test_continuity_evidence import _env, _loop, _resolve, _update, cr


def test_fresh_promise_after_the_hand_off_restarts_the_chase_story(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    row = _loop(tmp_path, action_type="waiting_on", summary="Dana owes the signed waiver",
                chased_count=cr.CHASE_MAX, handoff_asked_at="2026-06-20", chase_stalls=1,
                last_chased_at="2026-06-19", last_heard_at="2026-06-10 08:00:00")
    assert not brief_validate.is_urgent(row, "2026-06-24")
    msg = {"rowid": 30, "is_from_me": False, "handle": "+14155550109",
           "timestamp": "2026-06-24 08:00:00", "text": "Legal will send it on Friday."}
    update = _update(row, "waiting", "imessage", message_evidence_id(msg, "imessage"),
                     "will send it on Friday")
    active, = _resolve({"imessage": [msg]}, update)["active"]
    for field in ("chased_count", "handoff_asked_at", "chase_stalls", "last_chased_at"):
        assert field not in active, field
    assert active["last_heard_at"] == "2026-06-24 08:00:00"
    assert active["chase_after"] == "2026-06-26"
    assert cr.chase_due(active, "2026-06-27", datetime(2026, 6, 27, 9))


def test_snoozing_a_handed_off_waiting_loop_clears_the_hand_off():
    row = {"anchor_key": "k", "status": "open", "action_type": "waiting_on",
           "chased_count": 2, "handoff_asked_at": "2026-06-20"}
    retune_apply.snooze_row(row, 3, "2026-06-24")
    assert "handoff_asked_at" not in row and row["snoozed_until"] == "2026-06-27"
