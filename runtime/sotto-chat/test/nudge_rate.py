"""Disposable triage-to-delivery fixture for the connected usefulness check."""
from datetime import datetime, timezone
import hashlib
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff/event-triage/scripts'))
import compose_notification  # noqa: E402
import triage_event  # noqa: E402
from adapters.sotto import client as channel_client  # noqa: E402

def deliver(receiver, command):
    fixture = json.loads(command.removeprefix('nudge-rate='))
    os.environ['SOTTO_CHAT_RUNTIME_URL'] = fixture['runtime']
    os.environ['SOTTO_CHAT_RUNTIME_TOKEN'] = 'fixture-only-notification-bearer'
    receiver._runtime_adapter()._client = lambda timeout=60: channel_client.ChatClient(
        fixture['runtime'], os.environ['SOTTO_CHAT_RUNTIME_TOKEN'], timeout=timeout)
    receiver._candidate_retained_route_digest = lambda: fixture['route']
    now = datetime.now(timezone.utc)
    triage_event.classify_event = lambda *_: ('agent', 'urgent', 'fixture', 'Cedar')
    result = triage_event.triage({'events': [{'source': 'gmail', 'id': 'original-gmail-message',
        'threadId': 'cedar-first-thread', 'from': 'Cedar <cedar@example.test>',
        'subject': 'Cedar update', 'text': 'The update is ready.', 'timestamp': now.isoformat()}]},
        now_local=now, now_utc=now)
    job_id = result['job_id']
    decision = result['bundle']['events'][0]['decision_id']
    os.environ['SOTTO_DELIVERY_RUN_ID'] = job_id
    compose_notification.gemini.provider_key = lambda _provider: 'fixture-only'
    evidence = receiver._composition_evidence('event')
    text = compose_notification.compose('event', result['bundle'], now=now, enrich_fn=lambda items, _: items,
        llm=lambda *_a, **_kw: json.dumps({'items': [{'id': decision, 'text': 'The Cedar update is ready.',
                                                      'draft': '', 'decline': ''}]}))
    effects = receiver._read_delivery_effects(job_id)
    delivered = receiver._deliver_text(text, 'event', decision_ids=effects['notification_decision_ids'],
        effects=effects['effects'], run_id=job_id, source_revision_at_composition=evidence['source_revision'],
        route_digest_at_composition=evidence['route_digest'],
        source_text_sha256=hashlib.sha256(receiver._delivery_body(text).encode()).hexdigest())
    print(json.dumps({'delivered': delivered, 'dispatch_id': job_id, 'decision_id': decision}), flush=True)
