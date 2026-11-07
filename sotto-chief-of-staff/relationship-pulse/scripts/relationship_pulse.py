#!/usr/bin/env python3
"""
relationship_pulse.py — weekly relationship intelligence: who you're losing touch with and who's
waiting on you, computed from a WIDE read_local window (the Bridge reads ~6 weeks of messages/calls
on demand), PLUS longitudinal memory: each run snapshots per-contact {last_contact, interactions,
trend} into relationship_state.json's `history` block, so a previously-regular contact who has gone
silent LONGER than the window surfaces as `lapsed` ("you've fully lost touch") instead of vanishing.

PORT SOURCE: app/src-tauri/src/database/relationship_analytics.rs
  - calculate_cadence (intervals; "increasing" trend = recent intervals > 1.5x older = drifting)
  - compute_attention_queue ("waiting_on_you": they sent last, 3-14d; "losing_touch": cadence
    increasing + historically strong + 14d+ silent)
The Mac computed this in people.db; here it's a deterministic pass over the wide read_local snapshot.
Reply-review suggestions additionally require a visible request cue or missed call; silence alone
does not establish an obligation. At most five contextual suggestions are returned.

Usage (execute_code): relationship_pulse.py /tmp/sotto_local_6w.json   (or stdin)
Prints JSON: { "attention_queue":[...], "relationship_insights":[...], "lapsed":[...],
               "pulse_markdown":"..." }
Also writes $SOTTO_DATA/knowledge/relationship_state.json (queue + insights + per-contact history)
so the daily brief can surface it too. No history yet (first run) → no lapsed section; degrades clean.
"""
from __future__ import annotations

import json
import os
import re
import sys
from html import unescape
from datetime import date, datetime, timezone

_SHARED = os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "scripts")
_SHARED_LIB = os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "lib")
sys.path.insert(0, _SHARED)
if _SHARED_LIB not in sys.path:
    sys.path.insert(0, _SHARED_LIB)
from email.utils import getaddresses  # noqa: E402
from textutil import _arr, _is_likely_automated, _looks_like_phone_number, _s  # noqa: E402
from timeutil import _parse_ts, configured_user_email  # noqa: E402
from render_local import (_is_sent_email, rekey_seed_to_graph, resolve_contact_names,  # noqa: E402
                          seed_contact_index_from_contacts)
from chatfmt import to_chat  # noqa: E402
from ledger_io import dated_request_text  # noqa: E402
from relationship_importance import (activity_evidence, attention_tiebreak, classify,
                                     engagement_signals, meaningful_relationship,
                                     relationship_meeting_attendees)  # noqa: E402

WAITING_MIN_DAYS = 3       # relationship_analytics.rs:469
WAITING_MAX_DAYS = 14
LOSING_TOUCH_DAYS = 14     # relationship_analytics.rs:494
MIN_INTERACTIONS_FOR_TREND = 8   # need 8 dates → 7 intervals (rs:1149-1151)
MAX_QUEUE = 5
MAX_LAPSED = 5
PULSE_ITEM_CHARS = 140
HISTORY_MAX_AGE_DAYS = 365       # drop history entries silent for over a year (not a relationship)
MESSAGE_CHANNELS = ("imessage", "whatsapp", "calls")   # the Bridge lanes this pass reads
LAPSE_DISCLAIMER = "Some history may be missing, so I’ve left out ‘fully lost touch’ suggestions."
# Shared state keeps only the non-content shape of an inbound touch. Message text and subjects live
# only in the attended reply that read them: consent can change after relationship_state.json is
# written, and the brief, dashboard and triage all read that file.
STORED_INBOUND_FIELDS = ("at", "channel", "missed_call", "bulk", "direct", "request")
STORED_CONTEXT_FIELDS = ("channel", "date", "missed_call", "request")
_REQUEST_CUE = re.compile(r"\b(?:(?:can|could|would|will) you\b|please (?:send|share|confirm|review|reply|call)\b)", re.I)


def complete_channels(source_status=None, *, email_complete=False) -> list:
    """The channels this window read in full — the only ones whose silence can prove a lapse.

    `source_status` is the Bridge's per-source read status; only `ok` is a full read. A snapshot
    without one (an older Bridge, or a hand-saved read) keeps the original assumption that its
    message lanes were read. Email is complete only when its own read said so."""
    if source_status is None:
        channels = set(MESSAGE_CHANNELS)
    else:
        from source_context import _local_statuses  # noqa: PLC0415 — the Bridge status reader
        statuses = _local_statuses(source_status)
        channels = {channel for channel in MESSAGE_CHANNELS if statuses.get(channel) == "ok"}
    if email_complete:
        channels.add("email")
    return sorted(channels)


def _absence_proven(entry: dict, absence_complete) -> bool:
    """A lapse needs every channel you used with them read in full this window. A row from before
    per-channel history counts as a message-lane relationship."""
    if isinstance(absence_complete, bool):
        return absence_complete
    covered = set(absence_complete or ())
    used = entry.get("channels") if isinstance(entry.get("channels"), dict) else {}
    return bool(covered) and (set(used) or set(MESSAGE_CHANNELS)) <= covered


def _data_root() -> str:
    return os.environ.get("SOTTO_DATA", "/data")


def _state_path() -> str:
    return os.path.join(_data_root(), "knowledge", "relationship_state.json")


def _stored_inbound(value) -> dict:
    """The persisted inbound shape; any text or subject (including from an older file) is dropped."""
    return {key: value[key] for key in STORED_INBOUND_FIELDS if key in value} if isinstance(value, dict) else {}


def _stored_history_row(row):
    if isinstance(row, dict) and "latest_inbound" in row:
        return {**row, "latest_inbound": _stored_inbound(row.get("latest_inbound"))}
    return row


def _stored_queue_row(row):
    context = row.get("context") if isinstance(row, dict) else None
    if not isinstance(context, dict):
        return row
    return {**row, "context": {key: context[key] for key in STORED_CONTEXT_FIELDS if key in context}}


