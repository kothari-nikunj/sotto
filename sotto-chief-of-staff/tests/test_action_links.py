import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "action_links", os.path.join(HERE, "..", "_shared", "scripts", "action_links.py"))
al = importlib.util.module_from_spec(spec)
spec.loader.exec_module(al)


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    """Every link build now records to $SOTTO_DATA/events/drafts.jsonl — keep that inside tmp_path
    so the suite never writes to a real /data volume."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    return tmp_path


def _drafts(tmp_path):
    p = tmp_path / "events" / "drafts.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def test_imessage_link():
    assert al.link_for("imessage", "+1 (555) 123-4567", "On my way!") == \
        "imessage://+15551234567?body=On%20my%20way%21"


def test_whatsapp_https_form():
    assert al.link_for("whatsapp", "+15551234567", "hi there") == \
        "https://wa.me/15551234567?text=hi%20there"


def test_mailto_subject_and_body():
    url = al.link_for("email", "sarah@acme.com", "See attached.", subject="Re: Contract")
    assert url.startswith("mailto:sarah@acme.com?")
    assert "subject=Re%3A%20Contract" in url and "body=See%20attached." in url


def test_tel_strips_formatting():
    assert al.link_for("phone", "+1 (555) 123-4567") == "tel:+15551234567"


def test_sms_routes_messages():
    assert al.link_for("sms", "5551234567", "yo") == "sms:5551234567&body=yo"


def test_calendar_and_gmail_thread_channels():
    """Folded in from compose_brief's second builder: a meeting link is already a URL, and an email
    action with a thread but no address opens Gmail on the web."""
    assert al.link_for("calendar", " https://meet.google.com/abc ") == "https://meet.google.com/abc"
    assert al.link_for("calendar", "") == ""
    assert al.link_for("gmail_thread", "thr_88") == "https://mail.google.com/mail/u/0/#inbox/thr_88"


def test_unknown_channel_raises():
    try:
        al.link_for("carrier-pigeon", "x")
        assert False
    except ValueError:
        pass


def test_encoding_is_safe():
    # special chars must be percent-encoded so the link doesn't break
    url = al.link_for("imessage", "+15551234567", "a&b=c?d #e")
    assert "&" not in url.split("?body=")[1]  # the & in the body is encoded
    assert "%26" in url


# ── drafts.jsonl recording (roadmap Step 2 item 0) ────────────────────────────────────────────────

def test_every_link_build_records_the_draft(tmp_path):
    al.link_for("whatsapp", "+1 (555) 123-4567", "Sounds good — Thursday works.")
    rows = _drafts(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["channel"] == "whatsapp"
    assert r["message"] == "Sounds good — Thursday works."
    assert r["identifier"] == "15551234567"          # normalized exactly as the wa.me link is
    assert r["ts"].endswith("Z") and len(r["ts"]) == 20   # ISO-Z, same shape as queue.jsonl


def test_identifier_matches_what_the_link_embeds(tmp_path):
    """The recorded identifier IS the join key for the future draft-diff matcher — it must be the
    same normalization the URL carries, per channel."""
    for channel, ident, expected in [
        ("imessage", "+1 (555) 123-4567", "+15551234567"),
        ("sms", "555-123-4567", "5551234567"),
        ("whatsapp", "+15551234567", "15551234567"),
        ("email", " Sarah@Acme.com ", "Sarah@Acme.com"),
    ]:
        url = al.link_for(channel, ident, "draft body")
        assert al.normalized_identifier(channel, ident) == expected
        assert expected in url
    assert [r["identifier"] for r in _drafts(tmp_path)] == \
        ["+15551234567", "5551234567", "15551234567", "Sarah@Acme.com"]


def test_messageless_and_unknown_channels_record_nothing(tmp_path):
    al.link_for("phone", "+15551234567")             # tel: — no draft to capture
    al.link_for("imessage", "+15551234567", "")      # open-the-thread link, empty body
    al.link_for("imessage", "+15551234567", "   ")   # whitespace only
    with pytest.raises(ValueError):
        al.link_for("carrier-pigeon", "x", "hi")     # raises BEFORE recording
    assert _drafts(tmp_path) == []


def test_email_draft_records_subject(tmp_path):
    al.link_for("email", "sarah@acme.com", "See attached.", subject="Re: Contract")
    row = _drafts(tmp_path)[0]
    assert row["channel"] == "email" and row["subject"] == "Re: Contract"
    assert row["message"] == "See attached."


def test_appends_accumulate_one_line_per_build(tmp_path):
    for i in range(5):
        al.link_for("imessage", "+15551234567", f"draft {i}")
    rows = _drafts(tmp_path)
    assert [r["message"] for r in rows] == [f"draft {i}" for i in range(5)]


def test_recording_is_bounded_by_rotation(tmp_path, monkeypatch):
    """Mirrors events/queue.jsonl: once the file passes max_bytes it is rotated to the last
    keep_lines, so drafts.jsonl can never grow forever on the /data volume."""
    monkeypatch.setattr(al, "DRAFTS_MAX_BYTES", 200)
    monkeypatch.setattr(al, "DRAFTS_KEEP_LINES", 3)
    for i in range(40):
        al.link_for("imessage", "+15551234567", f"draft {i}")
    rows = _drafts(tmp_path)
    assert len(rows) <= 4                                  # 3 kept + the one appended after a rotate
    assert rows[-1]["message"] == "draft 39"               # the newest write always survives
    assert (tmp_path / "events" / "drafts.jsonl").stat().st_size < 4096


def test_recording_failure_never_breaks_the_link(monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", "/proc/definitely-unwritable")
    assert al.link_for("whatsapp", "+15551234567", "hi") == "https://wa.me/15551234567?text=hi"

    def _boom(*_a, **_k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(al, "normalized_identifier", _boom, raising=True)
    # even a hard failure inside the recorder is swallowed — the link is the product
    al.record_draft("imessage", "+15551234567", "still fine")
