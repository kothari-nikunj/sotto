#!/usr/bin/env python3
"""
brief_validate.py — deterministic post-hoc validator for a generated brief.

Roadmap v2 Sprint 1 item 6: machine-checkable rules move OUT of the extraction prompt's prose and
INTO code (the render_chat_text lesson — agents/models skip instructions; code doesn't). This module
is a PURE function over the brief output; it never blocks delivery. compose_brief.compose() runs it
post-extraction, logs violations as `[brief-validate]`, and feeds the violation list to the
critic/revise pass so the model fixes them.

Rules implemented (each returns a one-line violation string):
  a. every bold **Name** in the three action sections (Needs Attention Now / Should Handle Today /
     Already Handled) carries an `<!--id:...-->` marker immediately after it — except group-tagged
     names (the rendered source shows them as `### <name> [GROUP - no deep link]`), birthday lines,
     and anything inside the Coming Up schedule block.
  b. no person name appears as a bold entry twice across sections (one person = one entry).
  c. banned-phrase scan over the whole brief.
  d. Coming Up is capped at 5 content lines (a first-run brief gets +1 for the mandated trailing
     onboarding offer line — see _check_coming_up_length).
  e. every marker identifier (`<!--id:VALUE|ch:...-->`, `<!--meeting:event_id:VALUE|...-->`) appears
     VERBATIM somewhere in the rendered source text — an identifier the data never contained is a
     fabricated tap target.
  f. per action item, contextSummary / contextAsk / prose are pairwise DISTINCT (normalized
     token-overlap ratio > 0.8 fails — "one fact stated three ways").

Stdlib only. No I/O, no environment reads: validate() is pure and unit-testable.
"""
from __future__ import annotations

import re

# The three sections whose bold names are actionable entries (matched case-insensitively on the
# section header text so "## ✅ Already Handled" still matches).
_ACTION_SECTION_KEYS = ("needs attention", "should handle", "already handled")

BANNED_PHRASES = (
    "reached out",
    "following up",
    "high-priority tracked open loop",
    "waiting for your response",
    "require your immediate attention",   # matches "require(s) your immediate attention"
    "requires your immediate attention",
)

COMING_UP_MAX_LINES = 5

_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ID_MARKER_RE = re.compile(r"<!--id:([^|>]+?)\|")
_MEETING_MARKER_RE = re.compile(r"<!--meeting:event_id:([^|>]+?)\|")
_WORD_RE = re.compile(r"[a-z0-9']+")


def _is_coming_up_header(line: str) -> bool:
    """The Coming Up schedule block may open as a markdown heading OR a bold pseudo-header."""
    stripped = line.strip()
    m = _HEADING_RE.match(line)
    if m and "coming up" in m.group(1).lower():
        return True
    return bool(re.match(r"^\*\*coming up\*\*", stripped, re.I))


def _split_lines_with_sections(markdown: str):
    """Yield (line, section_key, in_coming_up) per line. section_key is the lowercased header text of
    the current `#`-heading (or "" before the first). Coming Up tracks BOTH forms: its own heading, or
    a bold `**Coming Up**` block that ends at the next heading."""
    section = ""
    in_coming_up = False
    for line in (markdown or "").splitlines():
        m = _HEADING_RE.match(line)
        if m:
            section = m.group(1).lower()
            in_coming_up = "coming up" in section
        elif _is_coming_up_header(line):
            in_coming_up = True
        yield line, section, in_coming_up


def _in_action_section(section_key: str) -> bool:
    return any(k in section_key for k in _ACTION_SECTION_KEYS)


def _is_birthday_line(line: str) -> bool:
    return "🎂" in line or "birthday" in line.lower()


def _is_group_name(name: str, rendered_source_text: str) -> bool:
    """Group threads render in the source as '### <label> [GROUP - no deep link]' — a bold name that
    matches a group label is exempt from the marker rule (groups have no deep link by design)."""
    return f"{name} [GROUP" in (rendered_source_text or "")


def _norm_tokens(text: str) -> set:
    return set(_WORD_RE.findall((text or "").lower()))


