#!/usr/bin/env python3
"""
outbox.py — the durable delivery outbox (ROADMAP § Reliability P0, item 2).

**Nothing Sotto says is marked delivered until the channel says so; what fails waits its turn
instead of dying.**

THE BUG THIS EXISTS FOR. `receiver._deliver_text` already refuses to lie about a send — a failed
`hermes send` leaves a loud `failed` receipt instead of a green one (Aug 2026, the seam that fixed
five nudge producers writing to a sink). But an honest receipt for a lost message is still a lost
message: the words were composed, the interrupt budget was spent, the run cost real tokens, and a
gateway that was down for ninety seconds threw all of it away. The Front Door plan puts a
third-party iMessage relay (Photon) in the command path, where an outage is a *when*, not an *if* —
so "the channel was down" must mean **queued and retried**, never **lost**.

WHERE IT SITS. `receiver._deliver_text` is the ONE place Sotto's words leave this box: every lane
(brief, event nudge, proactive, post-meeting tap, the release valve, the dashboard's run-now and
the midday digest) reaches the channel through it, because every one of them is spawned by
`_spawn_and_deliver` and delivered by that function. So the outbox wraps exactly that call:
enqueue BEFORE the first send attempt, transition only on the channel's ack. One writer, one file.

WHAT "THE CHANNEL ACKNOWLEDGES" MEANS TODAY, honestly. The strongest ack available at this seam is
`hermes send` exiting 0 — the CLI's own report that it handed the message to the platform. That is
weaker than a gateway send-API returning a message id, and it is what exists; when Photon (or any
channel with a real receipt) lands, `HOOKS["send"]` is the one function to strengthen and every
lane inherits it. Everything upstream of the send — a spawned run exiting 0 — is NOT an ack and is
not treated as one.

WHERE THE CHANNEL-HEALTH GATE STAYS. `receiver._delivery_channel_ready` is an on-disk probe (are
there WhatsApp creds?), and by its own docstring it cannot tell a live channel from a dead one. It
therefore stays exactly where it already earns its keep — UPSTREAM, before triage spends a nudge of
the daily interrupt budget — and it is deliberately NOT repeated here. At this seam the real probe
is the send itself, so the outbox never refuses to try. That also settles the budget question the
obvious way: a nudge is never DECIDED while the channel is known down, so a queued-then-expired row
never spent budget there is nothing to refund. What changed is the other half — once words exist,
the budget is already spent and throwing them away is pure loss, so a channel that fails now means
"queued and retried", never "dropped".

THE ONE THING IT CANNOT PROMISE. If the process dies in the window between the channel accepting a
message and this module recording that it did, the row is still pending and the next drain sends it
again. The attempt is charged BEFORE the send precisely so that window is bounded by
`MAX_ATTEMPTS` rather than unbounded — a duplicate brief is a nuisance, a silently lost one is the
failure this module exists to end. Every non-crash path is exactly-once: the transitions run inside
`json_transaction`'s locked read-modify-write, so two drains cannot both deliver one row.

THE DELIVER-ONCE GATE IS MACHINERY HERE, NOT A PROMPT (Aug 30, 2026). The evening brief went out
twice — the cron lane claimed `briefs/<day>.evening.delivered` at 17:34 and delivered, and the
wake-push lane's text still left through this outbox at 17:35, a full minute after that marker
existed. The claim lived only in the brief SKILL.md's step 6 ("if it prints `already`, STOP"), and
the run did not honour it. So every brief-kind row now proves OWNERSHIP of today's marker at this
seam, right before `HOOKS["send"]`: no marker, this row atomically creates one carrying its own run
id and sends; the marker already holds this row's run id (the run claimed, obediently), it sends;
the marker holds anyone else's id, the row goes STATUS_SUPERSEDED — receipted, payload dropped,
never retried, never sent. `HOOKS["brief_gate"]` is the receiver's, because the marker's path and
which labels even have one are already its to own.

EXPIRY IS PER KIND, AND REUSES THE FRESHNESS DOCTRINE THE FUNNEL ALREADY HAS — nothing new is
invented here:

  * a **nudge** expires `NUDGE_MAX_AGE_MIN` after it was composed — the same 240 minutes the
    release valve refuses to promote a held nudge past (`triage_event.VALVE_MAX_AGE_MIN`). A
    "meeting in 10 minutes" ping that arrives an hour late is worse than silence, so it is
    ledgered `expired` and never sent.
  * a **brief** (morning, evening, the weekly pulse) retries until the end of its LOCAL day and
    then `failed` — visibly, because a day with no brief is something you must be told about.
  * the **digest** retries until the end of its local day too, then `expired` — that day ends
    before the next digest window opens at 12:30, and tomorrow's digest supersedes it.

MAX_ATTEMPTS is the backstop underneath all three, not a fourth rule: at the capped backoff it is a
full day of trying, longer than any kind's expiry, so in practice expiry is what ends a row and this
only catches one whose clock stopped moving.

WHAT IT KEEPS, AND FOR HOW LONG. A row carries the message's text only while that text might still
have to be sent: `_close` takes the payload out of the file at the moment the row goes terminal, so
what remains is the id, the kind, the attempt count and the reason. Keeping a delivered brief's
words on the volume for a week would be exactly the situational storage the standing bars forbid.
The closed row itself is pruned after RETENTION_SECS, which is what keeps "what failed?" answerable.

Loaded by receiver.py via importlib (the relay/connectors/dashboard/calcache pattern); the receiver
wires HOOKS — late-bound lambdas over ITS globals — so this module never imports the receiver and
stays import-safe on its own. Stdlib only.

Named constants, not knobs (defaults matter — see CLAUDE.md): MAX_ATTEMPTS, BACKOFF_BASE_SECS,
BACKOFF_MAX_SECS, DRAIN_INTERVAL_SECS, NUDGE_MAX_AGE_MIN, RETENTION_SECS. The only env var is
SOTTO_OUTBOX=0, which turns the retry heartbeat off (the enqueue/ack path stays, so nothing is ever
sent unrecorded).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

# The tree's ONE content-id scheme (sha256 of a line, 16 hex). keys.py is the byte-identical
# vendored copy the receiver image carries; the outbox's idempotency key is that scheme applied to
# a message, not a second hashing convention invented here.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from keys import queue_key as _content_id  # noqa: E402

# ── Wiring surface (receiver overrides these; the defaults keep the module import-safe) ──────────

def _unwired(name):
    def _raise(*_a, **_k):
        raise RuntimeError(f"outbox.HOOKS[{name!r}] is unwired — load this module from receiver.py")
    return _raise


HOOKS = {
    "data_root": lambda: os.environ.get("SOTTO_DATA", "/data"),
    # THE locked read-modify-write for this image (connectors.json_transaction). No sane default:
    # an unlocked outbox is an outbox that double-sends.
    "json_transaction": _unwired("json_transaction"),
    # (body, target) -> (ok, detail). receiver._send_via_channel — the ONE call that touches the
    # channel, and the ONE function to strengthen when a channel offers a real receipt.
    "send": _unwired("send"),
    "record": lambda *a, **k: None,             # receiver._record_delivery — the receipt line
    "on_delivered": lambda payload: None,       # receiver: finalize the run's chase/handoff effects
    "local_today": lambda: time.strftime("%Y-%m-%d"),   # ONE tz resolution per process
    # (label, day, run_id) -> "send" | "superseded". THE deliver-once gate for the day's brief, asked
    # once per attempt, right before the channel is. receiver._brief_delivery_gate: it owns the
    # marker's path AND which labels have one (the pulse and the digest do not, and it answers
    # "send" for them), because that path already has exactly one owner and it is not this module.
    "brief_gate": _unwired("brief_gate"),
}

# ── The constants (this module is their one writer) ──────────────────────────────────────────────

KIND_BRIEF = "brief"
KIND_DIGEST = "digest"
KIND_NUDGE = "nudge"

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
# The other lane already sent today's brief, so this copy never will — terminal on the first
# attempt, never retried. Distinct from `expired` (which is about time) and from `failed` (which is
# about the channel): nothing went wrong here, another run simply got there first.
STATUS_SUPERSEDED = "superseded"

TERMINAL = (STATUS_DELIVERED, STATUS_FAILED, STATUS_EXPIRED, STATUS_SUPERSEDED)

GATE_SEND = "send"
GATE_SUPERSEDED = "superseded"
SUPERSEDED_DETAIL = ("today's brief was already delivered by the other lane — this copy was "
                     "not sent")

BACKOFF_BASE_SECS = 60        # first retry a minute later: a gateway restart is the common outage
BACKOFF_MAX_SECS = 900        # …doubling, capped at the */15 cadence every other heartbeat uses
DRAIN_INTERVAL_SECS = 60      # its own beat, not the valve's: a brief must not wait 15 min to retry
# The backstop, not the rule: EXPIRY is what normally ends a row, and 96 attempts is a full day at
# the capped backoff — longer than the longest expiry any kind has. It exists so a row whose day
# never seems to end (a clock jump, a stuck timezone) still stops, loudly, instead of forever.
MAX_ATTEMPTS = 96
# MUST equal triage_event.VALVE_MAX_AGE_MIN — the funnel's own "a held nudge this old is not worth
# promoting" number. Copy-plus-guard, exactly like keys.py and the dashboard's mirror of the same
# constant: tests/test_docs_drift.py fails the suite if the two ever disagree.
NUDGE_MAX_AGE_MIN = 240
RETENTION_SECS = 7 * 24 * 3600   # terminal rows linger a week so "what failed?" has an answer

_DRAIN_LOCK = threading.Lock()   # one drain at a time, whatever calls it
_INFLIGHT: set = set()           # ids being sent right now — the check-through-accept claim
_INFLIGHT_LOCK = threading.Lock()


def _root() -> str:
    """The volume, through the hook — the same spelling calcache and the dashboard use, so the
    shared-file-map guard in tests/test_docs_drift.py can read this module's paths like the rest."""
    return HOOKS["data_root"]()


