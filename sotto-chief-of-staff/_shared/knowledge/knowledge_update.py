#!/usr/bin/env python3
"""
knowledge_update.py — apply a brief's extracted_knowledge to the people/company graph.

PORT SOURCE: app/src-tauri/src/database/knowledge_files.rs::save_knowledge_extraction (line 2453)
Run by learn_step.py (the brief's learning phase) after compose_brief. Reads/writes knowledge/*.md on $SOTTO_DATA.

Usage:
    knowledge_update.py < extracted.json          # {"person_updates":[...], "company_updates":[...]}
    knowledge_update.py extracted.json
Prints a JSON diff: {"applied":{"new","confirmed","superseded","pruned"}, "person_files":[...], ...}

Entity-dedup lite (Editor Step 2 item 6) rides this same Learn step, in three deliberately
asymmetric pieces — because the two dupe shapes have opposite risk profiles:

  1. EXACT-IDENTIFIER AUTO-MERGE (deterministic, no LLM, no confirmation). Two person FILES that
     list the same normalized identifier (email/phone) are the same human — an identifier is not a
     coincidence. Merged with the existing, tested merge_person + os.remove, exactly
     migrate_people_dir's mechanics, right where migration already runs. Logged one line.
  2. NAME-SIMILARITY IS SUGGEST-ONLY, forever. Slug containment ("ben" ⊂ "ben-butler") or a high
     name-similarity ratio with NON-conflicting identifiers appends to
     knowledge/merge_suggestions.json (deduped, capped) for a human to confirm via
     `knowledge_edit.py --op merge`. Names never auto-merge: name-slug keying is the ORIGINAL
     identity bug (two different John Smiths in one file), and conflicting identifiers — two
     emails, or two phones, with nothing in common — suppress the suggestion entirely. A pair the
     user dismissed is tombstoned in the same file's `dismissed` list and never suggested again.
  3. COMPANIES GET PREVENTION, NOT REPAIR. Company files are keyed by name-slug with aliases and a
     domain stored but never consulted, so "YC" and "Y Combinator" became two files. Resolution now
     consults both (alias-slug and domain) before minting a new file, every company_name seen is
     recorded as an alias, and compose_brief feeds the known company names back into the extraction
     prompt so the model stops free-forming the name in the first place.

COMPANY RESEARCH lands here too, through the SAME `company_updates` lane — there is no second
company writer. `about` is the company's durable identity paragraph (what it builds, who founded
it, the market) and is REPLACED, not appended, so a richer dig or a user correction can say it
better; `news` items are the dated, source-URLed traction signals, deduped by URL and capped; and
`updated_by` + `last_researched` are a company's half of the provenance a researched PERSON gets
(on a company, `updated_by` names who owns the ABOUT — the only field a rewrite can destroy).
`company_knowledge()` is the matching read side, so the next dig asks only for what's new.
Companies deliberately have NO facts map (the on-disk shape stays byte-compatible with
knowledge_files.rs), so they carry per-FILE provenance rather than per-fact confidence — and
knowledge_edit.py `--op company-about` is how one is corrected, through this same apply().

RELATIONS ride here too, one sentence: a relation is a typed edge between two people Sotto knows,
stored on both ends, readable as a sentence ("Introduced to you by Vishnu Sharma"). The vocabulary
is closed (kg.RELATION_INVERSE) and this module is its ONE writer — link_relation writes the
forward edge and its inverse together, unlink_relation removes both, and merge_person_files
repoints every back-reference to a merged-away slug. Extraction feeds it via
`person_updates[].relations`; an `other_person_name` that resolves to nobody becomes a plain
"(unlinked)" fact rather than a guessed slug.

CRASH SAFETY, one sentence: an interrupted graph update finishes the next time anything touches the
graph. Every write lands atomically (`kg.write_person_file`), and the operations that span FILES —
a relation is on both people, a merge writes the survivor and deletes the loser — go through
`kg.run_journaled`, whose op list every entry point here replays first (`graph_lock`).
"""
from __future__ import annotations

import difflib
import glob
import hashlib
import heapq
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # knowledge.py, its sibling
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import knowledge as kg  # noqa: E402
import knowledge_correction as correction  # noqa: E402
import jsonstore  # noqa: E402 — THE lock for the volume (see apply)
from source_corrections import MAX_RECEIPTS  # noqa: E402

ACCEPTED_FACT_DISPOSITIONS = correction.ACCEPTED_FACT_DISPOSITIONS
fact_establishes_research_coverage = correction.fact_establishes_research_coverage


# Dedup-lite tuning. Kept small and boring: the suggestion list is a human's to-do list, not a
# report, and the pairwise scan must never become the Learn step's cost centre.
MERGE_SUGGESTIONS_MAX = 10          # the file holds at most this many open suggestions
MERGE_DISMISSED_MAX = 100           # tombstoned pairs kept, newest first
MERGE_SUGGEST_MAX_FILES = 300       # above this the O(n²) name scan is skipped entirely
MERGE_SUGGEST_MAX_BUCKET = 40       # a name-prefix shared by more people than this is noise
NAME_SIMILARITY_MIN = 0.9           # difflib ratio over name slugs, when neither contains the other
MERGE_SUGGESTIONS_FILE = "merge_suggestions.json"
SOURCE_CORRECTION_FILE_BYTES = 1024 * 1024

# Relations ride the same evidence bar as facts, one notch higher: a wrong EDGE is louder than a
# wrong fact (it renders as a sentence about how two people know each other), so an inferred one
# is dropped rather than stored.
RELATION_MIN_CONFIDENCE = 0.7


def _slug_for(name_or_id: str):
    # Always slugify; never fall back to the raw value (path-traversal guard, H2).
    return kg.safe_slug(name_or_id)


# ── Entity-dedup lite: people ─────────────────────────────────────────────────

def _person_head(path: str) -> "kg.PersonFile":
    """A person's IDENTITY only — name, identifiers, updated_at — as a PersonFile with no facts.

    Deliberately a partial parse: the facts map is ~90% of a person file's YAML and the dedup
    passes never look at it, so paying kg.parse_person_file for every file on every Learn step
    would make memory hygiene the step's cost centre (measured: ~4× slower on a 300-person graph).
    The serializer writes `facts:` LAST in the frontmatter, so everything above it is the identity
    block. A file that somehow ordered facts first simply yields no identifiers here — which can
    only ever SUPPRESS a merge, never cause a wrong one."""
    lines = []
    with open(path, encoding="utf-8") as f:
        first = f.readline()
        if first.strip() != "---":
            return kg.PersonFile()
        for line in f:
            if line.startswith("facts:") or line.rstrip("\r\n") == "---":
                break
            lines.append(line)
    fm = yaml.safe_load("".join(lines)) or {}
    if not isinstance(fm, dict):
        return kg.PersonFile()
    return kg.PersonFile(canonical_id=fm.get("canonical_id", ""), name=fm.get("name", ""),
                         identifiers=[str(i) for i in (fm.get("identifiers") or [])],
                         x_user_id=(str(fm.get("x_user_id") or "").strip() or None),
                         x_handles=[h for h in (fm.get("x_handles") or []) if isinstance(h, dict)],
                         updated_at=fm.get("updated_at", ""))


def _load_people() -> dict:
    """{path: identity-only PersonFile} for every readable people/*.md. One unparseable file is
    skipped, never fatal (the same posture migrate_people_dir takes)."""
    out = {}
    for path in sorted(glob.glob(os.path.join(kg.people_dir(), "*.md"))):
        try:
            out[path] = _person_head(path)
        except Exception:  # noqa: BLE001
            continue
    return out


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _merge_rank(p: "kg.PersonFile", path: str) -> tuple:
    """Sort key picking the file a merge should keep: the richest one wins — most identifiers, then
    the bigger file (facts are the bulk of a person file, and the heads carry none), then the more
    recently updated — with the filename as the final tie-break so the outcome is deterministic on
    two identical files."""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return (-len(p.identifiers), -size, _neg(p.updated_at or ""), _stem(path))


def _neg(s: str) -> tuple:
    return tuple(-ord(c) for c in s)


def identifier_kinds(p: "kg.PersonFile") -> dict:
    """Normalized identifiers split by KIND — {"email": {...}, "phone": {...}}. The kind split is
    what makes the conflict rule meaningful: "Ben" (phone only) and "Ben Butler" (email only) are
    not in conflict, while two files each carrying a DIFFERENT email are."""
    kinds = {"email": set(), "phone": set()}
    for i in p.identifiers:
        raw = str(i or "").strip()
        k = kg.normalize_identifier(raw)
        if not k:
            continue
        kinds["email" if "@" in raw else "phone"].add(k)
    return kinds


def identifiers_conflict(a: "kg.PersonFile", b: "kg.PersonFile") -> bool:
    """True when the two files positively contradict each other: for some identifier KIND both
    carry values and share none. That is the two-John-Smiths signature — two real, distinct
    addresses for one name — and it vetoes both the suggestion and the confirmed merge."""
    ka, kb = identifier_kinds(a), identifier_kinds(b)
    for kind in ("email", "phone"):
        if ka[kind] and kb[kind] and not (ka[kind] & kb[kind]):
            return True
    if a.x_user_id and b.x_user_id and str(a.x_user_id) != str(b.x_user_id):
        return True
    return False


def _merge_person_files_unlocked(dst_path: str, src_path: str, now: datetime | None = None) -> bool:
    """Union src INTO dst and delete src — kg.absorb_person_file, which is migrate_people_dir's
    exact mechanics (nothing is dropped; dst wins ties) plus the two steps relations owe a merge:
    the survivor drops any edge that now points at itself, and every OTHER file's back-reference to
    the vanishing slug is repointed at the survivor. Returns False for a no-op self-merge."""
    return kg.absorb_person_file(dst_path, src_path, now)


