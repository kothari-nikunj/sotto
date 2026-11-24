#!/usr/bin/env python3
"""outbox.py — the durable delivery outbox (ROADMAP § Reliability P0, item 2).

The receiver hands each brief, digest or nudge to this outbox before its first send.
Provider acceptance settles delivery. Post-delivery effects retry five times before
quarantine. Only a handset receipt could establish arrival.

THE ONE THING IT CANNOT PROMISE. A process can die between provider acceptance and recording the
reply. The outbox never replays an ambiguous send: it asks the chat runtime for the dispatch's
receipt until the row's window ends plus UNCONFIRMED_GRACE_SECS, then closes it `failed` ("could not
confirm delivery") and tells the owner once about a scheduled brief. Only a lookup that finds no
dispatch NOT_FOUND_AFTER_SECS after contact proves the runtime never saw it (it journals every
dispatch before provider contact and never re-sends a known id), so only that row is retried.

Before sending a brief, the receiver's brief_gate verifies ownership of today's marker.
Nudges expire at NUDGE_MAX_AGE_MIN. Briefs respect their explicit deadline and local day.
Digests expire at the end of the local day.

MAX_ATTEMPTS is the backstop underneath all three: a full day at the capped backoff.

WHAT IT KEEPS, AND FOR HOW LONG. Message text is removed when a row becomes terminal.
Delivered rows retain provider receipts and replayable effects until those effects settle;
invalidated rows retain only the replacement-enqueue intent until it succeeds. Closed rows are
pruned after RETENTION_SECS, except unfinished effects and terminal rows whose `on_terminal` hook has
not yet reconciled them. The pre-delivery work queue retains composed results for bounded recovery.

Loaded by receiver.py via importlib; the receiver wires HOOKS (late-bound lambdas over ITS globals),
so this module never imports the receiver and stays import-safe on its own. Stdlib only.

Named constants, not knobs (defaults matter — see CLAUDE.md): MAX_ATTEMPTS, MAX_EFFECT_ATTEMPTS, BACKOFF_BASE_SECS,
BACKOFF_MAX_SECS, DRAIN_INTERVAL_SECS, NUDGE_MAX_AGE_MIN, RETENTION_SECS, UNCONFIRMED_GRACE_SECS,
NOT_FOUND_AFTER_SECS. The only env var is
SOTTO_OUTBOX=0, which turns the retry heartbeat off (the enqueue/ack path stays, so nothing is ever
sent unrecorded).
"""
from __future__ import annotations

import json
import hashlib
import os
import sys
import threading
import time
import fcntl
import re
from functools import lru_cache

# The tree's ONE content-id scheme (keys.py); the idempotency key is that scheme applied to a message.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from keys import queue_key as _content_id  # noqa: E402
from visual_delivery import delivery_detail, artifact_id  # noqa: E402

def _unwired(name):
    def _raise(*_a, **_k):
        raise RuntimeError(f"outbox.HOOKS[{name!r}] is unwired — load this module from receiver.py")
    return _raise


HOOKS = {
    "data_root": lambda: os.environ.get("SOTTO_DATA", "/data"),
    # THE locked read-modify-write for this image (connectors.json_transaction). No sane default:
    # an unlocked outbox is an outbox that double-sends.
    "json_transaction": _unwired("json_transaction"),
    # (body, target, dispatch_id, attempt) -> (ok, detail, receipt). receiver._send_with_identity —
    # the ONE call that touches the channel. The outbox's durable key and charged attempt number
    # are the chat runtime's dispatch identity.
    "send": _unwired("send"),
    "send_gallery": _unwired("send_gallery"),
    "gallery_receipt": lambda key: None,
    "record": lambda *a, **k: None,             # receiver._record_delivery — the receipt line
    "on_delivered": lambda payload: None,       # receiver: finalize the run's chase/handoff effects
    "on_not_a_brief": lambda payload: None,     # receiver: a brief-kind row with no composed brief
    "local_today": lambda: time.strftime("%Y-%m-%d"),   # ONE tz resolution per process
    # (label, day, run_id) -> "send" | "superseded": THE deliver-once gate, asked right before the
    # channel. receiver._brief_delivery_gate owns the marker's path and which labels have one.
    "brief_gate": _unwired("brief_gate"),
    "valid": lambda payload: True,
    "refresh_brief_offer": lambda payload: payload,
    "delivery_ready": lambda payload: True,
    "on_invalid": lambda payload: None,
    "on_terminal": lambda row: True,  # persist producer outcome before receipt retention ends
    # A routine-only live authority check. Unwired by default so a staged or
    # recovered routine cannot contact the channel merely by carrying a digest.
    "routine_authorized": lambda payload: False,
    # Receiver-owned current source + channel observation. Failure affects only
    # future retained-text proof, never whether the original message is sent.
    "retained_proof_current": lambda proof: False,
    "handoff_context": lambda label: None,  # receiver: current {source_revision, route_digest} or None
}


@lru_cache(maxsize=1)
def delivery_proof():
    return load_shared_lib('protected_brief_detail', anchor=__file__)


def _retained_candidate(payload):
    return delivery_proof().retained_candidate(payload)

def _retained_current(proof):
    try:
        return proof is not None and HOOKS['retained_proof_current'](proof) is True
    except Exception:
        return False


KIND_BRIEF = "brief"
KIND_DIGEST = "digest"
KIND_NUDGE = "nudge"

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
# Another run got today's brief first: terminal on the first attempt; not about time or the channel.
STATUS_SUPERSEDED = "superseded"

TERMINAL = (STATUS_DELIVERED, STATUS_FAILED, STATUS_EXPIRED, STATUS_SUPERSEDED)

GATE_SEND = "send"
GATE_SUPERSEDED = "superseded"
GATE_NOT_A_BRIEF = "not_a_brief"     # a brief-kind body with no composed brief behind it
GATE_RETRY = "retry"                 # the marker's lock is held elsewhere: ask again next pass
NOT_A_BRIEF_DETAIL = ("no composed brief on the volume for today — the run never ran the composer, "
                      "so this text is not the brief and does not claim the day")
SUPERSEDED_DETAIL = ("today's brief was already delivered by the other lane — this copy was "
                     "not sent")

