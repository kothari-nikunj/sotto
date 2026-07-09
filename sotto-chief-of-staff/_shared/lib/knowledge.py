"""
Knowledge-graph core — faithful Python port of Sotto's knowledge-graph logic.

PORT SOURCE: app/src-tauri/src/database/knowledge_files.rs (parent dailybrief repo)
Carries the exact thresholds/algorithms (cite line numbers in comments) so the
people/company .md exhaust stays schema-compatible with today's Sotto files.

Used by: knowledge_update.py (apply extraction), knowledge_query.py (pack for LLM).
No external deps beyond PyYAML (yaml). Pure functions over (inputs, exhaust dir).
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import yaml  # PyYAML

# ── Constants (knowledge_files.rs:20-35) ──────────────────────────────────────
SCHEMA_VERSION = 1
MAX_FACTS_FOR_LLM = 15
MAX_FACTS_COMPACT = 5
MAX_TALKING_POINTS_FOR_LLM = 5
MAX_RECENT_ACTIVITY_FOR_LLM = 3
MAX_NEWS_FOR_LLM = 5
MAX_NEWS_ITEMS = 15              # knowledge_files.rs:21 — on-disk company news cap
MAX_COMPANY_CONTEXT_CHARS = 1000  # knowledge_files.rs:31 — on-disk company context cap
NOTES_EXCERPT_CHARS = 300
PRUNE_STALE_AFTER_DAYS = 60
CONFIDENCE_DECAY_PER_WEEK = 0.08
CONFIDENCE_FLOOR = 0.4

# Mutable fact types that may be superseded on medium similarity (knowledge_files.rs:1276)
MUTABLE_TYPES = {"relationship_change", "working_style", "milestone", "context"}

# Dedup stop-words (knowledge_files.rs:1248-1254)
STOP_WORDS = {
    "the", "is", "are", "was", "were", "been", "being",
    "has", "have", "had", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "for",
    "with", "from", "and", "but", "not", "that",
    "this", "its", "their", "his", "her", "they", "she",
}

# Company-name suffixes stripped during normalization (knowledge_files.rs:246-251)
_COMPANY_SUFFIXES = [
    ", inc.", ", inc", " inc.", " inc", ", llc", " llc",
    ", corp.", " corp.", ", corp", " corp", ", ltd.", " ltd.",
    ", ltd", " ltd", ", co.", " co.", " company",
    ", gmbh", " gmbh", " plc", ", plc",
    ".ai", ".io", ".co", ".com", ".dev", ".tech", ".app",
    " ai", " io", " co", " hq", " labs", " tech", " app", " dev",
]


def today_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Slug / normalization (knowledge_files.rs:233-282) ─────────────────────────
def slugify(name: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c == " ") else " " for c in name.lower())
    return "-".join(cleaned.split())


def normalize_company_name(name: str) -> str:
    result = name.strip().lower()
    for suffix in _COMPANY_SUFFIXES:
        if result.endswith(suffix):
            result = result[: -len(suffix)]
    return result


def company_slug(name: str) -> str:
    return slugify(normalize_company_name(name))


def safe_slug(value: str) -> Optional[str]:
    """Slugify and reject anything that can't be a safe single-segment filename.
    Never falls back to the raw value (defends against path traversal from LLM/message-derived
    names, e.g. '../../etc/x'). Returns None when there's no usable slug."""
    s = slugify(value or "")
    if not s or "/" in s or "\\" in s or s in (".", ".."):
        return None
    return s


def safe_path(directory: str, slug: str) -> str:
    """Join + assert the result stays inside `directory`. Raises ValueError on escape."""
    base = os.path.realpath(directory)
    full = os.path.realpath(os.path.join(base, f"{slug}.md"))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError(f"unsafe path for slug {slug!r}")
    return full


