#!/usr/bin/env python3
"""
render_local.py — the per-source `formatSourceForLLM`-style renderers for the brief pipeline.

Extracted verbatim from compose_brief.py (the 2,400-line monolith split) with ZERO behavior
change. This is the big block that turns the LocalData / Google / Granola payload into the FLEX
prompt's data section, exactly like the Mac backend's gemini-flex.ts helpers: contact-name
resolution + call processing (contacts.ts port), thread grouping / needs-response / known-person
filtering (thread-processing.ts port), the external-attendee research selection, and every
_format_* section renderer (emails, calendar, reminders, deferred/stale/commitments, knowledge,
browsing, escalation, signals, …).

Depends on textutil (accessors, phone/name/domain helpers) and timeutil (_parse_ts / _date_only).
compose_brief.py re-exports every name here at its old location for the `import compose_brief as
cb` compat surface (select_attendees.py imports select_attendees_for_research directly).
"""
from __future__ import annotations

import os
import re
import sys
# NOTE (test-freeze semantics after the compose_brief split): helpers here (e.g. _action_age) read
# THIS module's `datetime`. Monkeypatching `cb.datetime` no longer reaches them — that only rebinds the
# name in compose_brief. To freeze "now" for these helpers in a test, patch `render_local.datetime`.
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from textutil import (  # noqa: E402
    _arr, _obj, _s, _digits, normalize_phone_for_comparison, _format_phone_for_display,
    _normalize_identifier, _names_match, _looks_like_phone_number, _is_likely_automated,
)
from timeutil import _parse_ts, _date_only  # noqa: E402

# Mirrors api/src/lib/constants.ts
DEFERRED_UNREAD_PROMPT_CAP = 15
EMAIL_BODY_MAX = 30000


def build_contact_lookup(contacts: list) -> dict:
    lookup: dict[str, str] = {}
    for c in contacts:
        name = _s(c.get("name"))
        if not name:
            continue
        for phone in _arr(c, "phones"):
            d = _digits(phone)
            if not d:
                continue
            lookup[d] = name
            if len(d) >= 10:
                lookup[d[-10:]] = name
            if len(d) >= 7:
                lookup[d[-7:]] = name
            if len(d) == 11 and d.startswith("1"):
                lookup[d[1:]] = name
        for email in _arr(c, "emails"):
            if email:
                lookup[email.lower().strip()] = name
    return lookup




def resolve_imessage_name(handle: str, lookup: dict) -> str:
    if not handle:
        return "Unknown"
    if "@" in handle:
        el = handle.lower().strip()
        return lookup.get(el) or handle.split("@")[0]
    d = _digits(handle)
    if lookup.get(d):
        return lookup[d]
    if len(d) == 11 and d.startswith("1") and lookup.get(d[1:]):
        return lookup[d[1:]]
    if len(d) >= 10 and lookup.get(d[-10:]):
        return lookup[d[-10:]]
    if len(d) >= 7 and lookup.get(d[-7:]):
        return lookup[d[-7:]]
    if len(d) > 11:
        first11 = d[0:11]
        if first11.startswith("1") and lookup.get(first11[1:]):
            return lookup[first11[1:]]
        if lookup.get(first11):
            return lookup[first11]
        first10 = d[0:10]
        if lookup.get(first10):
            return lookup[first10]
        if d.startswith("1") and lookup.get(d[1:11]):
            return lookup[d[1:11]]
    return _format_phone_for_display(d)




def resolve_whatsapp_name(jid: str, partner_name: str, lookup: dict) -> str:
    if partner_name and partner_name.strip():
        return partner_name.strip()
    if not jid:
        return "Unknown"
    if lookup.get(jid):
        return lookup[jid]
    d = _digits(jid.split("@")[0])
    if lookup.get(d):
        return lookup[d]
    if len(d) == 11 and d.startswith("1") and lookup.get(d[1:]):
        return lookup[d[1:]]
    if len(d) >= 10 and lookup.get(d[-10:]):
        return lookup[d[-10:]]
    return _format_phone_for_display(d)




def resolve_call_name(phone: str, lookup: dict):
    if not phone:
        return None
    d = _digits(phone)
    if not d:
        return None
    if lookup.get(d):
        return lookup[d]
    if len(d) == 11 and d.startswith("1") and lookup.get(d[1:]):
        return lookup[d[1:]]
    if len(d) >= 10 and lookup.get(d[-10:]):
        return lookup[d[-10:]]
    if len(d) >= 7 and lookup.get(d[-7:]):
        return lookup[d[-7:]]
    return None




def build_canonical_resolver(contact_index: list):
    by_id = {}
    by_identifier = {}
    for entry in contact_index or []:
        identity = {
            "canonical_id": entry.get("canonical_id"),
            "name": entry.get("display_name"),
            "confidence": entry.get("confidence") or "high",
        }
        by_id[identity["canonical_id"]] = identity
        for idv in _arr(entry, "identifiers"):
            by_identifier[_normalize_identifier(idv)] = identity
    return by_identifier, by_id




def _build_connected_dict(phone_calls: list, wa_calls: list) -> dict:
    connected: dict[str, datetime] = {}
    for call in phone_calls:
        if call.get("is_outgoing") or call.get("is_answered"):
            p = normalize_phone_for_comparison(_s(call.get("phone")))
            t = _parse_ts(_s(call.get("timestamp")))
            if t and (p not in connected or t > connected[p]):
                connected[p] = t
    for call in wa_calls:
        if call.get("is_outgoing") or not call.get("is_missed"):
            p = normalize_phone_for_comparison(_s(call.get("jid")).split("@")[0])
            t = _parse_ts(_s(call.get("timestamp")))
            if t and (p not in connected or t > connected[p]):
                connected[p] = t
    return connected




