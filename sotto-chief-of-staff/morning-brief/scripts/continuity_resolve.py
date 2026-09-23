#!/usr/bin/env python3
"""One ledger row per obligation, across channels.

The pre-compose pass migrates exact duplicate identities, resolves legacy calendar shadows and
stamps eligible chases. The post-compose pass merges actions and applies the existing brief
model's task-specific completion proposals, bound to a ledger revision and real source message.
Contact, a word count, a URL and a calendar attendee are never proof of a deliverable.
Obligations stay until observed completion or a user correction, regardless of age or deadline.
Something the user owes that nothing has touched for PARK_AFTER_DAYS parks instead: kept on disk,
out of every view, revived by the next touch (a re-capture, a `keep`, a completion proposal).
"""
from __future__ import annotations

import json
import hashlib
import fcntl
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

import yaml

# Shared with the rest of the pipeline: compose_brief's zoneinfo-aware tz helpers (SOTTO_TIMEZONE /
# wizard settings) and ledger_io's frontmatter loader — one parser for all three ledger readers.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shared", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shared", "lib"))
import ledger_io  # noqa: E402
import log_outcome  # noqa: E402
from brief_validate import is_name_shaped  # noqa: E402  (one owner for "is this a person's name")
from textutil import _is_likely_automated, unwrap_tool_result  # noqa: E402
from timeutil import _env_tz, _now_local, _resolve_tz, configured_tz  # noqa: E402

TERMINAL_RETENTION_DAYS = 30          # continuity.rs:13
DEADLINE_GRACE_DAYS = 2              # legacy undated meeting-shadow retirement grace
CHASE_AFTER_DAYS = 3                 # default for SOTTO_CHASE_AFTER_DAYS (deadline-less waiting_ons)
CHASE_MAX = ledger_io.CHASE_MAX      # after two chases, stop nudging and hand it to sotto-loops
# The direction eligible for reminder chases — defined once in ledger_io so the resolver,
# loops_query and retune_scan cannot disagree about which way an obligation points.
is_waiting_on = ledger_io.is_waiting_on
ACTIVE = ledger_io.ACTIVE             # continuity.rs:227 — single source in ledger_io
TERMINAL = ledger_io.TERMINAL         # continuity.rs:230 (apply_commitments uses cr.TERMINAL)
PARKED = ledger_io.PARKED             # kept, hidden, revived by a touch — defined once in ledger_io
PARK_AFTER_DAYS = ledger_io.PARK_AFTER_DAYS
# The closed family vocabulary lives in ledger_io beside WAITING_ON_TYPES — one owner for "which
# words are which kind of debt", read here and by every future reader.
MEETING_TYPES = ledger_io.MEETING_TYPES                 # continuity.rs:1001
SCHEDULING_TYPES = ledger_io.SCHEDULING_TYPES
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
            "mon ", "tue ", "wed ", "thu ", "fri ", "sat ", "sun "]   # continuity.rs:564-565


def _data_root() -> str:
    return os.environ.get("SOTTO_DATA", "/data")


def _dir() -> str:
    return os.path.join(_data_root(), "knowledge", "continuity")