def _overlap_ratio(a: str, b: str) -> float:
    ta, tb = _norm_tokens(a), _norm_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _check_bold_markers_and_duplicates(markdown: str, rendered_source_text: str) -> list:
    violations = []
    seen_entries: dict = {}          # normalized name -> section it first appeared in
    for line, section, in_coming_up in _split_lines_with_sections(markdown):
        if not _in_action_section(section) or in_coming_up or _is_birthday_line(line):
            continue
        for m in _BOLD_RE.finditer(line):
            name = m.group(1).strip()
            if not name or name.lower() in ("coming up",):
                continue
            if _is_group_name(name, rendered_source_text):
                continue
            # (a) marker immediately after the closing ** of the bold name
            after = line[m.end():]
            if not after.startswith("<!--id:"):
                violations.append(
                    f"missing-marker: bold name '{name}' in section '{section}' has no <!--id:...--> "
                    f"marker immediately after it")
            # (b) one person = one entry — count only line-leading bold (the entry headers)
            if line.strip().startswith(("**", "- **", "* **")) and line.strip().find(f"**{m.group(1)}**") in (0, 2):
                key = name.lower()
                if key in seen_entries:
                    violations.append(
                        f"duplicate-entry: '{name}' appears as a bold entry in both "
                        f"'{seen_entries[key]}' and '{section}' — one person = one entry")
                else:
                    seen_entries[key] = section
    return violations


def _check_banned_phrases(markdown: str) -> list:
    low = (markdown or "").lower()
    hits = sorted({p for p in BANNED_PHRASES if p in low})
    # "require your immediate attention" is a substring of nothing else; collapse the s/no-s pair.
    if "requires your immediate attention" in hits and "require your immediate attention" in hits:
        hits.remove("require your immediate attention")
    return [f"banned-phrase: '{p}'" for p in hits]


def _check_coming_up_length(markdown: str, first_run: bool = False) -> list:
    count = 0
    for line, _section, in_coming_up in _split_lines_with_sections(markdown):
        if not in_coming_up:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if _HEADING_RE.match(line) or _is_coming_up_header(line):
            continue                     # the header itself isn't a content line
        count += 1
    # First brief: the one-time onboarding note MANDATES one trailing "what you can ask next" line
    # AFTER the brief, and Coming Up (usually last) absorbs it into this count. The offer is
    # prompt-mandated prose, not a fixed string, so a flag beats pattern-matching: allow the one
    # extra trailing line rather than have the critic delete a schedule line or the offer.
    limit = COMING_UP_MAX_LINES + (1 if first_run else 0)
    if count > limit:
        return [f"coming-up-overflow: {count} content lines (max {limit})"]
    return []


def _check_marker_identifiers(markdown: str, rendered_source_text: str) -> list:
    violations = []
    src = rendered_source_text or ""
    for regex, kind in ((_ID_MARKER_RE, "id"), (_MEETING_MARKER_RE, "meeting event_id")):
        for m in regex.finditer(markdown or ""):
            ident = m.group(1).strip()
            if ident and ident not in src:
                violations.append(
                    f"fabricated-identifier: {kind} '{ident}' does not appear verbatim in the source data")
    return violations


def _check_action_field_distinctness(action_items: list) -> list:
    violations = []
    for a in action_items or []:
        if not isinstance(a, dict):
            continue
        label = str(a.get("id") or a.get("contactName") or a.get("contact_name") or "?")
        fields = {
            "contextSummary": a.get("contextSummary") or a.get("summary") or "",
            "contextAsk": a.get("contextAsk") or "",
            "prose": a.get("prose") or "",
        }
        names = list(fields)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                fa, fb = fields[names[i]], fields[names[j]]
                if not fa or not fb:
                    continue
                if _overlap_ratio(fa, fb) > 0.8:
                    violations.append(
                        f"repetitive-action: action '{label}' has near-identical "
                        f"{names[i]}/{names[j]} — each must say something different")
    return violations


def validate(brief_markdown: str, action_items: list, rendered_source_text: str,
             first_run: bool = False) -> list:
    """Pure: run every machine-checkable brief rule; return a list of one-line violation strings
    (empty = clean). Never raises on malformed input — a validator crash must not cost a brief.
    `first_run` marks the one-time onboarding brief, whose mandated trailing offer line is allowed
    past the Coming Up cap (see _check_coming_up_length)."""
    violations = []
    try:
        violations += _check_bold_markers_and_duplicates(brief_markdown, rendered_source_text)
        violations += _check_banned_phrases(brief_markdown)
        violations += _check_coming_up_length(brief_markdown, first_run)
        violations += _check_marker_identifiers(brief_markdown, rendered_source_text)
        violations += _check_action_field_distinctness(action_items)
    except Exception:  # noqa: BLE001 — defensive: garbage in, no crash out
        pass
    return violations
