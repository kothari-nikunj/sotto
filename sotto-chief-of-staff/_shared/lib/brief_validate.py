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
  e2. every identifier on an ACTION OBJECT is one the source data carried (`allowed_identifiers`,
     the compose-time allowlist). Rule (e) reads the markdown only, so the identifiers the LINK
     BUILDER consumes — `contactIdentifier`, `emailReplyTo`, an event id — went unchecked: an
     external reviewer minted `sms:+19999999999` from a payload whose only recipient was
     `+15551234567`, with zero violations. This rule is normalized (a formatting variant of a real
     identifier is the same identity), and the composer ENFORCES it rather than logging it.
  f. per action item, contextSummary / contextAsk / prose are pairwise DISTINCT (normalized
     token-overlap ratio > 0.8 fails — "one fact stated three ways").
  g. every URGENT open/waiting action_ledger entry is NAMED somewhere in the brief, and the brief
     does not INVENTORY the rest — the open-items contract, restated (see `is_urgent`).
  h. a person Sotto already nudged the user about today (a DELIVERED nudge — compose_brief reads
     them off surfaced.jsonl ∩ delivery.jsonl) never leads the brief and takes at most
     ALREADY_NUDGED_MAX_LINES lines — "never tell you twice", measured rather than requested.
  i. a model-selected open ledger marker in an action section carries the saved summary, ask,
     and historical date into the existing critic/revision pass instead of counting a partial
     paraphrase as a complete task; two separately dated full fields of the same task request
     one complete ask with only distinct context rather than passing as a clean brief.

Pure: no I/O and no environment reads, so validate() is deterministic and unit-testable. Its one
import is textutil's identifier normalizer — the same one the rest of the pipeline keys identities
by, because "did the brief name this person?" is now answered by identity, not by prose.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from textutil import _normalize_identifier  # the ONE identifier normalizer (phones → full digits, NANP 1 dropped)
from calendar_context import SCHEDULE_DAY

# The three sections whose bold names are actionable entries (matched case-insensitively on the
# section header text so "## ✅ Already Handled" still matches).
_ACTION_SECTION_KEYS = ("needs attention", "should handle", "already handled")

# THE banned-phrase list, in code. Canonically stated ONCE, in _shared/references/voice.md (and
# distilled into the prompts from there); this tuple is its machine-checkable twin and must stay
# character-for-character equal to voice.md's eight. Everything that scans in Python imports it —
# tests/test_prompt_contract.py included — so there is no second list to drift.
BANNED_PHRASES = (
    "reached out",
    "following up",
    "has been reaching out regarding",
    "multiple emails received",
    "require your immediate attention",
    "needs a confirmation",
    "high-priority tracked open loop",
    "waiting for your response",
)

# Inflections of a banned phrase that the literal list can't catch. Reported under the canonical
# phrase, so widening the scan never invents a ninth banned phrase.
_BANNED_VARIANTS = {"requires your immediate attention": "require your immediate attention"}

COMING_UP_MAX_LINES = 5


def is_calendar_preview_note(line: str) -> bool:
    """Recognize legacy boilerplate for removal, never a meeting row."""
    plain = re.sub(r'^[\s*•-]+|[\s*]+$', '', line)
    return bool(re.fullmatch(r'Calendar preview\s*(?:[—–:-]|\()\s*open Calendar for the full schedule[.)\s:]*',
                             plain, re.I))


_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
# A tap marker's identifier, whether or not it carries the `|ch:` tail: `<!--id:VALUE|ch:x-->` and
# the bare `<!--id:VALUE-->` the prompt also documents both name a person, and both are evidence.
_ID_MARKER_RE = re.compile(r"<!--id:([^|>]+?)(?:\||-->)")
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
                        f"'{seen_entries[key]}' and '{section}' — review whether these repeat the same ask; "
                        "preserve distinct requests and different people sharing a name")
                else:
                    seen_entries[key] = section
    return violations


def _check_banned_phrases(markdown: str) -> list:
    low = (markdown or "").lower()
    hits = {p for p in BANNED_PHRASES if p in low}
    hits |= {canonical for variant, canonical in _BANNED_VARIANTS.items() if variant in low}
    return [f"banned-phrase: '{p}'" for p in sorted(hits)]


