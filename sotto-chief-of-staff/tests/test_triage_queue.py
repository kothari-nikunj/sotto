"""triage_queue.py — cross-channel needs-a-reply queue; reuses compose_brief thread-processing."""
import importlib.util, os

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("triage_queue", os.path.join(HERE, "..", "_shared", "scripts", "triage_queue.py"))
tq = importlib.util.module_from_spec(spec); spec.loader.exec_module(tq)


def test_local_queue_needs_response_and_resolves_names():
    local = {
        "contacts": [{"name": "Jake Rosen", "phones": ["+12069994970"]}],
        "imessage": [
            # Jake asked, user hasn't replied → needs response, name resolves from contacts
            {"handle": "+12069994970", "text": "can you intro me?", "is_from_me": False, "timestamp": "2026-06-25T09:00"},
            # an unknown phone-only sender → dropped
            {"handle": "+18885551234", "text": "your code is 123", "is_from_me": False, "timestamp": "2026-06-25T08:00"},
        ],
    }
    q = tq.local_queue(local, "imessage")
    names = [i["name"] for i in q]
    assert "Jake Rosen" in names                       # resolved + surfaced
    assert all("888" not in i["identifier"] for i in q)  # phone-only OTP sender dropped


def test_local_queue_skips_already_replied():
    local = {"contacts": [{"name": "Mira", "phones": ["+12065550000"]}],
             "imessage": [
                 {"handle": "+12065550000", "text": "thanks!", "is_from_me": False, "timestamp": "2026-06-25T09:00"},
                 {"handle": "+12065550000", "text": "you got it", "is_from_me": True, "timestamp": "2026-06-25T09:05"},
             ]}
    assert tq.local_queue(local, "imessage") == []     # user replied last → not in queue


def test_email_queue_filters_replied_and_promos():
    emails = [
        {"threadId": "t1", "from": "a@b.com", "subject": "Deal", "date": "2026-06-25T08:00",
         "labelIds": ["INBOX", "IMPORTANT", "UNREAD"], "snippet": "thoughts?"},
        {"threadId": "t2", "from": "me", "subject": "Re: x", "date": "2026-06-25T09:00",
         "labelIds": ["SENT"], "snippet": "done"},               # replied → skip
        {"threadId": "t3", "from": "promo@x.com", "subject": "Sale", "date": "2026-06-25T07:00",
         "labelIds": ["CATEGORY_PROMOTIONS"], "snippet": "50% off"},  # promo → skip
    ]
    q = tq.email_queue(emails)
    ids = [e["threadId"] for e in q]
    assert ids == ["t1"] and q[0]["important"] and q[0]["unread"]
