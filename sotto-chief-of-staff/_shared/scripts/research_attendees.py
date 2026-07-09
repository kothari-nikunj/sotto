#!/usr/bin/env python3
"""
research_attendees.py — batched attendee research via Gemini Search Grounding.

FAITHFUL PORT of the desktop app's api/src/services/gemini-research.ts (researchBatch /
researchAttendees). Learnings ported verbatim:
  • BATCH of 5 attendees per grounded Gemini call (25 people → 5 calls, not 25) — the big efficiency win
  • grounding + STRUCTURED output together: tools=[google_search] + responseSchema → clean attendee objects
  • dedupe by email, cap at 25, run batches CONCURRENTLY, 60s timeout per batch (one failure ≠ all fail)
  • feed a meeting-context summary so `relevance` reflects the actual agenda
  • uses the GOOGLE_AI_API_KEY we already have — no Firecrawl/Tavily/Parallel key

This replaces the per-attendee sub-agent fan-out for research: fewer calls, deterministic, structured,
and matches what shipped on the Mac. (web_research.py remains for ad-hoc one-off grounded lookups.)

Input (argv files or stdin JSON):
  --attendees /tmp/sotto_research_in.json   [{name,email}, …]  (from select_attendees.py)
  --context   /tmp/sotto_cal.json           calendar/events for the meeting-context summary (optional)
Output (stdout): {"attendees":[{email,title,company,relevance[],summary}, …]}
Env: GOOGLE_AI_API_KEY (required), SOTTO_GEMINI_MODEL (default gemini-3-flash-preview).
Test: SOTTO_LLM_STUB=/path/to/{"attendees":[...]}.json bypasses the network.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compose_brief as cb  # noqa: E402  (shared helpers: _s, _arr, _parse_ts, _diag-ish)

# Hoist the _shared/lib path onto sys.path ONCE at import (guarded), not per call. research() fans
# batches across a ThreadPool; a per-call sys.path.insert inside _metrics()/_diag() would have workers
# mutating sys.path concurrently every batch. A single guarded module-level insert is thread-safe.
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

MODEL = os.environ.get("SOTTO_GEMINI_MODEL", "gemini-3-flash-preview")
MAX_ATTENDEES = 25
BATCH_SIZE = 5
PER_BATCH_TIMEOUT = 60
MAX_CONCURRENCY = 5

# responseSchema (REST form) — same fields as the desktop BATCH_ATTENDEE_SCHEMA.
SCHEMA = {
    "type": "object",
    "properties": {
        "attendees": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "title": {"type": "string", "nullable": True},
                    "company": {"type": "string"},
                    "relevance": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                },
                "required": ["email", "company", "summary"],
            },
        }
    },
    "required": ["attendees"],
}


def _diag(msg: str) -> None:
    try:
        from sotto_log import diag  # _LIB already on sys.path (module-level, thread-safe)
        diag(msg)
    except Exception:
        print(msg, file=sys.stderr)


def _metrics():
    """Lazy, best-effort handle on the shared cost/latency accumulator. Swallowed on failure so
    research observability never blocks the brief."""
    try:
        import metrics  # _LIB already on sys.path (module-level, thread-safe)
        return metrics
    except Exception:
        return None


def _build_prompt(batch: list, context_summary: str) -> str:
    lines = []
    for a in batch:
        name = cb._s(a.get("name")) or cb._s(a.get("email")).split("@")[0]
        email = cb._s(a.get("email"))
        domain = email.split("@")[1] if "@" in email else ""
        has_last = len(name.split()) >= 2
        lines.append(f"- {name} <{email}>" if has_last
                     else f"- {name} <{email}> (use domain {domain} to identify company)")
    return (
        f"## Meeting Context\n{context_summary or '(none)'}\n\n"
        f"## Attendees to Research ({len(batch)} people)\n" + "\n".join(lines) + "\n\n"
        "## Task\nFor EACH attendee, search using \"[Name] [Company/Domain] LinkedIn\" and "
        "\"[Company name] product\". Return one entry per attendee containing:\n"
        "- email: exactly as listed above\n- title: current job title (null if not found)\n"
        "- company: full company name\n- relevance: 1-2 bullets on relevance to the meeting context\n"
        "- summary: 3-4 sentence professional bio — current focus, what they do, 2-3 past roles. "
        "Do NOT include email context or how the user knows them.\n"
        "Stay factual — never guess a title, employer, or funding stage. Do NOT state a specific dollar "
        "figure (ARR, valuation, raise, exit price) unless your web search actually surfaced it; if "
        "unsure, omit the number. If nothing is found: title=null, summary=\"No public profile found.\", "
        "relevance=[]."
    )


def _gemini_grounded(prompt: str, key: str, use_schema: bool) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={key}"
    gen = {"maxOutputTokens": 8192}
    if use_schema:
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = SCHEMA
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}], "generationConfig": gen}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    import time as _time
    t0 = _time.monotonic()
    with urllib.request.urlopen(req, timeout=PER_BATCH_TIMEOUT) as resp:
        data = json.loads(resp.read())
    m = _metrics()                        # tag this grounded batch as the 'research' phase (thread-safe)
    if m is not None:
        # Guard the CALL itself (not just metrics' internals): a foreign 'metrics' shadow via sys.modules
        # could raise here AFTER the billed batch succeeded and fail research for the whole brief.
        try:
            um = data.get("usageMetadata") or {}
            m.record("research", _time.monotonic() - t0,
                     um.get("promptTokenCount"), um.get("candidatesTokenCount"), MODEL)
        except Exception:
            pass
    cand = (data.get("candidates") or [{}])[0]
    return "".join(p.get("text", "") for p in (cand.get("content", {}).get("parts") or []))


def _research_batch(batch: list, context_summary: str, key: str) -> list:
    prompt = _build_prompt(batch, context_summary)
    # Some Gemini versions reject google_search + responseSchema together; if so, retry grounding-only
    # and parse the JSON the prompt asked for. Either way we degrade to [] on failure (never invent).
    for use_schema in (True, False):
        try:
            raw = _gemini_grounded(prompt + ("" if use_schema else "\n\nReturn ONLY JSON: "
                                  "{\"attendees\":[{email,title,company,relevance,summary}]}"),
                                   key, use_schema)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1].lstrip("json").strip() if "```" in raw else raw
            return (json.loads(raw) or {}).get("attendees", []) or []
        except Exception as e:  # noqa: BLE001
            if use_schema:
                continue  # try grounding-only
            _diag(f"[research_attendees] batch failed ({type(e).__name__}: {str(e)[:100]})")
            return []
    return []


def research(attendees: list, context_summary: str) -> dict:
    m = _metrics()
    if m is not None:
        try:
            m.start_run()
        except Exception:
            pass

    def _fin(res: dict) -> dict:
        # Emit the research phase's own [brief-cost] line + jsonl record (research runs as its own
        # process, ahead of compose, so it can't share compose's in-memory accumulator).
        if m is not None:
            try:
                import datetime as _dt
                m.emit(_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"), "research")
            except Exception:
                pass
        return res

    stub = os.environ.get("SOTTO_LLM_STUB")
    if stub:
        try:
            with open(stub, encoding="utf-8") as f:
                return _fin({"attendees": (json.load(f) or {}).get("attendees", [])})
        except Exception:
            return _fin({"attendees": []})
    key = os.environ.get("GOOGLE_AI_API_KEY")
    if not key or not attendees:
        return _fin({"attendees": []})
    # dedupe by email (lowercase), cap at 25
    seen, unique = set(), []
    for a in attendees:
        em = cb._s(a.get("email")).lower()
        if em and em not in seen:
            seen.add(em); unique.append(a)
    capped = unique[:MAX_ATTENDEES]
    if len(unique) > MAX_ATTENDEES:
        _diag(f"[research_attendees] capping {len(unique)} → {MAX_ATTENDEES}")
    batches = [capped[i:i + BATCH_SIZE] for i in range(0, len(capped), BATCH_SIZE)]
    out = []
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENCY, len(batches) or 1)) as ex:
        futs = [ex.submit(_research_batch, b, context_summary, key) for b in batches]
        for fut in as_completed(futs):
            out.extend(fut.result() or [])
    _diag(f"[research_attendees] {len(out)}/{len(capped)} researched in {len(batches)} grounded call(s)")
    return _fin({"attendees": out})


def _context_summary(calendar) -> str:
    events = calendar.get("events") if isinstance(calendar, dict) else calendar
    if not isinstance(events, list):
        return ""
    rows = []
    for e in sorted(events, key=lambda x: cb._s(x.get("start"))):
        t = cb._s(e.get("summary") or e.get("title"))
        desc = cb._s(e.get("description"))[:500]
        if t:
            rows.append(f'Meeting "{t}" at {cb._s(e.get("start"))}' + (f": {desc}" if desc else ""))
    return "\n".join(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--attendees")
    ap.add_argument("--context")
    a = ap.parse_args()

    def load(p, d):
        if not p:
            return d
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return d

    attendees = load(a.attendees, None)
    if attendees is None and not sys.stdin.isatty():
        try:
            attendees = json.loads(sys.stdin.read() or "[]")
        except Exception:
            attendees = []
    attendees = attendees if isinstance(attendees, list) else (attendees or {}).get("attendees", []) if isinstance(attendees, dict) else []
    print(json.dumps(research(attendees, _context_summary(load(a.context, [])))))


if __name__ == "__main__":
    main()
