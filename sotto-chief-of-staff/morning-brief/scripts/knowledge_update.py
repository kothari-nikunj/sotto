#!/usr/bin/env python3
"""
knowledge_update.py — apply a brief's extracted_knowledge to the people/company graph.

PORT SOURCE: app/src-tauri/src/database/knowledge_files.rs::save_knowledge_extraction (line 2453)
Run by Hermes via execute_code after compose_brief. Reads/writes knowledge/*.md on $SOTTO_DATA.

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
     emails, or two phones, with nothing in common — suppress the suggestion entirely.
  3. COMPANIES GET PREVENTION, NOT REPAIR. Company files are keyed by name-slug with aliases and a
     domain stored but never consulted, so "YC" and "Y Combinator" became two files. Resolution now
     consults both (alias-slug and domain) before minting a new file, every company_name seen is
     recorded as an alias, and compose_brief feeds the known company names back into the extraction
     prompt so the model stops free-forming the name in the first place.
"""
from __future__ import annotations

import difflib
import glob
import json
import os
import sys
from datetime import datetime, timezone

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "lib"))
import knowledge as kg  # noqa: E402

# Dedup-lite tuning. Kept small and boring: the suggestion list is a human's to-do list, not a
# report, and the pairwise scan must never become the Learn step's cost centre.
MERGE_SUGGESTIONS_MAX = 10          # the file holds at most this many open suggestions
MERGE_SUGGEST_MAX_FILES = 300       # above this the O(n²) name scan is skipped entirely
MERGE_SUGGEST_MAX_BUCKET = 40       # a name-prefix shared by more people than this is noise
NAME_SIMILARITY_MIN = 0.9           # difflib ratio over name slugs, when neither contains the other
MERGE_SUGGESTIONS_FILE = "merge_suggestions.json"


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
    return False


def merge_person_files(dst_path: str, src_path: str, now: datetime | None = None) -> bool:
    """Union src INTO dst and delete src — kg.merge_person + os.remove, migrate_people_dir's exact
    mechanics (nothing is dropped; dst wins ties). Returns False for a no-op self-merge."""
    if os.path.realpath(dst_path) == os.path.realpath(src_path):
        return False
    with open(dst_path, encoding="utf-8") as f:
        dst = kg.parse_person_file(f.read())
    with open(src_path, encoding="utf-8") as f:
        src = kg.parse_person_file(f.read())
    kg.merge_person(dst, src)
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write(kg.serialize_person_file(dst, now))
    os.remove(src_path)
    return True


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


def _write_merge_suggestions(items: list, iso: str) -> None:
    path = _suggestions_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated_at": iso, "suggestions": items[:MERGE_SUGGESTIONS_MAX]}, f, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def suggest_name_merges(now: datetime | None = None) -> list:
    """Refresh knowledge/merge_suggestions.json and return its entries.

    SUGGEST-ONLY BY CONSTRUCTION — this function never merges anything. A pair qualifies when the
    names are close (containment or a high ratio) AND their identifiers do not conflict; anything
    with a positive identifier contradiction (the two-John-Smiths shape) is dropped, so the
    suggestion list can never reinforce the name-slug bug it exists to clean up. Stale entries
    (either file gone — usually because the merge happened) are pruned on every pass, and
    first_seen survives so the dashboard can age them."""
    now = now or datetime.now(timezone.utc)
    people = _load_people()
    prior = {(_s(x.get("from")), _s(x.get("into"))): x for x in load_merge_suggestions()}
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
            if key in seen:
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
    """Forget a suggestion once it has been acted on (either direction). Best-effort."""
    pair = {(src_slug, dst_slug), (dst_slug, src_slug)}
    kept = [s for s in load_merge_suggestions() if (_s(s.get("from")), _s(s.get("into"))) not in pair]
    _write_merge_suggestions(kept, kg.now_iso(now or datetime.now(timezone.utc)))


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