def _has_request(latest: dict) -> bool:
    """A visible request cue: read from fresh text, or the boolean a prior observation stored."""
    if "text" in latest:
        return bool(_REQUEST_CUE.search(_excerpt(latest.get("text"))))
    return latest.get("request") is True


def _load_history() -> dict:
    """Per-contact snapshots from the last run: {name: {last_contact, interactions, trend}}.
    Missing/corrupt state → {} (first run degrades to the old no-memory behavior). Stored message
    text from an older writer is ignored here and scrubbed on the next write."""
    try:
        with open(_state_path(), encoding="utf-8") as f:
            h = (json.load(f) or {}).get("history")
        return {key: _stored_history_row(row) for key, row in h.items()} if isinstance(h, dict) else {}
    except Exception:
        return {}


def _ts(s: str):
    return _parse_ts(_s(s))


def _aware_ts(value):
    parsed = _ts(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed and parsed.tzinfo is None else parsed


_KG = None


def _knowledge():
    """Graph identity/context is useful; it never contributes weight or importance."""
    global _KG
    if _KG is None:
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "knowledge"))
            import knowledge as kg  # noqa: PLC0415
            _KG = kg
        except Exception:
            _KG = False
    return _KG or None


def _graph_context(name: str, cid: str = "", index=None):
    kg = _knowledge()
    if not kg:
        return None
    try:
        path = kg.find_person_file(cid=cid, index=index) if cid else None
        path = path or kg.find_person_file(name=name, index=index)
        if not path:
            return None
        with open(path, encoding="utf-8") as f:
            person = kg.parse_person_file(f.read())
        return {k: v for k, v in {"company": _s(person.company), "summary": _s(person.summary)}.items() if v}
    except Exception:
        return None


def _interactions_by_contact(local: dict, index=None) -> dict:
    """Group every inbound/outbound touch (message + call) per resolved contact. Skips group chats
    and unknown (phone-named) senders — same is_known_contact gate as the brief.

    Keyed by canonical_id when the resolver attached one, display name only as the fallback — a
    Contacts rename used to reset a person's whole longitudinal history, and two people sharing a
    name collapsed into one cadence. Each entry also tracks last-contact PER CHANNEL, so the state
    can finally say "last texted yesterday, last emailed never"."""
    people: dict = {}

    def add(name, ts, from_me, channel, cid="", conversation_id="", native_id="", engaged=True,
            context=None):
        nm = _s(name).strip()
        # "Unknown" is the resolver's sentinel — merging every unresolved sender into one fake
        # contact would fabricate cadence/waiting signals for a person who doesn't exist.
        if not nm or nm == "Unknown" or _looks_like_phone_number(nm):
            return
        d = _ts(ts)
        if d is None:
            return
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        key = _s(cid).strip() or nm
        p = people.setdefault(key, {"name": nm, "cid": _s(cid).strip(),
                                    "dates": [], "from_me": [], "from_them": [],
                                    "by_channel": {}, "events": [], "engaged_dates": [],
                                    "engaged_from_me": [], "engaged_from_them": []})
        if _s(cid).strip() and not p["cid"]:
            p["cid"] = _s(cid).strip()
        p["dates"].append(d)
        (p["from_me"] if from_me else p["from_them"]).append(d)
        if not from_me and d >= (p.get("latest_inbound") or {}).get("at", d):
            p["latest_inbound"] = {"at": d, "channel": channel, "native_id": _s(native_id)[:512], **(context or {})}
        if engaged:
            p["engaged_dates"].append(d)
            (p["engaged_from_me"] if from_me else p["engaged_from_them"]).append(d)
        if conversation_id and engaged:
            p["events"].append({"at": d, "from_me": bool(from_me), "channel": channel,
                                "conversation_id": _s(conversation_id), "native_id": _s(native_id)})
        prev = p["by_channel"].get(channel)
        if prev is None or d > prev:
            p["by_channel"][channel] = d

    for m in _arr(local, "imessage"):
        if m.get("is_group_chat"):
            continue
        add(m.get("resolved_name"), m.get("timestamp"), m.get("is_from_me"),
            "imessage", m.get("canonical_id"),
            m.get("chat_identifier") or m.get("chat_id") or m.get("handle"),
            m.get("message_id") or m.get("guid") or m.get("id"), context={"text": m.get("text")})
    for m in _arr(local, "whatsapp"):
        if m.get("is_group_chat"):
            continue
        nm = _s(m.get("resolved_name")) or _s(m.get("partner_name"))
        add(nm, m.get("timestamp"), m.get("is_from_me"), "whatsapp", m.get("canonical_id"),
            m.get("chat_id") or m.get("partner_id"), m.get("message_id") or m.get("id"),
            context={"text": m.get("text")})
    for c in _arr(local, "missed_calls"):
        add(c.get("name"), c.get("timestamp"), False, "calls", c.get("canonical_id"), engaged=False,
            context={"missed_call": True})
    for c in _arr(local, "recent_calls"):
        # Processed calls carry direction, while duration proves the call connected.
        try:
            connected = bool(c.get("is_answered")) or float(c.get("duration_seconds") or 0) > 0
        except (TypeError, ValueError):
            connected = False
        add(c.get("name"), c.get("timestamp"), c.get("direction") == "outgoing",
            "calls", c.get("canonical_id"), engaged=connected)
    # Email is a relationship channel. A mail you sent counts as a touch to each recipient; a mail
    # you received counts as a touch from its sender — for people you KNOW (contacts or graph),
    # which is the same known-contact gate the message lanes apply. Before this the pulse was
    # blind to the half of a relationship that lives in mail, and a no-Mac deploy reported every
    # Monday that relationships looked healthy (Sep 2026).
    known = _email_identities(local, index)
    me = _own_email()
    for m in _arr(local, "emails"):
        if not isinstance(m, dict):
            continue
        bulk = _bulk_email(m)
        sent = _is_sent_email(m)
        addrs = _email_addrs(m.get("to"), m.get("cc")) if sent \
            else _email_addrs(m.get("from") or m.get("sender"))[:1]
        for addr in addrs:
            if not addr or addr == me or _is_likely_automated(addr):
                continue
            who = known.get(addr)
            if not who:
                continue
            add(who["name"], m.get("date") or m.get("timestamp"), sent, "email", who["cid"],
                m.get("threadId") or m.get("thread_id"), m.get("id") or m.get("message_id"),
                engaged=not bulk,
                context={"text": m.get("snippet"), "subject": m.get("subject"), "bulk": bulk,
                         "direct": bool(me and me in _email_addrs(m.get("to")))})
    # A channel that resolves a name but not an id (processed calls) must not split a person in
    # two: fold each name-keyed entry into the cid-keyed entry carrying the same display name —
    # exactly what the old all-name keying did implicitly.
    by_name = {p["name"]: k for k, p in people.items() if p["cid"]}
    for key in [k for k, p in people.items() if not p["cid"] and by_name.get(p["name"], k) != k]:
        dst, src = people[by_name[people[key]["name"]]], people.pop(key)
        dst["dates"] += src["dates"]
        dst["from_me"] += src["from_me"]
        dst["from_them"] += src["from_them"]
        dst["events"] += src["events"]
        dst["engaged_dates"] += src["engaged_dates"]
        dst["engaged_from_me"] += src["engaged_from_me"]
        dst["engaged_from_them"] += src["engaged_from_them"]
        incoming = src.get("latest_inbound")
        if incoming and incoming["at"] >= (dst.get("latest_inbound") or {}).get("at", incoming["at"]):
            dst["latest_inbound"] = incoming
        for ch, d in src["by_channel"].items():
            if ch not in dst["by_channel"] or d > dst["by_channel"][ch]:
                dst["by_channel"][ch] = d
    return people


