"""The shared eligibility and exact source view of stale Gmail requests."""
import re
from email.utils import getaddresses

import source_context
from prep_selection import known_identities
from textutil import _arr, _s, _is_likely_automated
from timeutil import parse_observed_time

_CALENDAR_SUBJECT_RE = re.compile(
    r"^\s*(?:re:\s*|fwd?:\s*)*(?:accepted|declined|tentatively accepted|tentative|invitation|"
    r"updated invitation|cancell?ed event|new event|proposed new time|reminder)\s*:.*\s@\s", re.I)


def _latest_inbound_by_thread(emails):
    latest = {}
    for message in source_context.project_emails(emails):
        if not isinstance(message, dict) or not message.get("id") or message.get("isSent") is not False:
            continue
        labels = _arr(message, "labelIds")
        if {"SENT", "DRAFT"} & {str(label).upper() for label in labels}:
            continue
        thread = _s(message.get("threadId"))
        stamp = parse_observed_time(message.get("date") or message.get("timestamp"))
        if thread and stamp and (thread not in latest or latest[thread] < stamp):
            latest[thread] = stamp
    return latest


def _has_later_inbound(thread, sent_at, latest):
    inbound_at = latest.get(_s(thread))
    if inbound_at is None:
        return False
    original_at = parse_observed_time(sent_at)
    return bool(original_at and inbound_at > original_at)


def filter_threads(rows, local, emails=()):
    if not rows or not source_context.allowed("gmail"):
        return []
    known, _names = known_identities(local)
    contact_addresses = known_identities({"contacts": _arr(local, "contacts")})[0]
    if not source_context.allowed("contacts"):
        known, _names = known_identities({"person_knowledge": local.get("person_knowledge")})
        contact_addresses.clear()
    latest_inbound = _latest_inbound_by_thread(emails)
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        message = row.get("sent_message")
        sent_at = (message.get("date") if isinstance(message, dict) else None) or row.get("sentDate")
        if _has_later_inbound(row.get("threadId"), sent_at, latest_inbound):
            continue
        if not isinstance(message, dict):
            address = _s(row.get("toEmail")).lower().strip()
            if address in known and not _is_likely_automated(address) and not _CALENDAR_SUBJECT_RE.search(
                    _s(row.get("subject"))):
                out.append(row)
            continue
        recipients = getaddresses([_s(message.get("to")), _s(message.get("cc"))])
        addresses = {_s(addr).lower().strip() for _name, addr in recipients}
        eligible = sorted(addr for addr in addresses & known if not _is_likely_automated(addr))
        if eligible and not _CALENDAR_SUBJECT_RE.search(_s(message.get("subject")) or _s(row.get("subject"))):
            deps = ["contacts", "gmail"] if set(eligible) & contact_addresses else ["gmail"]
            out.append({**row, "toEmail": eligible[0],
                        "sent_message": {**message, "_loop_context_sources": deps, "_stale_targets": eligible}})
    return out


def untracked_threads(rows, ledger):
    tracked = {_s(row.get("source_thread_id")).strip() for row in ledger}
    return [row for row in rows if _s(row.get("threadId")) not in tracked]


def stale_waiting_on_evidence(google, local):
    """The same filtered, untracked native rows the prompt may cite to open a waiting-on."""
    emails = _arr(google, "emails")
    rows = filter_threads(_arr(local, "stale_threads") or _arr(google, "stale_threads")
                          or _arr(google, "staleThreads"), local, emails)
    recent = {_s(message.get("id")) for message in emails if isinstance(message, dict)}
    evidence = []
    for row in untracked_threads(rows, _arr(local, "action_ledger")):
        message = row.get("sent_message")
        if not isinstance(message, dict) or (ident := _s(message.get("id"))) in recent or not ident:
            continue
        if "SENT" in _arr(message, "labelIds") and "DRAFT" not in _arr(message, "labelIds"):
            evidence.append({**message, "isSent": True})
            recent.add(ident)
    return evidence




def capture_only_evidence(emails, stale):
    """Admitted stale rows can prove a new waiting-on. Current copies win by provider ID."""
    recent = {_s(row.get("id")) for row in emails if isinstance(row, dict)}
    latest_inbound = _latest_inbound_by_thread(emails)
    return [row for row in source_context.project_emails(stale) if isinstance(row, dict) and row.get("id")
            and _s(row["id"]) not in recent and "SENT" in _arr(row, "labelIds")
            and "DRAFT" not in _arr(row, "labelIds")
            and not _has_later_inbound(row.get("threadId"), row.get("date"), latest_inbound)]
