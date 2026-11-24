"""Atomic admission into the bounded deferred-event JSONL file."""
import json
import os
import tempfile

from work_queue import event_item_key
from funnel_policy import PROACTIVE_SOURCE
from sotto_log import bounded_append


def _append_one(path, entry, max_bytes, keep_lines):
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size:
                stream.seek(-1, os.SEEK_END)
            prefix = '\n' if size and stream.read(1) != b'\n' else ''
    except FileNotFoundError:
        prefix = ''
    bounded_append(path, prefix + json.dumps(entry), max_bytes, keep_lines, already_locked=True)
    with open(path, 'rb') as stream:
        os.fsync(stream.fileno())


def _retained_indices(lines):
    known = {}
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict) and isinstance(row.get('event'), dict):
            if row['event'].get('source') != PROACTIVE_SOURCE:
                known.setdefault(event_item_key(row['event']), index)
    return known


def append_batch(path, entries, max_bytes, keep_lines, *, restoring=False):
    """Caller holds the queue sidecar lock through read, replace and fsync."""
    if len(entries) == 1 and not restoring:
        return _append_one(path, entries[0], max_bytes, keep_lines)
    try:
        with open(path, encoding='utf-8', newline='') as stream:
            lines = stream.readlines()
    except FileNotFoundError:
        lines = []
    known = _retained_indices(lines)
    added = []
    changed = False
    for entry in entries:
        key = event_item_key(entry['event'])
        if entry['event'].get('source') == PROACTIVE_SOURCE or key not in known:
            added.append(json.dumps(entry) + '\n')
            known[key] = None
        elif restoring and known[key] is not None:
            index = known[key]
            row = json.loads(lines[index])
            if not row.get('valve_withheld_at'):
                lines[index] = json.dumps({**row, 'valve_withheld_at': entry['valve_withheld_at']}) + '\n'
                changed = True
    if not (added or changed):
        return
    if sum(len(line.encode('utf-8')) for line in lines) > max_bytes:
        lines = lines[-keep_lines:]
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix='.queue-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.writelines(lines + added)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
