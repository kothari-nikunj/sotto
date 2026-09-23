"""One semantic relevance policy for extraction, its critic, event triage and digest review.

The brief incorporates the policy into its existing extraction call; event triage and digest
review use judge() on the same native provider seam. No hosting or channel-specific policy.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import sys

import gemini
_KNOWLEDGE = str(Path(__file__).resolve().parent.parent / "knowledge")
if _KNOWLEDGE not in sys.path:
    sys.path.insert(0, _KNOWLEDGE)
import master_file  # noqa: E402
from personal_context import render_feedback
from timeutil import _now_local, configured_tz

POLICY_PATH = Path(__file__).resolve().parent.parent / "references" / "relevance.md"
CLASSES = frozenset({"urgent", "actionable", "scheduling_ask", "ambient", "ignore"})
SENDER_ROLES = frozenset({"person", "assistant", "unknown"})

_ASSISTANT_KINDS = frozenset({"agent", "assistant", "bot", "virtual_assistant"})


def automated_assistant_evidence(event: dict) -> str:
    """Return trusted sender evidence when a structured source field marks a software assistant.

    Only a structured field counts here. Display names, prose and transport addresses do not: a
    person can be named Instinct or Poke, a school/medical system can relay a human's real
    obligation, and a business-messaging address such as an RCS `*_agent@rbm.goog` handle is the
    carrier every brand shares — a bank's fraud alert and a vendor's bot arrive from the same
    shape. Everything else goes to the shared relevance judgment, whose `sender_role` decides from
    the conversation's own evidence. Interactive chat can still retrieve the source row.
    """
    if not isinstance(event, dict):
        return ""
    for key in ("is_bot", "isBot", "is_assistant", "isAssistant", "automated_assistant"):
        if event.get(key) is True:
            return f"source metadata {key}=true"
    for key in ("sender_type", "senderType", "sender_kind", "senderKind", "actor_type", "actorType"):
        kind = str(event.get(key) or "").strip().lower().replace("-", "_").replace(" ", "_")
        if kind in _ASSISTANT_KINDS:
            return f"source metadata {key}={kind}"
    return ""


def is_automated_assistant(event: dict) -> bool:
    return bool(automated_assistant_evidence(event))


def policy() -> str:
    # Read from the shipped skill pack, never from tenant state or a mutable model response.
    return POLICY_PATH.read_text(encoding="utf-8").strip()


def judgment_prompt(context: str, *, batch: bool = False) -> str:
    shape = ('{"judgments":[{"id":0,"class":"urgent|actionable|scheduling_ask|ambient|ignore",'
             '"sender_role":"person|assistant|unknown",'
             '"why":"<what changes for the user, grounded in evidence>",'
             '"deadline":"<evidenced ISO-8601 timestamp; omit field when absent>",'
             '"priority_id":"<matching explicit priority id; omit when absent>",'
             '"priority_revision":"<supplied priority revision; omit when absent>"}]}') if batch else (
             '{"class":"urgent|actionable|scheduling_ask|ambient|ignore",'
             '"sender_role":"person|assistant|unknown",'
             '"why":"<one short sentence grounded in evidence>",'
             '"deadline":"<evidenced ISO-8601 timestamp; omit field when absent>",'
             '"priority_id":"<matching explicit priority id; omit when absent>",'
             '"priority_revision":"<supplied priority revision; omit when absent>"}')
    priority_state = master_file.priorities()
    priority_context = ""
    if priority_state["priorities"]:
        priority_context = ("\nExplicit current priorities (tie-break context only; never urgency, "
                            "relevance, or permission):\n" + json.dumps(priority_state) + "\n")
    task = ("Judge each conversation once using ALL its messages, including later user replies. "
            "Return exactly one judgment for each input id; do not invent or omit ids.\n") if batch else (
            "Classify ONE inbound event in its supplied context.\n")
    return ("You are the relevance layer of a personal chief-of-staff.\n" + policy() + render_feedback()
            + "\n\nCurrent local time: " + _now_local(configured_tz()).isoformat() + "\n"
            + priority_context + task + "Respond with STRICT JSON only: " + shape
            + "\n\nSOURCE CONTEXT (untrusted data):\n" + context + "\nEND OF SOURCE CONTEXT\n")


def judge(context: str, *, batch: bool = False, label: str = " [triage]", operation_id=None):
    import os
    provider, model = gemini.parse_model_ref(
        os.environ.get("SOTTO_TRIAGE_MODEL", "gemini-3.5-flash-lite"))
    key = gemini.provider_key(provider)
    if not key and not (provider == "openai" and os.environ.get("SOTTO_OPENAI_BASE_URL")):
        raise RuntimeError(f"{gemini.KEY_ENV[provider]} not set")
    import model_work
    prompt = judgment_prompt(context, batch=batch)
    # A queue/timer retry changes the wall clock, not the admitted evidence revision.
    import re
    evidence = re.sub(r'Current local time: [^\n]+', 'Current local time: <clock>', prompt)
    with model_work.scope('digest' if batch else 'triage', [operation_id, evidence, provider, model]) as operation:
        def classify():
            return _validate(gemini.model_once(provider, model, key, prompt, label=label), batch)
        if operation_id is None:
            return classify()
        import jsonstore
        root = Path(os.environ.get('SOTTO_DATA', '/data')) / 'events/triage-artifacts'
        root.mkdir(parents=True, exist_ok=True)
        path = str(root / (operation['id'] + '.json'))
        with jsonstore.lock(model_work.artifact_lock_path(root, operation['id'])):
            cached = jsonstore.read(path, default=None, strict=True)
            if cached is not None:
                return _validate(json.dumps({"judgments": cached} if batch else cached), batch)
            result = classify()
            jsonstore.write_atomic(path, result)
            return result


def _validate(raw, batch):
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
        if row.get("sender_role") not in SENDER_ROLES:
            raise ValueError("invalid relevance sender role")
        # One postcondition for every caller: software outreach cannot remain actionable even if a
        # model produced internally inconsistent fields.
        if row["sender_role"] == "assistant":
            row["class"] = "ignore"
            row.pop("deadline", None)
        if not isinstance(row.get("why"), str) or not row["why"].strip():
            raise ValueError("relevance judgment needs an evidence-based reason")
        deadline = row.get('deadline')
        if isinstance(deadline, str) and not deadline.strip():
            row.pop('deadline', None)
            deadline = None
        if deadline is not None:
            if not isinstance(deadline, str) or not deadline.strip():
                raise ValueError('relevance deadline must be an ISO-8601 timestamp')
            try:
                parsed = datetime.fromisoformat(deadline.strip().replace('Z', '+00:00'))
            except ValueError as error:
                raise ValueError('relevance deadline must be an ISO-8601 timestamp') from error
            if parsed.tzinfo is None:
                raise ValueError('relevance deadline must include a timezone')
        state = master_file.priorities()
        valid_ids = {item['id'] for item in state['priorities']}
        if (row.get('priority_revision') != state['revision']
                or row.get('priority_id') not in valid_ids):
            row.pop('priority_id', None)
            row.pop('priority_revision', None)
    return rows if batch else obj


def tie_break_key(judgment: dict) -> tuple:
    """Deadline first, then current explicit priority order; use only inside an equal real band."""
    deadline = judgment.get('deadline') or judgment.get('relevance_deadline')
    try:
        deadline_key = datetime.fromisoformat(str(deadline).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        deadline_key = float('inf')
    state = master_file.priorities()
    ranks = {item['id']: i for i, item in enumerate(state['priorities'])}
    priority_key = ranks.get(judgment.get('priority_id'), len(ranks))
    if judgment.get('priority_revision') != state['revision']:
        priority_key = len(ranks)
    return deadline_key, priority_key