def path() -> str:
    """$SOTTO_DATA/events/outbox.json — one JSON document `{"rows": [...]}`, not a JSONL append log.

    The rows MUTATE (attempts, status, next attempt), so an append-only log would need compaction to
    stay readable, and compaction is a second writer. A whole-document rewrite through
    `connectors.json_transaction` gives the atomicity (tmp → os.replace) AND the cross-thread lock
    for free, from the helper the rest of the image already uses. Volumes hold a handful of rows a
    day; there is nothing here to stream."""
    return os.path.join(_root(), "events", "outbox.json")


def message_key(label: str, body: str) -> str:
    """The idempotency key: what makes two enqueues the SAME message. Same label, same words, same
    key — so a retried lane, a re-fired trigger or a replayed drain adds nothing. It is
    `keys.queue_key` — the tree's existing content-id — over `label` and the words, not a second
    hashing convention."""
    return _content_id(f"{label}\n{body}")


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
            if r.get("status") not in TERMINAL
            or (now - float(r.get("created_at") or 0)) <= RETENTION_SECS]


def _find(rows: list, key: str):
    for row in rows:
        if row.get("id") == key:
            return row
    return None


def _close(row: dict) -> dict:
    """Take the row's payload OUT of the file as it goes terminal, and hand it back to the caller.

    The outbox holds the words of a message only while it might still have to send them. A row that
    landed, gave up, or aged out keeps its id, kind, attempts and reason — everything the counts and
    the Record need — and nothing a person said. Storing a delivered brief's text for a week would
    be exactly the situational keeping the standing bars forbid."""
    payload = dict(row.get("payload") or {})
    row["payload"] = {"label": payload.get("label") or ""}
    return payload


