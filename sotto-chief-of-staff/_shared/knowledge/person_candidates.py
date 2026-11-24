"""Saved people a partial name could mean, for a person read that matched no one or several.

A candidate is a choice for the reader to confirm, never a match: each word of the request must
start a word of the saved name ("Avery" finds "Avery Quill"). Nothing is selected silently.
"""
from __future__ import annotations

import glob
import os

import knowledge as kg

LIMIT = 5


def candidates(selector: str) -> dict:
    words = selector.casefold().split()
    found = []
    for path in sorted(glob.glob(os.path.join(kg.people_dir(), '*.md'))):
        try:
            with open(path, encoding='utf-8') as handle:
                person = kg.parse_person_file(handle.read())
        except Exception:  # noqa: BLE001 - an unreadable file is not a candidate
            continue
        name = ' '.join(str(person.name or '').split())
        tokens = name.casefold().split()
        if words and all(any(token.startswith(word) for token in tokens) for word in words):
            identity = ', '.join(part for part in (person.title, person.company) if part)
            found.append({'name': name, 'canonical_id': person.canonical_id,
                          **({'identity': identity} if identity else {})})
    found.sort(key=lambda row: row['name'].casefold())
    return {'items': found[:LIMIT], 'omitted': max(0, len(found) - LIMIT),
            'scope': 'Saved people whose name starts with these words; confirm one, then read it by canonical_id.'}
