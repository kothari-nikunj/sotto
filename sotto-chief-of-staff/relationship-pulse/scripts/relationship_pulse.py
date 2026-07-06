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
sys.path.insert(0, _SHARED)
import compose_brief as cb  # noqa: E402

WAITING_MIN_DAYS = 3       # relationship_analytics.rs:469
WAITING_MAX_DAYS = 14
LOSING_TOUCH_DAYS = 14     # relationship_analytics.rs:494
MIN_INTERACTIONS_FOR_TREND = 8   # need 8 dates → 7 intervals (rs:1149-1151)
MAX_QUEUE = 20
LAPSED_MIN_INTERACTIONS = 5      # "previously regular" = at least this many touches in a past window
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


# ── Knowledge-graph importance weighting ──────────────────────────────────────
# The Mac app ranked the attention queue purely on interaction volume. Here we also weight by how
# much the user *tracks* a person in the knowledge graph: someone with a people/*.md file is someone
# worth remembering, and a richer file (facts, talking points, a known company/title) means a more
# important relationship. So a graph-flagged person going quiet outranks a chatty-but-shallow contact.
_KG = None  # None=unloaded, False=unavailable, module=loaded


def _knowledge():
    global _KG
    if _KG is None:
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "lib"))
            import knowledge as kg  # noqa: PLC0415
            _KG = kg
        except Exception:
            _KG = False
    return _KG or None


def _graph_lookup(name: str):
    """(weight, context) for a person from the knowledge graph. Untracked → (1.0, None), so the
    weighting is a pure enhancement that degrades to the old volume-only ranking when the graph is
    empty or unreadable. `context` carries grounded material (company/title/a talking point/a fact)
    the skill can use to draft a reconnect note WITHOUT improvising."""
    kg = _knowledge()
    if kg is None:
        return 1.0, None
    try:
        slug = kg.safe_slug(name)
        if not slug:
            return 1.0, None
        path = kg.safe_path(kg.people_dir(), slug)
        if not os.path.exists(path):
            return 1.0, None
        with open(path, encoding="utf-8") as f:
            p = kg.parse_person_file(f.read())
        active = kg.sorted_active_facts(p.facts)
        weight = 1.5                          # tracked at all → matters more than a random contact
        weight += min(len(active), 8) * 0.15  # depth of what we know
        if p.talking_points:
            weight += 0.3
        if p.company or p.title:
            weight += 0.2
        ctx = {
            "company": cb._s(p.company),
            "title": cb._s(p.title),
            "talking_point": cb._s(p.talking_points[0]) if p.talking_points else "",
            "fact": cb._s(active[0][1].text) if active else "",
            "summary": cb._s(p.summary),
        }
        return round(weight, 3), {k: v for k, v in ctx.items() if v}
    except Exception:
        return 1.0, None


def _ts(s: str):
    return cb._parse_ts(cb._s(s))


