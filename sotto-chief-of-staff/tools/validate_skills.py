#!/usr/bin/env python3
"""Lint every SKILL.md in the tap against the agentskills spec (name + description required)."""
import glob
import os
import re
import sys

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _frontmatter(text: str):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return yaml.safe_load(m.group(1)) if m else None


def main() -> int:
    errors = []
    files = glob.glob(os.path.join(ROOT, "**", "SKILL.md"), recursive=True)
    if not files:
        print("no SKILL.md found"); return 1
    for path in files:
        with open(path, encoding="utf-8") as f:
            fm = _frontmatter(f.read())
        rel = os.path.relpath(path, ROOT)
        if not fm:
            errors.append(f"{rel}: missing/invalid frontmatter"); continue
        if not fm.get("name"):
            errors.append(f"{rel}: missing 'name'")
        elif not re.fullmatch(r"[a-z0-9-]{1,64}", fm["name"]):
            errors.append(f"{rel}: name must be lowercase-hyphen, <=64 ({fm['name']})")
        desc = fm.get("description", "")
        if not desc:
            errors.append(f"{rel}: missing 'description'")
        elif not re.match(r"use (only )?when", desc.lower()):
            errors.append(f"{rel}: description should start with 'Use when' ({desc[:40]}…)")
        elif len(desc) > 1024:
            errors.append(f"{rel}: description >1024 chars")
    if errors:
        print("SKILL.md validation FAILED:")
        for e in errors:
            print("  -", e)
        return 1
    print(f"OK — {len(files)} skills valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
