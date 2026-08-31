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
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

# ── Relations ─────────────────────────────────────────────────────────────────
# ONE sentence: a relation is a typed edge between two people Sotto knows, stored on both ends,
# readable as a sentence. The vocabulary is CLOSED — an open vocabulary is how graphs rot — and it
# lives HERE (not in the writer) because the person-file format lives here: parse, serialize and
# merge_person all have to agree on it, and knowledge_query renders from it.
#
# Every type names its inverse; the writer (knowledge_update.link_relation) stores the forward edge
# on one file and the inverse on the other, so the two sides can't drift. Symmetric types are their
# own inverse. Types outside this map are DROPPED — on parse, on write, everywhere.
RELATION_INVERSE = {
    "introduced_by": "introduced",
    "introduced": "introduced_by",
    "works_with": "works_with",
    "family_of": "family_of",
    "partner_of": "partner_of",
    "met_through": "connected",
    "connected": "met_through",
}
# Read from the USER's vantage: a person file is someone the user knows, so "introduced_by" means
# "introduced to the user by". The dashboard carries the ONE other copy of this table (the receiver
# image cannot import the skills tree) and tests/test_docs_drift.py fails if the two ever differ.
RELATION_SENTENCE = {
    "introduced_by": "Introduced to you by {name}",
    "introduced": "Introduced {name} to you",
    "works_with": "Works with {name}",
    "family_of": "Family of {name}",
    "partner_of": "Partner of {name}",
    "met_through": "Met through {name}",
    "connected": "Connected you with {name}",
}
MAX_RELATIONS_FOR_LLM = 5        # the packed person block stays a block, not a directory
_MONTHS = ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December")

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
    # The 'Z' suffix promises UTC — convert aware datetimes, and default to UTC (a naive `now`
    # is trusted as already-UTC; callers must not pass server-local time here).
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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
class Relation:
    """One typed edge, as stored in a person's frontmatter. `slug` is the OTHER person's file stem
    (their canonical_id) — the link the dashboard and the packer both follow."""
    type: str = ""
    slug: str = ""
    name: str = ""
    date: Optional[str] = None      # when it happened (YYYY-MM-DD or YYYY-MM), if stated
    source: str = ""
    confidence: float = 0.9

    def key(self) -> tuple:
        """Edge identity — type + the other end. Writing the same edge twice is a no-op."""
        return (self.type, self.slug)

    def to_yaml_dict(self) -> dict:
        d: dict = {"type": self.type, "slug": self.slug, "name": self.name}
        if self.date:
            d["date"] = self.date
        d["source"] = self.source
        d["confidence"] = self.confidence
        return d

    @staticmethod
    def from_yaml_dict(d: dict) -> "Relation":
        conf = d.get("confidence", 0.9)
        return Relation(
            type=str(d.get("type") or "").strip(),
            slug=str(d.get("slug") or "").strip(),
            name=str(d.get("name") or "").strip(),
            date=(str(d["date"]).strip() or None) if d.get("date") is not None else None,
            source=str(d.get("source") or ""),
            confidence=float(conf) if isinstance(conf, (int, float)) else 0.9,
        )

    def sentence(self) -> str:
        return relation_sentence(self.type, self.name, self.date or "")


def relation_sentence(rel_type: str, name: str, date: str = "") -> str:
    """The edge as a readable sentence — "Introduced to you by Vishnu Sharma (May 2026)". Empty for
    a type outside the closed vocabulary or an edge with no name to say."""
    template = RELATION_SENTENCE.get(rel_type or "")
    if not template or not name:
        return ""
    when = _relation_date_label(date)
    return template.format(name=name) + (f" ({when})" if when else "")


