#!/usr/bin/env python3
"""
The retention sweep: THE one table of what Sotto's volume keeps, and for how long.

`docs/DATA-FLOW.md` promises the reader that everything on `$SOTTO_DATA` is deletable, and
`sotto-chief-of-staff/tools/forget.py` is the tool that keeps that promise when a person asks. This
module is the half a person never has to ask for: a daily sweep, fired from the receiver's own cron
thread, that ages out the exhaust nobody would otherwise remember to delete.

It is MACHINERY, deliberately. Retention written as a skill instruction is retention that stops
happening the first time a model has a better idea — four Hermes prompt-adherence failures in Aug
2026 settled that. Nothing here asks anyone to remember anything.

WHAT IT SWEEPS: exhaust — receipts, staged intermediates, dated markers, diagnostics.
WHAT IT NEVER SWEEPS: the graph (`knowledge/`), the continuity ledger, the golden corpus, your
settings, your credentials, and the decks you asked Sotto to read. `NEVER` below enforces that per
path, not per rule, so a typo'd glob cannot reach the memory either.

Three policies, because three cover every family:

  * `DELETE_OLDER` — the file goes when it is older than N days. A filename carrying a
    `YYYY-MM-DD` is read as the file's own age claim; everything else falls back to mtime.
  * `DROP_LINES_OLDER` — a JSONL ledger is rewritten without the lines whose `ts` is older than N
    days, atomically (tmp → `os.replace`, through `connectors.write_text`).
  * `TRUNCATE_TAIL` — a log is cut to its last N bytes IN PLACE. In place, not unlinked: a brief
    mid-flight holds it open, and unlinking it under that writer sends every later line to a file
    nobody can read (the reason `forget.py --logs` truncates too).

A family that some other module already ages is listed in `EXEMPT` with the owner named, not
re-implemented here — one rule, one writer. `EXEMPT` is not a leftovers bin: it is the other half of
the table, and `accounts_for()` reads both, which is what lets the drift guard prove that every
`forget.py` verb has an answer here.

Loaded by receiver.py via importlib (the outbox/connectors/calcache pattern); the receiver wires
HOOKS — late-bound lambdas over ITS globals — so this module never imports the receiver. Stdlib only.

Named constants, not knobs (defaults matter — see CLAUDE.md): every TTL below. There is no
`SOTTO_RETENTION_*` env var, because "how long do you keep receipts" is not a choice a default
can't serve.
"""
from __future__ import annotations

import glob as _glob
import json
import os
import re
import time

# ── Wiring surface (receiver overrides these; the defaults keep the module import-safe) ──────────

HOOKS = {
    "data_root": lambda: os.environ.get("SOTTO_DATA", "/data"),
    # THE atomic text write for this image (connectors.write_text — the primitive write_json is
    # built on). A JSONL rewrite that isn't atomic is a ledger a crash can halve.
    "write_text": lambda path, text, mode=0o600: _fallback_write_text(path, text, mode),
    # The advisory flock every appender of these ledgers also takes (connectors.file_lock): without
    # it, a line appended between this sweep's read and its replace simply vanished.
    "jsonl_lock": lambda path: _fallback_file_lock(path),
}


import contextlib as _contextlib
import fcntl as _fcntl


@_contextlib.contextmanager
def _fallback_file_lock(path: str):
    """Import-safe stand-in for connectors.file_lock — same flock-on-`<path>.lock` protocol."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lf = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _fcntl.flock(lf, _fcntl.LOCK_EX)
        yield
    finally:
        try:
            _fcntl.flock(lf, _fcntl.LOCK_UN)
        finally:
            os.close(lf)


def _fallback_write_text(path: str, text: str, mode: int = 0o600) -> None:
    """Import-safe stand-in so this module runs (and tests) without the receiver. The receiver
    replaces it with connectors.write_text, which is the same tmp-then-replace with one owner."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── The clock ────────────────────────────────────────────────────────────────────────────────────

# Local time of the daily sweep. 3:30 AM is after the evening brief's retries have given up and
# hours before the morning one stages anything, so a sweep can never race a live run for a file.
# A missed day self-heals: every policy is stated as an AGE, so the next sweep deletes exactly what
# this one would have plus a day's more.
SWEEP_LOCAL = (3, 30)

