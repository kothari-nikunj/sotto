#!/usr/bin/env python3
"""
dashboard.py — The Window, M1–M3 (docs/plans/web-dashboard-the-window.md): security rails, the
read-only JSON API, the M2 write endpoints, and the M3 live views (calendar via an on-demand
gather, persisted research cards, the action-ledger timeline) behind the /app web dashboard.
Stdlib only, same posture as receiver.py.

Loaded by receiver.py via importlib (the relay.py/connectors.py pattern). The receiver wires
/app, /app/login, /static/* and /api/* into its handler through owns()/handle(), and injects
HOOKS — late-bound lambdas over ITS module globals — so this module never imports the receiver
back and test monkeypatches on the receiver (DATA, google_connected, …) are seen here too.

Security model (M1 is read-only; the CSRF token is minted now for M2's writes):
  * The setup code stays the bootstrap secret only. Presenting it once (the login form, a valid
    `?code=` on /app, or the wizard's `sotto_setup` cookie) mints a random 32-byte session token.
    Only sha256(token) is stored — $SOTTO_DATA/dashboard_sessions.json, written atomically at
    0600 through THE one write helper (connectors.write_json, wired in as HOOKS["write_json"];
    every JSON write in this image goes through it). Cookie: HttpOnly, Secure, SameSite=Lax.
  * 30-day idle expiry, checked on every authed request; last_seen bumped at most once/hour so a
    busy dashboard doesn't hammer the volume.
  * Brute-force damping: global in-memory counter, 5 failures → 60s lockout (single-user box).
    The setup surface itself has no counter today, so this covers only the dashboard login.
  * CSP (default-src 'self' + nosniff + no-referrer) on every dashboard response; the login page
    is the ONE page allowed inline styles ('unsafe-inline' in style-src only there — it renders
    before any static asset exists). /api/* additionally sends Cache-Control: no-store.
  * Kill switch: SOTTO_DASHBOARD=0 → every dashboard path answers 404.
  * Audit: login_ok / login_fail / lockout appended to $SOTTO_DATA/dashboard_audit.jsonl.
  * Reads ONLY knowledge/, briefs/, style.json, preferences.json. Never connectors/*.json token
    fields, never the setup code or any bearer in a response body. Static serving is a two-name
    whitelist — no path ever reaches the filesystem unvalidated.

Write model (M2 — "edits feed the flywheel, not a side channel"):
  * Every POST /api/* is session-gated AND CSRF-enforced: header X-Sotto-CSRF must equal the
    session's minted token (403 on mismatch/absence; SameSite=Lax already blocks the classic
    cases, this covers the rest).
  * Fact + loop writes execute knowledge_edit.py in the SKILLS TREE via subprocess (located by the
    receiver's HOOKS["find_script"], the _find_sotto_script pattern) — so a dashboard correction
    rides the identical knowledge_update.apply() / ledger code path a chat correction does. The
    receiver image carries no PyYAML; the skills tree does. Skills tree absent → 503.
  * Preferences have no chat-equivalent write path, so /api/prefs edits preferences.json directly
    (whitelisted lists only, atomic 0600 write). Deleting a RECOMPUTED rule also writes a
    `suppressed` tombstone so learn_preferences.py can't resurrect it — a rule you delete stays
    deleted.
  * Every successful write appends {ts, event: "write", endpoint, target, op} to
    dashboard_audit.jsonl. Input caps: text <= 500 chars; empty/whitespace ops rejected.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import html as _htmlmod
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta

# The ids this process must compute IDENTICALLY to the skills tree (queue lines, style samples).
# keys.py is a byte-identical vendored copy of sotto-chief-of-staff/_shared/lib/keys.py — the
# receiver image has to render the waiting room and the Voice card with no skills tree on the box,
# so it cannot import that one. tests/test_docs_drift.py fails if the two files diverge.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from keys import queue_key as _queue_key, sample_hash as _style_hash  # noqa: E402

# ── Wiring surface (receiver overrides these; the defaults keep the module import-safe) ──────────

def _unwired_write_json(*_a, **_k):
    raise RuntimeError("dashboard.HOOKS['write_json'] is unwired — load this module from receiver.py")


HOOKS = {
    "data_root": lambda: os.environ.get("SOTTO_DATA", "/data"),
    "setup_code": lambda: "",
    "bridge_connected": lambda: False,
    "last_event_at": lambda: None,
    "google_ok": lambda: False,
    "whatsapp_ok": lambda: False,
    "connector_status": lambda: [],
    "connector_error": lambda service: None,
    "connector_has_refresh": lambda service: True,
    # Locates a skills-tree script (receiver wires _find_sotto_script). None → the skills tree is
    # not on this box and every write that needs it answers 503.
    "find_script": lambda *rel: None,
    # THE atomic JSON write for the image — connectors.write_json, wired by the receiver
    # (tmp at `mode`, then os.replace). Unlike the degrade-quietly hooks above this one has no
    # sane default: silently dropping a session or a preference write is worse than a loud error.
    "write_json": _unwired_write_json,
    # Cadence write half: "nudge me now" on a held item. The receiver runs the funnel's own
    # `triage_event.py --promote` and, on an agent verdict, stages + spawns exactly as the release
    # valve does — this module never spawns anything. Unwired → the control is simply unavailable.
    "promote_queued": lambda key: {"ok": False, "error": "unavailable",
                                   "reason": "promotion isn't available on this box"},
    # "Run it now": fire one adapters/hermes/crons.json job by name, through the same runner cron
    # uses. `job_names` is the registered list (a gated-off job isn't in it, so the button can't
    # offer what the box won't run).
    "run_job": lambda name: {"ok": False, "error": "unavailable",
                             "reason": "runs aren't available on this box"},
    "job_names": lambda: [],
    # Delivery honesty for the Cadence panel — which channel briefs and nudges leave by, and
    # whether it is live right now (the existing _whatsapp_status wording: "linked (ever)").
    "delivery_channel": lambda: "whatsapp",
    "delivery_ready": lambda: False,
    "whatsapp_status": lambda: "pairing",
    # "A newer Sotto is published" for the Today banner: the receiver's daily update check, already
    # freshness-gated (receiver.update_notice). Unwired, or a check that has stopped succeeding →
    # {"available": False} and the banner simply isn't rendered.
    "update_notice": lambda: {"available": False},
    # The ONE receiver-side calendar cache (calcache.py) — {events, generated_at, cached} or None
    # when the skills tree is absent. /api/calendar OWNS no gather of its own any more: the same
    # fetch + 10-min TTL also feeds the refresh thread that writes cache/calendar_today.json for
    # the triage in-meeting hold ("two competing caches is how drift starts" — ROADMAP Step 2).
    "calendar_snapshot": lambda: None,
}

# Frontend assets live next to this file; a parallel build produces them. Tests point this at a
# fixture dir. Serving is whitelist-only (STATIC_FILES) — the name never touches path joins unless
# it is exactly one of these keys, so traversal is structurally impossible.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    # The Connections surface (/setup and its satellite pages in receiver.py) layers this on top of
    # app.css — one product, one set of design tokens across /app and /setup.
    "setup.css": "text/css; charset=utf-8",
    # Self-hosted typefaces (the CSP forbids Google Fonts). The "fonts/" prefix is part of the
    # whitelist KEY — still an exact-match lookup, so traversal stays structurally impossible.
    # Besley (reading serif) + Martian Mono (label layer) — variable, latin subset, OFL.
    "fonts/Besley.woff2": "font/woff2",
    "fonts/Besley-italic.woff2": "font/woff2",
    "fonts/MartianMono.woff2": "font/woff2",
    # The two interactive documentation playgrounds. Their ONE source of truth is docs/; the
    # Dockerfile copies them in beside the frontend assets at image build (the least-machinery
    # option — the alternative, a second static root resolved at runtime, cannot be expressed as
    # one relative path that is correct in both the source tree and the flattened image layout).
    # Self-contained single files: inline CSS + inline JS, zero network. They get their own CSP
    # below, and no session — they carry no user data, only the rules the code already documents.
    "playground-architecture.html": "text/html; charset=utf-8",
    "playground-feedback-loops.html": "text/html; charset=utf-8",
}

SESSION_IDLE_SECS = 30 * 24 * 3600     # 30-day idle expiry
LAST_SEEN_BUMP_SECS = 3600             # bump last_seen at most hourly (limits volume writes)
LOCKOUT_AFTER = 5                      # failed codes before the lockout engages
LOCKOUT_SECS = 60
LOGIN_BODY_MAX = 8192                  # a form with one short code; anything bigger is garbage
API_BODY_MAX = 16384                   # write bodies: an op + <=500 chars of text
TEXT_MAX = 500                         # fact/preference text cap (the plan's input cap)
EDIT_TIMEOUT_SECS = 30                 # knowledge_edit.py subprocess budget
PEOPLE_INDEX_TTL_SECS = 600            # email → person join cache, alongside the calendar cache
LEDGER_DEFAULT_DAYS = 7                # /api/ledger window default
LEDGER_MAX_DAYS = 30                   # /api/ledger window cap
LEDGER_MAX_ROWS = 240                  # /api/ledger row cap after the merge
# Per-source shares, applied BEFORE the merge (newest rows of each source). The Record braids three
# streams (four since delivery receipts joined them) and they grow at wildly different rates — a chatty day of triage verdicts is dozens per
# hour, drafts and outcomes a handful per day — so one shared 200-row cap let triage push the other
# two off the page entirely. The shares sum to LEDGER_MAX_ROWS, which stays as the backstop.
# In one sentence: no single source can flood the Record.
LEDGER_MAX_PER_SOURCE = {"triage": 120, "outcome": 40, "dashboard": 40, "delivery": 40}

# ── Cadence panel bounds + the constants it MIRRORS from the funnel ──────────────────────────────
# The receiver image cannot import the skills tree (it may not be on the box at all), so the three
# values below are copies of triage_event.py's. They are read-only display/enable logic — every
# actual promotion is decided by triage_event.py --promote, which owns the rule. Change one, change
# both (the same discipline _local_today already carries for the timezone chain).
QUEUE_TAIL_LINES = 50            # how much of queue.jsonl the waiting room reads (newest N lines)
QUEUE_SHOWN = 15                 # …and how many of those it renders
PROMOTABLE_CLASSES = frozenset({"quiet", "cooldown", "stale", "budget", "meeting_hold"})
BUDGET_EXEMPT_CLASSES = frozenset({"missed_call", "escalation", "post_meeting"})
VALVE_MAX_AGE_MIN_DEFAULT = 240  # mirrors triage_event.VALVE_MAX_AGE_MIN — a real ask from 3h ago
#                                  still deserves a nudge, a 2-day-old one doesn't; "meeting_hold"
#                                  entries ignore it (a long meeting must not expire an ask).
VALVE_MAX_PER_HOUR_DEFAULT = 2   # mirrors triage_event.VALVE_MAX_PER_HOUR — the valve is a trickle,
#                                  not a flood; promotions spend the daily budget like any nudge.
# Sotto's own held nudges are never offerable here: the event skill has no branch for one, so
# "nudge me now" on a queued birthday would ask it to draft a reply to a birthday. Mirrors
# triage_event.PROACTIVE_SOURCE, which refuses the same rows for real in _valve_candidate.
PROACTIVE_SOURCE = "proactive"
CHASE_MAX_DEFAULT = 2            # mirrors continuity_resolve.CHASE_MAX — after two, it's your call
# Voice card: how many observed samples the Confirm control may offer, and how much of each is
# rendered. Style samples are VERBATIM messages the user sent, so the card shows a bounded, recent
# slice rather than the whole fingerprint.
VOICE_CANDIDATES_MAX = 12
VOICE_TEXT_MAX = 240
MERGE_SUGGESTIONS_SHOWN = 12     # the People card's cap on knowledge/merge_suggestions.json
ATTENTION_SHOWN = 12             # …and on relationship_state.json's attention queue
SNOOZE_SPEC_MAX = 32             # a snooze spec is "tomorrow" / "+2h" / "3pm" / an ISO stamp
QUEUE_KEY_RE = re.compile(r"\A[0-9a-f]{16}\Z")
STYLE_KEY_RE = re.compile(r"\A[0-9a-f]{16}\Z")
# The run-now jobs, keyed by the crons.json job name that IS their definition. `gate` is the
# volume marker that answers "has it already happened today?" — for a brief that is brief_marker's
# deliver-once flag, and the digest self-gates instead (see api_runs).
RUN_JOBS = (("sotto-morning-brief", "morning"),
            ("sotto-evening-brief", "evening"),
            ("sotto-midday-digest", "digest"))
CLAIM_STALE_SECS = 30 * 60       # mirrors receiver.CLAIM_STALE_SECS — a claim older than this died

SLUG_RE = re.compile(r"\A[a-z0-9_-]{1,128}\Z")
DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
KIND_RE = re.compile(r"\A[a-z0-9_-]{1,32}\Z")
BRIEF_FILE_RE = re.compile(r"\A(\d{4}-\d{2}-\d{2})_([a-z0-9_-]{1,32})\.json\Z")
PERSON_API_RE = re.compile(r"\A/api/people/([a-z0-9_-]{1,128})\Z")
PERSON_FACTS_RE = re.compile(r"\A/api/people/([a-z0-9_-]{1,128})/facts\Z")
PERSON_RELATIONS_RE = re.compile(r"\A/api/people/([a-z0-9_-]{1,128})/relations\Z")
FACT_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
MEMORY_TYPE_RE = re.compile(r"\A[a-z_]{1,32}\Z")
# Real anchor keys carry colons ("email:reply:name:sarah chen", "thread:<gmail id>") plus @/./
# spaces in the id:/name: forms — so anchors travel in the JSON BODY (no URL-encoding pain) and
# are validated as printable-no-control. They are only ever compared against ledger frontmatter
# (by knowledge_edit.py), never joined into a filesystem path.
ANCHOR_RE = re.compile(r"\A[^\x00-\x1f\x7f]{1,256}\Z")

ACTIVE_LOOP_STATUSES = frozenset({"open", "waiting", "failed", "blocked"})
# Meeting-prep/meeting-info entries are calendar shadows, not asks — the docket is their surface.
# They never leave /api/loops (and so never count in the overview), even when old volume files
# still carry them.
EXCLUDED_LOOP_ACTION_TYPES = frozenset({"meeting_prep", "meeting_info"})

# preferences.json real shape (learn_preferences.py + preferences.py): top-level rule lists
# `deprioritization_hints` / `edit_heavy` (["contact|action_type", ...]), dict `approval_defaults`
# ({"contact|action_type": tier}), and the user-stated `explicit` block's four lists. No rule
# carries an enabled flag, so the one safe op is delete; `analytics`/`version`/`style` stay
# untouchable. Deleting from a dict list uses the entry VALUE as its id (stable across the
# learner's wholesale rewrites, unlike an index). PREF_TOP_LISTS + PREF_DICTS are the RECOMPUTED
# ones — deleting from those also writes a `suppressed` tombstone (see _post_prefs).
PREF_TOP_LISTS = frozenset({"deprioritization_hints", "edit_heavy"})
PREF_EXPLICIT_LISTS = frozenset({"mute_senders", "mute_people", "mute_sections", "tone_notes",
                                 "vip_people"})
PREF_DICTS = frozenset({"approval_defaults"})
# The explicit block's lists DO have a chat-equivalent writer — preferences.py, the CLI the
# sotto-feedback skill uses — so adds and removes there ride it (see _run_prefs). These are its CLI
# verbs: {list: (add-verb, remove-verb)}. The recomputed lists above have no such verb, which is
# exactly why _post_prefs still edits them directly (and tombstones them).
# An empty remove-verb means the CLI has no PER-VALUE removal for that list (preferences.py's
# `clear-tone` clears all tone notes, which is a different act) — those deletes fall back to this
# module's own atomic edit, exactly as they did before.
PREF_EXPLICIT_VERBS = {
    "mute_senders": ("mute-sender", "unmute-sender"),
    "mute_people": ("mute-person", "unmute-person"),
    "mute_sections": ("mute-section", "unmute-section"),
    "vip_people": ("vip", "unvip"),
    "tone_notes": ("tone", ""),
}
# The Add form offers only the lists with a deterministic verb AND a rule you can state in one
# sentence. tone_notes is deliberately absent: "keep it terse" is a note to the writer, not a
# switch, and it belongs in the conversation that produced it.
PREF_ADDABLE = ("mute_senders", "mute_people", "mute_sections", "vip_people")

# XSS note (the plan's rule 4): every byte of user-adjacent data leaves this module as JSON only.
# The two HTML surfaces (_login_page, the assets-missing stub) contain zero user data.

_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; frame-ancestors 'none'")
_CSP_LOGIN = ("default-src 'self'; script-src 'self'; style-src 'unsafe-inline'; "
              "img-src 'self' data:; frame-ancestors 'none'")
# The self-contained doc playgrounds (STATIC_FILES' two .html entries). Inline CSS + inline JS is
# what "one file, works offline" MEANS, so those two are allowed — and in exchange this policy is
# tighter than _CSP everywhere else: `default-src 'none'` forbids every network fetch the app
# policy's 'self' permits (no connect, no fetch, no XHR, no websocket, no font, no frame), and
# base-uri/form-action/frame-ancestors are pinned because they do NOT fall back to default-src.
_CSP_DOC = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _root() -> str:
    return HOOKS["data_root"]()


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if ts is None else ts))


def _from_iso(s):
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


# ── Tolerant frontmatter parser (stdlib; no PyYAML — the receiver image doesn't carry it) ────────
# Parses the exhaust-schema subset: `key: value` scalars, inline ["a","b"] lists, nested maps by
# indentation (facts: → f_id: → fields), and block sequences (`relations:` → `- type: …` with the
# item's remaining fields indented under it). Anything it can't read is skipped, never raised — the
# skills-tree knowledge.py (which hard-imports yaml) stays the writer; this is a read-only window.

def _parse_scalar(s: str):
    s = s.strip()
    if not s:
        return ""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        try:
            return json.loads(s)
        except (json.JSONDecodeError, ValueError):
            inner = s[1:-1].strip()
            return [_parse_scalar(x) for x in inner.split(",")] if inner else []
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    return s


def parse_frontmatter(text: str):
    """`---\\n…\\n---\\n<body>` → (meta dict, body str). Tolerant: no frontmatter → ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    root: dict = {}
    stack: list[tuple[int, object]] = [(-1, root)]
    # The last key whose value was empty — the only key a following `- ` line can belong to. It is
    # what turns `relations:` + dashed lines into a list instead of an empty map.
    pending: tuple = ()
    for raw in lines[1:end]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        if stripped.startswith("- "):
            # A sequence item. safe_dump writes these at the SAME indent as their key; other
            # writers indent them — both land here, neither can be deeper than a nested map.
            if not pending or indent < pending[2]:
                continue
            holder, key, _ = pending
            if not isinstance(holder.get(key), list):
                holder[key] = []
            item_text = stripped[2:].strip()
            if ":" in item_text and not item_text.startswith(("\"", "'", "[")):
                k2, _, v2 = item_text.partition(":")
                item: dict = {}
                k2 = k2.strip().strip("\"'")
                if k2 and v2.strip() != "":
                    item[k2] = _parse_scalar(v2)
                holder[key].append(item)
                stack.append((indent, item))    # the item's remaining fields are indented under it
            else:
                holder[key].append(_parse_scalar(item_text))
            continue
        if ":" not in stripped:
            continue
        parent = stack[-1][1]
        if not isinstance(parent, dict):
            continue
        key, _, val = stripped.partition(":")
        key = key.strip().strip("\"'")
        if not key:
            continue
        if val.strip() == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
            pending = (parent, key, indent)
        else:
            parent[key] = _parse_scalar(val)
    return root, "\n".join(lines[end + 1:])