# ── Fact model ────────────────────────────────────────────────────────────────
@dataclass
class FactMeta:
    text: str = ""
    archived_text: Optional[str] = None
    type: str = ""
    status: str = "active"
    seen: int = 1
    conf: float = 0.8
    source: str = ""
    source_ref: str = ""
    first: str = ""
    last: str = ""

    def to_yaml_dict(self) -> dict:
        # Field order + skip-if-none mirrors serde (knowledge_files.rs:50-70)
        d: dict = {"text": self.text}
        if self.archived_text is not None:
            d["archived_text"] = self.archived_text
        d["type"] = self.type
        d["status"] = self.status
        d["seen"] = self.seen
        d["conf"] = self.conf
        d["source"] = self.source
        d["source_ref"] = self.source_ref
        d["first"] = self.first
        d["last"] = self.last
        return d

    @staticmethod
    def from_yaml_dict(d: dict) -> "FactMeta":
        return FactMeta(
            text=d.get("text", ""),
            archived_text=d.get("archived_text"),
            type=d.get("type", ""),
            status=d.get("status", "active"),
            seen=int(d.get("seen", 1)),
            conf=float(d.get("conf", 0.8)),
            source=d.get("source", ""),
            source_ref=d.get("source_ref", ""),
            first=d.get("first", ""),
            last=d.get("last", ""),
        )


@dataclass
class PersonFile:
    canonical_id: str = ""
    name: str = ""
    company: Optional[str] = None
    title: Optional[str] = None
    identifiers: list = field(default_factory=list)
    linkedin: Optional[str] = None
    last_researched: Optional[str] = None
    updated_at: str = ""
    updated_by: str = ""
    schema: int = SCHEMA_VERSION
    facts: dict = field(default_factory=dict)  # fact_id -> FactMeta
    summary: str = ""
    talking_points: list = field(default_factory=list)
    recent_activity: list = field(default_factory=list)
    notes: str = ""


# ── Dedup (knowledge_files.rs:1257-1303) ──────────────────────────────────────
def make_dedupe_key(fact: str) -> set:
    return {
        w for w in re.split(r"[^0-9a-z]+", fact.lower())
        if len(w) > 2 and w not in STOP_WORDS
    }


# DedupResult sentinels
BUMP, SUPERSEDE, NEW, SKIP = "bump", "supersede", "new", "skip"


def find_similar_fact(facts: dict, new_text: str, new_type: str, force_correction: bool):
    """Returns (action, existing_id|None). Mirrors find_similar_fact()."""
    new_words = make_dedupe_key(new_text)
    if not new_words:
        return (NEW, None)
    for fid, existing in facts.items():
        existing_words = make_dedupe_key(existing.text)
        if not existing_words:
            continue
        overlap = len(new_words & existing_words)
        smaller = min(len(new_words), len(existing_words))
        if smaller == 0:
            continue
        ratio = overlap / smaller
        if ratio > 0.5:
            if existing.status == "archived":
                return (SKIP, None)
            return (BUMP, fid)
        if 0.3 <= ratio <= 0.5:
            if force_correction or new_type in MUTABLE_TYPES:
                if existing.status == "archived":
                    return (SKIP, None)
                return (SUPERSEDE, fid)
    return (NEW, None)


def generate_fact_id(canonical_id: str, text: str, timestamp: str) -> str:
    h = hashlib.sha256(f"{canonical_id}|{text}|{timestamp}".encode()).digest()
    return "f_" + "".join(f"{b:02x}" for b in h[:5])


def generate_canonical_id(seed: str) -> str:
    # knowledge_files.rs:3150 — c_ + 12-hex prefix (6 bytes), so cold-start ids match the Mac app's.
    h = hashlib.sha256(seed.encode()).digest()
    return "c_" + "".join(f"{b:02x}" for b in h[:6])