def _process_missed_calls(calls, lookup, connected):
    by_name = {}
    for call in calls:
        if call.get("is_outgoing") or call.get("is_answered"):
            continue
        name = resolve_call_name(_s(call.get("phone")), lookup)
        if not name:
            continue
        p = normalize_phone_for_comparison(_s(call.get("phone")))
        t = _parse_ts(_s(call.get("timestamp")))
        if p in connected and t and connected[p] > t:
            continue
        prev = by_name.get(name)
        if not prev or (t and t > (_parse_ts(prev["timestamp"]) or t)):
            by_name[name] = {"name": name, "phone": _s(call.get("phone")),
                             "timestamp": _s(call.get("timestamp")), "call_type": _s(call.get("call_type"))}
    return list(by_name.values())




def _process_wa_missed_calls(calls, lookup, connected):
    by_name = {}
    for call in calls:
        if call.get("is_outgoing") or not call.get("is_missed"):
            continue
        jid = _s(call.get("jid"))
        p = normalize_phone_for_comparison(jid.split("@")[0])
        name = resolve_whatsapp_name(jid, "", lookup)
        if not name or name.startswith("(") or name.startswith("+"):
            continue
        t = _parse_ts(_s(call.get("timestamp")))
        if p in connected and t and connected[p] > t:
            continue
        prev = by_name.get(name)
        if not prev or (t and t > (_parse_ts(prev["timestamp"]) or t)):
            by_name[name] = {"name": name, "phone": jid,
                             "timestamp": _s(call.get("timestamp")), "call_type": "whatsapp_call"}
    return list(by_name.values())




