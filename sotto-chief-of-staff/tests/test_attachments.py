"""attachments.py — the attachment lane's one rule, exercised against the REAL converter.

    An attachment Sotto can read becomes text under its email; one it can't is named, never guessed.

These tests deliberately do NOT mock `anydoc`. The lane's whole claim is that conversion happens
locally, in-process, with no network and no hosted OCR — a suite that mocked the converter would
prove nothing about that. `firecrawl-anydoc` is pinned in requirements.txt and requirements-dev.txt
for exactly this reason.
"""
import importlib.util
import os

import pytest

HERE = os.path.dirname(__file__)


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "..", *parts))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load("attachments", "_shared", "lib", "attachments.py")
rl = _load("rl_attach", "_shared", "lib", "render_local.py")


# ── the readable half ───────────────────────────────────────────────────────────────────────────

def test_csv_converts_to_markdown_locally():
    """The proven smoke case: a CSV has no signature to sniff, so this also exercises the
    extension fallback in _detect."""
    out = A.convert_attachment("numbers.csv", b"a,b\n1,2\n")
    assert out["filename"] == "numbers.csv"
    assert "unreadable" not in out
    assert "| a | b |" in out["text"] and "| 1 | 2 |" in out["text"]


def test_plain_text_passes_straight_through():
    """anydoc claims no format for .txt — but Sotto can plainly read one, and naming a readable
    file "unrecognized format" would break this module's own rule."""
    out = A.convert_attachment("notes.txt", b"ship the thing on friday")
    assert out["text"] == "ship the thing on friday"


def _minimal_text_pdf(text=b"Hello Sotto"):
    """The smallest valid one-page PDF with a text run, built by hand — no fixture file, no
    generator dependency. Five objects, a real xref table, and a startxref that points at it."""
    stream = b"BT /F1 12 Tf 20 100 Td (" + text + b") Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += (b"trailer\n<< /Size " + str(len(objs) + 1).encode() + b" /Root 1 0 R >>\nstartxref\n"
            + str(xref).encode() + b"\n%%EOF\n")
    return out


def test_a_one_page_text_pdf_converts_locally():
    """The load-bearing case: a real PDF, converted in-process, with ocr='reject' left at its
    default. If this ever needs a network call to pass, the lane's privacy claim is broken."""
    out = A.convert_attachment("tiny.pdf", _minimal_text_pdf())
    assert "unreadable" not in out, out
    assert "Hello Sotto" in out["text"]


# ── the named half ──────────────────────────────────────────────────────────────────────────────

def test_garbage_bytes_are_named_with_a_reason():
    out = A.convert_attachment("mystery.bin", b"\x00\x01\x02\x03not a document")
    assert out == {"filename": "mystery.bin", "unreadable": A.UNREADABLE_UNKNOWN_FORMAT}


def test_an_image_is_named_without_ever_calling_the_converter(monkeypatch):
    """Garbage bytes under a .png name: if anydoc were consulted it would raise or return a
    different reason, so "image — not converted" proves the short-circuit ran first. The boobytraps
    make that structural rather than inferred."""
    monkeypatch.setattr(A.anydoc, "format_from_bytes",
                        lambda *a, **k: pytest.fail("anydoc consulted for an image"))
    monkeypatch.setattr(A.anydoc, "to_markdown_bytes",
                        lambda *a, **k: pytest.fail("anydoc consulted for an image"))
    for name in ("photo.png", "shot.JPG", "clip.gif", "live.heic"):
        assert A.convert_attachment(name, b"\x00\x01\x02random")["unreadable"] == A.UNREADABLE_IMAGE


def test_an_empty_file_is_named_not_rendered_blank():
    assert A.convert_attachment("void.csv", b"")["unreadable"] == A.UNREADABLE_EMPTY


def test_an_oversized_attachment_is_named_never_converted():
    """The byte cap is belt-and-braces: the gather already refuses to fetch these, but a row that
    arrives from an MCP host or a fixture must be named rather than converted."""
    out = A.convert_attachment("huge.pdf", b"x" * (A.MAX_ATTACHMENT_BYTES + 1))
    assert out["unreadable"] == A.UNREADABLE_TOO_LARGE


def test_a_missing_converter_names_every_attachment_with_the_reason(monkeypatch):
    """Fail toward silence, but SAY so: no converter on the host means the brief loses the TEXT,
    never the fact that a file was there."""
    monkeypatch.setattr(A, "anydoc", None)
    out = A.convert_attachment("deck.pdf", b"%PDF-1.4 whatever")
    assert out == {"filename": "deck.pdf", "unreadable": A.UNREADABLE_NO_CONVERTER}


