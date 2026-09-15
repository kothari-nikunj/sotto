"""Complete Gmail message reads shared by the live poll, briefs, and history."""
from __future__ import annotations

import base64
import html
import os
import re
from html.parser import HTMLParser

GOOGLE_HTTP_TIMEOUT = 30


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.hidden = 0

    def handle_data(self, data):
        if not self.hidden:
            self.out.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "li", "tr"}:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1


def _decode(data, size=0) -> str:
    if not data:
        if int(size or 0) > 0:
            raise RuntimeError("Gmail text part is missing its encoded data")
        return ""
    try:
        value = str(data)
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        return raw.decode("utf-8", errors="replace")
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Gmail text part has invalid encoded data") from exc


def _html_text(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        value = "".join(parser.out)
    except Exception:
        value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def extract_body(payload: dict) -> str:
    """Walk an arbitrary Gmail MIME tree, preferring plain text over an HTML alternative.

    Named parts are attachments and never become message prose. Multiple text parts at the same
    preference level are retained in wire order (ordinary multipart/mixed messages can contain
    more than one textual section).
    """
    plain, rich = [], []

    def walk(part):
        if not isinstance(part, dict) or str(part.get("filename") or "").strip():
            return
        mime = str(part.get("mimeType") or "").lower()
        if mime in {"text/plain", "text/html"}:
            body = part.get("body") or {}
            value = _decode(body.get("data"), body.get("size"))
            if value.strip() and mime == "text/plain":
                plain.append(value.strip())
            elif value.strip():
                converted = _html_text(value)
                if converted:
                    rich.append(converted)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    return "\n\n".join(plain or rich)


def message_fields(message: dict) -> dict:
    """Flatten one Gmail API ``format=full`` response into Sotto's provider-neutral fields."""
    if not isinstance(message, dict) or not isinstance(message.get("payload"), dict):
        raise RuntimeError("Gmail full message is missing its MIME payload")
    payload = message["payload"]
    headers = {str(h.get("name") or "").lower(): h.get("value", "")
               for h in payload.get("headers") or [] if isinstance(h, dict)}
    return {
        "id": message.get("id"), "threadId": message.get("threadId"),
        "from": headers.get("from", ""), "to": headers.get("to", ""),
        "cc": headers.get("cc", ""), "subject": headers.get("subject", ""),
        "date": headers.get("date", "") or message.get("internalDate", ""),
        "internalDate": message.get("internalDate", ""),
        "body": extract_body(payload), "snippet": message.get("snippet", ""),
        "labels": message.get("labelIds", []), "labelIds": message.get("labelIds", []),
        "payload": payload,
    }


def token_path() -> str:
    for base in (os.environ.get("HERMES_HOME", ""), os.path.expanduser("~/.hermes"),
                 "/root/.hermes"):
        if base and os.path.isfile(os.path.join(base, "google_token.json")):
            return os.path.join(base, "google_token.json")
    return ""


def google_service(api: str, version: str):
    path = token_path()
    if not path:
        raise RuntimeError("Google isn't connected on this host (no google_token.json)")
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google_auth_httplib2 import AuthorizedHttp
    import httplib2
    credentials = Credentials.from_authorized_user_file(path)
    transport = AuthorizedHttp(credentials, http=httplib2.Http(timeout=GOOGLE_HTTP_TIMEOUT))
    return build(api, version, http=transport, cache_discovery=False)


def gmail_service():
    return google_service("gmail", "v1")


def fetch_message(service, message_id: str) -> dict:
    raw = service.users().messages().get(
        userId="me", id=str(message_id), format="full").execute()
    if not isinstance(raw, dict):
        raise RuntimeError(f"Gmail returned no full message for {message_id}")
    return message_fields(raw)