# ── The TTLs (this module is their one writer) ───────────────────────────────────────────────────

# Receipts. "Did it land?" is a question about last week, not last year.
DELIVERY_RECEIPT_DAYS = 90
# …except the record of what Sotto sent on your behalf, kept twice as long on purpose: it is the
# authorization trail for outbound acts, and it is the one ledger you would want when you doubt one.
SEND_RECEIPT_DAYS = 180
# Triage verdicts — the "why didn't I get nudged?" substrate; a quarter answers every real ask.
TRIAGE_VERDICT_DAYS = 90
# One line per dashboard write, on the same clock as the delivery receipts.
DASHBOARD_AUDIT_DAYS = 90
# A draft's WORDS. draft_outcomes matches within 24 hours and the learning loop re-runs each brief,
# so a month is generous; past that it is old text about a conversation that already happened.
DRAFT_LEDGER_DAYS = 30
# Delivered briefs and their per-day markers, on the same clock as `knowledge/snapshots/` (which
# compose_brief.py already prunes at 60 days) — the brief and the payload it was built from age out
# together or the archive tells half a story.
BRIEF_ARCHIVE_DAYS = 60
# Staged intermediates: a payload or an effects file is read by the run it was staged for and is
# dead weight the next morning. A week is the crash margin, the same one the receiver's event
# bundles already get.
STAGED_DAYS = 7
# The once-per-day nudge dedup stamps. Only today's is ever read; a month is pure forensics.
PROACTIVE_STAMP_DAYS = 30
# The brief log's ceiling. sotto_log.bounded_append rotates it at 4 MB, but gemini.py appends to the
# same file WITHOUT that helper — so the file has a writer with no bound, and this is the backstop
# that makes one exist. Above the writer's threshold on purpose: in normal operation rotation keeps
# the file well under this and the sweep does nothing.
LOG_TAIL_MAX_BYTES = 5 * 1024 * 1024

# ── Policies ─────────────────────────────────────────────────────────────────────────────────────

DELETE_OLDER = "delete_older_than_days"
DROP_LINES_OLDER = "drop_lines_older_than_days"
TRUNCATE_TAIL = "truncate_to_last_bytes"

# Never a sweep target, whatever a rule asks for — checked per path, below every glob. The graph,
# the ledger, the corpus, your settings, your credentials, and the artifacts you asked Sotto to
# fetch: deleting any of those is a decision a person makes, not hygiene a daemon performs.
NEVER = ("knowledge", "corpus", "decks", "connectors", "config",
         "preferences.json", "intentions.jsonl", "setup_code")


class Rule:
    """One swept family: a `$SOTTO_DATA`-relative glob, a policy, its number, and the sentence that
    explains it. `why` is the row DATA-FLOW.md renders — if it doesn't fit in one sentence, the rule
    is too clever."""

    def __init__(self, pattern: str, policy: str, amount: int, why: str):
        self.pattern, self.policy, self.amount, self.why = pattern, policy, amount, why

    def __repr__(self) -> str:
        return f"Rule({self.pattern!r}, {self.policy}, {self.amount})"


class Exempt:
    """One family this sweep deliberately leaves alone, and who ages it instead (or why nothing has
    to). Half of the table: a family in neither list is a family nobody has thought about."""

    def __init__(self, pattern: str, why: str):
        self.pattern, self.why = pattern, why

    def __repr__(self) -> str:
        return f"Exempt({self.pattern!r})"


