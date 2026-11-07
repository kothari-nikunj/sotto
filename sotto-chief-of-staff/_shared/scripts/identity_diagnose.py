#!/usr/bin/env python3
"""
identity_diagnose.py — read-only operator diagnostic for a cross-channel duplicate open loop.

For every display name with open continuity rows on more than one channel, prints one JSON line per
row with only identity evidence (no summary, ask or message text): channel, stored canonical_id and
whether the people graph knows it, contact_identifier under the anchor and graph normalizers, the
unique-identifier owner, anchor_key, provenance kind, and whether a cached Apple Contacts card links
this identifier to another row of the same group. It writes nothing and never merges or re-keys:
grouping by display name here is only a question to inspect, never an identity.

Usage: SOTTO_DATA=/data identity_diagnose.py [--all]   (--all: every open row, not just split names)
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from textutil import _s  # noqa: E402
import jsonstore  # noqa: E402
import ledger_io  # noqa: E402


def _resolver():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "morning-brief",
                        "scripts", "continuity_resolve.py")
    spec = importlib.util.spec_from_file_location("identity_diagnose_continuity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cached_contacts() -> list:
    """Contacts from the last local snapshot, read directly (the snapshot loader may record status)."""
    path = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "knowledge", "last_local_snapshot.json")
    snapshot = jsonstore.read(path, default={})
    contacts = ((snapshot.get("local") or {}) if isinstance(snapshot, dict) else {}).get("contacts")
    return contacts if isinstance(contacts, list) else []


def _card_holds(cr, card: set, identifier: str) -> bool:
    """The resolver's own exact handle rules (email, phone digits, WhatsApp phone JID); never a name."""
    return bool(identifier) and any(cr._handle_matches(value, identifier) or cr._jid_matches_phone(identifier, value)
                                    for value in card)


def diagnose(include_all: bool = False, now=None) -> list[dict]:
    cr = _resolver()
    identity = cr.person_identity()
    known = (identity.get("index") or {}).get("by_cid") or {}
    unique = identity.get("unique_identifiers") or {}
    contacts = _cached_contacts()
    rows = ledger_io.load_active(now or datetime.now(timezone.utc))
    groups: dict = {}
    for row in rows:
        groups.setdefault(cr._normalize_name_for_dedup(_s(row.get("contact_name"))), []).append(row)
    out = []
    for name_key, members in sorted(groups.items()):
        channels = {cr.normalize_channel(_s(row.get("channel"))) for row in members}
        if not include_all and len(channels) < 2:
            continue
        cards = []
        for row in members:
            raw = _s(row.get("contact_identifier")).strip()
            cards.append({_s(v).strip().lower() for v in cr._collect_all_identifiers(raw, "", {"contacts": contacts})
                          if _s(v).strip()} if raw else set())
        for position, row in enumerate(members):
            raw = _s(row.get("contact_identifier")).strip()
            stored = _s(row.get("canonical_id")).strip().lower()
            provenance = cr._valid_provenance(row)
            linked = sorted(other for other, member in enumerate(members) if other != position
                            and _card_holds(cr, cards[position], _s(member.get("contact_identifier")).strip()))
            out.append({
                "group": name_key, "row": position, "channel": _s(row.get("channel")),
                "action_type": _s(row.get("action_type")), "canonical_id": stored,
                "canonical_id_in_graph": bool(stored and stored in known),
                "contact_identifier": raw,
                "anchor_normalized": cr._normalize_identifier_for_anchor(raw) if raw else None,
                "graph_normalized": cr.alias_identifier(raw) if raw else None,
                "unique_identifier_owner": unique.get(cr.alias_identifier(raw)) if raw else None,
                "group_id": _s(row.get("group_id")), "anchor_key": _s(row.get("anchor_key")),
                "provenance": provenance[0] if provenance else None,
                "contacts_card_found": len(cards[position]) > 1,
                "contacts_card_links_rows": linked,
            })
    return out


def main():
    for line in diagnose("--all" in sys.argv[1:]):
        print(json.dumps(line, sort_keys=True, default=_s))


if __name__ == "__main__":
    main()
