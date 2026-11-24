"""Archive state is a label, not proof of handling (audit 03-intelligence 2.1)."""
from render_local import _format_emails, _trim_email


def _email(mid, body, date, labels, **extra):
    return {"id": mid, "threadId": "t1", "from": "Sarah <sarah@example.com>", "to": "owner@example.com",
            "subject": f"Subject {mid}", "date": date, "body": body, "labelIds": labels, **extra}


def test_an_archived_unanswered_ask_keeps_its_body_for_extraction():
    ask = _email("ask", "Can you send the signed term sheet by Friday?",
                 "Mon, 09 Nov 2026 09:00:00 +0000", ["IMPORTANT"], owner_replied_later=False)
    out = _format_emails([_trim_email(ask)])
    assert "Can you send the signed term sheet by Friday?" in out
    assert "MessageId: ask" in out
    assert "Handled" not in out and "not proof of handling" in out


def test_an_archived_message_the_owner_later_answered_stays_subject_only():
    ask = _email("ask", "Can you send the deck?", "Mon, 09 Nov 2026 09:00:00 +0000", ["IMPORTANT"])
    reply = _email("reply", "Here it is.", "Mon, 09 Nov 2026 10:00:00 +0000", ["SENT"],
                   **{"from": "Owner <owner@example.com>"})
    out = _format_emails([_trim_email(ask), _trim_email(reply)])
    assert "Can you send the deck?" not in out
    assert "- Subject ask" in out


def test_archived_promotions_stay_subject_only():
    promo = _email("promo", "50% off everything", "Mon, 09 Nov 2026 09:00:00 +0000",
                   ["CATEGORY_PROMOTIONS"], threadId="t2")
    out = _format_emails([_trim_email(promo)])
    assert "50% off everything" not in out and "- Subject promo" in out


def test_thread_evidence_beats_the_capped_sent_lane():
    # The sent lane holds at most 15 replies; the thread read itself says the owner answered.
    ask = _email("ask", "Can you send the deck?", "Mon, 09 Nov 2026 09:00:00 +0000", ["IMPORTANT"],
                 owner_replied_later=True)
    out = _format_emails([_trim_email(ask)])
    assert "Can you send the deck?" not in out and "No Later Reply" not in out


def test_without_thread_evidence_reply_status_is_unknown_not_none():
    ask = _email("ask", "Can you send the deck?", "Mon, 09 Nov 2026 09:00:00 +0000", ["IMPORTANT"])
    out = _format_emails([_trim_email(ask)])
    assert "Can you send the deck?" in out
    assert "No Later Reply" not in out and "reply status unknown" in out


def test_archived_forums_and_automated_senders_are_bulk():
    forum = _email("forum", "weekly digest body", "Mon, 09 Nov 2026 09:00:00 +0000", ["CATEGORY_FORUMS"])
    robot = _email("robot", "your receipt body", "Mon, 09 Nov 2026 09:00:00 +0000", ["IMPORTANT"],
                   **{"from": "Shop <no-reply@shop.example>"})
    out = _format_emails([_trim_email(forum), _trim_email(robot)])
    assert "weekly digest body" not in out and "your receipt body" not in out


def test_the_thread_read_decides_whether_the_owner_replied_later():
    import gather_google as gg
    thread = {"id": "t1", "messages": [
        {"id": "ask", "threadId": "t1", "internalDate": "1000", "labelIds": ["INBOX"]},
        {"id": "reply", "threadId": "t1", "internalDate": "2000", "labelIds": ["SENT"]},
        {"id": "later", "threadId": "t1", "internalDate": "3000", "labelIds": ["INBOX"]}]}
    assert gg._owner_replied_after(thread, "t1", "ask") is True
    assert gg._owner_replied_after(thread, "t1", "later") is False
    assert gg._owner_replied_after(thread, "t1", "missing") is None
    assert gg.normalize_email({"id": "ask"}, {"owner_replied_later": False})["owner_replied_later"] is False