def apply(extracted: dict, now: datetime | None = None) -> dict:
    # UTC, not server-local: now_iso labels the timestamp with a 'Z' suffix, so feeding it local
    # time would write a lie into every updated_at in the graph.
    now = now or datetime.now(timezone.utc)
    today = kg.today_str(now)
    iso = kg.now_iso(now)
    counts = {"new": 0, "confirmed": 0, "superseded": 0, "pruned": 0}
    person_files: list[str] = []
    company_files: list[str] = []

    os.makedirs(kg.people_dir(), exist_ok=True)
    os.makedirs(kg.companies_dir(), exist_ok=True)

    # Files are keyed by canonical_id, with legacy name-slug files auto-migrated once (idempotent).
    # An existing person is found canonical_id → identifier → name, so "Sarah" texting and
    # "Sarah Chen" emailing land in ONE file instead of fragmenting the graph per name form.
    kg.migrate_people_dir(now)
    # Entity-dedup lite, half one: two files sharing an exact identifier are one human. Runs HERE,
    # piggybacking the migration call, so the day's updates land in the merged file rather than
    # re-splitting it — and BEFORE the index is built, so the index can't hand back a path that
    # this pass just deleted.
    auto_merged = auto_merge_by_identifier(now)
    index = kg.build_people_index()

    for upd in extracted.get("person_updates", []):
        ident = (upd.get("identifier") or "").strip()
        ident = ident.lower() if "@" in ident else ident
        name = upd.get("person_name") or ""
        cid = upd.get("canonical_id") or ""
        if not kg.valid_canonical_id(cid):
            cid = ""  # malformed/LLM-invented id → resolve by identifier/name instead
        if not cid and not ident and not _slug_for(name):
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
            if not cid:
                cid = kg.default_canonical_id(name, [ident] if ident else [])
            path = kg.safe_path(kg.people_dir(), cid)
            p = kg.PersonFile(
                canonical_id=cid, name=name or cid,
                identifiers=[ident] if ident else [],
                updated_at=iso, updated_by="brief_extraction",
            )

        if ident and ident not in p.identifiers:
            p.identifiers.append(ident)
        # A real name upgrades a placeholder (cid-as-name) but never overwrites an existing one.
        if name and (not p.name or p.name == p.canonical_id):
            p.name = name

        patch = upd.get("profile_patch") or {}
        if patch.get("title"):
            p.title = patch["title"]
        if patch.get("company"):
            new_slug = kg.company_slug(patch["company"]).replace("-", "")
            cur_slug = kg.company_slug(p.company).replace("-", "") if p.company else None
            if cur_slug != new_slug:
                p.company = patch["company"]
        if patch.get("linkedin"):
            p.linkedin = patch["linkedin"]
        # Research writers (persist_prep / prewarm_graph) stamp when they actually researched this
        # person; persist_prep.profile_is_fresh keys ONLY off this (file mtime is bumped by every
        # brief rewrite and says nothing about research recency).
        if upd.get("last_researched"):
            p.last_researched = str(upd["last_researched"])[:10]

        for fu in upd.get("facts", []):
            if float(fu.get("confidence", 0.8)) < 0.5:
                continue
            force = fu.get("change_type") == "correction"
            action, existing_id = kg.find_similar_fact(
                p.facts, fu["fact"], fu.get("memory_type", ""), force
            )
            if action == kg.BUMP:
                ex = p.facts[existing_id]
                ex.seen += 1
                ex.conf = min(ex.conf + 0.1, 1.0)
                ex.last = today
                counts["confirmed"] += 1
            elif action == kg.SUPERSEDE:
                ex = p.facts[existing_id]
                ex.status = "archived"
                ex.archived_text = ex.text
                fid = kg.generate_fact_id(cid, fu["fact"], today)
                p.facts[fid] = kg.FactMeta(
                    text=fu["fact"], type=fu.get("memory_type", ""), status="active",
                    seen=1, conf=float(fu.get("confidence", 0.8)),
                    # provenance: research writers (persist_prep) pass source="web_research";
                    # extraction writes omit it and keep the default label.
                    source=fu.get("source") or "brief_extraction",
                    source_ref=fu.get("source_ref", ""), first=today, last=today,
                )
                counts["superseded"] += 1
                counts["new"] += 1
            elif action == kg.NEW:
                fid = kg.generate_fact_id(cid, fu["fact"], today)
                p.facts[fid] = kg.FactMeta(
                    text=fu["fact"], type=fu.get("memory_type", ""), status="active",
                    seen=1, conf=float(fu.get("confidence", 0.8)),
                    source=fu.get("source") or "brief_extraction",
                    source_ref=fu.get("source_ref", ""), first=today, last=today,
                )
                counts["new"] += 1
            # SKIP: silently ignore

        before_archived = sum(1 for f in p.facts.values() if f.status == "archived")
        kg.prune_stale_facts(p.facts, now)
        counts["pruned"] += sum(1 for f in p.facts.values() if f.status == "archived") - before_archived

        p.updated_at = iso
        p.updated_by = "brief_extraction"
        with open(path, "w", encoding="utf-8") as f:
            f.write(kg.serialize_person_file(p, now))
        person_files.append(os.path.basename(path))
        # Keep the in-run index current so a later update in this same batch (other channel,
        # other name form) resolves to the file we just wrote instead of creating a duplicate.
        index["by_cid"][p.canonical_id] = path
        if ident:
            k = kg.normalize_identifier(ident)
            if k:
                index["by_identifier"][k] = path
        s = _slug_for(p.name)
        if s:
            index["by_name"][s] = path

    company_index = _company_index()
    for upd in extracted.get("company_updates", []):
        # Never use a caller-supplied company_slug raw — always slugify (H2). Then the alias/domain
        # lookup: "YC" resolves INTO the existing "Y Combinator" file instead of minting a second.
        path, slug = resolve_company_path(upd, company_index)
        if not slug:
            continue
        existing_news = []
        about = context = ""
        domain = last_news_update = None
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

        _write_company(path, slug, aliases, about, existing_news[:kg.MAX_NEWS_ITEMS], context, iso,
                       domain, last_news_update)
        company_files.append(os.path.basename(path))

    # Entity-dedup lite, half two: refresh the SUGGESTIONS after today's writes (a person created
    # minutes ago is exactly the dupe worth catching). Never merges — see suggest_name_merges.
    try:
        suggestions = suggest_name_merges(now)
    except Exception:  # noqa: BLE001 — memory hygiene must never cost the Learn step its writes
        suggestions = []

    return {"applied": counts, "person_files": person_files, "company_files": company_files,
            "auto_merged": auto_merged, "merge_suggestions": suggestions}


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


