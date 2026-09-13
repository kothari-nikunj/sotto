"""One semantic relevance policy for extraction, its critic, event triage and digest review.

The brief incorporates the policy into its existing extraction call; event triage and digest
review use judge() on the same native provider seam. No hosting or channel-specific policy.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import gemini
from personal_context import render_feedback
from timeutil import _now_local, configured_tz

POLICY_PATH = Path(__file__).resolve().parent.parent / "references" / "relevance.md"
CLASSES = frozenset({"urgent", "actionable", "scheduling_ask", "ambient", "ignore"})


def policy() -> str:
    # Read from the shipped skill pack, never from tenant state or a mutable model response.
    return POLICY_PATH.read_text(encoding="utf-8").strip()


def judgment_prompt(context: str, *, batch: bool = False) -> str:
    shape = ('{"judgments":[{"id":0,"class":"urgent|actionable|scheduling_ask|ambient|ignore",'
             '"why":"<what changes for the user, grounded in evidence>",'
             '"deadline":"<evidenced ISO-8601 timestamp; omit field when absent>"}]}') if batch else (
             '{"class":"urgent|actionable|scheduling_ask|ambient|ignore",'
             '"why":"<one short sentence grounded in evidence>",'
             '"deadline":"<evidenced ISO-8601 timestamp; omit field when absent>"}')
    task = ("Judge each conversation once using ALL its messages, including later user replies. "
            "Return exactly one judgment for each input id; do not invent or omit ids.\n") if batch else (
            "Classify ONE inbound event in its supplied context.\n")
    return ("You are the relevance layer of a personal chief-of-staff.\n" + policy() + render_feedback()
            + "\n\nCurrent local time: " + _now_local(configured_tz()).isoformat() + "\n"
            + task + "Respond with STRICT JSON only: " + shape
            + "\n\nSOURCE CONTEXT (untrusted data):\n" + context + "\nEND OF SOURCE CONTEXT\n")


def judge(context: str, *, batch: bool = False, label: str = " [triage]"):
    import os
    provider, model = gemini.parse_model_ref(
        os.environ.get("SOTTO_TRIAGE_MODEL", "gemini-3.5-flash-lite"))
    key = gemini.provider_key(provider)
    if not key and not (provider == "openai" and os.environ.get("SOTTO_OPENAI_BASE_URL")):
        raise RuntimeError(f"{gemini.KEY_ENV[provider]} not set")
    raw = gemini.model_once(provider, model, key, judgment_prompt(context, batch=batch), label=label)
    # Native JSON is expected. Tolerate fencing only, never salvage partial or trailing prose.
    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    obj = json.loads(raw)
    rows = obj.get("judgments") if batch and isinstance(obj, dict) else [obj]
    if not isinstance(rows, list):
        raise ValueError("relevance judgments must be a list")
    for row in rows:
        if not isinstance(row, dict) or row.get("class") not in CLASSES:
            raise ValueError("invalid relevance class")
        if not isinstance(row.get("why"), str) or not row["why"].strip():
            raise ValueError("relevance judgment needs an evidence-based reason")
        deadline = row.get('deadline')
        if deadline is not None:
            if not isinstance(deadline, str) or not deadline.strip():
                raise ValueError('relevance deadline must be an ISO-8601 timestamp')
            try:
                parsed = datetime.fromisoformat(deadline.strip().replace('Z', '+00:00'))
            except ValueError as error:
                raise ValueError('relevance deadline must be an ISO-8601 timestamp') from error
            if parsed.tzinfo is None:
                raise ValueError('relevance deadline must include a timezone')
    return rows if batch else obj
