"""Attachment-client construction through the daily Gmail gather."""

import pytest
from test_gather_google import _b64, _wire_attachment_gather, gg


@pytest.mark.parametrize('complete', [True, False])
def test_an_email_with_no_attachments_makes_no_attachment_call(monkeypatch, complete):
    """A part with no filename is not an attachment. No filename anywhere → no attachments().get,
    and no `attachments` key on the row at all."""
    body = {"size": 20}
    if complete:
        body["data"] = _b64(b"<p>Fictional mail</p>")
    svc = _wire_attachment_gather(monkeypatch, [{"filename": "", "mimeType": "text/html", "body": body}])
    opened = []
    def service():
        opened.append(True)
        return svc
    monkeypatch.setattr(gg, "_gmail_service", service)
    emails = gg.gather_gmail("/fake/api.py", 25, 12)
    assert "attachments" not in emails[0]
    assert svc.attachment_gets == []
    assert emails[0].get("_body_complete", False) is complete
    assert len(opened) == (1 if complete else 2)