def _relation_date_label(date: str) -> str:
    """"2026-05-14" / "2026-05" → "May 2026". Anything else passes through as written."""
    d = (date or "").strip()
    m = re.match(r"\A(\d{4})-(\d{2})(?:-\d{2})?\Z", d)
    if not m:
        return d
    month = int(m.group(2))
    return f"{_MONTHS[month - 1]} {m.group(1)}" if 1 <= month <= 12 else d


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
    relations: list = field(default_factory=list)  # [Relation] — typed edges, both ends
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
            # High overlap is normally the SAME assertion re-observed → bump. But an explicit
            # correction shares most of its words with the fact it corrects ("is NOT the
            # founder of…"), and bumping would STRENGTHEN the wrong fact (+0.1 conf, decay
            # clock reset, seen>1 immortality) while discarding the correction — the inverted
            # behavior this branch used to have. force_correction must win at ANY ratio.
            return (SUPERSEDE, fid) if force_correction else (BUMP, fid)
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
        # A correction the user made by hand is never restated by a pipeline, so seen stays 1 —
        # pruning it would let the wrong fact it corrected (re-BUMPed by every re-research)
        # outlive the correction. User words don't expire.
        if fact.source == "user_edit":
            continue
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
    # The closed vocabulary is enforced HERE, on the way in: an edge with a type nobody defined, or
    # with no other end to point at, is dropped once and never reaches a reader or a rewrite.
    relations = [r for r in (Relation.from_yaml_dict(rd)
                             for rd in (fm.get("relations") or []) if isinstance(rd, dict))
                 if r.type in RELATION_INVERSE and r.slug]
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
        relations=relations,
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
    # Relations sit in the IDENTITY block (above `facts:`) — the same half of the frontmatter
    # knowledge_update._person_head reads without paying for the facts map. Omitted when empty, so
    # a file with no edges is byte-identical to what it was before relations existed.
    if p.relations:
        d["relations"] = [r.to_yaml_dict() for r in p.relations]
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