# ── Expiry ───────────────────────────────────────────────────────────────────────────────────────

def _expiry(row: dict, now: float, today: str):
    """(terminal status, one-sentence reason) when this row has aged out, else None."""
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
        if row is None:
            row = {"id": key, "kind": kind, "created_at": now, "day": today, "attempts": 0,
                   "next_at": now, "last_error": "", "status": STATUS_PENDING, "payload": payload}
            rows.append(row)
        doc["rows"] = rows
        return str(row.get("status") or STATUS_PENDING)


def _claim(key: str, now: float, today: str):
    """Charge one attempt and hand back what to send, or say why not. One transaction, three
    answers: `None` (nothing to do — terminal, or not due yet), `aged` (it timed out — terminal),
    `send` (go, with `(attempts, day)` — the brief gate needs the row's own day, not the clock's).

    The attempt is charged BEFORE the send, under the lock: a crash mid-send then looks exactly like
    a failed attempt (backoff already set, budget already spent) instead of an unbounded replay."""
    with HOOKS["json_transaction"](path(), default={"rows": []}) as doc:
        rows = _rows(doc)
        doc["rows"] = rows
        row = _find(rows, key)
        if row is None or row.get("status") != STATUS_PENDING:
            return None
        aged = _expiry(row, now, today)
        if aged:
            row["status"], row["last_error"] = aged[0], aged[1]
            return ("aged", _close(row), aged)
        if now < float(row.get("next_at") or 0):
            return None
        row["attempts"] = int(row.get("attempts") or 0) + 1
        row["next_at"] = now + backoff_secs(row["attempts"])
        return ("send", dict(row.get("payload") or {}),
                (row["attempts"], str(row.get("day") or "")))