_SECTION_LABEL_WORDS = ('needs attention', 'should handle', 'already handled', 'still pending',
                        'still open', 'open loops', 'filtered', 'weekly review', 'what moved')


def _check_coming_up_length(markdown: str, first_run: bool = False) -> list:
    """Count the schedule lines a reader sees. A hidden marker is not a line. A bold `**Coming Up**`
    block ends at the next bold section label, and after its blank line only list rows continue
    it, so a sentence appended below the schedule is not counted as schedule."""
    count, bold_block, closed, after_blank = 0, False, False, False
    for line, _section, in_coming_up in _split_lines_with_sections(markdown):
        if _HEADING_RE.match(line):
            bold_block, closed, after_blank = False, False, False
        if not in_coming_up:
            continue
        stripped = line.strip()
        if _is_coming_up_header(line) and not _HEADING_RE.match(line):
            bold_block, closed, after_blank = True, False, False
            continue
        label = re.fullmatch(r'\*\*([^*]+)\*\*:?', stripped)
        if bold_block and (closed or (label and any(
                word in label.group(1).lower() for word in _SECTION_LABEL_WORDS))):
            closed = True
            continue
        if not stripped:
            after_blank = after_blank or (bold_block and count > 0)
            continue
        if bold_block and after_blank and '<!--loop:' in stripped:
            plain = stripped.lstrip('-•* ').strip()
            timed = re.match(rf'^(?:{SCHEDULE_DAY}[,:]?\s+)?'
                             r'(?:\d{1,2}(?::\d{2})?\s*[AP]M|All day)\s*[:—–-]',
                             plain, re.I)
            if not timed:
                # A marked obligation after the schedule belongs to the following
                # unheaded updates, as in the visual brief. Never count it as a meeting.
                closed = True
                continue
        if bold_block and after_blank and not re.match(r'[-•*]\s', stripped):
            continue
        if _HEADING_RE.match(line):
            continue                     # the header itself isn't a content line
        if is_calendar_preview_note(stripped) or not re.sub(r'<!--.*?-->', '', stripped).strip():
            continue
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


# ── THE fabricated-identifier test (one sentence, one implementation) ─────────────────────────────
# A tap target may only be an identifier the SOURCE DATA carried. compose_brief builds the allowlist
# from the gathered payload (build_tap_target_allowlist) and both callers — this validator and the
# link builder that refuses to mint a link — ask the question HERE, so "did the data contain this
# recipient?" can never come to mean two things.

# The action fields that can become a tap target. compose_brief strips exactly these from a refused
# action, so the list lives once, here, next to the check that reads it.
TAP_IDENTIFIER_FIELDS = ("contactIdentifier", "contact_identifier", "emailReplyTo", "emailThreadId",
                         "eventId", "event_id", "meetingLink")


def identifier_allowed(value: str, allowlist) -> bool:
    """Is `value` an identifier the source data carried? Normalized on BOTH sides through the
    pipeline's one identifier normalizer, so `+1 (555) 123-4567`, `15551234567` and
    `15551234567@s.whatsapp.net` are the same identity and `+19999999999` is not.

    `allowlist=None` means the caller has no source data to check against — that is ignorance, not
    evidence of fabrication, so everything is allowed (an empty SET, by contrast, is a payload that
    carried no identifiers at all and allows nothing)."""
    if allowlist is None:
        return True
    v = _normalize_identifier(str(value or ""))
    return bool(v) and v in allowlist


def _check_action_identifiers(action_items: list, allowlist) -> list:
    if allowlist is None:
        return []
    violations = []
    for a in action_items or []:
        if not isinstance(a, dict):
            continue
        label = str(a.get("id") or a.get("contactName") or a.get("contact_name") or "?")
        for field in TAP_IDENTIFIER_FIELDS:
            value = str(a.get(field) or "").strip()
            if value and not identifier_allowed(value, allowlist):
                violations.append(
                    f"fabricated-identifier: action '{label}' field {field} '{value}' is not in the "
                    f"source data — it can never be a tap target")
    return violations


