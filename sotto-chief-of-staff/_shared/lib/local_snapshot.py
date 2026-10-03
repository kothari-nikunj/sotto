"""Consent-filtered local snapshot resolution shared by brief composition and its runner.

An effective view retains the observation time of carried sources and never revives a
revoked source. The runner resolves this once before knowledge query and loop resolution.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from source_catalog import LOCAL_SNAPSHOT_TTL_HOURS, SOURCE_FIELDS
from textutil import _s
from timeutil import _parse_ts

SNAPSHOT_ARCHIVE_DAYS = 60

# ── No-Bridge fallback: cache the last good read_local so an asleep Mac degrades to yesterday's ──
# local data instead of a Google-only brief. The snapshot is the raw read_local payload + a stamp.
_LOCAL_SOURCE_KEYS = tuple(dict.fromkeys(
    field for fields in SOURCE_FIELDS.values() for field in fields
))




def _local_has_data(local: dict) -> bool:
    return any(local.get(k) for k in _LOCAL_SOURCE_KEYS)




def _snapshot_path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "knowledge", "last_local_snapshot.json")




def _snapshot_age_hours(captured_at: str):
    """Hours since the snapshot was captured, or None if the stamp can't be parsed. Handles the ISO
    `generated_at` form and the naive 'YYYY-MM-DD HH:MM:SS' fallback (treated as UTC)."""
    ts = _parse_ts(captured_at)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0




def _live_local_observation(local: dict) -> bool:
    """A successful empty read still observes the source; row counts never prove availability."""
    return isinstance(local, dict) and (bool(local.get("source_status")) or _local_has_data(local))


def _merge_local_snapshot(local: dict, snapshot: dict) -> dict:
    from source_context import SOURCE_FIELDS, project_local
    incoming = project_local(local)
    cached = project_local(snapshot.get("local") or {})
    if not cached:
        return incoming
    result = dict(incoming)
    status = dict(incoming.get("source_status") or {})
    availability = dict(incoming.get("_source_availability") or {})
    observed = dict(incoming.get("_source_observed_at") or {})
    prior_observed = cached.get("_source_observed_at") or {}
    prior_status = cached.get("source_status") or {}
    live_stamp = incoming.get("generated_at") or datetime.now(timezone.utc).isoformat()
    stale, unavailable = [], []
    for source, fields in SOURCE_FIELDS.items():
        known = source in status or source in prior_status or any(k in incoming or k in cached for k in fields)
        if not known:
            continue
        choice = status.get(source)
        if choice == "disabled" or prior_status.get(source) == "disabled" and not choice:
            for field in fields:
                result.pop(field, None)
            status[source] = "disabled"
            availability[source] = "disabled"
            continue
        # A partial/degraded read can still contain the freshest rows. Keep every valid field it
        # explicitly returned, including an empty list, but never blend in cached sibling fields:
        # one source timestamp cannot truthfully age a mixture of fresh and cached observations.
        if choice in ("partial", "degraded"):
            for field in fields:
                value = incoming.get(field)
                valid = (isinstance(value, dict) if field == "screen_time"
                         else isinstance(value, int) and not isinstance(value, bool)
                         if field == "contacts_total" else isinstance(value, list))
                if field not in incoming or not valid:
                    result.pop(field, None)
            observed[source] = live_stamp
            status[source] = choice
            availability[source] = "partial"
            continue
        # Current explicit ok plus empty data is a valid empty observation. Contacts without
        # a status remain the existing thin-pull case, so identity carry-forward still works.
        successful = choice == "ok" or (choice is None and any(result.get(k) for k in fields))
        if successful:
            observed[source] = live_stamp
            continue
        previous_stamp = prior_observed.get(source) or snapshot.get("captured_at")
        age = _snapshot_age_hours(previous_stamp)
        # Contacts are stable identity context; a live thin/failed pull can retain that identity
        # cache. A wholly offline snapshot still obeys the same 24-hour bound as before.
        usable = age is not None and 0 <= age <= LOCAL_SNAPSHOT_TTL_HOURS
        if source == "contacts" and _live_local_observation(incoming):
            usable = True
        carried = False
        for field in fields:
            result.pop(field, None)
            if usable and field in cached:
                result[field] = cached[field]
                carried = carried or bool(cached[field])
        observed[source] = previous_stamp
        status[source] = choice or "unavailable"
        availability[source] = "unavailable"
        if carried and previous_stamp:
            stale.append(previous_stamp)
        elif previous_stamp:
            unavailable.append(previous_stamp)
    if status:
        result["source_status"] = status
    if availability:
        result["_source_availability"] = availability
    if observed:
        result["_source_observed_at"] = observed
    if stale:
        result["_local_stale_since"] = min(stale)
    if unavailable and not stale:
        result["_local_unavailable_since"] = min(unavailable)
    return project_local(result)


def _save_local_snapshot(local: dict) -> dict:
    """One locked writer: preserve per-source freshness and never revive disabled sources."""
    import jsonstore
    from source_context import bridge_request_epoch, project_local, record_bridge_status
    record_bridge_status(local)
    merged = project_local(local)
    try:
        path = _snapshot_path()
        stamp = merged.get("generated_at") or datetime.now(timezone.utc).isoformat()
        with jsonstore.transaction(path, default={}) as previous:
            old_age, new_age = _snapshot_age_hours(previous.get("captured_at")), _snapshot_age_hours(stamp)
            old_request = bridge_request_epoch(previous.get("local"))
            new_request = bridge_request_epoch(merged)
            older = old_age is not None and new_age is not None and old_age < new_age
            if old_request is not None and new_request is not None:
                older = new_request < old_request
            elif new_request is not None and old_age is not None and old_age < 0:
                older = False  # migrate a legacy future-stamped snapshot on a real server read
            if older:
                # A slower old wake/brief input cannot roll the latest valid snapshot backwards.
                return project_local(previous.get("local") or {})
            merged = _merge_local_snapshot(merged, previous)
            previous.clear()
            previous.update(captured_at=stamp, local=merged)
        # The corpus archive is a projection of the same observation, never a competing writer.
        arch_dir = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "knowledge", "snapshots")
        os.makedirs(arch_dir, exist_ok=True)
        day = _s(stamp)[:10]
        arch = os.path.join(arch_dir, f"{day}.json")
        with jsonstore.transaction(arch, default={}) as archived:
            if not archived.get("captured_at") or _s(archived["captured_at"]) <= _s(stamp):
                archived.clear()
                archived.update(captured_at=stamp, local=merged)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_ARCHIVE_DAYS)).strftime("%Y-%m-%d")
        for old in os.listdir(arch_dir):
            if old.endswith(".json") and old[:10] < cutoff:
                os.remove(os.path.join(arch_dir, old))
    except (OSError, ValueError, TypeError, jsonstore.Unreadable):
        pass  # a broken cache never makes live context unavailable
    return merged


def _local_fallback(local: dict) -> dict:
    """Reuse only permitted, genuinely unavailable sources, with their original observation age."""
    import jsonstore
    from source_context import project_local, record_bridge_status
    if _live_local_observation(local):
        record_bridge_status(local)
    snapshot = jsonstore.read(_snapshot_path(), default={})
    return _merge_local_snapshot(local, snapshot) if snapshot else project_local(local)


def resolve_effective_local(local: dict) -> dict:
    """Resolve one live read against permitted cached sources, preserving original provenance."""
    return _save_local_snapshot(local) if _live_local_observation(local) else _local_fallback(local)


