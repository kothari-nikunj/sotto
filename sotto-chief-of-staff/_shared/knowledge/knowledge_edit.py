#!/usr/bin/env python3
"""
knowledge_edit.py — user-initiated edits from The Window dashboard (M2).

THE core principle (docs/archive/plans/web-dashboard-the-window.md): a dashboard edit is indistinguishable
from texting the correction — one code path. `correct` and `add` build the exact extraction payload
a chat correction produces and run it through knowledge_update.apply(), so SUPERSEDE semantics,
archived_text, dedup, migration — everything — is identical to chat. The only additions are the
user-edit guarantees apply()'s similarity dedup can't promise on its own:

  * correct: after apply(), if the TARGETED fact is still active (the correction text shared too
    few words for find_similar_fact to link them, ratio < 0.3) it is archived directly — the user
    pointed at that fact and said "this is wrong"; it must not survive. And if the correction text
    itself never landed (an archived near-twin made the dedup SKIP it), it is inserted directly
    with the same conf-1.0/user_edit provenance apply() would have written.
  * archive: sets status: archived (+ archived_text, mirroring SUPERSEDE) on the one fact and
    rewrites through the knowledge lib's serializer. Never deletes; every other field is untouched
    (updated_at included — archiving is bookkeeping, not new knowledge).

Fact ops are PEOPLE-ONLY: company files carry About/News/Context sections, not a facts map (the
on-disk shape stays byte-compatible with knowledge_files.rs), so there is no fact id to point at.
A company's correction lane is `company-about`: the About paragraph is the company's identity — what
it builds, who founded it, the market — and correcting it means REPLACING it, not superseding one
line of it. It runs through the same knowledge_update.apply() company lane research writes through,
and stamps `updated_by: user_edit`, which research then refuses to overwrite: a correction you made
stays made, exactly as a deleted preference stays deleted.

`loop` writes the SAME terminal transition fields the deterministic resolver
(morning-brief/scripts/continuity_resolve.py `_terminate` + `_persist`) writes: status,
resolution ("user_resolved" | "user_dismissed"), resolved_at (user-local date), persisting the
full frontmatter back with yaml.safe_dump(sort_keys=False) — resolver-shaped bytes.

`loop-add` and `loop-deadline` are the other two halves of owning a loop by hand. Both go through
continuity_resolve's OWN machinery — `_normalize_action` → `compute_anchor_key` → `_persist`, the
exact three calls apply_commitments.py makes when the followup writes a commitment into the ledger —
so a hand-added loop is byte-shaped like every other ledger entry and the brief's resolution sweep,
loops_query and the dashboard read it natively. A hand-added loop carries `channel: manual` and
`source: user_added`. A `you` item has no source thread to auto-close it; a `them` item with a known
identifier may still close when that person delivers, because direction—not source—owns resolution.
Re-adding the same ask on the same day dedupes onto the same anchor rather than making a second
file. Capture never counts as surfacing; only an accepted delivery receipt does. `loop-deadline`
writes the ONE field, which is also how a loop is
snoozed: the deterministic resolver expires a loop 2 days past its deadline, so moving the deadline
forward is the only "later" the ledger has — there is no second snooze state to invent.

`merge` is the confirmation half of entity-dedup lite (knowledge_update.py's module docstring has
the whole design): the Learn step auto-merges only on an EXACT shared identifier and merely
*suggests* name-similarity merges — this op is how a human (chat or a dashboard card) confirms one.
It runs through knowledge_update.merge_person_files, i.e. the same kg.merge_person + os.remove
mechanics migrate_people_dir uses, and refuses outright when the two files carry conflicting
identifiers (two John Smiths with two different emails are two people, whatever their names say).

`relation-add` / `relation-remove` are how a wrong EDGE is corrected — "Vishnu didn't introduce us".
Both write through knowledge_update's link_relation / unlink_relation, the one writer that keeps a
relation on BOTH people's files, so a removal from chat and a removal from the dashboard's ✕ leave
the graph in the same shape. The vocabulary is closed (kg.RELATION_INVERSE): an unknown --type is
refused here rather than half-written.

CLI (runs in the skills tree, where PyYAML + the libs live; invoked by the receiver's dashboard):
  knowledge_edit.py --slug <slug> --op correct --fact-id <id> --text "..."
  knowledge_edit.py --slug <slug> --op archive --fact-id <id>
  knowledge_edit.py --slug <slug> --op add --text "..." [--memory-type context]
  knowledge_edit.py --slug <slug> --op add-identifier --identifier "+1 (310) 924-5269"
  knowledge_edit.py --slug <slug> --op company-about --text "..."   (knowledge/companies/<slug>.md)
  knowledge_edit.py --op loop --anchor <anchor_key> --to resolved|dismissed
  knowledge_edit.py --op loop-add --text "..." [--contact "Name"] [--identifier a@b.com]
                                  [--direction you|them] [--deadline YYYY-MM-DD]
  knowledge_edit.py --op loop-deadline --anchor <anchor_key> --deadline YYYY-MM-DD|""
  knowledge_edit.py --op merge --from <slug> --into <slug>
  knowledge_edit.py --op merge-dismiss --from <slug> --into <slug>
  knowledge_edit.py --slug <slug> --op relation-add --type introduced_by --other-slug <slug>
                                  [--date YYYY-MM-DD]
  knowledge_edit.py --slug <slug> --op relation-remove --other-slug <slug> [--type <type>]

`add-identifier` is the one identifier a HUMAN supplies. Every other identifier in the graph arrived
from Apple Contacts or an email header, so a person who only ever texts from an unsaved number has
none — and reads as "+1 (310) 924-5269" in every brief. Naming it once attaches it to their file and
the shared identity resolver names them everywhere at once: their 1:1 thread, the label of any group
they are in, and each line they send in it. It refuses an identifier that already belongs to someone
else rather than moving it, because two files sharing an identifier is precisely what
`auto_merge_by_identifier` reads as proof they are one human — a careless add would merge two
strangers on the next brief.

Output: one JSON object on stdout — {"ok": true, "slug", "about"} for company-about;
{"ok": true, "slug", "identifier", "added", "identifiers"} for add-identifier;
{"ok": true, "slug", "fact_count"} for fact ops (merge adds
"merged_from"), {"ok": true, "anchor_key", "status"} for loop, {"ok": true, "anchor_key",
"created"} for loop-add, {"ok": true, "anchor_key", "deadline"} for loop-deadline, {"ok": true,
"dismissed"} for merge-dismiss, {"ok": true, "slug", "relations": N} for the relation ops — or
{"ok": false, "error"} + exit 2 on failure.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "lib"))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))   # ledger_io
sys.path.insert(0, _HERE)                                   # knowledge.py, its sibling

from cleanup_selection import all_open_selection, complete_open_selection, explicit_displayed_reference, short_numbered_completion  # noqa: E402
import knowledge as kg  # noqa: E402
import knowledge_correction as correction  # noqa: E402
from ledger_provenance import UnknownProvenanceError, classify_ledger_sources, digest_frontmatter  # noqa: E402

DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
SLUG_RE = re.compile(r"\A[a-z0-9_-]{1,128}\Z")
FACT_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
MEMORY_TYPE_RE = re.compile(r"\A[a-z_]{1,32}\Z")
# Anchor keys carry colons and, in the name: form, spaces/@/dots — allow any printable char,
# reject control chars. Anchors are only ever COMPARED against ledger frontmatter, never joined
# into a filesystem path.
ANCHOR_RE = re.compile(r"\A[^\x00-\x1f\x7f]{1,256}\Z")
TEXT_MAX = 500
SOURCE = correction.OWNER_SOURCE


class EditError(Exception):
    """A user-reportable failure — message goes out as {"ok": false, "error": ...}."""


class OwnerLoopPreEffectRefusalError(EditError):
    """An owner-loop conflict established under the canonical lock before any write."""

    def __init__(self, code: str):
        if code not in ("owner_event_conflict", "owner_anchor_collision"):
            raise ValueError("invalid owner-loop refusal")
        self.code = code
        super().__init__("owner event conflicts with its recorded text"
                         if code == "owner_event_conflict"
                         else "loop anchor already belongs to another event")


class OwnerLoopClosePreEffectRefusalError(EditError):
    """A deterministic refusal established before any close write."""

    def __init__(self, code: str):
        if code not in ("owner_close_target_unavailable", "owner_close_target_changed",
                        "owner_close_event_conflict"):
            raise ValueError("invalid owner close refusal")
        self.code = code
        super().__init__(code.replace("_", " "))


def _load_knowledge_update():
    """Import knowledge_update, this file's sibling (path-loaded: the skills tree is not a
    package). Kept lazy so `--op loop` never pays for it."""
    import importlib.util
    path = os.path.join(_HERE, "knowledge_update.py")
    spec = importlib.util.spec_from_file_location("knowledge_update", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_person(slug: str):
    """(path, PersonFile) for knowledge/people/<slug>.md. People only — see module docstring."""
    if not SLUG_RE.match(slug or ""):
        raise EditError("invalid slug")
    path = os.path.join(kg.people_dir(), f"{slug}.md")
    if not os.path.isfile(path):
        raise EditError(f"person not found: {slug}")
    with open(path, encoding="utf-8") as f:
        return path, kg.parse_person_file(f.read())


def _resolve_after_apply(p: "kg.PersonFile"):
    """The person's file after apply() ran (migration may have re-keyed a legacy name-slug file to
    {canonical_id}.md). Returns (path, PersonFile)."""
    cid = p.canonical_id if kg.valid_canonical_id(p.canonical_id) else ""
    ident = next((str(i) for i in p.identifiers if str(i).strip()), "")
    path = kg.find_person_file(name=p.name, identifier=ident, cid=cid)
    if not path or not os.path.exists(path):
        raise EditError("person file vanished after apply")
    with open(path, encoding="utf-8") as f:
        return path, kg.parse_person_file(f.read())


def _result(path: str, p: "kg.PersonFile") -> dict:
    active = sum(1 for f in p.facts.values() if f.status != "archived")
    return {"ok": True, "slug": os.path.splitext(os.path.basename(path))[0], "fact_count": active}


def _validated_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        raise EditError("empty text")
    if len(t) > TEXT_MAX:
        raise EditError(f"text too long (max {TEXT_MAX} chars)")
    return t


def _chat_payload(p: "kg.PersonFile", fact_text: str, change_type: str, memory_type: str) -> dict:
    """The exact extraction shape a chat correction produces — apply() sees no difference."""
    return {"person_updates": [{
        "person_name": p.name,
        "identifier": next((str(i) for i in p.identifiers if str(i).strip()), ""),
        "canonical_id": p.canonical_id if kg.valid_canonical_id(p.canonical_id) else "",
        "updated_by": SOURCE,   # the file-level stamp tells the truth, like the company lane
        "facts": [{"fact": fact_text, "change_type": change_type, "confidence": 1.0,
                   "source": SOURCE, "memory_type": memory_type}],
    }]}


def op_correct(slug: str, fact_id: str, text: str, now: datetime | None = None,
               *, title: str | None = None, company: str | None = None) -> dict:
    text = _validated_text(text)
    identity = {field: value for field, value in (('title', title), ('company', company)) if value is not None}
    if not correction.valid_identity(identity):
        raise EditError('invalid identity value')
    if not FACT_ID_RE.match(fact_id or ""):
        raise EditError("invalid fact id")
    path, p = _read_person(slug)
    target = p.facts.get(fact_id)
    if target is None:
        raise EditError(f"fact not found: {fact_id}")

    ku = _load_knowledge_update()
    ku.apply(_chat_payload(p, text, "correction", target.type), now)

    # User-edit guarantees on top of the chat path (see module docstring): the targeted fact must
    # not survive, and the correction text must exist as an active fact.
    path2, p2 = _resolve_after_apply(p)
    today = kg.today_str(now)
    changed = correction.correct_fact_state(p, p2, {'fact_id':fact_id,'text':text,**identity}, today, SOURCE)
    if changed:
        kg.write_person_file(path2, p2, now)
    return _result(path2, p2)


def op_archive(slug: str, fact_id: str, now: datetime | None = None) -> dict:
    if not FACT_ID_RE.match(fact_id or ""):
        raise EditError("invalid fact id")
    path, p = _read_person(slug)
    fact = p.facts.get(fact_id)
    if fact is None:
        raise EditError(f"fact not found: {fact_id}")
    if fact.status != "archived" or fact.archived_reason != "user_archived":
        fact.status = "archived"
        fact.archived_text = fact.text  # mirror SUPERSEDE's history-keeping
        fact.archived_reason = "user_archived"
        kg.write_person_file(path, p, now)
    return _result(path, p)


def _similar_fact_exists(p: "kg.PersonFile", text: str, memory_type: str) -> bool:
    """Would the canonical writer fold this text into an existing fact (bump/skip) instead of adding
    it? A merely related fact (the supersede band) is kept, and the exact text is added beside it."""
    return any(kg.find_similar_fact({fid: fact}, text, memory_type, False)[0] in (kg.BUMP, kg.SKIP)
               for fid, fact in p.facts.items())


def op_add(slug: str, text: str, memory_type: str = "context",
           now: datetime | None = None, *, maintain_identities: bool = True,
           exact_new: bool = False) -> dict:
    """`exact_new` (explicit owner teaching): add exactly this fact or refuse, never touching another
    fact. The writer's word-overlap dedup would otherwise bump an existing fact — even one the owner
    is contradicting — re-stamp it as owner-confirmed, or archive a related one."""
    text = _validated_text(text)
    memory_type = (memory_type or "context").strip()
    if not MEMORY_TYPE_RE.match(memory_type):
        raise EditError("invalid memory type")
    ku = _load_knowledge_update()
    # The refusal check and the write share the graph lock, so no similar fact can land in between.
    with (ku.graph_lock() if exact_new else contextlib.nullcontext()):
        _, p = _read_person(slug)
        if exact_new and _similar_fact_exists(p, text, memory_type):
            raise EditError("similar fact exists")
        payload = _chat_payload(p, text, "new", memory_type)
        if exact_new:
            payload['person_updates'][0]['facts'][0]['exact_new'] = True
        if not maintain_identities:
            # Existing canonical identity is sufficient; do not normalize/append an identifier.
            payload['person_updates'][0]['identifier'] = ''
        ku.apply(payload, now, maintain_identities=maintain_identities)
        return _result(*_resolve_after_apply(p))


MAX_IDENTIFIER_CHARS = 200


def _validated_identifier(identifier: str) -> str:
    """One email address or one phone number, and nothing else. Rejected up front rather than
    stored and puzzled over later: a name is not an identifier, and neither is a sentence."""
    ident = (identifier or "").strip()
    if not ident or len(ident) > MAX_IDENTIFIER_CHARS:
        raise EditError("identifier must be a non-empty email address or phone number")
    if ident.lower().endswith("@lid"):
        raise EditError("a WhatsApp @lid JID is a rotating privacy id, not an identifier — "
                        "use the person's phone number or email")
    if "@" in ident:
        ident = ident.lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", ident):
            raise EditError(f"not a valid email address: {ident}")
        return ident
    if not re.fullmatch(r"[\d\s\-+()./]+", ident) or len(re.sub(r"\D", "", ident)) < 7:
        raise EditError(f"not an email address or a phone number: {identifier.strip()[:80]}")
    return ident


def op_add_identifier(slug: str, identifier: str, now: datetime | None = None) -> dict:
    """Teach the graph that a phone number or address belongs to someone it already knows.

    THE GAP THIS CLOSES: a text from a number in neither Apple Contacts nor the graph shows up in
    briefs as "+1 (310) 924-5269", and there was no way to say who that is. Every other identifier
    Sotto holds arrived from Contacts or from an email header — this is the one a human supplies.
    Once added, the shared identity resolver names them everywhere at once: their 1:1 thread, the
    label of any group they're in, and the sender line of each message they send in it.

    Two refusals, and the second one is the important one:
      * the string must be an email address or a phone number (not a name, not a note);
      * **an identifier that already belongs to somebody else is refused, never moved.** Two files
        sharing one identifier is the exact state `auto_merge_by_identifier` treats as proof that
        two files are one human — so a careless add here would silently MERGE two real strangers on
        the next brief and take one of their histories with it. If the identifier is genuinely on
        the wrong file, that is a merge (`--op merge`) or a correction over there, and it is a
        decision a human makes with both names in front of them.

    Idempotent: adding an identifier the person already carries reports `added: false` and writes
    nothing. Rides `knowledge_update.apply()` like every other edit — one writer, so a number named
    from chat and one named from the dashboard leave byte-identical files."""
    path, p = _read_person(slug)
    ident = _validated_identifier(identifier)
    key = kg.normalize_identifier(ident)
    if not key:
        raise EditError(f"identifier normalizes to nothing: {ident}")

    if any(kg.normalize_identifier(str(i)) == key for i in p.identifiers):
        return {"ok": True, "slug": slug, "identifier": ident, "added": False,
                "identifiers": [str(i) for i in p.identifiers]}

    owner = kg.find_person_file(identifier=ident)
    if owner and os.path.abspath(owner) != os.path.abspath(path):
        other = os.path.splitext(os.path.basename(owner))[0]
        raise EditError(
            f"{ident} already belongs to {other} — an identifier names one human, so this is not an "
            f"add. If {other} and {slug} are the same person, merge them (--op merge --from "
            f"{other} --into {slug}); if the identifier is wrong on {other}, fix it there.")

    ku = _load_knowledge_update()
    # `canonical_id` is what makes this resolve to THIS file: apply() keys cid → identifier → name,
    # and the identifier we're adding is by definition not on any file yet.
    ku.apply({"person_updates": [{
        "person_name": p.name,
        "identifier": ident,
        "canonical_id": p.canonical_id if kg.valid_canonical_id(p.canonical_id) else "",
        "facts": [],
    }]}, now)

    path2, p2 = _resolve_after_apply(p)
    if not any(kg.normalize_identifier(str(i)) == key for i in p2.identifiers):
        raise EditError(f"identifier was not persisted onto {slug} — nothing changed")
    return {"ok": True, "slug": os.path.splitext(os.path.basename(path2))[0],
            "identifier": ident, "added": True,
            "identifiers": [str(i) for i in p2.identifiers]}


def op_company_about(slug: str, text: str, now: datetime | None = None) -> dict:
    """Correct a COMPANY record: replace knowledge/companies/<slug>.md's `## About` paragraph.

    The company equivalent of `correct` on a person, and the same principle — it rides
    knowledge_update.apply()'s `company_updates` lane, i.e. the exact path research writes through,
    so a dashboard edit, a texted correction and a research write leave byte-identical files. The
    `user_edit` stamp is the only difference, and it is what makes the correction stick: persist_prep
    declines to overwrite an About a human wrote."""
    text = _validated_text(text)
    if not SLUG_RE.match(slug or ""):
        raise EditError("invalid slug")
    if not os.path.isfile(os.path.join(kg.companies_dir(), f"{slug}.md")):
        raise EditError(f"company not found: {slug}")
    ku = _load_knowledge_update()
    ku.apply({"company_updates": [{"company_slug": slug, "about": text,
                                   "updated_by": SOURCE}]}, now)
    about = ku.company_knowledge(company_slug=slug).get("about") or ""
    return {"ok": True, "slug": slug, "about": about}


def op_merge(from_slug: str, into_slug: str, now: datetime | None = None) -> dict:
    """Confirm a name-similarity merge suggestion (Editor Step 2 item 6): union `from` INTO `into`
    and delete `from`. This is the ONLY path by which a name-based merge ever happens — the Learn
    step suggests, a human (chat or dashboard card) confirms, and the write runs through
    knowledge_update.merge_person_files, i.e. the same tested kg.merge_person + os.remove the
    migration uses. Refusals, in order: either file missing, a self-merge, or CONFLICTING
    identifiers — two files each carrying a different email (or a different phone) are two people,
    and no amount of name similarity outranks that. The suggestion is forgotten afterward."""
    src_path, src = _read_person(from_slug)
    dst_path, dst = _read_person(into_slug)
    if os.path.realpath(src_path) == os.path.realpath(dst_path):
        raise EditError("cannot merge a person into themselves")
    ku = _load_knowledge_update()
    if ku.identifiers_conflict(dst, src):
        raise EditError(
            f"identifiers conflict: {src.name or from_slug} and {dst.name or into_slug} carry "
            "different email/phone identifiers — they are different people, not a duplicate")
    ku.merge_person_files(dst_path, src_path, now)
    try:                                  # the suggestion has been acted on; stop offering it
        ku.drop_merge_suggestion(from_slug, into_slug, now)
    except Exception:  # noqa: BLE001
        pass
    with open(dst_path, encoding="utf-8") as f:
        merged = kg.parse_person_file(f.read())
    out = _result(dst_path, merged)
    out["merged_from"] = from_slug
    return out


def op_merge_dismiss(from_slug: str, into_slug: str, now: datetime | None = None) -> dict:
    """Forget a merge suggestion without merging ("these really are two people"). The write is
    knowledge_update.dismiss_merge_suggestion, which drops it AND tombstones the pair — a confirmed
    merge needs no tombstone (a file disappears), but a dismissal does, because the suggestions are
    recomputed from the people on disk on every apply and would otherwise re-ask tomorrow."""
    if not SLUG_RE.match(from_slug or "") or not SLUG_RE.match(into_slug or ""):
        raise EditError("invalid slug")
    ku = _load_knowledge_update()
    ku.dismiss_merge_suggestion(from_slug, into_slug, now)
    return {"ok": True, "dismissed": [from_slug, into_slug]}


def op_relation(slug: str, other_slug: str, rel_type: str, add: bool, date: str = "",
                now: datetime | None = None) -> dict:
    """Add or remove ONE typed edge between two people, through knowledge_update's link/unlink —
    the same writer the Learn step uses, so both ends always agree. Refusals, in order: a slug with
    no file (the edge would dangle), a self-edge, and a --type outside the closed vocabulary.
    Removing accepts an empty --type, meaning "whatever edge these two have"."""
    path, p = _read_person(slug)
    other_path, other = _read_person(other_slug)
    if os.path.realpath(path) == os.path.realpath(other_path):
        raise EditError("a relation needs two different people")
    rel_type = (rel_type or "").strip().lower()
    ku = _load_knowledge_update()
    if add and rel_type not in kg.RELATION_INVERSE:
        raise EditError("unknown relation type: " + (rel_type or "(blank)") + " — known types: "
                        + ", ".join(sorted(kg.RELATION_INVERSE)))
    if not add and rel_type and rel_type not in kg.RELATION_INVERSE:
        raise EditError("unknown relation type: " + rel_type)
    a_slug = os.path.splitext(os.path.basename(path))[0]
    b_slug = os.path.splitext(os.path.basename(other_path))[0]
    if add:
        changed = ku.link_relation(a_slug, b_slug, rel_type, name_a=p.name, name_b=other.name,
                                   date=_validated_date(date), source=SOURCE, confidence=1.0,
                                   now=now)
        n = 1 if changed else 0
    else:
        n = ku.unlink_relation(a_slug, b_slug, rel_type, now)
    with open(path, encoding="utf-8") as f:
        after = kg.parse_person_file(f.read())
    # `changed: 0` on an add means the edge was already there — idempotent, not a failure.
    return {"ok": True, "slug": a_slug, "relations": len(after.relations), "changed": n}


def _validated_date(date: str) -> str:
    d = (date or "").strip()
    if d and not DATE_RE.match(d):
        raise EditError("date must be YYYY-MM-DD")
    return d


def _load_continuity():
    """continuity_resolve from morning-brief/scripts (path-loaded — the tree is not a package).
    It owns the ledger's write shape; this CLI only ever calls its helpers, never reimplements
    them. Lazy, so a fact op never pays for it."""
    import importlib.util
    path = os.path.join(_HERE, "..", "..", "morning-brief", "scripts", "continuity_resolve.py")
    spec = importlib.util.spec_from_file_location("continuity_resolve", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _validated_deadline(deadline: str, allow_empty: bool = True) -> str:
    d = (deadline or "").strip()
    if not d:
        if allow_empty:
            return ""
        raise EditError("empty deadline")
    if not DATE_RE.match(d):
        raise EditError("deadline must be YYYY-MM-DD")
    return d


def _manual_loop_action(text: str, contact: str, identifier: str, deadline: str,
                        direction: str, when: datetime, cr) -> tuple[dict, str]:
    """Pure action/anchor construction shared by owner precheck and manual writer."""
    today = when.strftime("%Y-%m-%d")
    created_at = when.strftime("%Y-%m-%d %H:%M:%S")
    seed = hashlib.sha256(f"{direction}|{contact}|{text}|{today}".encode()).hexdigest()[:12]
    action = cr._normalize_action({
        "action_type": "follow_up" if direction == "you" else "waiting_on",
        # A hand-added loop belongs to no source thread. Direction still governs resolution.
        "channel": "manual", "contact_name": contact,
        "contact_identifier": identifier or None,
        "source_thread_id": f"manual:{seed}", "summary": text,
        "deadline": deadline or None, "created_at": created_at,
    })
    return action, cr.compute_anchor_key(action)


def _refuse_generic_owner_deadline(entry: dict) -> None:
    try:
        provenance, _sources = classify_ledger_sources(entry)
    except UnknownProvenanceError:
        return
    if provenance == "owner":
        raise EditError("owner task deadline cannot be edited in the dashboard")


def _op_loop_add_unlocked(text: str, contact: str = "", identifier: str = "", deadline: str = "",
                          direction: str = "you", now: datetime | None = None, cr=None,
                          *, owner_authored: bool = False, owner_event_id: str = "",
                          owner_tool_proof: dict | None = None) -> dict:
    """Add ONE loop by hand, through continuity_resolve's own write path (see module docstring).
    Anchored on a content hash of contact+text+day, so re-adding the same ask today reuses the
    existing file without counting another surfacing; an entry the user already closed is never
    resurrected (same rule apply_commitments applies)."""
    text = _validated_text(text)
    contact = (contact or "").strip()[:120]
    identifier = (identifier or "").strip()[:200]
    deadline = _validated_deadline(deadline)
    direction = (direction or "").strip().lower()
    if direction not in ("you", "them"):
        raise EditError("direction must be you or them")
    cr = cr or _load_continuity()
    when = now or cr._now_local(cr.configured_tz() or "+00:00")
    created_at = when.strftime("%Y-%m-%d %H:%M:%S")
    a, ak = _manual_loop_action(text, contact, identifier, deadline, direction, when, cr)
    items = cr._load_items()
    existing = items.get(ak)
    if existing is not None:
        if str(existing.get("status", "open")) in cr.TERMINAL:
            raise EditError("that loop was already closed — reopening isn't a thing; add a new one")
        if deadline:
            _refuse_generic_owner_deadline(existing)
            existing["deadline"] = deadline
        cr._persist(existing)
        return {"ok": True, "anchor_key": ak, "created": False}
    it = {
        "anchor_key": ak, "action_type": a.get("action_type"), "channel": a.get("channel"),
        "contact_name": a.get("contact_name"), "contact_identifier": a.get("contact_identifier"),
        "canonical_id": a.get("canonical_id"), "status": "open",
        "created_at": created_at,
        "summary": a.get("summary", ""), "ask": a.get("ask"),
        "meeting_time": a.get("meeting_time"), "deadline": a.get("deadline"),
        "source_thread_id": a.get("source_thread_id"),
        "source": "user_added",
    }
    if owner_authored:
        it["owner_event_id"] = owner_event_id
        if owner_tool_proof is not None:
            it["owner_loop_tool_create"] = owner_tool_proof
    cr._persist(it, new_owner_content=owner_authored)
    return {"ok": True, "anchor_key": ak, "created": True}


def op_loop_add(text: str, contact: str = "", identifier: str = "", deadline: str = "",
                direction: str = "you", now: datetime | None = None) -> dict:
    """Existing CLI/model-callable path: origin is unproved, so do not stamp it."""
    cr = _load_continuity()
    with cr._ledger_lock():
        return _op_loop_add_unlocked(text, contact, identifier, deadline, direction, now, cr)


def op_loop_add_admitted_owner_text(text: str, *, owner_text_span: str,
                                    owner_event_id: str, now: datetime) -> dict:
    """Future trusted-receiver seam for an exact owner-authored text span.

    The caller, not this module, must prove owner admission and that the span
    came directly from that owner event. This function is not CLI-dispatched or
    exposed to a model. A model-generated paraphrase or copied source text is
    not a qualifying span, even when the owner requested the action. The caller
    must also fence an immutable text digest with the event ID in an outer durable
    journal. A retained ledger row cannot authenticate historical absence after
    field removal, file deletion, or 30-day terminal pruning.
    """
    if (not isinstance(owner_event_id, str)
            or re.fullmatch(r"[A-Za-z0-9:_-]{1,200}", owner_event_id) is None
            or not isinstance(owner_text_span, str)
            or _validated_text(owner_text_span) != _validated_text(text)):
        raise EditError("owner-authored text evidence does not match")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise EditError("admitted owner event requires timezone-aware time")
    cr = _load_continuity()
    with cr._ledger_lock():
        text = _validated_text(text)
        # The row stores the event ID with its provenance digest under the canonical lock.
        # Strict I/O rejects detectable malformed/ambiguous files, but cannot establish
        # historical absence after field removal, deletion or terminal pruning. The caller's
        # outer durable journal must fence event ID and immutable text across the retry life.
        io_path = os.path.join(_HERE, "..", "lib", "protected_ledger_io.py")
        io_spec = importlib.util.spec_from_file_location("sotto_owner_protected_ledger_io", io_path)
        if io_spec is None or io_spec.loader is None:
            raise EditError("owner event ledger is unavailable")
        protected_io = importlib.util.module_from_spec(io_spec)
        io_spec.loader.exec_module(protected_io)
        try:
            rows = protected_io.load_protected_entries(cr._data_root(), strict=True)
        except protected_io.ProtectedLedgerUnavailableError as exc:
            raise EditError("owner event ledger is unavailable") from exc
        candidates = [row for row in rows if owner_event_id in
                      (row.get("owner_event_id"), row.get("owner_close_event_id"))]
        if candidates:
            if len(candidates) != 1:
                raise EditError("owner event intent is ambiguous")
            row = candidates[0]
            try:
                kind, _sources = classify_ledger_sources(row)
            except UnknownProvenanceError:
                raise EditError("owner event intent has invalid provenance")
            if (kind != "owner" or row.get("owner_event_id") != owner_event_id
                    or row.get("owner_loop_tool_create") is not None
                    or _validated_text(str(row.get("summary") or "")) != text):
                raise OwnerLoopPreEffectRefusalError("owner_event_conflict")
            cr._fsync_ledger_directory_tree()
            return {"ok": True, "anchor_key": row.get("anchor_key"), "created": False}

        # A different event may naturally map to the same content/day anchor. Do not turn an
        # existing source-backed or legacy row into an owner record (or report it as accepted
        # owner intent); the event key, rather than content deduplication, owns this operation.
        _proposed, proposed_anchor = _manual_loop_action(text, "", "", "", "you", now, cr)
        if any(row.get("anchor_key") == proposed_anchor for row in rows):
            raise OwnerLoopPreEffectRefusalError("owner_anchor_collision")
        return _op_loop_add_unlocked(text, now=now, cr=cr,
                                     owner_authored=True, owner_event_id=owner_event_id)


def _owner_close_request(anchor: str, command: str, owner_event_id: str, now: datetime) -> str:
    if (type(anchor) is not str or not anchor or anchor.strip() != anchor or len(anchor) > 256
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                   for char in anchor)
            or type(command) is not str or command != "/loop close " + anchor
            or type(owner_event_id) is not str
            or re.fullmatch(r"[A-Za-z0-9:_-]{1,200}", owner_event_id) is None
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None):
        raise EditError("invalid admitted owner close command")
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _owner_close_rows(cr):
    io_path = os.path.join(_HERE, "..", "lib", "protected_ledger_io.py")
    spec = importlib.util.spec_from_file_location("sotto_owner_close_protected_ledger_io", io_path)
    if spec is None or spec.loader is None:
        raise EditError("owner close ledger is unavailable")
    protected_io = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(protected_io)
    return protected_io.load_protected_entries(cr._data_root(), strict=True)


def _owner_close_target(rows: list[dict], anchor: str, owner_event_id: str,
                        *, inspect: bool = False) -> dict:
    for row in rows:
        if (row.get("owner_close_event_id") == owner_event_id
                and row.get("owner_loop_tool_close") is not None):
            raise OwnerLoopClosePreEffectRefusalError("owner_close_event_conflict")
        if row.get("owner_event_id") == owner_event_id:
            raise OwnerLoopClosePreEffectRefusalError("owner_close_event_conflict")
        if row.get("owner_close_event_id") == owner_event_id and row.get("anchor_key") != anchor:
            try:
                kind, sources = classify_ledger_sources(row)
            except UnknownProvenanceError as error:
                raise EditError("owner close provenance is unavailable") from error
            if kind != "owner" or sources:
                raise EditError("owner close provenance is unavailable")
            if inspect:
                raise EditError("owner close receipt is unavailable")
            raise OwnerLoopClosePreEffectRefusalError("owner_close_event_conflict")
    matches = [row for row in rows if row.get("anchor_key") == anchor]
    if len(matches) > 1:
        raise EditError("owner close ledger is ambiguous")
    if not matches:
        if inspect:
            raise EditError("owner close receipt is unavailable")
        raise OwnerLoopClosePreEffectRefusalError("owner_close_target_unavailable")
    row = matches[0]
    try:
        kind, sources = classify_ledger_sources(row)
    except UnknownProvenanceError as error:
        if row.get("content_provenance") is None and not inspect:
            raise OwnerLoopClosePreEffectRefusalError("owner_close_target_unavailable") from error
        raise EditError("owner close provenance is unavailable") from error
    if kind != "owner" or sources:
        if inspect:
            raise EditError("owner close receipt is unavailable")
        raise OwnerLoopClosePreEffectRefusalError("owner_close_target_unavailable")
    return row


def op_loop_close_prepare_admitted_owner_anchor(anchor: str, *, command: str,
                                                 owner_event_id: str, now: datetime) -> dict:
    """Read-only version proof for one exact owner command; caller must journal it."""
    _owner_close_request(anchor, command, owner_event_id, now)
    cr = _load_continuity()
    with cr._ledger_lock():
        row = _owner_close_target(_owner_close_rows(cr), anchor, owner_event_id)
        if row.get("status", "open") not in cr.ACTIVE or row.get("owner_close_event_id") is not None:
            raise OwnerLoopClosePreEffectRefusalError("owner_close_target_unavailable")
        return {"expected_row_digest": row["content_provenance"]["digest"]}


def op_loop_close_admitted_owner_anchor(anchor: str, *, command: str,
                                        owner_event_id: str, expected_row_digest: str,
                                        now: datetime) -> dict:
    """Close one proved owner row once through the canonical locked persistence path."""
    text_hash = _owner_close_request(anchor, command, owner_event_id, now)
    if type(expected_row_digest) is not str or re.fullmatch(r"[a-f0-9]{64}", expected_row_digest) is None:
        raise EditError("invalid owner close row version")
    cr = _load_continuity()
    with cr._ledger_lock():
        row = _owner_close_target(_owner_close_rows(cr), anchor, owner_event_id)
        if row.get("owner_close_event_id") == owner_event_id:
            if (row.get("owner_close_text_hash") != text_hash
                    or row.get("owner_close_previous_digest") != expected_row_digest):
                raise OwnerLoopClosePreEffectRefusalError("owner_close_event_conflict")
            if row.get("status") != "resolved" or row.get("resolution") != "user_resolved":
                raise EditError("owner close receipt is unavailable")
            cr._fsync_ledger_directory_tree()
            return {"ok": True, "anchor_key": anchor,
                    "post_digest": row["content_provenance"]["digest"], "created": False}
        if row.get("status", "open") not in cr.ACTIVE:
            raise OwnerLoopClosePreEffectRefusalError("owner_close_target_unavailable")
        if row["content_provenance"]["digest"] != expected_row_digest:
            raise OwnerLoopClosePreEffectRefusalError("owner_close_target_changed")
        if row.get("owner_close_event_id") is not None:
            raise OwnerLoopClosePreEffectRefusalError("owner_close_event_conflict")
        entry = cr._load_items().get(anchor)
        if entry is None or digest_frontmatter(entry) != expected_row_digest:
            raise EditError("owner close canonical row changed")
        cr._terminate(entry, "resolved", "user_resolved", now.strftime("%Y-%m-%d"))
        entry["owner_close_event_id"] = owner_event_id
        entry["owner_close_text_hash"] = text_hash
        entry["owner_close_previous_digest"] = expected_row_digest
        cr._persist(entry, owner_close_previous_digest=expected_row_digest)
        kind, sources = classify_ledger_sources(entry)
        if kind != "owner" or sources:
            raise EditError("owner close post-state is unavailable")
        return {"ok": True, "anchor_key": anchor,
                "post_digest": entry["content_provenance"]["digest"], "created": True}


def op_loop_close_inspect_admitted_owner_anchor(anchor: str, *, command: str,
                                                 owner_event_id: str, expected_row_digest: str,
                                                 now: datetime) -> dict:
    """Prove an interrupted close from the canonical row; never perform an effect."""
    text_hash = _owner_close_request(anchor, command, owner_event_id, now)
    if type(expected_row_digest) is not str or re.fullmatch(r"[a-f0-9]{64}", expected_row_digest) is None:
        raise EditError("invalid owner close row version")
    cr = _load_continuity()
    with cr._ledger_lock():
        row = _owner_close_target(_owner_close_rows(cr), anchor, owner_event_id, inspect=True)
        if (row.get("owner_close_event_id") != owner_event_id
                or row.get("owner_close_text_hash") != text_hash
                or row.get("owner_close_previous_digest") != expected_row_digest
                or row.get("status") != "resolved" or row.get("resolution") != "user_resolved"):
            raise EditError("owner close receipt is unavailable")
        cr._fsync_ledger_directory_tree()
        return {"ok": True, "anchor_key": anchor, "post_digest": row["content_provenance"]["digest"]}


def _loop_retarget_plan(anchor, text, contact, identifier, deadline, direction, now, cr):
    if not ANCHOR_RE.match(anchor or ""):
        raise EditError("invalid anchor key")
    text = _validated_text(text)
    contact = (contact or "").strip()[:120]
    identifier = (identifier or "").strip()[:200]
    deadline = _validated_deadline(deadline)
    direction = (direction or "").strip().lower()
    if direction not in ("you", "them"):
        raise EditError("direction must be you or them")
    when = now or cr._now_local(cr.configured_tz() or "+00:00")
    today = when.strftime("%Y-%m-%d")
    created_at = when.strftime("%Y-%m-%d %H:%M:%S")
    import hashlib
    h = hashlib.sha256(f"{direction}|{contact}|{text}|{today}".encode()).hexdigest()[:12]
    raw = {
        "action_type": "follow_up" if direction == "you" else "waiting_on",
        "channel": "manual", "contact_name": contact,
        "contact_identifier": identifier or None, "source_thread_id": f"manual:{h}",
        "summary": text, "deadline": deadline or None, "created_at": created_at,
    }
    action = cr._normalize_action(raw)
    replacement_key = cr.compute_anchor_key(action)
    replacement = {
        'anchor_key': replacement_key, 'action_type': action.get('action_type'),
        'channel': action.get('channel'), 'contact_name': action.get('contact_name'),
        'contact_identifier': action.get('contact_identifier'), 'status': 'open',
        'created_at': created_at, 'summary': text, 'deadline': action.get('deadline'),
        'source_thread_id': action.get('source_thread_id'), 'source': 'user_correction',
        'corrected_from': anchor,
    }
    return action, replacement_key, text, created_at, today, direction, replacement


def op_loop_retarget(anchor: str, text: str, contact: str = "", identifier: str = "",
                     deadline: str = "", direction: str = "them",
                     now: datetime | None = None) -> dict:
    """Correct a loop's direction in one locked command. The replacement is persisted first, so
    even an exceptional second write can leave a duplicate but can never make the obligation vanish."""
    cr = _load_continuity()
    action, replacement_key, text, created_at, today, direction, planned = _loop_retarget_plan(
        anchor, text, contact, identifier, deadline, direction, now, cr)
    with cr._ledger_lock():
        items = cr._load_items()
        old = items.get(anchor)
        if old is None or str(old.get("status", "open")) in cr.TERMINAL:
            raise EditError(f"open loop not found: {anchor}")
        replacement = items.get(replacement_key)
        if replacement is not None and str(replacement.get("status", "open")) in cr.TERMINAL:
            raise EditError("the corrected loop was already closed")
        if replacement is None:
            replacement = planned
        cr._persist(replacement)                  # obligation remains live before the old row retires
        cr._terminate(old, "dismissed", "user_retargeted", today)
        old["retargeted_to"] = replacement_key
        cr._persist(old)
    return {"ok": True, "old_anchor_key": anchor, "anchor_key": replacement_key,
            "status": "open", "direction": direction}


def op_loop_deadline(anchor: str, deadline: str) -> dict:
    """Set (or clear) ONE loop's deadline. A deadline orders the brief and marks the row overdue;
    nothing expires a loop (a loop you owe parks after 14 untouched days instead, and `keep`
    brings it back). Persists the full frontmatter back through the same
    yaml.safe_dump(sort_keys=False) shape op_loop uses."""
    if not ANCHOR_RE.match(anchor or ""):
        raise EditError("invalid anchor key")
    deadline = _validated_deadline(deadline)
    cr = _load_continuity()
    with cr._ledger_lock():
        entry = cr._load_items().get(anchor)
        if entry is None:
            raise EditError(f"loop not found: {anchor}")
        _refuse_generic_owner_deadline(entry)
        if deadline:
            entry["deadline"] = deadline
        else:
            entry.pop("deadline", None)
        cr._persist(entry)
    return {"ok": True, "anchor_key": anchor, "deadline": deadline}


def op_loop(anchor: str, to: str, today: str | None = None) -> dict:
    if not ANCHOR_RE.match(anchor or ""):
        raise EditError("invalid anchor key")
    if to not in ("resolved", "dismissed"):
        raise EditError("invalid target status")
    cr = _load_continuity()
    if today is None:
        import timeutil
        today = timeutil._user_local_date(timeutil.configured_tz() or "+00:00")
    with cr._ledger_lock():
        entry = cr._load_items().get(anchor)
        if entry is None:
            raise EditError(f"loop not found: {anchor}")
        # The resolver's termination semantics, with the user as the resolution source.
        cr._terminate(entry, to, f"user_{to}", today)
        cr._persist(entry)
    return {"ok": True, "anchor_key": anchor, "status": to}


def run(argv: list) -> dict:
    ap = argparse.ArgumentParser(
        description="Apply a user edit from The Window dashboard through the chat-equivalent "
                    "knowledge/continuity code paths. Fact ops are PEOPLE-ONLY (companies carry "
                    "no facts map); a company record is corrected with --op company-about.")
    ap.add_argument("--op", required=True,
                    choices=("correct", "archive", "add", "add-identifier", "company-about",
                             "loop", "loop-add", "loop-retarget", "loop-deadline", "merge", "merge-dismiss",
                             "relation-add", "relation-remove"))
    ap.add_argument("--slug", default="",
                    help="person slug (knowledge/people/<slug>.md) — or the COMPANY slug "
                         "(knowledge/companies/<slug>.md) for --op company-about")
    ap.add_argument("--fact-id", default="", dest="fact_id")
    ap.add_argument("--text", default="")
    ap.add_argument("--memory-type", default="context", dest="memory_type")
    ap.add_argument("--anchor", default="", help="continuity anchor_key (loop ops)")
    ap.add_argument("--to", default="", choices=("", "resolved", "dismissed"))
    ap.add_argument("--contact", default="", help="who the loop is with (loop-add)")
    ap.add_argument("--identifier", default="",
                    help="their email/phone — the one to attach (add-identifier), or the "
                         "counterpart's, if known (loop-add)")
    ap.add_argument("--direction", default="you", choices=("you", "them"),
                    help="who owes the action: you (default) or them (loop-add)")
    ap.add_argument("--deadline", default="", help="YYYY-MM-DD, or empty to clear")
    ap.add_argument("--from", default="", dest="from_slug",
                    help="person slug that DISAPPEARS into --into (merge op)")
    ap.add_argument("--into", default="", dest="into_slug",
                    help="person slug that SURVIVES the merge (merge op)")
    ap.add_argument("--type", default="", dest="rel_type",
                    help="relation type (relation ops) — introduced_by | introduced | works_with | "
                         "family_of | partner_of | met_through | connected")
    ap.add_argument("--other-slug", default="", dest="other_slug",
                    help="the person at the OTHER end of the relation")
    ap.add_argument("--date", default="", help="YYYY-MM-DD, when the relation happened")
    a = ap.parse_args(argv)
    if a.op == "correct":
        return op_correct(a.slug, a.fact_id, a.text)
    if a.op == "archive":
        return op_archive(a.slug, a.fact_id)
    if a.op == "add":
        return op_add(a.slug, a.text, a.memory_type)
    if a.op == "add-identifier":
        return op_add_identifier(a.slug, a.identifier)
    if a.op == "company-about":
        return op_company_about(a.slug, a.text)
    if a.op in ("relation-add", "relation-remove"):
        return op_relation(a.slug, a.other_slug, a.rel_type, a.op == "relation-add", a.date)
    if a.op == "merge":
        return op_merge(a.from_slug, a.into_slug)
    if a.op == "merge-dismiss":
        return op_merge_dismiss(a.from_slug, a.into_slug)
    if a.op == "loop-add":
        return op_loop_add(a.text, a.contact, a.identifier, a.deadline, a.direction)
    if a.op == "loop-retarget":
        return op_loop_retarget(a.anchor, a.text, a.contact, a.identifier, a.deadline, a.direction)
    if a.op == "loop-deadline":
        return op_loop_deadline(a.anchor, a.deadline)
    return op_loop(a.anchor, a.to)


class OwnerLoopToolRefusalError(EditError):
    """A proved natural owner request cannot mutate this canonical row."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _owner_task_phrase_matches(row, phrase):
    title=str(row.get('summary') or '').casefold();name=phrase.casefold();at=title.find(name)
    return (row.get('channel')=='manual' and at>=0
        and (at==0 or not title[at-1].isalnum())
        and (at+len(name)==len(title) or not title[at+len(name)].isalnum()))