@contextmanager
def _ledger_lock():
    """Cross-process mutation lock. The resolver, Granola capture and user corrections all use this
    same file, so a load/check/write sequence cannot interleave with another writer."""
    os.makedirs(_dir(), exist_ok=True)
    with open(os.path.join(_dir(), ".ledger.lock"), "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _normalize_action(a: dict) -> dict:
    """Map the brief's camelCase actionItems onto the snake_case shape continuity expects.
    Falls through to snake_case keys if already normalized (so both shapes work). PORT: the Mac
    pipeline builds an internal ActionItem from the FLEX actionItems before continuity.rs runs;
    without this shim every field reads None and all anchor keys collapse to "::"."""
    g = a.get
    return {
        "loop_id": g("loop_id") or g("loopId"),
        "new_obligation": g("new_obligation") is True or g("newObligation") is True,
        "action_type": g("action_type") or g("type"),
        "channel": g("channel"),
        "canonical_id": g("canonical_id"),
        "contact_identifier": g("contact_identifier") or g("contactIdentifier"),
        "contact_name": g("contact_name") or g("contactName") or "",
        "group_id": g("group_id") or g("groupId"),
        "source_thread_id": g("source_thread_id") or g("emailThreadId"),
        "source_message_id": g("source_message_id") or g("emailMessageId"),
        "summary": g("summary") or g("contextSummary") or g("prose") or "",
        "ask": g("ask") or g("contextAsk"),
        "meeting_time": g("meeting_time") or g("meetingTime"),
        "deadline": g("deadline") or g("deadlineDate") or g("contextDeadline"),
        "created_at": g("created_at"),
        "source": g("source"),
        "source_refs": g("source_refs") or g("evidence"),
        "resolution_mode": g("resolution_mode"),
    }


def normalize_channel(ch: str) -> str:
    ch = (ch or "").lower()
    if ch in ("gmail", "email", "apple_mail"):
        return "email"
    return ch


# The spelling normalizer lives in ledger_io so the writer and every read view classify a
# `Waiting-On ` exactly the same way (one concept, one implementation).
_normalize_action_type = ledger_io.normalize_action_type


def action_family(t: str) -> str:
    """The debt's family — the CLOSED set, so a word the extractor invents can never fork a row.

    continuity.rs:521-528 grouped related type spellings, but it returned an UNRECOGNIZED type
    verbatim, which made the family as open-ended as the
    model's vocabulary: one real ledger carried `action`, `task`, `review`, `read`, `info`,
    `reminder`, `document_mention`, `action_required` and `email_follow` alongside the documented
    types. The default closes that vocabulary: an obligation Sotto cannot classify still belongs
    to the ordinary follow-up family. Task identity is decided separately below.

    DIRECTION IS ITS OWN FAMILY: a reply you owe them and a deliverable they owe you are two debts
    even when they live on one thread, and merging them silently inverts resolution and the
    chase. So `waiting_on` anchors apart from the follow_up family it used to share — and it is the
    ONE family the default may never swallow, which is why it is tested first.
    """
    t = _normalize_action_type(t)
    if t in ledger_io.WAITING_ON_TYPES:
        return t
    if t in MEETING_TYPES:
        return "meeting"
    if t in SCHEDULING_TYPES:
        return "scheduling"
    return "follow_up"


def _normalize_name_for_dedup(name: str) -> str:
    # continuity.rs:234-238 — strip "<email>", keep first two words, lowercase.
    name = name or ""
    name = name.split("<", 1)[0].strip() if "<" in name else name
    return " ".join(name.split()[:2]).lower()


def _normalize_identifier_for_anchor(value: str):
    # continuity.rs:257-270 — email lowercased; phone → last 10 digits; else lowercased.
    trimmed = (value or "").strip()
    if not trimmed:
        return None
    if "@" in trimmed:
        return trimmed.lower()
    digits = re.sub(r"\D", "", trimmed)
    if len(digits) >= 10:
        return digits[-10:]
    return trimmed.lower()


def contact_anchor(canonical_id, identifier, name, group_id=None) -> str:
    # continuity.rs:272-282 — gid: > cid: > id: > name:. A group ask is keyed by the GROUP'S OWN ID
    # and a person ask by the person (canonical_id absorbs the same human's email-vs-phone drift);
    # the name is the last resort, because a name is what the extractor typed that day.
    if group_id and str(group_id).strip():
        return f"gid:{str(group_id).strip().lower()}"
    if canonical_id and str(canonical_id).strip():
        return f"cid:{str(canonical_id).strip().lower()}"
    norm = _normalize_identifier_for_anchor(identifier or "")
    if norm:
        return f"id:{norm}"
    return f"name:{_normalize_name_for_dedup(name or '')}"


# The thread ids Sotto MINTS for itself: a content hash standing in for a debt that has no
# counterpart to be keyed by (a commitment with no recipient, a hand-added loop). They are not
# conversations — they ARE the identity, so they always key the anchor. Collapsing them onto their
# contact would silently drop the second of two distinct commitments from the same meeting.
SYNTHETIC_THREAD_PREFIXES = ("commitment:", "granola:", "manual:")


def _is_synthetic_thread(tid: str) -> bool:
    return _s(tid).strip().lower().startswith(SYNTHETIC_THREAD_PREFIXES)


def has_counterpart(a: dict) -> bool:
    """Is there a WHO to key this debt by — a group, a resolved person, or an identifier? (A bare
    name is not one: it is what the extractor typed that day, which is why it is the last resort.)"""
    return bool(_s(a.get("group_id")).strip() or _s(a.get("canonical_id")).strip()
                or _normalize_identifier_for_anchor(_s(a.get("contact_identifier"))))


def _thread_key(a: dict, tid: str) -> str:
    # Legacy/base thread key. Direction separates the historical shapes; `_obligation_key` adds a
    # task digest when the same thread contains more than one obligation.
    fam = action_family(a.get("action_type", ""))
    return f"thread:{tid}:{fam}" if fam in ledger_io.WAITING_ON_TYPES else f"thread:{tid}"


# A counterpart anchor that names a MACHINE-VERIFIED identity — the group's platform id, the
# person's canonical_id, or a normalized email/phone. The `name:` form is not one: it is whatever the
# extractor typed that day.
STRONG_ANCHOR_PREFIXES = ("gid:", "cid:", "id:")


def _is_strong_anchor(anchor: str) -> bool:
    return _s(anchor).startswith(STRONG_ANCHOR_PREFIXES)


def _counterpart_key(a: dict) -> str:
    """The counterpart-family BASE for an obligation key, not the full obligation identity.

    A verified identity is stable across channels; only the guessed `name:` fallback retains the
    channel to keep same-named strangers apart. `_obligation_key` carries an exact same task onto an
    existing row, honors an explicit loopId for a paraphrase, and adds a task digest when this
    counterpart has a distinct obligation. Direction remains part of the base family."""
    anchor = contact_anchor(a.get("canonical_id"), a.get("contact_identifier"),
                            a.get("contact_name", ""), a.get("group_id"))
    fam = action_family(a.get("action_type", ""))
    if _is_strong_anchor(anchor):
        return f"{fam}:{anchor}"
    return f"{normalize_channel(a.get('channel',''))}:{fam}:{anchor}"


def compute_anchor_key(a: dict) -> str:
    """Return the legacy/base key used before task-specific disambiguation.

    The base identifies counterpart plus direction-family. A synthetic thread minted for a
    standalone commitment, or a real thread with no known counterpart, supplies that base instead.
    Callers creating or matching obligations must use `_obligation_key`; this helper alone does not
    mean every task for one counterpart is the same obligation."""
    tid = _s(a.get("source_thread_id")).strip()
    if tid and not _s(a.get("group_id")).strip() and (_is_synthetic_thread(tid) or not has_counterpart(a)):
        return _thread_key(a, tid)
    return _counterpart_key(a)


def _same_counterpart(a: dict, b: dict) -> bool:
    return _counterpart_key(a) == _counterpart_key(b)


def _obligation_key(a: dict, items: dict, origin_key: str = "") -> str:
    named = _s(a.get("loop_id"))
    if named:
        row = items.get(named)
        if row and _same_counterpart(a, row):
            return named
    base = compute_anchor_key(a)
    if origin_key:
        for key, row in items.items():
            if _s(row.get("origin_key")) == origin_key and _same_counterpart(a, row):
                return key
        # The first observed obligation for a counterpart owns the stable base while retaining its
        # origin identity. That lets an ordinary paraphrase and a later flagged replay converge on
        # the same row; only explicit siblings need origin-suffixed keys.
        if base not in items:
            return base
        digest = hashlib.sha256(origin_key.encode()).hexdigest()[:16]
        return f"{base}:origin:{digest}"
    # A unique known source recovers the right active task or tombstone if extraction omits its
    # loopId. One message can contain several asks, so shared evidence cannot choose between them.
    # Disjoint evidence alone never creates a task: a later message may simply repeat an old ask.
    refs = _request_evidence(a)
    if refs:
        matches = [key for key, row in items.items()
                   if _same_counterpart(a, row) and refs & _request_evidence(row)]
        if len(matches) == 1:
            return matches[0]
    # Ordinary extraction is deliberately stable across prose changes. A second obligation must
    # opt in with validated source evidence; otherwise a daily paraphrase updates this base row.
    return base


def _prior_anchor_keys(a: dict) -> set:
    """Every anchor shape THIS file's own rules have ever minted for one identity — today's, the
    pre-merge one where a thread outranked the person, the channel-prefixed one from before a
    verified identity stood on its own, and the one an OPEN family vocabulary minted when an
    unrecognized `type` was its own family. `_migrate_identity` only re-anchors a row carrying one of
    these, so an anchor somebody else composed (apply_commitments' `:c:<hash>` suffix, a future
    scheme) is never rewritten by a migration that doesn't understand it."""
    anchor = contact_anchor(a.get("canonical_id"), a.get("contact_identifier"),
                            a.get("contact_name", ""), a.get("group_id"))
    channel = normalize_channel(a.get("channel", ""))
    keys = {_counterpart_key(a)}
    # Legacy: channel:<family-or-raw-type>:<anchor>. The raw type is included because the old
    # `action_family` returned an unknown type verbatim — that is exactly the row we came to fold.
    for fam in (action_family(a.get("action_type", "")), _normalize_action_type(a.get("action_type"))):
        keys.add(f"{channel}:{fam}:{anchor}")
    tid = _s(a.get("source_thread_id")).strip()
    if tid and not _s(a.get("group_id")).strip():
        keys.add(_thread_key(a, tid))
    return keys


# ── Is this a DEBT? (the ledger's one entry gate) ─────────────────────────────
# A ledger row is a DEBT: something a person is waiting on from the user, or something the user
# promised. Not everything that mentions them. The ledger used to accept whatever the extractor
# handed it, which admitted receipt reminders, benefits enrollment, a badge-setup invite, cold
# pitches from strangers, and rows with no summary at all. Active obligations now remain until
# source-bound completion or a user correction, so the entry gate must reject non-obligations.
#
# THREE TESTS, TWO OF WHICH NEED NO JUDGMENT and therefore live here rather than in the extraction
# prompt (a model skips instructions; code doesn't):
#   1. A debt has to SAY what is owed. Under MIN_DEBT_WORDS words of summary-and-ask there is
#      nothing anyone could act on ("Caregiver Update" with an empty summary).
#   2. A debt is owed by or to SOMEBODY. No group, no person, no identifier and no name is not a
#      counterpart — and worse, every such row collapses onto the one anchor ending `name:`, so two
#      unrelated asks become one. It must never mint a row.
#   3. Nobody is waiting at a no-reply address. The counterpart test is textutil's
#      `_is_likely_automated` — the SAME predicate event triage drops automated senders with, so
#      the ledger is not re-deciding, badly, a question that already has one owner.
# The third shape of junk — is this them ANSWERING him, an FYI, or a cold pitch rather than an ask?
# — is judgment about what a message MEANS, so it belongs to the extraction prompt (see
# references/extraction-prompt.md, "The Debt Test"). Faking it here with keyword matching would drop
# real asks, and a brief that says "nothing for you today" is the worse failure.
MIN_DEBT_WORDS = 3
# The degenerate anchor: `…:name:` with nothing after it — an action that identifies nobody.
_ANCHORS_NOBODY = "name:"


def not_a_debt(a: dict) -> str:
    """Why this action may never open a ledger row, or "" when it may. Takes the snake_case shape
    (`_normalize_action`'s output), so it reads the same fields the anchor does. Deterministic, and
    every branch is one sentence a stranger can check."""
    words = f"{_s(a.get('summary'))} {_s(a.get('ask'))}".split()
    if len(words) < MIN_DEBT_WORDS:
        return "no summary — a debt has to say what is owed"
    if compute_anchor_key(a).endswith(_ANCHORS_NOBODY):
        return "no counterpart — a debt is owed by or to somebody"
    ident = _s(a.get("contact_identifier")).strip()
    if _is_likely_automated(ident):
        return f"automated counterpart <{ident}> — nobody is waiting at a no-reply address"
    return ""


# ── Group identity (the one counterpart with no per-person identifier) ────────
# A 1:1 loop anchors on an email or a phone number the data hands us. A GROUP had neither, so its
# anchor fell back to `name:<whatever the extractor called the group that day>` — an identity the
# model is free to reword, which is how one group ask became two open rows with two different
# labels. The Bridge already carries the group's stable `chat_guid` in LocalData; render_local
# exposes it to the model as `group_id:` and indexes it here, so a group's identity is a machine id
# and its label is just a label. Import-guarded: without the renderer, identity degrades to today's
# name fallback rather than costing a resolve.
try:
    import render_local as _render_local  # noqa: E402
except Exception:  # noqa: BLE001  # pragma: no cover — renderer is always importable in-tree
    _render_local = None

# ── Person identity (the knowledge graph resolves who, before anchoring keys them) ────────────────
# A person file IS the store of "these identifiers, this name, one human", and the graph's own index
# answers "who is this?" — canonical_id → identifier → exact name slug. Continuity asks THAT index
# rather than growing a second matcher, so a capture carrying only "Spencer Schneier" and one
# carrying his email share a counterpart base while distinct tasks still separate. What it
# deliberately does NOT use is
# knowledge_update's dedup-lite fuzzy tail (containment / similarity): a merge SUGGESTION a human
# confirms is not the same act as silently re-keying somebody's debt (see resolve_canonical_id).
# Import-guarded like the renderer: no graph, no resolution, today's behavior — never a failed
# resolve.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shared", "knowledge"))
try:
    import knowledge as _kg  # noqa: E402
    import knowledge_update as _kgu  # noqa: E402
except Exception:  # noqa: BLE001  # pragma: no cover — the graph is always importable in-tree
    _kg = _kgu = None


def person_identity() -> dict:
    """The knowledge graph's people index plus a path→canonical_id reverse map, or {} when there is
    no graph to resolve against. Built ONCE per resolve — one directory scan, not one per action."""
    if not (_kg and _kgu):
        return {}
    try:
        idx = _kg.build_people_index()
        return {"index": idx,
                "cid_by_path": {os.path.realpath(p): cid for cid, p in (idx.get("by_cid") or {}).items()}}
    except Exception:  # noqa: BLE001
        return {}


def resolve_canonical_id(name, identifier, person_index: dict) -> str:
    """The canonical_id of the person this debt is with, or "" — ambiguity is not a resolution, and
    neither is a shared name.

    ONE SENTENCE: an identifier IS an identity, so a debt carrying one resolves by that identifier
    or not at all, and a debt carrying none resolves only on a name the graph knows EXACTLY.

    A NAME MAY NEVER OVERRIDE AN UNKNOWN IDENTIFIER. `alex@fund.com` is a different Alex Kim than
    the one on file; letting his name win re-anchored his debt onto the known Alex's canonical_id,
    where `_migrate_identity` folded the two together and closed his ask as `merged_duplicate` —
    a stranger's debt, deleted, invisible in the brief and on the dashboard. So an identifier the
    graph does not contain resolves to nothing, and the name is not consulted.

    A FIRST NAME IS NOT A NAME, either: the graph's dedup-lite CONTAINMENT match ("Sam" ⊂ "Sam
    Patel") is right for a human reviewing a merge suggestion and wrong for silently re-keying a
    debt, so this asks the graph's exact index (`find_person_file` — the same lookup
    `resolve_relation_target` opens with) and stops there. A machine label ("Board sync", an email
    prefix) is never a person at all."""
    if not (person_index and _kg):
        return ""
    name, identifier = _s(name).strip(), _s(identifier).strip()
    index = person_index.get("index") or {}
    try:
        if identifier:
            path = _kg.find_person_file(identifier=identifier, index=index)
        elif name and is_name_shaped(name):
            path = _kg.find_person_file(name=name, index=index)
        else:
            path = None
    except Exception:  # noqa: BLE001
        return ""
    if not path or not os.path.exists(path):
        return ""
    return _s((person_index.get("cid_by_path") or {}).get(os.path.realpath(path)))


def group_identity(local: dict) -> dict:
    """The snapshot's group index ({"by_id", "by_label"} — see render_local.group_identity_index),
    or {} when there is no snapshot to build it from (an on-demand run without `local` simply keeps
    today's behavior)."""
    try:
        return _render_local.group_identity_index(local or {}) if _render_local else {}
    except Exception:  # noqa: BLE001
        return {}


def canonicalize_counterpart(a: dict, group_index: dict, person_index: dict | None = None) -> dict:
    """WHO is this debt with? A group's identity is its platform id, a person's is their
    canonical_id — and both are settled here, before any anchor is computed, so the anchor never has
    to guess. A group wins when the snapshot verifies one; otherwise the knowledge graph resolves
    the person. Neither is model-asserted, and an unresolved counterpart keeps today's behavior."""
    grouped = _canonicalize_group(a, group_index)
    if _s(grouped.get("group_id")).strip() or _s(grouped.get("canonical_id")).strip():
        return grouped
    cid = resolve_canonical_id(grouped.get("contact_name"), grouped.get("contact_identifier"),
                               person_index or {})
    return {**grouped, "canonical_id": cid} if cid else grouped


def _canonicalize_group(a: dict, group_index: dict) -> dict:
    """A group ask is owed by the GROUP, whose identity is its platform id — iMessage's chat_guid,
    WhatsApp's `…@g.us` JID — and whose name is only a label. An id the action carries counts ONLY
    when this snapshot actually contains it (source-verified, never model-asserted); a group ask
    with no id at all falls back to matching the label the renderer showed. Either way the action
    comes back carrying `group_id` and the group's own name."""
    if not (group_index and _render_local):
        return a
    by_id = group_index.get("by_id") or {}
    for candidate in (a.get("group_id"), a.get("contact_identifier"), a.get("source_thread_id")):
        hit = by_id.get(_render_local.group_id_key(_s(candidate)))
        if hit:
            channel, label = hit
            return {**a, "group_id": _render_local.group_id_key(_s(candidate)),
                    "channel": a.get("channel") or channel,
                    "contact_name": label or a.get("contact_name")}
    if _s(a.get("contact_identifier")).strip():
        return a                       # a 1:1 ask already has a person identifier — not a group
    hit = (group_index.get("by_label") or {}).get(
        (normalize_channel(a.get("channel", "")), _render_local.group_label_key(a.get("contact_name"))))
    if not hit:
        return a
    guid, label = hit
    return {**a, "group_id": _render_local.group_id_key(guid),
            "contact_name": label or a.get("contact_name")}


def _identifiers_match(a: str, b: str) -> bool:
    na, nb = _normalize_identifier_for_anchor(a), _normalize_identifier_for_anchor(b)
    return bool(na and nb and na == nb)


# ── Source-bound cross-channel evidence ────────────────────────────────────────────
# The brief may propose completion only for a named loop revision and a cited source message. The
# validator below confirms direction, timestamp, verbatim text and counterpart identity across
# email, iMessage and WhatsApp. Mere contact, any generic reply, a call or a calendar event closes
# nothing.

def _digits(s: str) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def _phone_matches(a: str, b: str) -> bool:
    da, db = _digits(a), _digits(b)
    if len(da) < 7 or len(db) < 7:
        return False
    return (da[-10:] if len(da) > 10 else da) == (db[-10:] if len(db) > 10 else db)


def _handle_matches(handle: str, ident: str) -> bool:
    if handle == ident:
        return True
    if "@" in handle and "@" in ident:
        return handle.lower() == ident.lower()
    return _phone_matches(handle, ident)


def _jid_matches_phone(jid: str, phone: str) -> bool:
    return _phone_matches((jid or "").split("@")[0], phone)


def _collect_all_identifiers(primary: str, contact_name: str, local: dict) -> list:
    """Expand only a contact card containing the primary identifier; a shared name proves nothing."""
    ids = [primary] if primary else []
    for contact in local.get("contacts") or []:
        values = [*(contact.get("emails") or []), *(contact.get("phones") or [])]
        if primary and any(_handle_matches(primary, _s(value)) for value in values):
            ids.extend(_s(value) for value in values if value)
    return list(dict.fromkeys(ids))


def _is_sent_email(m: dict) -> bool:
    labels = m.get("labelIds") or m.get("labels") or []
    return bool(m.get("isSent")) or (isinstance(labels, list)
                                     and "SENT" in {str(v).upper() for v in labels})


def _email_recipients(m: dict) -> list:
    """Lowercased addresses in To/Cc — a string ("A <a@x>, b@y") or a list of either."""
    raw = []
    for field in ("to", "cc"):
        v = m.get(field)
        raw += [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])
    return [addr.lower() for _name, addr in getaddresses(raw) if addr]


