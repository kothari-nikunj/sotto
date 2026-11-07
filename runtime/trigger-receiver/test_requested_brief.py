"""Explicit requests use durable gallery delivery without replaying learning or scheduled effects."""
import importlib.util
import json
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
import threading
from http.server import ThreadingHTTPServer
import urllib.request
import urllib.error

import pytest

HERE = Path(__file__).parent
PACK = HERE.parents[1] / 'sotto-chief-of-staff'


@pytest.fixture
def rec(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-host')
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', 'owner')
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'photon')
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    spec = importlib.util.spec_from_file_location('requested_test_receiver', HERE / 'receiver.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'DATA', str(tmp_path))
    monkeypatch.setattr(module, 'CHAT_SEND_TOKEN', 'private-test')
    monkeypatch.setattr(module, 'MCP_TOKEN', 'read-test')
    monkeypatch.setattr(module, '_local_now', lambda: datetime(2026, 10, 2, 20, tzinfo=timezone.utc))
    monkeypatch.setattr(module, '_find_sotto_script', lambda *r: str(PACK.joinpath(*r)))
    monkeypatch.setattr(module, '_fresh_selfhost_brief_hold', lambda: False)
    import onboarding
    monkeypatch.setattr(onboarding, 'scheduled_hold', lambda _: False)
    return module


def request(**kw):
    return {'kind': 'evening', 'refresh': False, 'owner': 'owner', 'chat_id': 'owner',
            'message_id': 'message-1', **kw}


def seed(root, *, status='delivered', sources=None):
    ident = 'a' * 32
    box = root / 'events/outbox.json'
    box.parent.mkdir(parents=True, exist_ok=True)
    box.write_text(json.dumps({'rows': [{'id': ident, 'day': '2026-10-02', 'kind': 'brief',
        'status': status, 'acceptance_uncertain': status == 'pending',
        'payload': {'label': 'cron:sotto-evening-brief'}}]}))
    path = root / 'events/work-inputs/brief-fixture/artifact.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {'brief_text': 'Needs Attention Now\nReview proposal\n\nShould Handle Today\n'
        'Reply to Alex\n\nComing Up\nTomorrow planning\n\nStill open\nSend document',
        '_delivery_run_ids': [ident], '_source_permissions': sources if sources is not None else ['gmail'],
        'composed_at': 100, '_source_cutoff': '2026-10-02T12:00:00Z'}
    path.write_text(json.dumps(artifact))
    return path


def test_ingress_deduplicates_original_message_but_allows_explicit_repeat(rec):
    first = rec.request_owner_brief(request())[1]['request_id']
    repeated = rec.request_owner_brief(request())[1]
    assert repeated == {'status': 'known_request', 'request_id': first}
    other = rec.request_owner_brief(request(message_id='message-2'))[1]['request_id']
    assert other != first
    job = rec.WORK_QUEUE.get(rec.DATA, first)
    assert job['payload']['label'].startswith('requested:')
    assert json.loads(job['payload']['prompt'])['pack'] == str(PACK)
    assert rec._brief_delivery_gate(job['payload']['label'], '2026-10-02', first) == 'send'


@pytest.mark.parametrize('change', [{'owner': 'stranger'}, {'chat_id': 'stranger'},
                                     {'kind': '../../file'}, {'refresh': 'false'}, {'message_id': ''}])
def test_ingress_rejects_untrusted_values(rec, change):
    assert rec.request_owner_brief(request(**change))[0] in (400, 403)


def test_ambiguous_prior_gallery_cannot_trigger_replacement(rec, tmp_path):
    seed(tmp_path, status='pending')
    assert rec.request_owner_brief(request())[1]['status'] == 'pending_delivery'


