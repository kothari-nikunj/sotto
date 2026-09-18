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

Usage (execute_code): relationship_pulse.py /tmp/sotto_local_6w.json   (or stdin)
Prints JSON: { "attention_queue":[...], "relationship_insights":[...], "lapsed":[...],
               "pulse_markdown":"..." }
Also writes $SOTTO_DATA/knowledge/relationship_state.json (queue + insights + per-contact history)
so the daily brief can surface it too. No history yet (first run) → no lapsed section; degrades clean.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

_SHARED = os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "scripts")
_SHARED_LIB = os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "lib")
sys.path.insert(0, _SHARED)
if _SHARED_LIB not in sys.path:
    sys.path.insert(0, _SHARED_LIB)
from email.utils import getaddresses  # noqa: E402
from textutil import _arr, _is_likely_automated, _looks_like_phone_number, _s  # noqa: E402
from timeutil import _parse_ts, configured_user_email  # noqa: E402
from render_local import _is_sent_email, resolve_contact_names  # noqa: E402
from chatfmt import to_chat  # noqa: E402
from relationship_importance import (activity_evidence, attention_tiebreak, classify,
                                     engagement_signals, meaningful_relationship,
                                     relationship_meeting_attendees)  # noqa: E402

WAITING_MIN_DAYS = 3       # relationship_analytics.rs:469
WAITING_MAX_DAYS = 14
LOSING_TOUCH_DAYS = 14     # relationship_analytics.rs:494
MIN_INTERACTIONS_FOR_TREND = 8   # need 8 dates → 7 intervals (rs:1149-1151)
MAX_QUEUE = 20
MAX_LAPSED = 10
HISTORY_MAX_AGE_DAYS = 365       # drop history entries silent for over a year (not a relationship)


def _data_root() -> str:
    return os.environ.get("SOTTO_DATA", "/data")


def _state_path() -> str:
    return os.path.join(_data_root(), "knowledge", "relationship_state.json")


def _load_history() -> dict:
    """Per-contact snapshots from the last run: {name: {last_contact, interactions, trend}}.
    Missing/corrupt state → {} (first run degrades to the old no-memory behavior)."""
    try:
        with open(_state_path(), encoding="utf-8") as f:
            h = (json.load(f) or {}).get("history")
        return h if isinstance(h, dict) else {}
    except Exception:
        return {}


def _ts(s: str):
    return _parse_ts(_s(s))


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

    def add(name, ts, from_me, channel, cid="", conversation_id="", native_id="", engaged=True):
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
        if engaged:
            p["engaged_dates"].append(d)
            (p["engaged_from_me"] if from_me else p["engaged_from_them"]).append(d)
        if conversation_id:
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
            m.get("message_id") or m.get("guid") or m.get("id"))
    for m in _arr(local, "whatsapp"):
        if m.get("is_group_chat"):
            continue
        nm = _s(m.get("resolved_name")) or _s(m.get("partner_name"))
        add(nm, m.get("timestamp"), m.get("is_from_me"), "whatsapp", m.get("canonical_id"),
            m.get("chat_id") or m.get("partner_id"), m.get("message_id") or m.get("id"))
    for c in _arr(local, "missed_calls"):
        add(c.get("name"), c.get("timestamp"), False, "calls", c.get("canonical_id"), engaged=False)
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
                m.get("threadId") or m.get("thread_id"), m.get("id") or m.get("message_id"))
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
        for ch, d in src["by_channel"].items():
            if ch not in dst["by_channel"] or d > dst["by_channel"][ch]:
                dst["by_channel"][ch] = d
    return people


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


