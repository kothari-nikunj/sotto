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

Fact ops are PEOPLE-ONLY: company files carry About/News/Context sections, not a facts map, and
knowledge_update's company path has no fact/correction lane — a company "fact" edit has no
chat-equivalent code path to ride. (knowledge/companies/<slug>.md is therefore not accepted here.)

`loop` writes the SAME terminal transition fields the deterministic resolver
(morning-brief/scripts/continuity_resolve.py `_terminate` + `_persist`) writes: status,
resolution ("user_resolved" | "user_dismissed"), resolved_at (user-local date), persisting the
full frontmatter back with yaml.safe_dump(sort_keys=False) — resolver-shaped bytes.

`merge` is the confirmation half of entity-dedup lite (knowledge_update.py's module docstring has
the whole design): the Learn step auto-merges only on an EXACT shared identifier and merely
*suggests* name-similarity merges — this op is how a human (chat or a dashboard card) confirms one.
It runs through knowledge_update.merge_person_files, i.e. the same kg.merge_person + os.remove
mechanics migrate_people_dir uses, and refuses outright when the two files carry conflicting
identifiers (two John Smiths with two different emails are two people, whatever their names say).

CLI (runs in the skills tree, where PyYAML + the libs live; invoked by the receiver's dashboard):
  knowledge_edit.py --slug <slug> --op correct --fact-id <id> --text "..."
  knowledge_edit.py --slug <slug> --op archive --fact-id <id>
  knowledge_edit.py --slug <slug> --op add --text "..." [--memory-type context]
  knowledge_edit.py --op loop --anchor <anchor_key> --to resolved|dismissed
  knowledge_edit.py --op merge --from <slug> --into <slug>

Output: one JSON object on stdout — {"ok": true, "slug", "fact_count"} for fact ops (merge adds
"merged_from"), {"ok": true, "anchor_key", "status"} for loop — or {"ok": false, "error"} + exit 2
on failure.
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
sys.path.insert(0, _HERE)  # ledger_io (which itself imports compose_brief from this dir)

import knowledge as kg  # noqa: E402

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
    """Import knowledge_update from morning-brief/scripts (path-loaded: the skills tree is not a
    package). Kept lazy so `--op loop` never pays for it."""
    import importlib.util
    path = os.path.join(_HERE, "..", "..", "morning-brief", "scripts", "knowledge_update.py")
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
                    "no facts map and have no correction lane).")
    ap.add_argument("--op", required=True, choices=("correct", "archive", "add", "loop", "merge"))
    ap.add_argument("--slug", default="", help="person slug (knowledge/people/<slug>.md)")
    ap.add_argument("--fact-id", default="", dest="fact_id")
    ap.add_argument("--text", default="")
    ap.add_argument("--memory-type", default="context", dest="memory_type")
    ap.add_argument("--anchor", default="", help="continuity anchor_key (loop op)")
    ap.add_argument("--to", default="", choices=("", "resolved", "dismissed"))
    ap.add_argument("--from", default="", dest="from_slug",
                    help="person slug that DISAPPEARS into --into (merge op)")
    ap.add_argument("--into", default="", dest="into_slug",
                    help="person slug that SURVIVES the merge (merge op)")
    a = ap.parse_args(argv)
    if a.op == "correct":
        return op_correct(a.slug, a.fact_id, a.text)
    if a.op == "archive":
        return op_archive(a.slug, a.fact_id)
    if a.op == "add":
        return op_add(a.slug, a.text, a.memory_type)
    if a.op == "merge":
        return op_merge(a.from_slug, a.into_slug)
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
