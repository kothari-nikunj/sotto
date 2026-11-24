"""Shared read policy. The runtime owns conversation provenance; this checks the request independently."""
import json
from pathlib import Path
import re
import unicodedata

# Source checkout and packaged image locations; TS compiles this same JSON into dist.
_base = Path(__file__).resolve().parent.parent


def _policy(name):
    paths = (_base / 'sotto-chat/src/tools' / name, _base / 'runtime/sotto-chat/dist/tools' / name)
    return json.loads(next(path for path in paths if path.is_file()).read_text())


POLICY = _policy('read-policy.json')
READ_CONTEXT_WINDOW_MS = POLICY['contextWindowMs']
# Replies to a delivered message: an exact quote lasts while the outbox keeps its receipt (seven days);
# an unthreaded reply reaches back twelve hours. Neither is read permission.
_DELIVERED = _policy('delivered-context-policy.json')
DELIVERED_THREADED_WINDOW_MS = _DELIVERED['threadedWindowMs']
DELIVERED_UNTHREADED_WINDOW_MS = _DELIVERED['unthreadedWindowMs']


def _matches(key, text):
    return re.search(POLICY[key], unicodedata.normalize('NFC', text), re.I) is not None


def read_cancelled(text):
    return _matches('denied', re.sub(POLICY['courtesy'], '', unicodedata.normalize('NFC', text), flags=re.I))


def owner_read_request(text):
    return not read_cancelled(text) and not _matches('reported', text) and not _matches('personalRecall', text) and _matches('request', text)


def explicit_read_reference(text):
    return _matches('reference', text)


def prep_research_request(text):
    return owner_read_request(text) and explicit_read_reference(text) and not _matches('prepSavedOnly', text)


def subject_mention(text, subject):
    return re.search(r'(?<![\w@./-])' + re.escape(unicodedata.normalize('NFC', subject)) + r'(?![\w@/-]|\.[\w])', unicodedata.normalize('NFC', text), re.I) is not None


def owner_subject_mention(text, subject):
    return not read_cancelled(text) and not _matches('reported', text) and subject_mention(text, subject)


def valid_research_subject(subject, mode):
    if not isinstance(subject, str) or not subject or len(subject) > 200 or subject.strip() != subject or _matches('subjectSentence', subject) or _matches('streetAddress', subject):
        return False
    allowed = " .’'-" if mode == 'person' else " .&’'-"
    return (len(subject.split()) <= (6 if mode == 'person' else 8)
            and (subject[0].isalpha() if mode == 'person' else subject[0].isalnum())
            and all(c.isalpha() or unicodedata.category(c).startswith('M')
                    or mode == 'company' and c.isdigit() or c in allowed for c in subject))


def research_target_answer(text, subject, mode):
    match = re.fullmatch(POLICY['targetAnswer'], text.strip(), re.I)
    return mode == 'company' and match is not None and match[1].lower() == subject.lower()
