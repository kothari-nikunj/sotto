#!/usr/bin/env python3
"""attachments.py — the attachment lane's ONE rule, and the caps that make it safe.

    An attachment Sotto can read becomes text under its email; one it can't is named, never guessed.

WHY THIS EXISTS: until now the gather fetched email BODIES only, so the deck that the whole thread
is about, the contract someone asked you to look at, the CSV with the numbers — all of it was
invisible to the brief. Sotto would summarize "Dana sent the Q3 deck" without ever having read the
Q3 deck. This module converts what it can to Markdown so the brief reasons over the actual content,
and NAMES what it can't so the brief never guesses at a file it never opened.

LOCAL-ONLY, ON PURPOSE. Conversion runs in-process through `anydoc`, a self-contained library with
no network calls of its own. `to_markdown_bytes` defaults to `ocr='reject'` and we never pass
anything else: a scanned PDF has no local text, so it is named "scanned document — no local text"
and stops there. There is no hosted-OCR path, no API key, and no new env var — a page of someone's
mail never leaves the box to become readable.

THE THREE CAPS live here as named constants (CLAUDE.md: tuning lives in named constants, never env
vars) and this module is their single owner. gather_google.py imports MAX_ATTACHMENTS_PER_EMAIL and
MAX_ATTACHMENT_BYTES from here rather than keeping its own copy, so the fetch side and the render
side can never drift apart.

WHAT USES IT: gather_google.py runs `convert_attachment` over the attachments it fetched for the
bodies cohort and hangs the results on each email row as `attachments: [...]`; render_local.py's
`_format_emails` renders them under their email as `↳ attachment "…"` lines. Nothing else — the
receiver's triage lane deliberately has no attachment handling (an interrupt decision is made on
metadata; opening files to decide whether to ring someone is the wrong trade).
"""
from __future__ import annotations

import os

# ── the three caps ──────────────────────────────────────────────────────────────────────────────
# Readable conversions per email. ALL filenames are still listed — the cap bounds how many get
# CONVERTED, not how many the brief knows about, because "there were four more attachments" is
# itself information and silently dropping them would be the guessing this module exists to prevent.
MAX_ATTACHMENTS_PER_EMAIL = 3

# Anything bigger is named, never fetched or converted: an 8MB ceiling covers ordinary decks,
# contracts and spreadsheets while keeping a single email from spending the brief's whole wall clock
# on a video someone attached.
MAX_ATTACHMENT_BYTES = 8_000_000

# Per converted attachment, in the prompt. A deck's Markdown runs ~4–8K characters and a
# contract's operative sections ~10–20K, so 12,000 reads a whole deck and the half of a contract
# that matters — the owner's inbox is decks and documents, and an excerpt that stops at the team
# slide was the wrong trade (owner, Aug 28). The truncation is VISIBLE (see TRUNCATION_MARKER) so
# the model knows it is reading an excerpt rather than a whole document.
MAX_ATTACHMENT_CHARS = 12_000

# The aggregate bound that makes the generous per-attachment cap safe: a day's attachments share
# ONE budget, spent in email order (newest first — the same order the cohort is fetched in). One
# document can be read whole; ten documents can't blow up the prompt. ~15K tokens of a 1M-token
# window — the ceiling exists to bound the pathological day, not to ration the ordinary one.
MAX_ATTACHMENT_CHARS_PER_BRIEF = 60_000

TRUNCATION_MARKER = "… [truncated]"

# Images are named without ever calling the converter. There is no local text in a photo, and the
# only way to get any would be hosted OCR — which this lane does not have and will not grow.
IMAGE_SUFFIXES = frozenset({
    "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "heic", "heif", "avif", "svg", "ico",
})

# Plain text is already Markdown enough. anydoc claims no format for .txt/.md/.log (they have no
# signature to sniff and it registers no extension for them), and naming a file Sotto can plainly
# read "unrecognized format" would break this module's own rule — so these decode straight through.
# Strict UTF-8 on purpose: bytes that aren't text get named rather than rendered as mojibake.
TEXT_SUFFIXES = frozenset({"txt", "md", "markdown", "text", "log"})

# Reasons, as one short line each — they are rendered verbatim to the reader, so they read as
# English and never as an exception class name.
UNREADABLE_IMAGE = "image — not converted"
UNREADABLE_SCANNED = "scanned document — no local text"
UNREADABLE_ENCRYPTED = "password-protected"
UNREADABLE_UNKNOWN_FORMAT = "unrecognized format"
UNREADABLE_NO_CONVERTER = "converter unavailable"
UNREADABLE_EMPTY = "empty file"
UNREADABLE_TOO_LARGE = "too large to read"

# Fail toward silence, but SAY so: if the converter isn't installed on this host, every attachment
# still gets NAMED with "converter unavailable" as its reason. The brief loses the text, never the
# fact that a file was there — which is the difference between degrading and lying.
try:
    import anydoc
except Exception:  # noqa: BLE001  (any import failure — missing wheel, bad ABI — degrades the same)
    anydoc = None


def _suffix(filename: str) -> str:
    """The lowercased extension without its dot ("Q3 Deck.PDF" → "pdf"), "" when there isn't one."""
    return os.path.splitext(str(filename or ""))[1].lstrip(".").lower()