def auto_merge_by_identifier(now: datetime | None = None) -> list:
    """Collision scan over the people index's by_identifier pass: build_people_index keeps ONE path
    per normalized identifier (last writer wins), so a genuine collision is invisible there — this
    walks the same normalization and keeps the groups. Two files sharing an exact email/phone are
    the same human; merge them. Deterministic, no LLM, no confirmation needed.

    Returns [{"into", "from", "identifier"}] — one entry per merge performed."""
    people = _load_people()
    by_ident: dict = {}
    for path, p in people.items():
        for i in p.identifiers:
            k = kg.normalize_identifier(str(i or ""))
            if k:
                by_ident.setdefault(k, []).append(path)
    merged: list = []
    gone: set = set()
    for ident in sorted(by_ident):
        paths = [p for p in dict.fromkeys(by_ident[ident]) if p not in gone and p in people]
        if len(paths) < 2:
            continue
        ranked = sorted(paths, key=lambda pp: _merge_rank(people[pp], pp))
        dst = ranked[0]
        for src in ranked[1:]:
            # Exact email/phone normally proves identity, but two different immutable X ids are a
            # positive contradiction caused by a bad prior link. Preserve both files for repair.
            if identifiers_conflict(people[dst], people[src]):
                continue
            try:
                if not merge_person_files(dst, src, now):
                    continue
            except Exception:  # noqa: BLE001 — one bad pair must not cost the Learn step
                continue
            gone.add(src)
            merged.append({"into": _stem(dst), "from": _stem(src), "identifier": ident})
            try:    # dst grew — re-read it so later groups rank against the merged file
                with open(dst, encoding="utf-8") as f:
                    people[dst] = kg.parse_person_file(f.read())
            except Exception:  # noqa: BLE001
                pass
    return merged


# ── Relations: the ONE writer ─────────────────────────────────────────────────
# A relation is a typed edge between two people Sotto knows, stored on BOTH ends, readable as a
# sentence. The vocabulary (kg.RELATION_INVERSE) is closed; every write goes through
# link_relation / unlink_relation, so the forward edge and its inverse cannot drift apart — there
# is no second place that appends to a `relations:` list.

def _person_path(slug: str) -> str:
    return kg.safe_path(kg.people_dir(), slug)


def new_person_stub(name: str, ident: str, iso: str, cid: str = "",
                    updated_by: str = "brief_extraction"):
    """(path, PersonFile) for a person we've never seen — name + identifiers, NO invented facts.
    THE identity stub: apply()'s create branch and the relation resolver both mint people here, so
    a person born from an edge is byte-shaped like one born from a message (or from prewarm_graph,
    which reaches this through apply)."""
    cid = cid or kg.default_canonical_id(name, [ident] if ident else [])
    return (kg.safe_path(kg.people_dir(), cid),
            kg.PersonFile(canonical_id=cid, name=name or cid,
                          identifiers=[ident] if ident else [],
                          updated_at=iso, updated_by=updated_by))


def _needs_edge(p: "kg.PersonFile", rel: "kg.Relation") -> bool:
    """False when this exact edge is already on file. Edge identity is (type, other end), so
    re-stating one never rewrites the date or confidence already there — which is also what makes
    replaying a finished link write no bytes."""
    return not any(r.key() == rel.key() for r in p.relations)


def _link_relation_unlocked(slug_a: str, slug_b: str, rel_type: str, name_a: str = "", name_b: str = "",
                  date: str = "", source: str = "brief_extraction", confidence: float = 0.9,
                  now: datetime | None = None) -> bool:
    """Write ONE typed edge on BOTH person files: `rel_type` from A to B, and its inverse from B to
    A. Returns True when anything changed. Refuses a type outside the closed vocabulary, a
    self-edge, and a side whose slug can't hold a file (see kg.read_or_stub_person)."""
    inverse = kg.RELATION_INVERSE.get(rel_type or "")
    if not inverse or not slug_a or not slug_b or slug_a == slug_b:
        return False
    now = now or datetime.now(timezone.utc)
    iso = kg.now_iso(now)
    _path_a, a = kg.read_or_stub_person(slug_a, name_a, iso, source)
    _path_b, b = kg.read_or_stub_person(slug_b, name_b, iso, source)
    if a is None or b is None:
        return False
    conf = max(0.0, min(float(confidence), 1.0))
    rel_ab = kg.Relation(type=rel_type, slug=slug_b, name=name_b or b.name,
                         date=date or None, source=source, confidence=conf)
    rel_ba = kg.Relation(type=inverse, slug=slug_a, name=name_a or a.name,
                         date=date or None, source=source, confidence=conf)
    ops = []
    if _needs_edge(a, rel_ab):
        ops.append(kg.edge_op(slug_a, name_a or a.name, rel_ab))
    if _needs_edge(b, rel_ba):
        ops.append(kg.edge_op(slug_b, name_b or b.name, rel_ba))
    if not ops:
        return False
    # An edge is ONE fact about TWO people, so the two file writes are one journaled batch: a crash
    # between them used to leave a relation only one side of it knew about.
    kg.run_journaled(ops, now)
    return True


def _unlink_relation_unlocked(slug_a: str, slug_b: str, rel_type: str = "",
                    now: datetime | None = None) -> int:
    """Remove the edge(s) between two people from BOTH files — the exact undo of link_relation. An
    empty rel_type removes every edge between them. Returns how many edges were dropped."""
    if not slug_a or not slug_b or slug_a == slug_b:
        return 0
    now = now or datetime.now(timezone.utc)
    inverse = kg.RELATION_INVERSE.get(rel_type or "") if rel_type else ""
    ops, dropped = [], 0
    for slug, other, want in ((slug_a, slug_b, rel_type), (slug_b, slug_a, inverse)):
        path = _person_path(slug)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            p = kg.parse_person_file(f.read())
        n = sum(1 for r in p.relations if r.slug == other and (not want or r.type == want))
        if not n:
            continue
        dropped += n
        ops.append(kg.unedge_op(slug, other, want))
    if ops:
        kg.run_journaled(ops, now)     # both ends or neither, exactly like the link
    return dropped


def resolve_relation_target(name: str, identifier: str, index: dict):
    """The person file an extracted `other_person_name` means, or None. Exact first (the same
    canonical_id → identifier → name index every write uses), then the dedup-lite name matching
    already in this module ("Vishnu" ⊂ "Vishnu Sharma") — and ONLY when it lands on exactly one
    candidate. Two plausible people is not a resolution; it's a guess, and a guessed slug is a
    wrong sentence on someone's page forever."""
    path = kg.find_person_file(name=name, identifier=identifier, index=index)
    if path and os.path.exists(path):
        return path
    want = kg.safe_slug(name or "")
    if not want:
        return None
    hits = {os.path.realpath(p) for s, p in index["by_name"].items()
            if _name_pair_reason(want, s) and os.path.exists(p)}
    return next(iter(hits)) if len(hits) == 1 else None


def _name_pair_reason(slug_a: str, slug_b: str) -> str:
    """"" when two name slugs aren't similar enough to be worth a human's attention, else the
    reason string the suggestion carries. Containment is token-wise ("ben" ⊂ "ben-butler", but NOT
    "ben" ⊂ "benjamin" — a prefix is not a name)."""
    if not slug_a or not slug_b:
        return ""
    ta, tb = set(slug_a.split("-")), set(slug_b.split("-"))
    if ta and tb and ta < tb:
        return f"name containment: {slug_a} ⊂ {slug_b}"
    if ta and tb and tb < ta:
        return f"name containment: {slug_b} ⊂ {slug_a}"
    # A ratio of R needs matches ≥ R·(la+lb)/2, and matches ≤ min(la, lb) — so lengths this far
    # apart can't reach the bar. Exact, and it skips the expensive call.
    la, lb = len(slug_a), len(slug_b)
    if min(la, lb) < NAME_SIMILARITY_MIN * (la + lb) / 2:
        return ""
    ratio = difflib.SequenceMatcher(None, slug_a, slug_b).ratio()
    if ratio >= NAME_SIMILARITY_MIN:
        return f"name similarity {ratio:.2f}: {slug_a} ≈ {slug_b}"
    return ""


def _candidate_pairs(slugs: dict):
    """The pairs worth comparing, out of the n² that exist. Both qualifying relations need real
    lexical overlap — token containment needs a SHARED TOKEN, and a ≥0.9 character ratio needs a
    common leading trigram — so bucketing on those two keys drops only pairs that could never
    qualify. The one bounded concession: a trigram bucket wider than MERGE_SUGGEST_MAX_BUCKET is
    skipped entirely (a prefix shared by dozens of people is noise, not a signal, and it is where
    the n² comes back). Shared-token pairs are never skipped."""
    tokens: dict = {}
    prefixes: dict = {}
    for path, slug in slugs.items():
        if not slug:
            continue
        for tok in set(slug.split("-")):
            tokens.setdefault(tok, []).append(path)
        prefixes.setdefault(slug[:3], []).append(path)
    seen: set = set()
    groups = list(tokens.values()) + [g for g in prefixes.values() if len(g) <= MERGE_SUGGEST_MAX_BUCKET]
    for group in groups:
        if len(group) < 2:
            continue
        group = sorted(group)
        for i, pa in enumerate(group):
            for pb in group[i + 1:]:
                if (pa, pb) not in seen:
                    seen.add((pa, pb))
                    yield (pa, pb)


def _suggestions_path() -> str:
    return os.path.join(kg.data_root(), "knowledge", MERGE_SUGGESTIONS_FILE)


def load_merge_suggestions() -> list:
    """The open suggestions, or [] (a missing/corrupt file is simply "none")."""
    try:
        with open(_suggestions_path(), encoding="utf-8") as f:
            data = json.load(f) or {}
        items = data.get("suggestions")
        return [s for s in items if isinstance(s, dict)] if isinstance(items, list) else []
    except Exception:  # noqa: BLE001
        return []


