#!/usr/bin/env python3
"""
metrics.py — a tiny, dependency-free per-run cost/latency accumulator for the brief pipeline.

Observability that must NEVER block a brief. Every public entry point swallows its own errors and
degrades to a no-op (with at most one diag warning), so a metrics bug can't fail delivery. Stdlib
only; all config is optional-with-defaults env.

Lifecycle (module-level, one accumulator per process — the pipeline is single-threaded per run;
research_attendees fans batches across a ThreadPool, so record() is guarded by a lock):

  start_run()                                   begin a fresh accumulator for this run
  record(phase, wall_s, in_tok, out_tok, model) one LLM call. phase ∈ extraction | critic | revise |
                                                research | fallback
  note_response(model, usageMetadata, wall_s, label)  bridge for _gemini_once (pulls tokens from the
                                                Gemini generateContent usageMetadata block)
  set_phase(phase)                              call_gemini stamps the phase of the NEXT _gemini_once
  summary()                                     totals + per-phase aggregation + an estimated cost
  emit(date, kind, skipped=)                    write ONE [brief-cost] line to compose_brief.log

Cost comes from PRICE_TABLE. An unknown model yields est=None (n/a) — we never guess a price.
"""
from __future__ import annotations

import os
import sys
import threading

# Hoist this module's own dir onto sys.path ONCE at import (guarded), not per call — record() runs on
# ThreadPool workers, so a per-call sys.path.insert in _warn()/emit() would mutate sys.path concurrently.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


# Compatibility view for callers that already supply uncached input and total billed output.
# Provider responses use normalize()/estimate(), including cached input and separate thinking.
from usage_accounting import PRICES  # noqa: E402
PRICE_TABLE = {model: {'in': prices[0], 'out': prices[2]} for model, prices in PRICES.items()}


_lock = threading.Lock()
_run = None            # the active accumulator (list of per-call dicts), or None when no run started
_phase = "extraction"  # the phase the next _gemini_once call belongs to (set by call_gemini)
_warned = False        # emit at most ONE diag warning per process if metrics misbehaves


def _warn(msg: str) -> None:
    """One-shot best-effort warning — a metrics failure is a footnote, never a brief failure."""
    global _warned
    if _warned:
        return
    _warned = True
    line = f"[brief-cost] metrics degraded this run ({msg})"
    try:
        from sotto_log import diag  # _HERE already on sys.path (module-level, thread-safe)
        diag(line)
    except Exception:
        try:
            print(line, file=sys.stderr)
        except Exception:
            pass


def start_run():
    """Begin a fresh accumulator for this run. Always resets (safe to call more than once)."""
    global _run, _phase
    with _lock:
        _run = []
        _phase = "extraction"
    return _run


def set_phase(phase: str) -> None:
    """call_gemini stamps the phase of the NEXT _gemini_once call (extraction/critic/revise)."""
    global _phase
    _phase = phase or "extraction"


def record(phase, wall_secs, prompt_tokens, output_tokens, model, usage=None) -> None:
    """Record one LLM call. Thread-safe (research batches run concurrently). Never raises."""
    try:
        with _lock:
            if _run is None:
                return
            _run.append({
                "phase": phase or "extraction",
                "wall_s": float(wall_secs or 0.0),
                "prompt_tokens": int(prompt_tokens or 0),
                "output_tokens": int(output_tokens or 0),
                "model": model or "",
                "usage": usage,
            })
    except Exception as e:  # noqa: BLE001
        _warn(type(e).__name__)


def note_response(model, usage_metadata, wall_secs, label: str = "") -> None:
    """Bridge for _gemini_once: pull promptTokenCount/candidatesTokenCount from a Gemini
    generateContent usageMetadata block and record under the active phase ('fallback' when the
    backup model fired — signalled by the ' [fallback]' label)."""
    try:
        phase = "fallback" if "fallback" in (label or "") else _phase
        um = usage_metadata or {}
        from usage_accounting import normalize
        counts = normalize(um)
        import model_work
        model_work.note_usage(model, um)
        record(phase, wall_secs, counts['input_tokens'], counts['billed_output_tokens'], model, counts)
    except Exception as e:  # noqa: BLE001
        _warn(type(e).__name__)


def _cost(prompt_tokens: int, output_tokens: int, model: str):
    """USD cost for one priced call, or None when the model isn't in PRICE_TABLE (never a guess)."""
    p = PRICE_TABLE.get(model)
    if not p:
        return None
    return (prompt_tokens / 1_000_000.0) * p["in"] + (output_tokens / 1_000_000.0) * p["out"]


