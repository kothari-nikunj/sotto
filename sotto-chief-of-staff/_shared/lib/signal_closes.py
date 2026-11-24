"""Row shape of a task Sotto closed on its own signal (`morning-brief/scripts/loop_check.py`).

One rule per transition, all written through continuity_resolve._persist:
  - close: an open task becomes resolved with a `resolution_basis` (basis, source, time) and the
    signal's source reference; no message text.
  - reported: the brief (evening or morning) that named the close was accepted for delivery (named once).
  - reopen: the owner said the close was wrong. The same row opens again, that exact signal
    (basis + source id, or a call's stable key) is recorded as rejected, and nothing that happened
    before the undo (`rejected_before`) can close the row again.
`fulfillment` / `deliver_to` belong to one ask: only a new row or a reopen by a new ask sets them.
"""
from __future__ import annotations

CLOSE_FIELDS = frozenset({"status", "resolution", "resolved_at", "closed_at", "resolution_source_refs",
                          "resolution_loop_version", "resolution_basis", "proof_transition"})
REOPEN_FIELDS = CLOSE_FIELDS | {"reopened_at", "rejected_signals", "rejected_before"}
BASES = frozenset({"intro_sent", "scheduled_meeting", "called"})


def is_signal_close(row: dict) -> bool:
    basis = row.get("resolution_basis")
    return (row.get("status") == "resolved" and isinstance(basis, dict)
            and basis.get("basis") in BASES and basis.get("basis") == row.get("resolution"))


def unreported(rows, now):
    import os
    from datetime import datetime, timezone
    from jsonstore import read, Unreadable
    now = now or datetime.now(timezone.utc)
    pending = [row for row in rows if row.get("anchor_key") and _pending(row, now)]
    if not pending:
        return []
    try:
        outbox = read(os.path.join(os.environ.get("SOTTO_DATA", "/data"), "events", "outbox.json"), {}, strict=True)
        reservations = _reserved_notices(outbox.get("rows", []))
        return [row for row in pending if not _reserved(notice(row), reservations)]
    except (AttributeError, TypeError, ValueError, Unreadable):
        return []  # An unreadable receipt can withhold a notice, never the rest of a brief.


def _pending(row, now):
    from datetime import datetime
    if not is_signal_close(row) or row["resolution_basis"].get("reported_at"):
        return False
    try:
        recorded = datetime.fromisoformat(str(row["resolution_basis"].get("recorded_at") or ""))
    except ValueError:
        return False
    return recorded.tzinfo is not None and recorded <= now


def notice(row):
    from delivery_effects import closure_version
    from ledger_provenance import source_loop_sources
    return {"kind": "closure_notice", "anchor_key": row["anchor_key"],
            "closure_version": closure_version(row), "recorded_at": row["resolution_basis"]["recorded_at"],
            "sources": sorted(source_loop_sources(row))}


def _matches_notice(row, effect):
    try:
        return notice(row) == effect
    except ValueError:
        return False


def notice_valid(row, effect, now):
    from datetime import datetime, timezone
    from source_context import allowed
    return (bool(row) and _pending(row, datetime.fromtimestamp(now, timezone.utc)) and _matches_notice(row, effect)
            and all(allowed(source) for source in effect["sources"]))


def _reserved_notices(rows, key=None):
    return [effect for row in rows if isinstance(row, dict) and row.get("id") != key
            and row.get("acceptance") in ("accepted", "in_flight", "unknown")
            for effect in (row.get("payload") or {}).get("effects") or []
            if effect.get("kind") == "closure_notice"]


def _reserved(effect, reservations):
    return any(prior.get("anchor_key") == effect.get("anchor_key") and (
        "closure_version" not in prior or
        (prior.get("closure_version"), prior.get("recorded_at")) ==
        (effect.get("closure_version"), effect.get("recorded_at"))) for prior in reservations)


def send_reserved(effects, rows, key):
    """A matching accepted or ambiguous outbox delivery owns the notice until resolved."""
    reservations = _reserved_notices(rows, key)
    return any(_reserved(effect, reservations) for effect in effects if effect.get("kind") == "closure_notice")


def _source_id(row: dict) -> str:
    refs = row.get("resolution_source_refs") or []
    return str(refs[0].get("sourceId") or "") if refs and isinstance(refs[0], dict) else ""


def rejected(row: dict, basis: str, source_id: str, key: str = "") -> bool:
    """The owner rejected this signal: same source id, or the same call (its id changes, its key does not)."""
    return any(isinstance(item, dict) and item.get("basis") == basis
               and (item.get("source_id") == source_id or (key and item.get("key") == key))
               for item in row.get("rejected_signals") or [])


def before_undo(row: dict, at: str) -> bool:
    """The signal happened no later than the owner's last undo, so it is part of what was rejected."""
    from datetime import datetime, timezone
    try:
        undone, when = datetime.fromisoformat(str(row.get("rejected_before") or "")), datetime.fromisoformat(at)
    except ValueError:
        return False
    utc = lambda value: value if value.tzinfo else value.replace(tzinfo=timezone.utc)  # noqa: E731
    return utc(when) <= utc(undone)


