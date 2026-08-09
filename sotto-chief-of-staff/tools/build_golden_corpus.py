#!/usr/bin/env python3
"""
build_golden_corpus.py — turn the owner's real $SOTTO_DATA into a PII-scrubbed, referentially
intact eval corpus (docs/plans/golden-corpus.md).

Runs WHERE THE DATA LIVES (the container / the machine holding the volume), stdlib + PyYAML only —
the same deps the image already carries. Nothing here calls out to the network unless you ask for
LLM label drafting.

    SOTTO_DATA=/data python3 tools/build_golden_corpus.py --version corpus-v1 --days 42

What it does, in order:
  1. GATHER   the last N days from $SOTTO_DATA (the cached read_local snapshot, the continuity
              ledger, preferences, the knowledge graph) plus any gather outputs you point at.
  2. MAP      every human it can see to ONE fake identity, keyed by HMAC-SHA256 of a secret key —
              same human → same alias across every channel and every file, same real domain → same
              fake domain (colleagues stay colleagues). The map is written OUTSIDE the corpus and is
              never needed to run evals.
  3. REWRITE  identities in structure AND in prose, then sweep whatever the map didn't know
              (any surviving address/number becomes an unmapped pseudonym rather than a leak).
  4. TOKENIZE every timestamp into the harness's relative form ({{TS-90m}}, {{ISO+3h}}, {{D-2d}}),
              so a corpus day can be re-based onto any clock at eval time.
  5. EMIT     evals/corpus/<version>/ — one fixture-shaped file per day, a manifest, and a DRAFT
              labels.yaml the owner corrects in the labeling session (evals/LABELING.md).
  6. SCAN     the emitted bytes for anything still on the map's real side and REFUSE to leave a
              corpus that leaks (exit 3).

STANDING RULE: "scrubbed" never means "shareable". The corpus is confidential regardless — it is
gitignored, it self-ignores (the builder drops a `*` .gitignore into it), and
tools/prepare-public-repo.sh deletes it and then greps for it (CORPUS GUARD).

DEVIATION from the plan doc (recorded on purpose): the plan's layer-2 scrub says "an NER rewrite
pass". The runtime image is python:3.12-slim + PyYAML + the Google client libs — no NER model, and
adding one (spaCy/transformers + weights) is a heavy dependency for a tool that runs on the user's
container. We use the knowledge graph's own identifier index as the entity list instead: the graph
already knows every name, email and phone Sotto has ever seen, which beats generic NER on exactly
this data. The residual sweep (step 3) covers what the index doesn't know, and rule 3 —
confidential regardless — covers what the sweep can't.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PACK, "_shared", "lib"))

from textutil import (  # noqa: E402  (_shared/lib is on sys.path, same as every sibling script)
    _CONSUMER_DOMAINS,
    _base_domain,
    _digits,
    _normalize_identifier,
    _normalize_name_key,
    _s,
    _sender_addr,
)

CORPUS_SCHEMA = 1
DEFAULT_DAYS = 42


def _load_module(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PACK, rel_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Alias vocabulary ─────────────────────────────────────────────────────────────────────────────
# Plain, unremarkable, obviously-not-real. `.example` is the RFC 2606 reserved TLD, so a fake domain
# can never collide with a company that exists.
FIRST_NAMES = ["Ada", "Bram", "Cleo", "Dov", "Elin", "Foss", "Gita", "Hale", "Ilse", "Jory",
               "Kade", "Lena", "Mose", "Nila", "Oren", "Pria", "Quin", "Rhea", "Soren", "Tova",
               "Uma", "Vero", "Wren", "Xara", "Yuli", "Zane", "Bexley", "Calla", "Dane", "Esme"]
LAST_NAMES = ["Ashby", "Brookner", "Calder", "Dunmore", "Ellery", "Fenwick", "Garrow", "Hollis",
              "Ivers", "Jessop", "Kettle", "Larkin", "Mowbray", "Norrell", "Ovett", "Paxton",
              "Quill", "Radcliffe", "Sandoval", "Thackery", "Umber", "Vance", "Wexley", "Yardley"]
DOMAIN_WORDS = ["northwind", "brightline", "quarrystone", "lambdale", "pentimento", "ridgeway",
                "solstice", "tessellate", "underwood", "vantage", "waypoint", "yarrow", "zephyr",
                "almanac", "beacon", "cinder", "driftwood", "everstone", "foundry", "granite"]
FREEMAIL_DOMAINS = ["mailbox.example", "inbox.example", "postbox.example", "letterbox.example"]

# A bare first name is rewritten only when it is >=3 chars and not one of these — otherwise a person
# called "Mark" or "Bill" would eat every verb in the corpus.
NAME_STOP = {
    "the", "and", "for", "you", "your", "our", "this", "that", "with", "from", "have", "has",
    "was", "are", "not", "but", "all", "can", "will", "would", "should", "could", "may", "one",
    "two", "new", "now", "day", "week", "call", "team", "time", "mark", "bill", "grace", "hope",
    "art", "may", "june", "april", "august", "sun", "dawn", "rose", "chase", "drew", "reed",
    "amber", "summer", "autumn", "faith", "joy", "rich", "frank", "max", "sky", "ray", "dean",
}

# Any run of >=10 digits with phone punctuation. Deliberately greedy: an unmapped number is
# pseudonymized rather than trusted.
PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{8,}\d")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# A timestamp has enough digits to look like a phone number. It isn't one.
DATEISH_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}")

# Timestamp shapes the tokenizer recognizes, in match order (most specific first).
_TS_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")
_TS_NAIVE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_TS_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_MD = re.compile(r"^\d{2}-\d{2}$")


def _hx(key: bytes, kind: str, value: str) -> int:
    """One keyed hash → an int. Every alias choice is a deterministic function of (key, kind, value),
    so the same volume + the same key rebuilds a byte-identical corpus."""
    d = hmac.new(key, f"{kind}:{value}".encode("utf-8"), hashlib.sha256).digest()
    return int.from_bytes(d[:8], "big")


def _pick(pool: list, n: int, bump: int) -> str:
    return pool[(n + bump) % len(pool)]


# ── The identity map (the whole scrub model, layer 1) ────────────────────────────────────────────

class IdentityMap:
    """One human → one alias, everywhere. Identities are UNIONED: a name and an email and a phone
    that ever co-occur on the same person become one group, so the same human keeps one fake name
    across iMessage, WhatsApp, Gmail and the graph."""

    def __init__(self, key: bytes):
        self.key = key
        self._groups: dict[str, dict] = {}      # gid -> {"names": set, "idents": set}
        self._by_ident: dict[str, str] = {}     # normalized identifier -> gid
        self._by_name: dict[str, str] = {}      # normalized name key -> gid
        self._next = 0
        self.alias: dict[str, dict] = {}        # signature -> {"name","first","last"}
        self.email_alias: dict[str, str] = {}
        self.phone_alias: dict[str, str] = {}
        self.domain_alias: dict[str, str] = {}
        self.company_alias: dict[str, str] = {}
        self.group_assets: dict[str, dict] = {}   # signature -> every alias string that IS this human
        self._sig: dict[str, str] = {}          # gid -> signature (set by freeze)
        self._text_re = None
        self._text_map: dict[str, str] = {}
        self._frozen = False

    # -- construction ----------------------------------------------------------------------------
    def _new_gid(self) -> str:
        self._next += 1
        gid = f"g{self._next}"
        self._groups[gid] = {"names": set(), "idents": set()}
        return gid

    def add(self, names, identifiers) -> str:
        """Register one person. Merges into every group that already knows any of these keys."""
        names = [_s(n).strip() for n in (names or []) if _s(n).strip()]
        names = [n for n in names if not _looks_like_identifier(n)]
        idents = [_normalize_identifier(_s(i)) for i in (identifiers or []) if _s(i).strip()]
        idents = [i for i in idents if i]
        hits = {self._by_ident[i] for i in idents if i in self._by_ident}
        hits |= {self._by_name[_normalize_name_key(n)] for n in names
                 if _normalize_name_key(n) in self._by_name}
        gid = sorted(hits)[0] if hits else self._new_gid()
        for other in sorted(hits):
            if other == gid:
                continue
            self._groups[gid]["names"] |= self._groups[other]["names"]
            self._groups[gid]["idents"] |= self._groups[other]["idents"]
            for k, v in list(self._by_ident.items()):
                if v == other:
                    self._by_ident[k] = gid
            for k, v in list(self._by_name.items()):
                if v == other:
                    self._by_name[k] = gid
            del self._groups[other]
        for n in names:
            self._groups[gid]["names"].add(n)
            self._by_name[_normalize_name_key(n)] = gid
        for i in idents:
            self._groups[gid]["idents"].add(i)
            self._by_ident[i] = gid
        return gid

    def add_domain(self, dom: str) -> None:
        dom = _s(dom).lower().strip()
        if dom:
            self.domain_alias.setdefault(dom, "")

    def add_company(self, name: str) -> None:
        name = _s(name).strip()
        if name:
            self.company_alias.setdefault(name, "")

    # -- allocation ------------------------------------------------------------------------------
    def freeze(self) -> None:
        """Allocate every alias. Allocation order is the sorted group SIGNATURE (the lowest of a
        group's normalized keys), not insertion order — so the map is reproducible from the same
        volume regardless of the order files happened to be read."""
        if self._frozen:
            return
        for gid, g in self._groups.items():
            keys = sorted(g["idents"]) + sorted(_normalize_name_key(n) for n in g["names"])
            self._sig[gid] = keys[0] if keys else gid

        taken_names, taken_emails, taken_phones, taken_domains = set(), set(), set(), set()
        for dom in sorted(self.domain_alias):
            self.domain_alias[dom] = self._alloc_domain(dom, taken_domains)
        for gid in sorted(self._groups, key=lambda g: (self._sig[g], g)):
            sig = self._sig[gid]
            self.alias[sig] = self._alloc_name(sig, taken_names)
            assets = {"name": self.alias[sig]["name"], "emails": [], "phones": []}
            for i, email in enumerate(sorted(e for e in self._groups[gid]["idents"] if "@" in e)):
                self.email_alias[email] = self._alloc_email(sig, email, i, taken_emails)
                assets["emails"].append(self.email_alias[email])
            for phone in sorted(p for p in self._groups[gid]["idents"] if "@" not in p):
                self.phone_alias[phone] = self._alloc_phone(phone, taken_phones)
                assets["phones"].append(self.phone_alias[phone])
            self.group_assets[sig] = assets
        for name in sorted(self.company_alias):
            self.company_alias[name] = self._alloc_company(name)
        self._build_text_rewriter()
        self._frozen = True

    def _alloc_name(self, sig: str, taken: set) -> dict:
        n = _hx(self.key, "name", sig)
        for bump in range(4096):
            first = _pick(FIRST_NAMES, n, bump)
            last = _pick(LAST_NAMES, n // len(FIRST_NAMES), bump * 7)
            full = f"{first} {last}"
            if full not in taken:
                taken.add(full)
                return {"name": full, "first": first, "last": last}
        raise RuntimeError("alias name pool exhausted")

    def _alloc_domain(self, dom: str, taken: set) -> str:
        base = _base_domain(dom) or dom
        sub = dom[: -len(base)].rstrip(".") if dom.endswith(base) and dom != base else ""
        if base in _CONSUMER_DOMAINS:
            fake_base = _pick(FREEMAIL_DOMAINS, _hx(self.key, "freemail", base), 0)
        else:
            fake_base = self.domain_alias.get(base) or ""
            if not fake_base:
                n = _hx(self.key, "domain", base)
                for bump in range(4096):
                    cand = _pick(DOMAIN_WORDS, n, bump) + (f"{bump}" if bump else "") + ".example"
                    if cand not in taken:
                        fake_base = cand
                        break
                taken.add(fake_base)
            self.domain_alias[base] = fake_base
        return f"{sub}.{fake_base}" if sub else fake_base

    def _alloc_email(self, sig: str, email: str, idx: int, taken: set) -> str:
        a = self.alias[sig]
        dom = email.split("@")[-1]
        fake_dom = self.domain_alias.get(dom) or self._alloc_domain(dom, set())
        self.domain_alias[dom] = fake_dom
        local = f"{a['first']}.{a['last']}".lower()
        for bump in range(4096):
            cand = f"{local}{(idx + bump) or ''}@{fake_dom}"
            if cand not in taken:
                taken.add(cand)
                return cand
        raise RuntimeError("alias email pool exhausted")

    def _alloc_phone(self, phone: str, taken: set) -> str:
        n = _hx(self.key, "phone", phone)
        for bump in range(1_000_000):
            cand = "+1555" + f"{(n + bump) % 10_000_000:07d}"
            if cand not in taken:
                taken.add(cand)
                return cand
        raise RuntimeError("alias phone pool exhausted")

    def _alloc_company(self, name: str) -> str:
        dom = self.domain_alias.get(_base_domain(name)) or ""
        word = dom.split(".")[0] if dom else _pick(DOMAIN_WORDS, _hx(self.key, "company", name), 0)
        return word.capitalize()

    # -- lookup ----------------------------------------------------------------------------------
    def name_for(self, name: str = "", identifier: str = "") -> str:
        gid = self._gid_for(name, identifier)
        return self.alias[self._sig[gid]]["name"] if gid else ""

    def _gid_for(self, name: str = "", identifier: str = ""):
        i = _normalize_identifier(_s(identifier))
        if i and i in self._by_ident:
            return self._by_ident[i]
        k = _normalize_name_key(_s(name))
        return self._by_name.get(k)

    def email_for(self, addr: str) -> str:
        a = _s(addr).strip().lower()
        if a in self.email_alias:
            return self.email_alias[a]
        dom = a.split("@")[-1] if "@" in a else ""
        fake_dom = self.domain_alias.get(dom) or self._alloc_domain(dom or "unknown.invalid", set())
        h = f"{_hx(self.key, 'unmapped-email', a):x}"[:8]
        out = f"unmapped-{h}@{fake_dom}"
        self.email_alias[a] = out
        return out

    def phone_for(self, number: str) -> str:
        k = _normalize_identifier(_s(number))
        if k in self.phone_alias:
            return self.phone_alias[k]
        out = self._alloc_phone(k or _s(number), set(self.phone_alias.values()))
        self.phone_alias[k] = out
        return out

    def identifier_for(self, idv: str) -> str:
        """One entry point for a raw handle/JID/address of unknown shape."""
        v = _s(idv).strip()
        if not v:
            return v
        if "@" in v and not v.endswith(("@s.whatsapp.net", "@g.us", "@c.us", "@lid")):
            return self.email_for(v)
        if v.endswith(("@s.whatsapp.net", "@g.us", "@c.us", "@lid")):
            prefix, suffix = v.split("@", 1)
            if _digits(prefix):
                return self.phone_for(prefix).lstrip("+") + "@" + suffix
            return f"{_hx(self.key, 'jid', prefix):x}"[:12] + "@" + suffix
        if len(_digits(v)) >= 7:
            return self.phone_for(v)
        return v

    # -- layer 2: in-text rewrite ------------------------------------------------------------------
    def _build_text_rewriter(self) -> None:
        """One alternation over every real string the graph knows, longest-first so 'Dana Wells'
        wins over 'Dana'. Emails and phone numbers are handled by their own regexes (below) because
        they have too many surface forms to enumerate. A name shorter than 4 chars or on the
        stop-list is never rewritten in prose — otherwise a contact called 'You' eats the corpus."""
        m: dict[str, str] = {}
        for gid, g in self._groups.items():
            alias = self.alias[self._sig[gid]]
            for real in g["names"]:
                if len(real) >= 4 and real.lower() not in NAME_STOP:
                    m[real] = alias["name"]
                parts = real.split()
                if len(parts) > 1:
                    for token, fake in ((parts[0], alias["first"]), (parts[-1], alias["last"])):
                        if len(token) >= 3 and token.lower() not in NAME_STOP:
                            m.setdefault(token, fake)
        for real, fake in self.company_alias.items():
            m.setdefault(real, fake)
        for real, fake in self.domain_alias.items():
            m.setdefault(real, fake)
        self._text_map = {k: v for k, v in m.items() if k}
        if not self._text_map:
            self._text_re = None
            return
        keys = sorted(self._text_map, key=lambda s: (-len(s), s))
        self._text_re = re.compile(r"(?<![\w.@])(" + "|".join(re.escape(k) for k in keys)
                                   + r")(?![\w@])", re.IGNORECASE)

    def rewrite_text(self, text: str) -> str:
        """Layer 2 + the residual sweep: known entities become their alias, and anything still
        shaped like an address or a number becomes an unmapped pseudonym."""
        s = _s(text)
        if not s:
            return s
        s = EMAIL_RE.sub(lambda mo: self.email_for(mo.group(0)), s)
        s = PHONE_RE.sub(lambda mo: (self.phone_for(mo.group(0))
                                     if len(_digits(mo.group(0))) >= 10 and not DATEISH_RE.search(mo.group(0))
                                     else mo.group(0)), s)
        if self._text_re is not None:
            lower = {k.lower(): v for k, v in self._text_map.items()}
            s = self._text_re.sub(lambda mo: lower.get(mo.group(1).lower(), mo.group(1)), s)
        return s

    # -- persistence -----------------------------------------------------------------------------
    def key_fingerprint(self) -> str:
        return hashlib.sha256(self.key).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "schema": CORPUS_SCHEMA,
            "key": self.key.hex(),
            "key_fingerprint": self.key_fingerprint(),
            "people": [{"real_names": sorted(g["names"]), "real_identifiers": sorted(g["idents"]),
                        "alias": self.alias[self._sig[gid]]}
                       for gid, g in sorted(self._groups.items())],
            "emails": self.email_alias,
            "phones": self.phone_alias,
            "domains": self.domain_alias,
            "companies": self.company_alias,
        }

    def real_strings(self) -> set:
        """Every string that MUST NOT survive into the corpus — the leak scan's needle list."""
        out = set()
        for g in self._groups.values():
            out |= {n for n in g["names"] if len(n) >= 4}
            out |= {i for i in g["idents"] if len(i) >= 6}
        out |= {e for e in self.email_alias if len(e) >= 6}
        out |= {p for p in self.phone_alias if len(p) >= 7}
        out |= {d for d in self.domain_alias if len(d) >= 5}
        out |= {c for c in self.company_alias if len(c) >= 4}
        return out


def _looks_like_identifier(name: str) -> bool:
    n = _s(name).strip()
    return ("@" in n) or bool(n and len(_digits(n)) >= 7)


# ── Gather: what the volume knows ────────────────────────────────────────────────────────────────

def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _arr(d, key):
    v = (d or {}).get(key)
    return v if isinstance(v, list) else []


def gather(data_root: str, local_path: str, gmail_path: str, cal_path: str,
           granola_path: str) -> dict:
    """Everything the corpus is built from. Missing pieces are simply absent — a volume with no
    Granola still produces a corpus."""
    snap = _read_json(local_path or os.path.join(data_root, "knowledge", "last_local_snapshot.json"), {})
    local = snap.get("local") if isinstance(snap.get("local"), dict) else snap
    gmail = _read_json(gmail_path, [])
    if isinstance(gmail, dict):
        gmail = _arr(gmail, "emails")
    return {
        "local": local if isinstance(local, dict) else {},
        "emails": gmail if isinstance(gmail, list) else [],
        "events": _read_json(cal_path, []) if isinstance(_read_json(cal_path, []), list) else [],
        "granola": _arr(_read_json(granola_path, {}), "meetings"),
        "ledger": _read_ledger(data_root),
        "preferences": (_read_json(os.path.join(data_root, "preferences.json"), {}) or {}).get("explicit") or {},
        "people": _read_people(data_root),
        "companies": _read_companies(data_root),
    }


def _read_ledger(data_root: str) -> list:
    """The continuity ledger's frontmatter, as the dicts the replay re-seeds from. An unreadable or
    malformed entry is skipped, never guessed at — same posture the ledger's own reader takes."""
    import yaml  # noqa: PLC0415 — a pack dependency (knowledge.py), loaded only where it's used
    kn = _load_module(os.path.join("_shared", "knowledge", "knowledge.py"), "knowledge")
    out = []
    for path in sorted(glob.glob(os.path.join(data_root, "knowledge", "continuity", "*.md"))):
        try:
            with open(path, encoding="utf-8") as f:
                head, _body = kn.split_frontmatter_body(f.read())
            fm = yaml.safe_load(head) if head else None
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if isinstance(fm, dict) and fm:
            out.append(fm)
    return out


def _read_people(data_root: str) -> list:
    kn = _load_module(os.path.join("_shared", "knowledge", "knowledge.py"), "knowledge")
    out = []
    for path in sorted(glob.glob(os.path.join(data_root, "knowledge", "people", "*.md"))):
        try:
            with open(path, encoding="utf-8") as f:
                p = kn.parse_person_file(f.read())
        except (OSError, ValueError):
            continue
        # Relation NAMES ride along: an edge stores the other person's display name, and while
        # the writer always links an edge to a real file (so that name is normally registered by
        # this same loop), a hand-edited or hand-imported file can carry a name no file claims.
        # Registering them here means the text rewriter pseudonymizes that name everywhere it
        # appears in the corpus, consistently with whoever else shares it.
        out.append({"name": p.name, "identifiers": list(p.identifiers), "company": p.company,
                    "relation_names": [r.name for r in p.relations if r.name]})
    return out


def _read_companies(data_root: str) -> list:
    out = []
    for path in sorted(glob.glob(os.path.join(data_root, "knowledge", "companies", "*.md"))):
        try:
            with open(path, encoding="utf-8") as f:
                head = f.read(4000)
        except OSError:
            continue
        m = re.search(r"^name:\s*(.+)$", head, re.M)
        d = re.search(r"^domain:\s*(.+)$", head, re.M)
        out.append({"name": _s(m.group(1)).strip().strip('"') if m else "",
                    "domain": _s(d.group(1)).strip().strip('"') if d else ""})
    return out


def build_map(raw: dict, key: bytes, user_email: str = "") -> IdentityMap:
    """Seed the map from every identity source on the volume, graph first (it is the richest)."""
    im = IdentityMap(key)
    if user_email:
        im.add([], [user_email])      # the owner is a group like any other — never a literal "You"
    for p in raw["people"]:
        im.add([p.get("name")], p.get("identifiers"))
        for rn in (p.get("relation_names") or []):
            im.add([rn], [])          # its own group unless another source links it to identifiers
    for c in _arr(raw["local"], "contacts"):
        im.add([c.get("name")], list(c.get("phones") or []) + list(c.get("emails") or []))
    for msg in _arr(raw["local"], "imessage") + _arr(raw["local"], "deferred_unread_imessage"):
        im.add([], [msg.get("handle")])
        for h in (msg.get("group_participants") or []):
            im.add([], [h])
    for msg in _arr(raw["local"], "whatsapp") + _arr(raw["local"], "deferred_unread_whatsapp"):
        im.add([msg.get("partner_name")], [msg.get("contact_jid"), msg.get("sender_jid")])
    for c in _arr(raw["local"], "calls"):
        im.add([], [c.get("phone")])
    for c in _arr(raw["local"], "whatsapp_calls"):
        im.add([], [c.get("jid")])
    for e in raw["emails"]:
        for header in ("from", "to"):
            for addr in re.findall(EMAIL_RE, _s(e.get(header))):
                im.add([_display_name(_s(e.get(header)), addr)], [addr])
    for ev in raw["events"]:
        for a in (ev.get("attendees") or []):
            if isinstance(a, dict):
                im.add([a.get("displayName") or a.get("name")], [a.get("email")])
    for mtg in raw["granola"]:
        for addr in (mtg.get("attendee_emails") or []):
            im.add([], [addr])
    for entry in raw["ledger"]:
        im.add([entry.get("contact_name")], [entry.get("contact_identifier")])
    for co in raw["companies"]:
        im.add_company(co.get("name"))
        im.add_domain(co.get("domain"))
    for p in raw["people"]:
        im.add_company(p.get("company"))
    for ident in list(im._by_ident):            # every email domain seen anywhere gets a mapping
        if "@" in ident:
            im.add_domain(ident.split("@")[-1])
    im.freeze()
    return im


def _display_name(header: str, addr: str) -> str:
    m = re.match(r"^\s*\"?([^\"<]+?)\"?\s*<", header or "")
    name = _s(m.group(1)).strip() if m else ""
    return "" if name.lower() == _s(addr).lower() else name


# ── Scrub: structure ─────────────────────────────────────────────────────────────────────────────

_IDENT_FIELDS = ("handle", "contact_jid", "sender_jid", "chat_guid", "phone", "jid",
                 "contact_identifier")
# Clock fields are carried through untouched — the tokenizer owns them, and a timestamp that went
# through the text rewriter first comes out the other side as a phone number.
_TIME_FIELDS = ("timestamp", "date", "start", "end", "created_at", "updated_at", "due_date",
                "modified_date", "generated_at", "last_used", "date_added", "birthday",
                "last_researched", "captured_at", "expires_at",
                # Ledger clocks — every one of them, spelled the way the ledger spells it. A field
                # named here but not written ("chased_after") tokenizes nothing at all.
                "deadline", "resolved_at", "snoozed_until", "reopened_at", "prior_resolved_at",
                "chase_after", "chase_pending", "last_chased_at")
_TEXT_FIELDS = ("text", "body", "snippet", "subject", "summary", "title", "description",
                "notes", "ai_summary", "location", "group_name", "partner_name", "reason")


def scrub(obj, im: IdentityMap):
    """Walk any pipeline payload: identifier fields go through the map, prose fields through the
    text rewriter, address headers keep their shape ('Name <addr>'), everything else is left alone."""
    if isinstance(obj, list):
        return [scrub(v, im) for v in obj]
    if not isinstance(obj, dict):
        return obj
    out = {}
    for k, v in obj.items():
        if isinstance(v, list) and all(isinstance(x, str) for x in v) and k in _STR_LIST_FIELDS:
            out[k] = [_scrub_bare(k, x, im) for x in v]
        elif isinstance(v, (dict, list)):
            out[k] = scrub(v, im)
        elif k in _TIME_FIELDS:
            out[k] = v
        elif k in ("from", "to", "organizer", "creator") and isinstance(v, str):
            out[k] = _scrub_address_header(v, im)
        elif k in _IDENT_FIELDS and isinstance(v, str):
            out[k] = im.identifier_for(v)
        elif k in ("email", "address") and isinstance(v, str):
            out[k] = im.email_for(v) if "@" in v else v
        elif k in ("name", "displayName", "contact_name", "sender") and isinstance(v, str):
            out[k] = im.name_for(name=v) or im.rewrite_text(v)
        elif k in ("phones", "emails") and isinstance(v, str):
            out[k] = im.identifier_for(v)
        elif k == "anchor_key" and isinstance(v, str):
            out[k] = im.rewrite_text(v)
        elif k in _TEXT_FIELDS and isinstance(v, str):
            out[k] = im.rewrite_text(v)
        elif isinstance(v, str) and ("@" in v or len(_digits(v)) >= 10) and _parse_any(v)[0] is None:
            out[k] = im.rewrite_text(v)
        else:
            out[k] = v
    return out


def _scrub_bare(key: str, value: str, im: IdentityMap) -> str:
    """A bare string inside an identifier/name LIST (contacts.emails, attendee_emails, mute_people…)
    — the shape a plain recursive walk sails straight past, which is exactly how PII escapes."""
    v = _s(value).strip()
    if not v:
        return v
    if key in ("mute_people",):
        return im.name_for(name=v) or im.rewrite_text(v)
    if key == "mute_senders" and v.startswith("@"):
        dom = v[1:].lower()
        return "@" + (im.domain_alias.get(dom) or im._alloc_domain(dom, set()))
    return im.identifier_for(v)


_STR_LIST_FIELDS = ("phones", "emails", "identifiers", "attendee_emails", "group_participants",
                    "mute_senders", "mute_people", "participants", "cc", "recipients")


def _scrub_address_header(header: str, im: IdentityMap) -> str:
    addr = _sender_addr(header) or ""
    if not addr:
        return im.rewrite_text(header)
    fake_addr = im.email_for(addr)
    fake_name = im.name_for(identifier=addr) or im.name_for(name=_display_name(header, addr))
    return f"{fake_name} <{fake_addr}>" if fake_name else fake_addr


# ── Tokenize: absolute clock → the harness's relative tokens ─────────────────────────────────────

def _parse_any(value: str):
    v = _s(value).strip()
    try:
        if _TS_ISO.match(v):
            dt = datetime.fromisoformat(v.replace("Z", "+00:00").replace(" ", "T", 1))
            return (dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt), "ISO"
        if _TS_NAIVE.match(v):
            return datetime.strptime(v, "%Y-%m-%d %H:%M:%S"), "TS"
        if _TS_DATE.match(v):
            return datetime.strptime(v, "%Y-%m-%d"), "D"
    except ValueError:
        return None, ""
    return None, ""


def tokenize(obj, base: datetime):
    """Every recognizable timestamp becomes {{TS-90m}} / {{ISO+3h}} / {{D-2d}} relative to the day's
    base — the exact vocabulary evals/run_evals.py already resolves, so a corpus day IS a fixture."""
    if isinstance(obj, list):
        return [tokenize(v, base) for v in obj]
    if isinstance(obj, dict):
        return {k: tokenize(v, base) for k, v in obj.items()}
    if not isinstance(obj, str):
        return obj
    dt, kind = _parse_any(obj)
    if dt is None:
        if _TS_MD.match(obj.strip()):           # Apple-Contacts birthday: MM-DD, year-agnostic
            try:
                d = datetime.strptime(f"{base.year}-{obj.strip()}", "%Y-%m-%d")
            except ValueError:
                return obj
            return _tok("MD", (d.date() - base.date()).days)
        return obj
    if kind == "D":
        return _tok("D", round((dt.date() - base.date()).days))
    minutes = round((dt - base).total_seconds() / 60)
    if minutes % 60 == 0:
        return _tok(kind, minutes // 60, "h")
    return _tok(kind, minutes, "m")


def _tok(kind: str, n: int, unit: str = "d") -> str:
    if kind in ("D", "MD"):
        unit = "d"
    return "{{%s}}" % kind if n == 0 else "{{%s%+d%s}}" % (kind, n, unit)


# ── Split: the volume's window → one fixture per day ─────────────────────────────────────────────

def _ts_of(item: dict, *keys) -> datetime | None:
    for k in keys:
        dt, _kind = _parse_any(_s(item.get(k)))
        if dt is not None:
            return dt
    return None


def drop_sensitive(raw: dict, senders: list, threads: list) -> int:
    """Layer 3 of the scrub model, the safe half: any sender or thread the owner names is dropped
    from the corpus ENTIRELY. Matched against the REAL identifiers, before pseudonymization — the
    owner names people he knows, not aliases he's never seen."""
    keys = {_normalize_identifier(s) for s in senders if _s(s).strip()}
    tids = {_s(t).strip() for t in threads if _s(t).strip()}
    if not keys and not tids:
        return 0
    dropped = 0

    def _keep(items, *ident_keys, thread_key=""):
        nonlocal dropped
        out = []
        for it in items:
            ids = {_normalize_identifier(_s(it.get(k))) for k in ident_keys}
            ids |= {_normalize_identifier(a) for a in re.findall(EMAIL_RE, _s(it.get("from")))}
            if (ids & keys) or (thread_key and _s(it.get(thread_key)) in tids):
                dropped += 1
                continue
            out.append(it)
        return out

    local = raw["local"]
    for key, fields in (("imessage", ("handle",)), ("whatsapp", ("contact_jid", "sender_jid")),
                        ("calls", ("phone",))):
        if isinstance(local.get(key), list):
            local[key] = _keep(local[key], *fields, thread_key="chat_guid")
    raw["emails"] = _keep(raw["emails"], "from", thread_key="threadId")
    return dropped


def split_days(raw: dict, days: int, end: datetime) -> list:
    """One day = [00:00, 24:00) of local wall-clock, most recent last. A day carries only the
    contacts its own messages reference, so day files stay small and referentially complete."""
    local, out = raw["local"], []
    buckets = {}
    def _put(day_key, section, item):
        buckets.setdefault(day_key, {}).setdefault(section, []).append(item)

    def _bucket(section, items, *keys):
        for it in items:
            dt = _ts_of(it, *keys)
            if dt is None:
                continue
            _put(dt.date(), section, it)

    _bucket("imessage", _arr(local, "imessage"), "timestamp")
    _bucket("whatsapp", _arr(local, "whatsapp"), "timestamp")
    _bucket("calls", _arr(local, "calls"), "timestamp")
    _bucket("emails", raw["emails"], "date")
    _bucket("events", raw["events"], "start")
    _bucket("granola", raw["granola"], "date")

    first = (end - timedelta(days=days - 1)).date()
    for n in range(days):
        d = first + timedelta(days=n)
        if d not in buckets:
            continue
        b = buckets[d]
        idents = set()
        for m in b.get("imessage", []):
            idents.add(_normalize_identifier(_s(m.get("handle"))))
        for m in b.get("whatsapp", []):
            idents.add(_normalize_identifier(_s(m.get("contact_jid"))))
        for c in b.get("calls", []):
            idents.add(_normalize_identifier(_s(c.get("phone"))))
        contacts = [c for c in _arr(local, "contacts")
                    if any(_normalize_identifier(_s(x)) in idents
                           for x in list(c.get("phones") or []) + list(c.get("emails") or []))]
        out.append({"date": d, "offset_days": (end.date() - d).days, "sections": b,
                    "contacts": contacts})
    return out


def day_fixture(day: dict, raw: dict, im: IdentityMap, user_email: str, tz: str) -> dict:
    """A corpus day in the EXACT shape evals/fixtures/*.json use — so the existing harness loads it
    with no new format and no new loader."""
    b, base = day["sections"], datetime.combine(day["date"], datetime.min.time()).replace(hour=12)
    local = {
        "generated_at": (base + timedelta(hours=11, minutes=59)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_hours": 24,
        "source_status": (raw["local"].get("source_status") or {}),
        "contacts": day["contacts"],
        "imessage": b.get("imessage", []),
        "whatsapp": b.get("whatsapp", []),
        "calls": b.get("calls", []),
        "reminders": _arr(raw["local"], "reminders"),
    }
    inputs = {
        "type": "morning", "first_run": False, "window_hours": 24,
        "google": {"userEmail": user_email, "userTimezone": tz,
                   "events": b.get("events", []), "emails": b.get("emails", [])},
        "granola": {"meetings": b.get("granola", [])},
        "local": local,
    }
    inputs = tokenize(scrub(inputs, im), base)
    inputs["google"]["userEmail"] = im.email_for(user_email) if user_email else ""
    n = day["offset_days"]
    return {
        "name": f"day-{n:02d}",
        "description": (f"corpus day T-{n}: {len(local['imessage'])} iMessage, "
                        f"{len(local['whatsapp'])} WhatsApp, {len(inputs['google']['emails'])} emails, "
                        f"{len(inputs['google']['events'])} calendar events"),
        "offset_days": n,
        "inputs": inputs,
        "signals": {"replied_thread_ids": [], "handled": []},
        "new_actions": [],
        "continuity_ledger": [],          # the replay accumulates the ledger across days, in order
    }


def stamp_gids(day: dict) -> dict:
    """Every scorable item gets a stable golden id (`_gid`) so labels can name it without naming a
    person. Extra keys are ignored by every consumer — the pipeline reads named fields only."""
    kinds = [("emails", day["inputs"]["google"]["emails"], "em"),
             ("events", day["inputs"]["google"]["events"], "ev"),
             ("imessage", day["inputs"]["local"]["imessage"], "im"),
             ("whatsapp", day["inputs"]["local"]["whatsapp"], "wa"),
             ("calls", day["inputs"]["local"]["calls"], "cl")]
    index = {}
    for _name, items, prefix in kinds:
        for i, it in enumerate(items, 1):
            if isinstance(it, dict):
                it["_gid"] = f"{prefix}-{i:02d}"
                index[it["_gid"]] = it
    return day


# ── Labels: the draft the owner corrects ─────────────────────────────────────────────────────────

_ASK_RE = re.compile(r"\?|\b(can you|could you|please|need|deadline|by (today|tomorrow|eod)|"
                     r"asap|urgent|sign off|approve|review|confirm)\b", re.I)


def draft_labels(days: list, im: IdentityMap, funnel_events, llm=None) -> dict:
    """Proposals, never verdicts. `entity_count` is real ground truth (it comes from the identity
    map, not the pipeline); the rest is a starting point the owner corrects in ~1 hour."""
    out = {"corpus": "", "reviewed": False, "days": {}}
    for day in days:
        emails = [e for e in day["inputs"]["google"]["emails"] if isinstance(e, dict)]
        needs, not_needs = [], []
        for e in emails:
            if e.get("isSent"):
                continue
            text = f"{_s(e.get('subject'))} {_s(e.get('snippet'))} {_s(e.get('body'))}"
            (needs if _ASK_RE.search(text) else not_needs).append(e["_gid"])
        nudge = {}
        for ev in funnel_events(day):
            gid = ev.get("_gid")
            if ev.get("source") == "calls":
                nudge[gid] = "nudge" if not ev.get("is_answered") and not ev.get("is_outgoing") else "drop"
            elif ev.get("is_group_chat"):
                nudge[gid] = "queue"
            else:
                nudge[gid] = "nudge" if _ASK_RE.search(_s(ev.get("text")) or _s(ev.get("body"))) else "queue"
        out["days"][day["name"]] = {
            "needs_attention": needs,
            "not_needs_attention": not_needs,
            "nudge": nudge,
            "entity_count": _entity_count(day, im),
            "open_loops_after": None,      # ← owner fills for the days they label; null = unscored
            "chases_after": None,          # ← {anchor_key: chased_count}; null = unscored
        }
    if llm is not None:
        _llm_refine(out, days, llm)
    return out


def _entity_count(day: dict, im: IdentityMap) -> int:
    """Ground truth for the dedup test: how many DISTINCT humans this day actually involves,
    counted on the map's groups — the answer the pipeline's identity resolution must reproduce."""
    blob = json.dumps(day["inputs"], ensure_ascii=False)
    return sum(1 for a in im.group_assets.values()
               if a["name"] in blob or any(x in blob for x in a["emails"] + a["phones"]))


_LABEL_PROMPT = """You are drafting GOLDEN LABELS for an eval corpus of one person's real (pseudonymized) day.
For each email id, say whether it deserved the brief's "Needs attention now" section — the things a
chief of staff would interrupt you about. Answer JSON only:
{"needs_attention": ["em-01", ...], "not_needs_attention": ["em-02", ...]}

DAY:
%s
"""


def _llm_refine(labels: dict, days: list, llm) -> None:
    """An LLM drafts, the owner corrects — correction is faster than authorship. A failed or
    unparseable call leaves the heuristic draft in place; label drafting never blocks a build."""
    for day in days:
        emails = [{"id": e.get("_gid"), "from": e.get("from"), "subject": e.get("subject"),
                   "snippet": _s(e.get("snippet"))[:400]}
                  for e in day["inputs"]["google"]["emails"] if isinstance(e, dict)]
        if not emails:
            continue
        try:
            raw = llm(_LABEL_PROMPT % json.dumps(emails, ensure_ascii=False, indent=1), {})
            got = json.loads(re.sub(r"^```(json)?|```$", "", _s(raw).strip(), flags=re.M))
            ids = {e["id"] for e in emails}
            entry = labels["days"][day["name"]]
            entry["needs_attention"] = [i for i in got.get("needs_attention", []) if i in ids]
            entry["not_needs_attention"] = [i for i in ids if i not in set(entry["needs_attention"])]
        except Exception as e:  # noqa: BLE001 — a draft is a convenience, never a build blocker
            print(f"   ! label draft for {day['name']} fell back to heuristics ({e})")


def emit_labels_yaml(labels: dict) -> str:
    """Hand-rolled YAML (the corpus builder stays stdlib-shaped, same discipline as run_evals'
    frontmatter emitter) — flat enough that PyYAML round-trips it and a human edits it happily."""
    L = [f"# Golden labels for {labels['corpus']} — DRAFT. See evals/LABELING.md.",
         "# Correct the proposals, fill every `open_loops_after:`, then set reviewed: true.",
         f"corpus: {labels['corpus']}",
         f"reviewed: {'true' if labels['reviewed'] else 'false'}",
         "days:"]
    for name, d in labels["days"].items():
        L.append(f"  {name}:")
        L.append("    needs_attention: [" + ", ".join(d["needs_attention"]) + "]")
        L.append("    not_needs_attention: [" + ", ".join(d["not_needs_attention"]) + "]")
        L.append("    entity_count: " + str(d["entity_count"]))
        L.append("    open_loops_after: " + ("null" if d["open_loops_after"] is None
                                             else "[" + ", ".join(d["open_loops_after"]) + "]"))
        L.append("    chases_after: " + ("null" if d.get("chases_after") is None
                                         else "{" + ", ".join(f"{k}: {v}" for k, v
                                                              in d["chases_after"].items()) + "}"))
        L.append("    nudge:")
        for gid, verdict in d["nudge"].items():
            L.append(f"      {gid}: {verdict}")
        if not d["nudge"]:
            L[-1] = "    nudge: {}"
    return "\n".join(L) + "\n"


# ── Leak scan: the last gate before anything is left on disk ─────────────────────────────────────

def leak_scan(blob: str, im: IdentityMap) -> list:
    """Every real string the map knows must be GONE from the emitted corpus. Reports the KIND and
    the count of each hit — never the value, because a leak report is itself a leak."""
    hits = []
    low = blob.lower()
    for real in sorted(im.real_strings()):
        needle = real.lower()
        n = low.count(needle)
        if n:
            hits.append((f"{'identifier' if ('@' in real or real.isdigit()) else 'name'}"
                         f":{hashlib.sha256(needle.encode()).hexdigest()[:8]}", n))
    return hits


# ── Main ─────────────────────────────────────────────────────────────────────────────────────────

def build(args) -> int:
    data_root = os.environ.get("SOTTO_DATA", "/data")
    out_dir = args.out or os.path.join(PACK, "evals", "corpus", args.version)
    map_path = args.map or os.path.join(data_root, "corpus-keys", f"{args.version}.map.json")
    if os.path.abspath(map_path).startswith(os.path.abspath(out_dir) + os.sep):
        print("ERROR: the identity map must live OUTSIDE the corpus (--map).")
        return 2

    key = bytes.fromhex(os.environ["SOTTO_CORPUS_KEY"]) if os.environ.get("SOTTO_CORPUS_KEY") \
        else (bytes.fromhex(_read_json(map_path, {}).get("key", "")) if os.path.exists(map_path)
              else secrets.token_bytes(32))
    if not key:
        key = secrets.token_bytes(32)

    print(f"== build_golden_corpus {args.version} ==")
    print(f"   data   : {data_root}")
    print(f"   corpus : {out_dir}")
    print(f"   map    : {map_path}  (never inside the corpus)")

    raw = gather(data_root, args.local, args.gmail, args.calendar, args.granola)
    dropped = drop_sensitive(raw, args.drop_sender, args.drop_thread)
    if dropped:
        print(f"-- dropped {dropped} item(s) from named sensitive senders/threads")
    user_email = args.user_email or ""
    im = build_map(raw, key, user_email)
    print(f"-- identity map: {len(im.alias)} people, {len(im.domain_alias)} domains, "
          f"key {im.key_fingerprint()}")

    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else datetime.now()
    days_raw = split_days(raw, args.days, end)
    if not days_raw:
        print("ERROR: no dated activity found in the window — nothing to build.")
        return 2
    days = [stamp_gids(day_fixture(d, raw, im, user_email, args.timezone)) for d in days_raw]

    llm = None
    if args.draft_labels_llm:
        from gemini import call_gemini      # _shared/lib is on sys.path (line 56)
        llm = call_gemini
    rg = _load_module(os.path.join("evals", "run_golden.py"), "run_golden")
    labels = draft_labels(days, im, rg.funnel_events, llm)
    labels["corpus"] = args.version

    # The seed ledger is a payload like any other, so it gets the same clock treatment: tokenized
    # against the OLDEST day's base (the day the replay starts from). Left absolute, every seeded
    # loop reads as future-dated for most of a replay that walks backwards — never chase-eligible,
    # never expiring, so `open_loops_after` scores nothing for any of them.
    oldest = datetime.combine(min(d["date"] for d in days_raw), datetime.min.time()).replace(hour=12)
    ledger = [{"filename": f"loop-{hashlib.sha256(json.dumps(e, sort_keys=True, default=str).encode()).hexdigest()[:8]}.md",
               "frontmatter": tokenize(scrub(e, im), oldest)}
              for e in raw["ledger"]]
    manifest = {
        "schema": CORPUS_SCHEMA,
        "version": args.version,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "key_fingerprint": im.key_fingerprint(),
        "timezone": args.timezone,
        "days": [{"name": d["name"], "offset_days": d["offset_days"], "file": f"days/{d['name']}.json"}
                 for d in days],
        "counts": {"people": len(im.alias), "days": len(days),
                   "emails": sum(len(d["inputs"]["google"]["emails"]) for d in days),
                   "messages": sum(len(d["inputs"]["local"]["imessage"])
                                   + len(d["inputs"]["local"]["whatsapp"]) for d in days)},
        "preferences": scrub(raw["preferences"], im),
        "seed_ledger": ledger,
        "note": "CONFIDENTIAL REGARDLESS — scrubbed is not shareable. See docs/plans/golden-corpus.md.",
    }

    blob = json.dumps({"manifest": manifest, "days": days}, ensure_ascii=False)
    hits = leak_scan(blob, im)
    if hits and not args.allow_leaks:
        print(f"LEAK SCAN: FAIL — {len(hits)} known real string(s) survived the scrub; nothing written.")
        for kind, n in hits[:20]:
            print(f"    {kind} × {n}")
        return 3
    print(f"LEAK SCAN: {'PASS' if not hits else f'{len(hits)} hit(s) FORCED PAST (--allow-leaks)'}")

    os.makedirs(os.path.join(out_dir, "days"), exist_ok=True)
    with open(os.path.join(out_dir, ".gitignore"), "w", encoding="utf-8") as f:
        f.write("# A corpus is real user data. It never gets committed, anywhere.\n*\n")
    for d in days:
        with open(os.path.join(out_dir, "days", d["name"] + ".json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1, sort_keys=True)
    labels_path = os.path.join(out_dir, "labels.yaml")
    if os.path.exists(labels_path) and not args.overwrite_labels:
        print(f"-- labels.yaml exists — kept (owner edits win); draft at labels.draft.yaml")
        labels_path = os.path.join(out_dir, "labels.draft.yaml")
    with open(labels_path, "w", encoding="utf-8") as f:
        f.write(emit_labels_yaml(labels))

    os.makedirs(os.path.dirname(map_path) or ".", exist_ok=True)
    fd = os.open(map_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(im.to_dict(), f, ensure_ascii=False, indent=1, sort_keys=True)

    print(f"-- wrote {len(days)} day(s), manifest, {os.path.basename(labels_path)}")
    print(f"-- identity map → {map_path} (0600, OUTSIDE the corpus, never needed to run evals)")
    print("\nNEXT: the labeling session — evals/LABELING.md (~1 hour, once per corpus version).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default="corpus-v1", help="corpus version dir name (default corpus-v1)")
    ap.add_argument("--out", help="corpus output dir (default evals/corpus/<version>)")
    ap.add_argument("--map", help="identity-map path (default $SOTTO_DATA/corpus-keys/<version>.map.json)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"window in days (default {DEFAULT_DAYS})")
    ap.add_argument("--end", help="last day of the window, YYYY-MM-DD (default: today)")
    ap.add_argument("--local", help="read_local snapshot (default $SOTTO_DATA/knowledge/last_local_snapshot.json)")
    ap.add_argument("--gmail", default="/tmp/sotto_gmail.json", help="gather_google --gmail-out")
    ap.add_argument("--calendar", default="/tmp/sotto_cal.json", help="gather_google --cal-out")
    ap.add_argument("--granola", default="/tmp/sotto_granola.json", help="gather_granola output")
    ap.add_argument("--user-email", dest="user_email", default="", help="the owner's own address")
    ap.add_argument("--timezone", default="+00:00", help="corpus timezone (default UTC)")
    ap.add_argument("--drop-sender", dest="drop_sender", action="append", default=[],
                    metavar="EMAIL_OR_PHONE",
                    help="drop everything from this person (repeatable) — the safe half of the "
                         "plan's layer-3 treatment for genuinely sensitive threads")
    ap.add_argument("--drop-thread", dest="drop_thread", action="append", default=[],
                    metavar="THREAD_ID", help="drop this Gmail threadId / group chat_guid (repeatable)")
    ap.add_argument("--draft-labels-llm", dest="draft_labels_llm", action="store_true",
                    help="let Gemini draft the needs-attention labels (needs GOOGLE_AI_API_KEY)")
    ap.add_argument("--overwrite-labels", dest="overwrite_labels", action="store_true",
                    help="replace an existing labels.yaml (default: keep it, write labels.draft.yaml)")
    ap.add_argument("--allow-leaks", dest="allow_leaks", action="store_true",
                    help="write the corpus even if the leak scan finds known real strings (don't)")
    return build(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