def op_loop_tool(args: dict, proof: dict, *, now: datetime, inspect: bool = False) -> dict | None:
    """One exact owner span or current protected close, under the canonical ledger lock."""
    cr = _load_continuity()
    event = proof['event_id']
    with cr._ledger_lock():
        rows = _owner_close_rows(cr)
        matches = [(row, key) for row in rows for key in ('owner_event_id', 'owner_close_event_id')
                   if row.get(key) == event]
        if matches:
            if len(matches) != 1:
                raise OwnerLoopToolRefusalError('event_conflict')
            row, key = matches[0]
            saved_key = 'owner_loop_tool_create' if args['operation'] == 'add' else 'owner_loop_tool_close'
            expected_key = 'owner_event_id' if args['operation'] == 'add' else 'owner_close_event_id'
            kind, sources = classify_ledger_sources(row)
            if key != expected_key or row.get(saved_key) != proof or kind != 'owner' or sources:
                raise OwnerLoopToolRefusalError('event_conflict')
            cr._fsync_ledger_directory_tree()
            return row
        if inspect:
            return None
        if args['operation'] == 'add':
            text = _validated_text(args['text'])
            direction,contact,identifier=args.get('direction','you'),args.get('contact',''),args.get('identifier','')
            _action, anchor = _manual_loop_action(text, contact, identifier, '', direction, now, cr)
            if any(row.get('anchor_key') == anchor for row in rows):
                raise OwnerLoopToolRefusalError('owner_anchor_collision')
            _op_loop_add_unlocked(text, contact=contact, identifier=identifier, direction=direction, now=now, cr=cr, owner_authored=True,
                                  owner_event_id=event, owner_tool_proof=proof)
            return next(row for row in _owner_close_rows(cr) if row.get('owner_event_id') == event)
        anchor = args['anchor']
        try:
            row = _owner_close_target(rows, anchor, event)
        except OwnerLoopClosePreEffectRefusalError as error:
            raise OwnerLoopToolRefusalError('target_unavailable') from error
        if row.get('status', 'open') not in cr.ACTIVE or row.get('owner_close_event_id') is not None:
            raise OwnerLoopToolRefusalError('target_unavailable')
        create = row.get('owner_loop_tool_create')
        if create is not None and create.get('identity_digest') != proof['identity_digest']:
            raise OwnerLoopToolRefusalError('target_unavailable')
        if args.get('correction'):
            selected=[r for r in rows if r.get('status','open') in cr.ACTIVE and _owner_task_phrase_matches(r,args['correction'])]
            if len(selected)!=1 or selected[0].get('anchor_key')!=anchor:
                raise OwnerLoopToolRefusalError('target_unavailable')
        previous = row['content_provenance']['digest']
        entry = cr._load_items().get(anchor)
        if entry is None or digest_frontmatter(entry) != previous:
            raise EditError('owner task canonical row changed')
        cr._terminate(entry, 'resolved', 'user_resolved', now.strftime('%Y-%m-%d'))
        entry['owner_close_event_id'] = event
        entry['owner_close_text_hash'] = proof['text_hash']
        entry['owner_close_previous_digest'] = previous
        entry['owner_loop_tool_close'] = proof
        cr._persist(entry, owner_close_previous_digest=previous, owner_close_tool_proof=proof)
        kind, sources = classify_ledger_sources(entry)
        if kind != 'owner' or sources:
            raise EditError('owner task post-state unavailable')
        return entry



