"""Pure fail-closed provenance projection for future continuity read tools.

This module does not write grants, stamp/backfill records, or read source authority.
Every legacy row without versioned content provenance is withheld. A digest detects
stale metadata after a row changes; it is not an authority grant or signature.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
import re
from typing import Callable

CANONICAL_SOURCES = frozenset({"gmail", "calendar", "granola", "imessage", "whatsapp", "contacts", "calls", "whatsapp_calls"})
ALIASES = {"email": "gmail", "apple_mail": "gmail", "gmail": "gmail",
           "calendar": "calendar", "granola": "granola", "imessage": "imessage",
           "whatsapp": "whatsapp", "contacts": "contacts", "calls": "calls", "whatsapp_calls": "whatsapp_calls"}
OWNER_SOURCES = frozenset({"user_added", "user_correction"})
INTERNAL_KEYS = frozenset({"_path", "_malformed", "content_provenance"})
MAX_FRONTMATTER_BYTES = 64 * 1024
MAX_FRONTMATTER_NODES = 4096
HEX_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")


class UnknownProvenanceError(ValueError):
    """The row cannot be proved safe to project; no raw row content should escape."""


class _Budget:
    def __init__(self):
        self.nodes = 0
        self.bytes = 0

    def visit(self, size=0):
        self.nodes += 1
        # Account for the tag/container syntax while traversing, before allocating
        # an encoded tree. The final JSON length check remains an exact second bound.
        self.bytes += size + 8
        if self.nodes > MAX_FRONTMATTER_NODES or self.bytes > MAX_FRONTMATTER_BYTES:
            raise UnknownProvenanceError("oversized frontmatter")


def _canonical(value, budget: _Budget, depth=0):
    if depth > 16:
        raise UnknownProvenanceError("nested frontmatter")
    budget.visit()
    if value is None:
        return ["N"]
    if type(value) is str:
        if len(value) > 8192:
            raise UnknownProvenanceError("long frontmatter scalar")
        size = len(value.encode("utf-8"))
        if size > 8192:
            raise UnknownProvenanceError("long frontmatter scalar")
        budget.visit(size)
        return ["S", value]
    if type(value) is bool:
        return ["B", value]
    if type(value) is int:
        text = str(value)
        budget.visit(len(text))
        return ["I", text]
    if type(value) is float:
        if not math.isfinite(value):
            raise UnknownProvenanceError("non-finite frontmatter")
        text = repr(value)
        budget.visit(len(text))
        return ["F", text]
    if isinstance(value, datetime):
        text = value.isoformat()
        budget.visit(len(text))
        return ["T", text]
    if isinstance(value, date):
        text = value.isoformat()
        budget.visit(len(text))
        return ["D", text]
    if type(value) is list and len(value) <= 1024:
        return ["L", [_canonical(item, budget, depth + 1) for item in value]]
    if type(value) is dict and len(value) <= 1024 and all(type(key) is str for key in value):
        pairs = []
        if any(len(key) > 8192 for key in value):
            raise UnknownProvenanceError("long frontmatter key")
        for key in sorted(value):
            budget.visit(len(key.encode("utf-8")))
            pairs.append([key, _canonical(value[key], budget, depth + 1)])
        return ["M", pairs]
    raise UnknownProvenanceError("unrepresentable frontmatter")


def digest_frontmatter(row: dict) -> str:
    """Hash the entire *parsed* frontmatter except path/parser markers and the digest itself.

    This binds all currently displayed and source-bearing fields, including chase/delivery
    state and future fields; a later writer must deliberately update provenance or the row
    disappears from protected reads. YAML formatting alone does not change the digest.
    """
    if type(row) is not dict or len(row) > 1024 + len(INTERNAL_KEYS) or row.get("_malformed"):
        raise UnknownProvenanceError("malformed frontmatter")
    payload = _canonical({key: value for key, value in row.items() if key not in INTERNAL_KEYS}, _Budget())
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_FRONTMATTER_BYTES:
        raise UnknownProvenanceError("oversized frontmatter")
    return hashlib.sha256(b"sotto-ledger-content-v1\0" + encoded).hexdigest()


def _token(value):
    if type(value) is not str or not value or len(value) > 512 or any(ord(ch) < 32 for ch in value):
        raise UnknownProvenanceError("invalid source token")
    return value.strip().lower()


def _source(value):
    token = _token(value)
    if token not in ALIASES:
        raise UnknownProvenanceError("unknown source")
    return ALIASES[token]


def _one_field(ref: dict, keys: tuple[str, ...]):
    present = [ref[key] for key in keys if key in ref and ref[key] is not None]
    if not present:
        return None
    if any(type(value) is not str or not value.strip() or len(value) > 512 for value in present):
        raise UnknownProvenanceError("invalid evidence field")
    values = {value.strip() for value in present}
    if len(values) != 1:
        raise UnknownProvenanceError("conflicting evidence fields")
    return values.pop()


def _refs(value, *, granola_thread=False):
    if value is None or value == []:
        return set()
    if type(value) is dict:
        value = [value]
    if type(value) is not list or not value or len(value) > 20:
        raise UnknownProvenanceError("invalid evidence list")
    result = set()
    for ref in value:
        if type(ref) is not dict:
            raise UnknownProvenanceError("invalid evidence row")
        source = _one_field(ref, ("sourceType", "source_type", "source"))
        identity = _one_field(ref, ("sourceId", "source_id", "id"))
        if source is None or (identity is None and not (source == "granola" and granola_thread
                                                     and type(ref.get("snippet")) is str and ref["snippet"].strip())):
            raise UnknownProvenanceError("incomplete evidence row")
        result.add(_source(source))
    return result


def _ledger_origins(row, *, source_loop=False):
    found = set()
    dependencies = row.get('birthday_importance_sources')
    if dependencies is not None:
        if (row.get('resolution_mode') != 'birthday_greeting' or row.get('source') != 'contacts'
                or type(dependencies) is not list
                or any(type(source) is not str or source not in CANONICAL_SOURCES for source in dependencies)
                or dependencies != sorted(set(dependencies))):
            raise UnknownProvenanceError('invalid birthday importance dependencies')
        found.update(dependencies)
    owner_marker = False
    origin = row.get("source")
    if origin is not None:
        if type(origin) is not str:
            raise UnknownProvenanceError("invalid origin")
        if origin in OWNER_SOURCES:
            owner_marker = True
        elif origin == "followup_commitment":
            found.add("granola")
        else:
            found.add(_source(origin))
    thread = row.get("source_thread_id")
    granola_thread = type(thread) is str and thread.startswith("granola:")
    request_refs = _refs(row.get("source_refs"), granola_thread=granola_thread)
    found |= request_refs
    # A closing message can be on another channel. It adds a consent dependency,
    # but cannot change which source originally bound this thread/message identity.
    origins = set(found)
    found |= _refs(row.get("resolution_source_refs"), granola_thread=granola_thread)
    if thread is not None and thread != "":
        if type(thread) is not str or len(thread) > 512:
            raise UnknownProvenanceError("invalid thread")
        if thread.startswith("granola:") and len(thread) > len("granola:"):
            found.add("granola")
        elif thread.startswith("manual:") and len(thread) > len("manual:"):
            if not owner_marker:
                raise UnknownProvenanceError("unbound manual thread")
        elif thread.startswith("commitment:"):
            if origin != "followup_commitment" or "granola" not in request_refs:
                raise UnknownProvenanceError("legacy commitment thread")
        elif origins != {"gmail"} and not (source_loop and origin in CANONICAL_SOURCES and origin in origins):
            raise UnknownProvenanceError("opaque thread has no unique Gmail provenance")
    if row.get("source_message_id") not in (None, ""):
        if type(row["source_message_id"]) is not str or len(row["source_message_id"]) > 512 or ("gmail" not in origins and not (source_loop and origin in CANONICAL_SOURCES and origin in origins)):
            raise UnknownProvenanceError("unbound message ID")
    return found, owner_marker, thread


def source_loop_sources(row):
    """Strict source identity for existing unstamped source rows; never grants or stamps."""
    if type(row) is not dict or row.get("_malformed"):
        raise UnknownProvenanceError("invalid source loop")
    if str(row.get('source') or '').strip().lower() in ('apple_mail', 'mail') or str(row.get('channel') or '').strip().lower() in ('apple_mail', 'mail'):
        raise UnknownProvenanceError("ambiguous Apple Mail authority")
    for refs in (row.get('source_refs'), row.get('resolution_source_refs')):
        for ref in ([refs] if isinstance(refs, dict) else refs or []):
            if not isinstance(ref, dict) or any(str(ref.get(key) or '').strip().lower() in ('apple_mail', 'mail')
                                               for key in ('source', 'sourceType', 'source_type')):
                raise UnknownProvenanceError("ambiguous source authority")
    if row.get("content_provenance") is not None:
        kind, sources = classify_ledger_sources(row)
        if kind != "sources":
            raise UnknownProvenanceError("not a source loop")
        return sources
    found, owner_marker, _thread = _ledger_origins(row, source_loop=True)
    if owner_marker or not found:
        raise UnknownProvenanceError("not a source loop")
    return frozenset(found)


def classify_ledger_sources(row: dict) -> tuple[str, frozenset[str]]:
    """Return ('owner'|'sources', source IDs), only for a fresh complete v1 record."""
    if type(row) is not dict:
        raise UnknownProvenanceError("invalid row")
    proof = row.get("content_provenance")
    if type(proof) is not dict or len(proof) != 4 or set(proof) != {"version", "kind", "sources", "digest"}:
        raise UnknownProvenanceError("missing versioned content provenance")
    if type(proof["version"]) is not int or proof["version"] != 1 or proof["kind"] not in ("owner", "sources"):
        raise UnknownProvenanceError("unsupported content provenance")
    if type(proof["digest"]) is not str or not HEX_DIGEST.fullmatch(proof["digest"]) or proof["digest"] != digest_frontmatter(row):
        raise UnknownProvenanceError("stale content provenance")
    claimed = proof["sources"]
    if type(claimed) is not list or len(claimed) > len(CANONICAL_SOURCES) or any(type(source) is not str or source not in CANONICAL_SOURCES for source in claimed):
        raise UnknownProvenanceError("invalid claimed source set")
    if claimed != sorted(set(claimed)):
        raise UnknownProvenanceError("invalid claimed source set")
    found, owner_marker, thread = _ledger_origins(row)
    if proof["kind"] == "owner":
        if (not owner_marker or found or row.get("channel") != "manual" or not
                (type(thread) is str and thread.startswith("manual:") and len(thread) > 7) or
                row.get("source_brief_at") is not None or claimed):
            raise UnknownProvenanceError("owner content not isolated")
        return "owner", frozenset()
    if not found or sorted(found) != claimed:
        raise UnknownProvenanceError("incomplete source union")
    return "sources", frozenset(found)


def project_entries(rows: list[dict], permit: Callable[[str], bool]) -> list[dict]:
    """Filter *before* rendering/counting. Authority failure invalidates the whole call."""
    visible = []
    for row in rows:
        try:
            kind, sources = classify_ledger_sources(row)
        except (UnknownProvenanceError, TypeError, ValueError, KeyError):
            continue
        # Keep authority outside the malformed-row catch: a callback exception must
        # withhold the whole result rather than silently producing a partial list.
        if kind == "owner" or all(permit(source) is True for source in sources):
            visible.append(row)
    return visible
