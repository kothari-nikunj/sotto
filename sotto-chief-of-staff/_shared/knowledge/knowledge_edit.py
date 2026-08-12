#!/usr/bin/env python3
"""
knowledge_edit.py — user-initiated edits from The Window dashboard (M2).

THE core principle (docs/plans/web-dashboard-the-window.md): a dashboard edit is indistinguishable
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
`source: user_added`: nothing auto-closes it, because nothing in your inbox corresponds to it — you
close it. Re-adding the same ask on the same day dedupes onto the same anchor (times_surfaced bumps)
rather than making a second file. `loop-deadline` writes the ONE field, which is also how a loop is
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
                                  [--deadline YYYY-MM-DD]
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
import json
import os
import re
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "lib"))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))   # ledger_io
sys.path.insert(0, _HERE)                                   # knowledge.py, its sibling

import knowledge as kg  # noqa: E402

DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
SLUG_RE = re.compile(r"\A[a-z0-9_-]{1,128}\Z")
FACT_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
MEMORY_TYPE_RE = re.compile(r"\A[a-z_]{1,32}\Z")
# Anchor keys carry colons and, in the name: form, spaces/@/dots — allow any printable char,
# reject control chars. Anchors are only ever COMPARED against ledger frontmatter, never joined
# into a filesystem path.
ANCHOR_RE = re.compile(r"\A[^\x00-\x1f\x7f]{1,256}\Z")
TEXT_MAX = 500
SOURCE = "user_edit"


class EditError(Exception):
    """A user-reportable failure — message goes out as {"ok": false, "error": ...}."""


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
        "facts": [{"fact": fact_text, "change_type": change_type, "confidence": 1.0,
                   "source": SOURCE, "memory_type": memory_type}],
    }]}


def op_correct(slug: str, fact_id: str, text: str, now: datetime | None = None) -> dict:
    text = _validated_text(text)
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
    changed = False
    tgt = p2.facts.get(fact_id)
    if tgt is not None and tgt.status != "archived":
        tgt.status = "archived"
        tgt.archived_text = tgt.text
        changed = True
    if not any(f.status != "archived" and f.text == text for f in p2.facts.values()):
        fid = kg.generate_fact_id(p2.canonical_id, text, today)
        p2.facts[fid] = kg.FactMeta(text=text, type=target.type, status="active", seen=1,
                                    conf=1.0, source=SOURCE, source_ref="",
                                    first=today, last=today)
        changed = True
    if changed:
        with open(path2, "w", encoding="utf-8") as f:
            f.write(kg.serialize_person_file(p2, now))
    return _result(path2, p2)


def op_archive(slug: str, fact_id: str, now: datetime | None = None) -> dict:
    if not FACT_ID_RE.match(fact_id or ""):
        raise EditError("invalid fact id")
    path, p = _read_person(slug)
    fact = p.facts.get(fact_id)
    if fact is None:
        raise EditError(f"fact not found: {fact_id}")
    if fact.status != "archived":       # already archived → idempotent no-op rewrite-free
        fact.status = "archived"
        fact.archived_text = fact.text  # mirror SUPERSEDE's history-keeping
        with open(path, "w", encoding="utf-8") as f:
            f.write(kg.serialize_person_file(p, now))
    return _result(path, p)


def op_add(slug: str, text: str, memory_type: str = "context",
           now: datetime | None = None) -> dict:
    text = _validated_text(text)
    memory_type = (memory_type or "context").strip()
    if not MEMORY_TYPE_RE.match(memory_type):
        raise EditError("invalid memory type")
    _, p = _read_person(slug)
    ku = _load_knowledge_update()
    ku.apply(_chat_payload(p, text, "new", memory_type), now)
    return _result(*_resolve_after_apply(p))


MAX_IDENTIFIER_CHARS = 200


def _validated_identifier(identifier: str) -> str:
    """One email address or one phone number, and nothing else. Rejected up front rather than
    stored and puzzled over later: a name is not an identifier, and neither is a sentence."""
    ident = (identifier or "").strip()
    if not ident or len(ident) > MAX_IDENTIFIER_CHARS:
        raise EditError("identifier must be a non-empty email address or phone number")
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
    knowledge_update.drop_merge_suggestion — the SAME call op_merge makes after a confirmed merge,
    so confirming and dismissing leave the suggestions file in the same shape by the same code."""
    if not SLUG_RE.match(from_slug or "") or not SLUG_RE.match(into_slug or ""):
        raise EditError("invalid slug")
    ku = _load_knowledge_update()
    ku.drop_merge_suggestion(from_slug, into_slug, now)
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


