#!/usr/bin/env python3
"""
docsend_fetch.py — read a DocSend deck someone SENT the user, without a browser.

WHY THIS EXISTS (owner ask, Aug 2026; BROWSER-USE.md §3 row 2's hardest case): founders send
DocSend links; the deck is the whole point of the email, and no crawler can read it — DocSend
gates the viewer behind an email form and renders every page as an IMAGE inside a JS viewer.
But the gate is an ordinary form POST and the pages are ordinary image URLs, so the whole job is
requests-level HTTP plus a multimodal model: submit the user's OWN email to the gate, fetch the
page images, and have Gemini read them. No Chrome, no cookie custody, no browser-use — the exact
outcome BROWSER-USE.md priced at +399MB, at zero footprint.

THE ONE THING TO UNDERSTAND BEFORE USING IT: **viewing a DocSend is visible to the sender.** They
see your email, the timestamp, and per-page time. That makes this OUTWARD-FACING — closer to a
send than a read — so it refuses to run in unattended runs (SOTTO_UNATTENDED, the same gate that
keeps gmail-send chat-only): Sotto reads a deck when YOU ask it to in chat, and the view that gets
logged is one you chose to make. A 6:30am cron must never leave view-receipts on founders' decks.

Flow (the documented docsend-scraper shape; every step fails LOUD with the step name):
  1. GET https://docsend.com/view/<id> — collect cookies + the authenticity_token.
  2. If the page asks for an email (most decks): POST the gate form with the user's email
     (configured_user_email() — their real address; --email overrides) and optional --passcode.
     A deck requiring EMAIL VERIFICATION (click-a-link) cannot be read headlessly — say so.
  3. GET /view/<id>/page_data/<n> for each page — JSON carrying the page's imageUrl.
  4. Download the images (≤ DOCSEND_MAX_PAGES) and send them to Gemini in ONE multimodal call:
     per-page text extraction, then the model's read of the deck.

Output (stdout JSON): {status:"ok", url, pages, title, text, pdf, cached,
                       pagesRead, expectedPages:null, complete:null, stopReason}
           — `text` starts with a coverage warning: a missing page is not proof of the end.
           `pages`/`pagesRead` count images read, not the deck's total pages; `text` is the deck
           read, `pdf` the saved file's path (or "" when the pages weren't PDF-embeddable)
           or   {status:"error", step, error}                  — always with the failing step.
Env: GOOGLE_AI_API_KEY (the vision read), SOTTO_GEMINI_MODEL, SOTTO_USER_EMAIL (override only),
     SOTTO_UNATTENDED (set ⇒ cache only — see below). No new variables.

STORAGE (the private docsend2pdf): a successful read saves TWO files under `$SOTTO_DATA/decks/` —
`<view_id>.pdf` (the page images assembled into a real PDF, downloadable from the dashboard at
/api/decks/<view_id>.pdf) and `<view_id>.json` (url, title, pages, the extracted text, fetched_at).
The json is the CACHE: asking about the same deck again answers from disk — no second view, no
second receipt on the founder's analytics — unless --fresh forces a re-fetch. The cache is also
the unattended story: SOTTO_UNATTENDED serves a cache HIT (reading a file is not a view) and
refuses a cache MISS, which is how a future brief can discuss a deck you already read without
ever logging a 6:31am view under your name. These are user-requested artifacts, not exhaust —
retention is yours (delete the files; DATA-FLOW.md carries the row).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import gemini_transport
from timeutil import configured_user_email  # noqa: E402

MODEL = os.environ.get("SOTTO_GEMINI_MODEL", "gemini-3.8-flash")
DOCSEND_MAX_PAGES = 30        # a seed deck is 10-20 pages; 30 covers the long tail without a book
HTTP_TIMEOUT = 30
UA = "OpenAI File Downloader, XaiImageApiFetch/1.0"
VIEW_RE = re.compile(
    r"\Ahttps?://(?P<host>(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*docsend\.com)"
    r"/view/(?P<id>[A-Za-z0-9_-]+)/?(?:[?#][^\s]*)?\Z", re.I)
TOKEN_RE = re.compile(r'name="authenticity_token"\s+value="([^"]+)"')
CSRF_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')


def _client_revision() -> str:
    """Release parked deck-reader request contracts when this owning client is repaired."""
    with open(__file__, "rb") as source:
        return hashlib.sha256(source.read()).hexdigest()
TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.I)


def is_docsend(url: str) -> bool:
    return bool(VIEW_RE.fullmatch((url or "").strip()))


class DocSendHeldError(Exception):
    """An admitted contact cannot safely be completed or replayed."""


def normalized_url(url: str) -> str:
    match = VIEW_RE.fullmatch((url or "").strip())
    if not match:
        raise ValueError("DocSend URL invalid")
    parsed = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit(("https", match.group("host").lower(),
                                   "/view/" + match.group("id"), parsed.query, ""))


class _Http:
    """One cookie-carrying session for the whole flow — the gate cookie must ride every later GET."""

    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def get(self, url: str, accept: str = "text/html") -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
        with self.opener.open(req, timeout=HTTP_TIMEOUT) as r:
            return r.read()

    def post_form(self, url: str, fields: dict, referer: str) -> bytes:
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": UA, "Referer": referer,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest"})
        with self.opener.open(req, timeout=HTTP_TIMEOUT) as r:
            return r.read()


class AttendedHttp:
    """Bounded, pinned public HTTPS session for one admitted deck read."""

    def __init__(self, url, guard, *, seconds=180):
        import time
        self.origin = urllib.parse.urlsplit(normalized_url(url)).netloc
        self.guard = guard
        self.deadline = time.monotonic() + seconds
        self.requests = self.total = self.image_bytes = 0
        self.cookies = {}

    def remaining(self):
        import time
        self.guard()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DocSendHeldError("deck deadline reached")
        return remaining

    def _request(self, url, method, data=None, referer=None, accept="text/html"):
        import http.client
        import ipaddress
        import socket
        import ssl
        from http.cookies import SimpleCookie
        for redirects in range(4):
            timeout = min(30, self.remaining())
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.port not in (None, 443)
                    or method == "POST" and parsed.netloc != self.origin
                    or accept != "image/*" and parsed.netloc != self.origin):
                raise DocSendHeldError("deck destination refused")
            addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
                raise DocSendHeldError("deck destination refused")
            address = addresses[0][4][0]
            self.requests += 1
            if self.requests > 80:
                raise DocSendHeldError("deck request bound reached")
            self.remaining()  # authority, email and cancellation immediately before contact
            connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout)
            def connect():
                raw = socket.create_connection((address, 443), timeout)
                connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
            connection.connect = connect
            headers = {"User-Agent": UA, "Accept": accept}
            if parsed.netloc == self.origin and self.cookies:
                headers["Cookie"] = "; ".join(key + "=" + value for key, value in self.cookies.items())
            if method == "POST":
                headers.update({"Content-Type": "application/x-www-form-urlencoded",
                                "Referer": referer, "X-Requested-With": "XMLHttpRequest"})
            try:
                connection.request(method, urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, "")), data, headers)
                wire_socket = connection.sock
                response = connection.getresponse()
                chunks, received = [], 0
                while received <= 2 * 1024 * 1024:
                    remaining = self.remaining()
                    if wire_socket is not None:
                        wire_socket.settimeout(min(30, remaining))
                    chunk = response.read1(min(65536, 2 * 1024 * 1024 + 1 - received))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                body = b"".join(chunks)
                self.total += len(body)
                if accept == "image/*":
                    self.image_bytes += len(body)
                if (len(body) > 2 * 1024 * 1024 or self.total > 6 * 1024 * 1024
                        or self.image_bytes > 5 * 1024 * 1024):
                    raise DocSendHeldError("deck response bound reached")
                if parsed.netloc == self.origin:
                    for key, value in response.getheaders():
                        if key.lower() == "set-cookie":
                            cookie = SimpleCookie()
                            cookie.load(value)
                            self.cookies.update({name: morsel.value for name, morsel in cookie.items()})
                status, location = response.status, response.getheader("Location")
            except DocSendHeldError:
                raise
            except Exception as exc:
                raise DocSendHeldError("deck contact uncertain") from exc
            finally:
                connection.close()
            self.remaining()
            if status in (301, 302, 303, 307, 308):
                target = urllib.parse.urljoin(url, location or "")
                if (redirects == 3 or not location or method == "POST" and
                        (status not in (302, 303) or urllib.parse.urlsplit(target).netloc != self.origin)):
                    raise DocSendHeldError("deck redirect refused")
                if method == "POST":
                    method, data, referer = "GET", None, None
                url = target
                continue
            if status == 404 and method == "GET" and "/page_data/" in parsed.path:
                raise urllib.error.HTTPError(url, 404, "page unavailable", None, None)
            if not 200 <= status < 300:
                raise DocSendHeldError("deck contact refused")
            return body
        raise DocSendHeldError("deck redirect bound reached")

    def get(self, url, accept="text/html"):
        return self._request(url, "GET", accept=accept)

    def post_form(self, url, fields, referer):
        return self._request(url, "POST", urllib.parse.urlencode(fields).encode(), referer)


def _err(step: str, msg: str) -> dict:
    return {"status": "error", "step": step, "error": msg}


FORM_RE = re.compile(r"<form\b[^>]*>.*?</form>", re.I | re.S)
INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
ATTR_RE = re.compile(r'\b(name|value|action)="([^"]*)"', re.I)


def _gate_form(page: str) -> tuple[str, dict] | None:
    """(action, {input name: value}) for the email-gate form, exactly as the page carries it — or
    None when the page has no form with a `link_auth_form[email]` input. Hidden inputs ride along
    with their default values (`_method`, `authenticity_token`, the email-sniffing fields, the
    timezone offset), so the POST is the one a browser would make, whatever DocSend adds next."""
    for form in FORM_RE.findall(page):
        if 'link_auth_form[email]' not in form:
            continue
        attrs = dict((k.lower(), v) for k, v in ATTR_RE.findall(form.split(">", 1)[0]))
        fields: dict = {}
        for tag in INPUT_RE.findall(form):
            a = dict((k.lower(), v) for k, v in ATTR_RE.findall(tag))
            name = a.get("name")
            if name and not name.startswith("feedback"):
                fields[name] = a.get("value", "")
        return attrs.get("action") or "", fields
    return None


def _browser_tz_offset() -> str:
    """What the gate's JS would send: JavaScript's getTimezoneOffset — minutes BEHIND UTC (420 for
    PDT), from the user's configured zone via THE timezone chain."""
    try:
        from tzchain import local_now  # noqa: PLC0415 — sibling in _shared/lib
        offset = local_now().utcoffset()
        return str(int(-offset.total_seconds() // 60)) if offset is not None else "0"
    except Exception:  # noqa: BLE001
        return "0"


# ── The private docsend2pdf: page images → one real PDF, page text → a cache file ─────────────────

def decks_dir() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "decks")


def deck_paths(view_id: str) -> tuple[str, str]:
    return (os.path.join(decks_dir(), f"{view_id}.pdf"),
            os.path.join(decks_dir(), f"{view_id}.json"))


def _jpeg_size(data: bytes):
    """(width, height, components) from a JPEG's SOF marker, or None. Stdlib-only on purpose —
    the container ships no imaging library, and DocSend's page images are JPEGs."""
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):          # SOF0-3: baseline/extended/progressive
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h, data[i + 9])
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        i += 2 + seg
    return None