def _interactions_by_contact(local: dict) -> dict:
    """Group every inbound/outbound touch (message + call) per resolved contact. Skips group chats
    and unknown (phone-named) senders — same is_known_contact gate as the brief."""
    people: dict = {}

    def add(name, ts, from_me):
        nm = cb._s(name).strip()
        if not nm or cb._looks_like_phone_number(nm):
            return
        d = _ts(ts)
        if d is None:
            return
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        p = people.setdefault(nm, {"dates": [], "from_me": [], "from_them": []})
        p["dates"].append(d)
        (p["from_me"] if from_me else p["from_them"]).append(d)

    for m in cb._arr(local, "imessage"):
        if m.get("is_group_chat"):
            continue
        add(m.get("resolved_name"), m.get("timestamp"), m.get("is_from_me"))
    for m in cb._arr(local, "whatsapp"):
        if m.get("is_group_chat"):
            continue
        nm = cb._s(m.get("resolved_name")) or cb._s(m.get("partner_name"))
        add(nm, m.get("timestamp"), m.get("is_from_me"))
    for c in cb._arr(local, "missed_calls") + cb._arr(local, "recent_calls"):
        add(c.get("name"), c.get("timestamp"), c.get("is_outgoing"))
    return people


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
    local = cb.resolve_contact_names(local)
    people = _interactions_by_contact(local)

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
    for name, p in people.items():
        if not p["dates"]:
            continue
        last = max(p["dates"])
        days_since = int((now - last).total_seconds() // 86400)
        last_them = max(p["from_them"]) if p["from_them"] else None
        last_you = max(p["from_me"]) if p["from_me"] else None
        weight, gctx = _graph_lookup(name)
        profiles.append({
            "name": name, "interactions": len(p["dates"]), "days_since": days_since,
            "last_from_them": last_them, "last_from_you": last_you,
            "trend": _cadence_trend(p["dates"]),
            "graph_weight": weight, "graph_context": gctx,
        })

    # Strength gate: historically meaningful = interaction count at/above the median (bypass if
    # the population is tiny — percentiles are unstable, rs:448-456).
    counts = sorted(pp["interactions"] for pp in profiles)
    median = counts[len(counts) // 2] if counts else 0
    strength_gate = 0 if len(profiles) < 20 else median

    queue = []
    for pp in profiles:
        w, gctx = pp["graph_weight"], pp["graph_context"]
        # waiting_on_you: they sent last and you haven't answered (3-14 days)
        lt, ly = pp["last_from_them"], pp["last_from_you"]
        if lt is not None and (ly is None or lt > ly):
            days = int((now - lt).total_seconds() // 86400)
            if WAITING_MIN_DAYS <= days <= WAITING_MAX_DAYS:
                e = {"display_name": pp["name"], "queue_type": "waiting_on_you",
                     "reason": f"Unanswered {days} days" if days >= 7 else f"Waiting {days} days for reply",
                     "days_waiting": days, "priority": round(pp["interactions"] * min(days, 7) * w, 2)}
                if gctx:
                    e["graph_context"] = gctx
                queue.append(e)
        # losing_touch: drifting apart, historically strong, 14d+ silent
        if pp["trend"] == "increasing" and pp["interactions"] >= strength_gate and pp["days_since"] >= LOSING_TOUCH_DAYS:
            e = {"display_name": pp["name"], "queue_type": "losing_touch",
                 "reason": f"Communication declining — last spoke {pp['days_since']} days ago",
                 "days_waiting": 0, "priority": round(pp["interactions"] * w, 2)}
            if gctx:
                e["graph_context"] = gctx
            queue.append(e)

    queue.sort(key=lambda q: q["priority"], reverse=True)
    queue = queue[:MAX_QUEUE]

    # ── Longitudinal: lapsed contacts — previously regular, now absent from the whole window ─────
    # Without history, a contact silent longer than the ~6-week read vanishes entirely. The prior
    # run's snapshot lets us surface them as fully-lost-touch, ranked BELOW losing_touch.
    lapsed = []
    current_names = set(people.keys())
    for name, h in history.items():
        if not isinstance(h, dict) or name in current_names:
            continue
        if int(h.get("interactions") or 0) < LAPSED_MIN_INTERACTIONS:
            continue   # a one-off back then isn't a lapsed relationship now
        last_known = cb._s(h.get("last_contact"))
        weight, gctx = _graph_lookup(name)
        reason = "You've fully lost touch — no contact in this whole window"
        if last_known:
            reason += f" (last contact {last_known})"
        e = {"display_name": name, "queue_type": "lapsed", "reason": reason,
             "last_contact": last_known, "days_waiting": 0,
             "priority": round(int(h.get("interactions") or 0) * weight, 2)}
        if gctx:
            e["graph_context"] = gctx
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
    for name, h in history.items():
        if not isinstance(h, dict) or name in current_names:
            continue
        last = _ts(cb._s(h.get("last_contact")))
        if last is None:
            # No parseable last_contact → the entry would dodge the >365d prune and resurface
            # forever. Drop it so pruning actually bounds the file.
            dropped.append(name)
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() / 86400.0 > HISTORY_MAX_AGE_DAYS:
            continue
        merged[name] = h
    if dropped:
        print("[relationship_pulse] dropping history entries with no parseable last_contact: "
              + ", ".join(dropped), file=sys.stderr)
    for pp in profiles:
        last = max(people[pp["name"]]["dates"])
        prev = history.get(pp["name"]) if isinstance(history.get(pp["name"]), dict) else {}
        try:
            prev_n = int(prev.get("interactions") or 0)
        except (TypeError, ValueError):
            prev_n = 0
        # Keep the PEAK interaction count: a 20-interaction regular who sends one ping must not
        # reset to 1 (they could then never clear the lapsed >= LAPSED_MIN_INTERACTIONS gate).
        merged[pp["name"]] = {"last_contact": last.strftime("%Y-%m-%d"),
                              "interactions": max(pp["interactions"], prev_n), "trend": pp["trend"]}

    return {"attention_queue": queue + lapsed, "relationship_insights": insights,
            "lapsed": lapsed, "history": merged,
            "pulse_markdown": _render(queue, lapsed)}


def _render(queue: list, lapsed: list | None = None) -> str:
    lapsed = lapsed or []
    waiting = [q for q in queue if q["queue_type"] == "waiting_on_you"]
    losing = [q for q in queue if q["queue_type"] == "losing_touch"]
    if not waiting and not losing and not lapsed:
        return "Your relationships look healthy this week — no one's been left waiting and no one important is going quiet."
    def _line(q):
        who = q["display_name"]
        co = cb._s((q.get("graph_context") or {}).get("company"))
        if co:
            who = f"{who} ({co})"
        return f"- **{who}** — {q['reason'].lower()}"

    lines = ["Here's your weekly relationship pulse — who could use a nudge."]
    if waiting:
        lines.append("\n*Waiting on you*")
        lines += [_line(q) for q in waiting]
    if losing:
        lines.append("\n*Going quiet* (you used to talk more)")
        lines += [_line(q) for q in losing]
    if lapsed:
        lines.append("\n*Fully lost touch* (you used to be in regular contact)")
        lines += [_line(q) for q in lapsed]
    return "\n".join(lines)


def _persist_state(result: dict):
    """Write the queue (+ the per-contact history snapshot) so the daily brief can surface it and the
    NEXT pulse run can detect lapsed contacts. A degraded (empty-window) result persists NOTHING —
    one bad read must not overwrite the longitudinal state."""
    if result.get("degraded"):
        return
    try:
        path = _state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        state = {"attention_queue": result["attention_queue"],
                 "relationship_insights": result["relationship_insights"]}
        if isinstance(result.get("history"), dict):
            state["history"] = result["history"]
        # tmp + atomic rename (preferences.py _save pattern): this file is read-back state — a torn
        # write would corrupt the next run's history.
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except Exception:
        pass


def main():
    raw = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
    local = json.loads(raw) if raw.strip() else {}
    result = compute(local, history=_load_history())
    _persist_state(result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