SWEEP = (
    Rule("events/delivery.jsonl", DROP_LINES_OLDER, DELIVERY_RECEIPT_DAYS,
         f"a delivery receipt older than {DELIVERY_RECEIPT_DAYS} days has outlived every question "
         "it answers"),
    Rule("events/sends.jsonl", DROP_LINES_OLDER, SEND_RECEIPT_DAYS,
         f"the record of what Sotto sent on your behalf is kept {SEND_RECEIPT_DAYS} days — twice "
         "as long as anything else, on purpose"),
    Rule("events/queue.jsonl", DROP_LINES_OLDER, TRIAGE_VERDICT_DAYS,
         f"a queued triage verdict older than {TRIAGE_VERDICT_DAYS} days can no longer explain a "
         "nudge you remember"),
    Rule("events/surfaced.jsonl", DROP_LINES_OLDER, TRIAGE_VERDICT_DAYS,
         f"the same {TRIAGE_VERDICT_DAYS} days for the verdict ledger the Record view reads"),
    Rule("events/drafts.jsonl", DROP_LINES_OLDER, DRAFT_LEDGER_DAYS,
         f"a draft's words are kept {DRAFT_LEDGER_DAYS} days — long enough to learn from, short "
         "enough not to be an archive of things you didn't say"),
    Rule("dashboard_audit.jsonl", DROP_LINES_OLDER, DASHBOARD_AUDIT_DAYS,
         f"one line per dashboard write, on the delivery receipts' {DASHBOARD_AUDIT_DAYS}-day clock"),
    Rule("briefs/????-??-??_*.json", DELETE_OLDER, BRIEF_ARCHIVE_DAYS,
         f"a delivered brief is kept {BRIEF_ARCHIVE_DAYS} days, the same as the snapshot it was "
         "built from"),
    Rule("briefs/????-??-??.*.named.json", DELETE_OLDER, BRIEF_ARCHIVE_DAYS,
         "which loops a brief named ages with that brief"),
    Rule("briefs/????-??-??.*.claim", DELETE_OLDER, BRIEF_ARCHIVE_DAYS,
         "a day's brief markers age with that day's brief"),
    Rule("briefs/????-??-??.*.delivered", DELETE_OLDER, BRIEF_ARCHIVE_DAYS,
         "the same, for the deliver-once marker"),
    Rule("briefs/????-??-??.*.payload.json", DELETE_OLDER, STAGED_DAYS,
         f"a staged wake payload is read by that morning's brief and is dead weight "
         f"{STAGED_DAYS} days later"),
    Rule("events/delivery-effects-*.json", DELETE_OLDER, STAGED_DAYS,
         f"a run's staged effects outlive the run only when it crashed; {STAGED_DAYS} days "
         "collects those"),
    Rule("proactive/????-??-??.json", DELETE_OLDER, PROACTIVE_STAMP_DAYS,
         "only today's nudge-dedup stamp is ever read; a month of them is already forensics"),
    Rule("logs/compose_brief.log", TRUNCATE_TAIL, LOG_TAIL_MAX_BYTES,
         f"the brief log keeps its last {LOG_TAIL_MAX_BYTES // (1024 * 1024)} MB — truncated in "
         "place, because a running brief holds it open"),
)