def source_offer_row(snapshot):
    """Read-only current source snapshot proof, shared by exact offer continuations."""
    cr = _load_continuity()
    with cr._ledger_lock():
        return _source_offer_row(cr, snapshot, _owner_close_rows(cr))


def _source_offer_row(cr, snapshot, strict_rows):
    from delivery_effects import loop_version, loop_identity
    from ledger_provenance import source_loop_sources
    from source_context import allowed
    if type(snapshot) is not dict or set(snapshot) != {'anchor_key', 'version', 'identity', 'sources'}:
        raise OwnerLoopToolRefusalError('target_unavailable')
    selected = [row for row in strict_rows if row.get('anchor_key') == snapshot['anchor_key']]
    row = selected[0] if len(selected) == 1 else None
    if row is None or row.get('status', 'open') not in cr.ACTIVE:
        raise OwnerLoopToolRefusalError('target_unavailable')
    try:
        sources = source_loop_sources(row)
    except ValueError as error:
        raise OwnerLoopToolRefusalError('target_unavailable') from error
    if (loop_version(row) != snapshot['version'] or loop_identity(row) != snapshot['identity']
            or not set(sources) <= set(snapshot['sources'])):
        raise OwnerLoopToolRefusalError('target_changed')
    if not snapshot['sources'] or any(allowed(source) is not True for source in snapshot['sources']):
        raise OwnerLoopToolRefusalError('source_unavailable')
    return row


