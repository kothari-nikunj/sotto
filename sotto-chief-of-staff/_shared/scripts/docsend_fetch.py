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

Output (stdout JSON): {status:"ok", url, pages, title, text, pdf, cached}  — `text` is the deck
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
from timeutil import configured_user_email  # noqa: E402

MODEL = os.environ.get("SOTTO_GEMINI_MODEL", "gemini-3.7-flash")
DOCSEND_MAX_PAGES = 30        # a seed deck is 10-20 pages; 30 covers the long tail without a book
HTTP_TIMEOUT = 30
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
VIEW_RE = re.compile(r"https?://(?:www\.)?docsend\.com/view/([A-Za-z0-9_-]+)", re.I)
TOKEN_RE = re.compile(r'name="authenticity_token"\s+value="([^"]+)"')
CSRF_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')
TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.I)


def is_docsend(url: str) -> bool:
    return bool(VIEW_RE.search(url or ""))


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


def _err(step: str, msg: str) -> dict:
    return {"status": "error", "step": step, "error": msg}


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
        page, xobj, cont = (first_page + i * 3, first_page + i * 3 + 1, first_page + i * 3 + 2)
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
        return rec if isinstance(rec, dict) and rec.get("text") else None
    except Exception:  # noqa: BLE001
        return None


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
        rec = {**{k: result[k] for k in ("url", "pages", "title", "text")}, "pdf": saved_pdf,
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
    m = VIEW_RE.search(url or "")
    if not m:
        return _err("url", f"not a docsend.com/view link: {url!r}")
    view_url = f"https://docsend.com/view/{m.group(1)}"
    http = http or _Http()

    try:
        page = http.get(view_url).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return _err("open", f"could not open the deck page: {type(e).__name__}: {e}")
    title_m = TITLE_RE.search(page)
    title = (title_m.group(1).strip() if title_m else "").removesuffix("| DocSend").strip(" -|")

    # The email gate, when present. authenticity_token is Rails' CSRF field; the csrf-token meta is
    # the fallback spelling. No token AND no email field ⇒ the deck is open — skip straight to pages.
    needs_email = 'name="link_auth_form[email]"' in page or "link_auth_form" in page
    if needs_email:
        tok = TOKEN_RE.search(page) or CSRF_RE.search(page)
        if not tok:
            return _err("gate", "email gate present but no auth token found — DocSend may have "
                                "changed their markup; open the link yourself")
        fields = {"utf8": "✓", "authenticity_token": tok.group(1),
                  "link_auth_form[email]": email, "commit": "Continue"}
        if passcode:
            fields["link_auth_form[passcode]"] = passcode
        try:
            http.post_form(view_url, fields, view_url)
            page = http.get(view_url).decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return _err("gate", f"email gate refused: {type(e).__name__}: {e}")
        if "link_auth_form" in page and 'name="link_auth_form[email]"' in page:
            hint = ("this deck requires a passcode — pass --passcode" if "passcode" in page.lower()
                    else "this deck requires EMAIL VERIFICATION (a click-the-link mail) — "
                         "open it yourself once, or ask the sender to relax the setting")
            return _err("gate", hint)

    # Page images: /view/<id>/page_data/<n> answers {imageUrl: ...} per page while the session
    # cookie from the gate rides along. Page 1 failing = the whole read failing, loudly.
    images = []
    for n in range(1, DOCSEND_MAX_PAGES + 1):
        try:
            meta = json.loads(http.get(f"{view_url}/page_data/{n}", accept="application/json"))
        except Exception:  # noqa: BLE001 — first missing page = the end of the deck
            break
        img_url = (meta or {}).get("imageUrl") or ""
        if not img_url:
            break
        try:
            images.append(http.get(img_url, accept="image/*"))
        except Exception as e:  # noqa: BLE001
            return _err("pages", f"page {n} image failed: {type(e).__name__}: {e}")
    if not images:
        return _err("pages", "no page images found — the gate may have silently failed, or "
                             "DocSend changed their viewer; open the link yourself")

    read = (vision or _gemini_read)(images, title)
    if not read:
        return _err("vision", "Gemini could not read the page images (check GOOGLE_AI_API_KEY)")
    return {"status": "ok", "url": view_url, "pages": len(images), "title": title, "text": read,
            "_images": images}


def read_deck(url: str, email: str, passcode: str = "", fresh: bool = False) -> dict:
    """The storing front door: cache first (a re-ask must not re-receipt the founder's analytics),
    else fetch + save. `fresh` forces a new fetch (and a new, visible, view)."""
    m = VIEW_RE.search(url or "")
    if not m:
        return _err("url", f"not a docsend.com/view link: {url!r}")
    view_id = m.group(1)
    if not fresh:
        cached = load_cached(view_id)
        if cached:
            return {"status": "ok", "cached": True, "pages": cached.get("pages", 0),
                    "url": cached.get("url", url), "title": cached.get("title", ""),
                    "text": cached["text"], "pdf": cached.get("pdf", "")}
    out = fetch_deck(url, email, passcode)
    if out.get("status") != "ok":
        return out
    images = out.pop("_images", [])
    out["pdf"] = _save_deck(view_id, out, images)
    out["cached"] = False
    return out


def _gemini_read(images: list, title: str) -> str:
    """ONE multimodal call: every page image inline, per-page extraction out. Direct REST on the
    same key/model as every other Gemini call in this tree (gemini.py's call shape is text-only by
    design — the brief pipeline never sends images, so this stays here with its one caller)."""
    key = os.environ.get("GOOGLE_AI_API_KEY", "").strip()
    if not key:
        return ""
    parts = [{"text": (f"These are the pages of a deck{f' titled {title!r}' if title else ''}, in "
                       "order. For each page: 'Page N:' then the page's actual text and what it "
                       "shows (charts/numbers included). Then 'SUMMARY:' — what the company does, "
                       "stage, ask, traction, team — ONLY from what the pages contain.")}]
    for img in images:
        parts.append({"inline_data": {"mime_type": "image/png",
                                      "data": base64.b64encode(img).decode()}})
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={key}",
        data=json.dumps({"contents": [{"parts": parts}]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
    except Exception:  # noqa: BLE001
        return ""
    cand = (data.get("candidates") or [{}])[0]
    return "".join(p.get("text", "") for p in (cand.get("content", {}).get("parts") or [])).strip()


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
        m = VIEW_RE.search(a.url or "")
        cached = load_cached(m.group(1)) if m else None
        if cached:
            print(json.dumps({"status": "ok", "cached": True, "pages": cached.get("pages", 0),
                              "url": cached.get("url", a.url), "title": cached.get("title", ""),
                              "text": cached["text"], "pdf": cached.get("pdf", "")}))
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