# Bare pleasantries are not new requests. This gate is only for reopening a closed request;
# completion itself uses a task-specific, source-bound proposal, never a text-length heuristic.
_CONTACT_ONLY_HINT = re.compile(
    r"^\W*(?:happy\s+\w+(?:\s+\w+)?|merry\s+\w+|congrat\w*|good\s+luck|well\s+done|thinking\s+of\s+you|"
    r"miss(?:ing)?\s+you|hope\s+you(?:'re|\s+are)\s+(?:well|ok|okay|good|doing\s+well)|"
    r"safe\s+travels|get\s+well(?:\s+soon)?|welcome\s+back|"
    r"thanks?(?:\s+you)?(?:\s+so\s+much|\s+a\s+lot)?|thx|ty|cheers|"
    r"ok(?:ay)?|k|yep|yup|nice|great|awesome|cool|lol|ha(?:ha)+|yay|"
    r"[\U0001F300-\U0001FAFF☀-➿]+)"
    r"[\W]*$", re.I)


def _is_answer(text: str) -> bool:
    """Exclude empty contact and bare pleasantries from fresh-request evidence."""
    t = _s(text).strip()
    return bool(t) and not _CONTACT_ONLY_HINT.match(t)


def _inbound_cutoff(created_at) -> str:
    """The instant a delivery has to beat — the cutoff BOTH directions use. New items carry a full
    local timestamp, which IS the cutoff. A LEGACY date-only `created_at` could have been recorded
    at any hour of that day, so nothing from that day counts — otherwise the 09:00 chat that
    preceded the 16:00 promise closes the debt on the day it was recorded, behind the user's back."""
    c = _s(created_at).strip()
    if not c:
        return ""
    return c if len(c) > 10 else f"{c[:10]} 23:59:59"


def _message_is_after(value, after: str) -> bool:
    """Compare ISO/local/RFC-2822 message dates against the ledger cutoff."""
    cutoff = _parse_dt(after)
    observed = _parse_dt(value)
    if observed is None:
        try:
            observed = parsedate_to_datetime(_s(value))
        except (TypeError, ValueError, OverflowError):
            return False
    if cutoff is None:
        return True
    return _to_user_zone(observed) > _to_user_zone(cutoff)


def _request_evidence(action: dict) -> set:
    refs = action.get("source_refs") or []
    if isinstance(refs, dict):
        refs = [refs]
    result = {(normalize_channel(_s(ref.get("sourceType") or ref.get("source_type") or ref.get("source"))),
               _s(ref.get("sourceId") or ref.get("source_id") or ref.get("id")))
              for ref in refs if isinstance(ref, dict)}
    if action.get("source_message_id"):
        result.add(("email", _s(action["source_message_id"])))
    return {(source, ident) for source, ident in result if source and ident}


def _observed_action_source(action: dict, local: dict,
                            observed_before: str) -> tuple[tuple[tuple[str, str], ...], str] | None:
    """Bind an explicitly separate obligation to messages that were actually in the snapshot.

    Identity, verbatim quotation when supplied, and an observed timestamp are mandatory. The
    timestamp becomes the obligation's chronology; extraction time and rewritten prose cannot mint
    a second row or make an old request look new.
    """
    refs = action.get("source_refs") or []
    if isinstance(refs, dict):
        refs = [refs]
    if not isinstance(refs, list) or not refs:
        return None
    primary = _s(action.get("contact_identifier"))
    identifiers = _collect_all_identifiers(primary, "", local)
    bound, observed_times = [], []
    for ref in refs:
        if not isinstance(ref, dict):
            return None
        source = normalize_channel(_s(ref.get("sourceType") or ref.get("source_type") or ref.get("source")))
        source_id = _s(ref.get("sourceId") or ref.get("source_id") or ref.get("id"))
        field = {"email": "emails", "imessage": "imessage", "whatsapp": "whatsapp"}.get(source)
        quote = " ".join(_s(ref.get("snippet")).split())
        if not field or not source_id:
            return None
        found = None
        for message in local.get(field) or []:
            ids = {_s(message.get(k)) for k in
                   ("source_id", "id", "messageId", "message_id", "guid", "rowid")}
            if source != "email" and _render_local:
                ids.add(_render_local.message_evidence_id(message, source))
            if source_id not in ids:
                continue
            timestamp = message.get("date") or message.get("timestamp")
            observed = _parse_dt(timestamp)
            if observed is None:
                try:
                    observed = parsedate_to_datetime(_s(timestamp))
                except (TypeError, ValueError, OverflowError):
                    observed = None
            text = _s(message.get("body") or message.get("text") or message.get("snippet"))
            if (observed is None or not _is_answer(text)
                    or _message_is_after(timestamp, observed_before)):
                continue
            if quote and quote not in " ".join(text.split()):
                continue
            if source == "email":
                outgoing = _is_sent_email(message)
                handles = (_email_recipients(message) if outgoing else
                           [parseaddr(_s(message.get("from") or message.get("sender")))[1]])
                group = ""
            else:
                handles = [_s(message.get("handle") or message.get("contact_jid"))]
                group = _s(message.get("chat_guid")) or (handles[0] if handles[0].endswith("@g.us") else "")
            if action.get("group_id"):
                matches = bool(_render_local and group and
                    _render_local.group_id_key(group) == _render_local.group_id_key(action["group_id"]))
            else:
                matches = not (group or message.get("is_group_chat")) and any(
                    _handle_matches(handle, ident) or _jid_matches_phone(handle, ident)
                    for handle in handles for ident in identifiers)
            if matches:
                found = _to_user_zone(observed).strftime("%Y-%m-%d %H:%M:%S")
                break
        if not found:
            return None
        bound.append((source, source_id))
        observed_times.append(found)
    return tuple(sorted(set(bound))), min(observed_times)


# Captured promises name concrete deliverables. The model still proposes completion, but a
# counterpart's unrelated reply (or a cherry-picked phrase inside a negation) cannot prove it.
_COMPLETION_FILLER = frozenset("a an the to of and for with on in at from by as into "
    "i we you he she they me us him her them my our your his their its it this that "
    "please complete finish make do get have has had will would can could "
    "should commit committed promise promised need needs".split())
_FULFILLMENT_CUE = re.compile(
    r"\b(?:sent|shared|forwarded|provided|delivered|submitted|attached|uploaded|completed|"
    r"finished|confirmed|updated|reviewed|signed|paid|booked|scheduled|introduced|enclosed)\b"
    r"|\bhere(?:'s| is| are)\b", re.I)
_UNFINISHED_CUE = re.compile(
    r"\b(?:not|never|haven't|hasn't|hadn't|didn't|don't|doesn't|can't|cannot|couldn't|"
    r"won't|wouldn't|isn't|aren't|wasn't|weren't|will|would|could|should|might|may|"
    r"plan|planning|hope|hoping|intend|intending|pending|without|almost|nearly|thought|mistaken)\b|\b\w+'ll\b|\b(?:going|need|needs) to\b|\?", re.I)

# The object alone is insufficient: reviewing a deck is not sending it. These conservative
# action families accept affirmative evidence; unfamiliar wording waits for user correction.
_CAPTURED_ACTIONS = (
    (frozenset("send share forward provide deliver attach upload submit".split()),
     re.compile(r"\b(?:sent|shared|forwarded|provided|delivered|attached|uploaded|submitted|enclosed)\b|\bhere(?:'s| is| are)\b", re.I)),
    (frozenset("review read".split()), re.compile(r"\b(?:reviewed|read)\b", re.I)),
    (frozenset("sign".split()), re.compile(r"\bsigned\b", re.I)),
    (frozenset("pay".split()), re.compile(r"\bpaid\b", re.I)),
    (frozenset("book schedule".split()), re.compile(r"\b(?:booked|scheduled|confirmed)\b", re.I)),
    (frozenset("confirm".split()), re.compile(r"\bconfirmed\b", re.I)),
    (frozenset("update".split()), re.compile(r"\bupdated\b", re.I)),
    (frozenset("introduce".split()), re.compile(r"\bintroduced\b", re.I)),
)


def _captured_fulfillment(row: dict, quote: str, text: str) -> bool:
    """Require the deliverable in an affirmative source clause; uncertain shorthand stays open.

    This is a conservative necessary check, not another semantic model. Existing proposal,
    identity, direction, chronology and verbatim-source checks still apply. User corrections do
    not go through this guard. Legacy captured rows without `ask` retain their summary fallback.
    """
    def words(value):
        return set(re.findall(r"[^\W_]+", _s(value).casefold()))

    task = _s(row.get("ask")).strip()
    if not task:
        task = re.sub(r"^(?:you committed to:|.+? owes:)\s*", "", _s(row.get("summary")), flags=re.I)
        task = re.split(r' \(from "| [—–] due ', task, maxsplit=1)[0]
    people = words(row.get("contact_name")) | words(os.environ.get("SOTTO_USER_NAME", ""))
    # Only action positions are verbs: "review" in "send the review deck" is an object qualifier.
    action_words = set()
    actions = []
    for verbs, cue in _CAPTURED_ACTIONS:
        matched = {verb for verb in verbs if re.search(
            r"(?:^|\b(?:please|to|and|then)\s+)" + verb + r"\b", task, re.I)}
        if matched:
            action_words.update(matched)
            actions.append(cue)
    terms = words(task) - _COMPLETION_FILLER - people - action_words
    if not terms or not terms <= words(quote):
        return False
    # Inspect the source sentence around the quote, not just the model's chosen substring:
    # `sent the deck` inside `I haven't sent the deck` must remain an outstanding promise.
    normalized = " ".join(text.replace("’", "'").split())
    quoted = " ".join(quote.replace("’", "'").split())
    start = normalized.find(quoted)
    if start < 0:
        return False
    left = max(normalized.rfind(mark, 0, start) for mark in ('.', '!', '?')) + 1
    end = start + len(quoted)
    ends = [pos + 1 for mark in ('.', '!', '?')
            if (pos := normalized.find(mark, end)) >= 0]
    clause = normalized[left:min(ends) if ends else len(normalized)]
    if _UNFINISHED_CUE.search(clause):
        return False
    return (all(cue.search(clause) for cue in actions) if actions
            else bool(_FULFILLMENT_CUE.search(clause)))