def load_merge_dismissed() -> list:
    """The tombstoned pairs — [{from, into, at}, …] — or [] (a missing/corrupt file is "none").
    Records the user's "no" in a suppression list, so a refresh
    that recomputes from scratch cannot resurrect what they already answered."""
    try:
        with open(_suggestions_path(), encoding="utf-8") as f:
            data = json.load(f) or {}
        items = data.get("dismissed")
        return [d for d in items if isinstance(d, dict)] if isinstance(items, list) else []
    except Exception:  # noqa: BLE001
        return []


def _pair_key(d: dict) -> frozenset:
    """A merge pair identifies two people, not a direction — match order-insensitively."""
    return frozenset((_s(d.get("from")), _s(d.get("into"))))


def _write_merge_suggestions(items: list, iso: str, dismissed: list | None = None) -> None:
    """The ONE writer of merge_suggestions.json. `dismissed` defaults to whatever is already on
    disk, so a suggestions refresh can never drop the tombstones it must obey."""
    path = _suggestions_path()
    tombs = load_merge_dismissed() if dismissed is None else dismissed
    try:
        kg.write_text_atomic(path, json.dumps(
            {"updated_at": iso, "suggestions": items[:MERGE_SUGGESTIONS_MAX],
             "dismissed": tombs[:MERGE_DISMISSED_MAX]}, indent=1))
    except OSError:
        pass


def suggest_name_merges(now: datetime | None = None) -> list:
    """Refresh knowledge/merge_suggestions.json and return its entries.

    SUGGEST-ONLY BY CONSTRUCTION — this function never merges anything. A pair qualifies when the
    names are close (containment or a high ratio) AND their identifiers do not conflict; anything
    with a positive identifier contradiction (the two-John-Smiths shape) is dropped, so the
    suggestion list can never reinforce the name-slug bug it exists to clean up. Stale entries
    (either file gone — usually because the merge happened) are pruned on every pass, and
    first_seen survives so the dashboard can age them.

    A pair the user dismissed is tombstoned in the same file and never suggested again — this
    recomputes from the people on disk every apply, so without the tombstone "these really are two
    people" would be re-asked tomorrow."""
    now = now or datetime.now(timezone.utc)
    people = _load_people()
    prior = {(_s(x.get("from")), _s(x.get("into"))): x for x in load_merge_suggestions()}
    tombstoned = {_pair_key(d) for d in load_merge_dismissed()}
    items: list = []
    if len(people) <= MERGE_SUGGEST_MAX_FILES:
        slugs = {path: kg.safe_slug(p.name or "") for path, p in people.items()}
        seen: set = set()
        for pa, pb in _candidate_pairs(slugs):
            a, b = people[pa], people[pb]
            reason = _name_pair_reason(slugs[pa], slugs[pb])
            if not reason or identifiers_conflict(a, b):
                continue
            # from = the sparser file (the one that disappears), into = the richer one.
            src, dst = sorted((pa, pb), key=lambda pp: _merge_rank(people[pp], pp))[::-1]
            key = (_stem(src), _stem(dst))
            if key in seen or frozenset(key) in tombstoned:
                continue
            seen.add(key)
            items.append({
                "from": key[0], "into": key[1],
                "from_name": people[src].name, "into_name": people[dst].name,
                "reason": reason,
                "first_seen": _s(prior.get(key, {}).get("first_seen")) or kg.today_str(now),
            })
    items.sort(key=lambda x: (x.get("first_seen", ""), x.get("into", ""), x.get("from", "")))
    _write_merge_suggestions(items, kg.now_iso(now))
    return items[:MERGE_SUGGESTIONS_MAX]


def drop_merge_suggestion(src_slug: str, dst_slug: str, now: datetime | None = None) -> None:
    """Forget a suggestion once it has been acted on (either direction). Best-effort. A CONFIRMED
    merge needs no tombstone — one of the two files is gone, so the pair can never be suggested
    again; only a dismissal does (`dismiss_merge_suggestion`)."""
    pair = {(src_slug, dst_slug), (dst_slug, src_slug)}
    kept = [s for s in load_merge_suggestions() if (_s(s.get("from")), _s(s.get("into"))) not in pair]
    _write_merge_suggestions(kept, kg.now_iso(now or datetime.now(timezone.utc)))


def dismiss_merge_suggestion(src_slug: str, dst_slug: str, now: datetime | None = None) -> None:
    """"These really are two people" — drop the suggestion AND tombstone the pair, so the next
    suggest_name_merges() (which recomputes from the files on disk) does not re-ask tomorrow."""
    now = now or datetime.now(timezone.utc)
    pair = {(src_slug, dst_slug), (dst_slug, src_slug)}
    kept = [s for s in load_merge_suggestions() if (_s(s.get("from")), _s(s.get("into"))) not in pair]
    key = frozenset((src_slug, dst_slug))
    tombs = [d for d in load_merge_dismissed() if _pair_key(d) != key]
    tombs.insert(0, {"from": src_slug, "into": dst_slug, "at": kg.today_str(now)})
    _write_merge_suggestions(kept, kg.now_iso(now), tombs)


def _s(v) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))


# ── Entity-dedup lite: companies (prevention, not repair) ─────────────────────

def _company_index() -> dict:
    """One pass over companies/*.md → {"by_alias": {alias_slug: path}, "by_domain": {domain: path}}.
    The frontmatter has carried `aliases` and `domain` since the Rust port; nothing ever READ them,
    which is exactly why "YC" and "Y Combinator" became two files."""
    by_alias: dict = {}
    by_domain: dict = {}
    for path in sorted(glob.glob(os.path.join(kg.companies_dir(), "*.md"))):
        try:
            with open(path, encoding="utf-8") as f:
                yaml_str, _body = kg.split_frontmatter_body(f.read())
            import yaml as _y
            fm = (_y.safe_load(yaml_str) if yaml_str else {}) or {}
        except Exception:  # noqa: BLE001
            continue
        for alias in ([fm.get("normalized")] + list(fm.get("aliases") or [])):
            slug = kg.safe_slug(kg.company_slug(_s(alias))) if alias else None
            if slug:
                by_alias.setdefault(slug, path)
        dom = _s(fm.get("domain")).strip().lower().lstrip("@")
        if dom:
            by_domain.setdefault(dom, path)
    return {"by_alias": by_alias, "by_domain": by_domain}


def _company_domain_of(upd: dict) -> str:
    """The domain an update carries, if any: an explicit `domain` field, or a company_name that IS
    a domain ("acme.com"). Never guessed from prose."""
    dom = _s(upd.get("domain")).strip().lower().lstrip("@")
    if dom and "." in dom:
        return dom
    name = _s(upd.get("company_name")).strip().lower()
    if "." in name and " " not in name and "@" not in name:
        return name
    return ""


def resolve_company_path(upd: dict, index: dict) -> tuple:
    """(path, slug) for a company update — slug first (today's behavior), then the alias and domain
    lookups that prevent the duplicate. Returns (None, None) when the name is unusable."""
    slug = kg.safe_slug(_s(upd.get("company_slug")) or kg.company_slug(_s(upd.get("company_name"))))
    if not slug:
        return (None, None)
    path = kg.safe_path(kg.companies_dir(), slug)
    if os.path.exists(path):
        return (path, slug)
    hit = index["by_alias"].get(slug)
    if not hit:
        dom = _company_domain_of(upd)
        hit = index["by_domain"].get(dom) if dom else None
    if hit and os.path.exists(hit):
        return (hit, _stem(hit))
    return (path, slug)



class CompanyLookupError(ValueError):
    """Closed failure for the opt-in unique saved-company read only."""
    def __init__(self, status):
        self.status = status
        super().__init__(status)


def _unique_company_path(upd):
    """Resolve actual saved candidates without changing legacy write resolution."""
    slug = kg.safe_slug(_s(upd.get('company_slug')) or kg.company_slug(_s(upd.get('company_name'))))
    if not slug:
        return None, None
    domain = _company_domain_of(upd)
    candidates = set()
    try:
        directory = kg.companies_dir()
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            names = []
        paths = [Path(directory) / name for name in sorted(names) if name.endswith('.md')]
        for path in paths:
            yaml_str, _ = kg.split_frontmatter_body(path.read_text(encoding='utf-8'))
            import yaml
            fm = (yaml.safe_load(yaml_str) if yaml_str else {}) or {}
            if (type(fm) is not dict or type(fm.get('aliases', [])) is not list
                    or any(type(a) is not str for a in fm.get('aliases', []))
                    or any(type(fm.get(k, '')) is not str for k in ('normalized', 'domain'))):
                raise ValueError('company metadata invalid')
            aliases = [fm.get('normalized', ''), *fm.get('aliases', [])]
            alias_slugs = {kg.safe_slug(kg.company_slug(a)) for a in aliases if a}
            saved_domain = fm.get('domain', '').strip().lower().lstrip('@')
            if path.stem == slug or slug in alias_slugs or domain and domain == saved_domain:
                candidates.add(str(path))
    except Exception as exc:  # noqa: BLE001 -- strict read must not misreport unreadable as missing
        raise CompanyLookupError('unavailable') from exc
    if len(candidates) > 1:
        raise CompanyLookupError('ambiguous')
    if not candidates:
        return None, None
    path = candidates.pop()
    return path, _stem(path)