# ── Atomic file writes ────────────────────────────────────────────────────────
def write_text_atomic(path: str, text: str) -> None:
    """THE graph's file writer: a process-unique temp, then `os.replace`.

    `open(path, "w")` truncates before it writes anything, and a crash in that window leaves a
    person file EMPTY — everything Sotto remembers about someone, gone, with no error anywhere.
    Every write under `knowledge/` goes through here so that failure mode cannot exist. The temp
    name carries the pid because a fixed `<path>.tmp` is itself a shared mutable resource (the
    jsonstore bug: two writers open the same temp, one renames it out from under the other)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
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


def write_person_file(path: str, p: "PersonFile", now: Optional[datetime] = None) -> None:
    """Serialize + write one person file atomically. The ONE way a person file reaches disk."""
    write_text_atomic(path, serialize_person_file(p, now))


# ── Person-file identity resolution (canonical_id-keyed store) ────────────────
# People files are keyed by canonical_id ({cid}.md), NOT by name slug. Name-slug keying was the
# identity-fragmentation root cause: two different "John Smith"s merged into one file, while one
# person whose name differs by channel ("Sarah" on iMessage vs "Sarah Chen" on email) split into
# two files. Every entry point resolves an existing person canonical_id → identifier → name, and
# migrate_people_dir() idempotently re-keys legacy name-slug files on first run.

def normalize_identifier(idv: str) -> str:
    """Mirror of textutil._normalize_identifier so file-store keys line up with the brief pipeline:
    phone-ish strings → last-10 digits; everything else (emails) → lowercase trimmed.
    A WhatsApp `@lid` JID is a rotating PRIVACY id, not a phone: its digit tail can collide with a
    real number and auto-merge two strangers irreversibly, so it never becomes a store key."""
    trimmed = (idv or "").strip().lower()
    if trimmed.endswith("@lid"):
        return ""
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
    # Edges union by (type, other end); dst's copy of a shared edge wins. The BACK-references on
    # the other people's files are repointed by knowledge_update.merge_person_files, which is where
    # the surviving slug is known — this half only unions the surviving file's own list.
    have = {r.key() for r in dst.relations}
    for r in src.relations:
        if r.key() not in have:
            have.add(r.key())
            dst.relations.append(r)
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


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def repoint_relations(src_slug: str, dst_slug: str, dst_name: str,
                      now: Optional[datetime] = None) -> int:
    """Every OTHER file whose edge points at `src_slug` now points at `dst_slug`. Called whenever a
    person file changes identity — a merge, or the migration re-keying a legacy name-slug file —
    because a relation names a slug, and a slug that no longer exists renders as a broken link.

    The files to visit are the SURVIVOR's own relations: edges are written in pairs (see
    knowledge_update.link_relation), so a back-reference to `src` can only exist on someone `src`
    also pointed at, and the survivor has already absorbed src's list. Repointing can collide with
    an edge the visited file already had — the duplicate is dropped, first one wins."""
    if not src_slug or not dst_slug or src_slug == dst_slug:
        return 0
    dst_path = safe_path(people_dir(), dst_slug)
    if not os.path.exists(dst_path):
        return 0
    with open(dst_path, encoding="utf-8") as f:
        dst = parse_person_file(f.read())
    repointed = 0
    for slug in dict.fromkeys(r.slug for r in dst.relations):
        try:
            path = safe_path(people_dir(), slug)
        except ValueError:
            continue
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            other = parse_person_file(f.read())
        kept, seen, touched = [], set(), False
        for r in other.relations:
            if r.slug == src_slug:
                r.slug, r.name, touched = dst_slug, dst_name or r.name, True
            if r.slug == slug or r.key() in seen:   # an edge to itself, or a duplicate: drop it
                touched = True
                continue
            seen.add(r.key())
            kept.append(r)
        if not touched:
            continue
        other.relations = kept
        write_person_file(path, other, now)
        repointed += 1
    return repointed


def read_or_stub_person(slug: str, name_hint: str = "", iso: str = "", updated_by: str = ""):
    """(path, PersonFile) for an existing slug — or a stub AT that exact slug when the file is
    missing, so an edge can never name a slug with nothing behind it. Returns (None, None) for a
    slug that is not a canonical_id: every person file is keyed by one (migrate_people_dir enforces
    it at every entry point), and a stub that isn't would be born already needing migration.

    Lives here, not in the relation writer, because the journal's `edge` op has to be able to
    materialize the same stub when it finishes an interrupted link — one implementation."""
    try:
        path = safe_path(people_dir(), slug)
    except ValueError:
        return (None, None)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return path, parse_person_file(f.read())
    if not valid_canonical_id(slug):
        return (None, None)
    return path, PersonFile(canonical_id=slug, name=name_hint or slug,
                            updated_at=iso, updated_by=updated_by)


def absorb_person_file(dst_path: str, src_path: str, now: Optional[datetime] = None) -> bool:
    """Union src INTO dst, delete src, and leave nobody pointing at the file that vanished.
    THE merge: migrate_people_dir (two legacy files, one canonical_id) and
    knowledge_update.merge_person_files (dedup-lite, and the user's confirmed merge) are the same
    operation and share this one implementation. Both files live in `knowledge/people/`.

    One merge is three writes across N files, so it is journaled as ONE op: a crash after the
    survivor is written but before the loser is deleted used to leave two files for one person and
    a set of edges pointing at a slug that no longer exists. Returns False for a self-merge or a
    merge whose loser is already gone (which is what a finished merge looks like)."""
    if os.path.realpath(dst_path) == os.path.realpath(src_path):
        return False
    if not (os.path.exists(dst_path) and os.path.exists(src_path)):
        return False
    run_journaled([merge_op(_stem(dst_path), _stem(src_path))], now)
    return True


def _absorb_now(dst_path: str, src_path: str, now: Optional[datetime] = None) -> bool:
    """absorb_person_file's body, WITHOUT the journal — it is the `merge` op's applier, and an
    applier that journals would journal itself forever."""
    with open(dst_path, encoding="utf-8") as f:
        dst = parse_person_file(f.read())
    with open(src_path, encoding="utf-8") as f:
        src = parse_person_file(f.read())
    merge_person(dst, src)
    src_slug, dst_slug = _stem(src_path), _stem(dst_path)
    # The survivor cannot point at the file that's about to vanish, nor at itself: both ends of
    # those edges are now one person.
    dst.relations = [r for r in dst.relations if r.slug not in (src_slug, dst_slug)]
    write_person_file(dst_path, dst, now)
    os.remove(src_path)
    repoint_relations(src_slug, dst_slug, dst.name, now)
    return True


# ── The interrupted-update journal ────────────────────────────────────────────
# ONE sentence: an interrupted graph update finishes the next time anything touches the graph.
#
# write_text_atomic covers ONE file. Some graph operations are not one file: a relation is stored on
# BOTH people, and a merge writes the survivor, deletes the loser and repoints everyone who pointed
# at it. A crash between those files left a one-sided edge or a dangling slug, and nothing noticed
# or repaired it (external review finding #6, Aug 31). So a multi-file batch writes its op list to
# `knowledge/.journal.json` BEFORE it touches the first file and removes it after the last; every
# writer entry point replays what it finds (knowledge_update.graph_lock).
#
# Format: {"ts": <iso>, "ops": [{"op": <kind>, …}]} — small on purpose; it names operations, it does
# not copy files. Every op is IDEMPOTENT (re-running a finished batch writes no bytes), which is
# what lets replay simply re-run the whole list without knowing how far the crash got.
JOURNAL_FILE = ".journal.json"


def journal_path() -> str:
    return os.path.join(data_root(), "knowledge", JOURNAL_FILE)


def edge_op(slug: str, name: str, rel: "Relation") -> dict:
    """"put this edge on this person file" — one file's half of a relation write."""
    return {"op": "edge", "slug": slug, "name": name, "rel": rel.to_yaml_dict()}


def unedge_op(slug: str, other: str, rel_type: str = "") -> dict:
    """"drop this person's edge(s) to `other`" — an empty type means every edge between them."""
    return {"op": "unedge", "slug": slug, "other": other, "type": rel_type or ""}


def merge_op(dst_slug: str, src_slug: str) -> dict:
    return {"op": "merge", "dst": dst_slug, "src": src_slug}


def rekey_op(src_slug: str, dst_slug: str, name: str) -> dict:
    """"this person file changed filename" — the move plus the repointing every edge owes it."""
    return {"op": "rekey", "src": src_slug, "dst": dst_slug, "name": name}


def run_journaled(ops: list, now: Optional[datetime] = None) -> None:
    """Write the ops down, apply them in order, remove the journal.

    An exception leaves the journal on disk ON PURPOSE: the batch is half-applied, and the next
    thing to touch the graph is what finishes it."""
    if not ops:
        return
    write_text_atomic(journal_path(), json.dumps({"ts": now_iso(now), "ops": ops}))
    for op in ops:
        _apply_journal_op(op, now)
    _clear_journal()


# An op that RAISES is retried on later replays, but not forever: a permanently broken op (bad
# disk, poisoned file) must eventually stop taxing every graph write. Five attempts spans days of
# real use before the give-up line prints.
JOURNAL_MAX_ATTEMPTS = 5


def replay_journal(now: Optional[datetime] = None) -> int:
    """Finish an interrupted multi-file update. Returns how many ops actually changed something;
    silent when there is nothing to finish.

    An op that applies cleanly (or has nothing left to do) is done. An op that RAISES is kept for
    the next replay — clearing it would declare victory over a half-applied graph (the external
    reviewer's exact objection, Aug 31) — until JOURNAL_MAX_ATTEMPTS, when it is dropped with a
    line. A journal that cannot be PARSED is cleared: it names nothing anyone can retry, and it
    must not brick every future graph write."""
    try:
        with open(journal_path(), encoding="utf-8") as f:
            data = json.load(f)
        ops = data.get("ops") if isinstance(data, dict) else None
        ops = ops if isinstance(ops, list) else []
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        _clear_journal()
        _journal_log("[sotto] knowledge journal: unreadable — cleared, nothing to finish")
        return 0
    done, retry = [], []
    for op in ops:
        try:
            if _apply_journal_op(op, now):
                done.append(str(op.get("op") or "?") if isinstance(op, dict) else "?")
        except Exception:  # noqa: BLE001 — one broken op must not strand the rest of the batch
            if isinstance(op, dict):
                op["attempts"] = int(op.get("attempts") or 0) + 1
                if op["attempts"] < JOURNAL_MAX_ATTEMPTS:
                    retry.append(op)
                else:
                    _journal_log(f"[sotto] knowledge journal: gave up on a {op.get('op') or '?'} "
                                 f"op after {JOURNAL_MAX_ATTEMPTS} attempts")
    if retry:
        write_text_atomic(journal_path(), json.dumps({"ts": now_iso(now), "ops": retry}))
    else:
        _clear_journal()
    if done:
        _journal_log(f"[sotto] knowledge journal: finished {len(done)} interrupted op(s) "
                     f"({', '.join(done)})")
    return len(done)


def _clear_journal() -> None:
    try:
        os.remove(journal_path())
    except OSError:
        pass


def _journal_log(msg: str) -> None:
    """A repair is the one thing here worth a line; everything else fails toward silence."""
    try:
        from sotto_log import diag
        diag(msg)
    except Exception:  # noqa: BLE001 — knowledge.py is importable without _shared/lib on the path
        import sys
        print(msg, file=sys.stderr)


def _op_edge(op: dict, now: Optional[datetime]) -> bool:
    rel = Relation.from_yaml_dict(op.get("rel") or {})
    if rel.type not in RELATION_INVERSE or not rel.slug:
        return False
    path, p = read_or_stub_person(str(op.get("slug") or ""), str(op.get("name") or ""),
                                  now_iso(now), rel.source)
    if p is None:
        return False
    if any(r.key() == rel.key() for r in p.relations):
        return False                    # already on file: the same edge twice writes no bytes
    p.relations.append(rel)
    write_person_file(path, p, now)
    return True


def _op_unedge(op: dict, now: Optional[datetime]) -> bool:
    slug, other = str(op.get("slug") or ""), str(op.get("other") or "")
    want = str(op.get("type") or "")
    if not slug or not other:
        return False
    try:
        path = safe_path(people_dir(), slug)
    except ValueError:
        return False
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        p = parse_person_file(f.read())
    keep = [r for r in p.relations if not (r.slug == other and (not want or r.type == want))]
    if len(keep) == len(p.relations):
        return False                    # already removed
    p.relations = keep
    write_person_file(path, p, now)
    return True


def _op_merge(op: dict, now: Optional[datetime]) -> bool:
    src_slug, dst_slug = str(op.get("src") or ""), str(op.get("dst") or "")
    try:
        dst = safe_path(people_dir(), dst_slug)
        src = safe_path(people_dir(), src_slug)
    except ValueError:
        return False
    if dst == src or not os.path.exists(dst):
        return False                    # no survivor: nothing this op can finish
    if os.path.exists(src):
        return _absorb_now(dst, src, now)
    # The loser is gone — but a crash can land BETWEEN its delete and the repointing, and "the
    # loser is gone" used to read as "this merge finished", leaving edges naming a deleted slug
    # forever (external reviewer's repro, Aug 31). Repointing is idempotent: a merge that truly
    # finished has nothing left naming src and this writes no bytes.
    with open(dst, encoding="utf-8") as f:
        dst_name = parse_person_file(f.read()).name
    return bool(repoint_relations(src_slug, dst_slug, dst_name, now))


def _op_rekey(op: dict, now: Optional[datetime]) -> bool:
    """Move a person file to its canonical_id name, then repoint every edge that names the old
    slug. Both halves no-op once done, and a crash that left BOTH names on disk is a merge."""
    src_slug, dst_slug = str(op.get("src") or ""), str(op.get("dst") or "")
    if not src_slug or not dst_slug or src_slug == dst_slug:
        return False
    try:
        src = safe_path(people_dir(), src_slug)
        dst = safe_path(people_dir(), dst_slug)
    except ValueError:
        return False
    moved = False
    if os.path.exists(src):
        if os.path.exists(dst):
            _absorb_now(dst, src, now)
        else:
            os.replace(src, dst)
        moved = True
    repointed = repoint_relations(src_slug, dst_slug, str(op.get("name") or ""), now)
    return moved or bool(repointed)


_JOURNAL_OPS = {"edge": _op_edge, "unedge": _op_unedge, "merge": _op_merge, "rekey": _op_rekey}


def _apply_journal_op(op, now: Optional[datetime]) -> bool:
    if not isinstance(op, dict):
        return False
    fn = _JOURNAL_OPS.get(op.get("op"))
    return bool(fn(op, now)) if fn else False


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
                    write_person_file(path, p, now)
                continue
            if os.path.exists(target):
                absorb_person_file(target, path, now)
                merged += 1
            else:
                if changed:
                    write_person_file(path, p, now)   # normalize in place, then move
                # The file is about to change identity: anyone holding an edge to its old name-slug
                # would hold one to a filename that doesn't exist, so the move and the repointing
                # are ONE journaled op rather than two things a crash can separate.
                run_journaled([rekey_op(_stem(path), _stem(target), p.name)], now)
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