def _check_action_loop_recipients(action_items: list, action_ledger: list,
                                  verified_loop_recipients=None) -> list:
    """Ask the existing revise pass to resolve a claimed task/recipient conflict.

    A globally observed address does not prove it belongs to the selected ledger row.
    Legacy identifierless rows have no recipient proof here and remain untouched.
    """
    by_anchor = {str(e.get('anchor_key')): e for e in open_entries(action_ledger)
                 if e.get('anchor_key')}
    violations = []
    for action in action_items or []:
        if not isinstance(action, dict):
            continue
        key = str(action.get('loopId') or action.get('loop_id') or '')
        row = by_anchor.get(key)
        if row is None or row.get('group_id'):
            continue
        claimed = _normalize_identifier(str(action.get('contactIdentifier') or
                                            action.get('contact_identifier') or ''))
        recorded = _entry_identifiers(row)
        # Optional trusted caller context, bound to this exact ledger row. A global
        # source allowlist or an alias claimed by the model does not establish ownership.
        aliases = verified_loop_recipients.get(key, ()) if isinstance(verified_loop_recipients, dict) else ()
        if isinstance(aliases, (set, frozenset, list, tuple)):
            recorded |= {_normalize_identifier(value) for value in aliases if isinstance(value, str)}
        if claimed and recorded and claimed not in recorded:
            violations.append(
                f"loop-recipient-mismatch: action for loop {key} names recipient "
                f"{action.get('contactIdentifier') or action.get('contact_identifier')} "
                f"but its saved identifier is {row.get('contact_identifier') or row.get('canonical_id')}. "
                f"Saved summary: {_one_line(row.get('summary'))}. "
                f"Saved ask: {_one_line(row.get('ask'))}. "
                "Use the exact source-backed task and recipient together, choose a different "
                "loop only if its own evidence supports the action, or remove the action. "
                "Keep both ledger tasks visible; do not infer completion or merge people by name.")
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
        # Prose may deliberately compose this action's background and exact next step.
        composed = (bool(fields['contextSummary'] and fields['contextAsk'])
                    and _overlap_ratio(fields['contextSummary'], fields['contextAsk']) <= 0.8
                    and all(_norm_tokens(fields[name]) <= _norm_tokens(fields['prose'])
                            for name in ('contextSummary', 'contextAsk')))
        names = list(fields)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                if composed and 'prose' in (names[i], names[j]):
                    continue
                fa, fb = fields[names[i]], fields[names[j]]
                if not fa or not fb:
                    continue
                if _overlap_ratio(fa, fb) > 0.8:
                    violations.append(
                        f"repetitive-action: action '{label}' has near-identical "
                        f"{names[i]}/{names[j]} — each must say something different")
    return violations


_NAME_WORD_RE = re.compile(r"^[A-Z][A-Za-z'’.-]*$")


def is_name_shaped(name: str) -> bool:
    """A person's name is one to three capitalized, digit-free words. Everything else that lands in
    `contact_name` — a meeting title ("Board sync"), an email prefix ("nikunj.k17") — is a machine
    label the brief has no reason to print verbatim, so it can never be the evidence token. Public
    because continuity_resolve asks the same question before resolving a counterpart against the
    knowledge graph: a machine label is not a person, and must never adopt a person's identity."""
    words = name.split()
    return 1 <= len(words) <= 3 and all(_NAME_WORD_RE.match(w) for w in words)


_LABEL_MIN_CHARS = 4          # shorter than this, a machine label is not distinctive evidence
_FULL_NAME_MIN_WORDS = 2      # "Sam" is a first name; a first name is shared


def _summary_word(entry: dict) -> str:
    """The longest 5+ character word of an entry's summary — the cheapest evidence that THIS loop,
    and not some other one, got mentioned."""
    words = sorted(_WORD_RE.findall(str(entry.get("summary") or "").lower()), key=len, reverse=True)
    return words[0] if words and len(words[0]) >= 5 else ""


def _ledger_token(entry: dict) -> str:
    """What must appear in the brief for this ledger entry to be DEMANDED of it: the contact's name
    when it is name-shaped, else the longest 5+ character word of its summary. An entry with neither
    has no honest token, so it is never reported missing — a token the brief was never going to
    contain would print the same "## Still open" line under every brief, forever. (It is not lost
    either: `unsurfaced_open_loops` picks up everything this cannot demand — see there.)"""
    name = str(entry.get("contact_name") or "").strip()
    if name and is_name_shaped(name):
        return name
    return _summary_word(entry)