def test_valid_owner_request_refreshes_activation_before_context_and_queue(rec, monkeypatch):
    import onboarding
    order = []
    monkeypatch.setattr(rec, '_activation_tick', lambda: order.append('activation'))
    monkeypatch.setattr(onboarding, 'scheduled_hold', lambda _: order.append('context') or False)
    original_spawn = rec._spawn_and_deliver
    def spawn(*args, **kwargs):
        order.append('queue')
        return original_spawn(*args, **kwargs)
    monkeypatch.setattr(rec, '_spawn_and_deliver', spawn)
    assert rec.request_owner_brief(request())[1]['status'] == 'queued'
    assert order == ['activation', 'context', 'queue']
    order.clear()
    for change in ({'owner': 'stranger'}, {'chat_id': 'stranger'}, {'refresh': 'false'}):
        assert rec.request_owner_brief(request(**change))[0] in (400, 403)
    monkeypatch.setattr(rec, '_deliver_target', lambda: 'telegram')
    assert rec.request_owner_brief(request())[0] == 409
    assert order == []


def _current_cache_proof(rec, monkeypatch):
    # The accepted artifact's retained source/route proof is still current (proved separately below).
    monkeypatch.setattr(rec, '_composition_evidence', lambda _: {'source_revision': 'a' * 64,
                                                                 'route_digest': 'b' * 64})
    monkeypatch.setattr(rec, '_requested_cache_current', lambda *_: True)


def test_accepted_replay_does_not_compose_or_reapply_actions(rec, tmp_path, monkeypatch):
    seed(tmp_path)
    monkeypatch.setattr(rec, '_source_context', lambda: SimpleNamespace(allowed=lambda _: True))
    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *a, **k: pytest.fail('replay called model runner'))
    _current_cache_proof(rec, monkeypatch)
    jid = rec.request_owner_brief(request())[1]['request_id']
    result = rec._execute_work(rec.WORK_QUEUE.get(rec.DATA, jid))
    assert result['text'].startswith('Needs Attention')
    assert result['effects'] == [{'kind': 'source_permissions', 'sources': ['gmail']}]
    assert result['coverage_until'] == '2026-10-02T12:00:00Z'


def test_cache_requires_same_day_accepted_binding_and_current_consent(rec, tmp_path):
    path = seed(tmp_path)
    load = rec.REQUESTED_BRIEF.accepted_artifact
    assert load(tmp_path, '2026-10-02', 'evening', lambda _: True)
    assert load(tmp_path, '2026-10-03', 'evening', lambda _: True) is None
    assert load(tmp_path, '2026-10-02', 'morning', lambda _: True) is None
    assert load(tmp_path, '2026-10-02', 'evening', lambda _: False) is None
    value = json.loads(path.read_text()); value.pop('_delivery_run_ids'); path.write_text(json.dumps(value))
    assert load(tmp_path, '2026-10-02', 'evening', lambda _: True) is None
    seed(tmp_path, status='pending')
    assert load(tmp_path, '2026-10-02', 'evening', lambda _: True) is None