def _bulk_email(message: dict) -> bool:
    """Use visible bulk evidence; a known sender alone does not make a newsletter personal."""
    labels = message.get("labelIds") or message.get("labels") or []
    if isinstance(labels, list) and {str(label).upper() for label in labels} & {
            "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS", "SPAM", "TRASH"}:
        return True
    headers = (message.get("payload") or {}).get("headers", [])
    if isinstance(headers, list) and any(
            _s(header.get("name")).lower() in {"list-id", "list-unsubscribe"}
            for header in headers if isinstance(header, dict)):
        return True
    text = _s(message.get("snippet")).lower()
    return "unsubscribe" in text or "manage your subscription" in text


def _excerpt(value, limit=180) -> str:
    # Do not promote a request quoted from an older message into a new ask.
    text = re.split(r"(?im)^\s*(?:>|on .+ wrote:|[- ]*forwarded message[- ]*)", unescape(_s(value)))[0]
    text = " ".join(text.split()).replace("*", "").strip()
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def _reply_context(person: dict):
    """A conservative visible cue to review, never a declaration that a reply is owed."""
    latest = person.get("latest_inbound") or {}
    if latest.get("bulk"):
        return None
    if latest.get("missed_call"):
        return {"channel": "calls", "date": latest["at"].date().isoformat(), "missed_call": True}
    if latest.get("channel") == "email" and not latest.get("direct"):
        return None  # a CC or unknown recipient is not an ask addressed to the owner
    # Deliberately small: ambiguous questions, greetings and subject-only rows stay out.
    if not _has_request(latest):
        return None
    context = {"channel": latest["channel"], "date": latest["at"].date().isoformat(), "request": True}
    if "text" in latest:   # a fresh, in-memory observation; retained state carries no wording
        context.update(subject=_excerpt(latest.get("subject"), 90), excerpt=_excerpt(latest.get("text")))
    return context


def _own_email() -> str:
    try:
        from timeutil import configured_user_email  # noqa: PLC0415
        return configured_user_email()
    except Exception:  # noqa: BLE001
        return ""


def _email_addrs(*fields) -> list:
    """Lowercased addresses from one or more header fields. Empty fields are dropped BEFORE
    parsing: a trailing empty entry makes the strict parser reject the whole header."""
    raw = [_s(f).strip() for f in fields if _s(f).strip()]
    return [a.lower() for _n, a in getaddresses(raw) if a] if raw else []


def _email_identities(local: dict, index=None) -> dict:
    """email (lower) → {name, cid} for everyone the user knows: Apple Contacts cards first, then
    the knowledge graph's own identifier index. A stranger's mail is not a relationship."""
    out: dict = {}
    for c in _arr(local, "contacts"):
        nm = _s(c.get("name")).strip()
        if not nm:
            continue
        for e in _arr(c, "emails"):
            if e:
                out.setdefault(_s(e).lower().strip(), {"name": nm, "cid": _s(c.get("canonical_id"))})
    kg = _knowledge()
    if kg:
        try:
            idx = index if index is not None else kg.build_people_index()
            for identifier, path in (idx.get("by_identifier") or {}).items():
                if "@" not in identifier or identifier in out:
                    continue
                with open(path, encoding="utf-8") as f:
                    person = kg.parse_person_file(f.read())
                if _s(person.name):
                    out[identifier] = {"name": _s(person.name), "cid": _s(person.canonical_id)}
        except Exception:
            pass
    return out