def _entry_identifiers(entry: dict) -> set:
    """The identity values a brief's tap marker could carry for this entry — its contact_identifier
    and its canonical_id, normalized. EMPTY for a group, whose identity is a chat id the brief
    deliberately never renders as a tap target (there is no way to deep-link a group), so a group
    line can only ever be proven by its label."""
    if str(entry.get("group_id") or "").strip():
        return set()
    ids = {_normalize_identifier(str(entry.get(k) or "")) for k in
           ("contact_identifier", "canonical_id")}
    return {i for i in ids if i}


def _brief_marker_ids(markdown: str) -> set:
    """Every identity the brief actually tapped: the values inside its `<!--id:…-->` markers. This
    is the brief's OWN machine-readable claim about who each line is about — the only proof of
    "already told" that cannot be faked by a coincidence of names."""
    return {i for i in (_normalize_identifier(m.group(1))
                        for m in _ID_MARKER_RE.finditer(markdown or "")) if i}


def _evidence_tokens(entry: dict) -> list:
    """The prose strings that prove the brief named an entry which has NO identifier to prove it
    with — the only case still reduced to arguing from words. A FULL name (two or three words) or a
    distinctive non-name label ("FPV / Piston", which can never be demanded of a brief but, when the
    brief printed it, means the ask was told). Never a lone first name: "Sam" in a line about Sam
    Patel is not evidence about the other Sam, and treating it as evidence silently deleted his
    overdue ask from both the printed lines and the count."""
    name = str(entry.get("contact_name") or "").strip()
    if name and is_name_shaped(name):
        return [name] if len(name.split()) >= _FULL_NAME_MIN_WORDS else []
    tokens = [t for t in (_summary_word(entry),) if t]
    if len(name) >= _LABEL_MIN_CHARS and name not in tokens:
        tokens.append(name)
    return tokens


def _token_present(token: str, low: str) -> bool:
    """Whole-word match only: "Sam" is not surfaced by the word "samples"."""
    return re.search(r"(?<!\w)" + re.escape(token.lower()) + r"(?!\w)", low) is not None


def _surfaced(entry: dict, markdown: str) -> bool:
    """Did the brief already NAME this entry? Proven by IDENTITY, not by prose: the entry's own
    identifier appearing inside one of the brief's `<!--id:…-->` markers.

    Name tokens were wrong in both directions at once. Two people sharing a first name: Sam Patel's
    line suppressed the other Sam's chased ask, which then landed in neither the printed lines nor
    the count — silently gone. And a narrative that says "Maya" does not prove "Maya Chen" was told,
    so she was printed a second time under Still open — the exact double-tell this contract exists
    to kill. An identifier is neither ambiguous nor paraphrasable, so it settles both.

    The name-token fallback survives only for entries carrying NO identifier at all (a group label,
    a hand-added loop), and it requires the full name — see `_evidence_tokens`."""
    ids = _entry_identifiers(entry)
    if ids:
        return bool(ids & _brief_marker_ids(markdown))
    low = (markdown or "").lower()
    return any(_token_present(t, low) for t in _evidence_tokens(entry))


# ── THE urgency predicate (the open-items contract, in one place) ─────────────────────────────────
# One sentence: an open loop earns its own line in the brief only when it is OVERDUE, DUE WITHIN 24
# HOURS, or ALREADY CHASED without an answer — every other open loop is one quiet count line and is
# worked by the proactive nudges, not by the brief. Nothing here is model-judged; it reads the ledger
# row. The validator, the composer and any future caller import THIS function, so "urgent" cannot
# come to mean two things.
ACTION_DEADLINE_HORIZON_SECONDS = 24 * 3600

def open_entries(action_ledger) -> list:
    """The open/waiting rows of a ledger payload — what the contract is about."""
    return [e for e in action_ledger or []
            if isinstance(e, dict) and e.get("status") in ("open", "waiting")]


def _next_day(today: str) -> str:
    try:
        return (date.fromisoformat(today[:10])
                + timedelta(seconds=ACTION_DEADLINE_HORIZON_SECONDS)).isoformat()
    except (ValueError, TypeError):
        return ""