def op_source_offer_reply(snapshot, saved, *, now, inspect=False):
    """One exact source obligation correction with its immutable receipt, under ledger lock."""
    from delivery_effects import loop_version, loop_identity
    cr = _load_continuity()
    with cr._ledger_lock():
        rows = cr._load_items()
        strict_rows = _owner_close_rows(cr)
        matches = [row for row in strict_rows if isinstance(row.get('source_offer_reply'), dict)
                   and row['source_offer_reply'].get('event_id') == saved['event_id']]
        if matches:
            if (len(matches) != 1 or matches[0]['source_offer_reply'].get('proof_hash') != saved['proof_hash']
                    or (not inspect and matches[0]['source_offer_reply'] != saved)):
                raise OwnerLoopToolRefusalError('event_conflict')
            cr._fsync_ledger_directory_tree()
            return matches[0]['source_offer_reply']['receipt']
        if inspect:
            return None
        row = _source_offer_row(cr, snapshot, strict_rows)
        target = saved['receipt']['outcome']['action']
        if target == 'declined':
            return saved['receipt']
        if target not in ('resolved', 'dismissed'):
            raise EditError('invalid source reply transition')
        canonical = rows.get(snapshot['anchor_key'])
        if canonical is None or loop_version(canonical) != loop_version(row) or loop_identity(canonical) != loop_identity(row):
            raise OwnerLoopToolRefusalError('target_changed')
        row = canonical
        cr._terminate(row, target, 'user_'+target, cr._to_user_zone(now).strftime('%Y-%m-%d'))
        row['source_offer_reply'] = saved
        cr._persist(row, source_offer_reply=saved)
        return saved['receipt']

def main():
    try:
        print(json.dumps(run(sys.argv[1:])))
    except EditError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(2)
    except Exception as e:  # noqa: BLE001 — the receiver parses stdout; never die JSON-less
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()


def op_source_loop_tool(args, proof, *, now, inspect=False, authority):
    """Exact source correction; private canonical intent keeps interrupted retargets recoverable."""
    from delivery_effects import loop_version, loop_identity
    from ledger_provenance import source_loop_sources
    cr = _load_continuity()
    expected_revision = args['source_revision']
    intent_started = False
    def guard(sources):
        current = authority()
        permitted = set(current.get('bridge_sources', []) + current.get('google_sources', [])
                        + current.get('connector_sources', []))
        if (current.get('complete') is not True or current.get('revision') != expected_revision
                or not set(sources) <= permitted):
            if inspect or intent_started:
                raise EditError('source correction current authority unavailable; intent remains held')
            raise OwnerLoopToolRefusalError('source_unavailable')
    def content(row):
        return {k:v for k,v in row.items() if k not in ('_path','source_loop_correction','content_provenance','proof_transition')}
    def receipt(old, record, replacement=None):
        # The original canonical digest excludes provenance/path; exclude only this private
        # receipt field additionally so retaining its post digest is not self-referential.
        common = {'version':1,'status':'committed', **{key:proof[key] for key in
                  ('action_id','event_id','payload_hash','args_hash')}, 'operation':args['operation']}
        if replacement is None:
            return {**common,'anchor_key':old['anchor_key'], 'post_digest':digest_frontmatter({k:v for k,v in old.items() if k!='source_loop_correction'}),
                    'loop_status':old['status']}
        return {**common,'old_anchor_key':old['anchor_key'],'anchor_key':replacement['anchor_key'],
                'old_post_digest':digest_frontmatter({k:v for k,v in old.items() if k!='source_loop_correction'}),
                'post_digest':replacement['content_provenance']['digest'],
                'loop_status':'open','direction':args['direction']}
    with cr._ledger_lock():
        rows = _owner_close_rows(cr)
        saved = [row for row in rows if isinstance(row.get('source_loop_correction'),dict)
                 and row['source_loop_correction'].get('proof',{}).get('event_id') == proof['event_id']]
        if saved:
            if len(saved) != 1 or saved[0]['source_loop_correction']['proof'] != proof:
                raise OwnerLoopToolRefusalError('event_conflict')
            old = saved[0]
            record = old['source_loop_correction']
            if record.get('status') == 'committed':
                retained = record.get('receipt')
                post = record.get('post_state')
                replacement_post = record.get('replacement_post_state')
                if (type(post) is not dict or content(post) != record.get('terminal')
                        or post.get('anchor_key') != args['anchor']
                        or record.get('original',{}).get('anchor_key') != args['anchor']
                        or (args['operation']=='correct' and (replacement_post is not None
                            or post.get('status') != args['to']))
                        or (args['operation']=='retarget' and (type(replacement_post) is not dict
                            or content(replacement_post) != record.get('replacement')
                            or post.get('status')!='dismissed'
                            or post.get('retargeted_to') != replacement_post.get('anchor_key')
                            or replacement_post.get('action_type') != ('follow_up' if args['direction']=='you' else 'waiting_on')))):
                    raise EditError('source correction retained receipt state unavailable')
                expected = receipt(post, record, replacement_post)
                if retained != expected:
                    raise EditError('source correction receipt unavailable')
                cr._fsync_ledger_directory_tree()
                return retained
            if not inspect:
                raise EditError('source correction outcome unconfirmed')
            expected_revision = authority().get('revision')
            if type(expected_revision) is not str or re.fullmatch(r'[a-f0-9]{64}',expected_revision) is None:
                raise EditError('source correction current authority unavailable')
            guard(record['sources'])
            if content(old) not in (record['original'], record['terminal']):
                raise EditError('source correction retained state changed')
        else:
            if inspect:
                return None
            matches = [row for row in rows if row.get('anchor_key') == args['anchor']]
            if len(matches) != 1 or matches[0].get('status','open') not in cr.ACTIVE:
                raise OwnerLoopToolRefusalError('target_unavailable')
            old = matches[0]
            if old.get('source_loop_correction') is not None:
                raise OwnerLoopToolRefusalError('target_unavailable')
            try:
                sources = sorted(source_loop_sources(old))
            except ValueError as error:
                try:
                    kind, permitted = classify_ledger_sources(old)
                except ValueError:
                    raise OwnerLoopToolRefusalError('target_unavailable') from error
                if kind != 'owner' or permitted:
                    raise OwnerLoopToolRefusalError('target_unavailable') from error
                sources = []
            guard(sources)
            if loop_version(old) != args['loop_version'] or loop_identity(old) != args['loop_identity']:
                raise OwnerLoopToolRefusalError('target_changed')
            record = {'proof':proof,'sources':sources,'original':content(old),'status':'started'}
            if args['operation'] == 'retarget':
                text = args.get('text', old.get('summary') or old.get('ask') or '')
                contact = args.get('contact', old.get('contact_name') or '')
                identifier = args.get('identifier', '' if contact != (old.get('contact_name') or '')
                                      else old.get('contact_identifier') or '')
                for key,value in (('text',text),('contact',contact),('identifier',identifier)):
                    if key in args and value not in proof['args']['correction'] and value != (
                            old.get('summary') or old.get('ask') if key=='text' else
                            old.get('contact_name') if key=='contact' else
                            old.get('contact_identifier') if contact == (old.get('contact_name') or '') else None):
                        raise OwnerLoopToolRefusalError('correction_unavailable')
                action, key, text, created, _today, _direction, planned = _loop_retarget_plan(
                    args['anchor'], text, contact, identifier, str(old.get('deadline') or ''), args['direction'], now, cr)
                if any(row.get('anchor_key') == key for row in rows):
                    raise OwnerLoopToolRefusalError('anchor_collision')
                record['replacement'] = {**planned, 'owner_event_id':proof['event_id'],
                                         'owner_source_correction':{'proof':proof,'sources':sources,
                                             'version':args['loop_version'],'identity':args['loop_identity']}}
            terminal = dict(old)
            target = args['to'] if args['operation']=='correct' else 'dismissed'
            cr._terminate(terminal, target, 'user_'+target if args['operation']=='correct' else 'user_retargeted',
                          cr._to_user_zone(now).strftime('%Y-%m-%d'))
            if 'replacement' in record:
                terminal['retargeted_to'] = record['replacement']['anchor_key']
            record['terminal'] = content(terminal)
            old['source_loop_correction'] = record
            intent_started = True
            cr._persist(old, source_loop_correction=record)
        # Intent is already fsynced. Retire source before any replacement is made visible.
        target = args['to'] if args['operation']=='correct' else 'dismissed'
        if old.get('status') in cr.ACTIVE:
            guard(record['sources'])
            old.update(record['terminal'])
            cr._persist(old, source_loop_correction=record)
        replacement = None
        if 'replacement' in record:
            plan = record['replacement']
            existing = [row for row in _owner_close_rows(cr) if row.get('anchor_key') == plan['anchor_key']]
            if existing:
                if len(existing)!=1 or content(existing[0]) != plan:
                    raise EditError('source correction replacement changed')
                replacement = existing[0]
                kind,sources = classify_ledger_sources(replacement)
                if kind!='owner' or sources:
                    raise EditError('source correction replacement provenance unavailable')
            else:
                guard(record['sources'])
                replacement = dict(plan)
                cr._persist(replacement, new_owner_content=True)
        guard(record['sources'])
        record['post_state'] = {k:v for k,v in old.items() if k not in ('_path','source_loop_correction')}
        if replacement is not None:
            record['replacement_post_state'] = {k:v for k,v in replacement.items() if k!='_path'}
        record['receipt'] = receipt(old, record, replacement)
        record['status'] = 'committed'
        old['source_loop_correction'] = record
        cr._persist(old, source_loop_correction=record)
        return record['receipt']


def _relation_digest(value):
    import hashlib
    import json
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        separators=(',',':')).encode()).hexdigest()


def _relation_pair(canonical_id, other_canonical_id, relation_type):
    import glob
    selected = {}
    for path in sorted(glob.glob(os.path.join(kg.people_dir(),'*.md'))):
        if os.path.islink(path):
            raise EditError('relationship target unavailable')
        with open(path,encoding='utf-8') as handle:
            person = kg.parse_person_file(handle.read())
        if person.canonical_id in (canonical_id,other_canonical_id):
            if person.canonical_id in selected:
                raise OwnerLoopToolRefusalError('target_changed')
            selected[person.canonical_id] = (path,person)
    if canonical_id == other_canonical_id or set(selected) != {canonical_id,other_canonical_id}:
        raise OwnerLoopToolRefusalError('target_changed')
    a,pa = selected[canonical_id]; b,pb = selected[other_canonical_id]
    sa,sb = (os.path.splitext(os.path.basename(path))[0] for path in (a,b))
    forward = [r.to_yaml_dict() for r in pa.relations if r.type==relation_type and r.slug==sb]
    inverse = [r.to_yaml_dict() for r in pb.relations if r.type==kg.RELATION_INVERSE[relation_type] and r.slug==sa]
    if len(forward)>1 or len(inverse)>1 or bool(forward)!=bool(inverse):
        raise OwnerLoopToolRefusalError('target_changed')
    pair = {'canonical_id':canonical_id,'other_canonical_id':other_canonical_id,
        'name':pa.name,'other_name':pb.name,'forward':forward,'inverse':inverse}
    return pair, (a,b), (sa,sb)


def relation_review(args, *, authority):
    value = authority()
    if value.get('complete') is not True or value.get('revision')!=args['source_revision']:
        raise OwnerLoopToolRefusalError('source_unavailable')
    ku = _load_knowledge_update()
    with ku.graph_lock():
        pair,_paths,_slugs = _relation_pair(args['canonical_id'],args['other_canonical_id'],args['relation_type'])
        for name in (pair['name'],pair['other_name']):
            if (not name or name.strip()!=name or len(name)>500
                    or any(ord(c)<32 or 127<=ord(c)<=159 or c in '\u2028\u2029' or 0xD800<=ord(c)<=0xDFFF for c in name)):
                raise OwnerLoopToolRefusalError('correction_unavailable')
        present = bool(pair['forward'])
        if args['mode']=='add' and present and args['date'] and args['date']!=pair['forward'][0].get('date',''):
            raise OwnerLoopToolRefusalError('target_changed')
        review = {'operation':'relation',**{k:args[k] for k in ('mode','canonical_id','other_canonical_id','relation_type','date','source_revision')},
            'name':pair['name'],'other_name':pair['other_name'],'present':present,'relation_version':_relation_digest(pair)}
        value = authority()
        if value.get('complete') is not True or value.get('revision')!=args['source_revision']:
            raise OwnerLoopToolRefusalError('source_unavailable')
        return {**review,'review_hash':_relation_digest(review)}


def op_admitted_relation(review, proposal, proof, *, now, inspect=False, authority):
    """One original paired relation write; retained uncertain effects never repeat."""
    import jsonstore
    import preferences
    path = preferences._path()
    def current():
        value = authority()
        if value.get('complete') is not True or value.get('revision')!=review['source_revision']:
            raise OwnerLoopToolRefusalError('source_unavailable')
    def receipt(post, changed):
        return {'version':1,'status':'committed',**{k:proof[k] for k in ('action_id','event_id','payload_hash','args_hash')},
            'operation':'relation',**{k:review[k] for k in ('mode','canonical_id','other_canonical_id','relation_type','date','review_hash')},
            'changed':changed,'post_digest':_relation_digest(post)}
    with jsonstore.lock(path):
        data = jsonstore.read(path,default={},strict=True)
        records = data.get('owner_preference_receipts',{})
        if type(records) is not dict:
            raise EditError('relationship receipt unavailable')
        saved = records.get(proof['event_id'])
        if saved is not None:
            if (type(saved) is not dict or saved.get('tag')!='person_relation' or saved.get('proof')!=proof
                    or saved.get('review')!=review or saved.get('proposal')!=proposal):
                raise OwnerLoopToolRefusalError('event_conflict')
            base = {'tag','proof','review','proposal','target','slugs','status'}
            if (type(saved.get('target')) is not dict or _relation_digest(saved['target'])!=review['relation_version']
                    or type(saved.get('slugs')) is not list or len(saved['slugs'])!=2
                    or any(type(slug) is not str or not slug or '/' in slug or slug in ('.','..') for slug in saved['slugs'])):
                raise EditError('relationship receipt unavailable')
            if set(saved)==base and saved['status']=='contacting':
                preferences._sync_existing_preferences(path)
                return {'version':1,'status':'unknown'}
            if (set(saved)!=base|{'post','receipt'} or saved['status']!='committed'
                    or saved['post']!=_relation_expected(saved['target'],review,saved['slugs'])
                    or saved['receipt']!=receipt(saved['post'],review['present']!=(review['mode']=='add'))):
                raise EditError('relationship receipt unavailable')
            preferences._sync_existing_preferences(path)
            return saved['receipt']
        if inspect:
            return None
        if any(type(row) is dict and row.get('tag')=='person_relation' and row.get('proposal',{}).get('action_id')==proposal['action_id'] for row in records.values()):
            raise OwnerLoopToolRefusalError('target_unavailable')
        current()
        ku = _load_knowledge_update()
        with ku.graph_lock():
            pair,paths,slugs = _relation_pair(review['canonical_id'],review['other_canonical_id'],review['relation_type'])
            trusted = relation_review({'operation':'propose_relation',**{k:review[k] for k in
                ('mode','canonical_id','other_canonical_id','relation_type','date','source_revision')}},authority=authority)
            if trusted!=review:
                raise OwnerLoopToolRefusalError('target_changed')
            current()
            saved = {'tag':'person_relation','proof':proof,'review':review,'proposal':proposal,
                'target':pair,'slugs':list(slugs),'status':'contacting'}
            records[proof['event_id']]=saved;data['owner_preference_receipts']=records
            preferences._durable_preferences_replace(path,data)
            try:
                op_relation(slugs[0],slugs[1],review['relation_type'],review['mode']=='add',review['date'],now)
                post,paths,_ = _relation_pair(review['canonical_id'],review['other_canonical_id'],review['relation_type'])
                if post!=_relation_expected(pair,review,slugs):
                    raise EditError('relationship write unconfirmed')
                for file in paths:
                    preferences._sync_existing_preferences(file)
                current()
                saved={**saved,'status':'committed','post':post,'receipt':receipt(post,review['present']!=(review['mode']=='add'))}
                records[proof['event_id']]=saved
                preferences._durable_preferences_replace(path,data)
                return saved['receipt']
            except Exception:
                return {'version':1,'status':'unknown'}