EXEMPT = (
    Exempt("events/outbox.json",
           "outbox.py prunes its own terminal rows after RETENTION_SECS (7 days)"),
    Exempt("events/bundle-*.json",
           "receiver._stage_bundle sweeps them at EVENT_BUNDLE_RETENTION_SECS (7 days)"),
    Exempt("cache/research_*.json",
           "research_attendees.py deletes its own older siblings at CACHE_KEEP_DAYS (7 days)"),
    Exempt("knowledge/snapshots/*.json",
           "compose_brief.py prunes the dated archive at SNAPSHOT_ARCHIVE_DAYS (60 days)"),
    Exempt("connectors/.pending/*.json",
           "connectors.sweep_pending drops abandoned authorize legs at PENDING_TTL_SECS"),
    Exempt("proactive/pending_offer.json",
           "pending_offer.py expires the one standing offer at read; a stale file is already inert"),
    Exempt("dashboard_sessions.json",
           "dashboard.py prunes expired sessions on the way through every read"),
    Exempt("knowledge/last_local_snapshot.json",
           "overwritten by every brief, so it cannot grow; forget.py --snapshot is how it goes"),
    Exempt("cache/calendar_today.json",
           "one file, overwritten by the calendar refresh; it cannot grow"),
    Exempt("cache/meeting_taps.json",
           "one file, rewritten per local day as {date, fired}; it cannot grow"),
    Exempt("cache/update_check.json", "one file, overwritten by the daily update check"),
    Exempt("cache/update_notice.json", "one file, overwritten per version"),
    Exempt("cache/hermes-version.json", "one file, written by start.sh at boot"),
    Exempt("events/seen.json", "a bounded idempotency ring, rewritten in place"),
    Exempt("events/gmail_seen.json", "one cursor file, rewritten in place"),
    Exempt("events/last.stamp", "one liveness stamp, rewritten in place"),
    Exempt("events/last_digest.txt", "one window stamp, rewritten in place"),
    Exempt("proactive/wake_run.last", "one throttle stamp; its mtime IS the value"),
    Exempt("proactive/retune_offer.last", "one cooldown stamp, rewritten in place"),
    Exempt("knowledge/**",
           "the memory — people, companies, master.md, style, outcomes, the continuity ledger "
           "(which resolves its own terminal items after 30 days). Deleting it is a decision you "
           "make about your own graph"),
    Exempt("corpus/**",
           "the golden corpus: confidential regardless, and written only by the corpus builder"),
    Exempt("decks/**",
           "a deck you asked Sotto to read is a requested artifact, not exhaust — DATA-FLOW.md "
           "calls these yours"),
    Exempt("connectors/**", "OAuth tokens and their caches: credentials, kept until you disconnect"),
    Exempt("config/**", "your setup choices, kept until you change them"),
    Exempt("preferences.json", "your rules — mutes, VIPs, tone, the snooze"),
    Exempt("intentions.jsonl", "your own one-shot recipes, folded by id and cancelled when they run"),
    Exempt("setup_code", "the credential that gates the setup surface"),
    Exempt("hermes/**", "the Hermes gateway's own state, including the WhatsApp session — not ours"),
    Exempt("whatsapp-pairing.txt", "one pairing artifact, rewritten by the pairing run"),
    Exempt("google-auth-url.txt", "one setup artifact, rewritten by start.sh"),
    Exempt("telegram-link.json", "the Telegram bot token: a credential"),
)


# ── Matching ─────────────────────────────────────────────────────────────────────────────────────

def _to_regex(pattern: str) -> re.Pattern:
    """A table pattern as a regex over a `$SOTTO_DATA`-relative path. `*` and `?` stay inside one
    path segment (so `cache/*.json` cannot reach `cache/sub/x.json`); a trailing `/**` means the
    directory and everything under it."""
    if pattern.endswith("/**"):
        head = re.escape(pattern[:-3])
        return re.compile(f"^{head}(/.*)?$")
    out = []
    for ch in pattern:
        out.append("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch))
    return re.compile("^" + "".join(out) + "$")


def protected(rel: str) -> bool:
    """True for a path NEVER touches. Checked below every glob, per path — the guard is the point."""
    rel = rel.replace(os.sep, "/")
    return any(rel == p or rel.startswith(p + "/") for p in NEVER)


def accounts_for(rel: str):
    """The table entry that covers this `$SOTTO_DATA`-relative path — a `Rule` when the sweep ages
    it, an `Exempt` when something else does (or nothing has to), None when the table has never
    heard of it. This is what makes "does forget.py agree with retention.py?" a test and not a
    reading exercise."""
    rel = rel.replace(os.sep, "/")
    for rule in SWEEP:
        if _to_regex(rule.pattern).match(rel):
            return rule
    for ex in EXEMPT:
        if _to_regex(ex.pattern).match(rel):
            return ex
    return None


# ── Ages ─────────────────────────────────────────────────────────────────────────────────────────

_DATE_IN_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TS_RE = re.compile(r"\A(\d{4}-\d{2}-\d{2})")


def _age_days(path: str, now: float) -> float:
    """How old the file is, in days. A `YYYY-MM-DD` in the basename is the file's own age claim and
    wins over mtime — a marker some later process touched is still a marker for ITS day."""
    m = _DATE_IN_NAME_RE.search(os.path.basename(path))
    if m:
        try:
            stamp = time.mktime(time.strptime(m.group(1), "%Y-%m-%d"))
            return (now - stamp) / 86400.0
        except ValueError:
            pass
    try:
        return (now - os.path.getmtime(path)) / 86400.0
    except OSError:
        return 0.0


def _line_is_young(line: str, cutoff: str) -> bool:
    """Keep a JSONL line iff its `ts` is on or after `cutoff` (a `YYYY-MM-DD` prefix compare — the
    tree writes every `ts` as an ISO-8601 UTC string, so lexical order IS chronological order).

    A line with no readable `ts` is KEPT. The sweep's job is to drop what it can prove is old;
    deleting a line it cannot read would make a parser bug into data loss."""
    try:
        row = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return True
    if not isinstance(row, dict):
        return True
    m = _TS_RE.match(str(row.get("ts") or ""))
    return True if not m else m.group(1) >= cutoff


# ── The policies ─────────────────────────────────────────────────────────────────────────────────

def _apply_delete(path: str, days: int, now: float) -> int:
    if _age_days(path, now) <= days:
        return 0
    os.remove(path)
    return 1


def _apply_drop_lines(path: str, days: int, now: float) -> int:
    """Rewrite the ledger without its old lines, atomically. Returns how many lines went.

    tmp → `os.replace` gives a new inode, which is safe HERE and would not be for the log: every
    writer of these ledgers opens, appends one line and closes (`sotto_log.bounded_append`), so
    none of them is holding the old inode when the swap lands. The log has a writer that may be —
    hence TRUNCATE_TAIL for it.

    Rewritten only when something actually leaves, so an untouched ledger keeps its inode and its
    mtime."""
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(now - days * 86400))
    # Under the `<path>.lock` sidecar flock (external review, Aug 31: read-then-replace without it
    # swallowed any line appended in between). The receiver's own appenders and event-triage's
    # budget writes take the same flock, so those cannot interleave; a skills-side append that runs
    # unlocked (google_action's receipt line) keeps a NARROW residue of the race — accepted, because
    # its writers never run at the 03:30 sweep and a fourth lock implementation deadlocked the tree
    # the day it was tried.
    with HOOKS["jsonl_lock"](path):
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        kept = [ln for ln in lines if not ln.strip() or _line_is_young(ln, cutoff)]
        dropped = len(lines) - len(kept)
        if dropped:
            HOOKS["write_text"](path, "".join(kept))
    return dropped