def _supersede(key: str, detail: str):
    """Close a row whose brief the other lane already delivered. Terminal like every other ending —
    the payload leaves the file and no drain will ever claim it again."""
    with HOOKS["json_transaction"](path(), default={"rows": []}) as doc:
        rows = _rows(doc)
        doc["rows"] = rows
        row = _find(rows, key)
        if row is None or row.get("status") != STATUS_PENDING:
            return None
        row["status"], row["last_error"] = STATUS_SUPERSEDED, detail[:300]
        return _close(row)


def _settle(key: str, ok: bool, detail: str, attempts: int):
    """Record the channel's answer. Delivered exactly once: a row already terminal is left alone, so
    two drains racing the same message cannot both mark it delivered or both give up on it."""
    with HOOKS["json_transaction"](path(), default={"rows": []}) as doc:
        rows = _rows(doc)
        doc["rows"] = rows
        row = _find(rows, key)
        if row is None or row.get("status") != STATUS_PENDING:
            return None
        if ok:
            row["status"], row["last_error"] = STATUS_DELIVERED, ""
            return ("delivered", _close(row))
        row["last_error"] = detail[:300]
        if attempts >= MAX_ATTEMPTS:
            row["status"] = STATUS_FAILED
            return ("gave_up", _close(row))
        return ("retry", dict(row.get("payload") or {}))


def _payload_receipt(payload: dict, status: str, detail: str = "") -> None:
    HOOKS["record"](payload.get("label") or "", status, detail,
                    usage=payload.get("usage"), decision_ids=payload.get("decision_ids"))