# ── Sessions ($SOTTO_DATA/dashboard_sessions.json: sha256(token) → record) ───────────────────────

_SESS_LOCK = threading.Lock()


def _sessions_path() -> str:
    return os.path.join(_root(), "dashboard_sessions.json")


def _load_sessions() -> dict:
    try:
        with open(_sessions_path(), encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_sessions(sess: dict) -> None:
    """THE atomic 0600 write (HOOKS["write_json"] → connectors.write_json): a crash mid-write can't
    corrupt the store, and the volume never holds it world-readable."""
    HOOKS["write_json"](_sessions_path(), sess)


def mint_session() -> str:
    """Mint a session: 32 random bytes out to the cookie, only the hash (plus a per-session CSRF
    token for M2) on disk. Expired sessions are pruned on the way through. Returns the raw token."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    now_iso = _iso()
    cutoff = time.time() - SESSION_IDLE_SECS
    with _SESS_LOCK:
        sess = {k: v for k, v in _load_sessions().items()
                if isinstance(v, dict) and (_from_iso(v.get("last_seen")) or 0) >= cutoff}
        sess[digest] = {"created": now_iso, "last_seen": now_iso,
                        "csrf": secrets.token_urlsafe(32)}
        try:
            _write_sessions(sess)
        except OSError:
            pass  # unwritable volume: the mint still answers, the session just won't persist
    return token


def _cookie(h, name: str):
    for part in (h.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name and v:
            return v
    return None


def _session_record(h):
    """The live session record for this request's sotto_session cookie, or None. Enforces the
    30-day idle expiry (expired entries are dropped) and bumps last_seen at most once an hour."""
    token = _cookie(h, "sotto_session")
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _SESS_LOCK:
        sess = _load_sessions()
        rec = sess.get(digest)
        if not isinstance(rec, dict):
            return None
        last = _from_iso(rec.get("last_seen")) or 0
        if now - last > SESSION_IDLE_SECS:
            sess.pop(digest, None)
            try:
                _write_sessions(sess)
            except OSError:
                pass
            return None
        if now - last >= LAST_SEEN_BUMP_SECS:
            rec["last_seen"] = _iso(now)
            try:
                _write_sessions(sess)
            except OSError:
                pass
        return rec


# ── Brute-force damping + audit ──────────────────────────────────────────────────────────────────

_LOGIN_LOCK = threading.Lock()
_LOGIN_STATE = {"fails": 0, "locked_until": 0.0}


def _lockout_remaining() -> float:
    with _LOGIN_LOCK:
        return max(0.0, _LOGIN_STATE["locked_until"] - time.time())


def _note_login_failure() -> bool:
    """Count a failed code presentation. True when THIS failure engaged the lockout."""
    with _LOGIN_LOCK:
        _LOGIN_STATE["fails"] += 1
        if _LOGIN_STATE["fails"] >= LOCKOUT_AFTER:
            _LOGIN_STATE["locked_until"] = time.time() + LOCKOUT_SECS
            _LOGIN_STATE["fails"] = 0
            return True
    return False


def _note_login_success() -> None:
    with _LOGIN_LOCK:
        _LOGIN_STATE["fails"] = 0


def _audit(event: str, **fields) -> None:
    """Append {ts, event, **fields} to $SOTTO_DATA/dashboard_audit.jsonl (best-effort, never
    raises). Writes use _audit("write", endpoint=…, target=…, op=…) per the plan's audit rail."""
    try:
        path = os.path.join(_root(), "dashboard_audit.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _iso(), "event": event, **fields}) + "\n")
    except OSError:
        pass


# ── Response plumbing (every dashboard byte leaves through these — headers are non-optional) ─────

def _headers(api: bool = False, login: bool = False, doc: bool = False) -> list:
    hs = [("Content-Security-Policy", _CSP_DOC if doc else (_CSP_LOGIN if login else _CSP)),
          ("X-Content-Type-Options", "nosniff"),
          ("Referrer-Policy", "no-referrer")]
    if api:
        hs.append(("Cache-Control", "no-store"))
    return hs


def _respond(h, code: int, ctype, data, headers=()):
    try:
        h.send_response(code)
        for k, v in headers:
            h.send_header(k, v)
        if ctype is not None and data is not None:
            h.send_header("Content-Type", ctype)
            h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        if data is not None:
            h.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
        pass  # client hung up — expected, not an error (same posture as receiver._write)


def _json(h, code: int, obj: dict):
    _respond(h, code, "application/json", json.dumps(obj).encode(), _headers(api=True))


def _page(h, code: int, markup: str, login: bool = False):
    _respond(h, code, "text/html; charset=utf-8", markup.encode(), _headers(login=login))


def _login_redirect(h, token: str):
    """302 → clean /app carrying the freshly minted session cookie. The setup code NEVER appears
    in a dashboard URL — this redirect is what scrubs it after ?code=/cookie auto-login."""
    _respond(h, 302, None, None,
             _headers() + [("Location", "/app"),
                           ("Set-Cookie", f"sotto_session={token}; Path=/; HttpOnly; Secure; "
                                          "SameSite=Lax")])


# ── Login page (the one inline-CSS exception; zero user data, zero JS) ───────────────────────────

def _login_page(msg: str = "") -> str:
    # The dashboard's design language (oat parchment / iron ink / banker's-green, Besley serif),
    # self-contained: inline CSS only (this page's CSP allows inline styles and nothing else).
    # /static/* serves without a session, so the vendored fonts CAN be declared here via
    # @font-face — font-src falls back to default-src 'self'. Georgia covers a cold start.
    err = f"<p class='err'>{_htmlmod.escape(msg)}</p>" if msg else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='color-scheme' content='light dark'>"
        "<title>Sotto — sign in</title><style>"
        "@font-face{font-family:'Besley';src:url('/static/fonts/Besley.woff2') format('woff2');"
        "font-weight:400 900;font-style:normal;font-display:swap}"
        "@font-face{font-family:'Besley';src:url('/static/fonts/Besley-italic.woff2') format('woff2');"
        "font-weight:400 900;font-style:italic;font-display:swap}"
        "@font-face{font-family:'Martian Mono';src:url('/static/fonts/MartianMono.woff2') format('woff2');"
        "font-weight:100 800;font-style:normal;font-display:swap}"
        # The panel system, hand-inlined: a borderless surface one shade off the
        # oat ground (app.css --panel/--panel-lift), display-scale wordmark, and
        # the eyebrow register on the label. Values MIRROR app.css's tokens —
        # this page cannot link a stylesheet, so the numbers live twice on
        # purpose; app.css is the source of truth for both.
        "body{font-family:'Besley',Georgia,serif;background:#f4efe4;color:#221c12;"
        "max-width:420px;margin:14vh auto 0;padding:0 20px;line-height:1.62;font-size:17px}"
        ".card{background:#ece7d9;border-radius:12px;padding:32px 28px 28px}"
        "h1{font-size:32px;font-style:italic;font-weight:700;letter-spacing:-.024em;"
        "line-height:1.05;margin:0 0 8px}"
        "h1 span{color:#26604e;font-style:normal}"
        "p{color:#6b6250;font-style:italic;margin:0 0 28px;font-size:17px;line-height:1.55}"
        "label{display:block;font-family:'Martian Mono',ui-monospace,monospace;font-size:11px;"
        "font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:#746b57;margin:0 0 10px}"
        "input{width:100%;box-sizing:border-box;padding:12px 14px;border:1px solid #ddd4c0;"
        "border-radius:6px;background:#fbf8f0;color:#221c12;"
        "font-family:'Martian Mono',ui-monospace,monospace;font-size:16px}"
        "input:focus{outline:none;border-color:#26604e;box-shadow:0 0 0 3px rgba(38,96,78,.1)}"
        "button{margin-top:16px;width:100%;padding:13px;border-radius:6px;border:none;"
        "box-shadow:inset 0 0 0 1px rgba(38,96,78,.4);"
        "background:#fbf8f0;color:#221c12;font-family:'Martian Mono',ui-monospace,monospace;"
        "font-size:11px;text-transform:uppercase;letter-spacing:.1em;font-weight:600;"
        "cursor:pointer}button:hover{background:rgba(38,96,78,.055)}"
        "button:active{transform:scale(.98)}"
        ".err{font-family:'Martian Mono',ui-monospace,monospace;font-style:normal;font-size:11px;"
        "text-transform:uppercase;letter-spacing:.1em;color:#a4321f;margin:0 0 16px}"
        "@media(prefers-color-scheme:dark){body{background:#191510;color:#eae3d3}"
        ".card{background:#211c15}"
        "p{color:#a89d86}label{color:#998e75}.err{color:#e2705a}"
        "h1 span{color:#95b2a4}"
        "input{background:#2a241b;border-color:#362e20;color:#eae3d3}"
        "input:focus{border-color:#64887a;box-shadow:0 0 0 3px rgba(149,178,164,.12)}"
        "button{background:#2a241b;box-shadow:inset 0 0 0 1px rgba(149,178,164,.5);"
        "color:#eae3d3}"
        "button:hover{background:rgba(149,178,164,.07)}}"
        "</style></head><body><div class='card'>"
        "<h1>Sotto<span>.</span></h1><p>Your chief of staff keeps this door locked.</p>"
        f"{err}"
        "<form method='post' action='/app/login'>"
        "<label for='code'>Setup code — printed in your deploy logs</label>"
        "<input id='code' name='code' type='password' placeholder='••••••••••••' "
        "autocomplete='current-password' "
        "autocapitalize='off' autocorrect='off' spellcheck='false' autofocus>"
        "<button>Open the dashboard</button></form></div></body></html>")


# ── Route dispatch (receiver.do_GET/do_POST call owns() then handle()) ───────────────────────────

def owns(path: str) -> bool:
    return (path in ("/app", "/app/login")
            or path.startswith("/static/")
            or path.startswith("/api/"))


def handle(h, method: str, path: str):
    # Kill switch first: SOTTO_DASHBOARD=0 makes the whole surface indistinguishable from absent.
    if (os.environ.get("SOTTO_DASHBOARD") or "").strip() == "0":
        return _json(h, 404, {"error": "not found"})
    if method == "POST":
        if path == "/app/login":
            return _handle_login(h)
        if path.startswith("/api/"):
            return _handle_api_post(h, path)
        return _json(h, 404, {"error": "not found"})
    if path == "/app":
        return _handle_app(h)
    if path == "/app/login":  # GETting the login endpoint just lands you on /app's login flow
        return _respond(h, 302, None, None, _headers() + [("Location", "/app")])
    if path.startswith("/static/"):
        return _handle_static(h, path)
    return _handle_api(h, path)


def _handle_app(h):
    if _session_record(h) is not None:
        return _serve_app_html(h)
    # Auto-login: a valid ?code= or the wizard's sotto_setup cookie is the same proof as the login
    # form — a user arriving from /setup must not type the code twice. Both mint a real session and
    # 302 to the CLEAN /app URL (the code never lives in dashboard links/history).
    q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
    supplied = _s((q.get("code") or [""])[0])
    want = (HOOKS["setup_code"]() or "").encode()
    if want and _lockout_remaining() <= 0:
        wizard = _s(_cookie(h, "sotto_setup") or "")
        for cand in (supplied, wizard):
            if cand and hmac.compare_digest(cand.encode(), want):
                _note_login_success()
                _audit("login_ok")
                return _login_redirect(h, mint_session())
        if supplied:  # an explicit wrong ?code= is a brute-force attempt — it counts
            _audit("login_fail")
            if _note_login_failure():
                _audit("lockout")
    return _page(h, 200, _login_page(), login=True)


def _read_form(h) -> dict:
    try:
        n = int(h.headers.get("Content-Length", 0))
    except ValueError:
        return {}
    if n <= 0 or n > LOGIN_BODY_MAX:
        return {}
    return urllib.parse.parse_qs(h.rfile.read(n).decode("utf-8", "replace"))


def _handle_login(h):
    if _lockout_remaining() > 0:
        return _page(h, 429, _login_page("Too many attempts — try again in a minute."), login=True)
    supplied = _s((_read_form(h).get("code") or [""])[0])
    want = (HOOKS["setup_code"]() or "").encode()
    if want and supplied and hmac.compare_digest(supplied.encode(), want):
        _note_login_success()
        _audit("login_ok")
        return _login_redirect(h, mint_session())
    _audit("login_fail")
    if _note_login_failure():
        _audit("lockout")
        return _page(h, 429, _login_page("Too many attempts — try again in a minute."), login=True)
    return _page(h, 403, _login_page("That code didn't match."), login=True)


def _serve_app_html(h):
    try:
        with open(os.path.join(STATIC_DIR, "app.html"), "rb") as f:
            data = f.read()
    except OSError:  # frontend not built/deployed yet — still a working (if bare) page
        data = b"<!doctype html><title>Sotto</title><p>dashboard assets missing</p>"
    return _respond(h, 200, "text/html; charset=utf-8", data, _headers())


def _handle_static(h, path: str):
    """Whitelist-only static serving: the request name must EQUAL a STATIC_FILES key before any
    filesystem path is built, so ../ (encoded or not) can never traverse — it just isn't a key."""
    name = path[len("/static/"):]
    ctype = STATIC_FILES.get(name)
    if ctype is None:
        return _json(h, 404, {"error": "not found"})
    try:
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            data = f.read()
    except OSError:
        return _respond(h, 200, "text/plain; charset=utf-8", b"dashboard assets missing", _headers())
    # The two doc playgrounds are single self-contained files, so they take the doc CSP (inline
    # CSS/JS allowed, the network forbidden outright). Everything else keeps the app policy.
    hdrs = _headers(doc=ctype.startswith("text/html"))
    if name.startswith("fonts/"):   # immutable vendored files — spare the phone a re-download
        hdrs.append(("Cache-Control", "public, max-age=604800, immutable"))
    else:                           # js/css must revalidate, or a redeploy ships a stale UI
        hdrs.append(("Cache-Control", "no-cache"))
    return _respond(h, 200, ctype, data, hdrs)


# ── Read-only JSON API (all session-gated; tolerant readers — missing data is empty, never 500) ──

def _handle_api(h, path: str):
    rec = _session_record(h)
    if rec is None:
        return _json(h, 401, {"error": "unauthorized"})
    try:
        if path == "/api/session":
            return _json(h, 200, {"csrf": _s(rec.get("csrf"))})
        if path == "/api/overview":
            return _json(h, 200, api_overview())
        if path == "/api/loops":
            return _json(h, 200, api_loops())
        if path == "/api/briefs":
            return _api_briefs(h)
        if path == "/api/people":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
            return _json(h, 200, api_people((q.get("q") or [""])[0]))
        m = PERSON_API_RE.match(path)
        if m:
            obj = api_person(m.group(1))
            return _json(h, 200, obj) if obj is not None else _json(h, 404, {"error": "not found"})
        if path == "/api/learned":
            return _json(h, 200, api_learned())
        if path == "/api/cadence":
            return _json(h, 200, api_cadence())
        if path == "/api/graph":
            return _json(h, 200, api_graph())
        if path == "/api/voice":
            return _json(h, 200, api_voice())
        if path == "/api/runs":
            return _json(h, 200, api_runs())
        if path == "/api/calendar":
            return _json(h, 200, api_calendar())
        if path == "/api/research":
            return _json(h, 200, api_research())
        if path == "/api/ledger":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
            return _json(h, 200, api_ledger((q.get("days") or [""])[0]))
        return _json(h, 404, {"error": "not found"})
    except Exception as e:  # noqa: BLE001 — a data-shape surprise must not 500 the dashboard
        print(f"[sotto] dashboard api error on {path}: {e}", flush=True)
        return _json(h, 200, {"error": "unreadable"})


def _read_json_file(*parts, default=None):
    try:
        with open(os.path.join(_root(), *parts), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def _list_md(dirname: str):
    """[(slug, frontmatter)] for knowledge/<dirname>/*.md — unreadable/unparseable files skipped."""
    d = os.path.join(_root(), "knowledge", dirname)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = []
    for n in names:
        if not n.endswith(".md"):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                meta, _ = parse_frontmatter(f.read())
        except OSError:
            continue
        out.append((n[:-3], meta))
    return out


def _local_today() -> str:
    """The user's local date. CANONICAL TZ ORDER, the same chain everywhere: SOTTO_TIMEZONE → TZ →
    $SOTTO_DATA/config/settings.json → server local. Every wall-clock feature resolves the day the
    same way — the skills tree's `timeutil.configured_tz()` is the canonical implementation, and this
    mirrors it because the receiver image can't import the skills tree (it may not be on the box at
    all); receiver._configured_tz_name() and start.sh's step 2 are the other two copies of the chain.
    Change one, change them all."""
    tz = _s(os.environ.get("SOTTO_TIMEZONE") or os.environ.get("TZ") or "")
    if not tz:
        settings = _read_json_file("config", "settings.json", default={}) or {}
        tz = _s(settings.get("timezone")) if isinstance(settings, dict) else ""
    if tz:
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415 — stdlib; imported lazily, may miss tzdata
            return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            pass
    return time.strftime("%Y-%m-%d")


def _brief_files():
    """[(date, kind, filename)] for briefs/<date>_<kind>.json, newest first. The trigger's
    .claim/.delivered/.payload.json siblings live in the same dir and are excluded by the regex."""
    try:
        names = os.listdir(os.path.join(_root(), "briefs"))
    except OSError:
        return []
    out = [(m.group(1), m.group(2), n) for n in names if (m := BRIEF_FILE_RE.match(n))]
    out.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return out


def _granola_state() -> str:
    """'ok' | 'reconnect' | 'absent' — the same honesty rules as the /setup tile: an error file
    from the gather, or an expired token with no refresh token, means Reconnect, not ok."""
    try:
        for s in HOOKS["connector_status"]():
            if s.get("service") != "granola":
                continue
            if not s.get("connected"):
                return "absent"
            if HOOKS["connector_error"]("granola") is not None:
                return "reconnect"
            exp = s.get("expires_at")
            if exp and exp < time.time() and not HOOKS["connector_has_refresh"]("granola"):
                return "reconnect"
            return "ok"
    except Exception:  # noqa: BLE001
        pass
    return "absent"


def _hook_bool(name: str) -> bool:
    try:
        return bool(HOOKS[name]())
    except Exception:  # noqa: BLE001
        return False


def _update_notice() -> dict:
    """The Today banner's whole input, normalized here so the page can trust three string fields.
    Anything unexpected — an unwired hook, a raised exception, a non-dict, a non-https doc link —
    reads as "no update", because a banner nobody can explain is worse than no banner."""
    try:
        raw = HOOKS["update_notice"]()
    except Exception:  # noqa: BLE001
        return {"available": False}
    if not isinstance(raw, dict) or raw.get("available") is not True:
        return {"available": False}
    version, current, url = _s(raw.get("version")), _s(raw.get("current")), _s(raw.get("url"))
    if not version or not url.startswith("https://"):
        return {"available": False}
    return {"available": True, "version": version, "current": current, "url": url}


def api_overview() -> dict:
    today = _local_today()
    try:
        last_event = HOOKS["last_event_at"]()
    except Exception:  # noqa: BLE001
        last_event = None
    return {
        "date": today,
        "briefs_today": [{"kind": k, "file": f} for (d, k, f) in _brief_files() if d == today],
        "loops_active": len(api_loops()["loops"]),
        "last_event_at": last_event,
        "bridge_connected": _hook_bool("bridge_connected"),
        "services": {
            "google": _hook_bool("google_ok"),
            "whatsapp": _hook_bool("whatsapp_ok"),
            "granola": _granola_state(),
        },
        # Housekeeping, not an alert: one subdued line on Today when a newer Sotto is published.
        "update": _update_notice(),
    }


def api_loops() -> dict:
    d = os.path.join(_root(), "knowledge", "continuity")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        names = []
    loops = []
    for n in names:
        if not n.endswith(".md"):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                meta, _ = parse_frontmatter(f.read())
        except OSError:
            continue
        status = _s(meta.get("status")).lower()
        if status not in ACTIVE_LOOP_STATUSES:
            continue
        action_type = _s(meta.get("action_type"))
        if re.sub(r"[\s-]+", "_", action_type.lower()) in EXCLUDED_LOOP_ACTION_TYPES:
            continue
        surfaced = meta.get("times_surfaced")
        chased = meta.get("chased_count")
        chased = chased if isinstance(chased, int) else 0
        loops.append({
            "anchor_key": _s(meta.get("anchor_key")),
            "action_type": action_type,
            "channel": _s(meta.get("channel")),
            "contact_name": _s(meta.get("contact_name")),
            "status": status,
            "created_at": _s(meta.get("created_at")),
            "times_surfaced": surfaced if isinstance(surfaced, int) else 0,
            "summary": _s(meta.get("summary")),
            "meeting_time": _s(meta.get("meeting_time")) or None,
            # The one editable field: for something YOU owe, continuity_resolve expires it 2 days
            # past its deadline, so the deadline IS its "later" — there is no separate snooze state.
            # (Something you're OWED is chased instead, never expired by its deadline.)
            "deadline": _s(meta.get("deadline"))[:10] or None,
            "source": _s(meta.get("source")) or None,
            # Chase state — written ONLY by continuity_resolve. The dashboard is positioned as a
            # complete alternative to chat, and chat can already say "chased once, Tuesday"; without
            # these four fields this page could not. `chased_out` is the hand-off: chased its two
            # times with no answer, so it is the user's call now (nudge again, or let it go).
            "chased_count": chased,
            "last_chased_at": _s(meta.get("last_chased_at"))[:10] or None,
            "chase_after": _s(meta.get("chase_after"))[:10] or None,
            "chased_out": chased >= CHASE_MAX_DEFAULT,
        })
    loops.sort(key=lambda x: x["created_at"], reverse=True)
    return {"loops": loops}


def _api_briefs(h):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
    date = _s((q.get("date") or [""])[0])
    kind = _s((q.get("kind") or [""])[0])
    if not date and not kind:
        return _json(h, 200, {"briefs": [{"date": d, "kind": k} for (d, k, _) in _brief_files()]})
    # Strict param validation BEFORE any path is built (date/kind feed a filename).
    if not DATE_RE.match(date) or not KIND_RE.match(kind):
        return _json(h, 400, {"error": "bad date/kind"})
    data = _read_json_file("briefs", f"{date}_{kind}.json", default=None)
    if data is None:
        return _json(h, 404, {"error": "not found"})
    # chat_text: the delivered chat rendering when the archive carries it. compose_brief emits it
    # as `brief_text` (render_chat_text of brief_markdown); older/other shapes may say chat_text.
    # Nothing stored → null, and the frontend renders from `data` itself.
    chat = None
    if isinstance(data, dict):
        for key in ("chat_text", "brief_text"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                chat = v
                break
    return _json(h, 200, {"date": date, "kind": kind, "data": data, "chat_text": chat})


def _title_from_slug(slug: str) -> str:
    """"acme-corp" → "Acme Corp" — the display fallback when frontmatter carries no name."""
    return " ".join(w.capitalize() for w in re.split(r"[_-]+", slug) if w)


def _body_sections(body: str):
    """Company markdown body → [{heading, text}]. Tolerant: `## Heading` (any # depth) starts a
    section; a headingless body becomes one unnamed section; heading-only sections are dropped.
    Text passes through verbatim — the frontend renders it as textContent, so any markdown that
    survives is inert."""
    sections = []
    heading, buf = "", []

    def flush():
        text = "\n".join(buf).strip()
        if text:
            sections.append({"heading": heading, "text": text})

    for line in (body or "").split("\n"):
        m = re.match(r"\A#{1,6}\s+(.*\S)\s*\Z", line)
        if m:
            flush()
            heading, buf = m.group(1), []
        else:
            buf.append(line)
    flush()
    return sections


# ── Relations: the receiver-side half of the closed vocabulary ───────────────────────────────────
# THE table lives in the skills tree (_shared/knowledge/knowledge.py — RELATION_INVERSE /
# RELATION_SENTENCE). This is its one other copy, and it exists only because the receiver image
# cannot import the skills tree (it may not be on the box at all). It is not free-floating:
# sotto-chief-of-staff/tests/test_docs_drift.py loads both and fails if they ever differ.
RELATION_SENTENCE = {
    "introduced_by": "Introduced to you by {name}",
    "introduced": "Introduced {name} to you",
    "works_with": "Works with {name}",
    "family_of": "Family of {name}",
    "partner_of": "Partner of {name}",
    "met_through": "Met through {name}",
    "connected": "Connected you with {name}",
}
_MONTHS = ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December")


def _relation_date_label(date: str) -> str:
    m = re.match(r"\A(\d{4})-(\d{2})(?:-\d{2})?\Z", _s(date).strip())
    if not m:
        return _s(date).strip()
    month = int(m.group(2))
    return f"{_MONTHS[month - 1]} {m.group(1)}" if 1 <= month <= 12 else _s(date).strip()


def relation_sentence(rel_type: str, name: str, date: str = "") -> str:
    """The edge as a readable sentence — "Introduced to you by Vishnu Sharma (May 2026)"."""
    template = RELATION_SENTENCE.get(_s(rel_type))
    if not template or not name:
        return ""
    when = _relation_date_label(date)
    return template.format(name=name) + (f" ({when})" if when else "")


def _relations(raw) -> list:
    """Frontmatter `relations:` → [{type, slug, name, sentence}] — the shape the person page renders
    and links from. An edge with a type outside the vocabulary, or with no slug to link to, is
    dropped here exactly as the skills tree drops it on parse."""
    out = []
    for rv in raw if isinstance(raw, list) else []:
        if not isinstance(rv, dict):
            continue
        rtype, slug = _s(rv.get("type")), _s(rv.get("slug"))
        name = _s(rv.get("name")) or slug
        if rtype not in RELATION_SENTENCE or not SLUG_RE.match(slug):
            continue
        out.append({"type": rtype, "slug": slug, "name": name,
                    "sentence": relation_sentence(rtype, name, _s(rv.get("date")))})
    return out


def _active_fact_count(facts) -> int:
    if not isinstance(facts, dict):
        return 0
    return sum(1 for f in facts.values()
               if isinstance(f, dict) and _s(f.get("status") or "active") == "active")


def api_people(query: str = "") -> dict:
    ql = _s(query).lower()
    people = []
    for typ, dirname in (("person", "people"), ("company", "companies")):
        for slug, meta in _list_md(dirname):
            name = _s(meta.get("name")) or (_title_from_slug(slug) if typ == "company" else slug)
            company = _s(meta.get("company"))
            if ql and ql not in name.lower() and ql not in company.lower():
                continue
            people.append({
                "slug": slug,
                "type": typ,
                "name": name,
                "company": company or None,
                "title": _s(meta.get("title")) or None,
                "fact_count": _active_fact_count(meta.get("facts")),
                "updated_at": _s(meta.get("updated_at")) or None,
            })
    people.sort(key=lambda p: p["name"].lower())
    return {"people": people[:200]}


def api_person(slug: str):
    """Frontmatter fields + normalized facts for people/<slug>.md, else companies/<slug>.md, else
    None. People additionally carry `relations` — [{type, slug, name, sentence}], each `slug` a
    person page this one links to. The slug is regex-validated at the route (and re-checked here)
    before any path join.
    Companies keep their substance in the markdown BODY (About/News/Context prose, per
    contracts/exhaust-schema.md), so they additionally carry `sections` and a guaranteed name."""
    if not SLUG_RE.match(slug):
        return None
    for typ, dirname in (("person", "people"), ("company", "companies")):
        path = os.path.join(_root(), "knowledge", dirname, slug + ".md")
        try:
            with open(path, encoding="utf-8") as f:
                meta, body = parse_frontmatter(f.read())
        except OSError:
            continue
        raw_facts = meta.pop("facts", None)
        relations = _relations(meta.pop("relations", None))
        facts = []
        if isinstance(raw_facts, dict):
            for fid, fv in raw_facts.items():
                if not isinstance(fv, dict):
                    continue
                conf = fv.get("conf")
                facts.append({
                    "id": fid,
                    "text": _s(fv.get("text")),
                    "type": _s(fv.get("type")),
                    "status": _s(fv.get("status")) or "active",
                    "seen": fv.get("seen") if isinstance(fv.get("seen"), int) else 0,
                    "conf": float(conf) if isinstance(conf, (int, float)) else 0.0,
                    "source": _s(fv.get("source")),
                    "source_ref": _s(fv.get("source_ref")),
                    "first": _s(fv.get("first")),
                    "last": _s(fv.get("last")),
                })
        facts.sort(key=lambda fx: (0 if fx["status"] == "active" else 1, -fx["conf"]))
        out = dict(meta)
        out.update({"slug": slug, "type": typ, "facts": facts})
        if typ == "company":
            out["sections"] = _body_sections(body)
            out["name"] = _s(meta.get("name")) or _title_from_slug(slug)
        else:
            # Relations are people-only: they link one person page to another.
            out["relations"] = relations
            # The two per-person switches the funnel actually reads, resolved by the SAME rule it
            # applies: an exact, case-insensitive match on the display name (nothing fuzzy, so
            # "Sam" never mutes "Samantha"). Both live in preferences.explicit; both are written
            # through preferences.py, the CLI chat uses.
            ex = _explicit_prefs()
            name_l = _s(meta.get("name")).lower()

            def _listed(key):
                return bool(name_l) and any(name_l == _s(v).lower()
                                            for v in (ex.get(key) or []) if isinstance(v, str))
            out["controls"] = {"name": _s(meta.get("name")),
                               "muted": _listed("mute_people"), "vip": _listed("vip_people")}
        return out
    return None


def api_learned() -> dict:
    """Style is summarized, never dumped: the fingerprint carries verbatim message samples, so only
    the REGISTER names — the keys of style.json's `canonical` block (work_email / work_message /
    personal_message) — and the per_person count leave this endpoint. Top-level keys are schema
    bookkeeping (canonical/master/sample_keys/…), never shown. Missing canonical → no buckets."""
    style = _read_json_file("style.json", default=None)
    summary: dict = {}
    if isinstance(style, dict):
        per = style.get("per_person")
        canonical = style.get("canonical")
        summary = {
            "buckets": sorted(canonical) if isinstance(canonical, dict) else [],
            "per_person": len(per) if isinstance(per, dict) else 0,
        }
        if isinstance(style.get("updated_at"), str):
            summary["updated_at"] = style["updated_at"]
    prefs = _read_json_file("preferences.json", default=None)
    if not isinstance(prefs, (dict, list)):
        prefs = {}
    return {"style": summary, "preferences": prefs}


# ── Cadence (the machine's volume controls, made visible) ────────────────────────────────────────
# One question, answered in one screen: how much has Sotto let itself interrupt you today, what is
# holding it back right now, and what is waiting behind those holds. Every number here is READ from
# the file the funnel actually decides on — budget.json, meeting_taps.json, valve_state.json,
# preferences.json, queue.jsonl — so the panel can never disagree with the funnel about the day.

def _int_env(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _budget_today(day: str) -> dict:
    """{spent, cap, left} — triage_event._budget_spent's rule: a stamp from another local date
    reads as 0 spent, which IS the midnight rollover (no cleanup job, no cron)."""
    cap = max(0, _int_env("SOTTO_NUDGE_BUDGET", 4))
    st = _read_json_file("events", "budget.json", default=None)
    spent = 0
    if isinstance(st, dict) and _s(st.get("date")) == day:
        try:
            spent = max(0, int(st.get("count") or 0))
        except (TypeError, ValueError):
            spent = 0
    return {"spent": spent, "cap": cap, "left": max(0, cap - spent)}


def _taps_today(day: str) -> dict:
    """{fired, cap} — calcache's date-keyed meeting_taps.json. Post-meeting taps have their OWN
    daily cap precisely so they and the interrupt budget can't starve each other."""
    cap = max(0, _int_env("SOTTO_TAP_MAX_PER_DAY", 3))
    st = _read_json_file("cache", "meeting_taps.json", default=None)
    fired = 0
    if isinstance(st, dict) and _s(st.get("date")) == day:
        v = st.get("fired")
        fired = len([k for k in v if isinstance(k, str)]) if isinstance(v, list) else 0
    return {"fired": fired, "cap": cap}


def _valve_hour() -> dict:
    """{promoted, cap} for the last hour — triage_event._valve_recent's own window."""
    cap = max(0, VALVE_MAX_PER_HOUR_DEFAULT)
    st = _read_json_file("events", "valve_state.json", default=None)
    now = time.time()
    recent = 0
    if isinstance(st, dict) and isinstance(st.get("promotions"), list):
        recent = len([t for t in st["promotions"]
                      if isinstance(t, (int, float)) and (now - t) < 3600])
    enabled = (os.environ.get("SOTTO_VALVE", "").strip() or "1") != "0"
    return {"promoted": recent, "cap": cap, "enabled": enabled}


def _explicit_prefs() -> dict:
    prefs = _read_json_file("preferences.json", default=None)
    ex = prefs.get("explicit") if isinstance(prefs, dict) else None
    return ex if isinstance(ex, dict) else {}


def _snooze_state(ex: dict) -> dict:
    """{until, active} for explicit.nudge_snooze_until. The stamp is a LOCAL wall-clock string
    (preferences.SNOOZE_FMT) — compared naively against the user's local clock, exactly as
    preferences.snooze_active does, so "quiet until 3" means 3pm where the user is."""
    until = _s(ex.get("nudge_snooze_until"))
    if not until:
        return {"until": "", "active": False}
    try:
        stamp = until if "T" in until else until + "T00:00:00"
        target = datetime.fromisoformat(stamp.replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return {"until": until, "active": False}   # a broken stamp never silences Sotto
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        tz = _s(os.environ.get("SOTTO_TIMEZONE") or os.environ.get("TZ") or "")
        if not tz:
            settings = _read_json_file("config", "settings.json", default={}) or {}
            tz = _s(settings.get("timezone")) if isinstance(settings, dict) else ""
        now_local = datetime.now(ZoneInfo(tz)).replace(tzinfo=None) if tz else datetime.now()
    except Exception:  # noqa: BLE001
        now_local = datetime.now()
    return {"until": until, "active": now_local < target}


def _meeting_hold_hint() -> str:
    """"2:30 PM" when the shared calendar snapshot says the user is in a peopled meeting right now,
    else "". ADVISORY ONLY — it exists so the "nudge me now" button can explain itself before it is
    pressed. The authority is triage_event's own in-meeting hold, which POST /api/cadence reports
    verbatim when it refuses a promotion."""
    snap = HOOKS["calendar_snapshot"]()
    if not isinstance(snap, dict):
        return ""
    now = datetime.now().astimezone()
    latest = None
    for ev in snap.get("events") or []:
        if not isinstance(ev, dict) or ev.get("all_day"):
            continue
        atts = ev.get("attendees")
        if not isinstance(atts, list) or not atts:
            continue                      # a solo block is not a room
        start, end = _event_dt(ev.get("start")), _event_dt(ev.get("end"))
        if start is None or end is None or not (start <= now < end):
            continue
        if latest is None or end > latest:
            latest = end
    if latest is None:
        return ""
    return latest.strftime("%-I:%M %p") if os.name != "nt" else latest.strftime("%I:%M %p")


def _event_dt(value):
    """A calendar event time → an AWARE datetime, or None. Date-only (all-day) reads as None."""
    v = _s(value)
    if not v or DATE_RE.match(v):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.astimezone()


def _looks_like_phone(name: str) -> bool:
    """MIRROR of textutil._looks_like_phone_number's intent: a "name" that is really a number.
    Used only to decide whether a waiting-room row can offer "nudge me now" — the funnel re-checks."""
    return bool(re.fullmatch(r"[\d\s()+\-.]{7,}", _s(name)))


def _waiting_room(now: float) -> tuple[list, int]:
    """The queue's user-meaningful tail: who is waiting, why they're being held, and how old it is.
    Reads at most QUEUE_TAIL_LINES lines and renders at most QUEUE_SHOWN, newest first. This answers
    "why haven't I heard about X" without opening the Record."""
    try:
        with open(os.path.join(_root(), "events", "queue.jsonl"), encoding="utf-8") as f:
            lines = f.readlines()[-QUEUE_TAIL_LINES:]
    except OSError:
        return [], 0
    rows = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        ev = entry.get("event") if isinstance(entry.get("event"), dict) else {}
        cls = _s(entry.get("verdict_class"))
        held = _s(entry.get("held_class"))
        sender = _s(entry.get("sender"))
        ts = _s(entry.get("ts"))
        at = _from_iso(ts)
        rows.append({
            "key": _queue_key(line),
            "sender": sender,
            "channel": _s(ev.get("source")),
            "class": cls,
            "held_class": held or None,
            "ts": ts,
            "age_min": int(max(0.0, (now - at) / 60.0)) if at is not None else None,
            # Display-only: the funnel decides for real on POST. A row is offerable when its class
            # is one the valve would ever release, its sender is a real resolved name, and it is not
            # one of Sotto's own nudges (those come back through their own lane, never the valve).
            "promotable": bool(cls in PROMOTABLE_CLASSES and sender
                               and sender != "Unknown" and not _looks_like_phone(sender)
                               and _s(ev.get("source")) != PROACTIVE_SOURCE),
            "budget_exempt": (held or cls) in BUDGET_EXEMPT_CLASSES,
        })
    rows.reverse()                                   # newest first
    return rows[:QUEUE_SHOWN], len(rows)


def api_cadence() -> dict:
    """GET /api/cadence → today's interrupt spend, the levers that are holding Sotto back, and the
    waiting room. Every field is a read of the funnel's own state file; nothing is recomputed."""
    day = _local_today()
    ex = _explicit_prefs()
    waiting, waiting_total = _waiting_room(time.time())
    try:
        channel = _s(HOOKS["delivery_channel"]()) or "whatsapp"
    except Exception:  # noqa: BLE001
        channel = "whatsapp"
    return {
        "date": day,
        "budget": _budget_today(day),
        "taps": _taps_today(day),
        "valve": _valve_hour(),
        "snooze": _snooze_state(ex),
        "quiet": {"start": _int_env("SOTTO_QUIET_START", 21),
                  "end": _int_env("SOTTO_QUIET_END", 7)},
        "delivery": {"channel": channel,
                     "ready": _hook_bool("delivery_ready"),
                     "whatsapp": _s(_hook_str("whatsapp_status"))},
        "meeting_until": _meeting_hold_hint(),
        "vip_people": [x for x in (ex.get("vip_people") or []) if isinstance(x, str)],
        "waiting": waiting,
        "waiting_total": waiting_total,
    }


def _hook_str(name: str) -> str:
    try:
        return _s(HOOKS[name]())
    except Exception:  # noqa: BLE001
        return ""


# ── The people graph's two open questions (merge suggestions + who's going quiet) ─────────────────

def api_graph() -> dict:
    """GET /api/graph → the two things about the people graph that are WAITING ON A HUMAN:
    knowledge/merge_suggestions.json (name-similarity pairs the Learn step deliberately did NOT
    merge — "merging two different people is worse than holding two files for one") and
    relationship_state.json's attention queue (who is waiting on a reply, who is going quiet).
    Both are read-only here; the merge card's Confirm/Dismiss writes through /api/graph POST."""
    data = _read_json_file("knowledge", "merge_suggestions.json", default=None)
    items = data.get("suggestions") if isinstance(data, dict) else None
    suggestions = []
    for s in (items if isinstance(items, list) else [])[:MERGE_SUGGESTIONS_SHOWN]:
        if not isinstance(s, dict):
            continue
        frm, into = _s(s.get("from")), _s(s.get("into"))
        if not SLUG_RE.match(frm) or not SLUG_RE.match(into):
            continue                       # only pairs we could actually act on are offered
        suggestions.append({"from": frm, "into": into,
                            "from_name": _s(s.get("from_name")) or _title_from_slug(frm),
                            "into_name": _s(s.get("into_name")) or _title_from_slug(into),
                            "reason": _s(s.get("reason")),
                            "first_seen": _s(s.get("first_seen")) or None})
    state = _read_json_file("knowledge", "relationship_state.json", default=None)
    queue = state.get("attention_queue") if isinstance(state, dict) else None
    by_name = {}                       # display name → dossier slug, so a row opens the person
    for slug, meta in _list_md("people"):
        nm = _s(meta.get("name")).lower()
        if nm and nm not in by_name:
            by_name[nm] = slug
    attention = []
    for q in (queue if isinstance(queue, list) else [])[:ATTENTION_SHOWN]:
        if not isinstance(q, dict):
            continue
        name = _s(q.get("display_name"))
        if not name:
            continue
        ctx = q.get("graph_context") if isinstance(q.get("graph_context"), dict) else {}
        attention.append({"name": name, "queue_type": _s(q.get("queue_type")),
                          "reason": _s(q.get("reason")),
                          "company": _s(ctx.get("company")) or None,
                          "slug": by_name.get(name.lower())})
    return {"merge_suggestions": suggestions, "attention": attention}


# ── The voice card (style.json, the one bucket with a reader and — until now — no writer) ─────────

def _style_sample(sample: dict, bucket: str, confirmed_keys: set) -> dict:
    text = _s(sample.get("text"))
    return {"key": _style_hash(sample), "bucket": bucket,
            "text": text[:VOICE_TEXT_MAX], "truncated": len(text) > VOICE_TEXT_MAX,
            "channel": _s(sample.get("channel")), "date": _s(sample.get("date"))[:10],
            "confirmed": _style_hash(sample) in confirmed_keys}


def api_voice() -> dict:
    """GET /api/voice → the fingerprint's own summary plus the samples the Confirm control acts on.

    These ARE verbatim messages the user sent, so the endpoint is deliberately bounded: the
    highest-signal samples per register, capped at VOICE_CANDIDATES_MAX and VOICE_TEXT_MAX chars,
    behind the same session everything else here is behind. Nothing else in style.json leaves —
    per-person pools, sample_keys and the raw recent stream stay where they are."""
    style = _read_json_file("style.json", default=None)
    if not isinstance(style, dict):
        return {"registers": [], "traits": [], "candidates": [], "confirmed": [],
                "updated_at": None}
    canonical = style.get("canonical") if isinstance(style.get("canonical"), dict) else {}
    confirmed_raw = [s for s in (style.get("confirmed") or []) if isinstance(s, dict)]
    confirmed_keys = {_style_hash(s) for s in confirmed_raw}
    registers, candidates = [], []
    for bucket in sorted(canonical):
        pool = [s for s in (canonical.get(bucket) or []) if isinstance(s, dict)]
        registers.append({"bucket": bucket, "samples": len(pool),
                          "confirmed": len([s for s in confirmed_raw
                                            if _s(s.get("bucket")) == bucket])})
        ranked = sorted(pool, key=lambda s: s.get("quality") if isinstance(s.get("quality"),
                                                                          (int, float)) else 0,
                        reverse=True)
        for s in ranked:
            if _s(s.get("text")):
                candidates.append(_style_sample(s, bucket, confirmed_keys))
    candidates = [c for c in candidates if not c["confirmed"]][:VOICE_CANDIDATES_MAX]
    master = style.get("master") if isinstance(style.get("master"), dict) else {}
    traits = []
    if _s(master.get("capitalization")) == "lowercase":
        traits.append("starts messages lowercase")
    if master.get("uses_exclamation_marks") is False:
        traits.append("rarely uses exclamation marks")
    elif master.get("uses_exclamation_marks") is True:
        traits.append("uses exclamation marks naturally")
    if master.get("uses_em_dashes"):
        traits.append("uses em dashes")
    for label, key in (("opens with", "greetings"), ("signs off with", "signoffs")):
        vals = [_s(v) for v in (master.get(key) or []) if _s(v)][:3]
        if vals:
            traits.append(f"{label} {', '.join(vals)}")
    return {"registers": registers, "traits": traits, "candidates": candidates,
            "confirmed": [_style_sample(s, _s(s.get("bucket")), confirmed_keys)
                          for s in confirmed_raw][:VOICE_CANDIDATES_MAX],
            "updated_at": _s(style.get("updated_at")) or None}


# ── Run it now (the same cron prompt, fired by hand) ──────────────────────────────────────────────

def _mtime_iso(*parts):
    try:
        return _iso(os.path.getmtime(os.path.join(_root(), *parts)))
    except OSError:
        return None


def api_runs() -> dict:
    """GET /api/runs → the crons.json jobs this box will run on demand, each with its honest state.

    The rule, in one sentence: a brief can be run by hand until it has been delivered today, and the
    digest is never blocked because it gates itself. "Delivered" is brief_marker.py's own
    `<date>.<kind>.delivered` flag — the deliver-once gate the cron and the wake-push already share,
    so the button can never disagree with the machine about whether today's brief went out. A live
    `.claim` without that flag means a run the receiver started is still in flight."""
    try:
        registered = set(HOOKS["job_names"]())
    except Exception:  # noqa: BLE001
        registered = set()
    day = _local_today()
    jobs = []
    for name, kind in RUN_JOBS:
        if name not in registered:
            continue
        job = {"name": name, "kind": kind, "available": True, "reason": "", "at": None}
        if kind in ("morning", "evening"):
            delivered = _mtime_iso("briefs", f"{day}.{kind}.delivered")
            if delivered:
                job.update({"available": False, "reason": "delivered", "at": delivered})
            else:
                claim = _mtime_iso("briefs", f"{day}.{kind}.claim")
                claim_at = _from_iso(claim) if claim else None
                if claim_at is not None and (time.time() - claim_at) < CLAIM_STALE_SECS:
                    job.update({"available": False, "reason": "running", "at": claim})
        else:
            job["at"] = _digest_window_at()
        jobs.append(job)
    return {"jobs": jobs, "channel": _s(_hook_str("delivery_channel")) or "whatsapp"}


def _digest_window_at():
    """When the midday-digest window last advanced (events/last_digest.txt — written by a digest run
    AND by the brief that wins the deliver-once claim). Shown as context, never as a gate."""
    try:
        with open(os.path.join(_root(), "events", "last_digest.txt"), encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return None
    return raw[:32] or None


# ── M3 live views (session-gated GETs: calendar, research cards, action-ledger timeline) ─────────

def _date_shift(date: str, days: int) -> str:
    """YYYY-MM-DD ± days, or "" for an unparseable input."""
    try:
        return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


# The calendar gather + its 10-minute cache used to live here. It moved to calcache.py (reached
# through HOOKS["calendar_snapshot"]) so the dashboard endpoint and the triage in-meeting hold
# consume ONE cache — see that module's header. What stays here is the serve-time knowledge-graph
# join below: the cache is raw by design, and only the docket wants attendees to carry identities.

# The knowledge-graph join for the docket: email → {name, title, company, slug} from
# knowledge/people frontmatter (`identifiers` carries the emails). Cached ~10 min alongside the
# calendar cache so a dashboard refresh re-reads neither the volume nor the graph every time.
_PEOPLE_IDX_LOCK = threading.Lock()
_PEOPLE_IDX_CACHE: dict = {"ts": 0.0, "value": None}


def _people_email_index() -> dict:
    """email (lowercased) → {name, title, company, slug} for every person on file. Tolerant:
    identifiers may be absent, non-list, or carry phone numbers — only @-shaped strings index.
    First person to claim an address keeps it (slug-sorted, so the winner is stable)."""
    now = time.time()
    with _PEOPLE_IDX_LOCK:
        val = _PEOPLE_IDX_CACHE["value"]
        if val is not None and now - _PEOPLE_IDX_CACHE["ts"] < PEOPLE_INDEX_TTL_SECS:
            return val
    idx: dict = {}
    for slug, meta in _list_md("people"):
        hit = {
            "name": _s(meta.get("name")) or _title_from_slug(slug),
            "title": _s(meta.get("title")),
            "company": _s(meta.get("company")),
            "slug": slug,
        }
        cands = list(meta.get("identifiers")) if isinstance(meta.get("identifiers"), list) else []
        cands.append(meta.get("email"))
        for cand in cands:
            email = cand.strip().lower() if isinstance(cand, str) else ""
            if "@" in email and email not in idx:
                idx[email] = hit
    with _PEOPLE_IDX_LOCK:
        _PEOPLE_IDX_CACHE["ts"] = now
        _PEOPLE_IDX_CACHE["value"] = idx
    return idx


def _enrich_calendar_events(events):
    """A copy of the normalized events with each attendee joined against the people index:
    known addresses gain title/company/slug (empty join fields are dropped) and a name when the
    calendar had none. Unknown attendees pass through untouched."""
    idx = _people_email_index()
    out = []
    for ev in events:
        atts = []
        for a in ev.get("attendees") or []:
            email = _s(a.get("email")).lower()
            hit = idx.get(email) if email else None
            if hit:
                a = dict(a)
                if not _s(a.get("name")):
                    a["name"] = hit["name"]
                if hit["title"]:
                    a["title"] = hit["title"]
                if hit["company"]:
                    a["company"] = hit["company"]
                a["slug"] = hit["slug"]
            atts.append(a)
        out.append({**ev, "attendees": atts})
    return out


def api_calendar() -> dict:
    """GET /api/calendar → {events, generated_at, cached} (calcache.py's 10-min cache), or
    {events: [], unavailable: true} — 200, not 503: the Today view degrades quietly — when the
    skills tree isn't on this box. A failed/empty gather is cached too, so a broken Google setup
    can't make every dashboard refresh fork a 60s subprocess. Attendees are joined against the
    people index at serve time (its own 10-min cache), so the docket carries who each attendee
    IS — the cache itself stays raw (the in-meeting hold reads the same fetch and wants no names).
    The gather, the TTL and the normalization all live in calcache.py now; this is the view."""
    snap = HOOKS["calendar_snapshot"]()
    if not isinstance(snap, dict):      # None = skills tree absent (or the hook is unwired)
        return {"events": [], "unavailable": True}   # cheap check — nothing worth caching
    events = snap.get("events") or []
    return {"events": _enrich_calendar_events(events),
            "generated_at": _s(snap.get("generated_at")),
            "cached": bool(snap.get("cached"))}


def api_research() -> dict:
    """GET /api/research → today's research cards from $SOTTO_DATA/cache/research_<date>.json
    (research_attendees.py's persistence hook), falling back to yesterday's flagged stale.
    Entries pass through VERBATIM — recent_activity/personal/company_summary are exactly what the
    cards render. Nothing on disk → empty, never 404."""
    today = _local_today()
    for date, stale in ((today, False), (_date_shift(today, -1), True)):
        if not date:
            continue
        data = _read_json_file("cache", f"research_{date}.json", default=None)
        if not isinstance(data, dict):
            continue
        attendees = data.get("attendees")
        out = {"attendees": attendees if isinstance(attendees, list) else [],
               "date": date, "stale": stale}
        if isinstance(data.get("written_at"), str):
            out["written_at"] = data["written_at"]
        return out
    return {"attendees": [], "date": None, "stale": False}


def api_ledger(days_param="") -> dict:
    """GET /api/ledger?days=N (default 7, cap 30) → outcomes.jsonl (log_outcome.py: {ts, action_id,
    outcome, channel, contact, action_type, tier, …}) merged with dashboard_audit.jsonl ({ts,
    event, …}) and events/surfaced.jsonl (triage_event.py: {ts, sender, channel, verdict, reason,
    class} — one line per triage verdict, the "why didn't I get nudged?" trail), each row passed
    through verbatim plus source: "outcome"|"dashboard"|"triage". Newest first, LEDGER_MAX_ROWS cap.
    Each source is trimmed to its own share (LEDGER_MAX_PER_SOURCE) BEFORE the merge, so no single
    source can flood the Record. Malformed lines and rows without a parseable ts are skipped."""
    try:
        days = int(_s(str(days_param)) or LEDGER_DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = LEDGER_DEFAULT_DAYS
    days = max(1, min(days, LEDGER_MAX_DAYS))
    cutoff = time.time() - days * 86400
    entries = []
    # "triage" is what Sotto DECIDED; "delivery" is whether the message it decided to send actually
    # landed. They were one fact until Aug 2026, which is how The Record showed "Nudged you" for
    # weeks about nudges that were spawned into a sink with no delivery channel (receiver.py
    # § Spawning a skill). Two facts, two writers, both on the timeline.
    for fname, source in (("outcomes.jsonl", "outcome"), ("dashboard_audit.jsonl", "dashboard"),
                          (os.path.join("events", "surfaced.jsonl"), "triage"),
                          (os.path.join("events", "delivery.jsonl"), "delivery")):
        try:
            with open(os.path.join(_root(), fname), encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        rows = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue                       # malformed line — skip, never fail the timeline
            if not isinstance(rec, dict):
                continue
            ts = _from_iso(rec.get("ts"))
            if ts is None or ts < cutoff:
                continue
            rows.append({**rec, "source": source})
        # Each ledger is append-ordered, so the tail IS the newest rows of that source. A source
        # with no declared share falls back to the total cap — i.e. to the old behavior.
        entries.extend(rows[-LEDGER_MAX_PER_SOURCE.get(source, LEDGER_MAX_ROWS):])
    entries.sort(key=lambda r: _s(r.get("ts")), reverse=True)   # both writers emit ISO-Z — sortable
    return {"days": days, "entries": entries[:LEDGER_MAX_ROWS]}


# ── M2 write endpoints (session + CSRF gated; the dashboard is a window, not a second brain) ─────
# Fact and loop writes shell out to the skills tree's knowledge_edit.py so a dashboard edit rides
# the IDENTICAL code path a texted correction does (knowledge_update.apply / the ledger writer).
# Only /api/prefs edits a file directly — preferences have no chat-equivalent write path.

def _read_json_body(h):
    """The request's JSON object, or None when absent/oversized/not-a-dict."""
    try:
        n = int(h.headers.get("Content-Length", 0))
    except ValueError:
        return None
    if n <= 0 or n > API_BODY_MAX:
        return None
    try:
        v = json.loads(h.rfile.read(n).decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _clean_text(v, maxlen: int = TEXT_MAX):
    """Stripped non-empty string within the cap, else None (empty/whitespace ops rejected)."""
    if not isinstance(v, str):
        return None
    t = v.strip()
    return t if t and len(t) <= maxlen else None


def _run_skill_cli(rel: tuple, args: list, label: str):
    """Run ONE skills-tree CLI (sys.executable, 30s budget, SOTTO_DATA pinned to the volume this
    server serves) and parse its JSON. Returns None when the skills tree is absent (→ 503);
    otherwise the CLI's dict, with a crashed/JSON-less run folded into {"ok": False, "error": …}.
    Every dashboard write that HAS a chat-equivalent CLI comes through here — knowledge_edit.py
    (facts, loops, merges), preferences.py (the explicit block), style_extract.py (voice
    confirmation) — so there is exactly one subprocess policy for the whole write surface."""
    try:
        script = HOOKS["find_script"](*rel)
    except Exception:  # noqa: BLE001
        script = None
    if not script:
        return None
    env = {**os.environ, "SOTTO_DATA": _root()}
    try:
        r = subprocess.run([sys.executable, script, *args], capture_output=True, text=True,
                           timeout=EDIT_TIMEOUT_SECS, env=env)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "error": f"{label} failed: {type(e).__name__}"}
    try:
        out = json.loads(r.stdout or "")
    except (json.JSONDecodeError, ValueError):
        out = None
    if not isinstance(out, dict):
        return {"ok": False, "error": (r.stderr or f"{label} produced no JSON").strip()[:300]}
    return out


def _run_prefs(args: list):
    """preferences.py — the CLI the sotto-feedback skill writes the user's explicit block with.
    Its commands print the explicit block on success and {"error": …} on failure, so the result is
    normalized to the {"ok": …} shape the rest of this module speaks."""
    out = _run_skill_cli(("_shared", "scripts", "preferences.py"), args, "preferences")
    if out is None:
        return None
    if _s(out.get("error")):
        return {"ok": False, "error": _s(out.get("error"))}
    return {"ok": True, "explicit": out}


def _run_knowledge_edit(args: list):
    """Run the skills tree's knowledge_edit.py — so a dashboard edit rides the identical
    knowledge_update.apply() / continuity_resolve code path a chat correction does."""
    out = _run_skill_cli(("_shared", "knowledge", "knowledge_edit.py"), args, "knowledge_edit")
    if out is None:
        return None
    if out.get("ok"):
        # The edit just rewrote a person file, so the serve-time people index (10-min TTL) now
        # holds the fact the user fixed. Expiring the stamp is the whole fix: the next read
        # rebuilds it. In one sentence: an edit the user just made is never served from a cache.
        _PEOPLE_IDX_CACHE["ts"] = 0.0
    return out


def _edit_failure(h, result: dict):
    """Map a CLI failure to a response: missing person/fact/loop → 404, everything else → 400.
    The error strings come from OUR CLI (not user data) and are safe to relay as JSON."""
    err = _s(result.get("error")) or "edit failed"
    return _json(h, 404 if "not found" in err else 400, {"error": err})


def _handle_api_post(h, path: str):
    rec = _session_record(h)
    if rec is None:
        return _json(h, 401, {"error": "unauthorized"})
    # CSRF: the per-session token (minted at login, fetched by app.js from /api/session) must ride
    # every write in the X-Sotto-CSRF header. Constant-time compare, same as the login code.
    want = _s(rec.get("csrf"))
    got = _s(h.headers.get("X-Sotto-CSRF") or "")
    if not want or not got or not hmac.compare_digest(got.encode(), want.encode()):
        return _json(h, 403, {"error": "bad csrf"})
    body = _read_json_body(h)
    if body is None:
        return _json(h, 400, {"error": "bad json body"})
    try:
        m = PERSON_FACTS_RE.match(path)
        if m:
            return _post_person_facts(h, m.group(1), body)
        m = PERSON_RELATIONS_RE.match(path)
        if m:
            return _post_person_relations(h, m.group(1), body)
        if path == "/api/loops":
            return _post_loops(h, body)
        if path == "/api/prefs":
            return _post_prefs(h, body)
        if path == "/api/cadence":
            return _post_cadence(h, body)
        if path == "/api/graph":
            return _post_graph(h, body)
        if path == "/api/voice":
            return _post_voice(h, body)
        if path == "/api/runs":
            return _post_runs(h, body)
        return _json(h, 404, {"error": "not found"})
    except Exception as e:  # noqa: BLE001 — a shape surprise must not 500-loop the dashboard
        print(f"[sotto] dashboard write error on {path}: {e}", flush=True)
        return _json(h, 500, {"error": "write failed"})


def _post_person_facts(h, slug: str, body: dict):
    """POST /api/people/<slug>/facts {op: correct|archive|add|company-about, fact_id?, text?,
    memory_type?}. On success re-reads and returns the record in the GET /api/people/<slug> shape
    so the UI refreshes in place (the CLI reports the final slug — migration may re-key a legacy
    file).

    `company-about` is the COMPANY half of this endpoint: companies live in the same
    /api/people/<slug> namespace (api_person serves both) but carry no facts map, so their one
    correctable field is the About paragraph — replaced, not superseded. Same CLI, same
    knowledge_update.apply() lane research writes through."""
    op = _s(body.get("op"))
    if op not in ("correct", "archive", "add", "company-about"):
        return _json(h, 400, {"error": "bad op"})
    args = [f"--slug={slug}", f"--op={op}"]
    if op in ("correct", "archive"):
        fact_id = _s(body.get("fact_id"))
        if not FACT_ID_RE.match(fact_id):
            return _json(h, 400, {"error": "bad fact_id"})
        args.append(f"--fact-id={fact_id}")
    if op in ("correct", "add", "company-about"):
        text = _clean_text(body.get("text"))
        if text is None:
            return _json(h, 400, {"error": "bad text (1-500 chars required)"})
        args.append(f"--text={text}")
    if op == "add":
        mt = _s(body.get("memory_type"))
        if mt:
            if not MEMORY_TYPE_RE.match(mt):
                return _json(h, 400, {"error": "bad memory_type"})
            args.append(f"--memory-type={mt}")
    result = _run_knowledge_edit(args)
    if result is None:
        return _json(h, 503, {"error": "skills tree unavailable"})
    if not result.get("ok"):
        return _edit_failure(h, result)
    _audit("write", endpoint=f"/api/people/{slug}/facts",
           target=_s(body.get("fact_id")) or slug, op=op)
    final = _s(result.get("slug")) or slug
    person = api_person(final) if SLUG_RE.match(final) else None
    if person is None:                       # readable person is the contract; degrade gracefully
        return _json(h, 200, {"ok": True, "slug": final})
    return _json(h, 200, person)


def _post_person_relations(h, slug: str, body: dict):
    """POST /api/people/<slug>/relations {op: "remove", type, other_slug} — the ✕ on a relation.
    Removal only: a relation is CLAIMED by the data (or by a typed instruction in chat), and the
    page's job is to let the user say "no, that's wrong". Rides knowledge_edit.py's
    relation-remove, which unlinks BOTH ends through the same writer the Learn step uses, and
    returns the refreshed person so the page re-renders from the server."""
    if _s(body.get("op")) != "remove":
        return _json(h, 400, {"error": "bad op"})
    other = _s(body.get("other_slug"))
    if not SLUG_RE.match(other):
        return _json(h, 400, {"error": "bad other_slug"})
    rel_type = _s(body.get("type"))
    if rel_type and rel_type not in RELATION_SENTENCE:
        return _json(h, 400, {"error": "bad relation type"})
    args = [f"--slug={slug}", "--op=relation-remove", f"--other-slug={other}"]
    if rel_type:
        args.append(f"--type={rel_type}")
    result = _run_knowledge_edit(args)
    if result is None:
        return _json(h, 503, {"error": "skills tree unavailable"})
    if not result.get("ok"):
        return _edit_failure(h, result)
    _audit("write", endpoint=f"/api/people/{slug}/relations",
           target=f"{slug}->{other}"[:200], op="relation-remove")
    person = api_person(slug)
    return _json(h, 200, person if person is not None else {"ok": True, "slug": slug})


def _post_loops(h, body: dict):
    """POST /api/loops — the ledger's four verbs, all through knowledge_edit.py:
      {anchor_key, op: resolve|dismiss}           terminal transition (the resolver's own fields)
      {op: "add", text, contact?, identifier?, deadline?}   a loop you own by hand
      {anchor_key, op: "deadline", deadline}      set/clear the deadline — which is also "later",
                                                  since the resolver expires 2 days past it.
    Returns {"ok": true, ...} plus the refreshed loops list so the view re-renders from the
    server (never optimistic)."""
    op = _s(body.get("op"))
    if op not in ("resolve", "dismiss", "add", "deadline"):
        return _json(h, 400, {"error": "bad op"})
    if op == "add":
        text = _clean_text(body.get("text"))
        if text is None:
            return _json(h, 400, {"error": "bad text (1-500 chars required)"})
        args = ["--op=loop-add", f"--text={text}"]
        contact = _clean_text(body.get("contact"), 120)
        if contact:
            args.append(f"--contact={contact}")
        ident = _clean_text(body.get("identifier"), 200)
        if ident:
            args.append(f"--identifier={ident}")
        deadline = _s(body.get("deadline"))
        if deadline:
            if not DATE_RE.match(deadline):
                return _json(h, 400, {"error": "bad deadline (YYYY-MM-DD)"})
            args.append(f"--deadline={deadline}")
        result = _run_knowledge_edit(args)
        if result is None:
            return _json(h, 503, {"error": "skills tree unavailable"})
        if not result.get("ok"):
            return _edit_failure(h, result)
        _audit("write", endpoint="/api/loops", target=_s(result.get("anchor_key"))[:200], op="add")
        return _json(h, 200, {"ok": True, "anchor_key": _s(result.get("anchor_key")),
                              **api_loops()})
    anchor = body.get("anchor_key")
    if not isinstance(anchor, str) or not ANCHOR_RE.match(anchor):
        return _json(h, 400, {"error": "bad anchor_key"})
    if op == "deadline":
        deadline = _s(body.get("deadline"))
        if deadline and not DATE_RE.match(deadline):
            return _json(h, 400, {"error": "bad deadline (YYYY-MM-DD)"})
        result = _run_knowledge_edit(["--op=loop-deadline", f"--anchor={anchor}",
                                      f"--deadline={deadline}"])
        if result is None:
            return _json(h, 503, {"error": "skills tree unavailable"})
        if not result.get("ok"):
            return _edit_failure(h, result)
        _audit("write", endpoint="/api/loops", target=anchor[:200], op="deadline")
        return _json(h, 200, {"ok": True, "deadline": deadline or None, **api_loops()})
    to = "resolved" if op == "resolve" else "dismissed"
    result = _run_knowledge_edit(["--op=loop", f"--anchor={anchor}", f"--to={to}"])
    if result is None:
        return _json(h, 503, {"error": "skills tree unavailable"})
    if not result.get("ok"):
        return _edit_failure(h, result)
    _audit("write", endpoint="/api/loops", target=anchor[:200], op=op)
    return _json(h, 200, {"ok": True})


# ── Cadence writes: the snooze (preferences.py) and one user-chosen promotion (the funnel) ────────

def _post_cadence(h, body: dict):
    """POST /api/cadence:
      {op: "snooze", spec}    — spec is preferences.py's own vocabulary ("today" / "3 days" is
                                expressed as an ISO date / "+2h" / "3pm" / "2026-08-11T07:00").
                                THE RULE, shown on the control: a snooze lifts when quiet hours do.
      {op: "unsnooze"}        — back to normal.
      {op: "promote", key}    — "nudge me now" on one held item: the receiver runs the funnel's own
                                `triage_event.py --promote`, which spends the day's interrupt
                                budget, respects the in-meeting hold, drops the entry from the
                                queue and records the promotion — then stages + spawns exactly as
                                the release valve does.
    Every clock decision (what "+2h" means, whether the snooze is still active, whether a promotion
    is allowed) belongs to those two programs; this handler only validates shapes."""
    op = _s(body.get("op"))
    if op not in ("snooze", "unsnooze", "promote"):
        return _json(h, 400, {"error": "bad op"})
    if op == "promote":
        key = _s(body.get("key"))
        if not QUEUE_KEY_RE.match(key):
            return _json(h, 400, {"error": "bad key"})
        try:
            result = HOOKS["promote_queued"](key)
        except Exception as e:  # noqa: BLE001
            print(f"[sotto] dashboard promote hook failed: {e}", flush=True)
            result = {"ok": False, "error": "unavailable", "reason": "promotion failed"}
        if not isinstance(result, dict):
            result = {"ok": False, "error": "unavailable", "reason": "promotion failed"}
        if not result.get("ok"):
            code = 404 if _s(result.get("error")) == "not_found" else 409
            return _json(h, code, {"error": _s(result.get("error")) or "refused",
                                   "reason": _s(result.get("reason"))})
        _audit("write", endpoint="/api/cadence", target=key, op="promote")
        return _json(h, 200, {"ok": True, "reason": _s(result.get("reason")), **api_cadence()})
    args = ["unsnooze-nudges"] if op == "unsnooze" else None
    if args is None:
        spec = _clean_text(body.get("spec"), SNOOZE_SPEC_MAX)
        if spec is None:
            return _json(h, 400, {"error": "bad spec"})
        args = ["snooze-nudges", spec]
    result = _run_prefs(args)
    if result is None:
        return _json(h, 503, {"error": "skills tree unavailable"})
    if not result.get("ok"):
        return _json(h, 400, {"error": _s(result.get("error")) or "snooze failed"})
    _audit("write", endpoint="/api/cadence", target=_s(body.get("spec"))[:64], op=op)
    return _json(h, 200, {"ok": True, **api_cadence()})


# ── Graph writes: confirm or dismiss ONE merge suggestion ─────────────────────────────────────────

def _post_graph(h, body: dict):
    """POST /api/graph {op: merge|dismiss, from, into} — the confirmation half of entity-dedup lite.
    `merge` runs knowledge_edit.py's merge op (kg.merge_person + the identifier-conflict refusal);
    `dismiss` forgets the suggestion through the same drop_merge_suggestion the merge itself calls.
    A human confirming is the ONLY way a name-similarity merge ever happens — in chat or here."""
    op = _s(body.get("op"))
    if op not in ("merge", "dismiss"):
        return _json(h, 400, {"error": "bad op"})
    frm, into = _s(body.get("from")), _s(body.get("into"))
    if not SLUG_RE.match(frm) or not SLUG_RE.match(into):
        return _json(h, 400, {"error": "bad slug"})
    cli_op = "merge" if op == "merge" else "merge-dismiss"
    result = _run_knowledge_edit([f"--op={cli_op}", f"--from={frm}", f"--into={into}"])
    if result is None:
        return _json(h, 503, {"error": "skills tree unavailable"})
    if not result.get("ok"):
        return _edit_failure(h, result)
    _audit("write", endpoint="/api/graph", target=f"{frm}->{into}"[:200], op=op)
    return _json(h, 200, {"ok": True, **api_graph()})


# ── Voice write: confirm ONE style sample ─────────────────────────────────────────────────────────

def _post_voice(h, body: dict):
    """POST /api/voice {op: "confirm", key} → style_extract.py --confirm, the one writer of
    style.json's `confirmed` bucket. In one sentence: confirming says "yes, that IS how I write",
    and the drafter quotes it first and stops letting it age out. Confirm only — the fingerprint is
    observed, never typed."""
    if _s(body.get("op")) != "confirm":
        return _json(h, 400, {"error": "bad op"})
    key = _s(body.get("key"))
    if not STYLE_KEY_RE.match(key):
        return _json(h, 400, {"error": "bad key"})
    result = _run_skill_cli(("_shared", "scripts", "style_extract.py"),
                            ["--confirm", key], "style_extract")
    if result is None:
        return _json(h, 503, {"error": "skills tree unavailable"})
    if not result.get("ok"):
        return _json(h, 404, {"error": _s(result.get("error")) or "confirm failed"})
    _audit("write", endpoint="/api/voice", target=key, op="confirm")
    return _json(h, 200, {"ok": True, **api_voice()})


# ── Run it now: fire one crons.json job by name, through the runner cron uses ─────────────────────

def _post_runs(h, body: dict):
    """POST /api/runs {name} → the same prompt the cron fires, now. The delivery still goes to the
    chat channel — the web triggers, the message arrives where messages live. Refuses anything the
    read side reported unavailable (a brief already delivered today, a run in flight), so the
    button and the machine can never disagree."""
    name = _s(body.get("name"))
    state = api_runs()
    job = next((j for j in state["jobs"] if j["name"] == name), None)
    if job is None:
        return _json(h, 404, {"error": "unknown job"})
    if not job["available"]:
        return _json(h, 409, {"error": job["reason"] or "unavailable", **state})
    try:
        result = HOOKS["run_job"](name)
    except Exception as e:  # noqa: BLE001
        print(f"[sotto] dashboard run hook failed: {e}", flush=True)
        result = {"ok": False, "error": "unavailable"}
    if not isinstance(result, dict) or not result.get("ok"):
        return _json(h, 503, {"error": _s((result or {}).get("reason"))
                                       or "that run couldn't be started"})
    _audit("write", endpoint="/api/runs", target=name, op="run")
    return _json(h, 200, {"ok": True, "skill": _s(result.get("skill")), **api_runs()})


def _write_prefs(prefs: dict) -> None:
    """THE atomic 0600 write of preferences.json — indent=2 because learn_preferences.py writes it
    that way and the two must not fight over the file's shape."""
    HOOKS["write_json"](os.path.join(_root(), "preferences.json"), prefs, 0o600, 2)


def _post_prefs(h, body: dict):
    """POST /api/prefs {op: "delete"|"add", list, value} → the updated preferences object.

    TWO write paths, because preferences.json has two halves and only one of them has a chat verb:
      * the `explicit` block (mutes, VIPs, tone notes) is the user's STATED preferences, and
        preferences.py is the CLI the sotto-feedback skill writes them with — so adds and per-value
        removes shell out to it (PREF_EXPLICIT_VERBS). Muting a sender from the dashboard is
        byte-for-byte what texting "stop surfacing them" does.
      * `deprioritization_hints`, `edit_heavy` and `approval_defaults` are LEARNED — recomputed
        from outcomes.jsonl by every Learn run, with no verb to state them — so a delete there is
        this module's own atomic edit, plus a tombstone.

    ONE SENTENCE: a rule you delete stays deleted. Because the learned lists are rebuilt every
    morning, removing the entry alone would let it reappear — the delete is therefore also recorded
    in the top-level `suppressed` list, which learn_preferences.py filters out when it rebuilds. The
    `explicit` block needs no tombstone: the learner carries it forward verbatim.
    (`suppressed` is top-level, not inside `explicit`, because preferences.py's _save() reshapes
    that block to its own LISTS/SCALARS and would drop any extra key.)"""
    op = _s(body.get("op"))
    if op not in ("delete", "add"):
        return _json(h, 400, {"error": "bad op"})
    lst = _s(body.get("list"))
    if op == "add":
        if lst not in PREF_ADDABLE:
            return _json(h, 400, {"error": "bad list"})
    elif lst not in PREF_TOP_LISTS | PREF_EXPLICIT_LISTS | PREF_DICTS:
        return _json(h, 400, {"error": "bad list"})
    value = _clean_text(body.get("value"))
    if value is None:
        return _json(h, 400, {"error": "bad value (1-500 chars required)"})
    verbs = PREF_EXPLICIT_VERBS.get(lst) or ("", "")
    verb = verbs[0] if op == "add" else verbs[1]
    if verb:
        result = _run_prefs([verb, value])
        if result is None:
            return _json(h, 503, {"error": "skills tree unavailable"})
        if not result.get("ok"):
            return _json(h, 400, {"error": _s(result.get("error")) or "that didn't save"})
        prefs = _read_json_file("preferences.json", default=None)
        if not isinstance(prefs, dict):
            prefs = {}
        _audit("write", endpoint="/api/prefs", target=f"{lst}:{value}"[:200], op=op)
        return _json(h, 200, prefs)
    if op == "add":                     # no verb for this list → nothing safe to invent
        return _json(h, 400, {"error": "bad list"})
    prefs = _read_json_file("preferences.json", default=None)
    if not isinstance(prefs, dict):
        prefs = {}
    removed = False
    if lst in PREF_DICTS:
        d = prefs.get(lst)
        if isinstance(d, dict) and value in d:
            d.pop(value)
            removed = True
    elif lst in PREF_TOP_LISTS:
        v = prefs.get(lst)
        if isinstance(v, list) and value in v:
            prefs[lst] = [x for x in v if x != value]
            removed = True
    else:  # explicit block lists (preferences.py's reserved user-stated block)
        ex = prefs.get("explicit")
        if isinstance(ex, dict) and isinstance(ex.get(lst), list) and value in ex[lst]:
            ex[lst] = [x for x in ex[lst] if x != value]
            removed = True
    if not removed:
        return _json(h, 404, {"error": "not found"})
    if lst in PREF_TOP_LISTS | PREF_DICTS:      # recomputed every Learn run → leave a tombstone
        sup = prefs.get("suppressed")
        if not isinstance(sup, list):
            sup = []
        tomb = {"list": lst, "value": value}
        if tomb not in sup:
            sup.append(tomb)
        prefs["suppressed"] = sup
    _write_prefs(prefs)
    _audit("write", endpoint="/api/prefs", target=f"{lst}:{value}"[:200], op="delete")
    return _json(h, 200, prefs)