def company_knowledge(company_name: str = "", domain: str = "", company_slug: str = "", *, unique: bool = False) -> dict:
    """What is ALREADY on file about a company — the read side of the writer above, and the only
    one. Returns {slug, name, domain, about, news[], context, updated_by, last_researched} for the file the
    same resolution (slug → alias → domain) would write to, or {} when nothing is on file.

    Two callers, one purpose — never buy a fact twice: `research_attendees` injects it into the
    focus pass as "already on file, don't repeat it", and `persist_prep` asks it whether the About
    it is about to write would be an upgrade or a downgrade. Best-effort: an unreadable graph
    returns {} and the caller simply researches as if cold."""
    upd = {"company_name": company_name, "domain": domain, "company_slug": company_slug}
    try:
        path, slug = _unique_company_path(upd) if unique else resolve_company_path(upd, _company_index())
        if not slug or not path or not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            yaml_str, body = kg.split_frontmatter_body(f.read())
        import yaml as _y
        fm = (_y.safe_load(yaml_str) if yaml_str else {}) or {}
        secs = _parse_company_body(body)
    except CompanyLookupError:
        raise
    except Exception as exc:  # noqa: BLE001 — reading memory must never fail a research run
        if unique:
            raise CompanyLookupError('unavailable') from exc
        return {}
    aliases = [_s(a) for a in (fm.get("aliases") or []) if _s(a).strip()]
    return {"slug": slug, "name": aliases[0] if aliases else _s(fm.get("normalized")) or slug,
            "domain": _s(fm.get("domain")),
            "about": secs["about"], "news": secs["news"], "context": secs["context"],
            "updated_by": _s(fm.get("updated_by")), "last_researched": _s(fm.get("last_researched"))}


def _duplicate_family(facts: dict, matched: str):
    """Resolve only explicit local duplicate links; retain each record's owning provenance.

    Rebuild from current records so a later proposal in the same apply sees earlier evidence.
    No cross-person lookup or semantic merge can supply a missing endpoint.
    """
    reverse = {}
    for fid, record in facts.items():
        if record.duplicate_of:
            reverse.setdefault(record.duplicate_of, []).append(fid)
    keeper, visited = matched, set()
    while True:
        if keeper in visited or keeper not in facts:
            return None
        visited.add(keeper)
        record = facts[keeper]
        if record.archived_reason == 'duplicate' or record.duplicate_of:
            if (record.status != 'archived' or record.archived_reason != 'duplicate'
                    or not record.duplicate_of):
                return None
            keeper = record.duplicate_of
        else:
            break
    family, pending = set(), [keeper]
    while pending:
        fid = pending.pop()
        if fid in family:
            return None
        family.add(fid)
        for child in reverse.get(fid, []):
            record = facts[child]
            if record.status != 'archived' or record.archived_reason != 'duplicate':
                return None
            pending.append(child)
    retained, incomplete, unknown, dates = set(), False, False, []
    for fid in family:
        record = facts[fid]
        refs = {value for value in [*record.evidence_refs, record.source_ref] if value}
        retained.update(refs)
        # No schema migration can recover references evicted by the rolling cap, or prove
        # completeness for legacy records lacking their evidence list.
        unknown |= not record.evidence_refs or record.seen > len(refs)
        incomplete |= len(record.evidence_refs) >= 64
        dates.append(record.last)
    try:
        latest = max(date.fromisoformat(value).isoformat() for value in dates)
    except (ValueError, TypeError):
        latest = None
    return keeper, retained, incomplete or unknown, latest, unknown


def _apply_fact(p: "kg.PersonFile", cid: str, fu: dict, today: str, counts: dict) -> dict:
    """One extracted fact onto one person: the bump / supersede / new decision and its bookkeeping.
    THE fact writer — apply()'s person loop and the relations fallback both land here, so an
    "unlinked" relation is stored by exactly the code that stores every other fact."""
    if float(fu.get("confidence", 0.8)) < 0.5:
        return {'disposition': 'rejected', 'reason': 'low_confidence'}
    if (fu.get('source') == 'apple_contacts' and fu.get('change_type') != 'correction'
            and not any(c.isalpha() for c in str(fu.get('fact') or ''))):
        return {'disposition': 'rejected', 'reason': 'non_assertion'}
    observed = str(fu.get('observed_date') or '')
    has_observed_date = bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', observed) and observed <= today)
    if has_observed_date:
        today = observed
    force = fu.get("change_type") == "correction"
    action, existing_id = kg.find_similar_fact(
        p.facts, fu["fact"], fu.get("memory_type", ""), force, include_tombstone_id=True
    )
    if fu.get('exact_new') is True and action != kg.NEW:
        # Explicit owner teaching adds exactly this fact or nothing: word overlap never bumps,
        # re-stamps or archives a fact the owner did not name (correction is a separate route).
        if action != kg.SUPERSEDE:
            return {'disposition': 'rejected', 'reason': 'similar_fact_exists', 'fact_id': existing_id}
        action, existing_id = kg.NEW, None
    source = fu.get('source') or 'brief_extraction'
    ref = str(fu.get('source_ref') or '')[:500]
    # Keep the writer-provided order: at the cap, lexicographic slicing could discard every new
    # reference forever merely because it sorted after the first 64.  The rolling window below
    # retains the newest distinct evidence while bounding the on-disk fact.
    supplied_refs = fu.get('evidence_refs', [])
    supplied_refs = supplied_refs if isinstance(supplied_refs, list) else []
    new_refs = list(dict.fromkeys(
        v for v in [*supplied_refs, *([ref] if ref else [])]
        if isinstance(v, str) and 0 < len(v) <= 500
    ))[-64:]
    if source != 'user_edit':
        invalid = kg.source_invalidated_refs()
        surviving = [value for value in new_refs if value not in invalid]
        if new_refs and not surviving:
            return {'disposition': 'rejected', 'reason': 'withdrawn_source_evidence'}
        new_refs = surviving
        if ref in invalid:
            ref = new_refs[0] if new_refs else ''
    if action != kg.SKIP and existing_id and p.facts[existing_id].source == 'user_edit' and source != 'user_edit':
        return {'disposition': 'rejected', 'reason': 'protected_owner_correction'}
    family_refs, family_incomplete, family_latest, family_unknown = set(), False, None, False
    family_resolved = False
    if source == 'observed_message' and not force:
        # Overlapping words do not prove two historical statements agree. Keep both assertions
        # for evidence-based curation instead of strengthening or overwriting one heuristically.
        # Active first (same reason as kg.find_similar_fact): an archived twin must not shadow the
        # live copy of the same words and turn a re-observation into a SKIP.
        exact = next((fid for fid, fact in sorted(p.facts.items(), key=lambda kv: kv[1].status == 'archived')
                      if ' '.join(fact.text.split()).casefold() == ' '.join(fu['fact'].split()).casefold()), None)
        if exact:
            family = _duplicate_family(p.facts, exact)
            if family is None:
                return {'disposition': 'rejected', 'reason': 'invalid_duplicate_lineage', 'fact_id': exact}
            exact, family_refs, family_incomplete, family_latest, family_unknown = family
            family_resolved = True
            if p.facts[exact].source == 'user_edit':
                return {'disposition': 'rejected', 'reason': 'protected_owner_correction', 'fact_id': exact}
            revivable = p.facts[exact].archived_reason == 'stale'
            action, existing_id = (kg.BUMP if p.facts[exact].status == 'active' or revivable else kg.SKIP, exact)
        else:
            action, existing_id = kg.NEW, None
    if action == kg.BUMP:
        if source != 'user_edit' and not family_resolved:
            family = _duplicate_family(p.facts, existing_id)
            if family is None:
                return {'disposition': 'rejected', 'reason': 'invalid_duplicate_lineage', 'fact_id': existing_id}
            existing_id, family_refs, family_incomplete, family_latest, family_unknown = family
            if p.facts[existing_id].source == 'user_edit':
                return {'disposition': 'rejected', 'reason': 'protected_owner_correction', 'fact_id': existing_id}
        ex = p.facts[existing_id]
        if source == 'user_edit':
            if ex.status == 'archived':
                ex.status, ex.archived_text, ex.archived_reason, ex.duplicate_of = 'active', None, None, None
            # An explicit confirmation upgrades authority once; automatic replay never does.
            upgraded = ex.source != 'user_edit'
            if upgraded:
                ex.seen += 1
                counts['confirmed'] += 1
            ex.source, ex.source_ref = 'user_edit', ref or 'user-confirmation'
            ex.conf = float(fu.get('confidence', 1.0))
            ex.last = max(ex.last or today, today)
            return {'disposition': 'confirmed' if upgraded else 'updated',
                    'reason': 'owner_confirmation', 'fact_id': existing_id}
        refs = list(dict.fromkeys([*(ex.evidence_refs or []),
                                   *([ex.source_ref] if ex.source_ref else [])]))
        retained = set(refs) | family_refs
        novel = [value for value in new_refs if value not in retained]
        if not novel:
            # Re-reading the same evidence is not independent confirmation: no seen/conf bump.
            # Nor is it a fresh observation: repeated or ref-less extraction can be the model
            # proposing the same words from the same input. Only distinct source evidence (or the
            # explicit user-confirmation branch above) may refresh evidence recency.
            return {'disposition': 'duplicate', 'reason': 'no_new_evidence', 'fact_id': existing_id}
        saturated = len(refs) >= 64 or family_incomplete
        # A provenance-only append must not turn a legacy incomplete list into apparent
        # completeness on the next proposal/restart. Without new schema, retain that signal
        # even after chronology earns corroboration; do not fabricate historical references.
        if not family_unknown:
            ex.evidence_refs = list(dict.fromkeys([*refs, *novel]))[-64:]
        if saturated and not (has_observed_date and family_latest
                              and observed > family_latest):
            # A bounded exact set cannot remember every evicted reference. Keep rolling provenance,
            # but after saturation require source-bound chronology before granting corroboration:
            # processing on a later day alone must not make replayed old evidence look new.
            return {'disposition': 'updated', 'reason': 'evidence_refs_only', 'fact_id': existing_id}
        if ex.status == 'archived':
            ex.status, ex.archived_text, ex.archived_reason, ex.duplicate_of = 'active', None, None, None
        ex.seen += 1
        ex.conf = min(ex.conf + 0.1, 1.0)
        ex.first = min(ex.first or today, today)
        ex.last = max(ex.last or today, today)
        counts["confirmed"] += 1
        return {'disposition': 'confirmed', 'reason': 'independent_evidence', 'fact_id': existing_id}
    elif action in (kg.SUPERSEDE, kg.NEW):
        if action == kg.SUPERSEDE:
            ex = p.facts[existing_id]
            if today < ex.last and not force:
                return {'disposition': 'rejected', 'reason': 'older_observation', 'fact_id': existing_id}
            ex.status = "archived"
            ex.archived_text = ex.text
            ex.archived_reason = "user_corrected" if source == "user_edit" else "superseded"
            counts["superseded"] += 1
        fid = kg.generate_fact_id(cid, fu["fact"], today)
        p.facts[fid] = kg.FactMeta(
            text=fu["fact"], type=fu.get("memory_type", ""), status="active",
            seen=1, conf=float(fu.get("confidence", 0.8)),
            # provenance: research writers (persist_prep) pass source="web_research";
            # extraction writes omit it and keep the default label.
            source=source, evidence_refs=new_refs[-64:],
            source_ref=ref, first=today, last=today,
        )
        counts["new"] += 1
        return {'disposition': 'superseded' if action == kg.SUPERSEDE else 'added',
                'reason': 'accepted', 'fact_id': fid}
    return {'disposition': 'ignored', 'reason': 'archived_match', 'fact_id': existing_id,
            'tombstone_reason': p.facts[existing_id].archived_reason if existing_id else None}


