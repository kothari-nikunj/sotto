"""Disposable queue and offline evidence fixtures for midday digest tests."""
import hashlib
import json
from datetime import datetime, timedelta, timezone


NOW = datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc)


def _entry(i, sender="Sarah Chen", cls="ambient", minutes_ago=0, text=None):
    ts = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ts": ts, "verdict_class": cls, "sender": sender,
            "event": {"source": "imessage", "rowid": i, "handle": "+1" + str(int(hashlib.sha256(sender.encode()).hexdigest()[:10], 16))[-10:],
                      "text": text or f"message {i}"}}


def _excerpt(conversation):
    index = next(i for i, m in enumerate(conversation['messages']) if m['text'])
    return {'message_index': index, 'excerpt': conversation['messages'][index]['text']}


def _write_queue(tmp_path, entries):
    d = tmp_path / "events"
    d.mkdir(parents=True, exist_ok=True)
    (d / "queue.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _proactive_entry(kind, title, cls="budget", minutes_ago=10):
    """A held proactive nudge, exactly as proactive_scan queues it: Sotto's own voice, with the
    nudge's title standing in for a sender."""
    ts = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ts": ts, "verdict_class": cls, "sender": title, "held_class": kind,
            "event": {"source": "proactive", "kind": kind, "key": f"{kind}:1", "text": title}}