def _write_company(path, slug, aliases, about, news, context, iso, domain=None, last_news_update=None):
    import yaml as _y
    # Field order mirrors the Rust CompanyFrontmatter (domain/last_news_update skip-if-none).
    fm = {"schema": kg.SCHEMA_VERSION, "normalized": slug, "aliases": aliases}
    if domain:
        fm["domain"] = domain
    fm["updated_at"] = iso
    fm["updated_by"] = "brief_extraction"
    if last_news_update:
        fm["last_news_update"] = last_news_update
    body = []
    if about:
        body.append("\n## About\n" + about + "\n")
    if news:
        body.append("\n## News\n" + "".join(f"- {n}\n" for n in news))
    if context:
        body.append("\n## Context\n" + context + "\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{_y.safe_dump(fm, sort_keys=False, allow_unicode=True)}---\n" + "".join(body))


def main():
    raw = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
    result = apply(json.loads(raw))
    try:  # visibility into the memory loop (served at /debug/brief-log)
        from sotto_log import diag
        a = result["applied"]
        diag(f"[knowledge_update] facts: {a['new']} new, {a['confirmed']} confirmed, "
             f"{a['superseded']} superseded, {a['pruned']} pruned | "
             f"{len(result['person_files'])} people + {len(result['company_files'])} companies written")
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


if __name__ == "__main__":
    main()
