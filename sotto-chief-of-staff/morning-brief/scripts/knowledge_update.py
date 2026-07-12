#!/usr/bin/env python3
"""
knowledge_update.py — apply a brief's extracted_knowledge to the people/company graph.

PORT SOURCE: app/src-tauri/src/database/knowledge_files.rs::save_knowledge_extraction (line 2453)
Run by Hermes via execute_code after compose_brief. Reads/writes knowledge/*.md on $SOTTO_DATA.

Usage:
    knowledge_update.py < extracted.json          # {"person_updates":[...], "company_updates":[...]}
    knowledge_update.py extracted.json
Prints a JSON diff: {"applied":{"new","confirmed","superseded","pruned"}, "person_files":[...], ...}
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "lib"))
import knowledge as kg  # noqa: E402


def _slug_for(name_or_id: str):
    # Always slugify; never fall back to the raw value (path-traversal guard, H2).
    return kg.safe_slug(name_or_id)


def apply(extracted: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now()
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
                    seen=1, conf=float(fu.get("confidence", 0.8)), source="brief_extraction",
                    source_ref=fu.get("source_ref", ""), first=today, last=today,
                )
                counts["superseded"] += 1
                counts["new"] += 1
            elif action == kg.NEW:
                fid = kg.generate_fact_id(cid, fu["fact"], today)
                p.facts[fid] = kg.FactMeta(
                    text=fu["fact"], type=fu.get("memory_type", ""), status="active",
                    seen=1, conf=float(fu.get("confidence", 0.8)), source="brief_extraction",
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

    for upd in extracted.get("company_updates", []):
        # Never use a caller-supplied company_slug raw — always slugify (H2).
        slug = kg.safe_slug(upd.get("company_slug") or kg.company_slug(upd.get("company_name", "")))
        if not slug:
            continue
        path = kg.safe_path(kg.companies_dir(), slug)
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

        existing_urls = {_url_of(n) for n in existing_news if _url_of(n)}
        existing_texts = set(existing_news)
        for item in upd.get("news", []):
            url = item.get("url")
            if url:
                if url in existing_urls:
                    continue
                existing_urls.add(url)
                formatted = f"[{item['text']}]({url})" + (f" — {item['date']}" if item.get("date") else "")
            else:
                if item["text"] in existing_texts:
                    continue
                existing_texts.add(item["text"])
                formatted = item["text"]
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

    return {"applied": counts, "person_files": person_files, "company_files": company_files}


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
    except Exception:
        pass
    print(json.dumps(result))


if __name__ == "__main__":
    main()
