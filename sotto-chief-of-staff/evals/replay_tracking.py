#!/usr/bin/env python3
"""Offline, continuous tracking-contract probe against the current production functions.

Synthetic analogues of the September 4 audit, NOT exported private messages or an LLM quality
score. Each run owns a fresh temporary data root and advances all 29 days without resetting it.
Exit 1 while any contract assertion fails; tests of the harness do not turn those gaps green.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

PACK = Path(__file__).resolve().parents[1]
for rel in ("_shared/lib", "_shared/scripts", "_shared/knowledge", "morning-brief/scripts"):
    sys.path.insert(0, str(PACK / rel))

import continuity_resolve as cr  # noqa: E402
import draft_outcomes as drafts  # noqa: E402
import ledger_io  # noqa: E402
import knowledge_edit  # noqa: E402
import pending_offer  # noqa: E402
import render_local  # noqa: E402

BASE = datetime(2026, 8, 1, 9, tzinfo=timezone.utc)
CHECKPOINTS = (0, 1, 7, 15, 28)


@contextmanager
def sandbox():
    # Deliberately no --data-root or production-input option. Never reuse inherited SOTTO_DATA.
    with tempfile.TemporaryDirectory(prefix="sotto-tracking-") as directory:
        with patch.dict(os.environ, {"SOTTO_DATA": directory, "SOTTO_TIMEZONE": "+00:00"}):
            yield Path(directory)


def _action(name, phone, ask, kind="follow_up", **extra):
    return {"contact_name": name, "contact_identifier": phone, "channel": "imessage",
            "action_type": kind, "summary": ask, "ask": ask, **extra}


def _append(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def replay():
    checks, checkpoints = [], []
    with sandbox() as root, redirect_stderr(io.StringIO()):
        people = {
            "Ada": "+12025550101", "Bram": "+12025550102", "Cleo": "+12025550103",
            "Dov": "+12025550104", "Elin": "+12025550105", "Foss": "+12025550106",
            "Gita": "+12025550107", "Hale": "+12025550108", "Ilse": "+12025550109",
            "Jory": "+12025550110",
        }
        initial = [
            _action("Ada", people['Ada'], "Send the pricing document"),
            _action("Ada", people['Ada'], "Introduce the operations lead"),
            _action("Bram", people['Bram'], "Test the staging dashboard and report findings"),
            _action("Cleo", people['Cleo'], "Receive the signed contract", "waiting_on"),
            _action("Dov", people['Dov'], "Receive the supplier quote", "waiting_on"),
            _action("Elin", people['Elin'], "Review the deck and decide next steps"),
            _action("Foss", people['Foss'], "Send the implementation note", deadline="2026-08-04"),
        ]
        def find(name):
            return [r for r in ledger_io.load_entries() if r.get('contact_name') == name]

        def check(day, key, expected, observed):
            checks.append({"day": day, "id": key, "expected": expected,
                           "observed": observed, "met": expected == observed})

        def live(name):
            return any(r.get('status') in ledger_io.ACTIVE for r in find(name))

        def signal(person, text, sent_at, seen_at, source="imessage", rowid=1):
            assert sent_at <= seen_at <= now, 'The replay must not ingest events from its future'
            event = {"source": source, "is_from_me": True, "text": text,
                     "timestamp": sent_at.strftime("%Y-%m-%d %H:%M:%S"), "rowid": rowid}
            if source == 'whatsapp':
                event['contact_jid'] = people[person].lstrip('+') + '@s.whatsapp.net'
            else:
                event['handle'] = people[person]
            _append(root/'events/queue.jsonl', {"ts": _iso(seen_at), "verdict_class": "signal", "event": event})

        def draft(person, text, at):
            assert at <= now, 'The replay must not offer drafts from its future'
            row = {"ts": _iso(at), "channel": "imessage", "identifier": people[person],
                   "text": text, "action_type": "follow_up"}
            _append(root/'events/drafts.jsonl', row)
            return drafts._draft_key(row)

        def outcome(key):
            return next((r['outcome'] for r in _rows(root/'outcomes.jsonl') if r.get('action_id') == key), None)

        cross_key = late_key = pre_key = None
        duplicate_keys = []
        for day in range(29):
            now = BASE + timedelta(days=day)
            local, new = {}, initial if day == 0 else []
            if day == 1:
                local = {
                    "imessage": [{"handle": people['Ada'], "is_from_me": True,
                                  "timestamp": now.strftime('%Y-%m-%d %H:%M:%S'), "text": "Happy birthday!"}],
                    "calendar_events": [{"summary": "Team catch-up", "start": _iso(now + timedelta(hours=1)),
                                         "attendees": [{"displayName": "Bram"}]}],
                }
            if day == 6:
                cross_key = draft('Gita', 'Can you send the signed agreement this week?', now - timedelta(hours=3))
                late_key = draft('Hale', 'Can you send the updated implementation plan?', now)
                pre_key = draft('Ilse', 'The implementation review is complete.', now)
                signal('Gita', 'Can you send the signed agreement this week?', now - timedelta(hours=2), now - timedelta(hours=2), 'whatsapp')
                # A separate send makes the original lane appear alive; it proves no rejection.
                signal('Bram', 'See you tomorrow', now - timedelta(hours=1), now - timedelta(hours=1), rowid=2)
            if day == 7:
                local = {"imessage": [{"handle": people['Cleo'], "is_from_me": False,
                                      "timestamp": now.strftime('%Y-%m-%d %H:%M:%S'),
                                      "text": "Unrelated weekend photos: https://example.com/photos"}]}
                # Ingested within the offer window, but actually written before the offer.
                signal('Ilse', 'The implementation review is complete.', BASE + timedelta(days=5), now - timedelta(hours=2), rowid=4)
            if day == 8:
                # Two offers of similar wording cannot both claim the same edited send.
                wording = 'Can you send the updated supplier contract this week?'
                duplicate_keys = [draft('Jory', wording, now - timedelta(hours=h)) for h in (2, 1)]
                signal('Jory', wording + ' Tuesday would help.', now, now, rowid=5)
            if day == 15:
                # Bridge catches up with a real post-offer send, after the grader's window closed.
                signal('Hale', 'Can you send the updated implementation plan?', BASE + timedelta(days=6, hours=1), now, rowid=3)
            if day == 28:
                # A newly captured ask from this person is not the old fulfilled ask reopened.
                new = [_action('Ada', people['Ada'], 'Review the new budget')]

            cr.resolve({"today": now.strftime('%Y-%m-%d'), "new_actions": new, "local": local}, now)
            with patch.object(drafts.log_outcome, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = now
                drafts.run(now)

            if day == 0:
                check(day, 'independent_asks', 2, len(find('Ada')))
            if day == 1:
                check(day, 'birthday_is_not_fulfillment', True, live('Ada'))
                check(day, 'calendar_is_not_dashboard_review', True, live('Bram'))
            if day in (3, 6):
                row = find('Dov')[0]
                # Real delivery finalizer, with its normal pending precondition. The global daily
                # selection budget is not under test; the fixture chooses which reminder landed.
                row['chase_pending'] = now.strftime('%Y-%m-%d')
                with cr._ledger_lock():
                    cr._persist(row)
                cr.finalize_chase(row['anchor_key'], now)
            if day == 7:
                check(day, 'unrelated_link_is_not_contract', True, live('Cleo'))
                check(day, 'cross_channel_draft_recognized', 'executed', outcome(cross_key))
                row = find('Dov')[0]
                check(day, 'reminders_not_claimed_as_chases', False, 'chased' in render_local._chase_note(row))
                with patch.object(pending_offer, '_now', return_value=now):
                    pending_offer.set_offer('handoff', 'Keep waiting for the quote?', person='Dov', anchor_key=row['anchor_key'])
                    receipt = pending_offer.dismiss_reply('no')
                check(day, 'no_keeps_obligation', (True, 'declined'), (live('Dov'), receipt['action']))
                # Explicit closure supplies a positive control and a future occurrence test.
                knowledge_edit.op_loop(row['anchor_key'], 'resolved', now.strftime('%Y-%m-%d'))
            if day == 15:
                check(day, 'undated_work_survives_absence', True, live('Elin'))
                check(day, 'overdue_work_survives_absence', True, live('Foss'))
                check(day, 'late_real_send_recognized', 'executed', outcome(late_key))
                check(day, 'preoffer_send_not_credited', False, outcome(pre_key) in ('executed', 'edited_and_sent'))
                # Ambiguous adoption may remain unknown; choosing a winner is not required.
                credited = sum(outcome(k) in ('executed', 'edited_and_sent') for k in duplicate_keys)
                check(day, 'one_send_not_double_credited', True, credited <= 1)
                with patch.object(pending_offer, '_now', return_value=now):
                    check(day, 'no_offer_uses_conversation', 'no_offer', pending_offer.dismiss_reply('no')['action'])
            if day == 28:
                # Prove retrying a read/resolve does not resurrect terminal work without new input.
                cr.resolve({"today": now.strftime('%Y-%m-%d')}, now, merge=False)
                check(day, 'explicit_done_stays_done', ['resolved'], [r['status'] for r in find('Dov')])
                check(day, 'distinct_new_ask_keeps_old_history', 3, len(find('Ada')))
            if day in CHECKPOINTS:
                checkpoints.append({"day": day, "rows": [
                    {k: r.get(k) for k in ('contact_name', 'ask', 'status', 'resolution', 'chased_count')}
                    for r in ledger_io.load_entries()]})

    return {"schema": 1, "days_advanced": 29,
            "checks": checks, "checkpoints": checkpoints,
            "limitations": ["Synthetic inputs; no LLM extraction or user-judgment score",
                            "Does not test migration",
                            "Does not execute onboarding, brief generation, dashboard, or adapter reply binding",
                            "Reminder selection budget is bypassed to exercise delivered-reminder semantics"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    result = replay()
    print(json.dumps(result, indent=2, default=str))
    return int(any(not c['met'] for c in result['checks']))


if __name__ == '__main__':
    raise SystemExit(main())