def test_private_gateway_capability_required_at_actual_http_route(rec, monkeypatch):
    calls = []
    monkeypatch.setattr(rec, 'request_owner_brief', lambda b: calls.append(b) or (200, {'status': 'queued'}))
    server = ThreadingHTTPServer(('127.0.0.1', 0), rec.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    def post(headers):
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/chat/brief',
            data=json.dumps(request()).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
    try:
        assert post({}) == 401
        assert post({'Authorization': 'Bearer read-test'}) == 403
        assert not calls
        assert post({'Authorization': 'Bearer read-test', 'X-Sotto-Chat-Send': 'private-test'}) == 200
        assert len(calls) == 1
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_auto_request_replayed_across_noon_has_one_identity(rec, monkeypatch):
    monkeypatch.setattr(rec, '_local_now', lambda: datetime(2026, 10, 2, 11, tzinfo=timezone.utc))
    first = rec.request_owner_brief(request(kind='auto'))[1]['request_id']
    monkeypatch.setattr(rec, '_local_now', lambda: datetime(2026, 10, 2, 13, tzinfo=timezone.utc))
    assert rec.request_owner_brief(request(kind='auto'))[1]['request_id'] == first
    assert json.loads(rec.WORK_QUEUE.get(rec.DATA, first)['payload']['prompt'])['kind'] == 'morning'


def test_provider_in_flight_is_not_permission_for_another_send(rec, tmp_path):
    seed(tmp_path, status='pending')
    path = tmp_path / 'events/outbox.json'
    state = json.loads(path.read_text())
    state['rows'][0].update(acceptance='in_flight', acceptance_uncertain=False)
    path.write_text(json.dumps(state))
    assert rec.request_owner_brief(request())[1]['status'] == 'pending_delivery'


def test_replay_uses_gallery_outbox_even_with_daily_marker(rec, tmp_path, monkeypatch):
    seed(tmp_path)
    monkeypatch.setattr(rec, '_source_context', lambda: SimpleNamespace(allowed=lambda _: True))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'valid', lambda _: True)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'on_delivered', lambda _: True)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda *a: pytest.fail('must render gallery'))
    calls = []
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send_gallery', lambda *a: calls.append(a) or
                        (True, '', {'message_id': 'accepted', 'message_ids': ['1', '2', '3', '4']}))
    real = rec._load_shared_lib
    def visual(name):
        if name != 'visual_delivery':
            return real(name)
        def prepare(body, label, target, *a, **kw):
            assert label.endswith(':sotto-evening-brief') and target == 'photon'
            kw['diagnostics'].update(format='gallery')
            return {'summary': 'Evening brief', 'images': ['1.png', '2.png', '3.png', '4.png']}
        return SimpleNamespace(prepare=prepare)
    monkeypatch.setattr(rec, '_load_shared_lib', visual)
    _current_cache_proof(rec, monkeypatch)
    jid = rec.request_owner_brief(request())[1]['request_id']
    result = rec._execute_work(rec.WORK_QUEUE.get(rec.DATA, jid))
    for _ in range(2):
        rec._deliver_text(result['text'], result['label'], effects=result['effects'], run_id=jid)
    assert len(calls) == 1
    rows = json.loads((tmp_path / 'events/outbox.json').read_text())['rows']
    assert rows[-1]['status'] == 'delivered'
    assert len(rows[-1]['receipt']['message_ids']) == 4


def test_malformed_cache_and_transactional_offer_are_not_replayed(rec, tmp_path):
    path = seed(tmp_path)
    artifact = json.loads(path.read_text())
    for extra in ({'composed_at': 'corrupt'}, {'composed_at': float('nan')},
                  {'_review_candidate': {'question': 'Send it?'}}, {'_review_question': 'procedure'}):
        path.write_text(json.dumps({**artifact, **extra}))
        assert rec.REQUESTED_BRIEF.accepted_artifact(tmp_path, '2026-10-02', 'evening', lambda _: True) is None


@pytest.mark.parametrize('directed', [False, True])
def test_refresh_or_owner_direction_does_not_reuse_accepted_artifact(rec, tmp_path, monkeypatch, directed):
    seed(tmp_path)
    def new_composition(*a, **kw):
        raise RuntimeError('fresh composition boundary reached')
    monkeypatch.setattr(rec.subprocess, 'Popen', new_composition)
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **kw: None)
    admission = {'event_id': 'a' * 64} if directed else None
    options = {'composition_direction': 'focusing on the open pricing decision'} if directed else {}
    jid = rec.request_owner_brief(request(refresh=not directed, **options), admission=admission)[1]['request_id']
    with pytest.raises(RuntimeError, match='fresh composition boundary reached'):
        rec._execute_work(rec.WORK_QUEUE.get(rec.DATA, jid))


def test_sotto_cached_replay_retains_current_source_route_proof(rec, tmp_path, monkeypatch):
    import hashlib
    seed(tmp_path)
    monkeypatch.setattr(rec, '_source_context', lambda: SimpleNamespace(allowed=lambda _: True))
    jid = rec.request_owner_brief(request())[1]['request_id']
    # This regression isolates the cached result boundary; fixed profile admission is tested separately.
    monkeypatch.setattr(rec, '_validate_fixed_work', lambda *_: None)
    monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *_: None)
    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *a, **k: pytest.fail('cache replay spawned model'))
    proof = {'source_revision': 'a' * 64, 'route_digest': 'b' * 64}
    monkeypatch.setattr(rec, '_composition_evidence', lambda _: proof)
    monkeypatch.setattr(rec, '_requested_cache_current', lambda *_: True)
    result = rec._execute_work(rec.WORK_QUEUE.get(rec.DATA, jid))
    assert result['source_revision_at_composition'] == proof['source_revision']
    assert result['route_digest_at_composition'] == proof['route_digest']
    assert result['source_text_sha256'] == hashlib.sha256(rec._delivery_body(result['text']).encode()).hexdigest()
    for observations in ([None, None], [proof, {**proof, 'route_digest': 'c' * 64}]):
        observed = iter(observations)
        monkeypatch.setattr(rec, '_composition_evidence', lambda _: next(observed))
        with pytest.raises(rec._WorkDeferredError):
            rec._execute_work(rec.WORK_QUEUE.get(rec.DATA, jid))


