#!/usr/bin/env python3
"""
gemini.py — the direct Gemini REST call + operator diagnostics for the brief pipeline.

Extracted verbatim from compose_brief.py (the 2,400-line monolith split) with ZERO behavior
change. Holds _gemini_once (one structured generateContent call), _is_retryable (429/5xx/timeout
classification) and call_gemini (the SOTTO_LLM_STUB test bypass + the optional
SOTTO_FALLBACK_MODEL / SOTTO_FALLBACK_API_KEY retry). _diag lives here too: the operator-visible
log helper that writes to the /data volume so brief diagnostics survive execute_code's sandbox.
No dependency on any sibling module.

compose_brief.py re-exports call_gemini / _diag / _gemini_once / _is_retryable at their old
location for the `import compose_brief as cb` compat surface.
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


def _gemini_once(model: str, key: str, prompt: str, label: str = "") -> str:
    """One structured Gemini REST call. Raises on HTTP/network error (so the caller can fall back)."""
    import time as _time
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.4},
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
    t0 = _time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as resp:  # 5-min ceiling
        data = json.loads(resp.read())
    wall = _time.monotonic() - t0
    _diag(f"[compose_brief] gemini {model}{label}: {len(prompt)} char prompt → {wall:.1f}s")
    m = _metrics()                                          # cost/latency: swallow any failure
    if m is not None:
        # Guard the CALL itself, not just metrics' internals: a foreign 'metrics' module shadowing via
        # sys.modules could raise here AFTER the billed call succeeded and fail the whole brief.
        try:
            m.note_response(model, data.get("usageMetadata"), wall, label)
        except Exception:
            pass
    return data["candidates"][0]["content"]["parts"][0]["text"]




def _is_retryable(err: Exception) -> bool:
    """Quota/transient failures worth a fallback: 429 RESOURCE_EXHAUSTED, 5xx, timeouts, network."""
    import socket
    import urllib.error
    if isinstance(err, urllib.error.HTTPError):
        return err.code == 429 or err.code >= 500
    return isinstance(err, (urllib.error.URLError, socket.timeout, TimeoutError))