def _relation_expected(pair,review,slugs):
    if review['mode']=='remove':
        return {**pair,'forward':[],'inverse':[]}
    if pair['forward']:
        return pair
    forward = kg.Relation(type=review['relation_type'],slug=slugs[1],name=pair['other_name'],
        date=review['date'] or None,source=SOURCE,confidence=1.0).to_yaml_dict()
    inverse = kg.Relation(type=kg.RELATION_INVERSE[review['relation_type']],slug=slugs[0],name=pair['name'],
        date=review['date'] or None,source=SOURCE,confidence=1.0).to_yaml_dict()
    return {**pair,'forward':[forward],'inverse':[inverse]}


def identifier_review(args, *, authority):
    import glob
    def current():
        value=authority()
        if value.get('complete') is not True or value.get('revision')!=args['source_revision']:
            raise OwnerLoopToolRefusalError('source_unavailable')
    current()
    try:
        ident=_validated_identifier(args['identifier'])
        normalized=kg.normalize_identifier(ident)
        if not normalized:
            raise EditError('identifier unavailable')
    except EditError as error:
        raise OwnerLoopToolRefusalError('correction_unavailable') from error
    with _load_knowledge_update().graph_lock():
        matches=[]
        for path in sorted(glob.glob(os.path.join(kg.people_dir(),'*.md'))):
            if os.path.islink(path):
                raise EditError('person unavailable')
            with open(path,encoding='utf-8') as handle:
                person=kg.parse_person_file(handle.read())
            if person.canonical_id==args['canonical_id']:
                matches.append((path,person))
        if len(matches)!=1:
            raise OwnerLoopToolRefusalError('target_changed')
        path,person=matches[0]
        name=person.name
        if (not isinstance(name,str) or not name or name.strip()!=name or len(name)>500
                or any(ord(c)<32 or 127<=ord(c)<=159 or 0xD800<=ord(c)<=0xDFFF or c in '\u2028\u2029' for c in name)):
            raise OwnerLoopToolRefusalError('target_changed')
        present=any(kg.normalize_identifier(str(i))==normalized for i in person.identifiers)
        owner=None if present else kg.find_person_file(identifier=ident)
        if owner and os.path.abspath(owner)!=os.path.abspath(path):
            raise OwnerLoopToolRefusalError('identifier_collision')
        result={'operation':'identifier','canonical_id':args['canonical_id'],'name':name,
            'identifier':args['identifier'],'normalized_identifier':normalized,'present':present,
            'source_revision':args['source_revision']}
        current()
        return {**result,'review_hash':_relation_digest(result)}


def op_admitted_identifier_add(args, proof, *, now, inspect=False, authority):
    """Inspect legacy teaching receipts; new direct teaching is unavailable."""
    import hashlib
    import json
    import jsonstore
    import preferences

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
            separators=(',', ':')).encode()).hexdigest()
    def receipt(post, added):
        return {'version':1,'status':'committed','action_id':proof['action_id'],
            'event_id':proof['event_id'],'payload_hash':proof['payload_hash'],'args_hash':proof['args_hash'],
            'operation':'add_identifier','canonical_id':args['canonical_id'],
            'identifier':args['identifier'],'added':added,'post_digest':digest(post)}
    path = preferences._path()
    with jsonstore.lock(path):
        data = jsonstore.read(path, default={}, strict=True)
        records = data.get('owner_preference_receipts', {})
        if type(records) is not dict:
            raise EditError('identifier receipt unavailable')
        saved = records.get(proof['event_id'])
        if saved is not None:
            if type(saved) is not dict or saved.get('proof') != proof or saved.get('tag') != 'person_identifier_add':
                raise OwnerLoopToolRefusalError('event_conflict')
            if saved.get('status') == 'contacting' and set(saved) == {'tag','proof','status'}:
                preferences._sync_existing_preferences(path)
                return {'version':1,'status':'unknown'}
            if (set(saved) != {'tag','proof','status','post','receipt'} or saved.get('status') != 'committed'
                    or type(saved['post']) is not dict or set(saved['post']) != {'canonical_id','identifiers'}
                    or saved['post']['canonical_id'] != args['canonical_id']
                    or type(saved['post']['identifiers']) is not list
                    or any(type(i) is not str for i in saved['post']['identifiers'])
                    or type(saved['receipt']) is not dict or type(saved['receipt'].get('added')) is not bool
                    or not any(kg.normalize_identifier(i) == kg.normalize_identifier(args['identifier']) for i in saved['post']['identifiers'])
                    or saved['receipt'] != receipt(saved['post'],saved['receipt']['added'])):
                raise EditError('identifier receipt unavailable')
            preferences._sync_existing_preferences(path)
            return saved['receipt']
        if inspect:
            return None
        raise OwnerLoopToolRefusalError('correction_unavailable')

def op_admitted_identifier_reviewed(review, proposal, proof, *, now, inspect=False, authority):
    args = {k:review[k] for k in ('canonical_id','identifier','source_revision')}
    """Teach one existing person; uncertain original graph writes never repeat."""
    import glob
    import hashlib
    import json
    import jsonstore
    import preferences

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
            separators=(',', ':')).encode()).hexdigest()
    def current():
        value = authority()
        if value.get('complete') is not True or value.get('revision') != args['source_revision']:
            raise OwnerLoopToolRefusalError('source_unavailable')
    def select():
        matches = []
        for candidate in sorted(glob.glob(os.path.join(kg.people_dir(), '*.md'))):
            if os.path.islink(candidate):
                raise EditError('person target unavailable')
            with open(candidate, encoding='utf-8') as handle:
                person = kg.parse_person_file(handle.read())
            if person.canonical_id == args['canonical_id']:
                matches.append((candidate, person))
        if len(matches) != 1:
            raise OwnerLoopToolRefusalError('target_changed')
        return matches[0]
    def receipt(post, added):
        return {'version':1,'status':'committed','action_id':proof['action_id'],
            'event_id':proof['event_id'],'payload_hash':proof['payload_hash'],'args_hash':proof['args_hash'],
            'operation':'identifier','canonical_id':args['canonical_id'],
            'identifier':args['identifier'],'added':added,'review_hash':review['review_hash'],'post_digest':digest(post)}
    path = preferences._path()
    with jsonstore.lock(path):
        data = jsonstore.read(path, default={}, strict=True)
        records = data.get('owner_preference_receipts', {})
        if type(records) is not dict:
            raise EditError('identifier receipt unavailable')
        saved = records.get(proof['event_id'])
        if saved is not None:
            if type(saved) is not dict or saved.get('proof') != proof or saved.get('review') != review or saved.get('proposal') != proposal or saved.get('tag') != 'person_identifier_reviewed':
                raise OwnerLoopToolRefusalError('event_conflict')
            if saved.get('status') == 'contacting' and set(saved) == {'tag','proof','status','review','proposal'}:
                preferences._sync_existing_preferences(path)
                return {'version':1,'status':'unknown'}
            if (set(saved) != {'tag','proof','status','review','proposal','post','receipt'} or saved.get('status') != 'committed'
                    or type(saved['post']) is not dict or set(saved['post']) != {'canonical_id','identifiers'}
                    or saved['post']['canonical_id'] != args['canonical_id']
                    or type(saved['post']['identifiers']) is not list
                    or any(type(i) is not str for i in saved['post']['identifiers'])
                    or type(saved['receipt']) is not dict or type(saved['receipt'].get('added')) is not bool
                    or saved['receipt']['added'] != (not review['present'])
                    or not any(kg.normalize_identifier(i) == kg.normalize_identifier(args['identifier']) for i in saved['post']['identifiers'])
                    or saved['receipt'] != receipt(saved['post'],saved['receipt']['added'])):
                raise EditError('identifier receipt unavailable')
            preferences._sync_existing_preferences(path)
            return saved['receipt']
        if inspect:
            return None
        if any(type(r) is dict and r.get('tag')=='person_identifier_reviewed' and r.get('proposal',{}).get('action_id')==proposal['action_id'] for r in records.values()):
            raise OwnerLoopToolRefusalError('target_unavailable')
        current()
        try:
            ident = _validated_identifier(args['identifier'])
            key = kg.normalize_identifier(ident)
            if not key:
                raise EditError('identifier normalizes to nothing')
        except EditError as error:
            raise OwnerLoopToolRefusalError('correction_unavailable') from error
        ku = _load_knowledge_update()
        with ku.graph_lock():
            candidate, person = select()
            duplicate = any(kg.normalize_identifier(str(i)) == key for i in person.identifiers)
            owner = None if duplicate else kg.find_person_file(identifier=ident)
            if owner and os.path.abspath(owner) != os.path.abspath(candidate):
                raise OwnerLoopToolRefusalError('identifier_collision')
            if identifier_review(args,authority=authority) != review:
                raise OwnerLoopToolRefusalError('target_changed')
            current()
            saved = {'tag':'person_identifier_reviewed','proof':proof,'review':review,'proposal':proposal,'status':'contacting'}
            records[proof['event_id']] = saved
            data['owner_preference_receipts'] = records
            preferences._durable_preferences_replace(path,data)
            try:
                result = op_add_identifier(os.path.splitext(os.path.basename(candidate))[0],args['identifier'],now)
                resulting_path, after = select()
                if (result.get('ok') is not True or type(result.get('added')) is not bool or result['added'] != (not review['present'])
                        or not any(kg.normalize_identifier(str(i)) == key for i in after.identifiers)):
                    raise EditError('identifier addition unconfirmed')
                preferences._sync_existing_preferences(resulting_path)
                current()
                post = {'canonical_id':after.canonical_id,'identifiers':[str(i) for i in after.identifiers]}
                saved = {**saved,'status':'committed','post':post,'receipt':receipt(post,result['added'])}
                records[proof['event_id']] = saved
                preferences._durable_preferences_replace(path,data)
                return saved['receipt']
            except Exception:
                return {'version':1,'status':'unknown'}


def _fact_add_person(canonical_id):
    import glob
    matches = []
    for path in sorted(glob.glob(os.path.join(kg.people_dir(), '*.md'))):
        if os.path.islink(path):
            raise EditError('person target unavailable')
        with open(path, encoding='utf-8') as handle:
            person = kg.parse_person_file(handle.read())
        if person.canonical_id == canonical_id:
            matches.append((path, person))
    if len(matches) != 1:
        raise OwnerLoopToolRefusalError('target_changed')
    return matches[0]


def fact_add_review(args, *, authority):
    """Review exact owner context against one existing canonical person identity."""
    current = authority()
    if current.get('complete') is not True or current.get('revision') != args['source_revision']:
        raise OwnerLoopToolRefusalError('source_unavailable')
    with _load_knowledge_update().graph_lock():
        _, person = _fact_add_person(args['canonical_id'])
        name = person.name
        if (not isinstance(name, str) or not name or name.strip() != name or len(name)>500
                or any(ord(c)<32 or 127<=ord(c)<=159 or 0xD800<=ord(c)<=0xDFFF or c in '\u2028\u2029' for c in name)):
            raise OwnerLoopToolRefusalError('target_changed')
        hint = next((str(i) for i in person.identifiers if str(i) and str(i).strip()==str(i) and len(str(i))<=200
            and not any(ord(c)<32 or 127<=ord(c)<=159 or 0xD800<=ord(c)<=0xDFFF or c in '\u2028\u2029' for c in str(i))),person.canonical_id)
        result = {'operation':'fact_add','canonical_id':person.canonical_id,'name':name,
            'text':args['text'],'source_revision':args['source_revision'],'identity_hint':hint,
            'person_version':_relation_digest({'canonical_id':person.canonical_id,'name':name,
                'identifiers':sorted(str(i) for i in person.identifiers)}),
            'present':any(f.status!='archived' and f.type=='context' and f.text==args['text'] for f in person.facts.values())}
        if not result['present'] and _similar_fact_exists(person, args['text'], 'context'):
            # Adding would bump an overlapping fact; changing a fact is the separate correction route.
            raise OwnerLoopToolRefusalError('similar_fact_exists')
        current = authority()
        if current.get('complete') is not True or current.get('revision') != args['source_revision']:
            raise OwnerLoopToolRefusalError('source_unavailable')
        return {**result,'review_hash':_relation_digest(result)}


def op_admitted_fact_add(review, proposal, proof, *, now, inspect=False, authority):
    """Use canonical op_add once; uncertain effects remain held for inspection."""
    import jsonstore
    import preferences
    args = {k:review[k] for k in ('canonical_id','text','source_revision')}
    def current():
        value = authority()
        if value.get('complete') is not True or value.get('revision') != args['source_revision']:
            raise OwnerLoopToolRefusalError('source_unavailable')
    def receipt(post):
        return {'version':1,'status':'committed','action_id':proof['action_id'],
            'event_id':proof['event_id'],'payload_hash':proof['payload_hash'],'args_hash':proof['args_hash'],
            'operation':'fact_add','canonical_id':args['canonical_id'],'fact_id':post['fact_id'],
            'added':not review['present'],'review_hash':review['review_hash'],'post_digest':_relation_digest(post)}
    path = preferences._path()
    with jsonstore.lock(path):
        data = jsonstore.read(path, default={}, strict=True)
        records = data.get('owner_preference_receipts', {})
        if type(records) is not dict:
            raise EditError('fact receipt unavailable')
        saved = records.get(proof['event_id'])
        if saved is not None:
            if (type(saved) is not dict or saved.get('proof')!=proof or saved.get('review')!=review
                    or saved.get('proposal')!=proposal or saved.get('tag')!='person_fact_add'):
                raise OwnerLoopToolRefusalError('event_conflict')
            if saved.get('status')=='contacting' and set(saved)=={'tag','proof','review','proposal','status'}:
                preferences._sync_existing_preferences(path)
                return {'version':1,'status':'unknown'}
            post = saved.get('post')
            if (set(saved)!={'tag','proof','review','proposal','status','post','receipt'} or saved.get('status')!='committed'
                    or type(post) is not dict or set(post)!={'canonical_id','fact_id','fact'}
                    or post['canonical_id']!=args['canonical_id'] or type(post['fact_id']) is not str or not FACT_ID_RE.fullmatch(post['fact_id'])
                    or type(post['fact']) is not dict or post['fact'].get('text')!=args['text']
                    or post['fact'].get('type')!='context' or post['fact'].get('status')=='archived'
                    or saved['receipt']!=receipt(post)):
                raise EditError('fact receipt unavailable')
            preferences._sync_existing_preferences(path)
            return saved['receipt']
        if inspect:
            return None
        if any(type(r) is dict and r.get('tag')=='person_fact_add' and r.get('proposal',{}).get('action_id')==proposal['action_id'] for r in records.values()):
            raise OwnerLoopToolRefusalError('target_unavailable')
        current()
        with _load_knowledge_update().graph_lock():
            candidate, _ = _fact_add_person(args['canonical_id'])
            if fact_add_review(args,authority=authority)!=review:
                raise OwnerLoopToolRefusalError('target_changed')
            current()
            saved = {'tag':'person_fact_add','proof':proof,'review':review,'proposal':proposal,'status':'contacting'}
            records[proof['event_id']] = saved
            data['owner_preference_receipts'] = records
            preferences._durable_preferences_replace(path,data)
            try:
                if not review['present']:
                    op_add(os.path.splitext(os.path.basename(candidate))[0],args['text'],'context',now,maintain_identities=False,exact_new=True)
                resulting_path, after = _fact_add_person(args['canonical_id'])
                matches = sorted((fid,f) for fid,f in after.facts.items() if f.status!='archived' and f.type=='context' and f.text==args['text'])
                if not matches:
                    raise EditError('fact addition unconfirmed')
                preferences._sync_existing_preferences(resulting_path)
                current()
                fid, fact = matches[0]
                post = {'canonical_id':after.canonical_id,'fact_id':fid,'fact':fact.to_yaml_dict()}
                saved = {**saved,'status':'committed','post':post,'receipt':receipt(post)}
                records[proof['event_id']] = saved
                preferences._durable_preferences_replace(path,data)
                return saved['receipt']
            except Exception:
                return {'version':1,'status':'unknown'}