def _apply_relations(pending: list, index: dict, counts: dict, dropped: list,
                     person_files: list, now: datetime) -> None:
    """Resolve and write the batch's extracted relations, after every person file it touched exists.

    Three outcomes, in order: the other person resolves → one edge on both ends; they don't resolve
    but the extraction gave an identifier → the identity stub, then the edge; neither → the edge is
    NOT invented, it is written on the subject as a plain fact ("Introduced to you by Vishnu
    (unlinked)"), because a slug we guessed is worse than a sentence we can't click."""
    today = kg.today_str(now)
    iso = kg.now_iso(now)
    for cid, subject_name, rels in pending:
        fallbacks = []
        for rel in rels if isinstance(rels, list) else []:
            if not isinstance(rel, dict):
                continue
            rtype = str(rel.get("type") or "").strip().lower()
            if rtype not in kg.RELATION_INVERSE:
                dropped.append(rtype or "(blank)")
                continue
            conf = rel.get("confidence", 0.8)
            conf = float(conf) if isinstance(conf, (int, float)) else 0.8
            if conf < RELATION_MIN_CONFIDENCE:
                continue
            other_name = str(rel.get("other_person_name") or "").strip()
            other_ident = str(rel.get("other_identifier") or "").strip()
            other_ident = other_ident.lower() if "@" in other_ident else other_ident
            if not other_name and not other_ident:
                continue
            date = str(rel.get("date") or "").strip()[:10]
            path = resolve_relation_target(other_name, other_ident, index)
            if path is None and other_ident:
                # An identifier is not a coincidence (the auto-merge's rule): it is enough identity
                # to mint the person. A bare NAME out of prose is not.
                path, stub = new_person_stub(other_name or other_ident, other_ident, iso)
                kg.write_person_file(path, stub, now)
                index["by_cid"][stub.canonical_id] = path
                k = kg.normalize_identifier(other_ident)
                if k:
                    index["by_identifier"][k] = path
                s = _slug_for(stub.name)
                if s:
                    index["by_name"][s] = path
                person_files.append(os.path.basename(path))
            if path is None:
                sentence = kg.relation_sentence(rtype, other_name, date)
                if sentence:
                    fallbacks.append({"fact": f"{sentence} (unlinked)", "memory_type": "context",
                                      "confidence": conf, "source": "brief_extraction"})
                continue
            if link_relation(cid, _stem(path), rtype, name_a=subject_name,
                             name_b=other_name, date=date, confidence=conf, now=now):
                counts["relations"] += 1
                for name in (f"{cid}.md", os.path.basename(path)):
                    if name not in person_files:
                        person_files.append(name)
        if not fallbacks:
            continue
        subject_path = _person_path(cid)
        if not os.path.exists(subject_path):
            continue
        with open(subject_path, encoding="utf-8") as f:
            p = kg.parse_person_file(f.read())
        for fu in fallbacks:
            _apply_fact(p, cid, fu, today, counts)
        kg.write_person_file(subject_path, p, now)


def apply_lock_target() -> str:
    """What `apply()` locks: `$SOTTO_DATA/knowledge/.apply` → the sidecar `.apply.lock`. Named once
    so a second caller can take THE graph lock rather than invent a second one."""
    return os.path.join(kg.data_root(), "knowledge", ".apply")


@contextmanager
def graph_lock():
    """THE graph's critical section: take the one lock, then finish any interrupted multi-file
    update before reading a single file.

    Every writer entry point in this module opens with it, which is what makes the rule true —
    *an interrupted graph update finishes the next time anything touches the graph*. Replay is
    silent unless it repairs something, and jsonstore.lock is reentrant, so the nested entry points
    (apply → auto-merge → link) pay for the lock and the replay check once."""
    with jsonstore.lock(apply_lock_target()):
        kg.replay_journal()
        yield


def apply(extracted: dict, now: datetime | None = None, *, maintain_identities: bool = True) -> dict:
    """Apply an extraction to the graph under ONE lock — the graph's single writer, serialized.

    Four processes call this concurrently (the brief's Learn step, meeting-prep research, the
    prewarm sweep, and a dashboard/chat edit), each doing read → modify → write over the same
    person and company markdown. That is the preferences.json race one level up: two applies that
    overlap can drop one's facts entirely, because the second parsed the file before the first wrote
    it. The lock is held across the WHOLE body — the migration, the auto-merge, every file write and
    the suggestion refresh — because those steps read each other's output.

    Explicit target-only owner edits disable identity maintenance to avoid incidental migration,
    merges or suggestion refresh; fact construction and persistence still use this writer.

    jsonstore.lock is reentrant within a process, so the locked merge/relation entry points nest here for free."""
    with graph_lock():
        return _apply(extracted, now, maintain_identities=maintain_identities)


def prune_stale_memory(now: datetime | None = None) -> dict:
    """Apply the existing age rule to untouched people through the same graph writer.

    Archive only stale single-observation facts; owner words and corroborated facts retain
    the existing exemption. No model, source fetch, identity migration or loop mutation.
    A failed write leaves the caller's weekly checkpoint unadvanced; replay is idempotent.
    """
    now = now or datetime.now(timezone.utc)
    counts = {'people': 0, 'pruned': 0, 'unreadable': 0}
    with graph_lock():
        for path in sorted(Path(kg.people_dir()).glob('*.md')):
            try:
                person = kg.parse_person_file(path.read_text())
            except Exception:  # noqa: BLE001
                # Like the index builders: one unreadable file is skipped and counted, never
                # rewritten, and cannot hold back archival for every other person.
                counts['unreadable'] += 1
                continue
            before = {fid for fid, fact in person.facts.items() if fact.status == 'archived'}
            kg.prune_stale_facts(person.facts, now)
            pruned = sum(fact.status == 'archived' and fid not in before
                         for fid, fact in person.facts.items())
            if pruned:
                # Do not refresh evidence dates or rewrite unchanged files.
                person.updated_at = kg.now_iso(now)
                person.updated_by = 'memory_prune'
                kg.write_person_file(str(path), person, now)
                counts['people'] += 1
                counts['pruned'] += pruned
    return counts


def migrate_source_reference(canonical_id, fact_id, expected_version, *, channel, legacy_ref,
                             original_records, current_record):
    """Apply one reviewed native-ID mapping, without asserting complete historical support.

    This deliberately has no automatic scan or model tool: callers must supply retained native
    identity/version proof and an exact fact version reviewed by the owner. Row-only originals
    and fingerprint guesses cannot establish a mapping. Evidence dates and owner words stay put.
    """
    from render_local import verified_legacy_reference_mapping

    if (not isinstance(canonical_id, str) or not kg.valid_canonical_id(canonical_id)
            or not isinstance(fact_id, str) or not fact_id or len(fact_id) > 256
            or not isinstance(expected_version, str)
            or re.fullmatch(r'[a-f0-9]{64}', expected_version) is None
            or not isinstance(original_records, list) or not 1 <= len(original_records) <= 64):
        raise ValueError('invalid reviewed source migration target')
    mapping = verified_legacy_reference_mapping(channel, legacy_ref, original_records, current_record)
    if mapping is None:
        return {'status': 'unsupported'}
    with graph_lock():
        original_path = Path(kg.people_dir()) / (canonical_id + '.md')
        if original_path.is_symlink():
            return {'status': 'unsupported'}
        path = Path(kg.safe_path(kg.people_dir(), canonical_id))
        try:
            with path.open('rb') as handle:
                raw = handle.read(SOURCE_CORRECTION_FILE_BYTES + 1)
            if len(raw) > SOURCE_CORRECTION_FILE_BYTES:
                return {'status': 'unsupported'}
            person = kg.parse_person_file(raw.decode('utf-8'), invalid_refs=set())
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            return {'status': 'unsupported'}
        fact = person.facts.get(fact_id)
        if (person.canonical_id != canonical_id or fact is None
                or kg.person_fact_version(canonical_id, fact_id, fact) != expected_version):
            return {'status': 'changed_during_review'}
        if fact.source == 'user_edit':
            return {'status': 'protected'}
        if legacy_ref not in [fact.source_ref, *fact.evidence_refs]:
            return {'status': 'unsupported'}
        new_ref = mapping['new_version_ref']
        if fact.source_ref == legacy_ref:
            fact.source_ref = new_ref
        # Empty evidence_refs remain empty: migrating one citation does not prove that the
        # legacy writer retained all supporting observations.
        fact.evidence_refs = list(dict.fromkeys(new_ref if ref == legacy_ref else ref
                                               for ref in fact.evidence_refs))
        kg.write_person_file(str(path), person)
        return {'status': 'migrated', 'fact_version': kg.person_fact_version(canonical_id, fact_id, fact)}