def _unreadable(filename: str, reason: str) -> dict:
    return {"filename": str(filename or "(unnamed)"), "unreadable": reason}


def _finish(filename: str, markdown: str) -> dict:
    """The ONE place converted text becomes a result: trimmed, capped at MAX_ATTACHMENT_CHARS with a
    visible tail, and named instead of returned empty when the conversion found nothing."""
    text = (markdown or "").strip()
    if not text:
        # It converted, but there was nothing in it. Naming it is more honest than an empty block.
        return _unreadable(filename, "no readable text")
    if len(text) > MAX_ATTACHMENT_CHARS:
        text = text[:MAX_ATTACHMENT_CHARS].rstrip() + TRUNCATION_MARKER
    return {"filename": filename, "text": text}


UNREADABLE_BUDGET_SPENT = "the brief's attachment budget is spent"


def apply_attachment_budget(per_email: list) -> list:
    """Spend MAX_ATTACHMENT_CHARS_PER_BRIEF across a brief's attachments, in email order.

    Takes the converted attachment lists in cohort order (one list per email, the same order the
    emails render in) and returns new lists: text rows pass through until the budget runs out, the
    row that crosses it is truncated to what remains (visible marker), and every text row after
    that is NAMED with the reason instead — the reader is told there was more, never shown a
    silently thinner brief. Named rows cost nothing; pure, no mutation of the input."""
    spent = 0
    out = []
    for rows in per_email:
        kept = []
        for row in rows or []:
            text = row.get("text")
            if text is None:
                kept.append(row)
                continue
            if spent >= MAX_ATTACHMENT_CHARS_PER_BRIEF:
                kept.append(_unreadable(row.get("filename"), UNREADABLE_BUDGET_SPENT))
                continue
            if spent + len(text) > MAX_ATTACHMENT_CHARS_PER_BRIEF:
                text = text[:MAX_ATTACHMENT_CHARS_PER_BRIEF - spent].rstrip() + TRUNCATION_MARKER
                spent = MAX_ATTACHMENT_CHARS_PER_BRIEF
            else:
                spent += len(text)
            kept.append({"filename": row.get("filename"), "text": text})
        out.append(kept)
    return out


def _detect(data: bytes, filename: str):
    """The file's format: content sniffing first, the filename's extension as the fallback.

    Sniffing is the honest answer (a .txt that is really a PDF converts as a PDF), but several real
    formats have no signature to sniff — CSV and plain text are just bytes — so a failed sniff falls
    through to the extension rather than giving up on a file we can plainly read. None when neither
    knows what this is."""
    for detect, arg in ((anydoc.format_from_bytes, data),
                        (anydoc.format_from_extension, _suffix(filename))):
        if not arg:
            continue
        try:
            fmt = detect(arg)
        except Exception:  # noqa: BLE001  (a signature-less format raises here — that's the fallback's job)
            continue
        if fmt is not None:
            return fmt
    return None


def convert_attachment(filename: str, data: bytes) -> dict:
    """One attachment → either `{"filename", "text"}` or `{"filename", "unreadable": "<reason>"}`.

    Pure: no network, no filesystem, no clock. Every failure path produces a NAMED attachment with a
    one-line reason rather than an exception, because the rule is that Sotto never silently swallows
    a file it was shown.
    """
    name = str(filename or "(unnamed)")

    # Images short-circuit BEFORE the converter is consulted: there is no local text to find, and
    # asking anydoc would only spend time arriving at the same answer.
    if _suffix(name) in IMAGE_SUFFIXES:
        return _unreadable(name, UNREADABLE_IMAGE)

    if not data:
        return _unreadable(name, UNREADABLE_EMPTY)
    if len(data) > MAX_ATTACHMENT_BYTES:
        return _unreadable(name, UNREADABLE_TOO_LARGE)

    if _suffix(name) in TEXT_SUFFIXES:
        try:
            return _finish(name, data.decode("utf-8"))
        except UnicodeDecodeError:
            return _unreadable(name, UNREADABLE_UNKNOWN_FORMAT)

    if anydoc is None:
        return _unreadable(name, UNREADABLE_NO_CONVERTER)

    fmt = _detect(data, name)
    if fmt is None:
        return _unreadable(name, UNREADABLE_UNKNOWN_FORMAT)

    try:
        # ocr='reject' is the DEFAULT and is never overridden. Passing ocr='hosted' would ship the
        # user's mail to a third party to be read; the lane's whole premise is that it doesn't.
        md = anydoc.to_markdown_bytes(data, fmt)
    except anydoc.NeedsOcrError:
        return _unreadable(name, UNREADABLE_SCANNED)
    except anydoc.EncryptedError:
        return _unreadable(name, UNREADABLE_ENCRYPTED)
    except anydoc.UnsupportedError:
        return _unreadable(name, UNREADABLE_UNKNOWN_FORMAT)
    except anydoc.ConvertError as e:  # every other conversion failure, in one short line
        return _unreadable(name, (str(e).strip().splitlines() or ["could not be read"])[0][:120]
                           or "could not be read")
    except Exception:  # noqa: BLE001  (a converter bug must never take the brief down with it)
        return _unreadable(name, "could not be read")

    return _finish(name, md)