def op_admitted_fact_correct(args, proof, *, now, inspect=False, authority):
    """One original correction; uncertainty in its two writes never permits reapplication."""
    import glob
    import hashlib
    import json
    import jsonstore
    import preferences

    path = preferences._path()
    def reject(code):
        raise OwnerLoopToolRefusalError(code)
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
            separators=(',', ':')).encode()).hexdigest()
    def select():
        matches = []
        for candidate in sorted(glob.glob(os.path.join(kg.people_dir(), '*.md'))):
            if os.path.islink(candidate):
                raise EditError('person target unavailable')
            with open(candidate, encoding='utf-8') as handle:
                person = kg.parse_person_file(handle.read())
            if person.canonical_id == args['canonical_id']:
                matches.append((candidate, person))
        if len(matches) != 1:
            reject('target_changed')
        return matches[0]
    def original(data):
        records = data.get('owner_preference_receipts', {})
        if type(records) is not dict:
            raise EditError('fact receipt unavailable')
        saved = records.get(proof['event_id'])
        if saved is not None:
            if type(saved) is not dict or saved.get('proof') != proof or saved.get('tag') != 'person_fact_correct':
                reject('event_conflict')
            if (type(saved.get('target')) is not dict
                    or digest([args['canonical_id'],args['fact_id'],saved['target']]) != args['fact_version']):
                raise EditError('fact receipt target unavailable')
            if saved.get('status') == 'contacting' and set(saved) == {'tag','proof','status','target'}:
                return records, saved
            if (saved.get('status') != 'committed' or set(saved) != {'tag','proof','status','target','post','receipt'}
                    or type(saved['post']) is not dict):
                raise EditError('fact receipt unavailable')
            post = saved['post']
            correction.validate_post(post, args, saved['target'], fact_id_re=FACT_ID_RE, error=EditError)
            expected = receipt(post)
            if saved['receipt'] != expected:
                raise EditError('fact receipt unavailable')
        return records, saved
    def receipt(post):
        return {'version':1,'status':'committed','action_id':proof['action_id'],
            'event_id':proof['event_id'],'payload_hash':proof['payload_hash'],'args_hash':proof['args_hash'],
            'operation':'fact_correct','canonical_id':args['canonical_id'],'fact_id':args['fact_id'],
            'replacement_fact_id':post['replacement_fact_id'],'post_digest':digest(post)}
    def current():
        value = authority()
        if value.get('complete') is not True or value.get('revision') != args['source_revision']:
            reject('source_unavailable')
    with jsonstore.lock(path):
        data = jsonstore.read(path, default={}, strict=True)
        records, saved = original(data)
        if saved is not None:
            preferences._sync_existing_preferences(path)
            return saved.get('receipt') if saved['status'] == 'committed' else {'version':1,'status':'unknown'}
        if inspect:
            return None
        current()
        ku = _load_knowledge_update()
        with ku.graph_lock():
            candidate, person = select()
            target = person.facts.get(args['fact_id'])
            if (target is None or target.status == 'archived'
                    or kg.person_fact_version(person.canonical_id,args['fact_id'],target) != args['fact_version']):
                reject('target_changed')
            current()
            saved = {'tag':'person_fact_correct','proof':proof,'status':'contacting','target':target.to_yaml_dict()}
            records[proof['event_id']] = saved
            data['owner_preference_receipts'] = records
            preferences._durable_preferences_replace(path,data)
            try:
                op_correct(os.path.splitext(os.path.basename(candidate))[0],args['fact_id'],args['text'],now,
                    **{field:args[field] for field in ('title','company') if field in args})
                resulting_path, after = select()
                old = after.facts.get(args['fact_id'])
                replacements = sorted((fid,fact) for fid,fact in after.facts.items()
                    if fact.status != 'archived' and fact.text == args['text'])
                if old is None or old.status != 'archived' or old.archived_reason != 'user_corrected' or not replacements:
                    raise EditError('fact correction unconfirmed')
                # The original graph writer atomically replaces without fsync. Establish
                # graph durability before the separate durable committed receipt.
                preferences._sync_existing_preferences(resulting_path)
                current()
                fid, fact = replacements[0]
                post = {'canonical_id':after.canonical_id,'fact_id':args['fact_id'],
                    'old_fact':old.to_yaml_dict(),'replacement_fact_id':fid,'replacement_fact':fact.to_yaml_dict()}
                post.update(correction.identity_post(after, args, fid, SOURCE, EditError))
                saved = {**saved,'status':'committed','post':post,'receipt':receipt(post)}
                records[proof['event_id']] = saved
                preferences._durable_preferences_replace(path,data)
                return saved['receipt']
            except Exception:
                return {'version':1,'status':'unknown'}


def op_admitted_loop_snooze(args, proof, *, now, inspect=False, authority):
    """Original one-row snooze; retained uncertain receipts never repeat the write."""
    from datetime import timedelta
    import jsonstore
    import preferences
    import retune_apply
    from delivery_effects import loop_version, loop_identity
    from ledger_provenance import source_loop_sources
    cr = _load_continuity()
    path = preferences._path()
    today = now.date().isoformat()
    until = None

    def guard(sources):
        current = authority()
        permitted = set(current.get('bridge_sources', []) + current.get('google_sources', [])
                        + current.get('connector_sources', []))
        if (current.get('complete') is not True or current.get('revision') != args['source_revision']
                or not set(sources) <= permitted):
            raise OwnerLoopToolRefusalError('source_unavailable')

    def receipt(post):
        return {'version':1,'status':'committed', **{key:proof[key] for key in
            ('action_id','event_id','payload_hash','args_hash')}, 'operation':'snooze',
            'anchor_key':args['anchor'],'post_digest':digest_frontmatter(post),
            'loop_status':post['status'],'snoozed_until':until}

    with jsonstore.lock(path):
        data = jsonstore.read(path, default={}, strict=True)
        records = data.setdefault('owner_preference_receipts', {})
        if type(records) is not dict:
            raise EditError('snooze receipt unavailable')
        saved = records.get(proof['event_id'])
        if saved is not None:
            if type(saved) is not dict or saved.get('tag') != 'loop_snooze' or saved.get('proof') != proof:
                raise OwnerLoopToolRefusalError('event_conflict')
            today = saved.get('today')
            try:
                from datetime import date
                until = (date.fromisoformat(today) + timedelta(days=args['days'])).isoformat()
            except (TypeError, ValueError, OverflowError):
                raise EditError('snooze receipt date unavailable') from None
            target = saved.get('target')
            if (type(target) is not dict or saved.get('until') != until
                    or loop_version(target) != args['loop_version'] or loop_identity(target) != args['loop_identity']
                    or target.get('anchor_key') != args['anchor'] or target.get('status','open') not in cr.ACTIVE):
                raise EditError('snooze receipt unavailable')
            if saved.get('status') == 'contacting' and set(saved) == {'tag','proof','target','today','until','status'}:
                preferences._sync_existing_preferences(path)
                return {'version':1,'status':'unknown'}
            post = saved.get('post')
            expected = dict(target)
            retune_apply.snooze_row(expected, args['days'], today)
            def content(row):
                return {k:v for k,v in row.items() if k not in ('_path','content_provenance','proof_transition')}
            if (saved.get('status') != 'committed' or set(saved) != {'tag','proof','target','today','until','status','post','receipt'}
                    or type(post) is not dict or content(post) != content(expected)
                    or post.get('status') not in cr.ACTIVE or saved['receipt'] != receipt(post)):
                raise EditError('snooze receipt unavailable')
            preferences._sync_existing_preferences(path)
            return saved['receipt']
        if inspect:
            return None
        try:
            until = (now.date() + timedelta(days=args['days'])).isoformat()
        except (OverflowError, ValueError):
            raise OwnerLoopToolRefusalError('correction_unavailable') from None
        with cr._ledger_lock():
            targets = [row for row in _owner_close_rows(cr) if row.get('anchor_key') == args['anchor']]
            if len(targets) != 1 or targets[0].get('status','open') not in cr.ACTIVE:
                raise OwnerLoopToolRefusalError('target_unavailable')
            row = targets[0]
            try:
                sources = source_loop_sources(row)
            except ValueError:
                try:
                    kind, sources = classify_ledger_sources(row)
                except ValueError:
                    raise OwnerLoopToolRefusalError('target_unavailable') from None
                if kind != 'owner' or sources:
                    raise OwnerLoopToolRefusalError('target_unavailable')
            guard(sources)
            if loop_version(row) != args['loop_version'] or loop_identity(row) != args['loop_identity']:
                raise OwnerLoopToolRefusalError('target_changed')
            target = {k:v for k,v in row.items() if k != '_path'}
            saved = {'tag':'loop_snooze','proof':proof,'target':target,'today':today,'until':until,'status':'contacting'}
            records[proof['event_id']] = saved
            preferences._durable_preferences_replace(path, data)
            try:
                guard(sources)
                retune_apply.snooze_row(row, args['days'], today)
                cr._persist(row, loop_snooze={'days':args['days'],'today':today})
                found = [r for r in _owner_close_rows(cr) if r.get('anchor_key') == args['anchor']]
                if len(found) != 1 or found[0].get('snoozed_until') != until or found[0].get('status') not in cr.ACTIVE:
                    raise EditError('snooze readback unavailable')
                post = {k:v for k,v in found[0].items() if k != '_path'}
                # Full original source/owner provenance must remain valid after the sanctioned write.
                if row.get('content_provenance') is not None:
                    classify_ledger_sources(post)
                cr._fsync_ledger_directory_tree()
                guard(sources)
                saved.update(status='committed', post=post, receipt=receipt(post))
                preferences._durable_preferences_replace(path,data)
                return saved['receipt']
            except Exception:
                return {'version':1,'status':'unknown'}


def op_admitted_loop_keep(args, proof, *, now, inspect=False, authority):
    """Original one-row keep; retained uncertain receipts never repeat the write."""
    import jsonstore
    import preferences
    import retune_apply
    from delivery_effects import loop_version, loop_identity
    from ledger_provenance import source_loop_sources
    cr = _load_continuity()
    path = preferences._path()
    today = now.date().isoformat()

    def guard(sources):
        current = authority()
        permitted = set(current.get('bridge_sources', []) + current.get('google_sources', [])
                        + current.get('connector_sources', []))
        if (current.get('complete') is not True or current.get('revision') != args['source_revision']
                or not set(sources) <= permitted):
            raise OwnerLoopToolRefusalError('source_unavailable')

    def receipt(post):
        return {'version':1,'status':'committed', **{key:proof[key] for key in
            ('action_id','event_id','payload_hash','args_hash')}, 'operation':'keep',
            'anchor_key':args['anchor'],'post_digest':digest_frontmatter(post),
            'loop_status':post['status']}

    with jsonstore.lock(path):
        data = jsonstore.read(path, default={}, strict=True)
        records = data.setdefault('owner_preference_receipts', {})
        if type(records) is not dict:
            raise EditError('keep receipt unavailable')
        saved = records.get(proof['event_id'])
        if saved is not None:
            if type(saved) is not dict or saved.get('tag') != 'loop_keep' or saved.get('proof') != proof:
                raise OwnerLoopToolRefusalError('event_conflict')
            today = saved.get('today')
            from datetime import date
            try:
                date.fromisoformat(today)
            except (TypeError, ValueError):
                raise EditError('keep receipt date unavailable') from None
            target = saved.get('target')
            if (type(target) is not dict
                    or loop_version(target) != args['loop_version'] or loop_identity(target) != args['loop_identity']
                    or target.get('anchor_key') != args['anchor'] or target.get('status','open') not in (cr.ACTIVE | cr.PARKED)):
                raise EditError('keep receipt unavailable')
            if saved.get('status') == 'contacting' and set(saved) == {'tag','proof','target','today','status'}:
                preferences._sync_existing_preferences(path)
                return {'version':1,'status':'unknown'}
            post = saved.get('post')
            expected = dict(target)
            retune_apply.keep_row(expected, today)
            def content(row):
                return {k:v for k,v in row.items() if k not in ('_path','content_provenance','proof_transition')}
            if (saved.get('status') != 'committed' or set(saved) != {'tag','proof','target','today','status','post','receipt'}
                    or type(post) is not dict or content(post) != content(expected)
                    or post.get('status') not in (cr.ACTIVE | cr.PARKED) or saved['receipt'] != receipt(post)):
                raise EditError('keep receipt unavailable')
            preferences._sync_existing_preferences(path)
            return saved['receipt']
        if inspect:
            return None
        with cr._ledger_lock():
            targets = [row for row in _owner_close_rows(cr) if row.get('anchor_key') == args['anchor']]
            if len(targets) != 1 or targets[0].get('status','open') not in (cr.ACTIVE | cr.PARKED):
                raise OwnerLoopToolRefusalError('target_unavailable')
            row = targets[0]
            try:
                sources = source_loop_sources(row)
            except ValueError:
                try:
                    kind, sources = classify_ledger_sources(row)
                except ValueError:
                    raise OwnerLoopToolRefusalError('target_unavailable') from None
                if kind != 'owner' or sources:
                    raise OwnerLoopToolRefusalError('target_unavailable')
            guard(sources)
            if loop_version(row) != args['loop_version'] or loop_identity(row) != args['loop_identity']:
                raise OwnerLoopToolRefusalError('target_changed')
            target = {k:v for k,v in row.items() if k != '_path'}
            saved = {'tag':'loop_keep','proof':proof,'target':target,'today':today,'status':'contacting'}
            records[proof['event_id']] = saved
            preferences._durable_preferences_replace(path, data)
            try:
                guard(sources)
                retune_apply.keep_row(row, today)
                cr._persist(row, loop_keep={'today':today})
                found = [r for r in _owner_close_rows(cr) if r.get('anchor_key') == args['anchor']]
                if len(found) != 1 or found[0].get('status') not in (cr.ACTIVE | cr.PARKED):
                    raise EditError('keep readback unavailable')
                post = {k:v for k,v in found[0].items() if k != '_path'}
                expected = dict(target)
                retune_apply.keep_row(expected, today)
                ignored = {'_path','content_provenance','proof_transition'}
                if ({k:v for k,v in post.items() if k not in ignored}
                        != {k:v for k,v in expected.items() if k not in ignored}):
                    raise EditError('keep readback mismatch')
                # Full original source/owner provenance must remain valid after the sanctioned write.
                if row.get('content_provenance') is not None:
                    classify_ledger_sources(post)
                cr._fsync_ledger_directory_tree()
                guard(sources)
                saved.update(status='committed', post=post, receipt=receipt(post))
                preferences._durable_preferences_replace(path,data)
                return saved['receipt']
            except Exception:
                return {'version':1,'status':'unknown'}


_CLEANUP_ROW_KEYS = {'index','anchor_key','name','what','direction','age_days','times_surfaced',
    'surface_count_provenance','overdue','deadline','chased_count','chased_out','suggestion',
    'loop_version','loop_identity'}
_CLEANUP_ORDINALS = ('first second third fourth fifth sixth seventh eighth ninth tenth eleventh '
    'twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth').split()


def _cleanup_hash(value):
    import hashlib
    import json
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def _cleanup_escape(value):
    return ''.join(('\\u%04x' % ord(c)) if ord(c)<32 or 127<=ord(c)<=159 or ord(c) in (0x2028,0x2029)
        else c for c in value)


def render_cleanup_list(value):
    from retune_scan import REMINDER_HANDOFF
    if value.get('scope') == 'open':
        lines = ['Open tasks:', '']
        lines += [f"{r['index']}. {_cleanup_escape(r['name'])} — {_cleanup_escape(r['what'])}" +
                  (' (waiting on them)' if r['direction']=='waiting_on_them' else '') for r in value['rows']]
        if not value['rows']:
            lines.append('No tasks can be safely numbered in this list.' if value['remaining']
                else 'No open tasks in the sources I can currently read.')
        if value['remaining']:
            lines += ['', f"{value['remaining']} more tasks are not shown."]
        if value['rows']:
            lines += ['', 'Tell me which ones are done. You can use the numbers.']
        return '\n'.join(lines)
    lines = ['Stale loops in your currently permitted sources:', '']
    for row in value['rows']:
        reasons = []
        if row['overdue']:
            reasons.append('overdue')
        if row['age_days'] is not None:
            reasons.append(f"{row['age_days']} days old")
        if row['times_surfaced']:
            reasons.append(f"surfaced {row['times_surfaced']} times")
        if row['chased_out']:
            # Retained reviews bind exact historical bytes. New scans use truthful reminder copy;
            # preserving the old vector keeps an already accepted proposal recoverable.
            reasons.append('reminded you twice; automatic reminders have stopped'
                if row['suggestion'] == REMINDER_HANDOFF
                else 'chased twice; automatic nudging has stopped')
        lines.extend([f"{row['index']}. {_cleanup_escape(row['name'])} — {_cleanup_escape(row['what'])}",
            '   '+('You owe' if row['direction']=='you_owe' else 'Waiting on them')+' · '+('; '.join(reasons) or 'needs your decision'),
            '   '+_cleanup_escape(row['suggestion'])])
    if not value['rows']:
        lines.append('No stale loops can be safely numbered in this list.' if value['remaining']
            else 'No stale loops were found in this permitted view.')
    if value['remaining']:
        lines.extend(['',f"{value['remaining']} more stale loops are not shown in this list."])
    if value['rows']:
        lines.extend(['','Tell me which numbers to dismiss, snooze, or keep. For example: dismiss 1; snooze 2 for 7 days; keep 3. Nothing has changed yet.'])
    return '\n'.join(lines)