def compute(local: dict, now: datetime | None = None, history: dict | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    history = history or {}
    local = resolve_contact_names(local)
    try:
        kg = _knowledge()
        index = kg.build_people_index() if kg else {}
    except Exception:
        index = {}
    people = _interactions_by_contact(local, index)
    meetings = _held_meetings(local, now, index)
    for key, held in meetings.items():
        person = people.setdefault(key, {"name": held["name"], "cid": held["cid"], "dates": [],
            "from_me": [], "from_them": [], "by_channel": {}, "events": [], "engaged_dates": [],
            "engaged_from_me": [], "engaged_from_them": []})
        person["dates"].extend(held["dates"])

    # Empty/degraded snapshot guard: a window with NO interactions at all means the read failed or
    # the Bridge was offline, not that the user ghosted everyone. Emitting lapsed entries here would
    # mark a 2-days-ago contact "fully lost touch", and merging would poison the longitudinal state.
    # Return empty results with degraded=True — _persist_state skips writing, keeping prior state.
    if not people:
        return {"attention_queue": [], "relationship_insights": [], "lapsed": [],
                "pulse_markdown": ("I couldn't read any interactions this window — "
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
            "trend": _cadence_trend(p["dates"]),
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
        # waiting_on_you: they sent last and you haven't answered (3-14 days)
        lt, ly = pp["last_from_them"], pp["last_from_you"]
        if lt is not None and (ly is None or lt > ly):
            days = int((now - lt).total_seconds() // 86400)
            if WAITING_MIN_DAYS <= days <= WAITING_MAX_DAYS:
                e = {"display_name": pp["name"], "queue_type": "waiting_on_you",
                     "reason": f"Unanswered {days} days" if days >= 7 else f"Waiting {days} days for reply",
                     "days_waiting": days, "priority": days * 10 + attention_tiebreak(pp["engagement"], now),
                     "engagement": pp["engagement"]}
                if pp["graph_context"]:
                    e["graph_context"] = pp["graph_context"]
                queue.append(e)
        # losing_touch: drifting apart, historically strong, 14d+ silent
        if pp["trend"] == "increasing" and meaningful and pp["days_since"] >= LOSING_TOUCH_DAYS:
            e = {"display_name": pp["name"], "queue_type": "losing_touch",
                 "reason": f"Communication declining — last spoke {pp['days_since']} days ago",
                 "days_waiting": 0, "priority": pp["days_since"] * 10 + attention_tiebreak(pp["engagement"], now),
                 "engagement": pp["engagement"]}
            if pp["graph_context"]:
                e["graph_context"] = pp["graph_context"]
            queue.append(e)

    queue.sort(key=lambda q: q["priority"], reverse=True)
    queue = queue[:MAX_QUEUE]

    # ── Longitudinal: lapsed contacts — previously regular, now absent from the whole window ─────
    # Without history, a contact silent longer than the ~6-week read vanishes entirely. The prior
    # run's snapshot lets us surface them as fully-lost-touch, ranked BELOW losing_touch.
    lapsed = []
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
        context = _graph_context(display, hkey if hkey != display else "", index)
        reason = "You've fully lost touch — no contact in this whole window"
        if last_known:
            reason += f" (last contact {last_known})"
        e = {"display_name": display, "queue_type": "lapsed", "reason": reason,
             "last_contact": last_known, "days_waiting": 0,
             "priority": attention_tiebreak(h.get("engagement"), now),
             "engagement": h.get("engagement") or {}}
        if context:
            e["graph_context"] = context
        lapsed.append(e)
    lapsed.sort(key=lambda q: q["priority"], reverse=True)
    lapsed = lapsed[:MAX_LAPSED]

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
        if not prev and pp["cid"]:
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
        merged[pp["key"]]["importance"] = pp["importance"]

    return {"attention_queue": queue + lapsed, "relationship_insights": insights,
            "lapsed": lapsed, "history": merged,
            "pulse_markdown": to_chat(_render(queue, lapsed))}


def _render(queue: list, lapsed: list | None = None) -> str:
    """The weekly pulse message. No meta-announcement opener — the sections just start (voice:
    calm, minimal); the weekly empty state stays a single always-speak line. Bold is written as
    markdown **…** here and converted to chat single-asterisk by to_chat — never pre-converted
    (chatfmt is idempotent, but one pipeline means one syntax at each stage)."""
    lapsed = lapsed or []
    waiting = [q for q in queue if q["queue_type"] == "waiting_on_you"]
    losing = [q for q in queue if q["queue_type"] == "losing_touch"]
    if not waiting and not losing and not lapsed:
        return "Relationships look healthy this week — no one's waiting on you, no one's gone quiet."
    def _line(q):
        who = q["display_name"]
        company = _s((q.get("graph_context") or {}).get("company"))
        if company:
            who = f"{who} ({company})"
        return f"- **{who}** — {q['reason'].lower()}"

    lines = []
    if waiting:
        lines.append("**Waiting on you**")
        lines += [_line(q) for q in waiting]
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
                    kept[key] = row
        state['history'] = kept
        if not history_only:
            state['attention_queue'] = result['attention_queue']
            state['relationship_insights'] = result['relationship_insights']


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
    if gmail_path:
        try:
            with open(gmail_path, encoding="utf-8") as f:
                g = json.load(f)
            emails = g.get("emails") if isinstance(g, dict) else g
            if isinstance(emails, list) and "emails" not in local:
                local = {**local, "emails": emails}
        except (OSError, ValueError):
            pass                                   # no mail file → the message-only pulse
    result = compute(local, history=_load_history())
    _persist_state(result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