def is_urgent(entry: dict, today: str = "") -> bool:
    """Overdue, due within 24 hours, or already chased without an answer — UNTIL the hand-off
    question has been asked. `today` is the brief's own user-local date ("YYYY-MM-DD"); without one
    only the chase clock can speak, because a deadline means nothing without a day to compare it to.

    THE HAND-OFF ENDS URGENCY. A `waiting_on` never expires, and a chased one is urgent forever, so
    a debt Sotto had chased its full two times held a named line in EVERY brief from then on — the
    wall this contract deleted, rebuilding itself one row at a time. Once the hand-off question has
    been DELIVERED (`handoff_asked_at`, stamped by continuity_resolve's `--finalize-handoff`) the
    user has been asked and it is their move: the loop drops to the count line and `/app#loops` and
    stays there until they resolve it, drop it, or say keep waiting (which clears the stamp)."""
    if not isinstance(entry, dict):
        return False
    if entry.get('resolution_mode') == 'birthday_greeting':
        return False
    if str(entry.get("handoff_asked_at") or "").strip():
        return False
    try:
        if int(entry.get("chased_count") or 0) >= 1:
            return True          # Sotto already asked and nobody answered — that IS the escalation
    except (TypeError, ValueError):
        pass
    deadline = str(entry.get("deadline") or "")[:10]
    tomorrow = _next_day(str(today or ""))
    return bool(deadline and tomorrow and deadline <= tomorrow)


def _printable(entry: dict, today: str = "") -> bool:
    """Can this open loop earn its own named line? Urgent AND carrying an honest token to name it
    with. Everything else is the count line's — see `unsurfaced_open_loops`."""
    return is_urgent(entry, today) and bool(_ledger_token(entry))


def missing_open_loops(brief_markdown: str, action_ledger: list, today: str = "") -> list:
    """The PRINTABLE open/waiting ledger entries the brief never names — the raw list behind rule
    (g), exposed so compose_brief's backstop can render exactly the misses this rule flags."""
    return [e for e in open_entries(action_ledger)
            if _printable(e, today) and not _surfaced(e, brief_markdown)]


def unsurfaced_open_loops(brief_markdown: str, action_ledger: list, today: str = "") -> list:
    """Every open loop the brief neither NAMED nor is about to PRINT — exactly what the single count
    line stands for. Not merely the non-urgent ones: an URGENT row with no honest token (a machine
    label for a name, a summary of short words) can never be printed either, and used to be in
    neither list — shown nowhere, counted nowhere, gone. The two lists are complementary by
    construction, so every open row lands in precisely one of surfaced / printed / counted."""
    return [e for e in open_entries(action_ledger)
            if not _printable(e, today) and not _surfaced(e, brief_markdown)]


def _deterministic_lines(markdown: str, sections) -> list:
    """(line, section, is_composer_block) per line. The composer registers a section only
    when its inserted block is the LAST matching heading; earlier model-written sections
    with that heading are still judged as model output."""
    rows = list(_split_lines_with_sections(markdown))
    last = {}
    start = 0
    for index, (line, section, _cu) in enumerate(rows):
        if _HEADING_RE.match(line):
            start = index
            if section in sections:
                last[section] = start
    owned, current = [], None
    for index, (line, section, _cu) in enumerate(rows):
        if _HEADING_RE.match(line):
            current = index
        owned.append((line, section, section in sections and last.get(section) == current))
    return owned


def _deterministic_loop_anchors(markdown: str, sections) -> set:
    """Loops the composer's own backstop sections print, each by its exact loop marker."""
    return {anchor for line, _section, mine in _deterministic_lines(markdown, sections) if mine
            for anchor in re.findall(r'<!--loop:([^<>\s]+)-->', line)}


def _dropped_historical_task_hint(entry: dict, today: str) -> str:
    """Send the existing revise pass the one-task wording rule before it adds a dropped row."""
    try:
        historical = date.fromisoformat(str(entry.get('created_at') or '')[:10]) < date.fromisoformat(today[:10])
    except (TypeError, ValueError):
        return ''
    if not historical or not _one_line(entry.get('summary')) or not _one_line(entry.get('ask')):
        return ''
    from ledger_io import deadline_context
    due = deadline_context(entry, today)
    if due['deadline'] is not None:
        return (f" For this one historical row, render one complete canonical ask with absolute deadline "
                f"{due['deadline']} (days overdue: {due['days_overdue']}), not two separately dated task "
                "sentences. The recording date is request provenance, not its deadline. Saved relative "
                "words may be paraphrased into the correct absolute date; preserve every distinct ask.")
    return (' For this one historical row, use one complete canonical ask under its original date, '
            'with only distinct source-backed summary context, not two separately dated task '
            'sentences. Preserve genuinely separate asks and their dates.')