def _held_meetings(local: dict, now: datetime, index=None) -> dict:
    """Ended Granola meetings by known attendee; calendar invitations alone are not attendance."""
    known, out = _email_identities(local, index), {}
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    # The canonical chain (env override, else the connected Google account) — the same one
    # granola_graph excludes the owner with. Reading the env var alone left the owner in every
    # 1:1 whenever it was unset, which is the normal managed case, so no meeting ever counted.
    user_email = configured_user_email()
    for meeting in _arr(local, "granola_meetings"):
        ended = _ts(meeting.get("end"))
        if ended and ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        if not meeting.get("meeting_id") or not ended or ended > now:
            continue
        for email in relationship_meeting_attendees(meeting.get("attendee_emails"), user_email):
            who = known.get(_s(email).lower().strip())
            if who:
                key = who["cid"] or who["name"]
                row = out.setdefault(key, {"name": who["name"], "cid": who["cid"], "dates": []})
                row["dates"].append(ended)
    return out


def _cadence_trend(dates: list) -> str:
    """Port of calculate_cadence trend: 'increasing' = intervals growing = drifting apart."""
    if len(dates) < MIN_INTERACTIONS_FOR_TREND:
        return "stable"
    sd = sorted(dates)
    intervals = [(sd[i] - sd[i - 1]).total_seconds() / 86400.0 for i in range(1, len(sd))]
    mid = len(intervals) // 2
    older = sum(intervals[:mid]) / mid if mid else 0
    recent = sum(intervals[mid:]) / (len(intervals) - mid) if (len(intervals) - mid) else 0
    if older < 0.5:
        return "stable"
    if recent > older * 1.5:
        return "increasing"     # less frequent → losing touch
    if recent < older * 0.67:
        return "decreasing"
    return "stable"


def _ambiguous_history_names(local, index):
    """The graph's by-name lookup chooses a latest file; inspect all exact identities instead."""
    # Only a uniquely linked Contacts card shares the graph identity; collisions stay separate.
    ambiguous = set()
    groups = [rekey_seed_to_graph(seed_contact_index_from_contacts(_arr(local, 'contacts')),
                                  _arr(local, 'contact_index'), ambiguous_names=ambiguous),
              _arr(local, 'contact_index')]
    graph, kg = [], _knowledge()
    if kg:
        for path in set((index.get('by_cid') or {}).values()):
            try:
                with open(path, encoding='utf-8') as stream:
                    person = kg.parse_person_file(stream.read())
                graph.append({'display_name': person.name, 'canonical_id': person.canonical_id})
            except Exception:
                return None  # an unreadable global identity set cannot prove a unique name
    groups.append(graph)
    names = {}
    for rows in groups:
        for row in rows:
            name = _s(row.get('display_name') or row.get('name')).strip().casefold()
            if _s(row.get('canonical_id')).strip():  # a row with no identity cannot be a second person
                names.setdefault(name, set()).add(row.get('canonical_id'))
    return ambiguous | {name for name, ids in names.items() if len(ids) > 1}


def _retained_activity(people, history, now, ambiguous):
    """Reuse dated canonical observations; never reconstruct message text from a summary."""
    for key, old in history.items():
        if not isinstance(old, dict):
            continue
        last = _aware_ts(old.get('last_contact'))
        if not last or not 0 <= (now - last).total_seconds() <= 42 * 86400:
            continue  # older absent rows still use the existing guarded lapse path
        if not key.startswith('c_') and (ambiguous is None or (old.get('name') or key).casefold() in ambiguous):
            continue
        if key not in people and not key.startswith('c_'):
            matches = [fresh_key for fresh_key, fresh in people.items()
                       if fresh['cid'] and fresh['name'] == (old.get('name') or key)]
            if len(matches) == 1:
                key = matches[0]  # identity migration must not detach a saved ask from a fresh reply
        p = people.setdefault(key, {'name': old.get('name') or key,
            'cid': key if key.startswith('c_') else '', 'dates': [], 'from_me': [],
            'from_them': [], 'by_channel': {}, 'events': [], 'engaged_dates': [],
            'engaged_from_me': [], 'engaged_from_them': []})
        p['dates'].append(last)
        evidence = old.get('importance_evidence') or {}
        for target, field in (('engaged_dates', 'active_days'),
                              ('engaged_from_me', 'sent_days'), ('engaged_from_them', 'received_days')):
            p[target].extend(at for day in evidence.get(field, []) if (at := _aware_ts(day)))
        for target, field in (('from_me', 'last_from_you'), ('from_them', 'last_from_them')):
            at = _aware_ts(old.get(field))
            if at:
                p[target].append(at)
        for channel, value in (old.get('channels') or {}).items():
            at = _aware_ts(value)
            if at and at > p['by_channel'].get(channel, datetime.min.replace(tzinfo=timezone.utc)):
                p['by_channel'][channel] = at
        incoming = _stored_inbound(old.get('latest_inbound'))  # never retained wording
        at = _aware_ts(incoming.get('at'))
        if at and at > (p.get('latest_inbound') or {}).get('at', datetime.min.replace(tzinfo=timezone.utc)):
            p['latest_inbound'] = {**incoming, 'at': at}


def _compact_commitments(rows):
    """Group display only by proved canonical people; unknown identities stay separate."""
    try:
        import importlib.util
        path = os.path.join(_SHARED, '..', '..', 'morning-brief', 'scripts', 'continuity_resolve.py')
        spec = importlib.util.spec_from_file_location('pulse_continuity_identity', path)
        resolver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resolver)
        identity = resolver.person_identity()
    except Exception:
        resolver, identity = None, {}
    grouped = {}
    known = (identity.get('index') or {}).get('by_cid') or {}
    unique = identity.get('unique_identifiers') or {}
    for row, direction in rows:
        stored = _s(row.get('canonical_id')).strip().lower()
        stored = stored if stored in known else ''
        raw = _s(row.get('identifier')).strip()
        alias = unique.get(resolver.alias_identifier(raw)) if resolver and raw else None
        # An identifier is an identity: with one, only its unique graph owner proves the person (a
        # stored ID it does not prove stays separate); without one, a graph-known stored ID does.
        cid = (alias or '') if raw else stored
        if raw and stored and alias != stored:
            cid = ''
        key = (resolver.contact_anchor(cid, '', ''), direction, bool(row.get('birthday_courtesy'))) if cid and not row.get('group_id') else ('row', id(row), direction)
        grouped.setdefault(key, []).append((row, direction, cid))
    return list(grouped.values())