def summary() -> dict:
    """Totals + per-phase aggregation + est cost. est_cost_usd is None (n/a) when there are no calls
    or ANY call's model is unpriced — e.g. an SOTTO_LLM_STUB run records with an empty model."""
    with _lock:
        calls = list(_run or [])
    phases: dict = {}
    total_in = total_out = 0
    total_wall = 0.0
    cost = 0.0
    priced = True
    basis = "exact"
    for c in calls:
        ph = phases.setdefault(c["phase"],
                               {"wall_s": 0.0, "prompt_tokens": 0, "output_tokens": 0, "calls": 0})
        ph["wall_s"] += c["wall_s"]
        ph["prompt_tokens"] += c["prompt_tokens"]
        ph["output_tokens"] += c["output_tokens"]
        ph["calls"] += 1
        total_in += c["prompt_tokens"]
        total_out += c["output_tokens"]
        total_wall += c["wall_s"]
        from usage_accounting import estimate, estimate_range
        cc = (estimate(c['model'], c['usage']) if c.get('usage') is not None
              else _cost(c["prompt_tokens"], c["output_tokens"], c["model"]))
        if cc is None and c.get('usage') is not None:
            # A compatible-chat response carries no cache detail, so the exact figure is unknown;
            # the footer's number is then the uncached upper bound (what the legacy table always
            # assumed) and says so, rather than going blank for the whole run.
            bounds = estimate_range(c['model'], c['usage'])
            if bounds is not None:
                cc, basis = bounds['upper'], "upper_bound"
        if cc is None:
            priced = False
        else:
            cost += cc
    for ph in phases.values():
        ph["wall_s"] = round(ph["wall_s"], 1)
    return {
        "calls": len(calls),
        "total_wall_s": round(total_wall, 1),
        "prompt_tokens": total_in,
        "output_tokens": total_out,
        "est_cost_usd": (round(cost, 4) if (priced and calls) else None),
        "est_cost_basis": (basis if (priced and calls) else None),
        "phases": phases,
    }


# ── emit ─────────────────────────────────────────────────────────────────────────────────────────
_DISPLAY = {"extraction": "extract", "fallback": "fallback", "critic": "critic",
            "revise": "revise", "research": "research"}
_ORDER = ["extraction", "fallback", "critic", "revise", "research"]


def _fmt_k(n) -> str:
    """Token count as a compact 'k' string: 98000→'98k', 9100→'9.1k', 400→'0.4k'."""
    k = (n or 0) / 1000.0
    return f"{round(k)}k" if k >= 10 else f"{k:.1f}k"


def _human_line(date, kind, s: dict, skipped) -> str:
    est = ("$" + format(s["est_cost_usd"], ".3f") + ("≤" if s.get("est_cost_basis") == "upper_bound" else "")
           if s["est_cost_usd"] is not None else "n/a")
    parts = [f"[brief-cost] kind={kind} date={date}",
             f"total={s['total_wall_s']:.1f}s", f"calls={s['calls']}",
             f"in={_fmt_k(s['prompt_tokens'])}", f"out={_fmt_k(s['output_tokens'])}", f"est={est}"]
    for ph in _ORDER:                                  # omit absent phases, keep a stable order
        d = s["phases"].get(ph)
        if d:
            parts.append(f"{_DISPLAY[ph]}={d['wall_s']:.1f}s/"
                         f"{_fmt_k(d['prompt_tokens'])}/{_fmt_k(d['output_tokens'])}")
    if skipped:
        parts.append("skipped=" + ",".join(_DISPLAY.get(p, p) for p in skipped))
    return " ".join(parts)


def emit(date, kind, skipped=None) -> str:
    """Write ONE [brief-cost] line to compose_brief.log (via the shared diag path). Best-effort;
    never raises into the pipeline. Returns the human line (so tests / the caller can assert it)
    or '' on failure.

    There used to be a second output here — one JSON object per run appended to
    logs/brief_metrics.jsonl. Every brief wrote it and nothing ever read it: no dashboard card, no
    reader anywhere in the pack, no roadmap item claiming it. The [brief-cost] line carries the same
    numbers for a human; the JSONL comes back the day a cost card exists to read it (ROADMAP
    batch-later ledger)."""
    try:
        s = summary()
        line = _human_line(date, kind, s, skipped or [])
        try:
            from sotto_log import diag  # _HERE already on sys.path (module-level, thread-safe)
            diag(line)
        except Exception:
            print(line, file=sys.stderr)
        return line
    except Exception as e:  # noqa: BLE001
        _warn(type(e).__name__)
        return ""
