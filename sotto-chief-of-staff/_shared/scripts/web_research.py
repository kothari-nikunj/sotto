#!/usr/bin/env python3
"""
web_research.py — grounded web search via Gemini Search Grounding (uses the GOOGLE_AI_API_KEY we ALREADY
have; no Firecrawl/Tavily/Brave key, no Nous Portal). This is the Google-native "search + read" path the
desktop app used (api/src/services/web-search.ts → Gemini grounding): one Gemini call with the
`google_search` tool returns synthesized content WITH citations to the real pages it read.

Why this over snippet-only host web_search: the model fetches and reads the sources, so attendee bios are
deeper AND every claim is backed by a citation — which is exactly what stops invented figures (the "$115M
Parse.ly exit" class of bug). Pairs with research-prompt.md's "no unsourced numbers" rule: a figure is
allowed only if it's in the grounded result's citations.

Usage:
  web_research.py "Peyton Casper Browserbase"            # prints {query, text, citations:[{title,uri}]}
  web_research.py --json '["q1","q2"]'                    # batch: prints [{query,text,citations}, ...]
Env: GOOGLE_AI_API_KEY (required), SOTTO_GEMINI_MODEL (default gemini-3.6-flash).
Test: SOTTO_LLM_STUB=/path/to/response.json bypasses the network (returns that file's text, no citations).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

MODEL = os.environ.get("SOTTO_GEMINI_MODEL", "gemini-3.6-flash")


def _diag(msg: str) -> None:
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
        from sotto_log import diag
        diag(msg)
    except Exception:
        print(msg, file=sys.stderr)


def research(query: str) -> dict:
    """One grounded Gemini search. Returns {query, text, citations:[{title,uri}]}. Never raises — on
    error returns empty text so a research sub-agent degrades (and the brief omits, never invents)."""
    stub = os.environ.get("SOTTO_LLM_STUB")
    if stub:
        try:
            with open(stub, encoding="utf-8") as f:
                return {"query": query, "text": f.read(), "citations": []}
        except Exception:
            return {"query": query, "text": "", "citations": []}
    key = os.environ.get("GOOGLE_AI_API_KEY")
    if not key:
        return {"query": query, "text": "", "citations": [], "error": "GOOGLE_AI_API_KEY not set"}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={key}"
    body = {
        "contents": [{"parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],   # native Google Search grounding
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except Exception as e:  # noqa: BLE001
        _diag(f"[web_research] '{query[:60]}' failed: {type(e).__name__}: {str(e)[:120]}")
        return {"query": query, "text": "", "citations": [], "error": str(e)[:200]}
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content", {}).get("parts") or []))
    citations = []
    for ch in (cand.get("groundingMetadata", {}).get("groundingChunks") or []):
        web = ch.get("web") or {}
        if web.get("uri"):
            citations.append({"title": web.get("title") or "", "uri": web.get("uri")})
    _diag(f"[web_research] '{query[:60]}' → {len(text)} chars, {len(citations)} citations")
    return {"query": query, "text": text, "citations": citations}


def main():
    args = sys.argv[1:]
    if args and args[0] == "--json":
        queries = json.loads(args[1]) if len(args) > 1 else json.loads(sys.stdin.read())
        print(json.dumps([research(str(q)) for q in queries]))
        return
    query = " ".join(args) if args else sys.stdin.read().strip()
    print(json.dumps(research(query)))


if __name__ == "__main__":
    main()
