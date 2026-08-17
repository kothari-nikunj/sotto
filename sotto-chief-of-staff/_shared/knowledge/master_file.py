#!/usr/bin/env python3
"""
master_file.py — THE one writer/reader for `$SOTTO_DATA/knowledge/master.md`, the master memory file.

WHY (owner, Aug 2026): briefs and prep re-inferred "who the user is, what the fund is, who the
partners are" from raw data every single run, and standing rules about HOW the user works had no
home at all — preferences.json is nudge behavior, style.json is writing voice. This file is the
canonical, always-in-context answer (the strongest idea in Lindy's memory writeup), kept inside
Sotto's bars: markdown a human can read and edit by hand, ONE writer module, edits are the user's
EXPLICIT words — the gateway confirms before writing and nothing is silently learned into it — and
a size cap so "always in context" stays honest.

Shape: `## <Section>` blocks; anything before the first heading is preserved as a preamble.
Recommended sections (names are free-form beyond these): **About** (who the user is, what the
business is), **People** (partners, colleagues, family — the cast around them), **Priorities**
(current standing focus), **Procedures** (standing rules — "one rule about how you do things is
worth 100 status updates", so this is the load-bearing one).

CLI (all output JSON):
    master_file.py get                                # the whole file ("" if absent)
    master_file.py sections                           # {section: char_count}
    master_file.py set     --section N [--text T]     # replace a section (creates it; stdin if no --text)
    master_file.py append  --section N [--text T]     # add lines to a section (creates it)
    master_file.py remove  --section N                # delete a section

Writes hold the jsonstore sidecar lock (a gateway edit and a brief's read can overlap) and land
atomically. A write that would push the file past MASTER_CHAR_CAP fails — with per-section sizes,
so the caller trims deliberately rather than the file silently outgrowing every prompt it rides in.
Deleting the file is the user's right: absent file = empty context, everything still runs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import jsonstore  # noqa: E402  (the ONE lock implementation)

MASTER_CHAR_CAP = 8000   # the file rides in EVERY brief/prep prompt — small enough to never matter


def path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "knowledge", "master.md")


def read() -> str:
    try:
        with open(path(), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def render_for_prompt() -> str:
    """The prompt-facing form: the file under a header that tells the model what it is. Empty file
    (or none) renders '' so callers can drop the block entirely."""
    text = read().strip()
    if not text:
        return ""
    return ("## MASTER CONTEXT (the user's own standing file — who they are, who is around them, "
            "how they work)\n"
            "Every line below was stated by the user themselves — treat it as ground truth and "
            "resolve names/roles against it first. The Procedures section is standing "
            "instructions: apply them.\n\n" + text)


# ── section machinery ─────────────────────────────────────────────────────────────────────────────

_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _split(text: str) -> tuple[str, list]:
    """(preamble, [(name, body), …]) — bodies keep their internal newlines, not the heading line."""
    parts = _HEADING.split(text)
    preamble = parts[0]
    sections = [(parts[i], parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
    return preamble, sections


def _join(preamble: str, sections: list) -> str:
    out = preamble.rstrip("\n")
    for name, body in sections:
        out += ("\n\n" if out else "") + f"## {name}\n" + body.strip("\n")
    return out.strip("\n") + "\n" if (out.strip() or sections) else ""


def _write_text_atomic(p: str, text: str) -> None:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class OverCap(Exception):
    """The write would exceed MASTER_CHAR_CAP. Carries {section: chars} so the caller can trim."""

    def __init__(self, sizes: dict):
        self.sizes = sizes
        super().__init__(f"master.md would exceed {MASTER_CHAR_CAP} chars")


def _mutate(fn) -> dict:
    """Read-modify-write under the sidecar lock. `fn(preamble, sections) -> (preamble, sections)`.
    Raises OverCap (file untouched) when the result would outgrow the cap."""
    p = path()
    with jsonstore.lock(p):
        preamble, sections = _split(read())
        preamble, sections = fn(preamble, sections)
        text = _join(preamble, sections)
        if len(text) > MASTER_CHAR_CAP:
            raise OverCap({name: len(body) for name, body in sections})
        _write_text_atomic(p, text)
    return {"ok": True, "chars": len(text),
            "sections": {name: len(body.strip("\n")) for name, body in sections}}


def set_section(name: str, text: str) -> dict:
    def fn(pre, secs):
        out, done = [], False
        for n, b in secs:
            if n.lower() == name.lower():
                out.append((n, "\n" + text.strip("\n") + "\n"))
                done = True
            else:
                out.append((n, b))
        if not done:
            out.append((name, "\n" + text.strip("\n") + "\n"))
        return pre, out
    return _mutate(fn)


def append_section(name: str, text: str) -> dict:
    def fn(pre, secs):
        out, done = [], False
        for n, b in secs:
            if n.lower() == name.lower():
                out.append((n, b.rstrip("\n") + "\n" + text.strip("\n") + "\n"))
                done = True
            else:
                out.append((n, b))
        if not done:
            out.append((name, "\n" + text.strip("\n") + "\n"))
        return pre, out
    return _mutate(fn)


def remove_section(name: str) -> dict:
    def fn(pre, secs):
        return pre, [(n, b) for n, b in secs if n.lower() != name.lower()]
    return _mutate(fn)


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("verb", choices=("get", "sections", "set", "append", "remove"))
    ap.add_argument("--section")
    ap.add_argument("--text")
    a = ap.parse_args()

    if a.verb == "get":
        print(json.dumps({"text": read(), "chars": len(read()), "cap": MASTER_CHAR_CAP}))
        return 0
    if a.verb == "sections":
        _, secs = _split(read())
        print(json.dumps({name: len(body.strip("\n")) for name, body in secs}))
        return 0
    if not a.section:
        print(json.dumps({"ok": False, "error": f"{a.verb} needs --section"}))
        return 2
    try:
        if a.verb == "remove":
            print(json.dumps(remove_section(a.section)))
            return 0
        text = a.text if a.text is not None else sys.stdin.read()
        if not text.strip():
            print(json.dumps({"ok": False, "error": "empty text — pass --text or pipe stdin"}))
            return 2
        fn = set_section if a.verb == "set" else append_section
        print(json.dumps(fn(a.section, text)))
        return 0
    except OverCap as e:
        print(json.dumps({"ok": False, "error": f"would exceed the {MASTER_CHAR_CAP}-char cap — "
                          "trim a section first (sizes below) — this file rides in every prompt",
                          "sections": e.sizes}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