def build_pdf(images: list) -> bytes | None:
    """Assemble JPEG page images into one PDF, pure stdlib: each page is an /Image XObject with
    /DCTDecode — the PDF embeds the JPEG bytes verbatim, so this is packaging, not re-encoding.
    Any non-JPEG page → None (the text read still succeeds; the PDF is a bonus, never the blocker)."""
    sizes = [_jpeg_size(img) for img in images]
    if not images or any(s is None for s in sizes):
        return None
    objs: list[bytes] = []                              # 1-indexed PDF objects, in order

    def ref(n: int) -> bytes:
        return f"{n} 0 R".encode()

    # Object layout: 1=Catalog, 2=Pages, then per page i: [Page, XObject, Contents].
    first_page = 3
    kids = b" ".join(ref(first_page + i * 3) for i in range(len(images)))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(images)).encode() + b" >>")
    for i, (img, (w, h, comps)) in enumerate(zip(images, sizes)):
        _, xobj, cont = (first_page + i * 3, first_page + i * 3 + 1, first_page + i * 3 + 2)
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources "
                    b"<< /XObject << /Im0 %s >> >> /Contents %s >>"
                    % (w, h, ref(xobj), ref(cont)))
        cs = b"/DeviceGray" if comps == 1 else b"/DeviceRGB"
        objs.append(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s "
                    b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n"
                    % (w, h, cs, len(img)) + img + b"\nendstream")
        stream = b"q %d 0 0 %d 0 0 cm /Im0 Do Q" % (w, h)
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref_at))
    return bytes(out)