# ── Decay / prune (knowledge_files.rs:507-525) ────────────────────────────────
def effective_confidence(fact: FactMeta, now: Optional[datetime] = None) -> float:
    today = (now or datetime.now()).date()
    try:
        last = datetime.strptime(fact.last, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        last = today
    days_since = max((today - last).days, 0)
    weeks = days_since / 7.0
    return max(fact.conf - weeks * CONFIDENCE_DECAY_PER_WEEK, CONFIDENCE_FLOOR)


def prune_stale_facts(facts: dict, now: Optional[datetime] = None) -> None:
    cutoff = ((now or datetime.now()) - timedelta(days=PRUNE_STALE_AFTER_DAYS)).strftime("%Y-%m-%d")
    for fact in facts.values():
        if fact.status == "active" and fact.seen <= 1 and fact.last < cutoff:
            fact.status = "archived"
            fact.archived_text = fact.text


def sorted_active_facts(facts: dict, now: Optional[datetime] = None):
    items = [(fid, f) for fid, f in facts.items() if f.status != "archived"]
    items.sort(key=lambda kv: (
        -effective_confidence(kv[1], now),
        _neg_str(kv[1].last),   # last DESC
        kv[1].first,            # first ASC
        kv[0],                  # id ASC
    ))
    return items


def _neg_str(s: str):
    # sort strings descending by mapping to a reverse-comparable key
    return tuple(-ord(c) for c in s)


# ── Person .md serialize / parse (knowledge_files.rs:370-616) ─────────────────
def split_frontmatter_body(content: str):
    if not content.startswith("---\n") and not content.startswith("---\r\n"):
        return (None, content)
    after = content[5:] if content.startswith("---\r\n") else content[4:]
    for sep, off in (("\n---\n", 5), ("\n---\r\n", 6)):
        idx = after.find(sep)
        if idx != -1:
            return (after[:idx], after[idx + off:])
    if after.endswith("\n---"):
        return (after[:-4], "")
    return (None, content)


def _parse_body(body: str) -> dict:
    sections: dict = {}
    cur = ""
    for line in body.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip().lower()
            sections.setdefault(cur, [])
        elif cur:
            sections.setdefault(cur, []).append(line)

    def join(key):
        return "\n".join(sections.get(key, [])).strip()

    def items(key):
        return [l.strip()[2:] for l in sections.get(key, []) if l.strip().startswith("- ")]

    return {
        "summary": join("summary"),
        "talking_points": items("talking points"),
        "recent_activity": items("recent activity"),
        "notes": join("notes"),
    }


def parse_person_file(content: str) -> PersonFile:
    yaml_str, body = split_frontmatter_body(content)
    fm = yaml.safe_load(yaml_str) if yaml_str else {}
    fm = fm or {}
    facts = {fid: FactMeta.from_yaml_dict(fd) for fid, fd in (fm.get("facts") or {}).items()}
    b = _parse_body(body)
    return PersonFile(
        canonical_id=fm.get("canonical_id", ""),
        name=fm.get("name", ""),
        company=fm.get("company"),
        title=fm.get("title"),
        identifiers=list(fm.get("identifiers") or []),
        linkedin=fm.get("linkedin"),
        last_researched=fm.get("last_researched"),
        updated_at=fm.get("updated_at", ""),
        updated_by=fm.get("updated_by", ""),
        schema=int(fm.get("schema", SCHEMA_VERSION)),
        facts=facts,
        summary=b["summary"],
        talking_points=b["talking_points"],
        recent_activity=b["recent_activity"],
        notes=b["notes"],
    )


def _person_frontmatter_dict(p: PersonFile) -> dict:
    d: dict = {"schema": p.schema, "canonical_id": p.canonical_id, "name": p.name}
    if p.company is not None:
        d["company"] = p.company
    if p.title is not None:
        d["title"] = p.title
    d["identifiers"] = p.identifiers
    if p.linkedin is not None:
        d["linkedin"] = p.linkedin
    if p.last_researched is not None:
        d["last_researched"] = p.last_researched
    d["updated_at"] = p.updated_at
    d["updated_by"] = p.updated_by
    d["facts"] = {fid: f.to_yaml_dict() for fid, f in p.facts.items()}
    return d


def serialize_person_file(p: PersonFile, now: Optional[datetime] = None) -> str:
    yaml_str = yaml.safe_dump(_person_frontmatter_dict(p), sort_keys=False, allow_unicode=True)
    out = []
    if p.summary:
        out.append("\n## Summary\n" + p.summary + "\n")
    active = sorted_active_facts(p.facts, now)
    if active:
        out.append("\n## Facts\n" + "".join(f"- {f.text}\n" for _, f in active))
    if p.talking_points:
        out.append("\n## Talking Points\n" + "".join(f"- {tp}\n" for tp in p.talking_points))
    if p.recent_activity:
        out.append("\n## Recent Activity\n" + "".join(f"- {ra}\n" for ra in p.recent_activity))
    if p.notes:
        out.append("\n## Notes\n" + p.notes + "\n")
    return f"---\n{yaml_str}---\n" + "".join(out)


# ── Exhaust dir helpers ───────────────────────────────────────────────────────
def data_root() -> str:
    return os.environ.get("SOTTO_DATA", "/data")


def people_dir() -> str:
    return os.path.join(data_root(), "knowledge", "people")


def companies_dir() -> str:
    return os.path.join(data_root(), "knowledge", "companies")


# ── Person-file identity resolution (canonical_id-keyed store) ────────────────
# People files are keyed by canonical_id ({cid}.md), NOT by name slug. Name-slug keying was the
# identity-fragmentation root cause: two different "John Smith"s merged into one file, while one
# person whose name differs by channel ("Sarah" on iMessage vs "Sarah Chen" on email) split into
# two files. Every entry point resolves an existing person canonical_id → identifier → name, and
# migrate_people_dir() idempotently re-keys legacy name-slug files on first run.

def normalize_identifier(idv: str) -> str:
    """Mirror of textutil._normalize_identifier so file-store keys line up with the brief pipeline:
    phone-ish strings → last-10 digits; everything else (emails) → lowercase trimmed."""
    trimmed = (idv or "").strip().lower()
    before_at = re.sub(r"@.*", "", trimmed)
    if re.fullmatch(r"[\d\s\-\+\(\)]+", before_at or ""):
        digits = re.sub(r"\D", "", trimmed)
        return digits[-10:]
    return trimmed


def valid_canonical_id(cid: str) -> bool:
    """A usable canonical_id ('c_' + hex). LLM/extraction-supplied ids that don't match are treated
    as absent — they'd otherwise become filenames."""
    return bool(re.fullmatch(r"c_[0-9a-f]{6,64}", cid or ""))


def default_canonical_id(name: str, identifiers: list) -> str:
    """The shared cold-start id scheme (matches knowledge_update / style_extract / prewarm_graph):
    sha256('kf:{name}|{first_email}') else sha256('kf:{name}')."""
    email = next((str(i).strip().lower() for i in identifiers or [] if "@" in str(i)), None)
    seed = f"kf:{name}|{email}" if email else f"kf:{name}"
    return generate_canonical_id(seed)


def merge_person(dst: "PersonFile", src: "PersonFile") -> "PersonFile":
    """Union two files describing the SAME person (same canonical_id) — used when migration finds a
    person split across two legacy name-slug files. dst wins ties; nothing is dropped."""
    for i in src.identifiers:
        if i and i not in dst.identifiers:
            dst.identifiers.append(i)
    for fid, f in src.facts.items():
        if fid not in dst.facts:
            dst.facts[fid] = f
    for tp in src.talking_points:
        if tp not in dst.talking_points:
            dst.talking_points.append(tp)
    for ra in src.recent_activity:
        if ra not in dst.recent_activity:
            dst.recent_activity.append(ra)
    if not dst.name:
        dst.name = src.name
    dst.company = dst.company or src.company
    dst.title = dst.title or src.title
    dst.linkedin = dst.linkedin or src.linkedin
    if (src.last_researched or "") > (dst.last_researched or ""):
        dst.last_researched = src.last_researched
    if len(src.summary) > len(dst.summary):
        dst.summary = src.summary
    if len(src.notes) > len(dst.notes):
        dst.notes = src.notes
    if (src.updated_at or "") > (dst.updated_at or ""):
        dst.updated_at = src.updated_at
    return dst


def migrate_people_dir(now: Optional[datetime] = None) -> dict:
    """Idempotent re-key of people/*.md from legacy name-slug filenames to {canonical_id}.md.
    Files with no (or invalid) canonical_id get one generated from name + identifiers and written
    back. Two legacy files that resolve to the SAME canonical_id are merged. Safe to call on every
    entry point — a migrated dir is a cheap no-op scan. Returns {"moved", "merged"}."""
    import glob as _glob
    moved = merged = 0
    d = people_dir()
    if not os.path.isdir(d):
        return {"moved": 0, "merged": 0}
    for path in sorted(_glob.glob(os.path.join(d, "*.md"))):
        try:
            with open(path, encoding="utf-8") as f:
                p = parse_person_file(f.read())
            fallback_name = os.path.splitext(os.path.basename(path))[0].replace("-", " ")
            changed = False
            if not valid_canonical_id(p.canonical_id):
                p.canonical_id = default_canonical_id(p.name or fallback_name, p.identifiers)
                changed = True
            target = safe_path(d, p.canonical_id)
            if os.path.realpath(path) == target:
                if changed:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(serialize_person_file(p, now))
                continue
            if os.path.exists(target):
                with open(target, encoding="utf-8") as f:
                    dst = parse_person_file(f.read())
                merge_person(dst, p)
                with open(target, "w", encoding="utf-8") as f:
                    f.write(serialize_person_file(dst, now))
                os.remove(path)
                merged += 1
            elif changed:
                with open(target, "w", encoding="utf-8") as f:
                    f.write(serialize_person_file(p, now))
                os.remove(path)
                moved += 1
            else:
                os.replace(path, target)
                moved += 1
        except Exception:  # noqa: BLE001 — one unreadable file must not block the store
            continue
    return {"moved": moved, "merged": merged}


def build_people_index() -> dict:
    """One pass over people/*.md → {'by_cid', 'by_identifier' (normalized), 'by_name' (name slug;
    when two DIFFERENT people share a name, the most recently touched file wins the name key —
    identifiers and canonical_id stay exact)."""
    import glob as _glob
    by_cid: dict = {}
    by_identifier: dict = {}
    by_name: dict = {}
    name_mtime: dict = {}
    for path in _glob.glob(os.path.join(people_dir(), "*.md")):
        try:
            with open(path, encoding="utf-8") as f:
                p = parse_person_file(f.read())
        except Exception:  # noqa: BLE001
            continue
        if p.canonical_id:
            by_cid[p.canonical_id] = path
        for i in p.identifiers:
            k = normalize_identifier(str(i))
            if k:
                by_identifier[k] = path
        s = safe_slug(p.name or "")
        if s:
            try:
                mt = os.path.getmtime(path)
            except OSError:
                mt = 0.0
            if s not in by_name or mt > name_mtime.get(s, 0.0):
                by_name[s] = path
                name_mtime[s] = mt
    return {"by_cid": by_cid, "by_identifier": by_identifier, "by_name": by_name}


def find_person_file(name: str = "", identifier: str = "", cid: str = "",
                     index: Optional[dict] = None) -> Optional[str]:
    """Resolve a person to their .md path: canonical_id → identifier (email/phone) → name slug.
    Pass a prebuilt `index` (build_people_index) when resolving many people in one run."""
    idx = index if index is not None else build_people_index()
    if cid and idx["by_cid"].get(cid):
        return idx["by_cid"][cid]
    if identifier:
        k = normalize_identifier(identifier)
        if k and idx["by_identifier"].get(k):
            return idx["by_identifier"][k]
    if name:
        s = safe_slug(name)
        if s and idx["by_name"].get(s):
            return idx["by_name"][s]
    return None
