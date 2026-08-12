#!/usr/bin/env python3
"""
pending_offer.py — the one question Sotto is currently waiting on an answer to.

THE BUG THIS EXISTS FOR (real WhatsApp transcript, Aug 2026): the 11:15 proactive tick sent
"You're meeting Shivani in ~44 min at Sightglass — want me to pull full prep on her?". At 11:44
the user replied "Sure". The gateway session that received "Sure" had never seen the question —
the nudge was composed and delivered by a DETACHED run (the cron lane today, `hermes send`
tomorrow) — so it resolved the bare affirmative against the last thing in its own history, a
group-chat item from the morning brief, and answered about that. The user had to retype "Give me
full prep on Shivani".

No prompt can fix a session that does not contain the question. So the question is written down
where the other process can read it: the lane that ASKS calls `set`, the gateway that receives the
bare "sure" calls `get`, acts on what comes back, and calls `clear`.

    pending_offer.py set --kind meeting_prep --person "Shivani" \
        --question "You're meeting Shivani in ~44 min at Sightglass — want me to pull full prep on her?"
    pending_offer.py get      → the offer JSON if fresh, `{}` if absent or expired
    pending_offer.py clear    → removes it

ONE offer at a time, newest wins. Two stacked questions is a user who should be ASKED which one
they meant, not guessed at — and the newer question is the one on their screen.

Expiry is checked at READ (`expires_at`, default 180 min): nothing daemonic, no sweeper, and a
stale file simply never answers. "Sure" three hours after the meeting started is not a yes to it.

Ephemeral by construction: `{ts, kind, question, person, detail, expires_at}` and nothing else.
This is not memory — a question the user answered (or didn't) three months ago could not be used
by any brief or prep, so nothing here is ever promoted to the graph.

State: `$SOTTO_DATA/proactive/pending_offer.json`, read/written under jsonstore's lock because the
writer (proactive lane) and the reader (gateway) are different processes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jsonstore  # noqa: E402

KINDS = ("meeting_prep", "commitment", "chase", "handoff", "retune_offer")
DEFAULT_TTL_MIN = 180   # a question goes stale in three hours; a named constant, not a knob


def _path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "proactive", "pending_offer.json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str):
    try:
        d = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def set_offer(kind: str, question: str, person: str = "", detail: str = "",
              ttl_min: int = DEFAULT_TTL_MIN) -> dict:
    """Record the question just delivered. Overwrites: newest wins, one offer at a time."""
    now = _now()
    offer = {
        "ts": now.isoformat(),
        "kind": kind,
        "question": question,
        "person": person or "",
        "detail": detail or "",
        "expires_at": (now + timedelta(minutes=ttl_min)).isoformat(),
    }
    path = _path()
    with jsonstore.lock(path):
        jsonstore.write_atomic(path, offer)
    return offer


def get_offer() -> dict:
    """The fresh offer, or `{}` — absent, unparseable, or past `expires_at`. Never partial: a
    caller gets a whole question it can act on, or nothing it could mistake for one."""
    path = _path()
    with jsonstore.lock(path):
        data = jsonstore.read(path, {})
    if not isinstance(data, dict) or not data.get("question"):
        return {}
    expires = _parse(data.get("expires_at", ""))
    if expires is None or _now() >= expires:
        return {}
    return data


def clear_offer() -> bool:
    """Drop the offer once it has been acted on (or explicitly declined). True if one was there."""
    path = _path()
    with jsonstore.lock(path):
        try:
            os.remove(path)
        except FileNotFoundError:
            return False
        except OSError:
            return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("set", help="record the question just delivered (overwrites)")
    s.add_argument("--kind", required=True, choices=KINDS)
    s.add_argument("--question", required=True, help="the sentence as delivered, verbatim")
    s.add_argument("--person", default="", help="who the offer is about, if it names someone")
    s.add_argument("--detail", default="", help="free text the acting session may need")
    s.add_argument("--ttl-min", type=int, default=DEFAULT_TTL_MIN)

    sub.add_parser("get", help="print the fresh offer, or {}")
    sub.add_parser("clear", help="remove the offer")

    a = ap.parse_args()
    if a.cmd == "set":
        print(json.dumps(set_offer(a.kind, a.question, a.person, a.detail, a.ttl_min)))
    elif a.cmd == "get":
        print(json.dumps(get_offer()))
    else:
        print("cleared" if clear_offer() else "none")


if __name__ == "__main__":
    main()
