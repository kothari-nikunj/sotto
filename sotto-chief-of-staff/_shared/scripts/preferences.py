#!/usr/bin/env python3
"""
preferences.py — the EXPLICIT side of Sotto's preference memory.

The repo already learns preferences from BEHAVIOR (approval-tiers/learn_preferences.py tallies
outcomes.jsonl → preferences.json). What was missing is the explicit channel: the user saying "stop
surfacing newsletters", "don't flag Bob", "keep it terse". Those are precious — they must never be
wiped by the behavioral learner, which rewrites preferences.json wholesale. So we keep them in the
SAME file under a reserved `explicit` block, and learn_preferences.py carries that block forward
untouched on every run.

`compose_brief` reads these to suppress muted senders / people / sections and to honor tone notes.
The `sotto-feedback` skill writes them via this CLI. Pure stdlib; never raises on read.

CLI:
  preferences.py show
  preferences.py mute-sender <email-or-@domain>      # newsletters / noisy senders
  preferences.py mute-person "<display name>"        # stop flagging them in the brief
  preferences.py mute-section <section>               # e.g. birthdays, screen_time
  preferences.py tone "<short note>"                 # e.g. "keep it terse"
  preferences.py unmute-sender <v> | unmute-person "<v>" | unmute-section <v> | clear-tone
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

LISTS = ("mute_senders", "mute_people", "mute_sections", "tone_notes")


def _root() -> str:
    return os.environ.get("SOTTO_DATA", "/data")


def _path() -> str:
    return os.path.join(_root(), "preferences.json")


def _load_all() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def empty_explicit() -> dict:
    return {k: [] for k in LISTS}


def load_explicit() -> dict:
    """The user's explicit preferences, always shaped (missing lists default to empty)."""
    ex = (_load_all().get("explicit") or {})
    out = empty_explicit()
    for k in LISTS:
        v = ex.get(k)
        if isinstance(v, list):
            out[k] = [str(x) for x in v if str(x).strip()]
    return out


def _norm(kind: str, value: str) -> str:
    value = (value or "").strip()
    if kind == "mute_senders":
        return value.lower()          # emails/domains are case-insensitive
    return value


def _save(explicit: dict) -> None:
    data = _load_all()
    explicit["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["explicit"] = explicit
    os.makedirs(_root(), exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _path())


def add(kind: str, value: str) -> dict:
    if kind not in LISTS:
        raise ValueError(f"unknown preference list: {kind}")
    value = _norm(kind, value)
    ex = load_explicit()
    if value and value not in ex[kind]:
        ex[kind].append(value)
    _save(ex)
    return ex


def remove(kind: str, value: str) -> dict:
    if kind not in LISTS:
        raise ValueError(f"unknown preference list: {kind}")
    value = _norm(kind, value)
    ex = load_explicit()
    ex[kind] = [x for x in ex[kind] if x != value]
    _save(ex)
    return ex


def sender_is_muted(email: str, muted: list) -> bool:
    """True if an email address matches a muted sender — exact address, or an '@domain' / 'domain'
    suffix rule (so '@news.acme.com' or 'news.acme.com' mutes the whole sending domain)."""
    e = (email or "").strip().lower()
    if not e:
        return False
    dom = e.split("@", 1)[1] if "@" in e else e
    for m in muted:
        m = (m or "").strip().lower()
        if not m:
            continue
        if m.startswith("@"):                 # "@domain" → whole-domain rule
            rule = m[1:]
            if rule and (dom == rule or dom.endswith("." + rule)):
                return True
        elif "@" in m:                        # full address → exact match
            if e == m:
                return True
        else:                                 # bare "domain" → whole-domain rule
            if dom == m or dom.endswith("." + m):
                return True
    return False


_CLI = {
    "mute-sender": ("mute_senders", add), "unmute-sender": ("mute_senders", remove),
    "mute-person": ("mute_people", add), "unmute-person": ("mute_people", remove),
    "mute-section": ("mute_sections", add), "unmute-section": ("mute_sections", remove),
    "tone": ("tone_notes", add),
}


def main():
    if len(sys.argv) < 2:
        print(json.dumps(load_explicit())); return
    cmd = sys.argv[1]
    if cmd == "show":
        print(json.dumps(load_explicit())); return
    if cmd == "clear-tone":
        ex = load_explicit(); ex["tone_notes"] = []; _save(ex)
        print(json.dumps(ex)); return
    if cmd not in _CLI:
        print(json.dumps({"error": f"unknown command: {cmd}"})); sys.exit(2)
    kind, fn = _CLI[cmd]
    value = sys.argv[2] if len(sys.argv) > 2 else ""
    if not value.strip():
        print(json.dumps({"error": "missing value"})); sys.exit(2)
    print(json.dumps(fn(kind, value)))


if __name__ == "__main__":
    main()
