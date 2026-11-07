"""Offline artifact inspection using the existing journey fixture and production readers.

Usage: python inspect_commitment_journey.py /absolute/path/to/checkout
The caller may point at an isolated base checkout for a comparable run. Every source,
critic/model decision and delivery boundary is scripted; this is not model acceptance.
Only throwaway state is written. Unexpected socket attempts fail even if caught upstream.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

denied = []
def network_guard(event, args):
    if event in ('socket.__new__', 'socket.connect', 'socket.sendto', 'socket.getaddrinfo',
                 'socket.gethostbyname', 'socket.gethostbyaddr'):
        denied.append(event)
        raise AssertionError('No network allowed in scripted journey')
sys.addaudithook(network_guard)

checkout = Path(sys.argv[1])
pack = checkout / 'sotto-agent/sotto-chief-of-staff'
sys.path.insert(0, str(pack / 'tests'))
import conftest  # noqa: F401 — reuse the fixture suite's production module paths
import commitment_journey_fixture as fixture
import continuity_resolve as cr
import compose_brief
import knowledge_query
import proactive_scan

runs = []
for repeat in range(5):
    with tempfile.TemporaryDirectory(prefix='sotto-inspected-journey-') as directory:
        os.environ.update(SOTTO_DATA=directory, SOTTO_TIMEZONE='UTC', SOTTO_DEPLOYMENT_MODE='self-hosted',
                          SOTTO_CAPTURED_SOURCES='["gmail","imessage"]',
                          SOTTO_PROACTIVE_CAPTURED_SOURCES='["gmail","imessage"]')
        started = time.perf_counter()
        journey = fixture.run_journey(directory, stamped=True)
        resolved_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        history = knowledge_query._read_obligation_history(interactive=True, topic='pricing document', topic_only=True)
        history_ms = (time.perf_counter() - started) * 1000
        brief = compose_brief._protected_action_ledger({'now':fixture.CLOSE_AT.isoformat()})
        reminders = proactive_scan._open_loops()
        nudges = proactive_scan.scan([], reminders, {}, 'owner@example.test', fixture.CLOSE_AT)['nudges']
        model_calls = []
        def scripted_model(prompt, inputs, **kwargs):
            model_calls.append('critic' if inputs.get('_critic') else 'revision' if inputs.get('_revise') else 'extraction')
            if inputs.get('_critic'):
                return json.dumps({'patches':[], 'score':88, 'summary':'Scripted fixture review.',
                                   'loop_verdicts':[], 'mention_verdicts':[]})
            return json.dumps({'brief_markdown':'## Today\nNo new messages to summarize.', 'actions':[], 'loop_updates':[]})
        started = time.perf_counter()
        composed = compose_brief.compose({'now':fixture.CLOSE_AT.isoformat(), 'type':'morning',
            'local':{}, 'google':{'emails':[], 'events':[]}}, llm=scripted_model, critic=True)
        compose_ms = (time.perf_counter() - started) * 1000
        restart = cr.resolve(journey['capture_payload'], fixture.CLOSE_AT)
        os.environ['SOTTO_CAPTURED_SOURCES'] = '["imessage"]'
        revoked = knowledge_query._read_obligation_history(interactive=True, topic='pricing document', topic_only=True)
        assert history['items'][0]['status'] == 'resolved'
        assert [row['summary'] for row in brief] == ['Send the launch checklist']
        assert [row['title'] for row in nudges] == ['Maya Journey — Send the launch checklist']
        assert 'Send the launch checklist' in composed['brief_markdown']
        assert 'pricing' not in composed['brief_markdown'].lower()
        assert not revoked['items'] and revoked['absence_is_authoritative'] is False
        runs.append({'repeat':repeat+1,'reconcileMs':resolved_ms,'retainedHistoryMs':history_ms,
                     'rows':journey['rows'],'history':history,
                     'briefEligible':[x['summary'] for x in brief],
                     'reminderEligible':reminders,
                     'reminderNudges':nudges, 'briefOutput':composed.get('brief_markdown'),
                     'scriptedModelStages':model_calls, 'composeMs':compose_ms,
                     'restartOpen':[x['summary'] for x in restart['active']],
                     'revokedHistory':revoked})
assert not denied, denied
print(json.dumps({'scope':'Scripted critic and source boundaries; production writer/readers; no model, provider or device',
                  'modelRequests':0,'costUsd':0,'unexpectedNetwork':denied,'runs':runs},default=str,indent=2))