def _calendar_due(value):
    raw = _s(value)
    try:
        return date.fromisoformat(raw).isoformat() if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw) else ''
    except ValueError:
        return ''


def _human_date(value, now=None):
    day = _calendar_due(value)
    if not day:
        return ''
    parsed = date.fromisoformat(day)
    current = (now or datetime.now(timezone.utc)).date()
    return f"{parsed.strftime('%b')} {parsed.day}" + (f", {parsed.year}" if parsed.year != current.year else '')


def _compact_text(value):
    text = _excerpt(value, PULSE_ITEM_CHARS)
    if text.endswith('…') and ' ' in text:
        text = text.rsplit(' ', 1)[0].rstrip('.,;:') + '…'
    return text


def _direct_address(value):
    """An email or phone a draft can be addressed to; an opaque group or chat id is not one."""
    value = _s(value).strip().lower()
    if not value or value.endswith('@g.us') or ';' in value:
        return False
    if '@' in value:
        return bool(re.fullmatch(r'[^@\s]+@[^@\s]+\.[a-z]{2,}', value))
    return bool(re.fullmatch(r'\+?[\d\s().-]+', value)) and len(re.sub(r'\D', '', value)) >= 7


def _request_key(item):
    """One open item for display counts: its precise ask AND its summary, so two tasks that
    share a short ask are still two."""
    return (_s(_commitment_request(item)).strip().casefold(), _s(item.get('what')).strip().casefold())


def _commitment_request(row):
    # Courtesy wording is normalized by the protected query; its original ask is not a debt.
    ask = _s(row.get('ask')).strip()
    return row.get('what') if row.get('birthday_courtesy') or not re.search(r'\w', ask) else ask


def with_commitments(result, loops, now=None):
    """Prioritize the canonical active ledger; this read never creates or resolves a debt."""
    rows = [(row, direction) for direction in ('you_owe', 'waiting_on_them')
            for row in loops.get(direction, []) if row.get('name') not in ('', '(unknown)')]
    rows.sort(key=lambda pair: (bool(pair[0].get('birthday_courtesy')), not pair[0].get('overdue'), -(pair[0].get('age_days') or 0)))
    if not rows:
        return result
    groups = _compact_commitments(rows)
    # A name-only chat request is safe to suggest only when the whole inventory
    # has one matching group, including items omitted from this compact display.
    # Every display name in a group counts: a short name on a later row is still that name.
    names = [name for group in groups
             for name in {_excerpt(item['name'], 100).casefold() for item, _, _ in group}]
    hidden = len(groups) - MAX_QUEUE
    proven = {cid for group in groups for _, _, cid in group if cid}
    groups = [[(item, direction) for item, direction, _ in group] for group in groups[:MAX_QUEUE]]
    rows = [group[0] for group in groups]
    lines = ['**Follow-ups**']
    help_name = ''
    for group in groups:
        row, direction = group[0]
        deadline = '' if row.get('birthday_courtesy') else _calendar_due(row.get('deadline'))
        due = f" ({'was due' if row.get('overdue') else 'due'} {_human_date(deadline, now)})" if deadline else ''
        direction_text = 'Waiting for them: ' if direction == 'waiting_on_them' else ''
        same_ask = [item for item, _ in group if _request_key(item) == _request_key(row)]
        # Relative wording stays quoted under its recording date: an ask re-captured later keeps
        # its original `created_at`, so it is never rewritten as a dated instruction.
        text = dated_request_text(_compact_text(_commitment_request(row)), same_ask, now)
        if row.get('birthday_courtesy'):
            text = f"{text} (Optional, no urgency)"
        extra = len({(_request_key(item), _calendar_due(item.get('deadline')))
                     for item, _ in group}) - 1
        if extra:
            text += f"; {extra} other open item{'s' if extra != 1 else ''}"
        text = text.rstrip('.') if due else text
        ending = '' if not due and text.removesuffix('”').endswith(('.', '?', '!', '…')) else '.'
        if (not help_name and not extra and not row.get('birthday_courtesy')
                and direction == 'you_owe' and _direct_address(row.get('identifier')) and not row.get('group_id')
                and names.count(_excerpt(row['name'], 100).casefold()) == 1):
            help_name = _excerpt(row['name'], 100)
        lines.append(f"- **{_excerpt(row['name'], 100)}** — {direction_text if not row.get('birthday_courtesy') else ''}{text}{due}{ending}")
    if hidden > 0:
        lines.append(f"{hidden} other item{'s are' if hidden != 1 else ' is'} on your list.")
    if help_name:
        lines.append(f'I can draft a reply to {help_name}.')
    # A suggestion is folded into a commitment only through a proven canonical identity, never a name.
    queue = [row for row in result.get('attention_queue', [])
             if _s(row.get('canonical_id')).strip().lower() not in proven][:MAX_QUEUE - len(rows)]
    if queue:
        lines.append(_render([row for row in queue if row['queue_type'] != 'lapsed'],
                             [row for row in queue if row['queue_type'] == 'lapsed']))
    # A held-back lapse keeps its one-line disclaimer when commitments lead the message.
    markdown = to_chat('\n'.join(lines)) + ('\n' + LAPSE_DISCLAIMER if result.get('lapse_withheld') else '')
    return {**result, 'pulse_markdown': markdown, 'commitments': [row for row, _ in rows]}


