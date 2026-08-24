#!/usr/bin/env python3
"""
gemini.py — the direct Gemini REST call + operator diagnostics for the brief pipeline.

Extracted verbatim from compose_brief.py (the 2,400-line monolith split) with ZERO behavior
change. Holds _gemini_once (one structured generateContent call), _is_retryable (429/5xx/timeout
classification) and call_gemini (the SOTTO_LLM_STUB test bypass + the optional
SOTTO_FALLBACK_MODEL / SOTTO_FALLBACK_API_KEY retry). _diag lives here too: the operator-visible
log helper that writes to the /data volume so brief diagnostics survive execute_code's sandbox.
No dependency on any sibling module.

This is the ONE owner of those four names — compose_brief.py and every other caller import them
from here.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def _diag(msg: str) -> None:
    """Diagnostics that must be VISIBLE to the operator. compose_brief runs inside Hermes' execute_code
    sandbox, which captures the script's stdout/stderr and returns it to the AGENT — it does NOT reach
    Railway's container logs. So besides stderr, append to a log file on the /data volume that the
    receiver serves at GET /debug/brief-log. Best-effort; never breaks a brief."""
    print(msg, file=sys.stderr)
    try:
        import datetime as _dt
        logdir = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "logs")
        os.makedirs(logdir, exist_ok=True)
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(os.path.join(logdir, "compose_brief.log"), "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass




def _metrics():
    """Lazy, best-effort handle on the cost/latency accumulator. Kept a soft dependency (imported on
    demand, swallowed on failure) so gemini.py never hard-fails a brief over observability."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import metrics
        return metrics
    except Exception:
        return None


def _gemini_once(model: str, key: str, prompt: str, label: str = "",
                 system: str | None = None, schema: dict | None = None) -> str:
    """One structured Gemini REST call. Raises on HTTP/network error (so the caller can fall back).

    Optional kwargs (default None = the old single-text-part behavior, so existing callers are
    untouched):
      system — sent as Gemini's `systemInstruction` field (the true system/user split). Callers keep
               it BYTE-STABLE across runs so Gemini's IMPLICIT prefix caching applies automatically;
               we deliberately do NOT build explicit cachedContents plumbing (two calls/day against a
               1h TTL would never hit).
      schema — sent as `generationConfig.responseSchema` (Gemini's OpenAPI-subset dialect, same
               approach research_attendees.py already uses) to pin the response contract.
    """
    import time as _time
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    gen: dict = {"response_mime_type": "application/json", "temperature": 0.4}
    if schema is not None:
        gen["responseSchema"] = schema
    body: dict = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen,
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
    t0 = _time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as resp:  # 5-min ceiling
        data = json.loads(resp.read())
    wall = _time.monotonic() - t0
    sys_note = f" (+{len(system)} sys)" if system else ""
    _diag(f"[compose_brief] gemini {model}{label}: {len(prompt)} char prompt{sys_note} → {wall:.1f}s")
    m = _metrics()                                          # cost/latency: swallow any failure
    if m is not None:
        # Guard the CALL itself, not just metrics' internals: a foreign 'metrics' module shadowing via
        # sys.modules could raise here AFTER the billed call succeeded and fail the whole brief.
        try:
            m.note_response(model, data.get("usageMetadata"), wall, label)
        except Exception:
            pass
    # A blocked prompt (promptFeedback.blockReason, no candidates) or a MAX_TOKENS-truncated
    # response (content without parts) is a 200 — a raw chained index dies with an opaque KeyError
    # AFTER the call was billed. Raise a diagnosable RuntimeError instead; RuntimeError is not in
    # _is_retryable's transient set, so call_gemini won't burn the fallback on a content block.
    candidates = data.get("candidates") or []
    if not candidates:
        block = (data.get("promptFeedback") or {}).get("blockReason") or "no candidates"
        raise RuntimeError(f"Gemini {model} returned no candidates (blockReason: {block})")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = parts[0].get("text") if parts else None
    if not isinstance(text, str):
        finish = candidates[0].get("finishReason") or "unknown"
        raise RuntimeError(f"Gemini {model} returned no text (finishReason: {finish})")
    return text