def validate_cleanup_list(value):
    from datetime import date
    from retune_scan import LEGACY_CHASE_HANDOFF, REMINDER_HANDOFF
    if (type(value) is not dict or set(value)-{'scope','anchor_keys'}!={'operation','source_revision','rows','total','remaining','review_hash'} or ('scope' in value and value['scope']!='open')
            or value['operation']!='cleanup_list' or type(value['rows']) is not list or len(value['rows'])>50
            or any(type(value[k]) is not str or re.fullmatch(r'[a-f0-9]{64}',value[k]) is None for k in ('source_revision','review_hash'))
            or type(value['total']) is not int or not 0<=value['total']<=1024
            or type(value['remaining']) is not int or value['remaining']!=value['total']-len(value['rows']) or value['remaining']<0):
        raise ValueError('cleanup list invalid')
    if 'anchor_keys' in value:
        _validate_cleanup_anchor_keys(value['anchor_keys'])
        if value.get('scope')!='open' or value['anchor_keys']!=[row.get('anchor_key') for row in value['rows'] if type(row) is dict]:
            raise ValueError('cleanup subset binding invalid')
    anchors = set()
    for index,row in enumerate(value['rows'],1):
        if type(row) is not dict or set(row)!=_CLEANUP_ROW_KEYS or type(row['index']) is not int or row['index']!=index:
            raise ValueError('cleanup row invalid')
        for key,bound in (('anchor_key',256),('name',4096),('what',200)):
            if type(row[key]) is not str or len(row[key])>bound or any(0xD800<=ord(c)<=0xDFFF for c in row[key]):
                raise ValueError('cleanup row text invalid')
        if not row['anchor_key'] or row['anchor_key'] in anchors:
            raise ValueError('cleanup anchor invalid')
        anchors.add(row['anchor_key'])
        if any(type(row[k]) is not str or re.fullmatch(r'[a-f0-9]{64}',row[k]) is None for k in ('loop_version','loop_identity')):
            raise ValueError('cleanup row proof invalid')
        if (row['direction'] not in ('you_owe','waiting_on_them') or row['surface_count_provenance'] not in ('accepted_delivery','unknown_legacy')
                or row['suggestion'] not in ('do it or dismiss','nudge or drop',LEGACY_CHASE_HANDOFF,REMINDER_HANDOFF)
                or any(type(row[k]) is not bool for k in ('overdue','chased_out'))
                or any(type(row[k]) is not int or not 0<=row[k]<=1000000 for k in ('times_surfaced','chased_count'))
                or row['age_days'] is not None and (type(row['age_days']) is not int or not 0<=row['age_days']<=1000000)):
            raise ValueError('cleanup row evidence invalid')
        if row['deadline'] is not None:
            if type(row['deadline']) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}',row['deadline']) is None:
                raise ValueError('cleanup deadline invalid')
            date.fromisoformat(row['deadline'])
    if value['review_hash']!=_cleanup_hash({k:v for k,v in value.items() if k!='review_hash'}) or len(render_cleanup_list(value).encode('utf-16-le'))//2>16000:
        raise ValueError('cleanup list capacity or hash invalid')


def _validate_cleanup_anchor_keys(keys):
    if (type(keys) is not list or not 1<=len(keys)<=50
            or any(type(key) is not str or not key or len(key)>256 or key.strip()!=key
                or any(ord(c)<32 or 127<=ord(c)<=159 or 0xD800<=ord(c)<=0xDFFF for c in key) for key in keys)
            or len(set(keys))!=len(keys)):
        raise ValueError('cleanup subset invalid')


def cleanup_selection_items(review, selection, *, owner_text=None, completion_matches=None, derive_only=False):
    """Derive frozen indices; collective admission requires text and a completion matcher.

    Collective derive_only is for trusted saved-argument shape checks, not owner authority.
    The receiver must pass the actual admitted owner_text and its shared completion matcher.
    """
    validate_cleanup_list(review)
    if type(selection) is not dict or selection.get('mode') not in ('all_except', 'explicit', 'singleton', 'all'):
        raise ValueError('cleanup selector invalid')
    mode = selection['mode']
    if mode in ('singleton', 'all'):
        if derive_only:
            if owner_text is not None or completion_matches is not None:
                raise ValueError('shape-only cleanup derivation cannot validate owner authority')
        elif owner_text is None or completion_matches is None:
            raise ValueError(f'{mode} selector requires admitted owner text and completion matcher')
    if set(selection) != {'mode', 'correction', 'reference' if mode in ('singleton', 'all') else 'excluded' if mode == 'all_except' else 'clauses'}:
        raise ValueError('cleanup selector shape invalid')
    text = selection['correction']
    if type(text) is not str or not text or text != text.strip() or len(text) > 16000 or re.search(r'[\u0085\ufeff]', text):
        raise ValueError('cleanup selector text invalid')
    if owner_text is not None and (text != owner_text.strip() or completion_matches is None):
        raise ValueError('cleanup selector owner text invalid')

    def references(refs, claim):
        if type(refs) is not list or not 1 <= len(refs) <= 50:
            raise ValueError('cleanup selector references invalid')
        selected, spans = [], []
        for ref in refs:
            if (type(ref) is not dict or set(ref) != {'first', 'last', 'correction'}
                    or type(ref['first']) is not int or type(ref['last']) is not int
                    or not 1 <= ref['first'] <= ref['last'] <= len(review['rows']) or type(ref['correction']) is not str):
                raise ValueError('cleanup selector reference invalid')
            token = re.fullmatch(r'#?([1-9][0-9]*)(?:[-–]#?([1-9][0-9]*))?', ref['correction'])
            if not token or int(token[1]) != ref['first'] or int(token[2] or token[1]) != ref['last']:
                raise ValueError('cleanup selector range invalid')
            found = list(re.finditer(r'(?<![\w#:/.-])' + re.escape(ref['correction']) + r'(?![\w:/-]|\.[0-9])', claim))
            if len(found) != 1:
                raise ValueError('cleanup selector span unavailable')
            spans.append(found[0].span())
            for index in range(ref['first'], ref['last'] + 1):
                if index in selected:
                    raise ValueError('cleanup selector overlap')
                selected.append(index)
        if not spans:
            raise ValueError('cleanup selector references invalid')
        if any(a < d and c < b for i, (a, b) in enumerate(spans) for j, (c, d) in enumerate(spans) if i != j):
            raise ValueError('cleanup selector overlapping spans')
        residual = claim
        for start, end in sorted(spans, reverse=True):
            residual = residual[:start] + ' ' + residual[end:]
        tail = claim[max(end for _start, end in spans):]
        return selected, residual, tail, residual[:len(residual)-len(tail)]

    if mode == 'all':
        reference = selection['reference']
        if not all_open_selection(review, reference):
            raise ValueError('cleanup selector requires complete displayed open list')
        if (not (explicit_displayed_reference(reference) or type(reference) is str and reference.lower() == 'all')
                or (completion_matches is not None and not completion_matches(text, [{'name': reference, 'correction': reference}]))):
            raise ValueError('cleanup selector explicit all completion unavailable')
        selected = [row['index'] for row in review['rows']]
    elif mode == 'singleton':
        if not complete_open_selection(review) or len(review['rows']) != 1:
            raise ValueError('cleanup selector requires one complete displayed task')
        reference = selection['reference']
        if (type(reference) is not str or not reference.isascii() or re.fullmatch(r'(?:it|(?:this|that)(?: one| task)?|the (?:remaining|last) (?:one|task))', reference, re.I) is None
                or (completion_matches is not None and not completion_matches(text, [{'name': reference, 'correction': reference}]))):
            raise ValueError('cleanup selector explicit singleton completion unavailable')
        selected = [review['rows'][0]['index']]
    elif mode == 'all_except':
        if not complete_open_selection(review):
            raise ValueError('cleanup selector requires complete open list')
        parts = re.split(r'(?<![\w])except(?![\w])', text, flags=re.I)
        if len(parts) != 2:
            raise ValueError('cleanup selector exception unavailable')
        claim = re.sub(r'[.!;]$', '', parts[0].strip()).strip()
        excluded, residual, tail, reference_residual = references(selection['excluded'], parts[1].strip())
        if re.fullmatch(r'(?:\s|,|&|\band\b)*[.!]?', residual, re.I) is None:
            # Remove only the contiguous exact exception references before checking
            # the remaining whole claim with the shared completion matcher.
            if re.fullmatch(r'all', claim, re.I) is None or re.fullmatch(r'(?:\s|,|&|\band\b)*', reference_residual, re.I) is None:
                raise ValueError('cleanup selector exception has other intent')
            if re.search(r'[0-9]', tail):
                raise ValueError('cleanup selector unreferenced numeric choice')
            claim += ' ' + tail.strip()
        all_tokens = list(re.finditer(r'(?<![^\W_])all(?![^\W_])', claim, re.I))
        if len(all_tokens) != 1 or (completion_matches is not None and not completion_matches(claim, [{'name': all_tokens[0][0], 'correction': all_tokens[0][0]}])):
            raise ValueError('cleanup selector completion unavailable')
        selected = [row['index'] for row in review['rows'] if row['index'] not in excluded]
    else:
        clauses = selection['clauses']
        if type(clauses) is not list or not 1 <= len(clauses) <= 50:
            raise ValueError('cleanup selector clauses invalid')
        selected, mentioned, offset = [], set(), 0
        for clause in clauses:
            if (type(clause) is not dict or set(clause) != {'action', 'correction', 'references'}
                    or clause['action'] not in ('resolve', 'leave_open') or type(clause['correction']) is not str or not clause['correction']):
                raise ValueError('cleanup selector clause invalid')
            start = text.find(clause['correction'], offset)
            if (start < 0 or text.find(clause['correction']) != text.rfind(clause['correction'])
                    or re.fullmatch(r'[\s.!;]*', text[offset:start]) is None or (offset == 0 and text[:start].strip())):
                raise ValueError('cleanup selector whole request unavailable')
            offset = start + len(clause['correction'])
            indices, residual, _tail, _reference_residual = references(clause['references'], clause['correction'])
            if mentioned.intersection(indices):
                raise ValueError('cleanup selector conflicting indices')
            mentioned.update(indices)
            if clause['action'] == 'resolve':
                if re.search(r'(?<![\w#:/.-])#?[1-9][0-9]*(?![\w:/-]|\.[0-9])', residual):
                    raise ValueError('cleanup selector unreferenced numeric choice')
                short_answer = short_numbered_completion(review, selection, clause, indices, residual)
                if not short_answer and completion_matches is not None and not completion_matches(clause['correction'], [{'name': ref['correction'], 'correction': ref['correction']} for ref in clause['references']]):
                    raise ValueError('cleanup selector completion clause unavailable')
                selected.extend(indices)
            elif re.fullmatch(r'(?:keep|leave)\s*(?:\s|,|&|\band\b)*open[.!]?', residual.strip(), re.I) is None:
                raise ValueError('cleanup selector leave-open clause invalid')
        if re.fullmatch(r'[\s.!;]*', text[offset:]) is None:
            raise ValueError('cleanup selector unconsumed text')
    if not selected:
        raise ValueError('cleanup selector empty completion')
    return [{'index': index, 'action': 'resolve', 'correction': text} for index in sorted(selected)]


def validate_cleanup_args(args, *, preview):
    if preview:
        if (type(args) is not dict or set(args)-{'scope','anchor_keys'}!={'operation','source_revision'} or ('scope' in args and args['scope']!='open') or args['operation']!='propose_cleanup'
                or type(args['source_revision']) is not str or re.fullmatch(r'[a-f0-9]{64}',args['source_revision']) is None):
            raise ValueError('cleanup proposal invalid')
        if 'anchor_keys' in args:
            _validate_cleanup_anchor_keys(args['anchor_keys'])
            if args.get('scope')!='open':
                raise ValueError('cleanup subset scope invalid')
        return
    if type(args) is not dict or set(args)-{'selection'}!={'operation','list','proposal','items'} or args['operation']!='cleanup':
        raise ValueError('cleanup arguments invalid')
    validate_cleanup_list(args['list'])
    proposal = args['proposal']
    if (type(proposal) is not dict or set(proposal)!={'event_id','action_id','args_hash','accepted_dispatch_id','source_revision'}
            or any(type(proposal[k]) is not str or re.fullmatch(r'[a-f0-9]{64}',proposal[k]) is None for k in ('event_id','action_id','args_hash','source_revision'))
            or type(proposal['accepted_dispatch_id']) is not str or not 1<=len(proposal['accepted_dispatch_id'])<=160
            or any(ord(c)<32 or 127<=ord(c)<=159 or ord(c) in (0x2028,0x2029) for c in proposal['accepted_dispatch_id'])
            or proposal['source_revision']!=args['list']['source_revision']):
        raise ValueError('cleanup approval invalid')
    if 'selection' in args:
        if args['items'] != cleanup_selection_items(args['list'], args['selection'], derive_only=True):
            raise ValueError('cleanup selector derived indices invalid')
        return
    if type(args['items']) is not list or not 1<=len(args['items'])<=50:
        raise ValueError('cleanup selection invalid')
    last = 0
    for item in args['items']:
        if (type(item) is not dict or item.get('action') not in ('dismiss','snooze','keep','resolve')
                or set(item)!=({'index','action','correction','days'} if item['action']=='snooze' else {'index','action','correction'})
                or type(item['index']) is not int or not last<item['index']<=len(args['list']['rows'])
                or type(item['correction']) is not str or not item['correction'] or len(item['correction'])>16000):
            raise ValueError('cleanup intent invalid')
        last = item['index']
        if item['action']=='snooze' and (type(item['days']) is not int or not 1<=item['days']<=3652058):
            raise ValueError('cleanup days invalid')
        if item['action']=='resolve':
            if not re.fullmatch(r'#?[1-9][0-9]*',item['correction']) or int(item['correction'].lstrip('#'))!=item['index']:
                raise ValueError('completion index invalid')
            continue
        ordinal = '|'.join(_CLEANUP_ORDINALS)
        pattern = r'(dismiss|keep|snooze)[ \t\r\n]+(?:the[ \t\r\n]+)?(?:item[ \t\r\n]+)?(#?[1-9][0-9]?|'+ordinal+r')'
        pattern += r'(?:[ \t\r\n]+(?:for[ \t\r\n]+)?([1-9][0-9]*)[ \t\r\n]+days?)?\.?'
        match = re.fullmatch(pattern,item['correction'],re.I)
        if not match:
            raise ValueError('cleanup explicit selection invalid')
        token = match[2].lower()
        index = _CLEANUP_ORDINALS.index(token)+1 if token in _CLEANUP_ORDINALS else int(token.lstrip('#'))
        if match[1].lower()!=item['action'] or index!=item['index'] or (int(match[3]) if match[3] else None)!=(item.get('days')):
            raise ValueError('cleanup selection binding invalid')


def _loop_contact_name(value):
    value=re.sub(r'\s+(?:(?:text|chat)\s+)?thread$', '', value.casefold())
    return ' '.join(re.findall(r'[^\W_]+',value))


def _explicit_loop_thread_selection(phrase):
    match=re.fullmatch(r"(.+?)[’']s\s+(.+)",phrase)
    return re.search(r'\s+(?:(?:text|chat)\s+)?thread$',match[1] if match else phrase,re.I) is not None


def _named_loop_selection(phrase):
    match=re.fullmatch(r"(.+?)[’']s\s+(.+)",phrase)
    def words(text):
        return [word[:-1] if len(word)>3 and word.endswith('s') else word for word in re.findall(r'[^\W_]+',text.casefold())]
    description=[word for word in words(match[2]) if word not in ('the','a','an','plan','task','loop')] if match else []
    return _loop_contact_name(match[1] if match else phrase), description, match is not None, words


