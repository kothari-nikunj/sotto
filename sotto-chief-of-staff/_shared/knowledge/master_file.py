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
    master_file.py prioritize --text T                # add one explicitly confirmed priority
    master_file.py set     --section N [--text T]     # replace a section (creates it; stdin if no --text)
    master_file.py append  --section N [--text T]     # add lines to a section (creates it)
    master_file.py remove  --section N                # delete a section

Writes hold the jsonstore sidecar lock (a gateway edit and a brief's read can overlap) and land
atomically, through the same `knowledge.write_text_atomic` every other file under `knowledge/` is
written with. A write that would push the file past MASTER_CHAR_CAP fails — with per-section sizes,
so the caller trims deliberately rather than the file silently outgrowing every prompt it rides in.
Deleting the file is the user's right: absent file = empty context, everything still runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(_HERE, "..", "lib")
for _p in (_LIB, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import jsonstore  # noqa: E402  (the ONE lock implementation)
import knowledge as kg  # noqa: E402  (the ONE atomic-write implementation for knowledge/)

MASTER_CHAR_CAP = 8000   # the file rides in EVERY brief/prep prompt — small enough to never matter
PRIORITY_MAX = 3


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


def priorities() -> dict:
    """Return at most three explicit priority lines with stable content ids and a set revision."""
    _, sections = _split(read())
    body = next((body for name, body in sections if name.strip().lower() == "priorities"), "")
    lines = []
    for raw in body.splitlines():
        line = re.sub(r"^\s*(?:[-*+] |\d+[.)]\s+)", "", raw).strip()
        if line:
            lines.append(line)
    lines = lines[:PRIORITY_MAX]
    rows = [{"id": hashlib.sha256(line.casefold().encode()).hexdigest()[:16], "text": line}
            for line in lines]
    revision = hashlib.sha256("\n".join(r["id"] for r in rows).encode()).hexdigest()[:16]
    return {"revision": revision, "priorities": rows}


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
        kg.write_text_atomic(p, text)
    return {"ok": True, "chars": len(text),
            "sections": {name: len(body.strip("\n")) for name, body in sections}}


def set_section(name: str, text: str) -> dict:
    if name.strip().lower() == "priorities":
        nonempty = [line for line in text.splitlines() if line.strip()]
        if len(nonempty) > PRIORITY_MAX:
            raise ValueError(f"Priorities accepts at most {PRIORITY_MAX} explicit lines")
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
    if name.strip().lower() == "priorities":
        raise ValueError("Priorities is a replacement set; use set, not append")
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


def add_priority(text: str) -> dict:
    """Add the user's explicitly confirmed line atomically, preserving every existing priority."""
    text = text.strip()
    if not text or "\n" in text:
        raise ValueError("A priority must be one non-empty line")

    def fn(pre, secs):
        body = next((body for name, body in secs if name.casefold() == "priorities"), "")
        lines = [re.sub(r"^\s*(?:[-*+] |\d+[.)]\s+)", "", raw).strip()
                 for raw in body.splitlines() if raw.strip()]
        if text.casefold() in {line.casefold() for line in lines}:
            return pre, secs
        if len(lines) >= PRIORITY_MAX:
            raise ValueError("Priorities is full; confirm a replacement set before changing it")
        lines.append(text)
        replacement = "\n".join("- " + line for line in lines)
        found = any(name.casefold() == "priorities" for name, _ in secs)
        updated = [(name, replacement if name.casefold() == "priorities" else value)
                   for name, value in secs]
        return pre, updated if found else [*updated, ("Priorities", replacement)]

    return _mutate(fn)


def remove_section(name: str) -> dict:
    def fn(pre, secs):
        return pre, [(n, b) for n, b in secs if n.lower() != name.lower()]
    return _mutate(fn)


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("verb", choices=("get", "sections", "set", "append", "remove", "prioritize"))
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
    if not a.section and a.verb != "prioritize":
        print(json.dumps({"ok": False, "error": f"{a.verb} needs --section"}))
        return 2
    try:
        if a.verb == "prioritize":
            print(json.dumps(add_priority(a.text or "")))
            return 0
        elif a.verb == "remove":
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
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