def test_scanned_and_encrypted_map_to_their_own_sentences(monkeypatch):
    """NeedsOcrError and EncryptedError are the two failures a reader can act on, so they get their
    own words instead of a generic one. Raised through the real exception classes."""
    for exc, reason in ((A.anydoc.NeedsOcrError, A.UNREADABLE_SCANNED),
                        (A.anydoc.EncryptedError, A.UNREADABLE_ENCRYPTED)):
        def _boom(*a, _e=exc, **k):
            raise _e("nope")
        monkeypatch.setattr(A.anydoc, "to_markdown_bytes", _boom)
        assert A.convert_attachment("doc.csv", b"a,b\n1,2\n")["unreadable"] == reason


def test_a_converter_reason_is_one_short_line(monkeypatch):
    """Reasons are rendered verbatim to the reader, so a multi-line traceback-ish message is
    flattened to its first line and bounded."""
    def _boom(*a, **k):
        raise A.anydoc.ConvertError("first line of trouble\nsecond line\n" + "x" * 400)
    monkeypatch.setattr(A.anydoc, "to_markdown_bytes", _boom)
    out = A.convert_attachment("doc.csv", b"a,b\n1,2\n")
    assert out["unreadable"] == "first line of trouble"


# ── the caps ────────────────────────────────────────────────────────────────────────────────────

def test_conversion_truncates_at_the_char_cap_with_a_visible_marker():
    """A 200-page document is not worth 200 pages of prompt, and the truncation must be VISIBLE so
    the model knows it is reading an excerpt."""
    out = A.convert_attachment("long.csv", ("a,b\n" + "1,2\n" * 5000).encode())
    assert out["text"].endswith(A.TRUNCATION_MARKER)
    assert len(out["text"]) <= A.MAX_ATTACHMENT_CHARS + len(A.TRUNCATION_MARKER)


def test_the_three_caps_are_named_constants_not_env_vars():
    """CLAUDE.md: tuning lives in named constants; a new env var needs a reason a default can't
    serve. This lane has no knob at all — no `os.environ`, no `getenv`, anywhere in it."""
    src = _source("_shared", "lib", "attachments.py")
    assert A.MAX_ATTACHMENTS_PER_EMAIL == 3
    assert A.MAX_ATTACHMENT_BYTES == 8_000_000
    assert A.MAX_ATTACHMENT_CHARS == 12_000
    assert A.MAX_ATTACHMENT_CHARS_PER_BRIEF == 60_000
    assert "os.environ" not in src and "getenv" not in src


def _source(*parts):
    with open(os.path.join(HERE, "..", *parts), encoding="utf-8") as f:
        return f.read()


def _code_lines(src):
    """Source reduced to its CODE tokens — comments and string literals (docstrings included)
    dropped. The invariants below are claims about what the lane does, and a docstring that names
    the thing the lane refuses to do must not be able to fail them."""
    import io
    import tokenize
    keep = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        keep.append(tok.string)
    return " ".join(keep)


LANE_FILES = (("_shared", "lib", "attachments.py"),
              ("_shared", "scripts", "gather_google.py"),
              ("_shared", "lib", "render_local.py"))


def test_hosted_ocr_is_never_requested_anywhere_in_the_lane():
    """THE privacy invariant: a page of the user's mail never leaves the box to become readable.
    `to_markdown_bytes` defaults to ocr='reject' and no file in the lane ever overrides it, passes
    an api_key, or names the hosted converter's credential."""
    for parts in LANE_FILES:
        code = _code_lines(_source(*parts))
        assert "hosted" not in code, parts          # ocr='hosted' — the one value that would ship mail out
        assert "api_key" not in code, parts         # anydoc's hosted-mode credential
        assert "api_url" not in code, parts
        assert "FIRECRAWL" not in code.upper(), parts