def _check_open_ledger_coverage(markdown: str, action_ledger: list, today: str = "",
                                deterministic_sections=()) -> list:
    printed = _deterministic_loop_anchors(markdown, deterministic_sections)
    return [f"dropped-open-loop: URGENT open ledger item '{_ledger_token(e)}' "
            f"(loop {e.get('anchor_key')}, recorded {str(e.get('created_at') or '')[:10] or 'date unknown'}) "
            f"never appears in the brief. Saved summary: {_one_line(e.get('summary'))}. "
            f"Saved ask: {_one_line(e.get('ask'))}. Show this exact task with its original date "
            f"or Already Handled with evidence; keep distinct asks separate"
            f"{_dropped_historical_task_hint(e, today)}"
            for e in missing_open_loops(markdown, action_ledger, today)
            if str(e.get('anchor_key') or '') not in printed]


# Grammar that never carries an ask, and the generic request verbs that never tell two asks
# apart ("call" is not "sign", so it stays). The composer's "task shown" matcher imports these:
# one rule for whether a line shows a task.
ASK_STOP_WORDS = frozenset(
    'the a an to of and for with on in at from by your you our my his her their it its '
    'this that up out back please about re over'.split())
TASK_STOP_WORDS = ASK_STOP_WORDS | {'asked', 'ask', 'send', 'review', 'reply', 'follow', 'is', 'was', 'or'}
_RELATIVE_DAY_WORDS = frozenset({'today', 'tomorrow', 'yesterday', 'tonight'})


def _one_line(value) -> str:
    return ' '.join(str(value or '').split())


def _repeats_dated_task_fields(line: str, summary: str, ask: str, recorded_label: str) -> bool:
    """Recognize two separately dated, exact fields of one row; never infer semantic duplicates."""
    prefix = f'From {recorded_label}: '

    def original_body(value):
        value = _one_line(value)
        if value.casefold().startswith(prefix.casefold()):
            value = value[len(prefix):]
        return value.strip(' “”‘’"').rstrip('.').casefold()

    summary_body, ask_body = original_body(summary), original_body(ask)
    if not summary_body or not ask_body or summary_body == ask_body:
        return False
    prose = re.sub(r'<!--.*?-->', '', line)
    dated = re.findall(re.escape(prefix) + r'(?:“([^”]*)”|‘([^’]*)’|"([^"]*)")',
                       prose, re.I)
    bodies = [_one_line(next((part for part in parts if part), '')).rstrip('.').casefold()
              for parts in dated]
    return summary_body in bodies and ask_body in bodies