def test_sotto_cache_requires_original_accepted_source_route_proof(rec, tmp_path, monkeypatch):
    artifact = json.loads(seed(tmp_path).read_text())
    proof = {'source_revision': 'a' * 64, 'route_digest': 'b' * 64}
    assert rec._requested_cache_current(artifact, proof) is False
    assert rec._requested_cache_current(artifact, None) is False
    # A legacy accepted artifact without retained original proof goes to fresh composition.
    monkeypatch.setattr(rec, '_source_context', lambda: SimpleNamespace(allowed=lambda _: True))
    jid = rec.request_owner_brief(request())[1]['request_id']
    monkeypatch.setattr(rec, '_validate_fixed_work', lambda *_: None)
    monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *_: None)
    monkeypatch.setattr(rec, '_composition_evidence', lambda _: proof)
    def fresh(*_):
        raise RuntimeError('fresh composition selected')
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(run_argv=fresh))
    with pytest.raises(RuntimeError, match='fresh composition selected'):
        rec._execute_work(rec.WORK_QUEUE.get(rec.DATA, jid))


@pytest.mark.parametrize('label,kind,queued', [
    ('cron:sotto-morning-brief', 'morning', False),
    ('cron:sotto-morning-brief', 'evening', True),
    ('brief:sotto-evening-brief', 'morning', True),
    ('run-now:sotto-morning-brief', 'evening', True),
    ('requested:' + 'b' * 32 + ':sotto-morning-brief', 'evening', True),
    ('unknown:sotto-morning-brief', 'evening', False),
    ('requested:malformed:sotto-morning-brief', 'evening', False),
    ('cron:sotto-welcome-brief', 'evening', False),
    ('cron:sotto-relationship-pulse', 'evening', False),
    (None, 'evening', False),
])
def test_uncertain_delivery_holds_its_edition_but_exact_opposite_can_queue(rec, tmp_path, monkeypatch, label, kind, queued):
    seed(tmp_path, status='pending')
    box = tmp_path / 'events/outbox.json'
    state = json.loads(box.read_text())
    state['rows'][0]['payload'] = {'label': label} if label is not None else {}
    state['rows'][0]['acceptance'] = 'unknown'
    box.write_text(json.dumps(state))
    before = json.dumps(state['rows'][0], sort_keys=True, separators=(',', ':')).encode()
    contacts = []
    original = rec._spawn_and_deliver
    monkeypatch.setattr(rec, '_spawn_and_deliver', lambda *a, **kw: contacts.append(True) or original(*a, **kw))
    result = rec.request_owner_brief(request(kind=kind, refresh=True))
    assert len(contacts) == int(queued)
    assert result[1]['status'] == ('queued' if queued else 'pending_delivery')
    after = json.loads(box.read_text())['rows'][0]
    assert json.dumps(after, sort_keys=True, separators=(',', ':')).encode() == before
    if queued:
        assert rec.WORK_QUEUE.get(str(tmp_path), result[1]['request_id']) is not None


def test_unverified_composition_direction_is_not_admitted(rec):
    assert rec.request_owner_brief(request(composition_direction='focus on pricing'))[0] == 400


def test_directed_delivered_artifact_is_not_replayed_as_ordinary_brief(rec, tmp_path):
    path = seed(tmp_path)
    artifact = json.loads(path.read_text())
    artifact['_composition_direction'] = True
    path.write_text(json.dumps(artifact))
    assert rec.REQUESTED_BRIEF.accepted_artifact(tmp_path, '2026-10-02', 'evening', lambda _: True) is None