BACKOFF_BASE_SECS = 60        # first retry a minute later: a gateway restart is the common outage
BACKOFF_MAX_SECS = 900        # …doubling, capped at the */15 cadence every other heartbeat uses
DRAIN_INTERVAL_SECS = 60      # its own beat, not the valve's: a brief must not wait 15 min to retry
# The backstop, not the rule: expiry ends a row; 96 capped attempts (a day) stops a clock-jumped one.
MAX_ATTEMPTS = 96
MAX_EFFECT_ATTEMPTS = 5       # accepted messages never resend; broken effects quarantine after five tries
UNCONFIRMED_GRACE_SECS = 30 * 60   # an unconfirmed send waits this long past its window for a late receipt
NOT_FOUND_AFTER_SECS = 120         # a dispatch the runtime still has not seen this long after contact never left
LATE_RECEIPT_SECS = 24 * 3600      # an unconfirmed close still records a provider receipt this long, never resends
PROVIDER_MESSAGE_ID_MAX = 512
UNCONFIRMED_DETAIL = 'could not confirm delivery: no receipt by the end of its window'
# Owner status notices wait through quiet hours/snooze (receiver gate), so they live 24 h, not a nudge's age.
OWNER_STATUS_PREFIXES = ('owner-reminder-status:', 'owner-reminder-orphaned:', 'delivery-unconfirmed:')
HANDOFF_CONTEXT_PREFIXES = ('owner-reminder-orphaned:', 'delivery-unconfirmed:', 'request-feedback:', 'status:no-sources')
from shared_lib import load_shared_lib  # noqa: E402
TASK_CONTEXT = load_shared_lib('delivered_tasks', anchor=__file__)
_brief_body_proof = load_shared_lib('accepted_briefs', anchor=__file__).body_proof
NUDGE_MAX_AGE_MIN = load_shared_lib('funnel_policy', anchor=__file__).VALVE_MAX_AGE_MIN
RETENTION_SECS = 7 * 24 * 3600   # terminal rows linger a week so "what failed?" has an answer

_DRAIN_LOCK = threading.Lock()   # one drain at a time, whatever calls it
_INFLIGHT: set = set()           # ids being sent right now — the check-through-accept claim
_INFLIGHT_LOCK = threading.Lock()
_EFFECT_LOCK_STATE = threading.local()


def _root() -> str:
    """The volume, through the hook — the same spelling calcache and the dashboard use, so the
    shared-file-map guard in tests/test_docs_drift.py can read this module's paths like the rest."""
    return HOOKS["data_root"]()


def path() -> str:
    """$SOTTO_DATA/events/outbox.json, atomically replaced under the connector transaction lock."""
    return os.path.join(_root(), "events", "outbox.json")


def message_key(label: str, body: str) -> str:
    """The idempotency key: what makes two enqueues the SAME message. Same label, same words, same
    key — so a retried lane, a re-fired trigger or a replayed drain adds nothing. It is
    `keys.queue_key` — the tree's existing content-id — over `label` and the words, not a second
    hashing convention."""
    return _content_id(f"{label}\n{body}")


