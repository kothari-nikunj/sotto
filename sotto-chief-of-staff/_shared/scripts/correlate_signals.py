#!/usr/bin/env python3
"""
correlate_signals.py — cross-source signal correlation CLI (used by the `people` skill).

PORT SOURCE: api/src/pipeline/signals.ts — the matchings:
  domain->email, granola->calendar, granola->email, file->meeting, file->email, file->granola
Plus multi-factor event scoring. Input is the merged context (local from the Bridge +
Gmail/Calendar/Granola from Hermes). Output: links + per-event signal scores.

CONVERGENCE: the domain/exclusion logic is SHARED with compose_brief._correlate_signals (we import
its `_base_domain` / `_is_excluded_domain`) so the brief and the people skill apply the SAME rules —
no hosting/consumer-domain false positives (e.g. google.com, gmail.com). compose_brief's version is
the rich in-brief correlator; this is the standalone CLI for the people skill's "who's slipping" view.

Usage: correlate_signals.py '{"chrome":[...],"emails":[...],"calendar":[...],"granola":[...],"files":[...]}'
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compose_brief as cb  # noqa: E402  (shared domain/exclusion helpers — one source of truth)


def _host(url: str) -> str:
    u = (url or "").split("//")[-1]
    return u.split("/")[0].lower()


def _norm(s: str) -> set:
    return {w for w in (s or "").lower().split() if len(w) > 2}


def correlate(ctx: dict) -> dict:
    links = []

    # 1) domain -> email: a browsed host matches an email sender's domain (base-domain compared, and
    #    hosting/consumer domains excluded — shared rules, so google.com/gmail.com don't false-match).
    browsed = {cb._base_domain(_host(c.get("url", ""))) for c in ctx.get("chrome", [])}
    browsed = {d for d in browsed if d and not cb._is_excluded_domain(d)}
    for e in ctx.get("emails", []):
        dom = cb._base_domain(cb._sender_addr(e.get("from", "")))
        if dom and not cb._is_excluded_domain(dom) and dom in browsed:
            links.append({"type": "domain->email", "email": e.get("id"), "domain": dom})

    # 2) granola -> calendar: meeting-notes title/attendees match a calendar event
    for g in ctx.get("granola", []):
        g_tokens = _norm(g.get("title", ""))
        g_people = {p.lower() for p in g.get("attendees", [])}
        for ev in ctx.get("calendar", []):
            ev_tokens = _norm(ev.get("summary", ""))
            ev_people = {cb._s(a.get("email") if isinstance(a, dict) else a).lower()
                         for a in ev.get("attendees", [])}
            if len(g_tokens & ev_tokens) >= 2 or (g_people & ev_people):
                links.append({"type": "granola->calendar", "granola": g.get("id"), "event": ev.get("id")})

    # 3) granola -> email: a meeting attendee is also an active email sender (compare addresses).
    email_addrs = {cb._sender_addr(e.get("from", "")).lower() for e in ctx.get("emails", []) if e.get("from")}
    for g in ctx.get("granola", []):
        for p in g.get("attendees", []):
            if cb._s(p).lower() in email_addrs:
                links.append({"type": "granola->email", "granola": g.get("id"), "person": p})

    # 4-6) file -> meeting / email / granola: a file the user downloaded relates to a meeting (title
    #      tokens), an email sender (its source-url domain == sender domain), or a past meeting (tokens).
    for fobj in ctx.get("files", []):
        f_tokens = _norm(fobj.get("name", ""))
        f_dom = cb._base_domain(_host(fobj.get("source_url", "") or fobj.get("url", "")))
        for ev in ctx.get("calendar", []):
            if len(f_tokens & _norm(ev.get("summary", ""))) >= 2:
                links.append({"type": "file->meeting", "file": fobj.get("name"), "event": ev.get("id")})
        if f_dom and not cb._is_excluded_domain(f_dom):
            for e in ctx.get("emails", []):
                if cb._base_domain(cb._sender_addr(e.get("from", ""))) == f_dom:
                    links.append({"type": "file->email", "file": fobj.get("name"), "email": e.get("id")})
        for g in ctx.get("granola", []):
            if len(f_tokens & _norm(g.get("title", ""))) >= 2:
                links.append({"type": "file->granola", "file": fobj.get("name"), "granola": g.get("id")})

    # multi-factor event score: more matched signals => higher priority
    scores = {}
    for ev in ctx.get("calendar", []):
        eid = ev.get("id")
        scores[eid] = sum(1 for l in links if l.get("event") == eid)

    return {"links": links, "event_scores": scores}


if __name__ == "__main__":
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    print(json.dumps(correlate(json.loads(raw))))
