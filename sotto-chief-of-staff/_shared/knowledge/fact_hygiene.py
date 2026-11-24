"""What a saved record SHOWS: each exact fact once, and only facts about its own person.

Losing a true fact is worse than showing a duplicate, so both rules are the narrowest that work.
Shared by the fact writer (knowledge_update) and the readers (knowledge_query):
- Exact copies: facts that are identical after normalizing case, whitespace, quote glyphs,
  trailing punctuation, "&" as "and", the articles a/an/the and a leading own name show once (the
  newest copy). Every differently worded fact stays visible.
- Wrong person: an automatic fact that leads with a different graph person's full name, or with
  a different graph person's first name with a clear subject cue (never the owner or a common
  word such as Will or May), and has no pronoun or name part referring back to this person.
- One-off arrangement: an automatic, non-preference fact pairing a clock time with a single weekday,
  a date, or another one-off cue. Explicit cadence and unbounded weekday sets are recurring.
`unreadable` is the one predicate: the writer rejects with its reason and readers hide the fact.
Owner-typed sources (`user_edit`, `apple_contacts`) are never judged by either rule.
"""
from __future__ import annotations

import os
import re

import knowledge as kg

OWNER_SOURCES = frozenset({"user_edit", "apple_contacts"})  # same set prune_stale_facts protects
PRONOUNS = frozenset({"she", "her", "hers", "he", "him", "his", "they", "them", "their"})
_GLYPHS = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})
_LEADING_NAME = re.compile(r"\s*([A-Z][a-z][A-Za-z'-]*(?:\s+[A-Z][a-z][A-Za-z'-]*){1,3})\b")
_LEADING_WORD = re.compile(
    r"\s*([A-Z][a-z]+)(?:\s*:|(?:,\s*[^,.;!?]+,)?\s+"
    r"(?i:is|was|has|had|will|works?|worked|serves?|served|leads?|led|founded|joined|"
    r"started|invested|introduced|prefers?|lives?|reports?)\b)"
)
_ARTICLES = frozenset({"a", "an", "the"})
# First names that also start ordinary sentences as words; a leading one is never judged.
COMMON_WORD_NAMES = frozenset({
    "will", "may", "mark", "grant", "bill", "chase", "frank", "guy", "max", "pat", "ray", "rich",
    "art", "hope", "joy", "grace", "rose", "june", "april", "august", "summer", "dawn", "hunter"})
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*[ap]\.?m\b", re.I)
_WEEKDAY_WORD = r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thurs?|fri|sat|sun)\b"
_WEEKDAY = re.compile(_WEEKDAY_WORD, re.I)
_WEEKDAY_SET = re.compile(rf"{_WEEKDAY_WORD}\s*(?:,|[-–—]|(?:and|to|through)\b)\s*{_WEEKDAY_WORD}", re.I)
_DATE = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec|january|february|march|april|june|july"
                   r"|august|september|october|november|december)\.?\s+\d{1,2}(?:st|nd|rd|th)?\b"
                   r"|\b(?!24/7\b(?!/))\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b", re.I)
_ONE_OFF_CUE = re.compile(r"\b(?:offered|countered|reschedul(?:e|ed|ing)|(?:this|next|last)\s+week)\b", re.I)
_RECURRING = re.compile(r"\b(?:every|daily|(?:bi)?weekly|fortnightly|(?:bi)?monthly|quarterly|annually|yearly"
                        r"|(?:mon|tues|wednes|thurs|fri|satur|sun)days)\b", re.I)
_people_cache: dict = {}


def _name_words(name) -> set:
    return {w.casefold() for w in re.findall(r"[A-Za-z]+", name or "") if len(w) > 1}


def copy_key(text: str, name: str = "") -> str:
    """The normalized string two copies of one assertion share. `name` (the record's person)
    drops that person's own name from the front."""
    words = str(text or "").translate(_GLYPHS).replace("&", " and ").split()
    own, start = _name_words(name), 0
    while start < len(words) - 1 and words[start].casefold() in own:
        start += 1
    words = [w for i, w in enumerate(words[start:])
             if not (w in _ARTICLES or (i == 0 and w.casefold() in _ARTICLES))]
    return " ".join(words).casefold().rstrip(".,;:!?").rstrip()


def same_assertion(a: str, b: str, name: str = "") -> bool:
    return copy_key(a, name) == copy_key(b, name)