def compute(local: dict, now: datetime | None = None, history: dict | None = None, *,
            absence_complete=False) -> dict:
    """`absence_complete` is True, False, or the channels read in full (see complete_channels)."""
    now = now or datetime.now(timezone.utc)
    history = history or {}
    local = resolve_contact_names(local)
    try:
        kg = _knowledge()
        index = kg.build_people_index() if kg else {}
    except Exception:
        index = {}
    people = _interactions_by_contact(local, index)
    ambiguous = _ambiguous_history_names(local, index)
    # Retained observations may still surface a saved ask, but a pass with no fresh observation
    # (Bridge offline) is degraded: it must not re-derive or overwrite the state it was read from.
    fresh = bool(people)
    _retained_activity(people, history, now, ambiguous)
    meetings = _held_meetings(local, now, index)
    fresh = fresh or bool(meetings)   # a held meeting is a fresh observation too
    for key, held in meetings.items():
        person = people.setdefault(key, {"name": held["name"], "cid": held["cid"], "dates": [],
            "from_me": [], "from_them": [], "by_channel": {}, "events": [], "engaged_dates": [],
            "engaged_from_me": [], "engaged_from_them": []})
        person["dates"].extend(held["dates"])

    # Empty/degraded snapshot guard: no usable known-person interactions may mean unavailable
    # reads or interactions without a known identity; it does not establish relationship lapse. Emitting lapsed entries here would
    # mark a 2-days-ago contact "fully lost touch", and merging would poison the longitudinal state.
    # Return empty results with degraded=True — _persist_state skips writing, keeping prior state.
    if not people and not any(isinstance(row, dict) and _absence_proven(row, absence_complete)
                             and meaningful_relationship(row.get('importance_evidence'))
                             for row in history.values()):
        return {"attention_queue": [], "relationship_insights": [], "lapsed": [],
                "pulse_markdown": ("I could not identify any known-person interactions this window — "
                                   "skipping the pulse rather than guessing."),
                "degraded": True}

    profiles = []
    for key, p in people.items():
        if not p["dates"]:
            continue
        last = max(p["dates"])
        days_since = int((now - last).total_seconds() // 86400)
        last_them = max(p["from_them"]) if p["from_them"] else None
        last_you = max(p["from_me"]) if p["from_me"] else None
        prev = history.get(key, {}) if isinstance(history.get(key), dict) else {}
        if (not prev and p['cid'] and ambiguous is not None and p['name'].casefold() not in ambiguous
                and isinstance(history.get(p['name']), dict)):
            prev = history[p['name']]
        person_evidence = {"dates": p["engaged_dates"], "from_me": p["engaged_from_me"],
            "from_them": p["engaged_from_them"],
            "held_meetings": (meetings.get(key) or {}).get("dates", [])}
        evidence = activity_evidence(person_evidence, prev.get("importance_evidence"), now)
        engagement = engagement_signals(p, prev.get("engagement"), now)
        context = _graph_context(p["name"], p["cid"], index)
        profiles.append({
            "key": key, "name": p["name"], "cid": p["cid"],
            "interactions": len(p["dates"]), "days_since": days_since,
            "last_from_them": last_them, "last_from_you": last_you,
            "trend": _cadence_trend(sorted(set(p["engaged_dates"]))),
            "by_channel": p["by_channel"], "importance_evidence": evidence,
            "importance": classify(evidence, now), "engagement": engagement,
            "graph_context": context,
        })

    queue = []
    for pp in profiles:
        evidence = pp["importance_evidence"]
        meaningful = meaningful_relationship({
            "sent_days": people[pp["key"]]["engaged_from_me"],
            "received_days": people[pp["key"]]["engaged_from_them"]})
        # A last inbound touch alone proves no obligation. Only a visible request/missed call
        # earns a suggestion, phrased as a review because coverage and intent may be incomplete.
        lt, ly = pp["last_from_them"], pp["last_from_you"]
        reply = _reply_context(people[pp["key"]])
        tier_rank = {"regular": 0, "vip": 1, "vvip": 2}.get(pp["importance"]["tier"], 0)
        if lt is not None and (ly is None or lt > ly):
            days = int((now - lt).total_seconds() // 86400)
            if reply and WAITING_MIN_DAYS <= days <= WAITING_MAX_DAYS:
                e = {"display_name": pp["name"], "canonical_id": pp["cid"], "queue_type": "waiting_on_you",
                     "reason": f"{'Missed call' if reply.get('missed_call') else 'Possible reply'} {days} days ago; "
                               "no later response in available history",
                     "days_waiting": days, "priority": 1000 + tier_rank * 100 + int(meaningful) * 50 + days * 2
                         + attention_tiebreak(pp["engagement"], now),
                     "engagement": pp["engagement"], "context": reply,
                     "importance": pp["importance"]}
                if pp["graph_context"]:
                    e["graph_context"] = pp["graph_context"]
                queue.append(e)
        # losing_touch: drifting apart, historically strong, 14d+ silent
        if pp["trend"] == "increasing" and meaningful and pp["days_since"] >= LOSING_TOUCH_DAYS:
            e = {"display_name": pp["name"], "canonical_id": pp["cid"], "queue_type": "losing_touch",
                 "reason": f"Communication had slowed; last observed contact {pp['days_since']} days ago",
                 "days_waiting": 0, "priority": 500 + tier_rank * 100 + min(pp["days_since"], 90)
                     + attention_tiebreak(pp["engagement"], now),
                 "engagement": pp["engagement"], "importance": pp["importance"]}
            if pp["graph_context"]:
                e["graph_context"] = pp["graph_context"]
            queue.append(e)

    queue.sort(key=lambda q: q["priority"], reverse=True)
    queue = queue[:MAX_QUEUE]

    # ── Longitudinal: lapsed contacts — previously regular, now absent from the whole window ─────
    # Without history, a contact silent longer than the ~6-week read vanishes entirely. The prior
    # run's snapshot lets us surface them as fully-lost-touch, ranked BELOW losing_touch.
    lapsed, withheld = [], False
    current_keys = set(people.keys())
    current_names = {p["name"] for p in people.values()}
    for hkey, h in history.items():
        display = _s((h or {}).get("name")) or hkey    # pre-identity entries were keyed by name
        if not isinstance(h, dict) or hkey in current_keys or display in current_names:
            continue   # present this window — under this key, or migrated to a cid key below
        evidence = h.get("importance_evidence") or {}
        meaningful = meaningful_relationship(evidence)
        if not meaningful:
            continue
        last_known = _s(h.get("last_contact"))
        last = _ts(last_known)
        if last is None:
            continue
        last = last.replace(tzinfo=timezone.utc) if last.tzinfo is None else last
        if (now.date() - last.astimezone(now.tzinfo).date()).days <= 42:
            continue
        if not _absence_proven(h, absence_complete):
            withheld = True   # silence on a channel this window did not fully read proves nothing
            continue
        context = _graph_context(display, hkey if hkey != display else "", index)
        reason = "You've fully lost touch — no contact in this whole window"
        if last_known:
            reason += f" (last contact {last_known})"
        e = {"display_name": display, "canonical_id": hkey if hkey.startswith("c_") else "",
             "queue_type": "lapsed", "reason": reason,
             "last_contact": last_known, "days_waiting": 0,
             "priority": attention_tiebreak(h.get("engagement"), now),
             "engagement": h.get("engagement") or {}}
        if context:
            e["graph_context"] = context
        lapsed.append(e)
    lapsed.sort(key=lambda q: q["priority"], reverse=True)
    lapsed = lapsed[:min(MAX_LAPSED, max(0, MAX_QUEUE - len(queue)))]

    insights = [{"display_name": q["display_name"], "insight_type": "gone_silent", "description": q["reason"]}
                for q in queue if q["queue_type"] == "losing_touch"]
    insights += [{"display_name": q["display_name"], "insight_type": "lapsed", "description": q["reason"]}
                 for q in lapsed]

    # New history snapshot: current-window contacts overwrite their entry; absent contacts carry
    # forward (so they stay lapsed candidates), pruned once silent > HISTORY_MAX_AGE_DAYS.
    merged = {}
    dropped = []
    for hkey, h in history.items():
        display = _s((h or {}).get("name")) or hkey
        if not isinstance(h, dict) or hkey in current_keys or display in current_names:
            continue   # a name-keyed entry for a person now cid-keyed migrates via the peak-merge
        last = _ts(_s(h.get("last_contact")))
        if last is None:
            # No parseable last_contact → the entry would dodge the >365d prune and resurface
            # forever. Drop it so pruning actually bounds the file.
            dropped.append(display)
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() / 86400.0 > HISTORY_MAX_AGE_DAYS:
            continue
        merged[hkey] = h
    if dropped:
        print("[relationship_pulse] dropping history entries with no parseable last_contact: "
              + ", ".join(dropped), file=sys.stderr)
    for pp in profiles:
        last = max(people[pp["key"]]["dates"])
        # Peak-merge against BOTH possible prior keys: this key, and (for a person the resolver
        # only now identified) the old display-name key — the migration path off name-keyed state.
        prev = history.get(pp["key"]) if isinstance(history.get(pp["key"]), dict) else {}
        if not prev and pp["cid"] and ambiguous is not None and pp['name'].casefold() not in ambiguous:
            prev = history.get(pp["name"]) if isinstance(history.get(pp["name"]), dict) else {}
        try:
            prev_n = int(prev.get("interactions") or 0)
        except (TypeError, ValueError):
            prev_n = 0
        # Keep the PEAK interaction count: a 20-interaction regular who sends one ping must not
        # reset to 1, which would discard the history needed to recognize a lapsed relationship.
        channels = {}
        for ch, prev_day in ((prev.get("channels") or {}) if isinstance(prev.get("channels"), dict) else {}).items():
            channels[ch] = _s(prev_day)
        for ch, d in pp["by_channel"].items():
            day = d.strftime("%Y-%m-%d")
            if channels.get(ch, "") < day:
                channels[ch] = day
        merged[pp["key"]] = {"name": pp["name"], "last_contact": last.strftime("%Y-%m-%d"),
                             "interactions": max(pp["interactions"], prev_n), "trend": pp["trend"],
                             "channels": channels, "importance_evidence": pp["importance_evidence"],
                             "engagement": pp["engagement"]}
        for field, value in (('last_from_you', pp['last_from_you']), ('last_from_them', pp['last_from_them'])):
            if value:
                merged[pp['key']][field] = value.isoformat()
        incoming = people[pp['key']].get('latest_inbound')
        if incoming:
            merged[pp['key']]['latest_inbound'] = _stored_inbound(
                {**incoming, 'at': incoming['at'].isoformat(), 'request': _has_request(incoming)})
        merged[pp["key"]]["importance"] = pp["importance"]

    return {"attention_queue": queue + lapsed, "relationship_insights": insights,
            "lapsed": lapsed, "history": merged,
            "pulse_markdown": to_chat(_render(queue, lapsed)) + ("\n" + LAPSE_DISCLAIMER if withheld else ""),
            "lapse_withheld": withheld, **({} if fresh else {"degraded": True})}


def _render(queue: list, lapsed: list | None = None) -> str:
    """The weekly pulse message. No meta-announcement opener — the sections just start (voice:
    calm, minimal); the weekly empty state stays a single always-speak line. Bold is written as
    markdown **…** here and converted to chat single-asterisk by to_chat — never pre-converted
    (chatfmt is idempotent, but one pipeline means one syntax at each stage)."""
    lapsed = lapsed or []
    waiting = [q for q in queue if q["queue_type"] == "waiting_on_you"]
    losing = [q for q in queue if q["queue_type"] == "losing_touch"]
    if not waiting and not losing and not lapsed:
        return "No clear follow-up to suggest from the available relationship history this week."
    def _line(q):
        who = q["display_name"]
        company = _s((q.get("graph_context") or {}).get("company"))
        if company:
            who = f"{who} ({company})"
        context = q.get("context") or {}
        if context:
            when = _human_date(context.get('date'))
            when = f" on {when}" if when else ''
            if context.get('missed_call'):
                text = f"Missed call{when}; consider calling back if still relevant."
            elif context.get('subject'):
                text = f"Email{when} about {_compact_text(context['subject'])}; review and reply if still relevant."
            elif context.get('excerpt'):
                # Their words stay quoted and attributed; relative wording keeps its recorded date.
                excerpt = _compact_text(context['excerpt'])
                request = dated_request_text(excerpt, [{'created_at': context.get('date', '')}])
                if request == excerpt:
                    request = f"Request{when}: “{excerpt}”"
                text = f"{request} — review and reply if still relevant."
            else:
                text = f"Earlier request{when}; review and reply if still relevant."
            return f"- **{who}** — {text}"
        return f"- **{who}** — {q['reason']}. Consider a check-in."

    lines = []
    if waiting:
        lines.append("**Worth a reply review**")
        lines += [_line(q) for q in waiting]
        lines.append("No later response found in the available history.")
    if losing:
        lines.append("\n**Going quiet** (you used to talk more)")
        lines += [_line(q) for q in losing]
    if lapsed:
        lines.append("\n**Fully lost touch** (you used to be in regular contact)")
        lines += [_line(q) for q in lapsed]
    return "\n".join(lines)


def _persist_state(result: dict, history_only=False, now=None):
    """One transactional writer for pulses and historical activity; history never queues nudges."""
    import jsonstore
    if result.get('degraded'):
        return
    now = now or datetime.now(timezone.utc)
    with jsonstore.transaction(_state_path(), default={}) as state:
        previous = state.setdefault('history', {})
        for key, row in result.get('history', {}).items():
            old = previous.get(key, {})
            left, right = old.get('importance_evidence', {}), row.get('importance_evidence', {})
            evidence = activity_evidence({'dates': right.get('active_days', []),
                'from_me': right.get('sent_days', []), 'from_them': right.get('received_days', []),
                'held_meetings': right.get('held_meeting_days', [])}, left, now)
            channels = dict(old.get('channels') or {})
            for channel, date in (row.get('channels') or {}).items():
                channels[channel] = max(channels.get(channel, ''), date)
            old_engagement = old.get('engagement') if isinstance(old.get('engagement'), dict) else {}
            row_engagement = row.get('engagement') if isinstance(row.get('engagement'), dict) else {}
            merged_engagement = {**old_engagement, **row_engagement,
                'reply_samples': list(old_engagement.get('reply_samples') or [])
                                 + list(row_engagement.get('reply_samples') or [])}
            previous[key] = {**old, **row,
                'last_contact': max(old.get('last_contact', ''), row.get('last_contact', '')),
                'channels': channels, 'importance_evidence': evidence, 'importance': classify(evidence, now),
                'engagement': engagement_signals({}, merged_engagement, now)}
            for field in ('last_from_you', 'last_from_them'):
                previous[key][field] = max((old.get(field, ''), row.get(field, '')),
                    key=lambda value: _aware_ts(value) or datetime.min.replace(tzinfo=timezone.utc))
            old_in, new_in = old.get('latest_inbound') or {}, row.get('latest_inbound') or {}
            previous[key]['latest_inbound'] = _stored_inbound(max((old_in, new_in), key=lambda value:
                _aware_ts(value.get('at')) or datetime.min.replace(tzinfo=timezone.utc)))
            previous[key]['interactions'] = max(int(old.get('interactions') or 0), int(row.get('interactions') or 0))
            if history_only and old:
                previous[key]['trend'] = old.get('trend', row.get('trend'))
        # Prune against the merged state too; concurrent or historical runs cannot resurrect a
        # year-old contact or leave a legacy name key beside its resolved canonical identity.
        names = {v.get('name') for k, v in previous.items() if k.startswith('c_')}
        kept = {}
        for key, row in previous.items():
            last = _ts(_s(row.get('last_contact')))
            if last and key not in names:
                last = last.replace(tzinfo=timezone.utc) if last.tzinfo is None else last
                if (now - last).total_seconds() <= HISTORY_MAX_AGE_DAYS * 86400:
                    kept[key] = _stored_history_row(row)   # scrub wording an older writer kept
        state['history'] = kept
        if not history_only:
            state['attention_queue'] = result['attention_queue']
            state['relationship_insights'] = result['relationship_insights']
        if isinstance(state.get('attention_queue'), list):
            state['attention_queue'] = [_stored_queue_row(row) for row in state['attention_queue']]


def observe_history(local, now=None):
    result = compute(local, now=now, history=_load_history())
    _persist_state(result, history_only=True, now=now)
    return {'people': len(result.get('history', {}))}


def main():
    argv = sys.argv[1:]
    gmail_path = ""
    if "--gmail" in argv:                          # the gather's 6-week Gmail file (both directions)
        i = argv.index("--gmail")
        gmail_path = argv[i + 1] if i + 1 < len(argv) else ""
        del argv[i:i + 2]
    raw = open(argv[0]).read() if argv else sys.stdin.read()
    local = json.loads(raw) if raw.strip() else {}
    if not isinstance(local, dict):
        local = {}
    email_complete = False
    if gmail_path:
        try:
            with open(gmail_path, encoding="utf-8") as f:
                g = json.load(f)
            emails = g.get("emails") if isinstance(g, dict) else g
            if isinstance(emails, list) and "emails" not in local:
                local = {**local, "emails": emails}
                # The gather wraps its rows with `truncated_at` when any read hit its limit.
                email_complete = not (isinstance(g, dict) and g.get("truncated_at"))
        except (OSError, ValueError):
            pass                                   # no mail file → the message-only pulse
    covered = complete_channels(local.get("source_status") if "source_status" in local else None,
                                email_complete=email_complete)
    result = compute(local, history=_load_history(), absence_complete=covered)
    _persist_state(result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