def test_the_converter_is_called_with_no_ocr_override():
    """The single call site passes exactly two positional arguments and NO keywords — adding an
    `ocr=` or `api_key=` here is how the invariant above would be lost, so it is asserted against
    the parsed call node rather than by grep."""
    import ast
    tree = ast.parse(_source("_shared", "lib", "attachments.py"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "to_markdown_bytes"]
    assert len(calls) == 1, "the converter has exactly one call site in this lane"
    assert len(calls[0].args) == 2 and not calls[0].keywords, \
        "to_markdown_bytes(data, fmt) — no ocr=, no api_key=, ever"


# ── the rendering ───────────────────────────────────────────────────────────────────────────────

def test_readable_and_unreadable_render_as_their_two_lines():
    e = rl._trim_email({"from": "Dana <d@acme.com>", "subject": "Q3", "labelIds": ["INBOX"],
                        "body": "see attached",
                        "attachments": [{"filename": "Deck.pdf", "text": "# Q3\nRevenue up"},
                                        {"filename": "scan.pdf",
                                         "unreadable": "scanned document — no local text"}]})
    out = rl._format_emails([e])
    assert '↳ attachment "Deck.pdf": # Q3\nRevenue up' in out
    assert '↳ attachment "scan.pdf" (unreadable: scanned document — no local text)' in out


def test_an_email_without_attachments_renders_exactly_as_before():
    """Back-compat: every host whose gather predates the lane, and every email that simply had no
    files, must render byte-identically to what it rendered yesterday."""
    row = {"from": "Dana <d@acme.com>", "subject": "Q3", "labelIds": ["INBOX"], "body": "hi"}
    out = rl._format_emails([rl._trim_email(row)])
    assert "↳ attachment" not in out and "hi" in out


def test_rendering_re_applies_the_char_cap():
    """The gather caps on the way in; the renderer caps again, because a row can also arrive from
    an MCP-host gather or a replayed fixture that never passed through convert_attachment."""
    e = rl._trim_email({"from": "a@b.com", "subject": "S", "labelIds": ["INBOX"],
                        "attachments": [{"filename": "big.md", "text": "y" * 99_000}]})
    out = rl._format_emails([e])
    assert A.TRUNCATION_MARKER in out
    assert len(out) < 99_000


def test_rendering_caps_the_number_of_text_blocks_but_still_names_the_rest():
    """ALL filenames are listed; only the CONVERTED ones are capped — "there were four more
    attachments" is itself information, and dropping it silently would be the guessing this lane
    exists to prevent."""
    atts = [{"filename": f"f{i}.md", "text": f"body {i}"} for i in range(5)]
    e = rl._trim_email({"from": "a@b.com", "subject": "S", "labelIds": ["INBOX"],
                        "attachments": atts})
    out = rl._format_emails([e])
    assert out.count("↳ attachment") == 5                    # every file is named
    assert "body 0" in out and "body 2" in out               # the first three carry text
    assert "body 3" not in out and "body 4" not in out       # the rest are named only
    assert f"over the {A.MAX_ATTACHMENTS_PER_EMAIL}-attachment limit" in out


def test_malformed_attachment_rows_are_skipped_not_crashed():
    e = rl._trim_email({"from": "a@b.com", "subject": "S", "labelIds": ["INBOX"],
                        "attachments": ["not a dict", {}, {"filename": "ok.md", "text": "fine"}]})
    out = rl._format_emails([e])
    assert '↳ attachment "ok.md": fine' in out


# ── the per-brief budget ────────────────────────────────────────────────────────────────────────

def _texty(name, n):
    return {"filename": name, "text": "x" * n}


def test_budget_passes_everything_through_on_an_ordinary_day():
    per_email = [[_texty("a.pdf", 10_000)], [_texty("b.pdf", 10_000), {"filename": "c.png", "unreadable": "image — not converted"}]]
    out = A.apply_attachment_budget(per_email)
    assert out[0][0]["text"] == "x" * 10_000
    assert out[1][0]["text"] == "x" * 10_000
    assert out[1][1]["unreadable"] == "image — not converted"  # named rows pass through untouched
    assert per_email[0][0]["text"] == "x" * 10_000  # pure — the input was not mutated


def test_budget_truncates_the_crossing_document_and_names_the_rest():
    # 5 × 12,000 = 60,000: the fifth email's doc CROSSES the budget mid-file, the sixth is named.
    per_email = [[_texty(f"{i}.pdf", 11_000)] for i in range(5)] + [[_texty("late.pdf", 9_000)]]
    out = A.apply_attachment_budget(per_email)
    assert all(o[0]["text"] == "x" * 11_000 for o in out[:5])  # 55,000 spent
    crossing = out[5][0]
    assert crossing["text"].endswith(A.TRUNCATION_MARKER)
    assert len(crossing["text"]) == 5_000 + len(A.TRUNCATION_MARKER)  # exactly what remained


def test_budget_spends_in_email_order_and_names_everything_after():
    per_email = [[_texty("first.pdf", 60_000)], [_texty("second.pdf", 100)], [_texty("third.pdf", 100)]]
    out = A.apply_attachment_budget(per_email)
    assert out[0][0]["text"] == "x" * 60_000  # the whole budget, one document, read whole
    assert out[1][0] == {"filename": "second.pdf", "unreadable": A.UNREADABLE_BUDGET_SPENT}
    assert out[2][0] == {"filename": "third.pdf", "unreadable": A.UNREADABLE_BUDGET_SPENT}


def test_budget_handles_empty_and_missing_lists():
    assert A.apply_attachment_budget([]) == []
    assert A.apply_attachment_budget([[], None, [_texty("a.csv", 5)]]) == [[], [], [{"filename": "a.csv", "text": "xxxxx"}]]