def load_cached(view_id: str) -> dict | None:
    """The saved read, if this deck was fetched before — answering from disk is NOT a view."""
    try:
        with open(deck_paths(view_id)[1], encoding="utf-8") as f:
            rec = json.load(f)
        return _with_coverage(rec) if isinstance(rec, dict) and rec.get("text") else None
    except Exception:  # noqa: BLE001
        return None


_STOP_REASONS = {"page_limit", "page_fetch_failed", "page_end_unverified", "legacy_cache"}


def _with_coverage(result: dict) -> dict:
    """Make the reader's limited evidence explicit, including for pre-coverage caches.

    No supported viewer response currently proves the total page count or the end of a deck.
    Do not infer either from a 404, empty metadata, an exception, or reaching our page limit.
    Keep the warning in text because the attended receipt intentionally projects a fixed schema.
    """
    reason = result.get("stopReason")
    if not isinstance(reason, str) or reason not in _STOP_REASONS:
        reason = "legacy_cache"
    pages = result.get("pages", 0)
    detail = {
        "page_limit": "The page limit was reached; additional pages may exist.",
        "page_fetch_failed": "The next page could not be fetched; additional pages may exist.",
        "page_end_unverified": "The next page was unavailable; the end of the deck is unverified.",
        "legacy_cache": "This cached read did not record how page collection ended.",
    }[reason]
    warning = (f"[DocSend coverage: {pages} page(s) read. Full-deck completeness is unknown. "
               f"{detail} Treat the following text and any PDF as only the pages read.]")
    text = result["text"]
    if not text.startswith(warning + "\n\n"):
        text = warning + "\n\n" + text
    return {**result, "pagesRead": pages, "expectedPages": None, "complete": None,
            "stopReason": reason, "text": text}


