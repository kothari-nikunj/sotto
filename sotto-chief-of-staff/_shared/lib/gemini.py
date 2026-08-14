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




_DEFAULT_FALLBACK_MODEL = "gemini-3-flash-preview"  # 1M context; priced in metrics.PRICE_TABLE
_PRIMARY_RETRY_BACKOFF_S = 3.0  # single bounded retry of the primary before falling back


def call_gemini(prompt: str, inputs: dict, system: str | None = None, schema: dict | None = None) -> str:
    """Structured Gemini call, returns the model's JSON text. Honors SOTTO_LLM_STUB for tests.
    Optional system/schema (default None = old behavior) become Gemini's systemInstruction and
    generationConfig.responseSchema — see _gemini_once.
    Resilience on 429/5xx/timeout (_is_retryable): one bounded retry of the primary after a short
    backoff, then a fallback call — so a Gemini quota blip (the 429 storm we hit) no longer fails
    the whole brief. The fallback model defaults to gemini-3-flash-preview; SOTTO_FALLBACK_MODEL
    overrides it, and setting it to the EMPTY STRING disables the fallback (the retry still runs).
    SOTTO_FALLBACK_API_KEY optionally supplies a second key — enough on its own to dodge per-project
    quota. The fallback model MUST be 1M-context: the brief prompt runs 100K–140K chars."""
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
    key = os.environ.get("GOOGLE_AI_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_AI_API_KEY not set (or use SOTTO_LLM_STUB for offline)")
    model = os.environ.get("SOTTO_GEMINI_MODEL", "gemini-3.7-flash")
    # SOTTO_FALLBACK_MODEL: unset → default to gemini-3-flash-preview (1M context, priced in
    # metrics.PRICE_TABLE as "the brief's automatic fallback"); set to "" → fallback DISABLED
    # (the explicit off switch); any other value overrides the default.
    fb_env = os.environ.get("SOTTO_FALLBACK_MODEL")
    fb_model = (_DEFAULT_FALLBACK_MODEL if fb_env is None else fb_env).strip()
    fb_key = (os.environ.get("SOTTO_FALLBACK_API_KEY") or "").strip()
    # Only pass the new kwargs when set, so tests/siblings that monkeypatch _gemini_once with the old
    # (model, key, prompt, label="") signature keep working untouched.
    kw = {}
    if system:
        kw["system"] = system
    if schema is not None:
        kw["schema"] = schema
    try:
        return _gemini_once(model, key, prompt, **kw)
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
            return _gemini_once(model, key, prompt, label=" [retry]", **kw)
        except Exception as e2:  # noqa: BLE001
            if (fb_model or fb_key) and _is_retryable(e2):
                _diag(f"[compose_brief] primary {model} failed again ({type(e2).__name__}) — falling "
                      f"back to {fb_model or model}{' (backup key)' if fb_key else ''}")
                return _gemini_once(fb_model or model, fb_key or key, prompt, label=" [fallback]", **kw)
            raise