def reopen(row: dict, today: str, now=None, reject: bool = True) -> None:
    """Undo one automatic close in place: no second row. An owner undo rejects the signal and
    everything before the undo; a cancelled meeting (`reject=False`) only reopens."""
    from datetime import datetime, timezone
    if not is_signal_close(row):
        raise ValueError("only an automatic close can be reopened")
    basis = row["resolution_basis"]
    signal = {"basis": basis["basis"], "source_id": _source_id(row),
              **({"key": basis["key"]} if basis.get("key") else {})}
    for key in CLOSE_FIELDS - {"status", "proof_transition"}:
        row.pop(key, None)
    row["status"], row["reopened_at"] = "open", today
    if reject:
        row["rejected_signals"] = [*(row.get("rejected_signals") or []), signal]
        row["rejected_before"] = (now or datetime.now(timezone.utc)).isoformat()


def restamp(proof: dict, it: dict, sources_of) -> dict:
    """A corrected source row keeps its proof; a `sources` proof names the row's sources after the change."""
    if proof.get("kind") != "sources":
        return dict(proof)
    return {**proof, "sources": sorted(sources_of({k: v for k, v in it.items() if k != "content_provenance"}))}


def fulfillment_fields(get) -> dict:
    """What would fulfill a captured task (loop_check closes intro/schedule/callback asks on their signal)."""
    kind = get("fulfillment")
    target = get("deliver_to") or get("deliverTo")
    return {"fulfillment": kind if kind in ("intro", "schedule", "callback", "none") else None,
            "deliver_to": (target.strip().lower() if kind != "none" and isinstance(target, str) else "") or None}


def callback_ask(row: dict) -> bool:
    """The row's own ask is a callback; a call never closes anything else (a promised deliverable least of all)."""
    from ledger_io import normalize_action_type
    return normalize_action_type(row.get("action_type")) == "call_back" or row.get("fulfillment") == "callback"


def new_ask(it: dict, action: dict) -> None:
    """A closed row reopened by a new ask: the old close goes, and what fulfills it is the new ask's or nothing."""
    for key in ("resolution", "resolved_at", "closed_at", "resolution_reason", "resolution_evidence",
                "resolution_source_refs", "resolution_loop_version", "resolution_basis", "callback_since"):
        it.pop(key, None)
    for key in ("fulfillment", "deliver_to"):
        it.pop(key, None)
        if action.get(key):
            it[key] = action[key]


def fold_fulfillment(keeper: dict, loser: dict) -> None:
    """Preserve an explicit refusal to auto-close, or a signal both asks named."""
    if "none" in (keeper.get("fulfillment"), loser.get("fulfillment")):
        keeper["fulfillment"] = "none"
        keeper.pop("deliver_to", None)
    elif any(keeper.get(key) != loser.get(key) for key in ("fulfillment", "deliver_to")):
        keeper.pop("fulfillment", None), keeper.pop("deliver_to", None)


def keep_callback(it: dict, since: str, is_after) -> None:
    """A later missed call on an open callback ask (captured or legacy) moves its call cutoff forward;
    no other row takes one."""
    if since and not it.get("group_id") and callback_ask(it):
        previous = str(it.get("callback_since") or "")
        if not previous or is_after(since, previous):
            it["callback_since"] = since


def _changed(previous: dict, it: dict) -> set:
    ignored = {"_path", "content_provenance"}
    return {key for key in (set(previous) | set(it)) - ignored if previous.get(key) != it.get(key)}


def persist_guard(previous: dict | None, it: dict, op: str):
    """Check one close/reported/reopen write; return the restamped provenance (or None if unstamped)."""
    from ledger_provenance import classify_ledger_sources, source_loop_sources
    if previous is None:
        raise ValueError("signal close requires an existing row")
    changed = _changed(previous, it)
    if op == "close":
        ok = (previous.get("status", "open") in ("open", "waiting", "failed", "blocked", "parked")
              and previous.get("resolution_mode") != "explicit" and is_signal_close(it)
              and changed <= CLOSE_FIELDS | {"reopened_at"})
    elif op == "reported":
        ok = (is_signal_close(previous) and changed == {"resolution_basis"}
              and {**previous["resolution_basis"], "reported_at": it["resolution_basis"].get("reported_at")}
              == it["resolution_basis"])
    elif op == "reopen":
        ok = is_signal_close(previous) and it.get("status") == "open" and changed <= REOPEN_FIELDS
    else:
        ok = False
    if not ok:
        raise ValueError(f"signal {op} changed unrelated content")
    proof = previous.get("content_provenance")
    if proof is None:
        return None
    try:
        kind, _sources = classify_ledger_sources(previous)
    except ValueError:
        return None                       # already stale: never heal provenance here
    if kind != "sources":
        raise ValueError("signal close requires a source row")
    sources = source_loop_sources({k: v for k, v in it.items() if k != "content_provenance"})
    return {**proof, "sources": sorted(sources)}


def acknowledge(effect: dict, accepted_at) -> bool:
    """The brief that named this close was accepted: record it so no later brief names it again."""
    import os
    import sys
    from datetime import datetime, timezone
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                    "morning-brief", "scripts"))
    import continuity_resolve as cr
    from delivery_effects import instant, loop_version
    accepted = instant(accepted_at)
    if accepted is None:
        return False
    with cr._ledger_lock():
        row = cr._load_items().get(str(effect.get("anchor_key") or ""))
        if not row or not is_signal_close(row) or row["resolution_basis"].get("reported_at"):
            return True
        if ("closure_version" in effect and not _matches_notice(row, effect)
                or "closure_version" not in effect and loop_version(row) != effect.get("loop_version")):
            return True  # Accept receipts already issued for legacy effects, without sending them again.
        row["resolution_basis"] = {**row["resolution_basis"],
                                   "reported_at": datetime.fromtimestamp(accepted, timezone.utc).isoformat()}
        cr._persist(row, signal_close="reported")
    return True