def project_source_correction(refs, now=None, *, after='', limit=100):
    """Bounded async projection of already admitted receipts through the canonical graph writer."""
    if (not isinstance(refs, (list, set, tuple)) or any(not isinstance(ref, str) for ref in refs)
            or len(refs) > MAX_RECEIPTS or not isinstance(after, str) or len(after) > 256 or '/' in after or '\\' in after
            or type(limit) is not int or not 1 <= limit <= 100):
        raise ValueError('invalid source correction projection bounds')
    admitted = set(refs) & kg.source_invalidated_refs()
    counts = {'status': 'complete', 'scanned': 0, 'changed': 0, 'archived': 0,
              'support_removed': 0, 'restored': 0, 'unsupported': 0, 'unreadable': 0,
              'next_cursor': '', 'complete': True}
    if not admitted:
        return counts
    now = now or datetime.now(timezone.utc)
    with graph_lock():
        paths = heapq.nsmallest(limit + 1,
                               (path for path in Path(kg.people_dir()).glob('*.md') if path.name > after))
        batch = paths[:limit]
        counts['complete'] = len(paths) <= limit
        counts['status'] = 'complete' if counts['complete'] else 'partial'
        for path in batch:
            counts['scanned'] += 1
            counts['next_cursor'] = path.name
            try:
                if path.is_symlink():
                    raise ValueError('symlink is not a graph projection target')
                with path.open('rb') as handle:
                    raw = handle.read(SOURCE_CORRECTION_FILE_BYTES + 1)
                if len(raw) > SOURCE_CORRECTION_FILE_BYTES:
                    raise ValueError('oversized graph projection target')
                person = kg.parse_person_file(raw.decode('utf-8'), invalid_refs=set())
                changed = kg.project_invalidated_evidence(person, admitted)
                if any(changed[key] for key in ('archived', 'support_removed', 'restored')):
                    person.updated_at = kg.now_iso(now)
                    if not kg.summary_protected(person):
                        person.updated_by = 'source_correction'
                    write_failed = False
                    try:
                        kg.write_person_file(str(path), person, now)
                    except OSError:
                        write_failed = True
                    if write_failed:
                        # No exception context containing writer diagnostics survives this seam.
                        raise RuntimeError('source correction write failed')
                    counts['changed'] += 1
                for key, count in changed.items():
                    counts[key] += count
            except (OSError, ValueError, TypeError, AttributeError, RecursionError, OverflowError, yaml.YAMLError):
                counts['unreadable'] += 1
        if counts['complete']:
            counts['next_cursor'] = ''
    return counts


def _apply(extracted: dict, now: datetime | None = None, *, maintain_identities: bool = True) -> dict:
    # UTC, not server-local: now_iso labels the timestamp with a 'Z' suffix, so feeding it local
    # time would write a lie into every updated_at in the graph.
    now = now or datetime.now(timezone.utc)
    today = kg.today_str(now)
    iso = kg.now_iso(now)
    counts = {"new": 0, "confirmed": 0, "superseded": 0, "pruned": 0, "relations": 0}
    fact_outcomes = {'total': 0, 'counts': {}, 'items': [], 'truncated': False}

    person_files: list[str] = []
    company_files: list[str] = []
    # (subject cid, subject name, [extracted relation]) — resolved AFTER every person file in this
    # batch is written, so "Vishnu introduced me to Priya" links even when Priya's file is created
    # by the same brief.
    pending_relations: list[tuple] = []
    dropped_relation_types: list[str] = []

    os.makedirs(kg.people_dir(), exist_ok=True)
    os.makedirs(kg.companies_dir(), exist_ok=True)

    # Files are keyed by canonical_id, with legacy name-slug files auto-migrated once (idempotent).
    # An existing person is found canonical_id → identifier → name, so "Sarah" texting and
    # "Sarah Chen" emailing land in ONE file instead of fragmenting the graph per name form.
    if maintain_identities:
        kg.migrate_people_dir(now)
    # Entity-dedup lite, half one: two files sharing an exact identifier are one human. Runs HERE,
    # piggybacking the migration call, so the day's updates land in the merged file rather than
    # re-splitting it — and BEFORE the index is built, so the index can't hand back a path that
    # this pass just deleted.
    auto_merged = auto_merge_by_identifier(now) if maintain_identities else []
    index = kg.build_people_index()

    for person_index, upd in enumerate(extracted.get("person_updates", [])):
        ident = (upd.get("identifier") or "").strip()
        ident = ident.lower() if "@" in ident else ident
        name = upd.get("person_name") or ""
        cid = upd.get("canonical_id") or ""
        if not kg.valid_canonical_id(cid):
            cid = ""  # malformed/LLM-invented id → resolve by identifier/name instead
        if not cid and not ident and not _slug_for(name):
            for fact_index, fact in enumerate(upd.get('facts', [])):
                correction.record_fact_outcome(fact_outcomes, person_index, fact_index, fact,
                            {'disposition': 'rejected', 'reason': 'missing_person_identity'})
            continue  # nothing usable to key this person by (garbage-name-only update)

        path = kg.find_person_file(name=name, identifier=ident, cid=cid, index=index)
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                p = kg.parse_person_file(f.read())
            # An EXPLICIT canonical_id that differs from the matched file means a DIFFERENT person
            # who happens to share the name (two "John Smith"s) — never merge them.
            if cid and kg.valid_canonical_id(p.canonical_id) and p.canonical_id != cid:
                path = None
            elif not kg.valid_canonical_id(p.canonical_id):
                p.canonical_id = cid or kg.default_canonical_id(name or p.name, p.identifiers or [ident])
                cid = p.canonical_id
            else:
                cid = p.canonical_id  # the FILE's identity is authoritative
        if not (path and os.path.exists(path)):
            path, p = new_person_stub(name, ident, iso, cid)
            cid = p.canonical_id

        if ident and ident not in p.identifiers:
            p.identifiers.append(ident)
        # `identifiers` — every OTHER way to reach the same human (a Contacts card carries three
        # emails and two phones). Deterministic writers pass it; the extraction schema does not,
        # because a model listing identifiers is a model inventing them. Storing them all is what
        # lets a message from the second phone resolve to this file instead of forking a new one.
        for extra in upd.get("identifiers") or []:
            e = _s(extra).strip()
            e = e.lower() if "@" in e else e
            if e and e not in p.identifiers:
                p.identifiers.append(e)
        # A real name upgrades a placeholder (cid-as-name) but never overwrites an existing one.
        if name and (not p.name or p.name == p.canonical_id):
            p.name = name

        patch = upd.get("profile_patch") or {}
        if patch.get("linkedin"):
            p.linkedin = patch["linkedin"]
        # Deterministic connector writers only: X's immutable id is typed identity, and handles
        # are observations with history. They never enter the generic email/phone identifier list.
        x_user_id = str(patch.get("x_user_id") or "").strip()
        if x_user_id and x_user_id.isdigit() and (not p.x_user_id or p.x_user_id == x_user_id):
            p.x_user_id = x_user_id
        if patch.get("x_handle"):
            kg.observe_x_handle(p, patch["x_handle"], today)
        if isinstance(patch.get("x_resolution"), dict):
            p.x_resolution = dict(patch["x_resolution"])
        covered = any(fu.get('source') == 'web_research' for fu in upd.get('facts', []))
        for fact_index, fu in enumerate(upd.get("facts", [])):
            outcome = _apply_fact(p, cid, fu, today, counts)
            correction.record_fact_outcome(fact_outcomes, person_index, fact_index, fu, outcome)
            if fu.get('source') == 'web_research':
                covered = covered and fact_establishes_research_coverage(outcome)
            if fact_index == upd.get("profile_support_fact_index") and outcome['disposition'] in ACCEPTED_FACT_DISPOSITIONS:
                correction.apply_profile(p, patch, _s(upd.get('updated_by')), outcome.get('fact_id', ''))
        if 'profile_support_fact_index' not in upd:
            correction.apply_profile(p, patch, _s(upd.get('updated_by')), company_slug=kg.company_slug)

        # Research freshness includes deliberate terminal decisions, but not invalid evidence.
        if upd.get("last_researched") and covered:
            p.last_researched = str(upd["last_researched"])[:10]

        before_archived = sum(1 for f in p.facts.values() if f.status == "archived")
        kg.prune_stale_facts(p.facts, now)
        counts["pruned"] += sum(1 for f in p.facts.values() if f.status == "archived") - before_archived

        p.updated_at = iso
        # Honor the writer's own name, as the company lane already does — a user edit or a research
        # sweep must not masquerade as a brief extraction in the file's provenance stamp.
        p.updated_by = _s(upd.get("updated_by")).strip() or "brief_extraction"
        kg.write_person_file(path, p, now)
        person_files.append(os.path.basename(path))
        # Keep the in-run index current so a later update in this same batch (other channel,
        # other name form) resolves to the file we just wrote instead of creating a duplicate.
        index["by_cid"][p.canonical_id] = path
        for known in p.identifiers:      # every identifier on the file, not just today's
            k = kg.normalize_identifier(known)
            if k:
                index["by_identifier"][k] = path
        if p.x_user_id:
            index.setdefault("by_x_user_id", {})[str(p.x_user_id)] = path
        for item in p.x_handles:
            handle = kg.normalize_x_handle(item.get("handle")) if isinstance(item, dict) else ""
            if handle:
                index.setdefault("by_x_handle", {})[handle] = path
        s = _slug_for(p.name)
        if s:
            index["by_name"][s] = path
        if upd.get("relations"):
            pending_relations.append((p.canonical_id, p.name, upd["relations"]))

    _apply_relations(pending_relations, index, counts, dropped_relation_types, person_files, now)

    company_index = _company_index()
    for upd in extracted.get("company_updates", []):
        # Never use a caller-supplied company_slug raw — always slugify (H2). Then the alias/domain
        # lookup: "YC" resolves INTO the existing "Y Combinator" file instead of minting a second.
        path, slug = resolve_company_path(upd, company_index)
        if not slug:
            continue
        existing_news = []
        about = context = existing_by = ""
        domain = last_news_update = last_researched = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                yaml_str, body = kg.split_frontmatter_body(f.read())
            import yaml as _y
            fm = _y.safe_load(yaml_str) if yaml_str else {}
            secs = _parse_company_body(body)
            existing_news, about, context = secs["news"], secs["about"], secs["context"]
            aliases = (fm or {}).get("aliases") or [upd.get("company_name", "")]
            # Preserve metadata the Mac app relies on (was being erased on rewrite).
            domain = (fm or {}).get("domain")
            last_news_update = (fm or {}).get("last_news_update")
            last_researched = (fm or {}).get("last_researched")
            existing_by = _s((fm or {}).get("updated_by")).strip()
        else:
            aliases = [upd.get("company_name", "")]

        # Prevention, continued: every name form we've now seen for this company becomes a stored
        # alias, so the NEXT brief that says "YC" resolves here instead of forking a second file.
        name_seen = _s(upd.get("company_name")).strip()
        if name_seen and not any(kg.company_slug(_s(a)) == kg.company_slug(name_seen)
                                 for a in aliases):
            aliases = list(aliases) + [name_seen]
        if not domain:
            domain = _company_domain_of(upd) or None
        for a in aliases:
            a_slug = kg.safe_slug(kg.company_slug(_s(a))) if a else None
            if a_slug:
                company_index["by_alias"].setdefault(a_slug, path)
        if domain:
            company_index["by_domain"].setdefault(domain, path)

        existing_urls = {_url_of(n) for n in existing_news if _url_of(n)}
        existing_texts = set(existing_news)
        for item in upd.get("news", []):
            text = item.get("text")
            if not text:
                continue  # extraction sometimes omits text — a missing key must not kill the update
            url = item.get("url")
            if url:
                if url in existing_urls:
                    continue
                existing_urls.add(url)
                formatted = f"[{text}]({url})" + (f" — {item['date']}" if item.get("date") else "")
            else:
                if text in existing_texts:
                    continue
                existing_texts.add(text)
                formatted = text
            existing_news.insert(0, formatted)
        for cu in upd.get("context_updates", []):
            context = (context + "\n" + cu).strip() if context else cu
        # Cap context to the on-disk limit, keeping the most-recent tail at a line boundary
        # (knowledge_files.rs:2666-2671) so the file doesn't grow unbounded.
        if len(context) > kg.MAX_COMPANY_CONTEXT_CHARS:
            start = len(context) - kg.MAX_COMPANY_CONTEXT_CHARS
            nl = context.find("\n", start)
            context = context[nl + 1:] if nl != -1 else context[start:]
        if upd.get("news"):
            last_news_update = iso[:10]
        # `about` is the company's durable identity paragraph — what it builds, who founded it, the
        # market. REPLACE, not append: it is the one thing a later, richer research pass (or a user
        # correction) should be able to say better, and appending would grow it every run. Callers
        # decide whether their `about` is an upgrade (persist_prep asks company_knowledge first);
        # this writer just does what it's told, exactly like profile_patch on a person.
        new_about = _s(upd.get("about")).strip()
        # …with ONE floor under it: a `user_edit` About is a correction the user made by hand, and a
        # pipeline never overwrites it. An empty About, or one owned by any pipeline, is fair game.
        if new_about and about and existing_by == "user_edit" \
                and _s(upd.get("updated_by")).strip() != "user_edit":
            new_about = ""
        if new_about:
            about = new_about[:kg.MAX_COMPANY_CONTEXT_CHARS]
        # `updated_by` on a company names WHO OWNS THE ABOUT — because `about` is the only field a
        # rewrite can destroy (news is append-only and URL-deduped, context is append-only). So an
        # update that doesn't touch the About leaves the stamp alone: a `user_edit` correction
        # survives a later news write, and persist_prep still sees it and declines to overwrite.
        written_by = existing_by or "brief_extraction"
        if new_about:
            written_by = _s(upd.get("updated_by")).strip() or "brief_extraction"
        if upd.get("last_researched"):
            last_researched = str(upd["last_researched"])[:10]

        _write_company(path, slug, aliases, about, existing_news[:kg.MAX_NEWS_ITEMS], context, iso,
                       domain, last_news_update, written_by, last_researched)
        company_files.append(os.path.basename(path))

    # Entity-dedup lite, half two: refresh the SUGGESTIONS after today's writes (a person created
    # minutes ago is exactly the dupe worth catching). Never merges — see suggest_name_merges.
    try:
        suggestions = suggest_name_merges(now) if maintain_identities else []
    except Exception:  # noqa: BLE001 — memory hygiene must never cost the Learn step its writes
        suggestions = []

    return {"applied": counts, "fact_outcomes": fact_outcomes,
            "person_files": person_files, "company_files": company_files,
            "auto_merged": auto_merged, "merge_suggestions": suggestions,
            "dropped_relation_types": sorted(set(dropped_relation_types))}