def _check_marked_loop_wording(markdown: str, action_ledger: list, today: str = '') -> list:
    """Give the existing revise pass exact source wording for a selected but shortened task.

    Only model-authored action sections are checked. The deterministic Still open backstop is
    deliberately shorter, and a completed task in Already Handled needs evidence, not a replay
    of the original request. This rule is advisory; it never closes or hides a ledger row.
    """
    by_anchor = {str(e.get('anchor_key')): e for e in open_entries(action_ledger)
                 if e.get('anchor_key')}
    violations = []
    for line, section, in_coming_up in _split_lines_with_sections(markdown):
        if in_coming_up or not any(s in section for s in ('needs attention', 'should handle')):
            continue
        plain = re.sub(r'<!--.*?-->|[*_]', '', line).lower()
        shown = _norm_tokens(plain)
        marked = set(re.findall(r'<!--loop:([^<>\s]+)-->', line))
        for key in marked:
            row = by_anchor.get(key)
            if row is None:
                continue
            saved = (_one_line(row.get('summary')), _one_line(row.get('ask')))
            # The composer's rule: the line shows the task when it carries the whole summary,
            # or (with this marker) the whole precise ask. Only then is the wording complete.
            name_words = _norm_tokens(str(row.get('contact_name') or ''))
            terms = [(_norm_tokens(value) - TASK_STOP_WORDS) - name_words for value in saved]
            missing = [sorted(words - shown) for words in terms]
            if (len(terms[0]) >= 2 and not missing[0]) or (len(terms[1]) >= 2 and not missing[1]):
                missing = [[], []]
            recorded = str(row.get('created_at') or '')[:10]
            recorded_label = recorded or 'date unknown'
            missing_date = False
            repeated_fields = False
            try:
                when = date.fromisoformat(recorded)
                current = date.fromisoformat(today[:10])
                prior_year = when.year != current.year
                recorded_label = f'{when:%b} {when.day}' + (f', {when.year}' if prior_year else '')
                historical = when < current
                relative = bool((_norm_tokens(' '.join(saved)) & _RELATIVE_DAY_WORDS) & shown)
                labels = ((f'{when:%b} {when.day}' + (f', {when.year}' if prior_year else '')).lower(),
                          (f'{when:%B} {when.day}' + (f', {when.year}' if prior_year else '')).lower(),
                          recorded)
                missing_date = historical and relative and not any(label in plain for label in labels)
                # An old date attached to a quoted summary does not date a fresh-looking
                # "tomorrow" in the unquoted instruction after it (the observed relay case).
                quoted = re.sub(r'“[^”]*”|‘[^’]*’|"[^"]*"', '', plain)
                # A line carrying several task anchors may have a fresh sibling ask;
                # do not attribute its relative date to the historical row.
                undated_relative = (historical and len(marked) == 1 and
                                    bool(_norm_tokens(quoted) & _RELATIVE_DAY_WORDS &
                                         _norm_tokens(saved[1])))
                repeated_fields = (historical and len(marked) == 1 and
                                   _repeats_dated_task_fields(line, saved[0], saved[1], recorded_label))
            except (TypeError, ValueError):
                undated_relative = False
            if any(missing) or missing_date or undated_relative or repeated_fields:
                reasons = []
                if missing[0]:
                    reasons.append('missing summary words ' + ', '.join(missing[0]))
                if missing[1]:
                    reasons.append('missing ask words ' + ', '.join(missing[1]))
                if missing_date:
                    reasons.append('historical relative day lacks original date')
                if undated_relative:
                    reasons.append('historical relative day appears outside dated quotation')
                if repeated_fields:
                    reasons.append('summary and ask repeated as separate historical task sentences')
                repair = (f"Use one complete canonical ask in one quotation dated {recorded_label}; "
                          "retain only distinct source-backed context without a second task "
                          "sentence, and preserve any separate asks and dates."
                          if repeated_fields else
                          "Recorded " + recorded_label + "; keep that date attached to old "
                          "today/tomorrow wording and preserve other asks in the paragraph")
                from ledger_io import deadline_context
                if deadline_context(row, today)['deadline'] is not None:
                    repair = _dropped_historical_task_hint(row, today) or repair
                violations.append(
                    f"marked-loop-wording: loop {key} ({'; '.join(reasons)}). "
                    f"Saved summary: {saved[0]}. Saved ask: {saved[1]}. {repair}")
    return violations


# The wall this contract exists to kill: a model-written section that LISTS open loops instead of
# deciding about them. One line is a pointer ("9 other open loops — see /app#loops"); two or more is
# an inventory. The composer's own urgent backstop is appended after the pre-critic validation; the
# final validation names that block in `deterministic_sections`, so this rule only ever judges the
# model's own writing.
_INVENTORY_HEADING_RE = re.compile(r"still open|open loops|outstanding items|loose ends", re.I)
_INVENTORY_MAX_LINES = 1


def _check_no_open_loop_inventory(markdown: str, deterministic_sections=()) -> list:
    counts: dict = {}
    for line, section, mine in _deterministic_lines(markdown, deterministic_sections):
        if mine or not _INVENTORY_HEADING_RE.search(section or ""):
            continue
        stripped = line.strip()
        if stripped and not _HEADING_RE.match(line):
            counts[section] = counts.get(section, 0) + 1
    return [f"open-loop-inventory: section '{s}' lists {n} open loops — the brief names what is "
            f"urgent and points at the rest; it does not inventory them"
            for s, n in counts.items() if n > _INVENTORY_MAX_LINES]