def _attempt(key: str) -> bool:
    """One send attempt for one row. True iff the CHANNEL acknowledged it."""
    now, today = time.time(), str(HOOKS["local_today"]() or "")
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            return False          # another thread is already sending this exact message
        _INFLIGHT.add(key)
    try:
        claim = _claim(key, now, today)
        if claim is None:
            return False
        state, payload, extra = claim
        if state == "aged":
            _payload_receipt(payload, extra[0], extra[1])
            return False
        attempts, day = extra
        label = str(payload.get("label") or "")
        # THE DELIVER-ONCE GATE, asked before the channel is (Aug 30: the evening brief went out
        # twice, a full minute after the winning lane's marker existed, because the gate was an
        # INSTRUCTION in the skill and the run did not honour it). A brief-kind row now proves at
        # this seam that it owns today's marker; one that doesn't is superseded, not sent.
        if kind_for(label) == KIND_BRIEF:
            # A row whose lane did not name a run falls back to its OWN id: the gate's whole
            # question is "did this copy claim the marker?", and the row id answers it just as
            # durably across retries as a run id would.
            gate = str(HOOKS["brief_gate"](label, day, str(payload.get("run_id") or "") or key)
                       or GATE_SEND)
            if gate == GATE_SUPERSEDED:
                closed = _supersede(key, SUPERSEDED_DETAIL)
                if closed is not None:
                    print(f"[sotto] {label}: superseded — {SUPERSEDED_DETAIL}", flush=True)
                    _payload_receipt(closed, STATUS_SUPERSEDED, SUPERSEDED_DETAIL)
                return False
        try:
            ok, detail = HOOKS["send"](payload.get("body") or "", payload.get("target") or "")
        except Exception as e:  # noqa: BLE001 — a send that raises is a send that failed
            ok, detail = False, f"{type(e).__name__}: {e}"
        settled = _settle(key, ok, detail, attempts)
        if settled is None:
            return bool(ok)
        state, payload = settled
        if state == "delivered":
            _payload_receipt(payload, STATUS_DELIVERED)
            try:
                HOOKS["on_delivered"](payload)
            except Exception as e:  # noqa: BLE001 — the message landed; the follow-up must not undo that
                print(f"[sotto] outbox: post-delivery effects failed ({type(e).__name__}: {e})",
                      flush=True)
            return True
        # LOUD on every failed attempt, exactly as before the outbox existed: a nudge decided and
        # then not delivered is the thing this whole seam is here to make impossible to miss. What
        # is NEW is the second half of the sentence — the receipt now says whether this failure is
        # final or merely the latest attempt, so the Record can't read "failed" as "gone".
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


# ── The three public entry points ────────────────────────────────────────────────────────────────

def deliver(payload: dict) -> bool:
    """Enqueue this message, then try to send it. True iff the channel acknowledged it NOW; False
    means it is on file and the drain owns it from here — never that it was lost.

    `payload` is {label, body, target, decision_ids, usage, effects, run_id}: everything a retry
    needs, so the drain never has to re-run anything to deliver what was already composed. `run_id`
    is the spawning run's identity — the same one its child env carries as SOTTO_DELIVERY_RUN_ID —
    and it is what the brief gate compares against the marker."""
    label, body = str(payload.get("label") or ""), str(payload.get("body") or "")
    key = message_key(label, body)
    now, today = time.time(), str(HOOKS["local_today"]() or "")
    if _enqueue(key, kind_for(label), payload, now, today) != STATUS_PENDING:
        return False          # this exact message already reached a terminal state — no double-send
    return _attempt(key)


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
        except Exception:  # noqa: BLE001 — an unreadable outbox must never kill the heartbeat
            return {"attempted": 0, "delivered": 0}
        attempted = delivered = 0
        for key in keys:
            attempted += 1
            if _attempt(key):
                delivered += 1
        return {"attempted": attempted, "delivered": delivered}
    finally:
        _DRAIN_LOCK.release()


def counts() -> dict:
    """{pending, failed} for the dashboard — current state, read from the outbox itself rather than
    from the receipt log, because the log is history and this is what is still owed to you."""
    out = {"pending": 0, "failed": 0}
    try:
        # A plain read, deliberately: this runs on every dashboard page load, and taking the write
        # lock to count rows would make a stat line contend with the drain that is doing the work.
        with open(path(), encoding="utf-8") as f:
            rows = _rows(json.load(f) or {})
    except Exception:  # noqa: BLE001 — no outbox, or an unreadable one, is a quiet zero
        return out
    for row in rows:
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