def _parse_company_body(body: str) -> dict:
    sections: dict = {}
    cur = ""
    for line in body.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip().lower()
            sections.setdefault(cur, [])
        elif cur:
            sections.setdefault(cur, []).append(line)
    join = lambda k: "\n".join(sections.get(k, [])).strip()
    items = lambda k: [l.strip()[2:] for l in sections.get(k, []) if l.strip().startswith("- ")]
    return {"about": join("about"), "news": items("news"), "context": join("context")}


def _url_of(news_line: str):
    i = news_line.find("](")
    if i == -1:
        return None
    j = news_line.find(")", i + 2)
    return news_line[i + 2:j] if j != -1 else None


def _write_company(path, slug, aliases, about, news, context, iso, domain=None,
                   last_news_update=None, updated_by="brief_extraction", last_researched=None):
    import yaml as _y
    # Field order mirrors the Rust CompanyFrontmatter (domain/last_news_update skip-if-none);
    # last_researched is additive and skip-if-none, so an older reader ignores it.
    fm = {"schema": kg.SCHEMA_VERSION, "normalized": slug, "aliases": aliases}
    if domain:
        fm["domain"] = domain
    fm["updated_at"] = iso
    fm["updated_by"] = updated_by or "brief_extraction"
    if last_news_update:
        fm["last_news_update"] = last_news_update
    if last_researched:
        fm["last_researched"] = last_researched
    body = []
    if about:
        body.append("\n## About\n" + about + "\n")
    if news:
        body.append("\n## News\n" + "".join(f"- {n}\n" for n in news))
    if context:
        body.append("\n## Context\n" + context + "\n")
    kg.write_text_atomic(
        path, f"---\n{_y.safe_dump(fm, sort_keys=False, allow_unicode=True)}---\n" + "".join(body))


def main():
    raw = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
    result = apply(json.loads(raw))
    try:  # visibility into the memory loop (served at /debug/brief-log)
        from sotto_log import diag
        a = result["applied"]
        diag(f"[knowledge_update] facts: {a['new']} new, {a['confirmed']} confirmed, "
             f"{a['superseded']} superseded, {a['pruned']} pruned | "
             f"{len(result['person_files'])} people + {len(result['company_files'])} companies written")
        # Relations: what was linked, and what the model invented outside the closed vocabulary.
        if a.get("relations") or result.get("dropped_relation_types"):
            unknown = ", ".join(result.get("dropped_relation_types") or []) or "none"
            diag(f"[knowledge_update] relations: {a.get('relations', 0)} edge(s) linked "
                 f"(both ends) | unknown types dropped: {unknown}")
        # Entity-dedup lite, one line: what was merged (deterministic) and what awaits a human.
        if result.get("auto_merged") or result.get("merge_suggestions"):
            merges = ", ".join(f"{m['from']}→{m['into']} ({m['identifier']})"
                               for m in result.get("auto_merged") or []) or "none"
            diag(f"[knowledge_update] dedup: {len(result.get('auto_merged') or [])} auto-merged "
                 f"[{merges}] | {len(result.get('merge_suggestions') or [])} name-similarity "
                 "suggestion(s) awaiting confirmation")
    except Exception:
        pass
    print(json.dumps(result))


def merge_person_files(dst_path: str, src_path: str, now: datetime | None = None) -> bool:
    """The locked entry point — a human-confirmed merge (knowledge_edit --op merge) must not race a
    brief's apply() over the same person files. jsonstore.lock is reentrant within a process, so
    apply()'s own auto_merge path, which arrives here already holding the lock, nests for free."""
    with graph_lock():
        return _merge_person_files_unlocked(dst_path, src_path, now)


def link_relation(*a, **k):
    """Locked for the same reason as merge_person_files — relations write BOTH people's files."""
    with graph_lock():
        return _link_relation_unlocked(*a, **k)


def unlink_relation(*a, **k):
    with graph_lock():
        return _unlink_relation_unlocked(*a, **k)