# Rule (h) — "never tell you twice". The prompt asks the model to compress an already-delivered
# nudge to ONE matter-of-fact line and never to open with it; this is the measurement behind that
# ask. Two lines, not one, because the same person may legitimately also appear in Coming Up.
ALREADY_NUDGED_MAX_LINES = 2


def _content_lines(markdown: str) -> list:
    return [line for line, _section, _cu in _split_lines_with_sections(markdown)
            if line.strip() and not _HEADING_RE.match(line)]


def _check_already_nudged(markdown: str, nudged_senders) -> list:
    # Full names only: a single token is a common word as often as a person ("Will send the
    # redline", "Grace period", "Mark as read"), and rule (g) already learned that lesson.
    senders = []
    for s in (nudged_senders or []):
        s = str(s or "").strip()
        if len(s.split()) >= 2 and is_name_shaped(s) and s not in senders:
            senders.append(s)
    content = _content_lines(markdown)
    if not senders or not content:
        return []
    violations = []
    lead = content[0].lower()
    for s in senders:
        if _token_present(s, lead):
            violations.append(
                f"already-nudged: '{s}' opens the brief — Sotto already nudged the user about them "
                f"today; it compresses to one matter-of-fact line, never the opener")
            continue
        n = sum(1 for line in content if _token_present(s, line.lower()))
        if n > ALREADY_NUDGED_MAX_LINES:
            violations.append(
                f"already-nudged: '{s}' takes {n} lines — Sotto already nudged the user about them "
                f"today; at most {ALREADY_NUDGED_MAX_LINES} (one status line, plus a Coming Up "
                f"mention), never re-told as news")
    return violations


def validate(brief_markdown: str, action_items: list, rendered_source_text: str,
             first_run: bool = False, action_ledger: list | None = None,
             today: str = "", allowed_identifiers=None, already_nudged=None,
             deterministic_sections=(), reviewed_task_keys=(), verified_loop_recipients=None) -> list:
    """Pure: run every machine-checkable brief rule; return a list of one-line violation strings
    (empty = clean). Never raises on malformed input — a validator crash must not cost a brief.
    `first_run` marks the one-time onboarding brief, whose mandated trailing offer line is allowed
    past the Coming Up cap (see _check_coming_up_length). `action_ledger` is the open-loop ledger the
    brief was built from (rule g); omit it and that rule simply doesn't run. `today` is the brief's
    user-local date, which is what makes a deadline urgent or not. `allowed_identifiers` is the
    compose-time tap-target allowlist (rule e2); omit it and that rule doesn't run either.
    `already_nudged` is the list of sender names Sotto nudged the user about today (rule h); omit
    it and that rule doesn't run. `deterministic_sections` names the section headings the composer
    itself appended (its Still open backstop, the weekly review): the final check counts the loops
    they print as shown and does not read them as model-written inventory. `reviewed_task_keys`
    contains only tasks whose exact unchanged display was accepted by the existing critic;
    those skip lexical coverage checks, never identity or action-authority checks.
    `verified_loop_recipients` is trusted caller proof of aliases for exact ledger rows;
    model output never supplies it, and global source allowlists remain independent."""
    violations = []
    try:
        violations += _check_bold_markers_and_duplicates(brief_markdown, rendered_source_text)
        violations += _check_banned_phrases(brief_markdown)
        violations += _check_coming_up_length(brief_markdown, first_run)
        violations += _check_marker_identifiers(brief_markdown, rendered_source_text)
        violations += _check_action_identifiers(action_items, allowed_identifiers)
        violations += _check_action_loop_recipients(action_items, action_ledger, verified_loop_recipients)
        violations += _check_action_field_distinctness(action_items)
        unreviewed = [row for row in action_ledger or []
                      if not isinstance(row, dict) or row.get('anchor_key') not in reviewed_task_keys]
        violations += _check_open_ledger_coverage(brief_markdown, unreviewed, today,
                                                  deterministic_sections)
        violations += _check_marked_loop_wording(brief_markdown, unreviewed, today)
        violations += _check_no_open_loop_inventory(brief_markdown, deterministic_sections)
        violations += _check_already_nudged(brief_markdown, already_nudged)
    except Exception:  # noqa: BLE001 — defensive: garbage in, no crash out
        pass
    return violations