def _completion_evidence(row: dict, update: dict, local: dict):
    """Validate identity, direction, time and a verbatim quotation; the existing brief judges meaning.

    Raw text is data, never a request to change the ledger. An unsupported/missing proposal leaves
    the obligation open. One proposal names one row; meeting attendees and thread IDs alone prove
    nothing. Local contact expansion requires the primary identifier to be present on that card.
    """
    primary = _s(row.get("contact_identifier"))
    identifiers = _collect_all_identifiers(primary, "", local)
    wanted_inbound = is_waiting_on(row.get("action_type"))
    refs = update.get("evidence")
    if not isinstance(refs, list) or not refs:
        return None
    for evidence in refs:
        if not isinstance(evidence, dict):
            return None
        source = normalize_channel(_s(evidence.get("sourceType")))
        field = {"email": "emails", "imessage": "imessage", "whatsapp": "whatsapp"}.get(source)
        quote = " ".join(_s(evidence.get("snippet")).split())
        source_id = _s(evidence.get("sourceId"))
        if not field or not source_id or not quote:
            return None
        found = None
        for message in local.get(field) or []:
            ids = {_s(message.get(k)) for k in ("source_id", "id", "messageId", "message_id", "guid", "rowid")}
            if source != "email" and _render_local:
                ids.add(_render_local.message_evidence_id(message, source))
            if source_id not in ids:
                continue
            outgoing = _is_sent_email(message) if source == "email" else bool(message.get("is_from_me"))
            if outgoing == wanted_inbound:
                continue
            timestamp = message.get("date") or message.get("timestamp")
            if not _message_is_after(timestamp, _inbound_cutoff(row.get("created_at"))):
                continue
            text = _s(message.get("body") or message.get("text") or message.get("snippet"))
            if quote not in " ".join(text.split()):
                continue
            if (row.get("resolution_mode") == "source_grounded"
                    and update.get("status") == "resolved"
                    and not _captured_fulfillment(row, quote, text)):
                continue
            if source == "email":
                handles = _email_recipients(message) if outgoing else [parseaddr(_s(message.get("from") or message.get("sender")))[1]]
                group = ""
            else:
                handles = [_s(message.get("handle") or message.get("contact_jid"))]
                group = _s(message.get("chat_guid")) or (handles[0] if handles[0].endswith("@g.us") else "")
            if row.get("group_id"):
                matches = bool(_render_local and group and _render_local.group_id_key(group) == _render_local.group_id_key(row["group_id"]))
            else:
                matches = not (group or message.get("is_group_chat")) and any(
                    _handle_matches(handle, ident) or _jid_matches_phone(handle, ident)
                    for handle in handles for ident in identifiers)
            if matches:
                found = {"timestamp": _s(timestamp), "text": text,
                         "description": f"{'Inbound' if wanted_inbound else 'Outgoing'} {source}: {quote}"}
                break
        if not found:
            return None
    return found


def _new_request_since_closure(action: dict, closed: dict, local: dict) -> str:
    """A person is not a dismissed request. Reopen only for a newly sourced incoming message
    after the user's closure, never a new extraction timestamp or a paraphrase of old evidence.
    Existing evidence fields bind local messages; a new email thread can bind without message ID.
    Legacy date-only closures conservatively cover that entire day.
    """
    cutoff = _s(closed.get("closed_at")) or _inbound_cutoff(closed.get("resolved_at"))
    if not cutoff or not _parse_dt(cutoff):
        return ""
    identifiers = _collect_all_identifiers(action.get("contact_identifier") or "",
                                           action.get("contact_name") or "", local)
    fresh_refs = _request_evidence(action) - _request_evidence(closed)
    thread = _s(action.get("source_thread_id"))
    new_thread = bool(thread and thread != _s(closed.get("source_thread_id"))
                      and not _is_synthetic_thread(thread))
    for source, field in (("email", "emails"), ("imessage", "imessage"), ("whatsapp", "whatsapp")):
        for message in local.get(field) or []:
            outgoing = _is_sent_email(message) if source == "email" else message.get("is_from_me")
            if outgoing:
                continue
            timestamp = message.get("date") or message.get("timestamp")
            if not _message_is_after(timestamp, cutoff):
                continue
            ids = {_s(message.get(key)) for key in ("source_id", "id", "messageId", "message_id", "guid", "rowid")}
            if source != "email" and _render_local:
                ids.add(_render_local.message_evidence_id(message, source))
            bound = any(src == source and ident in ids for src, ident in fresh_refs)
            if source == "email":
                bound = bound or (new_thread and thread == _s(message.get("threadId") or message.get("thread_id")))
                handle = parseaddr(_s(message.get("from") or message.get("sender")))[1].lower()
            else:
                handle = _s(message.get("handle") or message.get("contact_jid"))
            group = _s(action.get("group_id"))
            message_group = _s(message.get("chat_guid"))
            if group:
                counterpart_matches = bool(_render_local and message_group
                    and _render_local.group_id_key(message_group) == _render_local.group_id_key(group))
            else:
                # Group messages name an individual sender, not a one-to-one conversation.
                counterpart_matches = not (message_group or message.get("is_group_chat")) and any(
                    _handle_matches(handle, ident) or _jid_matches_phone(handle, ident) for ident in identifiers)
            if not bound or not counterpart_matches:
                continue
            if not _is_answer(message.get("body") or message.get("text") or message.get("snippet")):
                continue
            observed = _parse_dt(timestamp)
            if observed is None:
                observed = parsedate_to_datetime(_s(timestamp))
            return _to_user_zone(observed).strftime("%Y-%m-%d %H:%M:%S")
    return ""


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_PROMISE_WEEKDAY_RE = re.compile(
    r"\b(?:by|on|before|until|till|this|next)\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", re.I)
_PROMISE_MONTHDAY_RE = re.compile(
    r"\b(?:by|on|before|until|till)\s+(?:the\s+)?(?:(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
    r"|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?)\b", re.I)


def promised_date(text: str, ref: datetime):
    """The date a reply names for delivery — "by Tuesday", "tomorrow", "on the 16th of March",
    "end of week", "next week" — as YYYY-MM-DD, or None when the text names none. Deliberately
    small: a promise Sotto can't read is a promise it waits the usual days on."""
    t = _s(text).lower()
    if not t:
        return None
    day = ref.date() if isinstance(ref, datetime) else ref
    m = _PROMISE_MONTHDAY_RE.search(t)
    if m:
        dnum = int(m.group(1) or m.group(4))
        mon = (m.group(2) or m.group(3))[:3]
        month = next(i for i, name in enumerate(_MONTHS, 1) if name.startswith(mon))
        try:
            candidate = day.replace(month=month, day=dnum)
        except ValueError:
            return None
        if candidate < day:
            try:
                candidate = candidate.replace(year=day.year + 1)
            except ValueError:
                return None
        return candidate.strftime("%Y-%m-%d")
    if re.search(r"\btomorrow\b", t):
        return (day + timedelta(days=1)).strftime("%Y-%m-%d")
    if re.search(r"\b(?:end of (?:the )?week|eow|this week)\b", t):
        ahead = (4 - day.weekday()) % 7          # the coming Friday (today, if Friday)
        return (day + timedelta(days=ahead)).strftime("%Y-%m-%d")
    if re.search(r"\bnext week\b", t):
        return (day + timedelta(days=7)).strftime("%Y-%m-%d")
    if re.search(r"\b(?:end of (?:the )?month|eom)\b", t):
        first_next = (day.replace(day=1) + timedelta(days=32)).replace(day=1)
        return (first_next - timedelta(days=1)).strftime("%Y-%m-%d")
    m = _PROMISE_WEEKDAY_RE.search(t)
    if m:
        target = next(i for i, name in enumerate(_WEEKDAYS) if name.startswith(m.group(1).lower()))
        ahead = (target - day.weekday()) % 7 or 7
        return (day + timedelta(days=ahead)).strftime("%Y-%m-%d")
    return None


def _s(v) -> str:
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.isoformat()   # unquoted YAML dates parse as date/datetime — stringify as ISO
    return str(v)


_TZ_CACHE: dict = {}


def _user_tzinfo():
    """The user's zone (SOTTO_TIMEZONE / wizard-detected settings, via compose_brief), else UTC.
    Cached per configured value: without the cache the settings file is re-read/parsed for every
    datetime conversion — O(items × events) file I/O per brief."""
    key = _env_tz() or ""
    if key not in _TZ_CACHE:
        _TZ_CACHE[key] = _resolve_tz((key or configured_tz()) or "+00:00") or timezone.utc
    return _TZ_CACHE[key]


def _parse_dt(s):
    """ISO datetime with PROPER offset handling ('Z' / '±HH:MM', 'T' or space separator, date-only).
    The old strptime(...[:19]) silently dropped the UTC offset — an off-by-one day near midnight
    across zones. Returns a (possibly naive) datetime, or None when unparseable."""
    s = _s(s).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:  # tolerate junk after the seconds (e.g. a nonstandard fraction/suffix)
            return datetime.fromisoformat(s[:19].replace(" ", "T"))
        except ValueError:
            return None


def _to_user_zone(dt: datetime) -> datetime:
    """Aware → converted to the user's zone; naive → assumed already user-local (ledger timestamps
    and Mac-side local data carry no offset)."""
    tzi = _user_tzinfo()
    return dt.replace(tzinfo=tzi) if dt.tzinfo is None else dt.astimezone(tzi)


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def chase_after_days() -> int:
    """SOTTO_CHASE_AFTER_DAYS (default 3) — how long a deadline-less `waiting_on` sits before it
    becomes chase-eligible, and the gap between chases. Read like every other knob: env, floored."""
    return max(1, _int((os.environ.get("SOTTO_CHASE_AFTER_DAYS") or "").strip(), CHASE_AFTER_DAYS))