def _cached_result(cached: dict, url: str) -> dict:
    return {"status": "ok", "cached": True, "pages": cached.get("pages", 0),
            "url": cached.get("url", url), "title": cached.get("title", ""),
            **{key: cached[key] for key in
               ("text", "pagesRead", "expectedPages", "complete", "stopReason")},
            "pdf": cached.get("pdf", "")}


def _save_deck(view_id: str, result: dict, images: list) -> str:
    """Write <id>.pdf + <id>.json under decks/. Best-effort: a failed save never fails the read."""
    pdf_path, json_path = deck_paths(view_id)
    saved_pdf = ""
    try:
        os.makedirs(decks_dir(), exist_ok=True)
        pdf = build_pdf(images)
        if pdf:
            with open(pdf_path, "wb") as f:
                f.write(pdf)
            saved_pdf = pdf_path
        from datetime import datetime, timezone
        rec = {**{k: result[k] for k in ("url", "pages", "title", "text", "pagesRead",
                                       "expectedPages", "complete", "stopReason")}, "pdf": saved_pdf,
               "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        tmp = f"{json_path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, json_path)
    except Exception:  # noqa: BLE001
        pass
    return saved_pdf


def fetch_deck(url: str, email: str, passcode: str = "", http: _Http | None = None,
               vision=None) -> dict:
    """The whole flow. `http`/`vision` injectable for tests. Returns the output contract above."""
    m = VIEW_RE.fullmatch((url or "").strip())
    if not m:
        return _err("url", f"not a docsend.com/view link: {url!r}")
    view_url = normalized_url(url)
    http = http or _Http()

    try:
        page = http.get(view_url).decode("utf-8", "replace")
    except DocSendHeldError:
        raise
    except Exception as e:  # noqa: BLE001
        return _err("open", f"could not open the deck page: {type(e).__name__}: {e}")
    title_m = TITLE_RE.search(page)
    title = (title_m.group(1).strip() if title_m else "").removesuffix("| DocSend").strip(" -|")

    # The email gate, when present. The form is POSTED AS DOCSEND WROTE IT: every hidden input it
    # carries, at the action it names, with only the email (and passcode) filled in. A hand-built
    # field list 404'd the day DocSend added a hidden `_method` — Rails routes a form by that field,
    # so a POST without it lands on a route that does not exist (Sep 3, 2026) — and would 404 again
    # the next time they add one. No form but a token ⇒ the older markup; no token either ⇒ open deck.
    needs_email = 'name="link_auth_form[email]"' in page or "link_auth_form" in page
    if needs_email:
        parsed = _gate_form(page)
        if parsed:
            action, fields = parsed
        else:
            tok = TOKEN_RE.search(page) or CSRF_RE.search(page)
            if not tok:
                return _err("gate", "email gate present but no auth token found — DocSend may have "
                                    "changed their markup; open the link yourself")
            action, fields = view_url, {"utf8": "✓", "authenticity_token": tok.group(1),
                                        "commit": "Continue"}
        fields["link_auth_form[email]"] = email
        if passcode:
            fields["link_auth_form[passcode]"] = passcode
        if "link_auth_form[timezone_offset]" in fields and not fields["link_auth_form[timezone_offset]"]:
            fields["link_auth_form[timezone_offset]"] = _browser_tz_offset()
        try:
            http.post_form(urllib.parse.urljoin(view_url, action), fields, view_url)
            page = http.get(view_url).decode("utf-8", "replace")
        except DocSendHeldError:
            raise
        except Exception as e:  # noqa: BLE001
            return _err("gate", f"email gate refused: {type(e).__name__}: {e}")
        if "link_auth_form" in page and 'name="link_auth_form[email]"' in page:
            hint = ("this deck requires a passcode — pass --passcode" if "passcode" in page.lower()
                    else "This deck requires email verification. Sotto's cloud reader cannot use "
                         "your Mac browser session. Open it in your browser to view it yourself, "
                         "or ask the sender for a PDF or a link that does not require verification.")
            return _err("gate", hint)

    # Page images: /view/<id>/page_data/<n> answers {imageUrl: ...} per page while the session
    # cookie from the gate rides along. Page 1 failing = the whole read failing, loudly.
    images = []
    stop_reason = "page_limit"
    for n in range(1, DOCSEND_MAX_PAGES + 1):
        try:
            page_url = urllib.parse.urlsplit(view_url)
            page_url = page_url._replace(path=page_url.path + f"/page_data/{n}").geturl()
            meta = json.loads(http.get(page_url, accept="application/json"))
        except DocSendHeldError:
            raise
        except urllib.error.HTTPError as exc:
            stop_reason = "page_end_unverified" if exc.code == 404 else "page_fetch_failed"
            break
        except Exception:  # noqa: BLE001 — preserve usable pages without claiming a complete deck
            stop_reason = "page_fetch_failed"
            break
        if not isinstance(meta, dict):
            stop_reason = "page_fetch_failed"
            break
        img_url = meta.get("imageUrl") or ""
        if not img_url:
            stop_reason = "page_end_unverified"
            break
        if not isinstance(img_url, str):
            stop_reason = "page_fetch_failed"
            break
        try:
            images.append(http.get(img_url, accept="image/*"))
        except DocSendHeldError:
            raise
        except Exception as e:  # noqa: BLE001
            return _err("pages", f"page {n} image failed: {type(e).__name__}: {e}")
    if not images:
        return _err("pages", "no page images found — the gate may have silently failed, or "
                             "DocSend changed their viewer; open the link yourself")

    read = (vision or _gemini_read)(images, title)
    if not read:
        return _err("vision", "Gemini could not read the page images (check GOOGLE_AI_API_KEY)")
    return _with_coverage({"status": "ok", "url": view_url, "pages": len(images),
                           "title": title, "text": read, "stopReason": stop_reason,
                           "_images": images})


def read_deck(url: str, email: str, passcode: str = "", fresh: bool = False, *,
              http=None, vision=None, exact_cache=False) -> dict:
    """The storing front door: cache first (a re-ask must not re-receipt the founder's analytics),
    else fetch + save. `fresh` forces a new fetch (and a new, visible, view)."""
    if exact_cache:
        import jsonstore
        view_id = VIEW_RE.fullmatch(url.strip()).group("id")
        with jsonstore.lock(os.path.join(decks_dir(), "read-" + view_id)):
            return _read_deck(url, email, passcode, fresh, http=http, vision=vision,
                              exact_cache=True)
    return _read_deck(url, email, passcode, fresh, http=http, vision=vision)


def _read_deck(url, email, passcode, fresh, *, http=None, vision=None, exact_cache=False):
    m = VIEW_RE.fullmatch((url or "").strip())
    if not m:
        return _err("url", f"not a docsend.com/view link: {url!r}")
    view_id = m.group("id")
    if not fresh:
        cached = load_cached(view_id)
        if cached and exact_cache and cached.get("url") != normalized_url(url):
            raise DocSendHeldError("cached deck target differs")
        if cached:
            return _cached_result(cached, url)
    out = fetch_deck(url, email, passcode, http=http, vision=vision)
    if out.get("status") != "ok":
        return out
    images = out.pop("_images", [])
    out["pdf"] = _save_deck(view_id, out, images)
    out["cached"] = False
    return out


def _gemini_read(images: list, title: str, *, timeout=120, max_body=None, guard=None) -> str:
    """ONE multimodal call: every page image inline, per-page extraction out. Direct REST on the
    same key/model as every other Gemini call in this tree (gemini.py's call shape is text-only by
    design — the brief pipeline never sends images, so this stays here with its one caller)."""
    import time
    deadline = time.monotonic() + timeout
    key = gemini_transport.credential()
    if not key:
        return ""
    parts = [{"text": (f"These are the pages of a deck{f' titled {title!r}' if title else ''}, in "
                       "order. For each page: 'Page N:' then the page's actual text and what it "
                       "shows (charts/numbers included). Then 'SUMMARY:' — what the company does, "
                       "stage, ask, traction, team — ONLY from what the pages contain.")}]
    for img in images:
        parts.append({"inline_data": {"mime_type": "image/png",
                                      "data": base64.b64encode(img).decode()}})
    import model_work
    evidence = [title, MODEL, [hashlib.sha256(image).hexdigest() for image in images]]
    try:
        with model_work.scope('deck_read', [evidence, _client_revision()], occurrence=True):
            body = {"contents": [{"parts": parts}],
                    "generationConfig": model_work.generation_config(MODEL)}
            request_contract = {"inline_mime_type": "image/png",
                                "generationConfig": body["generationConfig"]}
            with model_work.attempt('gemini', MODEL, json.dumps(evidence), schema=request_contract):
                if max_body is not None and len(json.dumps(body).encode("utf-8")) > max_body:
                    raise DocSendHeldError("deck image request exceeds bound")
                req = gemini_transport.request(MODEL, body, key)
                if guard is not None:
                    guard()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DocSendHeldError("deck model deadline reached")
                with urllib.request.urlopen(req, timeout=remaining) as r:
                    if max_body is None:
                        raw = r.read()
                    else:
                        chunks, received = [], 0
                        while received <= 512 * 1024:
                            if guard is not None:
                                guard()
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise DocSendHeldError("deck model deadline reached")
                            socket = getattr(getattr(getattr(r, "fp", None), "raw", None), "_sock", None)
                            if socket is not None:
                                socket.settimeout(remaining)
                            chunk = r.read1(min(65536, 512 * 1024 + 1 - received))
                            if not chunk:
                                break
                            chunks.append(chunk)
                            received += len(chunk)
                        raw = b"".join(chunks)
                        if len(raw) > 512 * 1024:
                            raise DocSendHeldError("deck model response exceeds bound")
                    data = json.loads(raw)
                model_work.note_usage(MODEL, data.get('usageMetadata'))
                cand = (data.get("candidates") or [{}])[0]
                if cand.get('finishReason') == 'MAX_TOKENS':
                    raise ValueError('Gemini deck read reached MAX_TOKENS')
                text = "".join(p.get("text", "")
                               for p in (cand.get("content", {}).get("parts") or [])).strip()
                if not text:
                    raise ValueError('Gemini deck read returned no text')
    except DocSendHeldError:
        raise
    except Exception as exc:  # noqa: BLE001
        if max_body is not None:
            raise DocSendHeldError("deck model contact uncertain") from exc
        return ""
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description="Read a DocSend deck (chat-only — see module docstring)")
    ap.add_argument("--url", required=True)
    ap.add_argument("--email", default="")
    ap.add_argument("--passcode", default="")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore the cache and fetch again (logs a NEW view with the sender)")
    a = ap.parse_args()

    # The outward-facing gate, same posture as google_action's send gate: an unattended run must
    # never log a view-receipt on a founder's deck at 6:31am under the user's name. A CACHE HIT is
    # allowed through — reading a file already on the volume is not a view — which is what lets a
    # brief discuss a deck the user already read in chat.
    if os.environ.get("SOTTO_UNATTENDED", "").strip():
        m = VIEW_RE.fullmatch((a.url or "").strip())
        cached = load_cached(m.group("id")) if m else None
        if cached:
            print(json.dumps(_cached_result(cached, a.url)))
            return 0
        print(json.dumps(_err("unattended", "refused: unattended run — viewing a DocSend logs "
                              "YOUR visit with the sender. Ask Sotto to read it in chat instead.")))
        return 2

    email = a.email.strip() or configured_user_email()
    if not email:
        print(json.dumps(_err("email", "no viewer email known — pass --email or connect Google")))
        return 2
    out = read_deck(a.url, email, a.passcode, fresh=a.fresh)
    print(json.dumps(out))
    return 0 if out.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