def _graph_people(index: str = "people") -> dict:
    """name slug → (canonical_id, identifiers), or with index="firsts" first name → list of those;
    rebuilt when the people directory changes."""
    directory = kg.people_dir()
    try:
        stamp = (directory, os.stat(directory).st_mtime_ns, len(os.listdir(directory)))
    except OSError:
        return {}
    if _people_cache.get("stamp") != stamp:
        people, firsts = {}, {}
        for path in kg.build_people_index()["by_name"].values():
            try:
                with open(path, encoding="utf-8") as handle:
                    person = kg.parse_person_file(handle.read())
            except (OSError, ValueError):
                continue
            entry = (person.canonical_id, {kg.normalize_identifier(str(i)) for i in person.identifiers})
            people[kg.safe_slug(person.name or "")] = entry
            first = re.match(r"[A-Za-z]{2,}\b", person.name or "")
            if first:
                firsts.setdefault(first.group(0).casefold(), []).append(entry)
        _people_cache.update(stamp=stamp, people=people, firsts=firsts)
    return _people_cache[index]


def _person_name(person) -> str:
    return person if isinstance(person, str) else person.name


def names_someone_else(text: str, person) -> bool:
    """True when a different graph person clearly leads the fact and it does not refer back to this person."""
    name, cid = (person, "") if isinstance(person, str) else (person.name, person.canonical_id)
    words = {w.casefold() for w in re.findall(r"[A-Za-z]+", text or "")}
    if words & (_name_words(name) | PRONOUNS):
        return False
    lead, first = _LEADING_NAME.match(text or ""), _LEADING_WORD.match(text or "")
    if lead:
        matches = [m for m in [_graph_people().get(kg.safe_slug(lead.group(1)))] if m]
    elif first and first.group(1).casefold() not in COMMON_WORD_NAMES:
        matches = _graph_people("firsts").get(first.group(1).casefold(), [])
    else:
        return False
    from timeutil import configured_user_email
    owner = kg.normalize_identifier(configured_user_email() or "")
    if any(owner and owner in idents for _, idents in matches):
        return False
    return any(match_cid and match_cid != cid for match_cid, _ in matches)


def _one_off(text: str) -> bool:
    text = text or ""
    return bool(_CLOCK.search(text) and not _RECURRING.search(text)
                and (_DATE.search(text) or _ONE_OFF_CUE.search(text)
                     or (_WEEKDAY.search(text) and not _WEEKDAY_SET.search(text))))


def unreadable(text: str, person, kind: str = ""):
    """Why an automatic fact does not belong on this person's record, or None. Owner-typed
    sources are exempt; callers check the source."""
    if names_someone_else(text, person):
        return "about_someone_else"
    if kind != "preference" and _one_off(text):
        return "one_off_arrangement"
    return None


def is_readable(fact, person) -> bool:
    return fact.source in OWNER_SOURCES or unreadable(fact.text, person, fact.type) is None


def observed_match(fact, text, person) -> bool:
    """Match readable copies and tombstones, so hidden legacy types cannot absorb visible facts."""
    return same_assertion(fact.text, text, person.name) and (
        is_readable(fact, person) or (fact.status == "archived" and fact.archived_reason != "stale")
    )


def _fact_key(fid, fact, name=""):
    return ("owner", fid) if fact.source in OWNER_SOURCES else copy_key(fact.text, name)


def readable_conflicts(person, state) -> list:
    """Keep only live conflict pairs whose facts belong in this person's context."""
    people = state.get('people')
    record = people.get(person.canonical_id) if isinstance(people, dict) else None
    pairs = record.get('pairs') if isinstance(record, dict) else None
    if not isinstance(pairs, list):
        return []
    return [pair for pair in kg.live_conflict_pairs(person, pairs[:kg.CONFLICT_PAIRS])
            if all(is_readable(person.facts[fid], person) for fid in pair)]


def readable_facts(ranked: list, person, conflicts=()) -> list:
    """Hide wrong-person and disputed facts; show the newest exact copy at the best rank."""
    name = _person_name(person)
    disputed = {_fact_key(fid, person.facts[fid], name) for pair in conflicts for fid in pair}
    kept = {}
    for fid, fact in ranked:
        key = _fact_key(fid, fact, name)
        if key in disputed or not is_readable(fact, person):
            continue
        previous = kept.get(key)
        if previous is None or (fact.last or "", fact.first or "") > (previous[1].last or "", previous[1].first or ""):
            kept[key] = (fid, fact)
    return list(kept.values())


def distinct_lines(lines: list, *, newest_first: bool) -> list:
    """Saved prose lines → each exact copy once, keeping the newest. Blank lines and headings stay."""
    order = range(len(lines)) if newest_first else range(len(lines) - 1, -1, -1)
    seen, keep = set(), set()
    for i in order:
        line = str(lines[i])
        if not line.strip() or line.lstrip().startswith("#"):
            keep.add(i)
        elif copy_key(line) not in seen:
            seen.add(copy_key(line))
            keep.add(i)
    return [line for i, line in enumerate(lines) if i in keep]


def distinct_news(news: list) -> list:
    """Company news is stored newest first."""
    return distinct_lines(news, newest_first=True)


def distinct_context(context: str) -> str:
    """Company context is appended, so its newest line is last."""
    return "\n".join(distinct_lines(str(context).split("\n"), newest_first=False))