def _days_old(created_at, ref: datetime):
    try:
        return (ref - datetime.strptime(_s(created_at)[:10], "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return None


def _relationship_history() -> dict:
    try:
        with open(os.path.join(_data_root(), "knowledge", "relationship_state.json"),
                  encoding="utf-8") as f:
            history = json.load(f).get("history", {})
        return history if isinstance(history, dict) else {}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _learned_chase_after(it: dict, default_at: datetime, evaluation_now: datetime,
                         obligation_at: datetime | None = None, history: dict | None = None) -> datetime:
    """Apply finite counterpart latency only as a delay, never beyond an obligation deadline."""
    default_at = _to_user_zone(default_at)
    evaluation_now = _to_user_zone(evaluation_now)
    obligation_at = _to_user_zone(obligation_at) if obligation_at is not None else None
    try:
        from relationship_importance import engagement_for_contact, learned_chase_after
        contact = {"name": it.get("contact_name"), "canonical_id": it.get("canonical_id")}
        engagement = engagement_for_contact(contact, history if history is not None
                                            else _relationship_history())
        learned = learned_chase_after(default_at, engagement, evaluation_now, obligation_at)
        learned = _to_user_zone(learned) if isinstance(learned, datetime) else default_at
    except (OSError, ValueError, TypeError, AttributeError):
        learned = default_at
    deadline = _s(it.get("deadline"))[:10]
    if deadline:
        try:
            deadline_at = datetime.strptime(deadline, "%Y-%m-%d").replace(tzinfo=learned.tzinfo)
            if deadline_at <= evaluation_now:
                return default_at
            learned = min(learned, deadline_at)
        except ValueError:
            pass
    return max(default_at, learned)


def chase_due(it: dict, today: str, ref: datetime, history: dict | None = None) -> bool:
    """Is this waiting_on due for a chase? Deadline-less ones ripen after `chase_after_days`;
    a post-deadline one escalates immediately (it's already late); `chase_after` (stamped by the
    last chase) governs afterwards; and after CHASE_MAX chases we stop and leave it to sotto-loops."""
    if not is_waiting_on(it.get("action_type")):
        return False
    if _int(it.get("chased_count")) >= CHASE_MAX:
        return False
    after = _s(it.get("chase_after"))[:10]
    if after:
        return after <= today
    deadline = _s(it.get("deadline"))[:10]
    if deadline:
        return deadline < today          # post-deadline → escalate now, no ripening wait
    try:
        created = datetime.strptime(_s(it.get("created_at") or today)[:10], "%Y-%m-%d").replace(
            tzinfo=_user_tzinfo())
    except ValueError:
        return False
    due = _learned_chase_after(it, created + timedelta(days=chase_after_days()), ref, created,
                               history)
    return due.date().isoformat() <= today


def _clear_stale_pending(active: list, today: str):
    """A chase stamped yesterday but never delivered leaves NO trace: the pending stamp is dropped
    and `chased_count` was never touched. A nudge the user never saw must not be one of the two
    they get."""
    for it in active:
        pending = _s(it.get("chase_pending"))[:10]
        if pending and pending < today:
            it.pop("chase_pending", None)
            # Count the stall: the day's one stamp went to this row and nothing was delivered.
            # _stamp_chase ranks stalled rows last, so one blocked loop (quiet hours at the tick,
            # a snooze, a dead channel) rotates to the back instead of holding the whole chase
            # lane every morning (Day-7 simulation, Sep 2026).
            it["chase_stalls"] = int(it.get("chase_stalls") or 0) + 1
            _persist(it)


def _stamp_chase(active: list, today: str, ref: datetime):
    """ONE chase stamp per local day, on ONE item — the oldest/most-overdue eligible waiting_on.
    PHASE ONE of two: this writes `chase_pending` and nothing else. `chased_count`, `last_chased_at`
    and `chase_after` all move in --finalize-chase, which proactive_scan calls after the nudge
    actually went out — a chase counts when it is delivered, not when it is proposed. This file
    stays the single writer either way."""
    if any(_s(it.get("last_chased_at"))[:10] == today or _s(it.get("chase_pending"))[:10] == today
           for it in active):
        return                            # already chased (or proposed a chase) today
    overdue = lambda it: bool(_s(it.get("deadline"))[:10] and _s(it.get("deadline"))[:10] < today)
    ranked = sorted(active, key=lambda it: (int(it.get("chase_stalls") or 0),
                                            not overdue(it),
                                            -(_days_old(it.get("created_at"), ref) or 0)))
    history = _relationship_history()
    for it in ranked:
        if chase_due(it, today, ref, history):
            it["chase_pending"] = today
            _persist(it)
            return


def _find_item(items: dict, key: str):
    """The ledger row answering to this anchor_key, by dict key or by stored frontmatter."""
    return items.get(key) or next((v for v in items.values() if _s(v.get("anchor_key")) == key), None)


def _follow_merge(items: dict, it: dict) -> dict:
    """The LIVE row a folded one became. A nudge is delivered against the anchor_key its lane was
    holding, and an evening fold can retire that row between the nudge going out and the finalize
    coming back — writing the delivery there would credit a dismissed row while the live debt keeps
    its old count, and the user gets nudged past CHASE_MAX. `merged_into` is the forwarding address
    the fold leaves behind; follow it (guarded against a cycle) to the row that is actually open."""
    seen = set()
    while _s(it.get("status")) in TERMINAL and _s(it.get("merged_into")).strip():
        key = _s(it["merged_into"]).strip()
        if key in seen:
            break
        seen.add(key)
        nxt = _find_item(items, key)
        if nxt is None:
            break
        it = nxt
    return it


def _finalize_chase_unlocked(anchor_key: str, now: datetime | None = None) -> dict:
    """PHASE TWO: the nudge was DELIVERED, so the chase now counts. Called by proactive_scan's fired
    path (`continuity_resolve.py --finalize-chase <anchor_key>`) so the ledger keeps exactly one
    writer. Idempotent within the day; a no-op when nothing was pending, and never a write to a
    TERMINAL row — a chase lands on the live debt (via `merged_into`) or nowhere."""
    now = now or _now_local(configured_tz() or "+00:00")
    today = _to_user_zone(now).strftime("%Y-%m-%d")
    key = (anchor_key or "").strip()
    if not key:
        return {"ok": False, "detail": "missing anchor_key"}
    items = _load_items()
    it = _find_item(items, key)
    if it is None:
        return {"ok": False, "anchor_key": key, "detail": "no ledger item with that anchor_key"}
    it = _follow_merge(items, it)
    if it.get("status", "open") in TERMINAL:
        return {"ok": False, "anchor_key": key, "chased_count": _int(it.get("chased_count")),
                "detail": "ledger item is closed — a chase is never counted against a dead row"}
    if _s(it.get("last_chased_at"))[:10] == today:
        return {"ok": True, "anchor_key": key, "chased_count": _int(it.get("chased_count")),
                "detail": "already finalized today"}
    if _s(it.get("chase_pending"))[:10] != today:
        return {"ok": False, "anchor_key": key, "chased_count": _int(it.get("chased_count")),
                "detail": "no chase pending for today"}
    it["chased_count"] = _int(it.get("chased_count")) + 1
    it["last_chased_at"] = today
    it["chase_after"] = _learned_chase_after(
        it, _to_user_zone(now) + timedelta(days=chase_after_days()), now,
        _to_user_zone(now), _relationship_history()).strftime("%Y-%m-%d")
    it.pop("chase_pending", None)
    # A delivered chase ends the stall penalty: the stall was a property of the days the lane was
    # down, not of this loop, and a row must not stay demoted after a chase actually landed.
    it.pop("chase_stalls", None)
    _persist(it)
    return {"ok": True, "anchor_key": _s(it.get("anchor_key")) or key,
            "chased_count": it["chased_count"],
            "last_chased_at": today, "chase_after": it["chase_after"], "detail": "chase delivered"}


def finalize_chase(anchor_key: str, now: datetime | None = None) -> dict:
    with _ledger_lock():
        return _finalize_chase_unlocked(anchor_key, now)


def _finalize_handoff_unlocked(anchor_key: str, now: datetime | None = None) -> dict:
    """The hand-off question was DELIVERED, so the row records that the USER has been asked.

    The chase's two-phase rule, applied to the lane that ends it: a question counts when it reaches
    the person, never when it is proposed. Once `handoff_asked_at` is stamped the ask is the user's
    to answer — `brief_validate.is_urgent` stops giving the loop a named line in every brief and
    `proactive_scan._handoff_candidates` never asks again — and the loop waits in the count line and
    on `/app#loops` until they resolve it, drop it, or say keep waiting (each of which clears the
    whole chase state, `ledger_io.CHASE_STATE_FIELDS`). Idempotent; never written to a TERMINAL row.
    """
    now = now or _now_local(configured_tz() or "+00:00")
    today = _to_user_zone(now).strftime("%Y-%m-%d")
    key = (anchor_key or "").strip()
    if not key:
        return {"ok": False, "detail": "missing anchor_key"}
    items = _load_items()
    it = _find_item(items, key)
    if it is None:
        return {"ok": False, "anchor_key": key, "detail": "no ledger item with that anchor_key"}
    it = _follow_merge(items, it)
    if it.get("status", "open") in TERMINAL:
        return {"ok": False, "anchor_key": key,
                "detail": "ledger item is closed — nothing left to hand off"}
    already = _s(it.get("handoff_asked_at"))[:10]
    if already:
        return {"ok": True, "anchor_key": _s(it.get("anchor_key")) or key,
                "handoff_asked_at": already, "detail": "already asked"}
    it["handoff_asked_at"] = today
    _persist(it)
    return {"ok": True, "anchor_key": _s(it.get("anchor_key")) or key,
            "handoff_asked_at": today, "detail": "hand-off question delivered"}


def finalize_handoff(anchor_key: str, now: datetime | None = None) -> dict:
    with _ledger_lock():
        return _finalize_handoff_unlocked(anchor_key, now)


def meeting_passed(meeting_time: str, created_at: str, today: str) -> bool:
    """continuity.rs:536-573 — handles ISO timestamps AND relative times (vs created_at).
    Offset-bearing timestamps are converted to the USER'S zone before taking the date, so a meeting
    stored as e.g. 06:30Z (= 23:30 the previous day in LA) resolves on the right local day."""
    if not meeting_time:
        return False
    mt = meeting_time
    mtl = mt.lower()
    now = datetime.strptime(today, "%Y-%m-%d")
    # ISO-ish: "2026-03-12 10:00" / "2026-03-12T10:00:00-08:00" — compare the USER-LOCAL date part.
    if mt.startswith("20") and len(mt) >= 10:
        dt = _parse_dt(mt)
        if dt is None:
            return mt[:10] < today   # unparseable tail — fall back to the raw date prefix
        return _to_user_zone(dt).strftime("%Y-%m-%d") < today
    try:
        created = datetime.strptime((created_at or "")[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    if "tomorrow" in mtl and created <= now - timedelta(days=1):
        return True
    if "today" in mtl and created <= now - timedelta(days=1):
        return True
    if any(d in mtl for d in WEEKDAYS) and created <= now - timedelta(days=7):
        return True
    return False


def _load_items(with_shadowed: bool = False):
    """All ledger entries keyed by anchor_key (falling back to filename), each carrying its
    "_path" for persist-back. Loading/parsing is ledger_io's — one parser for every reader.
    MALFORMED files are skipped entirely: they must never be surfaced as active items and never
    persisted over (that used to rewrite them as '---\\n{}\\n---', destroying the content).

    TWO FILES, ONE ANCHOR_KEY — the duplicate nobody could see. A migration re-anchors a row in
    place (the file keeps its path, the key it answers to changes), so two files can end up carrying
    the same anchor_key. This dict then kept whichever sorted last and silently DROPPED the other:
    it was never reconciled or updated — but `ledger_io.load_active` reads files, not
    this dict, so the brief, the count line and /app#loops all went on showing it forever. Returned
    as `shadowed` with with_shadowed=True so the one pass that writes can fold them (resolve()); the
    read-only callers keep today's signature."""
    items, shadowed = {}, []
    malformed = []
    for fm in ledger_io.load_entries(with_path=True, include_bare=True):
        if fm.get("_malformed"):
            malformed.append(fm["_path"])
            continue
        if not any(k != "_path" for k in fm):
            # Bare file (no frontmatter, e.g. a plain .md dropped in the dir) or an empty '{}'
            # fence: adopting it would surface a content-free open loop and _persist would rewrite
            # the file as '---\n{}\n---', destroying its contents. Never adopt, never persist over.
            continue
        key = fm.get("anchor_key") or os.path.basename(fm["_path"])
        if key in items:
            shadowed.append(fm)
            continue
        items[key] = fm
    if malformed:
        print("[continuity_resolve] skipping malformed ledger file(s) (left untouched): "
              + ", ".join(malformed), file=sys.stderr)
    return (items, shadowed) if with_shadowed else items


def _fold_shadowed(items: dict, shadowed: list, today: str) -> list:
    """Fold every file that shares a live row's anchor_key into that row, by the same rules as any
    other duplicate (older age, newest words, the chases both have spent, loser terminal as
    `merged_duplicate`). A shadow of a TERMINAL row is simply retired — never resurrected."""
    folded = []
    for loser in shadowed:
        keeper = items.get(_s(loser.get("anchor_key")))
        if keeper is None or keeper is loser:
            continue
        if _s(loser.get("status", "open")) in TERMINAL:
            continue
        if _s(keeper.get("status", "open")) in TERMINAL:
            _terminate(loser, "dismissed", "merged_duplicate", today)
            loser["merged_into"] = _s(keeper.get("anchor_key"))
            _persist(loser)
        else:
            _fold_duplicate(keeper, loser, today)
        folded.append(loser)
    return folded


def _row_identity(it: dict) -> dict:
    """A ledger row's identity fields in the action shape compute_anchor_key reads."""
    return {"action_type": it.get("action_type"), "channel": it.get("channel"),
            "canonical_id": it.get("canonical_id"), "contact_identifier": it.get("contact_identifier"),
            "contact_name": it.get("contact_name"), "group_id": it.get("group_id"),
            "source_thread_id": it.get("source_thread_id"),
            "summary": it.get("summary"), "ask": it.get("ask")}


# The chase fields that fold by DATE (the count folds by max instead). Derived from ledger_io's
# one list, so a new chase field is folded the day it is named rather than silently dropped.
_CHASE_DATE_FIELDS = tuple(k for k in ledger_io.CHASE_STATE_FIELDS if k != "chased_count")


def _latest(a, b) -> str:
    """The later of two ledger dates, "" when neither is set. A date that exists always beats one
    that doesn't — an empty field is "never", not "long ago"."""
    sa, sb = _s(a)[:10], _s(b)[:10]
    return sa if sa >= sb else sb


def _fold_duplicate(keeper: dict, loser: dict, today: str):
    """Two rows proven to be the same obligation: the OLDER created_at is its real age, the NEWER words are the live
    ask, and the loser closes as bookkeeping — `merged_duplicate` is terminal and renders as nothing
    anywhere, because a dedupe is not an outcome that moved.

    EVERY CHASE FIELD FOLDS BY VALUE, never by which row happens to be older: the most chases, the
    latest clock, the hand-off question asked if either row asked it. A chase is a nudge that was
    actually delivered to a human, so no fold may un-send one — copying an older twin's blank chase
    state over the keeper's spent two is what let Sotto nudge somebody a third and a fourth time,
    and adopting the older twin's staler `chase_after` re-armed the clock the day after a chase."""
    keeper_created = _s(keeper.get("created_at")) or "9999"
    loser_created = _s(loser.get("created_at")) or "9999"
    if loser_created < keeper_created:
        keeper["created_at"] = loser.get("created_at")     # the older row IS the debt's real age
    newer = loser if loser_created >= keeper_created else keeper
    for k in ("summary", "ask", "deadline"):
        if newer is not keeper and newer.get(k):
            keeper[k] = newer[k]
    # Chases are spent nudges: whichever row carries more of them carries the truth about how often
    # this person has already been asked. Never fewer than either row believed.
    keeper["chased_count"] = max(_int(keeper.get("chased_count")), _int(loser.get("chased_count")))
    if not keeper["chased_count"]:
        keeper.pop("chased_count", None)
    for k in _CHASE_DATE_FIELDS:
        latest = _latest(keeper.get(k), loser.get(k))
        if latest:
            keeper[k] = latest
        else:
            keeper.pop(k, None)
    # Only accepted-delivery provenance is trustworthy. Legacy `times_surfaced` counted model
    # proposals and remains inert for compatibility; exact receipt hashes make the merge replay-safe.
    surfaces = [value for value in (keeper.get("delivery_surface"), loser.get("delivery_surface"))
                if isinstance(value, dict) and value.get("schema") == 1]
    if surfaces:
        keys = sorted({key for value in surfaces for key in value.get("delivery_keys", [])
                       if isinstance(key, str)})
        firsts = [_s(value.get("first_at")) for value in surfaces if _s(value.get("first_at"))]
        lasts = [_s(value.get("last_at")) for value in surfaces if _s(value.get("last_at"))]
        keeper["delivery_surface"] = {"schema": 1,
                                      "delivery_keys": keys,
                                      "first_at": min(firsts) if firsts else "",
                                      "last_at": max(lasts) if lasts else ""}
    _terminate(loser, "dismissed", "merged_duplicate", today)
    loser["merged_into"] = _s(keeper.get("anchor_key"))
    _persist(keeper)
    _persist(loser)


def _retire_anchorless(rows: list, today: str) -> list:
    """Close every live row that identifies NOBODY — no group, no person, no identifier, no name.

    The entry gate stops new ones (`not_a_debt`); this is the same sentence applied to the rows
    already on the volume, and it has to run before anything folds. Such a row cannot be resolved
    (every cross-channel check needs an identifier), cannot be chased, cannot be tapped — and worse,
    every one of them collapses onto the single anchor ending `name:`, so the next pass would fold
    two unrelated asks into one debt. Closed as `expired`/`anchorless`, which renders as nothing,
    exactly like the `unreachable` exit a chased-out waiting_on takes."""
    retired = []
    for it in rows:
        if _s(it.get("status", "open")) in TERMINAL:
            continue
        if not compute_anchor_key(_row_identity(it)).endswith(_ANCHORS_NOBODY):
            continue
        _terminate(it, "expired", "anchorless", today)
        _persist(it)
        retired.append(it)
    return retired


def _terminal_twin(items: dict, canon: dict, old_key: str) -> dict | None:
    """Did this same debt already close under an EARLIER anchor shape? A terminal row is never
    re-anchored (a loop the user closed stays closed and keeps its key), so when the key shape
    changes the closed twin keeps the old spelling — and a live row migrating onto the new spelling
    would quietly slip past the never-resurrect guard. Ask under every shape this file has minted."""
    for k in _prior_anchor_keys(canon):
        if k == old_key:
            continue
        row = items.get(k)
        if row is not None and _s(row.get("status", "open")) in TERMINAL:
            return row
    return None


def _retire_into_terminal(items: dict, old_key: str, row: dict, terminal: dict, today: str) -> None:
    """A live legacy spelling of an already-closed debt is bookkeeping, not a resurrection."""
    _terminate(row, "dismissed", "merged_duplicate", today)
    row["merged_into"] = _s(terminal.get("anchor_key"))
    _persist(row)
    items.pop(old_key, None)


def _migrate_identity(items: dict, group_index: dict, today: str,
                      person_index: dict | None = None) -> list:
    """Idempotently migrate legacy identity shapes before matching today's actions.

    A live row adopts the group's platform id or the person's graph identity. Rows fold only when
    Legacy resolver-minted aliases fold onto the stable counterpart-and-direction base regardless
    of prose. Explicit distinct obligations already carry custom, task or origin keys and remain
    separate because those keys are outside `_prior_anchor_keys`.

    Two rows are never touched: a TERMINAL one (a loop the user closed stays closed), and one whose
    stored anchor this file did not mint (`_prior_anchor_keys`) — a content-hashed commitment anchor
    means something the migration cannot see, so it is left exactly as it is."""
    folded = []
    for old_key in list(items):
        it = items.get(old_key)
        if it is None or it.get("status", "open") in TERMINAL:
            continue
        identity = _row_identity(it)
        if (_s(it.get("anchor_key")) or old_key) not in _prior_anchor_keys(identity):
            continue                      # not an anchor this file composed — not ours to move
        canon = canonicalize_counterpart(identity, group_index, person_index)
        new_key = _obligation_key(canon, {k: v for k, v in items.items() if k != old_key})
        adopted = {k: canon[k] for k in ("group_id", "canonical_id", "contact_name")
                   if _s(canon.get(k)).strip() and _s(canon.get(k)) != _s(it.get(k))}
        if new_key == old_key and not adopted:
            continue                      # already anchored on the truth — the idempotent case
        twin = items.get(new_key) if new_key != old_key else None
        if twin is not None and twin.get("status", "open") in TERMINAL:
            _retire_into_terminal(items, old_key, it, twin, today)
            folded.append(it)
            continue                      # the same debt already closed — never resurrect it
        terminal_twin = _terminal_twin(items, canon, old_key) if twin is None else None
        if terminal_twin is not None:
            _retire_into_terminal(items, old_key, it, terminal_twin, today)
            folded.append(it)
            continue                      # …closed under an EARLIER anchor shape; still closed
        it.update(adopted)
        if new_key == old_key:
            _persist(it)
        elif twin is None:
            items.pop(old_key, None)
            it["anchor_key"] = new_key     # the file keeps its path; the key it answers to changes
            items[new_key] = it
            _persist(it)
        else:
            _fold_duplicate(twin, it, today)   # the row already holding the key survives
            items.pop(old_key, None)
            folded.append(it)
    return folded


def _resolve_unlocked(payload: dict, now: datetime | None = None, *,
                      merge: bool = True, resolve_existing: bool = True) -> dict:
    """The full pass by default. `merge=False` (--resolve-only) runs BEFORE the brief composes, so
    the brief reasons about a ledger resolved as of this morning instead of last night's;
    `resolve_existing=False` (--merge-only) runs after it, ingesting the brief's own actions[]."""
    now = now or _now_local(configured_tz() or "+00:00")   # user-zone "now", not server UTC
    today = payload.get("today") or _to_user_zone(now).strftime("%Y-%m-%d")
    # A full LOCAL timestamp, not a bare date: `created_at` is the cutoff every inbound-delivery
    # check compares against, and "2026-06-23" sorts before every message sent that day — including
    # the ones that arrived hours BEFORE the promise was made.
    created_stamp = f"{_s(today)[:10]} {_to_user_zone(now).strftime('%H:%M:%S')}"
    # The read_local snapshot supplies source messages for proposal validation and fresh-request
    # reopening. The SKILL passes it AS-IS and it may still be the raw MCP tool-result wrapper, so
    # unwrap it as compose_brief does or every evidence check sees {}.
    local_data = unwrap_tool_result(payload.get("local") or {})
    if payload.get("events") and "events" not in local_data:
        local_data = {**local_data, "events": payload["events"]}
    email_data = payload.get("emails")
    if isinstance(email_data, dict):
        email_data = email_data.get("emails") or email_data.get("items") or []
    if isinstance(email_data, list) and "emails" not in local_data:
        local_data = {**local_data, "emails": email_data}
    items, shadowed = _load_items(with_shadowed=True)
    from delivery_effects import loop_version
    # Group identity, straight from the snapshot the model was shown (see canonicalize_counterpart).
    group_index = group_identity(local_data)
    # Person identity, straight from the knowledge graph — built once, read by every anchor below.
    person_index = person_identity()

    resolved, expired, active, parked = [], [], [], []
    # Cutoffs derive from the brief's `today` (the deterministic reference the payload carries),
    # NOT the wall clock — so an offline replay / fixture with a fixed `today` resolves identically
    # regardless of when it runs.
    try:
        ref = datetime.strptime(_s(today)[:10], "%Y-%m-%d")
    except ValueError:
        ref = _to_user_zone(now).replace(tzinfo=None)
    retention_cutoff = (ref - timedelta(days=TERMINAL_RETENTION_DAYS)).strftime("%Y-%m-%d")
    deadline_cutoff = (ref - timedelta(days=DEADLINE_GRACE_DAYS)).strftime("%Y-%m-%d")  # continuity.rs:975

    # 0) identity first: fold the files no dict could hold, then re-anchor every live row onto its
    #    counterpart's real identity. BEFORE the merge, not after it — today's capture has to be
    #    able to LAND on yesterday's row. Run after it, a row still wearing a legacy anchor was
    #    invisible to the merge, the capture opened a second row, and the fold that followed threw
    #    away the reopen path (a resolved loop came back as a brand-new debt with no history).
    if resolve_existing:
        expired.extend(_retire_anchorless(list(items.values()) + shadowed, today))
        _fold_shadowed(items, shadowed, today)
        _migrate_identity(items, group_index, today, person_index)
    versions = {key: loop_version(row) for key, row in items.items()}
    proposed_updates = payload.get("loop_updates") or payload.get("loopUpdates") or []
    if not isinstance(proposed_updates, list):
        proposed_updates = []
    update_ids = {_s(u.get("loopId")) for u in proposed_updates if isinstance(u, dict)}

    # 1) merge new actions by anchor_key. Capture is not delivery: only an accepted outbox effect
    #    may record that the transport accepted a brief naming the loop. Accept the brief's
    #    camelCase actionItems OR snake_case via _normalize_action.
    merged = []
    rejected = []
    skipped_user_terminal = []
    origin_ordinals = {}
    for raw in (payload.get("new_actions", []) if merge else []):
        a = canonicalize_counterpart(_normalize_action(raw), group_index, person_index)
        # The ledger holds COMMUNICATION DEBTS — replies owed, follow-ups, commitments,
        # waiting-ons. Meeting prep/info actions are calendar shadows: the docket is their
        # surface and the calendar already resolves them by passing. Creating loops for them
        # buried the real asks under a mirror of tomorrow's schedule (owner-reported).
        # Legacy entries on existing volumes still close via the meeting-passed resolver.
        # Keyed on the FAMILY, not on two literal spellings: the extractor also writes `meeting`
        # and `calendar`, and those spellings walked straight past the old check — four rows for
        # one sync on the owner's volume.
        if action_family(a.get("action_type")) == "meeting":
            continue
        # An unanswered INVITE is the same kind of shadow: compose_brief mints an `rsvp` ask for it
        # and the calendar closes it — you answer, or it passes. Never a ledger row.
        if _normalize_action_type(a.get("action_type")) == "rsvp" and normalize_channel(a.get("channel")) == "calendar":
            continue
        why = not_a_debt(a)
        if why:
            rejected.append(f"{_s(a.get('contact_name')) or '(unnamed)'}: {why}")
            continue
        named = _s(a.get("loop_id"))
        named_row = items.get(named) if named else None
        if named and (not named_row or not _same_counterpart(a, named_row)):
            # A hallucinated ID may neither discard a real extraction nor edit another person's
            # row. Record the bad reference, then apply the stable counterpart default.
            rejected.append(f"{_s(a.get('contact_name'))}: unknown or mismatched loopId; used counterpart base")
            a["loop_id"] = None
            named = ""
        if named and named in update_ids:
            # One response cannot redefine an obligation and claim to have fulfilled that revision.
            continue

        origin_key = ""
        if a.get("new_obligation"):
            observed = _observed_action_source(a, local_data, created_stamp)
            if observed is None:
                # Unsupported separation neither forks nor rewrites the existing task. The log is
                # the receipt; the next extraction can retry with evidence from the real snapshot.
                rejected.append(f"{_s(a.get('contact_name'))}: newObligation evidence not observed")
                continue
            else:
                refs, observed_at = observed
                base = compute_anchor_key(a)
                ordinal_group = (base, refs)
                ordinal = origin_ordinals.get(ordinal_group, 0) + 1
                origin_ordinals[ordinal_group] = ordinal
                origin_key = json.dumps([base, refs, ordinal], separators=(",", ":"))
                a["origin_key"] = origin_key
                a["created_at"] = observed_at
        ak = _obligation_key(a, items, origin_key)
        if ak in update_ids:
            continue
        if ak not in items:
            closed = [it for it in items.values() if it.get("status") in TERMINAL
                      and _same_counterpart(a, it)]
            if closed:
                observed = max((_new_request_since_closure(a, it, local_data) for it in closed), default="")
                if not observed:
                    skipped_user_terminal.append(_s(a.get("contact_name")) or "(unnamed)")
                    continue
                a["created_at"] = observed
        if ak in items:
            it = items[ak]
            if it.get("status") in TERMINAL:
                observed = _new_request_since_closure(a, it, local_data)
                if not observed:
                    skipped_user_terminal.append(
                        f"{_s(it.get('contact_name')) or '(unnamed)'} ({_s(it.get('resolution'))})")
                    continue
                a["created_at"] = observed
            merged.append(it)
            if it.get("status") in PARKED:
                # Re-captured: the ask is live again. No new-request evidence is needed — parking
                # was Sotto's silence, not the user's closure.
                unpark(it, today)
            if it.get("status") not in TERMINAL:
                # A live anchor re-captured: the ASK is today's, not the day it was first seen.
                # `action_type` stays put — direction now has its own anchor family, so a genuine
                # change of direction forks a new item instead of silently inverting this one.
                for k in ("summary", "ask", "deadline"):
                    if a.get(k):
                        it[k] = a[k]
            if not a.get("source"):
                # This brief carried the loop (see the new-row branch below) — the evening's
                # accountability block is about what THIS morning flagged, carried or new.
                it["source_brief_at"] = created_stamp
            if it.get("status") in TERMINAL:
                # A NEW action on a TERMINAL anchor = the person came back after the loop closed
                # (e.g. they replied again the day after resolution). Without a re-open the action
                # is absorbed here and step 2 `continue`s on the terminal status — the person
                # vanishes for the whole retention window. Re-open with the fresh ask.
                #
                # User closures reach here only with fresh request evidence (checked above).
                it.pop("resolution", None)
                it.pop("resolved_at", None)
                it.pop("closed_at", None)
                for field in ("source_thread_id", "source_message_id", "source_refs"):
                    it[field] = a.get(field)
                it["status"] = "open"
                it["reopened_at"] = today
                it["created_at"] = a.get("created_at") or created_stamp   # fresh ask — restart the clock
                for k in ledger_io.CHASE_STATE_FIELDS:
                    it.pop(k, None)      # …and a fresh chase clock: last month's chases aren't this ask's
                for k in ("summary", "ask", "channel", "meeting_time", "deadline"):
                    if a.get(k):
                        it[k] = a[k]
        else:
            items[ak] = {
                "anchor_key": ak, "action_type": a.get("action_type"), "channel": a.get("channel"),
                "contact_name": a.get("contact_name"), "contact_identifier": a.get("contact_identifier"),
                "canonical_id": a.get("canonical_id"), "status": "open",
                "created_at": a.get("created_at") or created_stamp,
                "summary": a.get("summary", ""), "ask": a.get("ask"),
                "meeting_time": a.get("meeting_time"), "deadline": a.get("deadline"),
                "source_thread_id": a.get("source_thread_id"),
                "source_message_id": a.get("source_message_id"),
                "group_id": a.get("group_id"),
                "source": a.get("source"), "source_refs": a.get("source_refs"),
                "origin_key": a.get("origin_key"),
                "resolution_mode": a.get("resolution_mode"),
            }
            if not a.get("source"):
                # A brief's own action (a sourced one — a Granola commitment — names its source).
                # Stamped so the evening can ask what happened to what the morning flagged: the
                # Evening Accountability block reads this field and had no writer (Sep 2026).
                items[ak]["source_brief_at"] = created_stamp
            merged.append(items[ak])

    if rejected:
        # This includes hard rejections and safe identity fallbacks, so the heading cannot claim
        # every proposal was dropped. Each detail says which outcome occurred without message text.
        print("[continuity_resolve] action proposal adjustments: " + "; ".join(rejected),
              file=sys.stderr)
    if skipped_user_terminal:
        # Same shape of receipt, for the other refusal: the loops a fresh capture would have
        # re-opened and the user's own closure kept shut.
        print("[continuity_resolve] left closed, the user closed it: "
              + "; ".join(skipped_user_terminal), file=sys.stderr)

    rejected_updates = {}
    accepted_updates = 0
    reply_history = None          # relationship_state.json, read at most once per pass
    proposal_observed_at = datetime.now(timezone.utc).isoformat()

    def reject_update(reason):
        rejected_updates[reason] = rejected_updates.get(reason, 0) + 1
        log_outcome.record_loop_proposal(update, row, "rejected", reason, proposal_observed_at)

    for update in proposed_updates:
        row = None
        if not isinstance(update, dict):
            reject_update("malformed_update")
            continue
        key = _s(update.get("loopId"))
        row = items.get(key)
        proposal_row = dict(row) if row else None
        if not row:
            reject_update("unknown_loop")
            continue
        if row.get("status") in TERMINAL or row.get("resolution_mode") == "explicit":
            reject_update("protected_loop")
            continue
        if not update.get("loopVersion") or update["loopVersion"] != versions.get(key):
            reject_update("stale_revision")
            continue
        proof = _completion_evidence(row, update, local_data)
        if not proof:
            reject_update("unsupported_evidence")
            continue
        if update.get("status") == "resolved":
            _terminate(row, "resolved", "delivered" if is_waiting_on(row.get("action_type")) else "replied", today)
            row["resolution_evidence"] = proof["description"]
            row["resolution_source_refs"] = update["evidence"]
            resolved.append(row)
        elif update.get("status") == "waiting" and is_waiting_on(row.get("action_type")):
            # A specific promise postpones only this obligation. Replayed evidence never buys time.
            if not _message_is_after(proof["timestamp"], _s(row.get("last_heard_at"))):
                reject_update("replayed_promise")
                continue
            promise_ref = _parse_dt(proof["timestamp"])
            if promise_ref is None:
                try:
                    promise_ref = parsedate_to_datetime(_s(proof["timestamp"]))
                except (TypeError, ValueError, OverflowError):
                    promise_ref = ref
            promise_ref = _to_user_zone(promise_ref)
            named = promised_date(proof["text"], promise_ref)
            row["last_heard_at"] = proof["timestamp"]
            if not named and reply_history is None:
                reply_history = _relationship_history()
            chase_after = (named or _learned_chase_after(
                row, promise_ref + timedelta(days=chase_after_days()), now,
                promise_ref, reply_history).strftime("%Y-%m-%d"))
            hard_deadline = _s(row.get("deadline"))[:10]
            # Bound an explicit promised date by a future obligation deadline. The unnamed path's
            # helper already preserves the ordinary floor; a second clamp here would undo it.
            row["chase_after"] = (min(chase_after, hard_deadline)
                                  if named and hard_deadline and hard_deadline > today else chase_after)
            row.pop("chase_pending", None)
        else:
            reject_update("unsupported_status")
            continue
        _persist(row)
        log_outcome.record_loop_proposal(update, proposal_row, "accepted", "evidence_verified",
                                        proposal_observed_at)
        accepted_updates += 1

    if rejected_updates:
        # Counts and reason codes explain a held loop without logging messages or model quotes.
        print("[continuity_resolve] rejected loop updates: " + json.dumps(rejected_updates, sort_keys=True),
              file=sys.stderr)
    update_outcomes = {"accepted": accepted_updates,
                       "rejected": sum(rejected_updates.values()),
                       "rejected_by_reason": rejected_updates,
                       "total": len(proposed_updates)}
    print("[continuity_resolve] loop update outcomes: "
          + json.dumps(update_outcomes, sort_keys=True), file=sys.stderr)

    if not resolve_existing:
        # --merge-only: persist exactly what this pass touched (nothing else is rewritten, so a
        # dashboard/retune write landing in the same window survives) and report what's open.
        for it in merged:
            _persist(it)
        open_now = [it for it in items.values()
                    if it.get("status", "open") not in TERMINAL
                    and it.get("status") not in PARKED
                    and not (_s(it.get("snoozed_until"))[:10] > today)]
        return {"resolved": _strip(resolved), "expired": [], "active": _strip(open_now)}

    # 2) Maintain legacy meeting shadows and snoozes. Ordinary task completion was already handled
    # above through revision-bound, source-bound loopUpdates; no generic contact signal closes it.
    for ak, it in items.items():
        status = it.get("status", "open")
        # _s() everywhere we slice: yaml.safe_load yields datetime.date for unquoted dates and
        # None for explicit nulls — a raw [:10] on those is a TypeError that kills the whole step.
        if status in TERMINAL:
            if (_s(it.get("resolved_at")) or "9999")[:10] < retention_cutoff:   # prune past retention
                _remove(it)
            continue
        created = (_s(it.get("created_at")) or today)[:10]
        explicit = _s(it.get("resolution_mode")).strip() == "explicit"
        # Ordinary obligations close only through evidence-bound proposals above. A generic
        # replied/handled signal, unrelated meeting, age or overdue date cannot pay off a task.
        # f) meeting passed (meeting types only) → resolved, not expired
        is_meeting = _normalize_action_type(it.get("action_type")) in MEETING_TYPES
        if not explicit and is_meeting and (meeting_passed(_s(it.get("meeting_time")), created, today)
                           or (not it.get("meeting_time") and created < deadline_cutoff)):
            _terminate(it, "resolved", "meeting_passed", today); resolved.append(it); _persist(it); continue
        # g) user-snoozed (via sotto-loops): keep the file, but don't surface until the date passes.
        if _s(it.get("snoozed_until"))[:10] > today:
            _persist(it); continue
        # i) parked: kept, out of every view, until a touch above revives it. Something you OWE
        #    that nothing touched for PARK_AFTER_DAYS parks now — unless its deadline is still
        #    ahead (a date is a reason to keep showing it) or the user confirmed it themselves
        #    (`explicit`: only they change its state). What you're owed is chased instead.
        if status in PARKED:
            parked.append(it); _persist(it); continue
        # Composition is not delivery. Old rows and missed evenings remain active until the
        # outbox has accepted their warning on a previous local day, for this exact task/touch.
        if (ledger_io.park_candidate(it, today) and ledger_io.parking_notice_current(it)
                and _s(it.get("parking_notice_at")) < today):
            it["status"], it["parked_at"] = "parked", today
            parked.append(it); _persist(it); continue
        active.append(it); _persist(it)

    # h) the chase clock: yesterday's undelivered proposal expires, then stamp (at most) one
    #    chase-due waiting_on as PENDING for today. proactive_scan delivers it and calls
    #    --finalize-chase; nothing else writes chase state.
    _clear_stale_pending(active, today)
    _stamp_chase(active, today, ref)

    return {"resolved": _strip(resolved), "expired": _strip(expired), "active": _strip(active),
            "parked": _strip(parked)}


def resolve(payload: dict, now: datetime | None = None, *,
            merge: bool = True, resolve_existing: bool = True) -> dict:
    """Serialize the full ledger transaction; readers remain lock-free because each file replace is
    atomic."""
    with _ledger_lock():
        return _resolve_unlocked(payload, now, merge=merge, resolve_existing=resolve_existing)


def _strip(lst: list) -> list:
    return [{k: v for k, v in it.items() if k != "_path"} for it in lst]


def unpark(it: dict, today: str):
    """A parked row comes back to life: open again, and `reopened_at` restarts the park clock (the
    same stamp a terminal row gets when a fresh ask re-opens it — one field for "went live again").
    The one writer for the transition, used by the resolver's re-capture and sotto-loops' `keep`."""
    if it.get("status") in PARKED:
        it["status"] = "open"
        it.pop("parked_at", None)
        it["reopened_at"] = today


def acknowledge_parking_notice(effect: dict, accepted_at) -> bool:
    """The existing outbox commits a warning only after provider acceptance, under our writer lock.

    Retries are idempotent. A capture, correction or keep since composition makes an old warning
    inapplicable; acknowledgement then succeeds without changing the newer obligation.
    """
    from delivery_effects import instant, loop_version
    accepted = instant(accepted_at)
    if accepted is None:
        return False
    day = _to_user_zone(datetime.fromtimestamp(accepted, timezone.utc)).date()
    with _ledger_lock():
        it = _load_items().get(_s(effect.get("anchor_key")))
        if (not it or loop_version(it) != effect.get("loop_version")
                or ledger_io.last_touch_day(it) != effect.get("touch")
                or not ledger_io.park_candidate(it, (day + timedelta(days=1)).isoformat())):
            return True
        if ledger_io.parking_notice_current(it):
            return True
        it.update(parking_notice_at=day.isoformat(), parking_notice_touch=effect["touch"],
                  parking_notice_version=effect["loop_version"])
        _persist(it)
    return True


def acknowledge_loop_surfaced(effect: dict, receipt: dict) -> bool:
    """Count a named loop only after provider acceptance, once per provider receipt.

    Legacy `times_surfaced` was incremented by model capture and is intentionally ignored. The
    versioned object below is the explicit provenance that distinguishes delivered visibility from
    old proposal activity; the outbox run identity is hashed so replay detection stores no provider
    addressing data in the ledger. Exact keys preserve idempotence across arbitrary retry volume;
    canonical loops are finite and each accepted brief adds only one short hash.
    """
    from delivery_effects import instant, loop_identity, loop_version
    accepted = instant(receipt.get("accepted_at"))
    delivery_id = _s(effect.get("delivery_id")).strip()
    if accepted is None or not delivery_id:
        return False
    delivery_key = hashlib.sha256(delivery_id.encode()).hexdigest()[:24]
    accepted_at = datetime.fromtimestamp(accepted, timezone.utc).isoformat()
    with _ledger_lock():
        it = _load_items().get(_s(effect.get("anchor_key")))
        # The same test staging applied: the obligation's identity, not its wording. A row that was
        # restated between staging and acceptance is the same debt the user read about; a row that
        # closed, or became a different debt, is not counted. Effects staged before identity
        # existed still bind their exact version.
        same = (loop_identity(it) == effect.get("loop_identity") if it and effect.get("loop_identity")
                else bool(it) and loop_version(it) == effect.get("loop_version"))
        if not it or it.get("status", "open") not in ACTIVE or not same:
            return True  # changed after validation: delivered text cannot mutate the newer row
        provenance = it.get("delivery_surface")
        if not isinstance(provenance, dict) or provenance.get("schema") != 1:
            provenance = {"schema": 1, "delivery_keys": []}
        keys = [key for key in provenance.get("delivery_keys", []) if isinstance(key, str)]
        if delivery_key in keys:
            return True
        keys.append(delivery_key)
        prior_first = _s(provenance.get("first_at"))
        first = min(prior_first, accepted_at) if prior_first else accepted_at
        last = max(_s(provenance.get("last_at")), accepted_at)
        it["delivery_surface"] = {"schema": 1,
                                  "first_at": first, "last_at": last,
                                  "delivery_keys": keys}
        _persist(it)
    return True


def _terminate(it: dict, status: str, resolution: str, today: str):
    it["status"], it["resolution"], it["resolved_at"] = status, resolution, today
    if resolution.startswith("user_"):
        instant = _to_user_zone(datetime.now(timezone.utc))
        it["closed_at"] = instant.isoformat() if str(instant.date()) == today else _inbound_cutoff(today)


def _persist(it: dict):
    os.makedirs(_dir(), exist_ok=True)
    path = it.get("_path") or os.path.join(_dir(), f"{_safe(it['anchor_key'])}.md")
    previous = None
    try:
        with open(path, encoding="utf-8") as handle:
            previous = ledger_io.parse_frontmatter(handle.read())
    except FileNotFoundError:
        pass
    # Save the transition with the row, then project it through the existing outcome writer.
    # A retry can re-append the same identity safely; no second store or scheduling loop.
    event = log_outcome.loop_transition(previous, it)
    if event:
        it["proof_transition"] = event
    fm = {k: v for k, v in it.items() if k != "_path"}
    body = f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)}---\n"
    fd, tmp = tempfile.mkstemp(prefix=".loop-", suffix=".tmp", dir=_dir())
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        log_outcome.record_loop_event(event)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _remove(it: dict):
    p = it.get("_path")
    if p and os.path.exists(p):
        os.remove(p)


def _safe(s: str) -> str:
    # Non-alnum → '-' (no traversal) + a short hash of the full key so distinct anchor_keys
    # that normalize to the same chars (e.g. "thread:A/B" vs "thread:A-B") don't collide.
    import hashlib
    slug = "".join(c if c.isalnum() else "-" for c in s)[:72]
    return f"{slug}-{hashlib.sha256(s.encode()).hexdigest()[:8]}"


def main():
    argv = sys.argv[1:]
    for flag, fn in (("--finalize-chase", finalize_chase), ("--finalize-handoff", finalize_handoff)):
        if flag in argv:
            i = argv.index(flag)
            print(json.dumps(fn(argv[i + 1] if len(argv) > i + 1 else ""), default=_s))
            return
    merge = "--resolve-only" not in argv
    resolve_existing = "--merge-only" not in argv
    files = [a for a in argv if not a.startswith("--")]
    raw = open(files[0]).read() if files else sys.stdin.read()
    result = resolve(json.loads(raw), merge=merge, resolve_existing=resolve_existing)
    try:  # visibility into the continuity loop (served at /debug/brief-log)
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "lib"))
        from sotto_log import diag
        diag(f"[continuity_resolve] {len(result.get('active', []))} open loops, "
             f"{len(result.get('resolved', []))} resolved, {len(result.get('expired', []))} expired, "
             f"{len(result.get('parked', []))} parked")
    except Exception:
        pass
    # default=_s: items loaded from frontmatter can carry datetime.date values (unquoted YAML
    # dates) — they must serialize as ISO strings, not kill the step at the very last print.
    print(json.dumps(result, default=_s))


if __name__ == "__main__":
    main()