def routine_payload_digest(payload: dict) -> str:
    """Full SHA-256 binding for an opted-in routine's immutable handoff.

    This does not replace the existing content-id or change generic duplicate
    run behavior. The digest is retained without the body after terminal scrub.
    """
    if not isinstance(payload, dict):
        raise ValueError('invalid routine outbox payload')
    frozen = {key: value for key, value in payload.items()
              if key != 'routine_handoff_digest'}
    try:
        encoded = json.dumps(frozen, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError('invalid routine outbox payload') from error
    return hashlib.sha256(b'sotto-routine-outbox-v1\0' + encoded).hexdigest()


def kind_for(label: str) -> str:
    """Which expiry a message lives under, from the label the seam already carries: a label naming
    the digest is a digest, one naming a brief or the weekly pulse is a brief, everything else is a
    nudge (events, the proactive lane, the post-meeting tap, the release valve)."""
    text = (label or "").lower()
    if "digest" in text:
        return KIND_DIGEST
    if "brief" in text or "pulse" in text:
        return KIND_BRIEF
    return KIND_NUDGE


def backoff_secs(attempts: int) -> int:
    """Doubling from BACKOFF_BASE_SECS, capped at BACKOFF_MAX_SECS — one sentence, no schedule
    table: wait a minute, then two, then four, and never longer than a quarter of an hour."""
    return min(BACKOFF_BASE_SECS * (2 ** max(0, attempts - 1)), BACKOFF_MAX_SECS)


# ── The file ─────────────────────────────────────────────────────────────────────────────────────

def _rows(doc) -> list:
    rows = doc.get("rows")
    if not isinstance(rows, list):
        rows = []
        doc["rows"] = rows
    return [r for r in rows if isinstance(r, dict)]


def _prune(rows: list, now: float) -> list:
    """Terminal rows older than RETENTION_SECS leave; pending rows never do (only a terminal
    transition may end a row's life — that is the whole promise)."""
    return [r for r in rows
            if r.get("status") not in TERMINAL or r.get("effects_pending")
            or not _reconcile_terminal(r)
            or (now - float(r.get("finished_at") or r.get("created_at") or 0)) <= RETENTION_SECS]


def _reconcile_terminal(row):
    if row.get('terminal_reconciled'):
        return True
    try:
        if HOOKS['on_terminal'](dict(row)) is False:
            return False
    except Exception:
        return False  # retain the outcome until its producer can durably acknowledge it
    row['terminal_reconciled'] = True
    return True


def reconcile_terminal():
    """Recover producer state even after a crash between terminal delivery and its effect."""
    with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
        for row in _rows(doc):
            if row.get('status') in TERMINAL and not row.get('effects_pending'):
                _reconcile_terminal(row)


def _find(rows: list, key: str):
    return next((r for r in rows if r.get("id") == key), None)


def _close(row: dict) -> dict:
    """Take the row's payload OUT of the file as it goes terminal, and hand it back to the caller.

    The outbox holds the words of a message only while it might still have to send them. A row that
    landed, gave up, or aged out keeps its id, kind, attempts and reason — everything the counts and
    the Record need — and nothing a person said. Storing a delivered brief's text for a week would
    be exactly the situational keeping the standing bars forbid."""
    payload = dict(row.get("payload") or {})
    row.pop('offer_route_at_contact', None)
    row.setdefault("finished_at", time.time())
    row["payload"] = {"label": payload.get("label") or ""}
    if payload.get('prep_meeting_start') is not None:
        row['payload']['prep_meeting_start'] = payload['prep_meeting_start']
    return payload


def _quarantine(row: dict) -> dict:
    """End a row whose post-delivery effects gave up, and hand its payload to the caller.

    The message itself already went out; what stays on file for RETENTION_SECS is only what a human
    needs to diagnose the quarantine — the label, the reason, the run id. The effects the row was
    carrying are per-source ADDRESSING (handle, chat id, phone, email, thread id), and a row that
    will never be replayed has no more use for them than a delivered one does, so it closes on the
    same terms the applied path closes on."""
    row["effects_pending"] = False
    row["effects_status"] = "quarantined"
    row["effects_error"] = "post-delivery effect failed permanently"
    row["finished_at"] = time.time()    # retention on a quarantine ages from the quarantine
    payload = _close(row)
    if payload.get("run_id"):
        row["payload"]["run_id"] = payload["run_id"]
    return payload


# ── Expiry ───────────────────────────────────────────────────────────────────────────────────────

def _expiry(row: dict, now: float, today: str):
    """(terminal status, one-sentence reason) when this row has aged out, else None."""
    payload = row.get('payload') or {}
    valid_until = payload.get('valid_until')
    if valid_until is not None and now >= float(valid_until):
        return STATUS_EXPIRED, 'the useful delivery window has ended'
    if str(payload.get('label', '')).startswith(OWNER_STATUS_PREFIXES) and valid_until is not None:
        return None
    kind = row.get("kind")
    if kind == KIND_NUDGE:
        age = now - float(row.get("created_at") or 0)
        if age > NUDGE_MAX_AGE_MIN * 60:
            return STATUS_EXPIRED, (f"expired — a nudge older than {NUDGE_MAX_AGE_MIN} min is past "
                                    "the window the funnel would promote it in")
        return None
    if today and row.get("day") and row["day"] != today:
        if kind == KIND_BRIEF:
            return STATUS_FAILED, f"failed — {row['day']} ended and this brief never reached you"
        return STATUS_EXPIRED, (f"expired — {row['day']} ended before the next digest window; "
                                "tomorrow's digest supersedes it")
    return None


# ── Enqueue → attempt → ack ──────────────────────────────────────────────────────────────────────

def _enqueue(key: str, kind: str, payload: dict, now: float, today: str) -> str:
    """Write the row BEFORE anything is sent. Returns the row's status. A key already on file is a
    no-op — a duplicate enqueue must never add a second copy of the same message."""
    with HOOKS["json_transaction"](path(), default={"rows": []}) as doc:
        rows = _prune(_rows(doc), now)
        row = _find(rows, key)
        routine_digest = payload.get('routine_handoff_digest')
        if routine_digest is not None:
            if (not isinstance(routine_digest, str) or len(routine_digest) != 64 or
                    routine_digest != routine_payload_digest(payload)):
                raise RuntimeError('routine outbox handoff digest is invalid')
        if row is None:
            row = _new_row(key, kind, payload, now, today)
            if routine_digest is not None:
                row['routine_handoff_digest'] = routine_digest
            rows.append(row)
        elif routine_digest is not None and row.get('routine_handoff_digest') != routine_digest:
            raise RuntimeError('routine outbox handoff conflicts with prior row')
        doc["rows"] = rows
        return str(row.get("status") or STATUS_PENDING)


def feedback_skill(label: str) -> str | None:
    """Exact owner-requested skill labels eligible for route-bound terminal notices."""
    match = re.fullmatch(r'skill:[a-f0-9]{64}:(meeting-prep|ask-research|followup|relationship-pulse|proactive)', label)
    if match:
        return match[1]
    if re.fullmatch(r'meeting-prep:(?:[a-f0-9]{32}|[a-f0-9]{64})', label):
        return 'meeting-prep'
    return None


def scheduled_pulse(label):
    return label in ('cron:sotto-relationship-pulse', 'run-now:sotto-relationship-pulse')


def requires_source_scope(label):
    return bool(scheduled_pulse(label) or label == 'onboarding:history-discovery:sotto-welcome-brief'
                or label.startswith('meeting-prep:') or feedback_skill(label))


def enqueue_work_feedback(job: dict, *, enqueue: bool = True) -> bool | str:
    """True clears feedback, False waits, and a dry run returns 'notice' when one is due.
    Unknown acceptance waits ten minutes or until two drains before a known meeting.
    """
    if job.get('status') not in ('done', 'failed', 'expired'):
        return False
    meta = job['feedback']
    now, today = time.time(), str(HOOKS['local_today']() or '')
    key = message_key('request-feedback', job['id'])
    with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
        rows = _rows(doc)
        doc['rows'] = rows
        if _find(rows, key) is not None:
            return True
        original = _find(rows, job['id'])
        skill = feedback_skill(meta['label'])
        # A completed skill with no original row produced nothing to deliver. That is success, not
        # failure: an urgent scan stays silent by design; any other requested skill says so plainly.
        quiet_done = bool(skill and job['status'] == 'done' and original is None)
        if quiet_done and skill == 'proactive':
            return True
        uncertain = False
        meeting_start = meta.get('prep_meeting_start') if skill == 'meeting-prep' else None
        if original is not None:
            if original.get('status') == STATUS_DELIVERED or original.get('acceptance') == 'accepted':
                return True
            uncertain = bool(original.get('acceptance_uncertain') or original.get('acceptance') in (
                'unknown', 'in_flight'))
            if original.get('status') == STATUS_PENDING and not uncertain:
                return False  # Uncontacted/retryable delivery still owns the original result.
            stamp = float(original.get('finished_at') or original.get('created_at') or now)
            if meeting_start is None and skill == 'meeting-prep':
                meeting_start = (original.get('payload') or {}).get('prep_meeting_start')
            if uncertain:
                stamp = float(original.get('created_at') or now) + 600
                if meeting_start is not None:
                    stamp = min(stamp, meeting_start - 2 * DRAIN_INTERVAL_SECS)
        else:
            stamp = float(job.get('finished') or now)
        notice_until = stamp + 4 * 3600
        if meeting_start is not None:
            notice_until = min(notice_until, meeting_start)
        if now < stamp:
            return False
        if now >= notice_until:
            return True
        descriptions = {'meeting-prep': 'meeting prep', 'ask-research': 'research',
                        'followup': 'follow-up', 'relationship-pulse': 'relationship pulse',
                        'proactive': 'urgent check'}
        description = (descriptions[skill] if skill else
                       ('morning' if meta['label'].endswith('sotto-morning-brief') else 'evening') + ' brief')
        if uncertain:
            body = (f"I couldn't confirm whether your requested {description} arrived. "
                    "Please check the conversation before requesting another copy.")
        elif quiet_done:
            body = f"I finished your requested {description}, but there was nothing to send."
        elif skill:
            body = (f"I couldn't finish your requested {description}. "
                    "I won't keep retrying this request.")
        else:
            body = (f"I couldn't finish your requested {description}. I won't keep retrying this request. "
                    "Please ask me for it again when you're ready.")
        if not enqueue:
            return 'notice'
        payload = {'label': 'request-feedback:' + job['id'], 'body': body, 'target': meta['target'],
                   'run_id': key, 'effects': [], 'valid_until': notice_until,
                   'feedback_for': job['id'], 'feedback_owner': meta['owner'],
                   'feedback_chat_id': meta['chat_id']}
        if meeting_start is not None:
            payload['prep_meeting_start'] = meeting_start
        if skill:
            payload['feedback_route_digest'] = meta.get('route_digest')
        rows.append(_new_row(key, KIND_NUDGE, payload, now, today))
    return True


def _new_row(key, kind, payload, now, today):
    return {'id': key, 'kind': kind, 'created_at': now, 'day': today, 'attempts': 0, 'next_at': now,
            'last_error': '', 'status': STATUS_PENDING, 'payload': payload,
            'acceptance': 'not_attempted', 'acceptance_uncertain': False}


def unconfirmed_notice_body(edition):
    return (f"I couldn't confirm your {edition} brief was delivered. "
            "If you don't see it, ask me for your brief and I'll send it now.")


_LATE = ('effects', 'run_id', 'coverage_until', 'decision_ids', 'offer_route_at_contact', 'prep_meeting_start')


def _awaiting(row):
    """Pending, or closed unconfirmed while a late receipt may still be recorded."""
    return row.get('status') == STATUS_PENDING or time.time() < float(row.get('late_until') or 0)


def _unconfirmed_close(rows, row, now, today):
    """Close an ambiguous send as failed; a scheduled brief also queues one content-free notice.
    Requested work already has its own notice (`enqueue_work_feedback`)."""
    row['status'], row['last_error'] = STATUS_FAILED, UNCONFIRMED_DETAIL
    route, payload = row.get('offer_route_at_contact'), _close(row)
    if row.get('routine_handoff_digest') is None:  # never the words, only what finishes a late delivery
        row['payload'].update({k: v for k, v in {**payload, 'offer_route_at_contact': route}.items() if v and k in _LATE})
        row['late_until'] = now + LATE_RECEIPT_SECS
    edition = re.fullmatch(r'(?!requested:).*:sotto-(morning|evening)-brief', str(payload.get('label') or ''))
    if edition and payload.get('target'):
        key = message_key('delivery-unconfirmed', row['id'])
        rows.append(_new_row(key, KIND_NUDGE, {
            'label': 'delivery-unconfirmed:' + row['id'], 'body': unconfirmed_notice_body(edition[1]),
            'target': payload['target'], 'run_id': key, 'effects': [], 'valid_until': now + 24 * 3600}, now, today))
    return ('aged', payload, (STATUS_FAILED, UNCONFIRMED_DETAIL))
def _suppress_confirmed_feedback(rows, row):
    key = (row.get('payload') or {}).get('feedback_for')
    original = _find(rows, key) if key else None
    if original and (original.get('status') == STATUS_DELIVERED or original.get('acceptance') == 'accepted'):
        _supersede_row(row, 'requested delivery confirmed before feedback send', False)
        return True
    return False


def _claim(key: str, now: float, today: str):
    """Charge before sending so a crash preserves backoff; hold ambiguous sends for reconciliation."""
    with HOOKS["json_transaction"](path(), default={"rows": []}) as doc:
        rows = _rows(doc)
        doc["rows"] = rows
        row = _find(rows, key)
        if row is None or row.get("status") != STATUS_PENDING:
            return None
        if _suppress_confirmed_feedback(rows, row):
            return None
        # Once a send crossed the provider boundary, relevance expiry cannot prove it was not
        # accepted. Keep its replay identity and poll only on the existing capped backoff.
        if row.get('acceptance') in ('unknown', 'in_flight'):
            if now < float(row.get('next_at') or 0):
                return None
            # Owner reminders keep their receiver-owned reconciliation (`_owner_timer_terminal`).
            if not str((row.get('payload') or {}).get('label', '')).startswith('owner-reminder') and _expiry(row, now, today):
                if now >= row.setdefault('window_ended_at', now) + UNCONFIRMED_GRACE_SECS:
                    return _unconfirmed_close(rows, row, now, today)
            row['attempts'] = min(int(row.get('attempts') or 0) + 1, MAX_ATTEMPTS)
            row['next_at'] = now + backoff_secs(row['attempts'])
            return ('held', {}, (row['attempts'], str(row.get('day') or '')))
        aged = _expiry(row, now, today)
        if aged:
            row["status"], row["last_error"] = aged[0], aged[1]
            return ("aged", _close(row), aged)
        if now < max(float(row.get('next_at') or 0), float((row.get('payload') or {}).get('not_before') or 0)):
            return None
        row["attempts"] = int(row.get("attempts") or 0) + 1
        row["next_at"] = now + backoff_secs(row["attempts"])
        return ("send", dict(row.get("payload") or {}),
                (row["attempts"], str(row.get("day") or "")))


def _supersede(key, detail, recompose=False):
    with HOOKS["json_transaction"](path(), default={"rows": []}) as doc:
        if (row := _find(_rows(doc), key)) and row.get("status") == STATUS_PENDING:
            return _supersede_row(row, detail, recompose)
def _keep_routine(row, payload):
    """A closed routine handoff keeps only the identity its handoff ledger reconciles against."""
    if row.get('routine_handoff_digest') is not None:
        row['payload'].update(target=payload.get('target'), routine_handoff_digest=row['routine_handoff_digest'],
                              **{f: payload[f] for f in ('routine_source_revision', 'routine_route_digest') if f in payload})


def _supersede_row(row, detail, recompose):
    row["status"], row["last_error"] = STATUS_SUPERSEDED, detail[:300]
    payload = _close(row)
    if recompose:
        row.update(effects_pending=True, effect_phase='invalidate', effects_next_at=0)
        row['payload'] = {k: v for k, v in payload.items() if k in ('label', 'valid_until')}
        _keep_routine(row, payload)
        row['payload'].update(run_id=payload.get('run_id') or row['id'], day=row.get('day'),
                              acceptance=row.get('acceptance', 'unknown'),
                              acceptance_uncertain=bool(row.get('acceptance_uncertain')))
    return payload


def invalidated_claim(run_id, day, label):
    """Authoritative evidence for releasing an invalidated brief's unaccepted reservation."""
    with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
        row = _find(_rows(doc), run_id)
        return bool(row and row.get('status') == STATUS_SUPERSEDED and row.get('day') == day
                    and row.get('effect_phase') == 'invalidate' and definitely_unsent(row)
                    and (row.get('payload') or {}).get('label') == label)


def definitely_unsent(row) -> bool:
    """The one proof a closed row never reached the provider; a row with no acceptance field is none."""
    return (row.get('status') in TERMINAL and row.get('status') != STATUS_DELIVERED and not row.get(
        'acceptance_uncertain') and row.get('acceptance') in ('not_attempted', 'rejected'))

def _begin_send(key, retained_at_contact=False, offer_route=None, replacement=None):
    with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
        rows = _rows(doc)
        row = _find(rows, key)
        if not row or row.get('status') != STATUS_PENDING or _suppress_confirmed_feedback(rows, row):
            return False
        if row.get('acceptance') in ('unknown', 'in_flight'):
            return False
        payload = row.get('payload') or {}
        if (payload.get('feedback_for') or feedback_skill(payload.get('label') or '') == 'meeting-prep') and _expiry(row, time.time(), row.get('day') or ''):
            return False  # Validation crossed the deadline; the next claim records expiry.
        effects = payload.get('effects') or []
        if any(e.get('kind') == 'closure_notice' for e in effects) and load_shared_lib(
                'signal_closes', anchor=__file__).send_reserved(effects, rows, key):
            return _supersede_row(row, 'closure notice has another delivery receipt', recompose=True)
        if replacement is not None:
            if row.get('acceptance') != 'not_attempted' or 'contacted_at' in row:
                return False
            row['payload'] = replacement
        row['acceptance_uncertain'] = 'acceptance' not in row or row.get('acceptance_uncertain', False)
        row['acceptance'], row['contacted_at'] = 'in_flight', time.time()
        row['retained_proof_at_contact'] = retained_at_contact is True
        if offer_route:
            row['offer_route_at_contact'] = dict(offer_route)
        else:
            row.pop('offer_route_at_contact', None)
        return True


def _settle(key: str, ok: bool, detail: str, attempts: int, receipt=None, *, retained_now=False):
    """Settle an awaiting row once under the outbox lock."""
    with HOOKS["json_transaction"](path(), default={"rows": []}) as doc:
        rows = _rows(doc)
        doc["rows"] = rows
        row = _find(rows, key)
        if row is None or not _awaiting(row) or row.get('status') != STATUS_PENDING and not ok:
            return None
        if ok:
            late = bool(row.pop('late_until', None))
            notice = _find(rows, message_key('delivery-unconfirmed', key)) if late else None
            if notice and notice.get('status') == STATUS_PENDING and notice.get('acceptance', 'not_attempted') == 'not_attempted':
                _supersede_row(notice, 'the brief was confirmed delivered after all', False)
            row['acceptance'], row['acceptance_uncertain'] = 'accepted', False
            row["status"], row["last_error"] = STATUS_DELIVERED, ""
            offer_route = row.get('offer_route_at_contact') or (row.get('payload') or {}).get('offer_route_at_contact')
            payload = _close(row)
            row['receipt'] = {**(receipt or {}), 'accepted_at': time.time(), **({'late': True} if late else {}),
                              **delivery_proof().accepted_notification(payload, receipt, row.get('retained_proof_at_contact'), retained_now),
                              **_brief_body_proof(payload),
                              **TASK_CONTEXT.accepted(payload, receipt, row.get('retained_proof_at_contact') and retained_now)}
            visual_id = artifact_id(payload.get('presentation'))
            text_artifact = payload.get('retained_text_artifact')
            text_id = text_artifact.get('id') if isinstance(text_artifact, dict) else None
            if visual_id or (isinstance(text_id, str) and re.fullmatch(r'[a-f0-9]{24}', text_id)):
                row['receipt']['visual_artifact_id' if visual_id else 'text_artifact_id'] = visual_id or text_id
                row['receipt']['target_hash'] = hashlib.sha256(str(payload.get('target') or '').encode()).hexdigest()
                proof = _retained_candidate(payload)
                if (proof is not None and row.get('retained_proof_at_contact') is True
                        and retained_now is True and isinstance(receipt, dict)
                        and type(receipt.get('route_proof_version')) is int
                        and receipt['route_proof_version'] == 1
                        and receipt.get('route_digest') == proof['route_digest']
                        and receipt.get('payload_hash') == proof['gallery_payload_sha256']):
                    row['receipt']['retained_text_proof'] = proof
            # Drop message text immediately, retain only replayable post-acceptance effects.
            row['payload'] = {k: v for k, v in payload.items() if k in
                              ('label', 'effects', 'run_id', 'coverage_until', 'decision_ids')}
            _keep_routine(row, payload)
            if offer_route:
                row['payload']['offer_route_at_contact'] = offer_route
            row['payload']['receipt'] = row['receipt']
            row['effects_pending'] = True
            row['effects_next_at'] = 0
            return ('delivered', payload)
        acceptance = (receipt or {}).get('acceptance', 'unknown')
        row['acceptance'] = acceptance if acceptance in ('not_attempted', 'rejected') else 'unknown'
        if row['acceptance'] == 'unknown':
            row['acceptance_uncertain'] = True
        row["last_error"] = detail[:300]
        # Attempt exhaustion cannot turn an ambiguous provider commit into a definitive
        # failure. Keep the reconciliation identity; `_claim` will hold it without resending.
        if row['acceptance'] != 'unknown' and attempts >= MAX_ATTEMPTS:
            row["status"], payload = STATUS_FAILED, _close(row)
            row['payload'].update({'run_id': payload['run_id']} if payload.get('run_id') else {})  # releases its marker
            return ("gave_up", payload)
        return ("retry", dict(row.get("payload") or {}))


def _definitive_receipt_failure(key: str, acceptance: str) -> None:
    """A sidecar receipt can prove a prior operation stopped before provider dispatch."""
    if acceptance not in ('not_attempted', 'rejected'):
        return
    with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
        row = _find(_rows(doc), key)
        if row and _awaiting(row):
            row['acceptance'] = acceptance
            row['acceptance_uncertain'] = False
            if row.pop('late_until', None):
                row.pop('terminal_reconciled', None)  # on_terminal sees the definite answer


def _reconcile_ambiguous_send(key: str) -> bool:
    """Record a provider acceptance before expiry/validity can suppress future delivery.

    This never sends. Only a bounded provider message id is acceptance evidence; every missing,
    malformed or unresolved result leaves the existing outbox state authoritative.
    """
    with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
        row = _find(_rows(doc), key)
        if (not row or not _awaiting(row)
                or row.get('acceptance') not in ('unknown', 'in_flight')):
            return False
        if time.time() < float(row.get('next_at') or 0):
            return False
        closed = row.get('status') != STATUS_PENDING  # late: only an explicit no-send reopens it
        if closed:
            row['next_at'] = time.time() + BACKOFF_MAX_SECS
        attempts = int(row.get('attempts') or 0)
        contacted = float(row.get('contacted_at') or row.get('created_at') or 0)
    try:
        late = HOOKS['gallery_receipt'](key)
    except Exception as e:  # lookup failure is never permission to replay an ambiguous gallery
        print(f"[sotto] gallery receipt lookup failed ({type(e).__name__})", flush=True)
        return False
    if not isinstance(late, dict):
        return False
    acceptance = late.get('acceptance')
    message_id = late.get('message_id')
    if (acceptance == 'accepted' and isinstance(message_id, str)
            and 0 < len(message_id) <= PROVIDER_MESSAGE_ID_MAX):
        with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
            payload = (_find(_rows(doc), key) or {}).get('payload') or {}
            candidate = delivery_proof().contact_scope(payload)
        settled = _settle(key, True, '', attempts, late,
                          retained_now=_retained_current(candidate))
        if settled is not None:
            _payload_receipt(settled[1], STATUS_DELIVERED)
            _apply_effects(key)
        return True
    if acceptance == 'not_found' and not closed and time.time() - contacted >= NOT_FOUND_AFTER_SECS:
        acceptance = 'not_attempted'
    _definitive_receipt_failure(key, acceptance)
    return False


def _payload_receipt(payload: dict, status: str, detail: str = "") -> None:
    if status == STATUS_DELIVERED and not detail:
        detail = delivery_detail(payload.get("presentation_status"))
    HOOKS["record"](payload.get("label") or "", status, detail,
                    usage=payload.get("usage"), decision_ids=payload.get("decision_ids"),
                    run_id=payload.get("run_id") or "")


def _routine_authorized(payload: dict, expected_digest=None) -> bool:
    if expected_digest is None and 'routine_handoff_digest' not in payload:
        return True
    try:
        if not isinstance(expected_digest, str) or payload.get('routine_handoff_digest') != expected_digest:
            return False
        if 'body' in payload and payload['routine_handoff_digest'] != routine_payload_digest(payload):
            return False
        return HOOKS['routine_authorized'](payload) is True
    except Exception:
        return False


def _attempt(key: str) -> bool:
    """One send attempt for one row. True iff the CHANNEL acknowledged it."""
    now, today = time.time(), str(HOOKS["local_today"]() or "")
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            return False          # another thread is already sending this exact message
        _INFLIGHT.add(key)
    try:
        with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
            row = _find(_rows(doc), key)
            original = dict(row.get('payload') or {}) if row else None
            snapshot = dict(row) if row else None
            routine_digest = row.get('routine_handoff_digest') if row else None
        if original is not None and not _routine_authorized(original, routine_digest):
            return False
        if _reconcile_ambiguous_send(key):
            return True
        # Cadence holds do not spend a send attempt; expiry and uncertain receipts retain
        # their existing owner even while delivery is quiet. Readiness failures hold this row.
        if (snapshot and snapshot.get('acceptance') not in ('unknown', 'in_flight')
                and _expiry(snapshot, now, today) is None):
            try:
                if not HOOKS['delivery_ready'](original or {}):
                    return False
            except Exception:
                return False
        claim = _claim(key, now, today)
        if claim is None:
            return False
        state, payload, extra = claim
        if state == "aged":
            _payload_receipt(payload, extra[0], extra[1])
            return False
        if state == 'held':
            return False
        attempts, day = extra
        label = str(payload.get("label") or "")
        try:
            current = bool(HOOKS['valid'](payload))
            fresh = HOOKS['refresh_brief_offer'](payload) if current else payload
            current = current and fresh is not None and (fresh is payload or 'contacted_at' not in (snapshot or {}))
        except Exception as e:  # noqa: BLE001 — one row's broken check must not stall the rows behind it
            # The attempt is charged, so a check that keeps raising still runs out of budget.
            print(f"[sotto] {label or '?'}: validity check failed ({type(e).__name__}) — held for the next pass",
                  flush=True)
            return False
        if not current:
            closed = _supersede(key, 'the underlying item changed or its source was disconnected', recompose=True)
            if closed is not None:
                _payload_receipt(closed, STATUS_SUPERSEDED, 'item no longer current')
            _apply_effects(key)
            return False
        replacement = fresh if fresh is not payload else None
        payload = fresh
        # THE DELIVER-ONCE GATE (Aug 30 the evening brief went out twice): a brief-kind row proves
        # here that it owns today's marker; one that doesn't is superseded, not sent.
        if kind_for(label) == KIND_BRIEF:
            # A row with no run id answers "did this copy claim the marker?" with its own id.
            gate = str(HOOKS["brief_gate"](label, day, str(payload.get("run_id") or "") or key)
                       or GATE_SEND)
            if gate == GATE_SUPERSEDED:
                closed = _supersede(key, SUPERSEDED_DETAIL)
                if closed is not None:
                    print(f"[sotto] {label}: superseded — {SUPERSEDED_DETAIL}", flush=True)
                    _payload_receipt(closed, STATUS_SUPERSEDED, SUPERSEDED_DETAIL)
                return False
            if gate == GATE_RETRY:
                # The marker's lock was busy: neither ours nor provably another lane's. The row
                # stays pending (its attempt is charged) and the next pass asks the gate again.
                print(f"[sotto] {label}: the deliver-once gate was busy — held for the next pass", flush=True)
                return False
            if gate == GATE_NOT_A_BRIEF:
                # Failed, loudly, on the first attempt — retrying the same non-brief cannot help,
                # and the day stays unclaimed for the lane that can still compose one.
                # Refused before any send, so nothing can have reached the provider.
                settled = _settle(key, False, NOT_A_BRIEF_DETAIL, MAX_ATTEMPTS,
                                  {'acceptance': 'not_attempted'})
                if settled is not None:
                    print(f"[sotto] {label}: NOT SENT — {NOT_A_BRIEF_DETAIL}", flush=True)
                    _payload_receipt(settled[1], STATUS_FAILED, NOT_A_BRIEF_DETAIL)
                    try:
                        HOOKS["on_not_a_brief"](settled[1])
                    except Exception as e:  # noqa: BLE001 — the receipt stands; the follow-up is best-effort
                        print(f"[sotto] outbox: not-a-brief follow-up failed ({type(e).__name__}: {e})",
                              flush=True)
                return False
        receipt = {}
        if not _routine_authorized(payload, routine_digest):
            return False
        candidate = delivery_proof().contact_scope(payload)
        capture = HOOKS.get('offer_route')
        offer_route = capture(payload) if capture else {}
        if offer_route is None:
            return False
        retained = _retained_current(candidate)
        proof = ((label.startswith(HANDOFF_CONTEXT_PREFIXES) and HOOKS['handoff_context'](label))
                 or {'source_revision': payload.get('prep_source_revision'), 'route_digest': payload.get('prep_route_digest')})
        prep = feedback_skill(label) == 'meeting-prep'
        required = requires_source_scope(label)
        current_scope = (required or not payload.get('presentation')) and _retained_current(proof)
        if required and not current_scope:
            return False  # No provider contact. The next claim rechecks validity and expiry.
        context = {'source_revision': proof['source_revision']} if current_scope else {}
        deadline = {}
        if payload.get('valid_until') is not None and (prep or payload.get('prep_meeting_start') is not None):
            deadline['valid_until_ms'] = int(payload['valid_until'] * 1000)
        begun = _begin_send(key, retained, offer_route, replacement)
        if isinstance(begun, dict):
            _payload_receipt(begun, STATUS_SUPERSEDED, 'closure notice is already in delivery')
            _apply_effects(key)
        if begun is not True:
            return False
        try:
            if payload.get('presentation'):
                answer = HOOKS['send_gallery'](payload['presentation'], payload.get('target') or '', key, **deadline)
            else:
                answer = HOOKS['send'](payload.get('body') or '', payload.get('target') or '', key, attempts, **context, **deadline)
            ok, detail = answer[:2]
            receipt = answer[2] if len(answer) > 2 else {}
        except Exception as e:  # a send that raises has unknown acceptance, never confirmed delivery
            ok, detail = False, type(e).__name__
            receipt = {'acceptance': 'unknown'}
        settled = _settle(key, ok, detail, attempts, receipt,
                          retained_now=_retained_current(candidate))
        if settled is None:
            return bool(ok)
        state, payload = settled
        if state == "delivered":
            _payload_receipt(payload, STATUS_DELIVERED)
            _apply_effects(key)
            return True
        # LOUD on every failed attempt; the receipt says whether this failure is final or the latest.
        print(f"[sotto] {label or '?'}: delivery FAILED to {payload.get('target')} — {detail[:300]}",
              flush=True)
        if state == "gave_up":
            _payload_receipt(payload, STATUS_FAILED,
                             f"{detail} — gave up after {attempts} attempts")
        else:
            _payload_receipt(payload, STATUS_FAILED,
                             f"{detail} — queued, retry {attempts}/{MAX_ATTEMPTS}")
        return False
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(key)


def _apply_effects(key):
    # An invalidation callback may synchronously enqueue and settle its replacement. It is already
    # inside the one OS-locked effect lane, so permit that same thread to apply the nested row.
    if getattr(_EFFECT_LOCK_STATE, 'held', False):
        return _apply_effects_locked(key)
    # One independent lock serializes callbacks without holding the outbox JSON transaction. A
    # crashed process releases flock automatically; a busy callback makes another pass skip rather
    # than wait behind user-facing work.
    lock_fd = os.open(path() + '.effects.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        _EFFECT_LOCK_STATE.held = True
        try:
            return _apply_effects_locked(key)
        finally:
            _EFFECT_LOCK_STATE.held = False
    finally:
        os.close(lock_fd)


def _apply_effects_locked(key):
    now = time.time()
    exhausted = None
    with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
        row = _find(_rows(doc), key)
        candidate = dict(row.get('payload') or {}) if row and row.get('effects_pending') else None
        routine_digest = row.get('routine_handoff_digest') if row else None
    if candidate is not None and not _routine_authorized(candidate, routine_digest):
        return False
    with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
        row = _find(_rows(doc), key)
        if not row or not row.get('effects_pending') or now < row.get('effects_next_at', 0):
            return False
        payload = dict(row.get('payload') or {})
        routine_digest = row.get('routine_handoff_digest')
        phase = row.get('effect_phase', 'accepted')
        # The attempt is persisted before invoking external effect code so a process death counts.
        # On recovery, quarantine an already exhausted row before calling that crashing code again.
        attempt = int(row.get('effects_attempts') or 0)
        if attempt >= MAX_EFFECT_ATTEMPTS:
            exhausted = _quarantine(row)
        else:
            attempt += 1
            row['effects_next_at'] = now + 60
            row['effects_attempts'] = attempt
    if exhausted is not None:
        _record_effect_quarantine(exhausted, phase)
        return False
    try:
        if not _routine_authorized(payload, routine_digest):
            return False
        # None is the legacy hook's successful return; explicit False is a retryable failure.
        ok = HOOKS['on_invalid' if phase == 'invalidate' else 'on_delivered'](payload) is not False
    except Exception as error:
        ok = False
        print(f'[sotto] outbox: effects pending ({type(error).__name__})', flush=True)
    if ok:
        with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
            row = _find(_rows(doc), key)
            if row and row.get('status') in (STATUS_DELIVERED, STATUS_SUPERSEDED):
                row['effects_pending'] = False
                row['effects_status'] = 'applied'
                row['finished_at'] = time.time()
                _close(row)
    elif attempt >= MAX_EFFECT_ATTEMPTS:
        quarantined = None
        with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
            row = _find(_rows(doc), key)
            if row and row.get('effects_pending'):
                quarantined = _quarantine(row)
        print(f'[sotto] outbox: effects quarantined after {MAX_EFFECT_ATTEMPTS} attempts', flush=True)
        if quarantined is not None:
            _record_effect_quarantine(quarantined, phase, announce=False)
    return ok


def _record_effect_quarantine(payload, phase, announce=True):
    if announce:
        print(f'[sotto] outbox: effects quarantined after {MAX_EFFECT_ATTEMPTS} attempts', flush=True)
    # A print line is not a receipt: the message DID go out, and the Record must say that what was
    # supposed to follow it never happened — otherwise recovery has no durable explanation.
    detail = (f'superseded, but releasing its reservation for a fresh brief failed '
              f'permanently after {MAX_EFFECT_ATTEMPTS} attempts (quarantined)'
              if phase == 'invalidate' else
              f'delivered, but its follow-up effects failed permanently after '
              f'{MAX_EFFECT_ATTEMPTS} attempts (quarantined)')
    _payload_receipt(payload, STATUS_FAILED, detail)


# ── The three public entry points ────────────────────────────────────────────────────────────────

def deliver(payload: dict) -> bool:
    """Enqueue this message, then try to send it. True iff the channel acknowledged it NOW; False
    means it is on file and the drain owns it from here — never that it was lost.

    `payload` is {label, body, target, decision_ids, usage, effects, run_id}: everything a retry
    needs, so the drain never has to re-run anything to deliver what was already composed. `run_id`
    is the spawning run's identity — the same one its child env carries as SOTTO_DELIVERY_RUN_ID —
    and it is what the brief gate compares against the marker."""
    label, body = str(payload.get("label") or ""), str(payload.get("body") or "")
    key = str(payload.get('run_id') or '') or message_key(label, body)
    now, today = time.time(), str(HOOKS["local_today"]() or "")
    if _enqueue(key, kind_for(label), payload, now, today) != STATUS_PENDING:
        return False          # this exact message already reached a terminal state — no double-send
    return _attempt(key)


def owns_run(run_id):
    """An enqueued result belongs to the outbox, including unresolved provider acceptance."""
    with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
        return _find(_rows(doc), run_id) is not None


def receipt_for_run(run_id: str, expected_digest: str) -> dict | None:
    """Project a seven-day receipt without content. Missing evidence holds the routine."""
    if not isinstance(run_id, str) or not isinstance(expected_digest, str):
        raise ValueError('invalid routine outbox lookup')
    with HOOKS['json_transaction'](path(), default={'rows': []}) as doc:
        row = _find(_rows(doc), run_id)
        if row is None:
            return None
        if row.get('routine_handoff_digest') != expected_digest:
            raise RuntimeError('routine outbox receipt identity conflict')
        receipt = row.get('receipt') or {}
        uncertain = row.get('acceptance_uncertain')
        message_id = receipt.get('message_id')
        if (uncertain is True and row.get('status') == STATUS_DELIVERED and row.get('acceptance') == 'accepted'
                and isinstance(message_id, str) and 0 < len(message_id) <= PROVIDER_MESSAGE_ID_MAX):
            uncertain = False  # older accepted rows could retain uncertainty after receipt lookup
        return {'kind': 'outbox', 'run_id': run_id, 'digest': expected_digest,
                'status': row.get('status'), 'acceptance': row.get('acceptance'),
                'acceptance_uncertain': uncertain,
                'effects_pending': row.get('effects_pending'),
                'effects_status': row.get('effects_status'),
                'accepted_at': receipt.get('accepted_at')}


def drain() -> dict:
    """One retry pass over every pending row, oldest first. Returns {attempted, delivered} — the
    heartbeat logs nothing on a quiet pass, because a quiet outbox is the normal state."""
    if not _DRAIN_LOCK.acquire(blocking=False):
        return {"attempted": 0, "delivered": 0}    # a pass is already running; two would race
    try:
        try:
            with HOOKS["json_transaction"](path(), default={"rows": []}) as doc:
                rows = _prune(_rows(doc), time.time())
                doc["rows"] = rows
                keys = [str(r.get("id")) for r in rows if r.get("status") == STATUS_PENDING]
                late = [str(r.get('id')) for r in rows if r.get('late_until') and _awaiting(r)]
                effect_keys = [str(r.get('id')) for r in rows if r.get('effects_pending')]
        except Exception:  # noqa: BLE001 — an unreadable outbox must never kill the heartbeat
            return {"attempted": 0, "delivered": 0}  # a set-aside one is recorded where it is set aside
        for key in effect_keys:
            _apply_effects(key)
        for key in late:  # never sends: only records a receipt that arrived after the close
            _reconcile_ambiguous_send(key)
        attempted = delivered = 0
        for key in keys:
            attempted += 1
            if _attempt(key):
                delivered += 1
        return {"attempted": attempted, "delivered": delivered}
    finally:
        _DRAIN_LOCK.release()


def invalidation_revision(kind, day):
    """A reverted context must not reuse a terminal, invalidated prepared artifact."""
    if not kind:
        return ''
    try:
        with open(path(), encoding='utf-8') as stream:
            rows = _rows(json.load(stream))
    except FileNotFoundError:
        return ''
    ids = sorted(str(row['id']) for row in rows if row.get('day') == day
                 and row.get('effect_phase') == 'invalidate'
                 and str((row.get('payload') or {}).get('label', '')).endswith('sotto-' + kind + '-brief'))
    return _content_id('\n'.join(ids)) if ids else ''


def counts() -> dict:
    """{pending, failed} for the dashboard — current state, read from the outbox itself rather than
    from the receipt log, because the log is history and this is what is still owed to you."""
    out = {"pending": 0, "failed": 0, "effects_pending": 0, "effects_failed": 0}
    try:
        # A plain read, deliberately: this runs on every dashboard page load, and taking the write
        # lock to count rows would make a stat line contend with the drain that is doing the work.
        with open(path(), encoding="utf-8") as f:
            rows = _rows(json.load(f) or {})
    except Exception:  # noqa: BLE001 — no outbox, or an unreadable one, is a quiet zero
        return out
    for row in rows:
        if row.get("effects_pending"):
            out["effects_pending"] += 1
        if row.get('effects_status') == 'quarantined':
            out['effects_failed'] += 1
        if row.get("status") == STATUS_PENDING:
            out["pending"] += 1
        elif row.get("status") == STATUS_FAILED:
            out["failed"] += 1
    return out


def start_drain_thread():
    """Start the retry heartbeat at server boot. SOTTO_OUTBOX=0 disables the heartbeat (not the
    outbox: messages are still enqueued before every send, so nothing is ever sent unrecorded).
    Sleeps FIRST — at boot the first drain has nothing to do and the channel may not be up yet.
    Returns the thread, or None when disabled."""
    if (os.environ.get("SOTTO_OUTBOX", "").strip() or "1") == "0":
        return None

    def _loop():
        while True:
            time.sleep(DRAIN_INTERVAL_SECS)
            try:
                drain()
            except Exception as e:  # noqa: BLE001 — the heartbeat must never die
                print(f"[sotto] outbox drain error: {e}", flush=True)

    t = threading.Thread(target=_loop, name="outbox-drain", daemon=True)
    t.start()
    return t
