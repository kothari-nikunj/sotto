"""The permitted source snapshot that loop evidence checks read (the continuity payload's local,
email and calendar data), shared by the ledger resolver and the loop check step."""
from __future__ import annotations

from textutil import unwrap_tool_result


def snapshot_local(payload: dict) -> dict:
    """Permitted source snapshot for evidence checks, unwrapping a raw MCP tool-result wrapper."""
    from source_context import allowed, project_emails, project_local
    local_data = project_local(unwrap_tool_result(payload.get("local") or {}))
    if not allowed("gmail"):
        local_data.pop("emails", None)
    elif "emails" in local_data:
        local_data["emails"] = project_emails(local_data["emails"])
    if not allowed("calendar"):
        local_data.pop("events", None)
    elif payload.get("events") and "events" not in local_data:
        local_data = {**local_data, "events": payload["events"]}
    email_data = payload.get("emails")
    if isinstance(email_data, dict):
        email_data = email_data.get("emails") or email_data.get("messages") or email_data.get("items") or []
    email_data = project_emails(email_data)
    if allowed("gmail") and isinstance(email_data, list) and "emails" not in local_data:
        local_data = {**local_data, "emails": email_data}
    stale = local_data.pop("stale_sent", None)
    local_data.pop("stale_capture", None)
    if isinstance(stale, list) and stale and allowed("gmail"):
        from stale_sent import capture_only_evidence
        local_data["stale_capture"] = capture_only_evidence(local_data.get("emails", []), stale)
    return local_data