def _process_recent_calls(phone_calls, wa_calls, lookup):
    by_name = {}
    for call in phone_calls:
        if not call.get("is_outgoing") and not call.get("is_answered"):
            continue
        name = resolve_call_name(_s(call.get("phone")), lookup)
        if not name:
            continue
        direction = "outgoing" if call.get("is_outgoing") else "incoming"
        t = _parse_ts(_s(call.get("timestamp")))
        prev = by_name.get(name)
        if not prev or (t and t > (_parse_ts(prev["timestamp"]) or t)):
            by_name[name] = {"name": name, "phone": _s(call.get("phone")), "timestamp": _s(call.get("timestamp")),
                             "call_type": _s(call.get("call_type")), "duration_seconds": call.get("duration_seconds"),
                             "direction": direction}
    for call in wa_calls:
        if call.get("is_missed"):
            continue
        phone = _s(call.get("jid")).split("@")[0]
        name = resolve_call_name(phone, lookup)
        if not name:
            continue
        direction = "outgoing" if call.get("is_outgoing") else "incoming"
        t = _parse_ts(_s(call.get("timestamp")))
        prev = by_name.get(name)
        if not prev or (t and t > (_parse_ts(prev["timestamp"]) or t)):
            by_name[name] = {"name": name, "phone": phone, "timestamp": _s(call.get("timestamp")),
                             "call_type": "whatsapp", "direction": direction}
    return sorted(by_name.values(), key=lambda c: _parse_ts(c["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)




def resolve_contact_names(local: dict) -> dict:
    """Port of resolveContactNames — adds resolved_name to messages and computes missed/recent calls."""
    lookup = build_contact_lookup(_arr(local, "contacts"))
    by_identifier, _by_id = build_canonical_resolver(_arr(local, "contact_index"))

    def resolve_identity(idv):
        return by_identifier.get(_normalize_identifier(idv))

    out = dict(local)

    def _attach(m, identity):
        # Carry canonical_id + confidence so the known-person rescue (contacts.ts:286-293) works.
        if identity:
            if identity.get("canonical_id") and not m.get("canonical_id"):
                m["canonical_id"] = identity.get("canonical_id")
            if identity.get("confidence") and not m.get("confidence"):
                m["confidence"] = identity.get("confidence")
        return m

    def with_im_name(msg):
        identity = resolve_identity(_s(msg.get("handle")))
        fallback = resolve_imessage_name(_s(msg.get("handle")), lookup)
        m = dict(msg)
        m["resolved_name"] = identity["name"] if identity and identity.get("confidence") == "high" else fallback
        return _attach(m, identity)

    def with_wa_name(msg):
        identity = resolve_identity(_s(msg.get("contact_jid")))
        fallback = resolve_whatsapp_name(_s(msg.get("contact_jid")), _s(msg.get("partner_name")), lookup)
        m = dict(msg)
        m["resolved_name"] = identity["name"] if identity and identity.get("confidence") == "high" else fallback
        return _attach(m, identity)

    out["imessage"] = [with_im_name(m) for m in _arr(local, "imessage")]
    out["whatsapp"] = [with_wa_name(m) for m in _arr(local, "whatsapp")]
    out["deferred_unread_imessage"] = [with_im_name(m) for m in _arr(local, "deferred_unread_imessage")]
    out["deferred_unread_whatsapp"] = [with_wa_name(m) for m in _arr(local, "deferred_unread_whatsapp")]

    connected = _build_connected_dict(_arr(local, "calls"), _arr(local, "whatsapp_calls"))
    missed = _process_missed_calls(_arr(local, "calls"), lookup, connected) + \
        _process_wa_missed_calls(_arr(local, "whatsapp_calls"), lookup, connected)
    missed.sort(key=lambda c: _parse_ts(c["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    out["missed_calls"] = missed
    out["recent_calls"] = _process_recent_calls(_arr(local, "calls"), _arr(local, "whatsapp_calls"), lookup)
    return out




# ---------------------------------------------------------------------------
# Source renderers (port of gemini-flex.ts formatSourceForLLM-style helpers)
# ---------------------------------------------------------------------------

def _norm_escalation_tone(s: str) -> str:
    # The backend strips alarm/escalation language; we pass through the text faithfully here.
    return _s(s)




def _action_age(created_at: str) -> str:
    created = _parse_ts(created_at)
    if not created:
        return "unknown"
    now = datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    hours = int((now - created).total_seconds() // 3600)
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"




# Port of thread-processing.ts: system/status-message patterns (low signal, stripped before the
# LLM sees a thread) and the ask/commitment heuristic that keeps a thread "needs response" even
# after a short user ack.
_SYSTEM_MESSAGE_PATTERNS = [
    re.compile(r"^.{0,30} added .{0,50} to the group\.?$", re.I),
    re.compile(r"^.{0,30} removed .{0,50} from the group\.?$", re.I),
    re.compile(r"^.{0,30} left( the group)?\.?$", re.I),
    re.compile(r"^.{0,30} changed the (group |subject|icon|photo|name)", re.I),
    re.compile(r"^.{0,30} created( this)? group", re.I),
    re.compile(r"^Messages and calls are end-to-end encrypted", re.I),
    re.compile(r"^.{0,30} changed their phone number", re.I),
    re.compile(r"^Missed (voice |video )?call", re.I),
    re.compile(r"^This chat is with a business account", re.I),
]


_ASK_OR_COMMITMENT_PATTERN = re.compile(
    r"\?|can you|could you|would you|please|pls|need (?:you|your)|let me know|send me|share|"
    r"review|confirm|rsvp|follow up|circle back|are you able|do you mind|when can|what time|"
    r"wdyt|thoughts", re.I)


_SHORT_ACK_MAX_CHARS = 20




def _is_system_message(text: str) -> bool:
    """Port of thread-processing.ts isSystemMessage."""
    t = (text or "").strip()
    return bool(t) and any(p.search(t) for p in _SYSTEM_MESSAGE_PATTERNS)




def _compute_last_unreplied_ask(messages_chronological) -> bool:
    """Port of thread-processing.ts computeLastUnrepliedAskFromOther: True if the other side asked
    something and the user hasn't actually answered (a short ack ≤20 chars doesn't clear it)."""
    pending = False
    for m in messages_chronological:
        if m.get("is_from_me"):
            if len(_s(m.get("text")).strip()) > _SHORT_ACK_MAX_CHARS:
                pending = False
            continue
        if _ASK_OR_COMMITMENT_PATTERN.search(_s(m.get("text"))):
            pending = True
    return pending




def _group_messages_into_threads(messages, channel, lookup=None):
    """Port of groupIntoThreads: one thread per resolved_name/handle, ordered by time. Carries
    is_group_chat (True if ANY message is a group), confidence/canonical_id (for the known-person
    rescue), and last_unreplied_ask (computed over non-system messages). `lookup` is the contact
    name index (build_contact_lookup over local.contacts) — REQUIRED for iMessage, whose handles are
    raw phone numbers the Bridge does not pre-resolve; without it names fall back to bare digits and
    the LLM mixes them up."""
    lookup = lookup or {}
    threads = {}
    for m in messages:
        if channel == "imessage":
            ident = _s(m.get("handle"))
            name = _s(m.get("resolved_name")) or resolve_imessage_name(ident, lookup)
        else:
            ident = _s(m.get("contact_jid"))
            name = (_s(m.get("resolved_name")) or _s(m.get("partner_name"))
                    or resolve_whatsapp_name(ident, "", lookup))
        key = (name or ident).lower()
        t = threads.setdefault(key, {"name": name or ident, "identifier": ident,
                                     "is_group_chat": False, "messages": [],
                                     "canonical_id": None, "confidence": None})
        if m.get("is_group_chat"):
            t["is_group_chat"] = True            # group if ANY message is a group (thread-processing.ts:124)
        if m.get("canonical_id") and not t["canonical_id"]:
            t["canonical_id"] = _s(m.get("canonical_id"))
        if m.get("confidence") and not t["confidence"]:
            t["confidence"] = _s(m.get("confidence"))
        t["messages"].append(m)
    result = []
    for t in threads.values():
        t["messages"].sort(key=lambda x: _s(x.get("timestamp")))
        non_system = [m for m in t["messages"] if not _is_system_message(_s(m.get("text")))]
        t["last_unreplied_ask"] = _compute_last_unreplied_ask(non_system)
        result.append(t)
    return result




def _thread_last_is_user(thread) -> bool:
    msgs = thread.get("messages") or []
    return bool(msgs and msgs[-1].get("is_from_me"))




def _thread_needs_response(thread) -> bool:
    """Port of filterNeedsResponseCandidates: needs a reply if the other side sent last, OR the
    user's last word was a short ack that didn't actually answer their ask."""
    return (not _thread_last_is_user(thread)) or bool(thread.get("last_unreplied_ask"))




def _thread_is_known_person(thread, known_emails: set, known_names: list,
                            known_canonical_ids: set = frozenset()) -> bool:
    """Port of thread-processing.ts `is_known_contact` + the isKnownPerson rescue
    (contacts.ts:268-295). Keep a thread when: it's a group chat; Contacts resolved a real name;
    the resolver was high-confidence; its canonical_id is in a known set (calendar attendees with
    meeting_count>0, attention_queue, relationship_insights, action_ledger); or its identifier/name
    matches a known calendar attendee / graph person. `is_from_me` does NOT make a thread known."""
    if thread.get("is_group_chat"):
        return True
    name = _s(thread.get("name"))
    if not _looks_like_phone_number(name):
        return True  # Contacts resolved a real name → known
    if _s(thread.get("confidence")) == "high":           # contacts.ts:286
        return True
    cid = _s(thread.get("canonical_id"))
    if cid and cid in known_canonical_ids:               # contacts.ts:287-293
        return True
    # Rescue an unresolved (phone-named) thread only if we otherwise know this person.
    ident = _s(thread.get("identifier")).lower().strip()
    if ident and ident in known_emails:
        return True
    if name and any(_names_match(name, kn) for kn in known_names):
        return True
    return False




def _format_thread_as_text(thread, channel) -> str:
    group_tag = " [GROUP - no deep link]" if thread.get("is_group_chat") else ""
    identifier = "" if thread.get("is_group_chat") else _s(thread.get("identifier"))
    lines = [f"### {thread.get('name')}{group_tag}"]
    if identifier:
        lines.append(f"identifier: {identifier} | channel: {channel}")
    # Strip system/status noise ("X added Y to group", "Missed voice call") before the LLM.
    real = [m for m in (thread.get("messages") or []) if not _is_system_message(_s(m.get("text")))]
    for m in real[-10:]:
        direction = "[USER SENT]" if m.get("is_from_me") else "[THEY SENT]"
        lines.append(f"  {direction} {_s(m.get('text'))}")
    return "\n".join(lines)




def _format_threads_as_text(threads, channel, status=None) -> str:
    if not threads:
        if status == "unavailable":
            return "(source unavailable on this device)"
        if status == "disabled":
            return "(disabled by user)"
        return "(none)"
    return "\n\n".join(_format_thread_as_text(t, channel) for t in threads)




def _is_sent_email(e) -> bool:
    labels = e.get("labelIds") or e.get("labels") or []
    return "SENT" in labels or bool(e.get("isSent"))




def _trim_email(e) -> dict:
    headers = e.get("headers") or {}
    from_ = headers.get("from") or e.get("from") or ""
    subject = headers.get("subject") or e.get("subject") or ""
    labels = e.get("labelIds") or e.get("labels") or []
    body = (e.get("body") or e.get("snippet") or "")[:EMAIL_BODY_MAX]
    m = re.search(r"<([^>]+)>", from_)
    sender_email = m.group(1) if m else (from_.strip() if "@" in from_ else "")
    return {
        "threadId": e.get("threadId"),
        "from": from_,
        "to": headers.get("to") or e.get("to") or "",
        "subject": subject,
        "date": headers.get("date") or e.get("date") or "",
        "body": body,
        "senderEmail": sender_email,
        "isSent": "SENT" in labels or bool(e.get("isSent")),
        "isArchived": "INBOX" not in labels if labels else bool(e.get("isArchived")),
        "isPrimary": (not any(l.startswith("CATEGORY_") for l in labels)) if labels else True,
        "isImportant": "IMPORTANT" in labels,
        "isStarred": "STARRED" in labels,
        "isPromotional": "CATEGORY_PROMOTIONS" in labels,
        "isUpdate": "CATEGORY_UPDATES" in labels,
        "isSocial": "CATEGORY_SOCIAL" in labels,
    }




def _format_emails(emails) -> str:
    if not emails:
        return "(no emails)"
    sent, active, archived = [], [], []
    for e in emails:
        if e["isSent"]:
            sent.append(e)
        elif e["isArchived"]:
            archived.append(e)
        else:
            active.append(e)

    def fmt(e):
        flags = []
        if e["isSent"]:
            flags.append("SENT_BY_USER")
        if e["isPrimary"]:
            flags.append("PRIMARY")
        if e["isImportant"]:
            flags.append("IMPORTANT")
        if e["isStarred"]:
            flags.append("STARRED")
        if e["isPromotional"]:
            flags.append("promo")
        if e["isUpdate"]:
            flags.append("update")
        if e["isSocial"]:
            flags.append("social")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        addr = (f"To: {e['to']}\nFrom: {e['from']}" if e["isSent"]
                else f"From: {e['from']}\nSenderEmail: {e['senderEmail']}")
        body = (e.get("body") or "").strip()
        return (f"### {e['subject'] or '(no subject)'}{flag_str}\n{addr}\n"
                f"Date: {e['date']}\nThreadId: {e['threadId'] or 'none'}" + (f"\n\n{body}" if body else ""))

    sections = []
    if active:
        sections.append(f"### Active Emails ({len(active)})\n" + "\n\n".join(fmt(e) for e in active))
    if sent:
        sections.append(f"### Your Sent Emails ({len(sent)}) — these are YOUR replies, loop is CLOSED for these threads\n"
                        + "\n\n".join(fmt(e) for e in sent))
    if archived:
        lines = "\n".join(f"- {e['subject'] or '(no subject)'} (from: {e['from']})"
                          + (f" [threadId: {e['threadId']}]" if e["threadId"] else "") for e in archived)
        sections.append(f"### Archived/Handled Emails ({len(archived)})\n{lines}")
    return "\n\n".join(sections)




def _format_calendar(events) -> str:
    if not events:
        return "(no upcoming events)"
    by_day = {}
    for e in events:
        day = _s(e.get("start")).split("T")[0] or "Unknown"
        by_day.setdefault(day, []).append(e)
    sections = []
    for day, evs in by_day.items():
        lines = []
        for e in evs:
            title = e.get("summary") or "(no title)"
            atts = e.get("attendees") or []
            line = f"- {_s(e.get('start'))}: **{title}**"
            if atts:
                line += f" ({len(atts)} attendees)"
            externals = []
            for a in atts:
                email = _s(a.get("email")).lower()
                name = a.get("displayName") or (email.split("@")[0] if email else "")
                externals.append(f"    - {name} <{email}>")
            if externals:
                line += "\n" + "\n".join(externals)
            line += f"\n    event_id: {e.get('id')}"
            if e.get("start"):
                line += f" | start: {e.get('start')}"
            if e.get("meetingLink"):
                line += f"\n    meetingLink: {e.get('meetingLink')}"
            if e.get("description"):
                desc = _s(e.get("description"))
                line += f"\n    description: {desc[:1000]}{'...' if len(desc) > 1000 else ''}"
            lines.append(line)
        sections.append(f"### {day}\n" + "\n".join(lines))
    return "\n\n".join(sections)




# ---------------------------------------------------------------------------
# External-attendee selection (port of gemini-flex.ts processCalendarEvents
# `_needs_research` filter — who in today's meetings needs researching).
# ---------------------------------------------------------------------------

MAX_ATTENDEES_TO_RESEARCH = 25  # mirrors gemini-research.ts


RESEARCH_HORIZON_HOURS = 72     # mirrors processCalendarEvents (full attendee detail within 72h)




# Port of claude-flex.ts isHighQualityResearch: a graph/cached profile is "good enough to skip
# re-research" only if it actually says something. Thin/placeholder profiles get re-researched.
_LOW_QUALITY_PROFILE_PHRASES = ("no public profile", "no additional info", "no additional background",
                                "email signature", "team member", "works at")




def _is_high_quality_profile(packed: str) -> bool:
    """True if a packed person-knowledge string carries real substance (a facts line ≥ ~50 chars,
    no low-quality placeholder phrases). Mirrors isHighQualityResearch over the knowledge graph,
    which is the cloud's attendee cache."""
    text = _s(packed)
    low = text.lower()
    if any(p in low for p in _LOW_QUALITY_PROFILE_PHRASES):
        return False
    facts = ""
    for line in text.split("\n"):
        if line.startswith("="):           # "= fact; fact; …" (knowledge_query pack format)
            facts = line[1:].strip()
            break
    return len(facts) >= 50




def _known_identities(local: dict, research_quality: bool = False) -> tuple[set, list]:
    """Known emails + display names (Apple Contacts + the person knowledge graph). With
    research_quality=True, a graph person counts as known ONLY if their profile is high-quality —
    so thin/stale graph entries are re-researched instead of being skipped forever (the Mac's
    re-research-on-low-quality behavior, since the cloud graph is the attendee cache)."""
    emails, names = set(), []
    for c in _arr(local, "contacts"):
        nm = _s(c.get("name")).strip()
        if nm:
            names.append(nm)
        for e in _arr(c, "emails"):
            if e:
                emails.add(e.lower().strip())
    # person_knowledge values are packed strings "Name (id) | role @ company | email"
    for packed in _obj(local, "person_knowledge").values():
        if research_quality and not _is_high_quality_profile(packed):
            continue  # thin profile → re-research this attendee
        head = _s(packed).split("\n", 1)[0]
        nm = head.split("(")[0].strip()
        if nm:
            names.append(nm)
        m = re.search(r"\|\s*([^\s|]+@[^\s|]+)", head)
        if m:
            emails.add(m.group(1).lower().strip())
    return emails, names




def _format_attendee_research(inputs) -> str:
    """Render the host-researched attendee briefs (port of the meeting-research lines the Mac
    backend weaves into meeting_prep actions). Each entry is {email, title, company, relevance[],
    summary}. The agent populates this via the host's native web search (see research-prompt.md);
    here we only format it into the meeting-prep context the FLEX prompt reads."""
    research = _arr(inputs, "attendee_research")
    if not research:
        return ""
    lines = []
    for r in research:
        email = _s(r.get("email")).lower()
        title = _s(r.get("title"))
        company = _s(r.get("company"))
        header = email
        if title and company:
            header = f"{email} — {title} at {company}"
        elif company:
            header = f"{email} — {company}"
        summary = _s(r.get("summary")).strip()
        block = [f"- {header}"]
        if summary and summary.lower() != "no public profile found.":
            block.append(f"  {summary}")
        for bullet in _arr(r, "relevance"):
            b = _s(bullet).strip()
            if b:
                block.append(f"  · {b}")
        lines.append("\n".join(block))
    return ("## Attendee Research (PRE-COMPUTED — external people in today's meetings)\n"
            "Background on the external attendees of today's meetings, researched from public sources.\n"
            "Use ONLY to enrich `meeting_prep` action items — populate externalContext (who they are, what their\n"
            "company does) and background (prior roles, funding stage), and inform the action's prose. Do NOT\n"
            "narrate this in the brief and do NOT invent facts beyond what is stated here.\n\n"
            + "\n".join(lines) + "\n")




def _format_reminders(reminders, status=None) -> str:
    if not reminders:
        return ("(source unavailable on this device)" if status == "unavailable"
                else "(disabled by user)" if status == "disabled" else "(no reminders)")
    return "\n".join(f"- {_s(r.get('title'))} (due: {r.get('due_date') or 'no date'})" for r in reminders)




def _format_birthdays(local) -> str:
    """Contacts (Apple Contacts ZBIRTHDAY → 'MM-DD') whose birthday falls in the next 7 days, soonest
    first. Year-agnostic; the 7-day window is tolerant of timezone off-by-one. Empty if none."""
    import datetime as _dt
    today = _dt.date.today()
    items = []
    for c in _arr(local, "contacts"):
        mmdd = _s(c.get("birthday"))
        name = _s(c.get("name"))
        if not (mmdd and name):
            continue
        try:
            mo, da = int(mmdd[:2]), int(mmdd[3:5])
            nxt = _dt.date(today.year, mo, da)
        except ValueError:
            continue  # malformed or 02-29 in a non-leap year
        if nxt < today:
            try:
                nxt = _dt.date(today.year + 1, mo, da)
            except ValueError:
                continue
        days = (nxt - today).days
        if days <= 7:
            when = "TODAY 🎂" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
            items.append((days, f"- **{name}** — birthday {when}"))
    items.sort()
    return "\n".join(line for _, line in items)




def _format_missed_calls(calls, status=None) -> str:
    if not calls:
        return ("(source unavailable on this device)" if status == "unavailable"
                else "(disabled by user)" if status == "disabled" else "(no missed calls)")
    return "\n".join(f"- **{c.get('name')}** ({c.get('call_type') or 'phone'}) - {c.get('timestamp')}\n  identifier: {c.get('phone')}"
                     for c in calls)




def _format_recent_calls(calls) -> str:
    if not calls:
        return ""
    out = []
    for c in calls:
        secs = c.get("duration_seconds")
        if secs:
            if secs >= 3600:
                dur = f"{secs // 3600}h {round((secs % 3600) / 60)}min"
            elif secs >= 60:
                dur = f"{round(secs / 60)}min"
            else:
                dur = f"{round(secs)}sec"
            dur = f" [{dur}]"
        else:
            dur = ""
        out.append(f"- **{c.get('name')}** ({c.get('call_type')}, {c.get('direction')}){dur} - {c.get('timestamp')}")
    body = "\n".join(out)
    return ("\n## Recent Calls (successful connections)\n"
            "These are calls the user made or received recently. Use to add context: "
            "\"You spoke with Sarah for 45 minutes yesterday — her email today continues that conversation.\"\n"
            f"{body}\n")




def _stale_local_note(local) -> str:
    """When the brief is running on a cached read_local (Bridge was unreachable), tell the model the
    local data is from an earlier capture so it frames it honestly ('your Mac was last seen …')."""
    since = _s((local or {}).get("_local_stale_since"))
    if not since:
        return ""
    return (f"## Local Context Is From An Earlier Snapshot\n"
            f"The Bridge was unreachable, so iMessage/WhatsApp/calls/notes below are from the last "
            f"capture ({since}), NOT live. Lead with anything time-sensitive from Gmail/Calendar, and "
            f"when you reference a message/call, note it may be stale (\"as of your Mac's last sync\"). "
            f"Do not claim these are from this morning.\n\n")




def _format_source_availability(avail) -> str:
    if not avail:
        return ""
    labels = {"imessage": "iMessage", "whatsapp": "WhatsApp", "calls": "Phone Calls",
              "whatsapp_calls": "WhatsApp Calls", "reminders": "Apple Reminders",
              "chrome": "Chrome History", "granola": "Meeting Notes (Granola)"}
    unavailable, disabled = [], []
    for sid, status in avail.items():
        label = labels.get(sid, sid)
        if status == "disabled":
            disabled.append(label)
        elif status != "available":
            unavailable.append(label)
    if not unavailable and not disabled:
        return ""
    lines = ["## Data Source Availability"]
    if unavailable:
        lines.append(f"Unavailable on this device: {', '.join(unavailable)}")
    if disabled:
        lines.append(f"Disabled by user: {', '.join(disabled)}")
    lines.append("When a source is unavailable or disabled, do not create action items that depend on it.")
    return "\n".join(lines)




def _format_deferred_unread(local) -> str:
    im = _arr(local, "deferred_unread_imessage")[:DEFERRED_UNREAD_PROMPT_CAP]
    wa = _arr(local, "deferred_unread_whatsapp")[:DEFERRED_UNREAD_PROMPT_CAP]
    tracked = {(_s(a.get("source_thread_id")).strip()) for a in _arr(local, "action_ledger") if _s(a.get("source_thread_id")).strip()}
    # Drop no-reply/notification senders (contacts.ts isLikelyAutomated) and threads already tracked.
    em = [e for e in _arr(local, "deferred_unread_emails")
          if _s(e.get("threadId")) not in tracked
          and not _is_likely_automated(_s(e.get("senderEmail")) or _s(e.get("from")))][:DEFERRED_UNREAD_PROMPT_CAP]
    if not im and not wa and not em:
        return ""
    sections = []
    if im:
        sections.append("**iMessage (unread, last 30d)**\n" + "\n".join(
            f"- {m.get('resolved_name') or m.get('handle')} ({m.get('days_old')}d old, {_date_only(m.get('timestamp'))}): \"{_s(m.get('text'))[:160]}\""
            for m in im))
    if wa:
        sections.append("**WhatsApp (chats with unread)**\n" + "\n".join(
            f"- {m.get('resolved_name') or m.get('partner_name') or m.get('contact_jid')} ({m.get('unread_count')} unread, {m.get('days_old')}d old): \"{_s(m.get('text'))[:160]}\""
            for m in wa))
    if em:
        sections.append("**Gmail (unread from known senders)**\n" + "\n".join(
            f"- {e.get('from')} — \"{e.get('subject')}\" ({e.get('daysOld')}d old)\n  {e.get('snippet')}\n  Thread: {e.get('threadId')}"
            for e in em))
    return ("### Deferred Items (user saw these but hasn't acted)\n"
            "These are inbound messages still marked unread that fall outside today's normal extraction window. "
            "The user has likely seen them and chose to defer — that itself is a follow-up signal.\n\n"
            + "\n\n".join(sections) + "\n\n"
            "Rules:\n"
            "- Treat these as candidate actions if the content warrants follow-up (a question, a request, a substantive update).\n"
            "- Skip if it's clearly low-signal (a \"thanks\", an FYI, a confirmation).\n"
            "- Reference age subtly when relevant (\"from a few days back\") — don't shame.\n"
            "- For Gmail items, the threadId is provided so the action can link to it.\n")




def _format_stale_threads(local) -> str:
    stale = _arr(local, "stale_threads")
    if not stale:
        return ""
    tracked = {(_s(a.get("source_thread_id")).strip()) for a in _arr(local, "action_ledger") if _s(a.get("source_thread_id")).strip()}
    visible = [t for t in stale if _s(t.get("threadId")) not in tracked]
    if not visible:
        return ""
    body = "\n".join(
        f"- **{t.get('to')}**: \"{t.get('subject')}\" — sent {t.get('daysSinceSent')} days ago ({_s(t.get('sentDate')).split('T')[0]})\n"
        f"  Thread ID: {t.get('threadId')}\n  {t.get('snippet')}"
        for t in visible)
    return ("### Stale Outbound Threads (PRE-COMPUTED from Gmail — trust these signals)\n"
            "These are emails YOU sent that received no reply and are not already tracked in continuity.\n"
            "Use as source for new \"follow_up_stale\" actions only.\n"
            "Do NOT re-scan raw emails to find stale threads — this filtered list is authoritative.\n\n"
            f"{body}\n\n"
            "For each stale thread:\n"
            "- Emit a \"follow_up_stale\" action with evidence pointing to the threadId\n"
            "- Set contextUrgencyReason to days since sent\n"
            "- Set confidence 0.7-0.9 (higher for older threads with substantive content)\n"
            "- Skip trivial threads (\"thanks\", \"sounds good\") — they don't need follow-up\n")




def _format_past_commitments(local) -> str:
    commitments = _arr(local, "past_commitments")
    if not commitments:
        return ""
    body = "\n".join(
        f"- **{c.get('contactName')}** [{c.get('type')}{('/' + c.get('channel')) if c.get('channel') else ''}]: "
        f"{c.get('summary')}{(' (status: ' + c.get('status') + ')') if c.get('status') else ''}"
        f"{(' — since ' + _s(c.get('createdAt')).split('T')[0]) if c.get('createdAt') else ''}"
        f"{(' (surfaced ' + str(c.get('timesSurfaced')) + 'x)') if c.get('timesSurfaced') else ''}"
        for c in commitments)
    return ("### Commitment History for Key People (historical context only)\n"
            "Do NOT duplicate any item already present in TRACKED OPEN LOOPS. Use this only as background context.\n\n"
            f"{body}\n")




def _format_action_ledger(local) -> str:
    actions = _arr(local, "action_ledger")
    if not actions:
        return ""
    active = [a for a in actions if a.get("status") in ("open", "waiting")]
    if not active:
        return ""
    lines = []
    for a in active:
        age = _action_age(_s(a.get("created_at")))
        summary = _norm_escalation_tone(a.get("summary"))
        ask = _norm_escalation_tone(a.get("ask")) if a.get("ask") else ""
        lines.append(f"- {a.get('action_type')} ({age}): {a.get('contact_name')} via {a.get('channel')}: {summary}"
                     + (f" — {ask}" if ask else ""))
    body = "\n".join(lines)
    return ("## Open Commitments (ACTION LEDGER from previous briefs)\n"
            "These are unresolved action items from previous briefs still awaiting completion.\n"
            "- If today's data shows the commitment was fulfilled, mention it in Already Handled\n"
            "- If still open, may warrant a follow-up action item or brief mention\n"
            "- Do NOT duplicate — if creating a new action for the same person/topic, reference continuity\n"
            "- Age indicates urgency: items open 2d+ need attention\n\n"
            f"{body}\n")




def _format_attention_queue(local) -> str:
    queue = _arr(local, "attention_queue")[:20]
    if not queue:
        return ""
    body = "\n".join(
        f"- {q.get('display_name')} ({q.get('queue_type')}): {q.get('reason')}"
        f"{(' — ' + str(q.get('days_waiting')) + 'd') if (q.get('days_waiting') or 0) > 0 else ''}"
        f"{(' via ' + ', '.join(q.get('channels_waiting'))) if q.get('channels_waiting') else ''}"
        for q in queue)
    return ("## Attention Queue (people who need your attention)\n"
            "These people either are WAITING for your reply, or are relationships LOSING TOUCH.\n\n"
            f"{body}\n")




def _format_relationship_insights(local) -> str:
    insights = _arr(local, "relationship_insights")[:15]
    if not insights:
        return ""
    body = "\n".join(f"- {i.get('display_name')} ({i.get('insight_type')}): {i.get('description')}" for i in insights)
    return ("## Relationship Pattern Changes (detected over 6-week trends)\n"
            "These are structural changes in communication patterns, NOT individual messages.\n\n"
            f"{body}\n")




def _format_knowledge_section(local) -> str:
    pk = _obj(local, "person_knowledge")
    ck = _obj(local, "company_knowledge")
    journal = _s(local.get("journal_context"))
    parts = []
    if pk:
        parts.append("### People You Know")
        parts.append("Format: Name (id) | role @ company | email. = facts. > talking points. ~ activity. # notes.")
        parts.extend(pk.values())
    if ck:
        parts.append("### Company Context")
        parts.extend(ck.values())
    if journal:
        parts.append("### Today's Context")
        parts.append(journal)
    inner = "\n\n".join(parts)
    if not inner:
        return ""
    return ("## What You Know About Today's People (from knowledge files)\n"
            "Pre-packed knowledge about people and companies relevant to today's brief.\n\n"
            f"{inner}\n")




def _format_contact_notes(local) -> str:
    notes = [c for c in _arr(local, "contacts") if _s(c.get("notes")).strip()]
    if not notes:
        return ""
    body = "\n".join(f"- **{c.get('name')}**: {_s(c.get('notes')).strip()}" for c in notes)
    return ("### Contact Notes (from Apple Contacts)\n"
            "Personal notes the user saved about contacts. Use to enrich context — roles, how they met, preferences.\n"
            f"{body}\n")




def _format_apple_notes(local) -> str:
    notes = _arr(local, "apple_notes")[:15]
    if not notes:
        return ""
    body = "\n".join(
        f"- **{n.get('title')}** ({n.get('folder')}, modified {n.get('modified_date')})\n"
        f"  {_s(n.get('snippet'))[:200]}{'...' if len(_s(n.get('snippet'))) > 200 else ''}"
        for n in notes)
    return ("### Recent Apple Notes (modified in last 48h)\n"
            f"{body}\n"
            "Apple Notes capture the user's unstructured thinking — quick jots, draft plans, ideas, phone captures.\n"
            "Cross-reference with people and meetings: if a note mentions someone in today's brief, reference it: "
            "\"You jotted a note about X — their meeting is at 10.\"\n"
            "If a note relates to an upcoming meeting or project, mention it as context.\n")




def _format_granola_meetings(local) -> str:
    meetings = [m for m in _arr(local, "granola_meetings") if m.get("ai_summary") or m.get("your_notes")][:10]
    if not meetings:
        return "(none)"
    out = []
    for m in meetings:
        summary = _s(m.get("ai_summary") or m.get("your_notes"))[:2000]
        attendees = ", ".join((m.get("attendee_emails") or [])[:5])
        out.append(f"**{m.get('title')}** ({m.get('date')})\nAttendees: {attendees}\n{summary}")
    return "\n\n".join(out)




def _format_top_browsing_domains(local) -> str:
    merged = {}
    for entry in _arr(local, "chrome_history") + _arr(local, "safari_history"):
        d = _s(entry.get("domain"))
        ex = merged.get(d)
        if ex:
            ex["visit_count"] += entry.get("visit_count") or 0
            for t in (entry.get("top_titles") or []):
                if len(ex["top_titles"]) < 5 and t not in ex["top_titles"]:
                    ex["top_titles"].append(t)
        else:
            merged[d] = {"domain": d, "visit_count": entry.get("visit_count") or 0,
                         "top_titles": list(entry.get("top_titles") or [])}
    sorted_d = sorted([e for e in merged.values() if e["visit_count"] >= 2],
                      key=lambda e: e["visit_count"], reverse=True)[:25]
    if not sorted_d:
        return "(none)"
    return "\n".join(
        f"- {e['domain']} ({e['visit_count']} visits)" + (": " + ", ".join(e["top_titles"][:3]) if e["top_titles"] else "")
        for e in sorted_d)




def _format_search_queries(local) -> str:
    queries = _arr(local, "search_queries") + _arr(local, "safari_search_queries")
    seen, unique = set(), []
    for q in queries:
        lq = _s(q).lower()
        if lq and lq not in seen:
            seen.add(lq)
            unique.append(lq)
        if len(unique) >= 20:
            break
    if not unique:
        return ""
    return ("### Recent Search Queries (from browser)\n" + "\n".join(unique) + "\n"
            "These indicate active research. If ANY search query matches a person's name, company, or topic in the brief, "
            "MENTION IT in that person's entry: \"You searched for X recently — they're the one asking about Y.\"\n")




def _format_screen_time(local) -> str:
    apps = (_obj(local, "screen_time").get("top_apps") or [])[:10]
    if not apps:
        return "(none)"
    return "\n".join(f"- {a.get('app_name')}: {a.get('minutes')}min" for a in apps)




def _format_recent_files(local) -> str:
    files = _arr(local, "recent_files")[:15]
    if not files:
        return "(none)"
    out = []
    for f in files:
        status = "✓ opened" if f.get("status") == "opened" else "✗ unopened"
        source = ""
        url = _s(f.get("source_url"))
        if url:
            m = re.match(r"https?://([^/]+)(/[^?#]*)?", url)
            if m:
                host = m.group(1)
                path = (m.group(2) or "")[:60]
                path = path if path and path != "/" else ""
                source = f" (from: {host}{path})"
        out.append(f"- {f.get('filename')} [{status}]{source}")
    return "\n".join(out)




def _format_meeting_archive(local) -> str:
    ctxs = _arr(local, "meeting_archive_context")
    if not ctxs:
        return ""
    body = "\n\n".join(
        f"**{c.get('label')}**\n" + "\n".join(
            f"- {t.get('date')}: \"{t.get('subject')}\" from {t.get('from')}\n  {t.get('snippet')}"
            for t in (c.get("threads") or []))
        for c in ctxs)
    return ("### Prior Email History with Meeting Attendees (from Gmail archive — up to 90 days)\n"
            f"{body}\n"
            "Use for meeting prep: \"Last month you discussed X with Sarah — today's meeting continues that thread.\"\n"
            "Reference prior conversations naturally in meeting prep context and action items.\n")




def _format_reconciliation(local, brief_type) -> str:
    if brief_type != "evening":
        return ""
    actions = _arr(local, "action_ledger")
    if not actions:
        return ""
    body = "\n".join(
        f"- {'✅ ' if a.get('status') == 'resolved' else ''}[{a.get('action_type')}] {a.get('contact_name')} via {a.get('channel')}: "
        f"{_norm_escalation_tone(a.get('summary'))}" + (f" — {_norm_escalation_tone(a.get('ask'))}" if a.get("ask") else "")
        for a in actions if a.get("source_brief_at"))
    if not body:
        return ""
    return ("## Evening Accountability (morning commitments — did you follow through?)\n"
            "These action items were generated in this morning's brief. Check today's messages, emails, and calls for evidence of completion.\n\n"
            f"{body}\n\n"
            "For each item:\n"
            "- If today's data shows it was completed → include in ✅ Already Handled with a note like \"Completed today\"\n"
            "- If still open → mention in a \"Still Pending\" subsection with increased urgency\n"
            "- Frame as accountability: \"This morning's brief flagged X. Here's what happened.\"\n"
            "- These overlap with the Action Ledger above — use this section for the evening accountability framing\n")




def _format_signal_scores(scores) -> str:
    if not scores:
        return "(none)"
    return "\n".join(f"- **{s.get('event')}**: score {s.get('score')} ({', '.join(s.get('signals') or [])})" for s in scores)




def _format_granola_context(ctx) -> str:
    if not ctx:
        return "(none)"
    return "\n".join(f"- **{g.get('meeting_title')}** ({g.get('last_meeting')}) with {g.get('person')}\n  {_s(g.get('summary'))[:2000]}" for g in ctx)




def _format_file_matches(matches) -> str:
    if not matches:
        return "(none)"
    out = []
    for f in matches[:12]:
        conf = "🔗 download-source match" if f.get("confidence") == "high" else "🔍 keyword match (speculative)"
        status = "✓ reviewed" if f.get("status") == "opened" else "✗ unread"
        out.append(f"- **{f.get('filename')}** → {f.get('event')} [{status}] ({conf}: {', '.join(f.get('keywords') or [])})")
    return "\n".join(out)




def _format_domain_research(boosts, local) -> str:
    if not boosts:
        return "(none)"
    out = []
    chrome = _arr(local, "chrome_history")
    for s in boosts:
        entry = next((h for h in chrome if h.get("domain") == s.get("domain")), None)
        pages = f" — pages visited: {', '.join(entry.get('top_titles') or [])}" if entry and entry.get("top_titles") else ""
        out.append(f"- **{s.get('person')}** ({s.get('email')}) — researched {s.get('domain')}{pages}")
    return "\n".join(out)




def _format_escalation_signals(signals) -> str:
    if not signals:
        return ""
    body = "\n".join(f"- {s.get('name')}: {s.get('narrative')} — {s.get('escalation_level')} channels" for s in signals)
    return ("## Cross-Channel Escalation (PRE-COMPUTED — trust these signals)\n"
            "These contacts reached out via multiple channels within 48 hours.\n"
            "Use this as a priority signal, while keeping writing calm and matter-of-fact.\n\n"
            f"{body}\n\n"
            "Rules:\n"
            "- Place escalating contacts in top priority\n"
            "- Reference the channel timeline naturally\n"
            "- Keep escalation wording subtle (no warning icons or all-caps labels)\n")