def validate_equivalent_groups(groups, facts, conflicts):
    """Validate the entire ID-only plan before any write, including overlap with conflicts."""
    if not isinstance(groups, list) or len(groups) > 8:
        raise ValueError('invalid equivalence groups')
    used = set()
    conflicted = {fid for pair in conflicts for fid in pair}
    for group in groups:
        if (not isinstance(group, list) or not 2 <= len(group) <= 8
                or any(not isinstance(fid, str) or fid not in facts for fid in group)
                or len(set(group)) != len(group) or used.intersection(group)
                or conflicted.intersection(group)):
            raise ValueError('invalid equivalence references')
        used.update(group)


CONFLICT_PEOPLE = 100   # people with stored contradiction pairs; the oldest record is evicted first
from knowledge import CONFLICT_PAIRS, live_conflict_pairs  # noqa: E402, F401




def curation_fact_version(fact: "kg.FactMeta") -> str:
    """Semantic/authority version for bounded curation, independent of sightings and file time."""
    payload = {
        'text': fact.text, 'type': fact.type, 'source': fact.source,
        'source_ref': fact.source_ref,
        'evidence_refs': sorted(json.dumps(ref, sort_keys=True, separators=(',', ':'))
                                for ref in fact.evidence_refs),
        'status': fact.status, 'archived_text': fact.archived_text,
        'archived_reason': fact.archived_reason, 'duplicate_of': fact.duplicate_of,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _conflict_at(record) -> str:
    value = record.get('at') if isinstance(record, dict) else None
    return value if isinstance(value, str) else ''


def consolidate(slug, expected_hash, summary_refs, conflicts, now=None, *, equivalent_groups=None,
                supplied_versions=None, supplied_conflict_revision=None):
    """Apply a Dreamer selection through the graph writer, preserving evidence and human edits.

    The model selects existing fact IDs. It cannot supply replacement prose, confidence,
    instructions, identity, or deletion targets. Equivalents retain their original archived evidence.
    """
    if not kg.valid_canonical_id(slug):
        raise ValueError('invalid person id')
    if (supplied_conflict_revision is not None and
            (not isinstance(supplied_conflict_revision, str) or len(supplied_conflict_revision) != 64
             or any(ch not in '0123456789abcdef' for ch in supplied_conflict_revision))):
        raise ValueError('invalid conflict revision')
    now = now or datetime.now(timezone.utc)
    with graph_lock():
        path = _person_path(slug)
        raw_bytes = Path(path).read_bytes()
        if hashlib.sha256(raw_bytes).hexdigest() != expected_hash:
            return {'status': 'changed_during_review'}
        raw = raw_bytes.decode('utf-8')
        person = kg.parse_person_file(raw)
        active = {fid: fact for fid, fact in person.facts.items() if fact.status == 'active'}
        if supplied_versions is not None:
            if (not isinstance(supplied_versions, dict) or not supplied_versions
                    or any(not isinstance(fid, str) or not isinstance(version, str)
                           or len(version) != 64 for fid, version in supplied_versions.items())):
                raise ValueError('invalid supplied versions')
            if any(fid not in active or curation_fact_version(active[fid]) != version
                   for fid, version in supplied_versions.items()):
                return {'status': 'changed_during_review'}
            scoped = {fid: active[fid] for fid in supplied_versions}
        else:
            scoped = active
        if (not isinstance(summary_refs, list) or len(summary_refs) > 4
                or len(set(summary_refs)) != len(summary_refs)
                or any(fid not in scoped for fid in summary_refs)
                or not isinstance(conflicts, list) or len(conflicts) > 3):
            raise ValueError('invalid consolidation references')
        for pair in conflicts:
            if not isinstance(pair, list) or len(pair) != 2 or len(set(pair)) != 2 or any(fid not in scoped for fid in pair):
                raise ValueError('invalid contradiction references')
        groups = equivalent_groups if equivalent_groups is not None else []
        validate_equivalent_groups(groups, scoped, conflicts)
        # A summary from one bounded window identifies only its submitted references. Other
        # assertions remain in storage and retrieval; a later window may replace this derived view.
        protected_summary = kg.summary_protected(person)
        conflict_path = os.path.join(kg.data_root(), 'knowledge', 'conflicts.json')
        # Lock ordering is graph -> conflicts, matching the writer. Conflict storage is bounded by
        # eviction, never by refusal: a full store must not stop curation of the next person.
        with jsonstore.lock(conflict_path):
            state = jsonstore.read(conflict_path, default={'people': {}}, strict=True)
            if not isinstance(state, dict) or not isinstance(state.get('people'), dict):
                raise ValueError('invalid conflict state')
            people = state['people']
            previous = people.get(slug, {})
            if not isinstance(previous, dict):
                return {'status': 'invalid_conflict_state'}
            previous_pairs = previous.get('pairs', [])
            if (not isinstance(previous_pairs, list)
                    or any(not isinstance(pair, list) or len(pair) != 2
                           or any(not isinstance(fid, str) for fid in pair)
                           or len(set(pair)) != 2 for pair in previous_pairs)):
                return {'status': 'invalid_conflict_state'}
            current_revision = hashlib.sha256(json.dumps(previous_pairs, sort_keys=True).encode()).hexdigest()
            if supplied_conflict_revision is not None and current_revision != supplied_conflict_revision:
                return {'status': 'changed_during_review'}
            scoped_ids = set(scoped)
            # Keep an unreviewed pair only while both of its facts are still active; a pair whose
            # side was edited or archived is already invisible to every reader and is dropped here.
            preserved = ([pair for pair in live_conflict_pairs(person, previous_pairs)
                          if not set(pair) <= scoped_ids]
                         if supplied_versions is not None else [])
            candidates, pair_keys = [], set()
            for pair in [*conflicts, *preserved]:   # this review's findings first, then older ones
                key = tuple(sorted(pair))
                if key not in pair_keys:
                    candidates.append(pair)
                    pair_keys.add(key)
            merged = candidates[:CONFLICT_PAIRS]
            duplicates, seen, representatives = 0, {}, {}
            # Every live candidate's facts stay unfolded in this write, including one evicted for room.
            conflict_endpoints = {fid for pair in candidates for fid in pair}
            protected_refs = (set(person.summary_refs) if protected_summary else set()) | conflict_endpoints
            # The chosen existing fact keeps its own provenance, dates, seen count and confidence.
            # Other assertions remain in the file with their independent source/consent ownership.
            # Never pool source refs across grants or make repeated paraphrases stronger evidence.
            # Keeper rule: a user correction, else the most corroborated copy (seen, then effective
            # confidence); the model's order only breaks ties. Its wording preference must not archive
            # a corroborated copy (never age-pruned) behind a heard-once one that ages out at day 60.
            def rank(fid):
                fact = active[fid]
                return (fact.source == 'user_edit', fact.seen, kg.effective_confidence(fact, now))

            def fold(fid, keeper):
                nonlocal duplicates
                fact = active[fid]
                fact.status, fact.archived_text, fact.archived_reason = 'archived', fact.text, 'duplicate'
                fact.duplicate_of = representatives[fid] = keeper
                duplicates += 1

            for group in groups:
                keeper = max(group, key=rank)  # max() keeps the first of equals: model order breaks ties
                for fid in group:
                    if fid != keeper and active[fid].source != 'user_edit' and fid not in protected_refs:
                        fold(fid, keeper)
            for fid in sorted(scoped, key=lambda fid: (tuple(-v for v in rank(fid)), fid)):
                fact = scoped[fid]
                if fact.status != 'active':
                    continue
                key = (fact.type, ' '.join(fact.text.split()).casefold())
                if key in seen and fact.source != 'user_edit' and fid not in protected_refs:
                    fold(fid, seen[key])
                else:
                    seen[key] = fid
            # A bare contact-note number is not an assertion. Keep the original in the archive,
            # but stop displaying it as a learned fact. Human-authored facts are never filtered.
            for fid, fact in scoped.items():
                if (fact.status == 'active' and fact.source == 'apple_contacts'
                        and not any(c.isalpha() for c in fact.text) and fid not in protected_refs):
                    fact.status, fact.archived_text, fact.archived_reason = 'archived', fact.text, 'non_assertion'
            if not protected_summary:
                def representative(fid):
                    while fid in representatives:
                        fid = representatives[fid]
                    return fid
                summary_refs = list(dict.fromkeys(representative(fid) for fid in summary_refs))
                summary_refs = [fid for fid in summary_refs if active[fid].status == 'active']
                person.summary_refs = summary_refs
                person.summary = ' '.join(person.facts[fid].text for fid in person.summary_refs)
            # Curation is not a new observation: preserve each fact's date/confidence and file recency.
            kg.write_person_file(path, person, now)
            written_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            if merged:
                people.pop(slug, None)   # re-inserted as the newest record
                at = kg.now_iso(now)
                while len(people) >= CONFLICT_PEOPLE:
                    people.pop(min(people, key=lambda cid: (_conflict_at(people[cid]), cid)))
                people[slug] = {'pairs': merged, 'at': at}
            else:
                people.pop(slug, None)
            jsonstore.write_atomic(conflict_path, state)
            return {'status': 'ok', 'duplicates': duplicates, 'conflicts': len(merged),
                    'summary_facts': len(person.summary_refs), 'protected_summary': bool(protected_summary),
                    'hash': written_hash,
                    'committed_versions': {fid: curation_fact_version(person.facts[fid]) for fid in scoped}}


# The LAST lines of this file, on purpose. This guard once sat above the three locked wrappers —
# fine for every test (imports execute the whole file before calling anything) and broken for
# PRODUCTION, where the CLI runs as __main__ and main() executed before merge_person_files /
# link_relation existed: every relation and auto-merge in a brief's Learn step was silently lost
# (caught by an external review, Aug 31 — the silent kind, exit 0, "relations: 0"). Any def added
# below this guard recreates that bug; the CLI regression test runs this file AS A SCRIPT to pin it.
if __name__ == "__main__":
    main()
