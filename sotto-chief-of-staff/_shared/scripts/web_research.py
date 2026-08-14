#!/usr/bin/env python3
"""
web_research.py — THE search seam. One resolver, two capabilities, three providers.

THE RULE, in one sentence: **for each capability, use the first provider whose key is present —
`web_search`: Exa → Gemini grounding; `deep_research`: Parallel → Exa → Gemini grounding — and a
provider that errors or comes back empty falls through to the next; with none left the caller is
told the search is unavailable, never handed an ungrounded guess.**

Selection is by KEY PRESENCE only. There is no provider knob and there never will be one: the two
new variables are credentials (`EXA_API_KEY`, `PARALLEL_API_KEY`), and a credential is the only kind
of variable a default can't serve.

The two capabilities are the two shapes this tree actually asks for — not an invented taxonomy:
  web_search    — a query in, grounded text + citations out. This module's `research()`; the
                  schedule skill's venue lookups, ad-hoc one-offs.
  deep_research — a prompt + a JSON schema in, a grounded structured object out. That is
                  `research_attendees.py`'s `_grounded_json` (attendee profiles, the recency sweep,
                  the focus company dive) — multi-step research whose answer must land in fields.
Every provider here answers natively in the shape the capability names: Exa `/search` (semantic,
`type:"deep"` + `outputSchema` for the structured shape), Parallel `/v1/tasks/runs` (research task
with `output_schema`), Gemini `generateContent` with the `google_search` tool.

Why bother when Gemini grounding works: because grounding is Gemini's floor, not the best available
search, and because until now a deploy without `GOOGLE_AI_API_KEY` had NO research at all
(docs/MODELS.md §5c). Exa/Parallel close that hole and beat grounding when present.

Usage:
  web_research.py "Peyton Casper Browserbase"   # prints {query, text, citations:[{title,uri}], provider}
  web_research.py --json '["q1","q2"]'          # batch: prints [{query,text,citations,provider}, …]
Env: EXA_API_KEY / PARALLEL_API_KEY / GOOGLE_AI_API_KEY (any one is enough), SOTTO_GEMINI_MODEL
     (default gemini-3.7-flash).
Test: SOTTO_LLM_STUB=/path/to/response.json bypasses the network entirely (returns that file's text).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

MODEL = os.environ.get("SOTTO_GEMINI_MODEL", "gemini-3.7-flash")

# Hoist _shared/lib onto sys.path ONCE at import (guarded), not per _diag() call: deep_research runs
# on research_attendees' ThreadPool workers, so a per-call insert would mutate sys.path concurrently.
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

# ── the resolver ─────────────────────────────────────────────────────────────────────────────────
# One table, one precedence per capability, one presence check. Everything else in this file is a
# provider implementation; nothing else decides who answers.
KEY_ENV = {"exa": "EXA_API_KEY", "parallel": "PARALLEL_API_KEY", "gemini": "GOOGLE_AI_API_KEY"}
CAPABILITIES = {
    "web_search": ("exa", "gemini"),
    "deep_research": ("parallel", "exa", "gemini"),
}

HTTP_TIMEOUT = 60             # a one-off lookup's whole budget (the old grounded-call timeout)
EXA_NUM_RESULTS = 6           # enough sources for a bio; more is noise you pay to read
EXA_TEXT_MAX_CHARS = 2000     # per result — the caller reads this text, it never needs whole pages
PARALLEL_BASE = "https://api.parallel.ai"
PARALLEL_PROCESSOR = "base"   # the cheap end of Parallel's research processors; deep_research is
                              # called inside research_attendees' 60–90s per-batch budget, so a
                              # minutes-long processor (core/pro/ultra) could never answer in time.
PARALLEL_CREATE_TIMEOUT = 20  # creating a run is an ack; the waiting happens on /result


def _diag(msg: str) -> None:
    try:
        from sotto_log import diag  # _LIB already on sys.path (module-level, thread-safe)
        diag(msg)
    except Exception:
        print(msg, file=sys.stderr)


def _key(provider: str) -> str:
    return os.environ.get(KEY_ENV[provider], "").strip()


def provider_chain(capability: str) -> list:
    """The providers that could answer this capability RIGHT NOW, in precedence order. Presence of
    the credential is the whole test — nothing is configured, enabled, or chosen anywhere else."""
    return [p for p in CAPABILITIES[capability] if _key(p)]


def _remaining(deadline: float) -> int:
    """Seconds left of the CALLER's budget. The ladder's budget is per CALL, not per provider: the
    caller (research_attendees enforces 60–90s a batch) sized one number for the whole lookup, so a
    three-rung fall-through has to fit inside it — otherwise the measured cost is the budget times
    the number of rungs. Floored at 1 so a rung that starts late still makes one real attempt
    instead of erroring on a zero timeout."""
    return max(1, int(deadline - time.monotonic()))


def _first_answer(capability: str, attempt) -> tuple:
    """Walk the chain; return (answer, provider) from the first provider that produces one. A
    provider that raises or returns nothing falls through to the next — the same degrade the
    pipeline already applies to a broken source. Nothing left → (None, "")."""
    for name in provider_chain(capability):
        try:
            out = attempt(name)
        except Exception as e:  # noqa: BLE001 — a dead provider is a fallthrough, not a failure
            _diag(f"[web_research] {capability} via {name} failed: "
                  f"{type(e).__name__}: {str(e)[:120]}")
            continue
        if out:
            return out, name
        _diag(f"[web_research] {capability} via {name} returned nothing — trying the next provider")
    return None, ""


# ── wire primitives (stdlib only, like every other gather in this tree) ──────────────────────────

def _post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get(url: str, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def plain_json_schema(schema):
    """Gemini's `responseSchema` is the OpenAPI subset, where `nullable: true` is a keyword; plain
    JSON Schema has no such keyword. Strip it so ONE schema constant can be sent to all three
    providers instead of every schema being written twice."""
    if isinstance(schema, dict):
        return {k: plain_json_schema(v) for k, v in schema.items() if k != "nullable"}
    if isinstance(schema, list):
        return [plain_json_schema(v) for v in schema]
    return schema


# ── Exa (https://api.exa.ai) ─────────────────────────────────────────────────────────────────────
# Verified against exa-labs/openapi-spec (Exa Search API 1.2.0): POST /search, `x-api-key` header,
# `contents.text` for page text, and `type:"deep"` + `outputSchema` for synthesized structured
# output (returned as `output.content`, cited by `output.grounding[].citations[]`).

def _exa_search(query: str, timeout: float) -> dict | None:
    data = _post("https://api.exa.ai/search",
                 {"query": query, "type": "auto", "numResults": EXA_NUM_RESULTS,
                  "contents": {"text": {"maxCharacters": EXA_TEXT_MAX_CHARS}}},
                 {"x-api-key": _key("exa")}, timeout)
    results = [r for r in (data.get("results") or []) if isinstance(r, dict) and r.get("url")]
    if not results:
        return None
    text = "\n\n".join(
        f"## {r.get('title') or r['url']}\n{r['url']}\n{(r.get('text') or '').strip()}"
        for r in results)
    return {"query": query, "text": text,
            "citations": [{"title": r.get("title") or "", "uri": r["url"]} for r in results]}


def _exa_deep(prompt: str, schema: dict, timeout: float) -> dict | None:
    data = _post("https://api.exa.ai/search",
                 {"query": prompt, "type": "deep", "numResults": EXA_NUM_RESULTS,
                  "outputSchema": plain_json_schema(schema)},
                 {"x-api-key": _key("exa")}, timeout)
    content = (data.get("output") or {}).get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return None
    return content if isinstance(content, dict) else None


# ── Parallel (https://api.parallel.ai) ───────────────────────────────────────────────────────────
# Verified against parallel-web/parallel-sdk-python (generated from Parallel's OpenAPI spec):
# POST /v1/tasks/runs {input, processor, task_spec:{output_schema:{type:"json", json_schema}}} →
# {run_id, status}; the result is fetched with GET /v1/tasks/runs/{run_id}/result?timeout=<seconds>,
# which BLOCKS until the run finishes — so the caller's research budget is the poll, and a run that
# outlives it 408s and falls through to the next provider instead of stalling a brief.

def _parallel_task(prompt: str, schema: dict, timeout: float) -> dict | None:
    headers = {"x-api-key": _key("parallel")}
    t0 = time.monotonic()
    run = _post(f"{PARALLEL_BASE}/v1/tasks/runs",
                {"input": prompt, "processor": PARALLEL_PROCESSOR,
                 "task_spec": {"output_schema": {"type": "json",
                                                 "json_schema": plain_json_schema(schema)}}},
                headers, min(PARALLEL_CREATE_TIMEOUT, timeout))
    run_id = run.get("run_id")
    if not run_id:
        return None
    left = max(1, int(timeout - (time.monotonic() - t0)))
    res = _get(f"{PARALLEL_BASE}/v1/tasks/runs/{run_id}/result?timeout={left}", headers, left + 5)
    content = (res.get("output") or {}).get("content")
    return content if isinstance(content, dict) else None


# ── Gemini Search Grounding (the floor: the key we already have) ─────────────────────────────────

def _gemini_search(query: str, timeout: float) -> dict | None:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}"
           f":generateContent?key={_key('gemini')}")
    data = _post(url, {"contents": [{"parts": [{"text": query}]}],
                       "tools": [{"google_search": {}}]}, {}, timeout)
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content", {}).get("parts") or []))
    citations = []
    for ch in (cand.get("groundingMetadata", {}).get("groundingChunks") or []):
        web = ch.get("web") or {}
        if web.get("uri"):
            citations.append({"title": web.get("title") or "", "uri": web.get("uri")})
    return {"query": query, "text": text, "citations": citations} if text else None


# ── the two capabilities ─────────────────────────────────────────────────────────────────────────

_WEB_SEARCH = {"exa": _exa_search, "gemini": _gemini_search}

NO_PROVIDER = ("no search provider connected — set EXA_API_KEY, PARALLEL_API_KEY, or "
               "GOOGLE_AI_API_KEY")


def research(query: str) -> dict:
    """ONE web_search lookup. Returns {query, text, citations:[{title,uri}], provider}. Never raises
    — with no provider left it returns empty text plus `error`, so the caller SAYS the lookup is
    unavailable instead of answering from the model's memory."""
    stub = os.environ.get("SOTTO_LLM_STUB")
    if stub:
        try:
            with open(stub, encoding="utf-8") as f:
                return {"query": query, "text": f.read(), "citations": [], "provider": "stub"}
        except Exception:
            return {"query": query, "text": "", "citations": [], "provider": "stub"}
    # ONE deadline for the whole lookup, not one per rung: HTTP_TIMEOUT is documented as "a one-off
    # lookup's whole budget", and Exa → Gemini would otherwise cost twice that.
    deadline = time.monotonic() + HTTP_TIMEOUT
    out, provider = _first_answer("web_search",
                                  lambda p: _WEB_SEARCH[p](query, _remaining(deadline)))
    if out is None:
        return {"query": query, "text": "", "citations": [], "provider": None,
                "error": NO_PROVIDER if not provider_chain("web_search") else "search failed"}
    out["provider"] = provider
    _diag(f"[web_research] '{query[:60]}' → {provider}: {len(out['text'])} chars, "
          f"{len(out['citations'])} citations")
    return out


def deep_research(prompt: str, schema: dict, timeout: float, gemini) -> tuple:
    """ONE deep_research call: a prompt + a JSON schema in, the parsed object out (or None), plus
    the provider that answered. `gemini` is the caller's own grounded-structured call — the Gemini
    rung lives with `research_attendees`, which owns that call's schema retry and cost metrics, so
    the LADDER stays here as the single writer of the precedence rule.

    `timeout` is the budget for the WHOLE call, shared by every rung: one deadline is fixed here and
    each provider gets what is LEFT of it, so a full three-rung fall-through still fits the per-batch
    budget `research_attendees` enforces instead of costing it once per provider."""
    deadline = time.monotonic() + max(1, int(timeout))
    attempts = {"parallel": lambda: _parallel_task(prompt, schema, _remaining(deadline)),
                "exa": lambda: _exa_deep(prompt, schema, _remaining(deadline)),
                "gemini": lambda: gemini(prompt, schema, _remaining(deadline))}
    return _first_answer("deep_research", lambda p: attempts[p]())


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