def op_loop_add(text: str, contact: str = "", identifier: str = "", deadline: str = "",
                now: datetime | None = None) -> dict:
    """Add ONE loop by hand, through continuity_resolve's own write path (see module docstring).
    Anchored on a content hash of contact+text+day, so re-adding the same ask today bumps
    times_surfaced instead of forking a second file; an entry the user already closed is never
    resurrected (same rule apply_commitments applies)."""
    import hashlib
    text = _validated_text(text)
    contact = (contact or "").strip()[:120]
    identifier = (identifier or "").strip()[:200]
    deadline = _validated_deadline(deadline)
    cr = _load_continuity()
    when = now or cr._now_local(cr.configured_tz() or "+00:00")
    today = when.strftime("%Y-%m-%d")
    h = hashlib.sha256(f"{contact}|{text}|{today}".encode()).hexdigest()[:12]
    raw = {
        "action_type": "follow_up",
        # A hand-added loop belongs to no inbox: `manual` means the deterministic resolver has
        # nothing to match it against, so it stays open until the user closes it.
        "channel": "manual",
        "contact_name": contact,
        "contact_identifier": identifier or None,
        "source_thread_id": f"manual:{h}",
        "summary": text,
        "deadline": deadline or None,
        "created_at": today,
    }
    a = cr._normalize_action(raw)
    ak = cr.compute_anchor_key(a)
    items = cr._load_items()
    existing = items.get(ak)
    if existing is not None:
        if str(existing.get("status", "open")) in cr.TERMINAL:
            raise EditError("that loop was already closed — reopening isn't a thing; add a new one")
        existing["times_surfaced"] = int(existing.get("times_surfaced", 1) or 1) + 1
        if deadline:
            existing["deadline"] = deadline
        cr._persist(existing)
        return {"ok": True, "anchor_key": ak, "created": False}
    it = {
        "anchor_key": ak, "action_type": a.get("action_type"), "channel": a.get("channel"),
        "contact_name": a.get("contact_name"), "contact_identifier": a.get("contact_identifier"),
        "canonical_id": a.get("canonical_id"), "status": "open",
        "created_at": today, "times_surfaced": 1,
        "summary": a.get("summary", ""), "ask": a.get("ask"),
        "meeting_time": a.get("meeting_time"), "deadline": a.get("deadline"),
        "source_thread_id": a.get("source_thread_id"),
        "source": "user_added",
    }
    cr._persist(it)
    return {"ok": True, "anchor_key": ak, "created": True}


def op_loop_deadline(anchor: str, deadline: str) -> dict:
    """Set (or clear) ONE loop's deadline — which is also how a loop is snoozed: the resolver
    expires a loop 2 days past its deadline, so a later date IS "not yet". Persists the full
    frontmatter back through the same yaml.safe_dump(sort_keys=False) shape op_loop uses."""
    if not ANCHOR_RE.match(anchor or ""):
        raise EditError("invalid anchor key")
    deadline = _validated_deadline(deadline)
    import ledger_io  # lazy: pulls in compose_brief
    import yaml
    entry = next((fm for fm in ledger_io.load_entries(with_path=True)
                  if str(fm.get("anchor_key") or "") == anchor), None)
    if entry is None:
        raise EditError(f"loop not found: {anchor}")
    path = entry.pop("_path")
    if deadline:
        entry["deadline"] = deadline
    else:
        entry.pop("deadline", None)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{yaml.safe_dump(entry, sort_keys=False, allow_unicode=True)}---\n")
    return {"ok": True, "anchor_key": anchor, "deadline": deadline}


def op_loop(anchor: str, to: str, today: str | None = None) -> dict:
    if not ANCHOR_RE.match(anchor or ""):
        raise EditError("invalid anchor key")
    if to not in ("resolved", "dismissed"):
        raise EditError("invalid target status")
    import ledger_io  # lazy: pulls in compose_brief
    import yaml
    entry = next((fm for fm in ledger_io.load_entries(with_path=True)
                  if str(fm.get("anchor_key") or "") == anchor), None)
    if entry is None:
        raise EditError(f"loop not found: {anchor}")
    if today is None:
        import timeutil
        today = timeutil._user_local_date(timeutil.configured_tz() or "+00:00")
    path = entry.pop("_path")
    # The resolver's _terminate + _persist field semantics, with the user as the resolution source.
    entry["status"] = to
    entry["resolution"] = f"user_{to}"          # user_resolved | user_dismissed
    entry["resolved_at"] = today
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{yaml.safe_dump(entry, sort_keys=False, allow_unicode=True)}---\n")
    return {"ok": True, "anchor_key": anchor, "status": to}


def run(argv: list) -> dict:
    ap = argparse.ArgumentParser(
        description="Apply a user edit from The Window dashboard through the chat-equivalent "
                    "knowledge/continuity code paths. Fact ops are PEOPLE-ONLY (companies carry "
                    "no facts map); a company record is corrected with --op company-about.")
    ap.add_argument("--op", required=True,
                    choices=("correct", "archive", "add", "add-identifier", "company-about",
                             "loop", "loop-add", "loop-deadline", "merge", "merge-dismiss",
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
        return op_loop_add(a.text, a.contact, a.identifier, a.deadline)
    if a.op == "loop-deadline":
        return op_loop_deadline(a.anchor, a.deadline)
    return op_loop(a.anchor, a.to)


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