def validate_resolve_many_args(args):
    if (type(args) is not dict or set(args)-{'allow_partial'}!={'operation','source_revision','selection_mode','items'} or ('allow_partial' in args and args['allow_partial'] is not True)
            or args['operation']!='resolve_many' or type(args['source_revision']) is not str
            or args['selection_mode'] not in ('named_people','owner_tasks')
            or re.fullmatch(r'[a-f0-9]{64}',args['source_revision']) is None
            or type(args['items']) is not list or not 1<=len(args['items'])<=20):
        raise ValueError('multi resolve arguments invalid')
    names,anchors=set(),set()
    for item in args['items']:
        if type(item) is not dict or set(item) not in ({'name','correction'},{'name','anchor_key','loop_version','loop_identity','correction'}):
            raise ValueError('multi resolve row invalid')
        for key,bound in (('name',500),('anchor_key',256),('correction',16000)):
            if key not in item:
                continue
            value=item[key]
            if type(value) is not str or not value or value!=value.strip() or len(value)>bound or any(ord(c)<32 or 127<=ord(c)<=159 or c in '\u2028\u2029' or 0xD800<=ord(c)<=0xDFFF for c in value):
                raise ValueError('multi resolve text invalid')
        if item['correction']!=item['name'] or item['name'].casefold() in names or item.get('anchor_key') in anchors:
            raise ValueError('multi resolve selection invalid')
        names.add(item['name'].casefold())
        if 'anchor_key' not in item:
            continue
        anchors.add(item['anchor_key'])
        if any(type(item[k]) is not str or re.fullmatch(r'[a-f0-9]{64}',item[k]) is None for k in ('loop_version','loop_identity')):
            raise ValueError('multi resolve proof invalid')


class _CleanupSourceUnavailableError(OwnerLoopToolRefusalError):
    def __init__(self, *, global_unavailable):
        super().__init__('source_unavailable')
        self.global_unavailable = global_unavailable


def _cleanup_authority(authority, revision, sources=()):
    current = authority()
    permitted = set(current.get('bridge_sources',[])+current.get('google_sources',[])+current.get('connector_sources',[]))
    if current.get('complete') is not True or current.get('revision')!=revision:
        raise _CleanupSourceUnavailableError(global_unavailable=True)
    if not set(sources)<=permitted:
        raise _CleanupSourceUnavailableError(global_unavailable=False)
    return sorted(permitted)


def cleanup_list(args, *, now, authority):
    from loops_query import permitted_rows
    import retune_scan
    import ledger_io
    from delivery_effects import loop_version, loop_identity
    cr = _load_continuity()
    permitted = _cleanup_authority(authority,args['source_revision'])
    with cr._ledger_lock():
        rows = permitted_rows(cr._data_root(),permitted)
        scanned = retune_scan._scan_rows(ledger_io.active_entries(rows,now),now,include_all=args.get('scope')=='open')['stale_loops']
        scanned = [r for r in scanned if r['chased_out']]+[r for r in scanned if not r['chased_out']]
        total = len(scanned)
        if 'anchor_keys' in args:
            # Selection supplies identity and order only; every displayed field comes from current canonical rows.
            selected = []
            for key in args['anchor_keys']:
                matches = [row for row in scanned if row['anchor_key']==key]
                if len(matches)!=1:
                    raise OwnerLoopToolRefusalError('target_unavailable')
                selected.append(matches[0])
            scanned = selected
        listed = []
        for row in scanned:
            if len(listed)>=(50 if args.get('scope')=='open' else 20):
                break
            matched = [r for r in rows if r.get('anchor_key')==row['anchor_key']]
            if len(matched)!=1:
                if 'anchor_keys' in args:
                    raise OwnerLoopToolRefusalError('target_unavailable')
                # Folded files retain anchors and filenames. Without an exact writable survivor
                # proof, withhold this anchor rather than guess or block independent tasks.
                continue
            listed.append({**row,'index':len(listed)+1,'loop_version':loop_version(matched[0]),'loop_identity':loop_identity(matched[0])})
        value = {'operation':'cleanup_list','source_revision':args['source_revision'],'rows':listed,
            'total':total,'remaining':total-len(listed)}
        if 'anchor_keys' in args:
            value['anchor_keys']=args['anchor_keys'][:]
        if args.get('scope')=='open':
            value['scope']='open'
            # Fit the accepted text limit without losing or renumbering shown rows.
            while 'anchor_keys' not in args and listed and len(render_cleanup_list(value).encode('utf-16-le'))//2>16000:
                listed.pop()
                value['remaining']=total-len(listed)
        value['review_hash'] = _cleanup_hash(value)
        try:
            validate_cleanup_list(value)
        except ValueError:
            raise OwnerLoopToolRefusalError('correction_unavailable') from None
        _cleanup_authority(authority,args['source_revision'])
        return value


def op_admitted_loop_cleanup(args, proof, *, now, inspect=False, authority):
    """One real owner event retains each original local edit without replaying uncertainty."""
    import jsonstore
    import preferences
    import retune_apply
    import retune_scan
    from datetime import datetime, timedelta, date, timezone
    from delivery_effects import loop_version, loop_identity
    from ledger_provenance import source_loop_sources, digest_frontmatter
    cr = _load_continuity()
    path = preferences._path()
    direct = args['operation']=='resolve_many'
    if direct:
        validate_resolve_many_args(args)
    revision = args['source_revision'] if direct else args['list']['source_revision']
    listed_rows = args['items'] if direct else args['list']['rows']
    partial_sources = bool(args.get('allow_partial')) if direct else all(item['action']=='resolve' for item in args['items'])
    current_rows = None
    ignored = {'_path','content_provenance','proof_transition'}

    def content(row):
        return {k:v for k,v in row.items() if k not in ignored}

    def transform(row, item, today, instant):
        expected = dict(row)
        if direct or item['action']=='resolve':
            cr._terminate(expected,'resolved','user_resolved',today,now=instant)
        elif item['action']=='dismiss':
            cr._terminate(expected,'dismissed','user_dismissed',today,now=instant)
        elif item['action']=='snooze':
            retune_apply.snooze_row(expected,item['days'],today)
        else:
            retune_apply.keep_row(expected,today)
        return expected

    def receipt(saved):
        outcomes = []
        for item,progress in zip(args['items'],saved['progress'],strict=True):
            listed = item if direct else listed_rows[item['index']-1]
            out = {'index':len(outcomes)+1 if direct else item['index'],'action':'resolve' if direct else item['action'],'anchor_key':listed.get('anchor_key',''),'status':progress['status']}
            if out['status']=='applied':
                post = progress['post']
                out.update(post_digest=digest_frontmatter(post),loop_status=post['status'])
                if not direct and item['action']=='snooze':
                    out['snoozed_until']=post['snoozed_until']
            elif out['status']=='refused':
                out['reason']=progress['reason']
            outcomes.append(out)
        return {'version':1,'status':'committed', **{k:proof[k] for k in ('action_id','event_id','payload_hash','args_hash')},
            'operation':'resolve_many' if direct else 'cleanup',
            **({} if direct else {'review_hash':args['list']['review_hash']}),
            'outcome':'complete' if all(i['status']=='applied' for i in outcomes) else 'partial','items':outcomes}

    def check_saved(saved):
        if (type(saved) is not dict or saved.get('tag')!='loop_cleanup' or saved.get('proof')!=proof
                or saved.get('args')!=args):
            raise OwnerLoopToolRefusalError('event_conflict')
        if set(saved)-{'tag','proof','args','today','instant','progress','receipt'} or not {'tag','proof','args','today','instant','progress'}<=set(saved):
            raise EditError('cleanup receipt unavailable')
        instant = datetime.fromisoformat(saved['instant'])
        if (instant.tzinfo is None or instant.utcoffset() is None or instant.date().isoformat()!=saved['today']
                or instant.astimezone(timezone.utc)!=datetime.fromtimestamp(proof['admitted_at_ms']/1000,timezone.utc)):
            raise EditError('cleanup receipt clock unavailable')
        date.fromisoformat(saved['today'])
        progress = saved['progress']
        if type(progress) is not list or len(progress)!=len(args['items']):
            raise EditError('cleanup progress unavailable')
        for item,entry in zip(args['items'],progress,strict=True):
            if type(entry) is not dict or entry.get('status') not in ('pending','contacting','applied','refused','unknown','skipped'):
                raise EditError('cleanup item unavailable')
            state=entry['status']
            allowed = {'status','target','sources','post'} if state=='applied' else {'status','reason'} if state=='refused' else {'status','target','sources'} if state in ('pending','contacting','unknown') else {'status'}
            if set(entry)!=allowed:
                raise EditError('cleanup item shape unavailable')
            if state=='refused' and entry['reason'] not in ('target_changed','target_unavailable','source_unavailable','correction_unavailable'):
                raise EditError('cleanup refusal unavailable')
            if 'target' in entry:
                target=entry['target']; listed=item if direct else listed_rows[item['index']-1]
                if (type(target) is not dict or target.get('anchor_key')!=listed['anchor_key']
                        or loop_version(target)!=listed['loop_version'] or loop_identity(target)!=listed['loop_identity']
                        or type(entry['sources']) is not list or entry['sources']!=sorted(set(entry['sources']))):
                    raise EditError('cleanup saved target unavailable')
            if state=='applied':
                post=entry['post']
                expected=transform(target,item,saved['today'],instant)
                if type(post) is not dict or content(post)!=content(expected):
                    raise EditError('cleanup post unavailable')
                if post.get('content_provenance') is not None:
                    classify_ledger_sources(post)
        if 'receipt' in saved:
            if any(p['status'] in ('pending','contacting') for p in progress) or saved['receipt']!=receipt(saved):
                raise EditError('cleanup aggregate unavailable')
        return instant

    def target_unavailable(stage):
        # Fixed branch labels only: never log the selector, row, proof or exception.
        try:
            from sotto_log import diag
            diag('[owner_loop_target_refused] ' + stage)
        except Exception:
            pass  # Diagnostics must not change the refusal or durable outcome.
        raise OwnerLoopToolRefusalError('target_unavailable') from None

    def target(item):
        listed=item if direct else listed_rows[item['index']-1]
        if 'anchor_key' not in listed:
            target_unavailable('unresolved-selection')
        rows=current_rows
        matches=[r for r in rows if r.get('anchor_key')==listed['anchor_key']]
        if len(matches)!=1 or matches[0].get('status','open') not in cr.ACTIVE:
            target_unavailable('anchor-selection')
        row=matches[0]
        if direct:
            def named(r):
                if args['selection_mode']=='owner_tasks':
                    return _owner_task_phrase_matches(r,item['name'])
                full=_loop_contact_name(str(r.get('contact_name') or ''))
                return full==_named_loop_selection(item['name'])[0] or (not _explicit_loop_thread_selection(item['name']) and full.split(' ')[0]==_named_loop_selection(item['name'])[0])
            same_name=[r for r in rows if r.get('status','open') in cr.ACTIVE
                and named(r)]
            if args['selection_mode']=='named_people':
                _,description,qualified,words=_named_loop_selection(item['name'])
                # This selects one obligation, not one merged person. Include hidden active
                # peers so another matching ask still makes the owner's description ambiguous.
                def described(candidate):
                    evidence=set(words(str(candidate.get('summary') or '')+' '+str(candidate.get('ask') or '')))
                    return not qualified or bool(description) and all(word in evidence for word in description)
                same_name=[r for r in same_name if described(r)]
            if len(same_name)!=1 or same_name[0].get('anchor_key')!=row.get('anchor_key') or not named(row):
                target_unavailable('description-selection')
        try:
            sources=sorted(source_loop_sources(row))
        except ValueError:
            try:
                kind,sources=classify_ledger_sources(row)
            except ValueError:
                target_unavailable('provenance-unavailable')
            if kind!='owner' or sources:
                target_unavailable('provenance-unavailable')
            sources=[]
        if direct and args['selection_mode']=='owner_tasks':
            kind,provenance_sources=classify_ledger_sources(row)
            if kind!='owner' or provenance_sources:
                target_unavailable('provenance-unavailable')
        _cleanup_authority(authority,revision,sources)
        if loop_version(row)!=listed['loop_version'] or loop_identity(row)!=listed['loop_identity']:
            raise OwnerLoopToolRefusalError('target_changed')
        if not direct:
            # Accepted age/surface evidence is historical; only current label/direction bind the chosen target.
            current=retune_scan._scan_rows([row],now,include_all=args['list'].get('scope')=='open')['stale_loops']
            if len(current)!=1 or any(current[0][k]!=listed[k] for k in ('name','what','direction')):
                raise OwnerLoopToolRefusalError('target_changed')
        if not direct and item['action']=='snooze':
            try:
                date.fromisoformat(today)+timedelta(days=item['days'])
            except (ValueError,OverflowError):
                raise OwnerLoopToolRefusalError('correction_unavailable') from None
        return row,sources

    with jsonstore.lock(path):
        data=jsonstore.read(path,default={},strict=True)
        records=data.setdefault('owner_preference_receipts',{})
        if type(records) is not dict:
            raise EditError('cleanup receipts unavailable')
        saved=records.get(proof['event_id'])
        if saved is not None:
            check_saved(saved)
            if 'receipt' not in saved:
                for entry in saved['progress']:
                    if entry['status']=='contacting':
                        entry['status']='unknown'
                    elif entry['status']=='pending':
                        entry.clear();entry['status']='skipped'
                saved['receipt']=receipt(saved)
                preferences._durable_preferences_replace(path,data)
            preferences._sync_existing_preferences(path)
            return saved['receipt']
        if inspect:
            return None
        if not direct and (args['proposal']['event_id']==proof['event_id'] or any(type(r) is dict and r.get('tag')=='loop_cleanup'
                and r.get('args',{}).get('proposal',{}).get('action_id')==args['proposal']['action_id'] for r in records.values())):
            raise OwnerLoopToolRefusalError('event_conflict')
        _cleanup_authority(authority,revision)
        today=now.date().isoformat()
        saved={'tag':'loop_cleanup','proof':proof,'args':args,'today':today,'instant':now.isoformat(),'progress':[]}
        with cr._ledger_lock():
            # Canonical writers share this lock. Keep one strict snapshot through gather and
            # the first effect; each existing post-write strict read refreshes it for the next.
            # No authority result is cached, and _persist still validates the actual prior file.
            current_rows=_owner_close_rows(cr)
            # Preserve the semantics of already-admitted older requests on recovery.
            if direct and not args.get('allow_partial'):
                for item in args['items']:
                    target(item)
            for item in args['items']:
                try:
                    row,sources=target(item)
                    saved['progress'].append({'status':'pending','target':{k:v for k,v in row.items() if k!='_path'},'sources':sources})
                except OwnerLoopToolRefusalError as error:
                    if error.code=='source_unavailable' and (not partial_sources or getattr(error,'global_unavailable',True)):
                        raise
                    saved['progress'].append({'status':'refused','reason':error.code})
            records[proof['event_id']]=saved
            preferences._durable_preferences_replace(path,data)
            stopped=False
            for item,entry in zip(args['items'],saved['progress'],strict=True):
                if entry['status']=='refused':
                    continue
                if stopped:
                    entry.clear();entry['status']='skipped'
                    continue
                try:
                    row,sources=target(item)
                except OwnerLoopToolRefusalError as error:
                    entry.clear();entry.update(status='refused',reason=error.code)
                    if error.code=='source_unavailable' and (not partial_sources or getattr(error,'global_unavailable',True)):
                        stopped=True
                    preferences._durable_preferences_replace(path,data)
                    continue
                entry['status']='contacting'
                preferences._durable_preferences_replace(path,data)
                effect_started=False
                try:
                    _cleanup_authority(authority,revision,sources)
                    expected=transform(row,item,today,now)
                    row.clear()
                    row.update(expected)
                    effect_started=True
                    if direct or item['action']=='resolve':
                        cr._persist(row,loop_resolve={'today':today,'now':now.isoformat()})
                    elif item['action']=='dismiss':
                        cr._persist(row,loop_dismiss={'today':today,'now':now.isoformat()})
                    elif item['action']=='snooze':
                        cr._persist(row,loop_snooze={'today':today,'days':item['days']})
                    else:
                        cr._persist(row,loop_keep={'today':today})
                    refreshed_rows=_owner_close_rows(cr)
                    found=[r for r in refreshed_rows if r.get('anchor_key')==row['anchor_key']]
                    if len(found)!=1 or content(found[0])!=content(expected):
                        raise EditError('cleanup readback unavailable')
                    current_rows=refreshed_rows
                    post={k:v for k,v in found[0].items() if k!='_path'}
                    if post.get('content_provenance') is not None:
                        classify_ledger_sources(post)
                    cr._fsync_ledger_directory_tree()
                    _cleanup_authority(authority,revision,sources)
                    entry.update(status='applied',post=post)
                    preferences._durable_preferences_replace(path,data)
                except Exception as error:
                    if isinstance(error,_CleanupSourceUnavailableError) and not effect_started:
                        entry.clear();entry.update(status='refused',reason='source_unavailable')
                        stopped=not partial_sources or error.global_unavailable
                        preferences._durable_preferences_replace(path,data)
                        continue
                    if entry['status']=='applied':
                        # A failed durable marker response must be read on inspection; never overwrite a possibly committed item.
                        return {'version':1,'status':'unknown'}
                    # Even a failed completion-marker write is uncertainty until its durable saved record is inspected.
                    entry.pop('post',None);entry['status']='unknown'
                    stopped=True
            saved['receipt']=receipt(saved)
            preferences._durable_preferences_replace(path,data)
            return saved['receipt']