def _is_retryable(err: Exception) -> bool:
    """Quota/transient failures worth a fallback: 429 RESOURCE_EXHAUSTED, 5xx, timeouts, network."""
    import socket
    import urllib.error
    if isinstance(err, urllib.error.HTTPError):
        return err.code == 429 or err.code >= 500
    return isinstance(err, (urllib.error.URLError, socket.timeout, TimeoutError))


# ── The provider seam (docs/plans/model-agnostic-pipeline.md) ─────────────────────────────────────
# One sentence: a model is named "provider/model" and every compose call dispatches on the
# provider, so people bring the family they already pay for — while Gemini stays the opinionated
# default and the gemini-only capabilities (search grounding, url_context, DocSend vision) keep
# reading GOOGLE_AI_API_KEY/SOTTO_GEMINI_MODEL untouched.
PROVIDERS = ("gemini", "openai", "anthropic")
KEY_ENV = {"gemini": "GOOGLE_AI_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
# Known context windows (tokens). Deliberately short: only models we can vouch for. Unknown models
# get a one-line warning and proceed — the user may know something this table doesn't.
CONTEXT_WINDOWS = {
    "gemini-3.7-flash": 1_000_000, "gemini-3-flash-preview": 1_000_000,
    "gemini-3.5-flash-lite": 1_000_000, "gemini-3-pro-preview": 1_000_000,
}
# The brief prompt has hit 170,835 input tokens in production; this floor leaves heavy-day headroom.
BRIEF_CONTEXT_FLOOR = 400_000
ANTHROPIC_MAX_TOKENS = 16_384   # Messages API requires max_tokens; briefs observed ≤ ~8K out


def parse_model_ref(ref: str, default_provider: str = "gemini") -> tuple[str, str]:
    """"openai/gpt-…" → ("openai", "gpt-…"); a bare model name keeps today's meaning (gemini)."""
    ref = (ref or "").strip()
    if "/" in ref:
        prov, model = ref.split("/", 1)
        prov = prov.strip().lower()
        if prov not in PROVIDERS:
            raise RuntimeError(f"unknown model provider {prov!r} — know: {', '.join(PROVIDERS)}")
        return prov, model.strip()
    return default_provider, ref


def provider_key(provider: str) -> str:
    return (os.environ.get(KEY_ENV[provider]) or "").strip()


def _openai_base() -> str:
    """api.openai.com unless overridden — the override is the subscription story: point it at any
    OpenAI-compatible endpoint (a LiteLLM/OpenRouter proxy, a local gateway carrying your Codex or
    Claude subscription auth) and the pipeline rides it. With the override set, OPENAI_API_KEY is
    optional (local proxies often need none)."""
    return (os.environ.get("SOTTO_OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")


def _post_json(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:  # same 5-min ceiling as _gemini_once
        return json.loads(resp.read())


def _schema_system(system: str | None, schema: dict | None) -> str | None:
    """The universal structured-output path for OpenAI-compatible endpoints: json_object mode plus
    the schema stated in the system text. Chosen over response_format json_schema deliberately —
    proxies (LiteLLM, OpenRouter, local gateways) support json_object near-universally, and the
    pipeline already validates every response downstream."""
    if schema is None:
        return system
    line = "Respond with a single JSON object matching this JSON Schema exactly:\n" + json.dumps(schema)
    return f"{system}\n\n{line}" if system else line


def _openai_once(model: str, key: str, prompt: str, label: str = "",
                 system: str | None = None, schema: dict | None = None) -> str:
    import time as _time
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    sys_text = _schema_system(system, schema)
    messages = ([{"role": "system", "content": sys_text}] if sys_text else []) + \
               [{"role": "user", "content": prompt}]
    body: dict = {"model": model, "messages": messages, "temperature": 0.4,
                  "response_format": {"type": "json_object"}}
    t0 = _time.monotonic()
    data = _post_json(f"{_openai_base()}/chat/completions", body, headers)
    wall = _time.monotonic() - t0
    _diag(f"[compose_brief] openai {model}{label}: {len(prompt)} char prompt → {wall:.1f}s")
    m = _metrics()
    if m is not None:
        try:
            u = data.get("usage") or {}
            m.note_response(model, {"promptTokenCount": u.get("prompt_tokens", 0),
                                    "candidatesTokenCount": u.get("completion_tokens", 0)}, wall, label)
        except Exception:  # noqa: BLE001
            pass
    choices = data.get("choices") or []
    text = ((choices[0].get("message") or {}).get("content")) if choices else None
    if not isinstance(text, str) or not text:
        finish = (choices[0].get("finish_reason") if choices else None) or "no choices"
        raise RuntimeError(f"openai {model} returned no text (finish_reason: {finish})")
    return text


def _anthropic_once(model: str, key: str, prompt: str, label: str = "",
                    system: str | None = None, schema: dict | None = None) -> str:
    import time as _time
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               # long-context beta: enables the 1M window on models that support it; harmlessly
               # ignored elsewhere. Without it a heavy brief cannot fit a 200K default window.
               "anthropic-beta": "context-1m-2025-08-07"}
    body: dict = {"model": model, "max_tokens": ANTHROPIC_MAX_TOKENS, "temperature": 0.4,
                  "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    if schema is not None:
        # Native structured output: a forced tool call whose input IS the schema.
        body["tools"] = [{"name": "result", "description": "The structured result.",
                          "input_schema": schema}]
        body["tool_choice"] = {"type": "tool", "name": "result"}
    t0 = _time.monotonic()
    data = _post_json("https://api.anthropic.com/v1/messages", body, headers)
    wall = _time.monotonic() - t0
    _diag(f"[compose_brief] anthropic {model}{label}: {len(prompt)} char prompt → {wall:.1f}s")
    m = _metrics()
    if m is not None:
        try:
            u = data.get("usage") or {}
            m.note_response(model, {"promptTokenCount": u.get("input_tokens", 0),
                                    "candidatesTokenCount": u.get("output_tokens", 0)}, wall, label)
        except Exception:  # noqa: BLE001
            pass
    for block in data.get("content") or []:
        if schema is not None and block.get("type") == "tool_use":
            return json.dumps(block.get("input") or {})
        if schema is None and block.get("type") == "text" and block.get("text"):
            return block["text"]
    raise RuntimeError(f"anthropic {model} returned no usable content "
                       f"(stop_reason: {data.get('stop_reason') or 'unknown'})")


def model_once(provider: str, model: str, key: str, prompt: str, label: str = "",
               system: str | None = None, schema: dict | None = None) -> str:
    """One call on the named provider. Dispatch is a plain if-chain ON PURPOSE: tests monkeypatch
    _gemini_once/_openai_once/_anthropic_once on this module, and a dispatch table frozen at import
    would defeat them."""
    kw: dict = {}
    if system:
        kw["system"] = system
    if schema is not None:
        kw["schema"] = schema
    if provider == "openai":
        return _openai_once(model, key, prompt, label=label, **kw)
    if provider == "anthropic":
        return _anthropic_once(model, key, prompt, label=label, **kw)
    return _gemini_once(model, key, prompt, label=label, **kw)


def _floor_check(provider: str, model: str) -> None:
    win = CONTEXT_WINDOWS.get(model)
    if win is None:
        _diag(f"[compose_brief] {provider}/{model}: unknown context window — proceeding "
              f"(the brief needs ≥{BRIEF_CONTEXT_FLOOR:,} tokens)")
        return
    if win < BRIEF_CONTEXT_FLOOR:
        raise RuntimeError(f"{provider}/{model} has a {win:,}-token window — below the brief's "
                           f"{BRIEF_CONTEXT_FLOOR:,} floor; pick a bigger model")




_DEFAULT_FALLBACK_MODEL = "gemini-3-flash-preview"  # 1M context; priced in metrics.PRICE_TABLE
_PRIMARY_RETRY_BACKOFF_S = 3.0  # single bounded retry of the primary before falling back


def call_gemini(prompt: str, inputs: dict, system: str | None = None, schema: dict | None = None) -> str:
    """Structured compose call, returns the model's JSON text. Honors SOTTO_LLM_STUB for tests.
    The name says gemini for history; SOTTO_BRIEF_MODEL ("provider/model") routes it to gemini
    (default), openai, or anthropic — see the provider seam above. Optional system/schema map to
    each provider's native mechanism (systemInstruction+responseSchema / system+json_object /
    system+forced tool).
    Resilience on 429/5xx/timeout (_is_retryable): one bounded retry of the primary after a short
    backoff, then a SAME-FAMILY fallback call. On gemini the fallback defaults to
    gemini-3-flash-preview; other providers have no default fallback. SOTTO_FALLBACK_MODEL
    overrides ("" disables; cross-family values are refused), SOTTO_FALLBACK_API_KEY optionally
    supplies a second key. The fallback model MUST clear the brief's context floor: the prompt
    runs 100K–140K chars."""
    # Cost/latency: tag the phase of the coming call from the inputs sentinel the critic/revise pass
    # sets (default extraction), so _gemini_once records under the right phase. Best-effort.
    phase = ("critic" if (isinstance(inputs, dict) and inputs.get("_critic"))
             else "revise" if (isinstance(inputs, dict) and inputs.get("_revise"))
             else "extraction")
    try:
        _metrics().set_phase(phase)
    except Exception:  # noqa: BLE001
        pass
    stub = os.environ.get("SOTTO_LLM_STUB")
    if stub:
        import time as _time
        t0 = _time.monotonic()
        with open(stub, encoding="utf-8") as f:
            content = f.read()
        try:                                   # stub: real wall, tokens 0, unpriced model → est n/a
            _metrics().record(phase, _time.monotonic() - t0, 0, 0, "")
        except Exception:  # noqa: BLE001
            pass
        return content
    # SOTTO_BRIEF_MODEL ("provider/model") names the compose model; unset falls through to
    # SOTTO_GEMINI_MODEL, so every existing install behaves exactly as today. Gemini stays the
    # opinionated default — this seam exists so people can bring the family they already pay for.
    ref = (os.environ.get("SOTTO_BRIEF_MODEL") or "").strip() \
        or os.environ.get("SOTTO_GEMINI_MODEL", "gemini-3.7-flash")
    provider, model = parse_model_ref(ref)
    key = provider_key(provider)
    if not key and not (provider == "openai" and os.environ.get("SOTTO_OPENAI_BASE_URL")):
        raise RuntimeError(f"{KEY_ENV[provider]} not set (or use SOTTO_LLM_STUB for offline)")
    _floor_check(provider, model)
    # SOTTO_FALLBACK_MODEL: unset → gemini-3-flash-preview on the gemini provider, NO default on
    # any other (a default that crossed families would silently send the user's data to a provider
    # they never configured — privacy boundary, not tuning); set to "" → fallback DISABLED; any
    # other value overrides, and it MUST stay in the primary's family.
    fb_env = os.environ.get("SOTTO_FALLBACK_MODEL")
    fb_ref = ((_DEFAULT_FALLBACK_MODEL if provider == "gemini" else "")
              if fb_env is None else fb_env).strip()
    fb_model = ""
    if fb_ref:
        fb_provider, fb_model = parse_model_ref(fb_ref, default_provider=provider)
        if fb_provider != provider:
            raise RuntimeError(f"SOTTO_FALLBACK_MODEL {fb_ref!r} crosses model families "
                               f"({provider} → {fb_provider}) — the fallback must stay in the "
                               f"primary's family; a quota blip must never send your data to a "
                               f"provider you didn't configure")
    fb_key = (os.environ.get("SOTTO_FALLBACK_API_KEY") or "").strip()
    try:
        return model_once(provider, model, key, prompt, system=system, schema=schema)
    except Exception as e:  # noqa: BLE001
        if not _is_retryable(e):
            raise
        # One bounded retry of the primary first — a lone 429/503 blip at 6:30am shouldn't
        # immediately abandon the primary model. Short backoff; then the fallback if still down.
        import time as _time
        _diag(f"[compose_brief] primary {model} failed ({type(e).__name__}) — retrying once "
              f"after {_PRIMARY_RETRY_BACKOFF_S:.0f}s")
        _time.sleep(_PRIMARY_RETRY_BACKOFF_S)
        try:
            return model_once(provider, model, key, prompt, label=" [retry]",
                              system=system, schema=schema)
        except Exception as e2:  # noqa: BLE001
            if (fb_model or fb_key) and _is_retryable(e2):
                _diag(f"[compose_brief] primary {model} failed again ({type(e2).__name__}) — falling "
                      f"back to {fb_model or model}{' (backup key)' if fb_key else ''}")
                return model_once(provider, fb_model or model, fb_key or key, prompt,
                                  label=" [fallback]", system=system, schema=schema)
            raise