def _apply_truncate(path: str, max_bytes: int, _now: float) -> int:
    """Cut the log to its last `max_bytes`, IN PLACE (same inode: a brief mid-flight holds it open
    in append mode and must keep writing to the same file). Whole lines only — a tail that starts
    mid-line is a line nobody can parse. Returns bytes reclaimed."""
    size = os.path.getsize(path)
    if size <= max_bytes:
        return 0
    with open(path, "rb") as f:
        f.seek(size - max_bytes)
        tail = f.read()
    cut = tail.find(b"\n")
    tail = tail[cut + 1:] if cut != -1 else tail
    with open(path, "r+b") as f:
        f.write(tail)
        f.truncate(len(tail))
    return size - len(tail)


_APPLY = {
    DELETE_OLDER: _apply_delete,
    DROP_LINES_OLDER: _apply_drop_lines,
    TRUNCATE_TAIL: _apply_truncate,
}


# ── The sweep ────────────────────────────────────────────────────────────────────────────────────

def _root() -> str:
    return HOOKS["data_root"]()


def sweep(now: float | None = None) -> dict:
    """Apply every rule once. Best-effort PER PATH: a file the sweep can't read or write costs one
    log line and nothing else — retention is hygiene, and hygiene that can take a daemon down is
    worse than exhaust. Returns the summary the tick logs and the tests read."""
    root = _root()
    now = time.time() if now is None else now
    swept, errors = [], []
    for rule in SWEEP:
        for path in sorted(_glob.glob(os.path.join(root, *rule.pattern.split("/")))):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if protected(rel):   # unreachable by construction; the guard is the point
                errors.append({"path": rel, "error": "protected: this is memory, not exhaust"})
                continue
            try:
                n = _APPLY[rule.policy](path, rule.amount, now)
            except (OSError, ValueError, UnicodeDecodeError) as e:
                errors.append({"path": rel, "error": f"{type(e).__name__}: {e}"})
                continue
            if n:
                swept.append({"path": rel, "policy": rule.policy, "amount": n})
    return {"data_root": root, "swept": swept, "count": len(swept), "errors": errors}
