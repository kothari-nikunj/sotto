"""Future-only composition → accepted gallery → protected text, all offline."""
import importlib.util
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import receiver as rec


BRIEF = """Good morning — Monday, September 14
Needs Attention Now
Alex — Complete the preschool waiver by tomorrow; the form is still outstanding.
Should Handle Today
Jordan — Reply to the lunch invitation for Thursday.
Coming Up
10:00 AM — Team meeting
12:00 PM — Lunch with Alex
Already Handled
Sam — Replied yesterday.
Filtered
Donation blast and social chatter.
"""
BODY_DIGEST = hashlib.sha256(BRIEF.strip().encode()).hexdigest()


def _request(body, *, peer='127.0.0.1', auth=True, path='/runtime/brief-detail'):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    sent = []
    handler = SimpleNamespace(path=path, client_address=(peer, 1234),
                              headers={'Content-Length': str(len(raw)),
                                       'Content-Type': 'application/json'},
                              rfile=io.BytesIO(raw),
                              _authed=lambda token: auth and token == rec.MCP_TOKEN,
                              _send=lambda status, payload: sent.append((status, payload)))
    rec.Handler.do_POST(handler)
    return sent[0]


def _libraries():
    path = Path(rec.__file__).resolve().parents[2] / 'sotto-chief-of-staff/_shared/lib'
    spec = importlib.util.spec_from_file_location('protected_brief_fixture', path / 'protected_brief_detail.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    proof = rec._load_shared_lib('retained_brief_proof')
    return module, proof


def test_owner_detail_repairs_pending_activation_before_route_proof(monkeypatch):
    events = []
    state = {'acked': False}
    route = 'a' * 64

    def repair():
        events.append('repair')
        state['acked'] = True

    def source():
        events.append('source')
        return {'version': 1, 'revision': 'b' * 64, 'complete': True}

    def candidate():
        events.append('route')
        return route if state['acked'] else None

    def read(_root, body, *, source_revision, route_digest):
        events.append('read')
        assert body == {'latest': True, 'offset': 0, 'limit': 80}
        assert source_revision == 'b' * 64 and route_digest == route
        return {'status': 'not_found', 'coverage': 'accepted_artifacts_only'}

    monkeypatch.setattr(rec, '_activation_tick', repair)
    monkeypatch.setattr(rec, '_runtime_source_authority', source)
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', candidate)
    monkeypatch.setattr(rec, '_load_shared_lib', lambda name: SimpleNamespace(read=read, validate_selector=_libraries()[0].validate_selector)
                        if name == 'protected_brief_detail' else None)
    assert _request({'latest': True, 'offset': 0, 'limit': 80}) == (
        200, {'status': 'not_found', 'coverage': 'accepted_artifacts_only'})
    assert events == ['repair', 'source', 'route', 'read', 'source', 'route']


def test_owner_detail_requires_ack_and_never_repairs_untrusted_requests(monkeypatch):
    events = []
    monkeypatch.setattr(rec, '_activation_tick', lambda: events.append('repair'))
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {
        'version': 1, 'revision': 'b' * 64, 'complete': True})
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: None)
    assert _request({'latest': True, 'offset': 0, 'limit': 80}, auth=False)[0] == 401
    assert _request({'latest': True, 'offset': 0, 'limit': 80}, peer='192.0.2.10')[0] == 401
    assert _request(b'{"latest":true,"latest":true,"offset":0,"limit":80}')[0] == 400
    assert events == []
    assert _request({'latest': True, 'offset': 0, 'limit': 80})[0] == 503
    assert events == ['repair']


def test_node_gallery_unicode_payload_hash_contract():
    _reader, proof = _libraries()
    images = [hashlib.sha256(f'image-{index}'.encode()).hexdigest() for index in range(4)]
    assert proof.gallery_payload_hash('Résumé 📨 / 東京', images) == (
        '912ec2e24f8a0af4bcf90eb36fe264712422bfe0475abfbf1f418b14ea809210')
    assert proof.route_digest('photon', 'tenant-1', 'a' * 64, 'owner-é', 'account-1',
                              'line-1', 'imessage:iMessage;-;+15555550123~+15555550999') == (
        '5d1bb3f8b0ddb0a2b32969f08aaebaf488c9f0125390d44cea7d0cccd2c6d609')


def test_protected_json_rejects_float_overflow_and_duplicate_keys(tmp_path):
    reader, _proof = _libraries()
    document = tmp_path / 'document.json'
    document.write_text('{"accepted_at":1e999}')
    document.chmod(0o600)
    with pytest.raises(reader.UnavailableError):
        reader._private_json(tmp_path, ('document.json',), 1024)
    document.write_text('{"accepted_at":1,"accepted_at":2}')
    with pytest.raises(reader.UnavailableError):
        reader._private_json(tmp_path, ('document.json',), 1024)


def test_route_candidate_uses_receiver_private_expected_channel_and_live_ack(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_configured_tz_name', lambda: 'America/Los_Angeles')
    client = object()
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(
        _client=lambda timeout: client if timeout == 5 else None))
    original = rec._load_shared_lib
    seen = []
    def module(name):
        if name != 'runtime_channel_proof':
            return original(name)
        def check_current_channel(**kwargs):
            seen.append(kwargs)
            return {'version': 1, 'status': 'current_channel_proved',
                    'action_authorized': False, 'route_digest': 'a' * 64}
        return SimpleNamespace(check_current_channel=check_current_channel)
    monkeypatch.setattr(rec, '_load_shared_lib', module)
    assert rec._candidate_retained_route_digest() == 'a' * 64
    assert seen[0]['chat_client'] is client
    assert seen[0]['expected_channel_path'] == tmp_path / 'runtime/expected-channel.json'
    assert seen[0]['receiver_data_root'] == str(tmp_path)
    assert seen[0]['expected_zone'] == 'America/Los_Angeles'


def test_optional_composition_proof_never_changes_ordinary_runner_validation(monkeypatch):
    monkeypatch.setattr(rec, '_deliver_target', lambda: (_ for _ in ()).throw(
        RuntimeError('optional channel observation unavailable')))
    assert rec._composition_evidence('cron:sotto-morning-brief') is None
    assert rec._composition_evidence('background:fixture') is None


def _fixture(tmp_path, monkeypatch):
    reader, proof = _libraries()
    route = proof.route_digest('photon', '', 'e' * 64, 'owner', 'account', 'line',
                               'imessage:owner-guid~+15555550123')
    state = {'revision': 'a' * 64}
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'photon')
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', 'owner')
    monkeypatch.setenv('SOTTO_VISUAL_BRIEFS', '1')
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {
        'version': 1, 'revision': state['revision'], 'complete': True, 'bridge_sources': []})
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: route)
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(
        gallery_available=lambda: True, run_argv=lambda *_args: ['/fixture-runner'],
        read_usage=lambda _path: None, validate_fixed_work=lambda *_args: None))
    monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *_args: None)
    monkeypatch.setattr(rec, '_record_delivery', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rec, '_read_delivery_effects', lambda _run: {})
    monkeypatch.setattr(rec, '_composed_brief_recently', lambda *_args: True)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send_gallery', lambda presentation, *_args:
                        (True, '', {'acceptance': 'accepted', 'message_id': 'provider-parent',
                                    'route_proof_version': 1,
                                    'route_digest': route,
                                    'payload_hash': presentation['retained_text_proof'][
                                        'gallery_payload_sha256']}))
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'brief_gate', lambda *_args: 'send')
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'valid', lambda _payload: True)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'on_delivered', lambda _payload: None)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'on_invalid', lambda _payload: None)
    return reader, route, state


class FakeProcess:
    returncode = 0
    pid = 12345

    def communicate(self, timeout):
        return BRIEF, ''

    def poll(self):
        return 0


def test_new_accepted_proof_survives_restart_but_revoke_regrant_and_route_change_withhold(
        tmp_path, monkeypatch):
    reader, route, state = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *_args, **_kwargs: FakeProcess())
    job = {'id': 'fixture-run', 'kind': 'run', 'owner': 'worker', 'payload': {
        'runner': ['/fixture-runner'], 'prompt': '{}', 'label': 'cron:sotto-morning-brief'}}
    result = rec._execute_work(job)
    assert result['source_revision_at_composition'] == 'a' * 64
    assert result['route_digest_at_composition'] == route
    assert result['source_text_sha256'] == BODY_DIGEST
    rec.WORK_QUEUE.enqueue(str(tmp_path), 'run', job['payload'], key='fixture-run')
    leased = rec.WORK_QUEUE.claim(str(tmp_path), 'worker')
    rec.WORK_QUEUE.save_result(str(tmp_path), leased['id'], 'worker', result)
    saved = rec.WORK_QUEUE.get(str(tmp_path), leased['id'])['result']
    assert rec._deliver_text(saved['text'], saved['label'], run_id=leased['id'],
                             source_revision_at_composition=saved['source_revision_at_composition'],
                             route_digest_at_composition=saved['route_digest_at_composition'],
                             source_text_sha256=saved['source_text_sha256'])
    rows = json.loads((tmp_path / 'events/outbox.json').read_text())['rows']
    assert len(rows) == 1
    receipt = rows[0]['receipt']
    assert receipt['route_digest'] == route and 'retained_text_proof' in receipt
    artifact = receipt['visual_artifact_id']
    selector = {'artifact_id': artifact, 'offset': 0, 'limit': 80}
    found = reader.read(tmp_path, selector, source_revision=state['revision'], route_digest=route)
    assert found['status'] == 'found' and found['coverage'] == 'accepted_artifacts_only'
    assert found['text'] == BRIEF.strip()[:80]
    assert found['research_evidence'] == {'status': 'unavailable'}
    latest = reader.read(tmp_path, {'latest': True, 'offset': 0, 'limit': 80},
                         source_revision=state['revision'], route_digest=route)
    assert latest == found
    assert _request({'latest': True, 'offset': 0, 'limit': 80}) == (200, found)
    assert _request({'artifact_id': artifact, 'offset': 0, 'limit': 80}) == (200, found)
    assert _request({'latest': True, 'offset': 0, 'limit': 80}, auth=False)[0] == 401
    assert _request({'latest': True, 'offset': 0, 'limit': 80}, peer='192.0.2.10')[0] == 401
    for invalid in (b'{"latest":true,"latest":true,"offset":0,"limit":80}',
                    {'latest': 1, 'offset': 0, 'limit': 80},
                    {'artifact_id': '../secret', 'offset': 0, 'limit': 80},
                    {'latest': True, 'offset': 0, 'limit': 80, 'extra': 'x'}):
        assert _request(invalid)[0] == 400
    next_page = reader.read(tmp_path, {'artifact_id': artifact, 'offset': found['next_offset'],
                                       'limit': 80}, source_revision=state['revision'], route_digest=route)
    assert next_page['status'] == 'found'
    manifest_path = tmp_path / 'cache/visual-briefs' / artifact / 'manifest.json'
    original_manifest = manifest_path.read_bytes()
    altered = json.loads(original_manifest)
    altered['full_text'] = altered['full_text'] + ' later source text'
    manifest_path.write_text(json.dumps(altered))
    assert reader.read(tmp_path, selector, source_revision='a' * 64,
                       route_digest=route)['status'] == 'withheld'
    manifest_path.write_bytes(original_manifest)
    image_path = manifest_path.parent / '01.png'
    original_image = image_path.read_bytes()
    image_path.write_bytes(original_image + b'changed-after-acceptance')
    assert reader.read(tmp_path, selector, source_revision='a' * 64,
                       route_digest=route)['status'] == 'withheld'
    image_path.write_bytes(original_image)
    # Re-import the protected reader as a fresh receiver process would.
    restarted, _ = _libraries()
    assert restarted.read(tmp_path, selector, source_revision='a' * 64,
                          route_digest=route)['status'] == 'found'
    manifest_path.unlink()
    manifest_path.symlink_to(tmp_path / 'events/outbox.json')
    assert reader.read(tmp_path, selector, source_revision='a' * 64,
                       route_digest=route)['status'] == 'unavailable'
    manifest_path.unlink()
    manifest_path.write_bytes(original_manifest)
    manifest_path.chmod(0o600)
    state['revision'] = 'b' * 64  # revoked, while the old Boolean fingerprint could stay true
    assert _request({'latest': True, 'offset': 0, 'limit': 80})[1]['status'] == 'withheld'
    assert reader.read(tmp_path, selector, source_revision=state['revision'],
                       route_digest=route)['status'] == 'withheld'
    state['revision'] = 'c' * 64  # regranted before the next query
    assert reader.read(tmp_path, selector, source_revision=state['revision'],
                       route_digest=route)['status'] == 'withheld'
    assert reader.read(tmp_path, selector, source_revision='a' * 64,
                       route_digest='d' * 64)['status'] == 'withheld'


def test_legacy_sidecar_tampering_and_wrong_route_never_mint_proof(tmp_path, monkeypatch):
    reader, route, _state = _fixture(tmp_path, monkeypatch)
    artifact = 'a' * 24
    folder = tmp_path / 'cache/visual-briefs' / artifact
    folder.mkdir(parents=True)
    manifest = {'id': artifact, 'kind': 'brief', 'preview': False, 'title': 'Brief',
                'full_text': 'Old private text'}
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    (folder / 'delivery-legacy.json').write_text(json.dumps({'acceptance': 'accepted'}))
    outbox = tmp_path / 'events/outbox.json'
    outbox.parent.mkdir()
    outbox.write_text(json.dumps({'rows': [{'status': 'delivered', 'acceptance': 'accepted',
        'receipt': {'visual_artifact_id': artifact, 'message_id': 'legacy', 'accepted_at': 1}}]}))
    outbox.chmod(0o600)
    selector = {'artifact_id': artifact, 'offset': 0, 'limit': 80}
    assert reader.read(tmp_path, selector, source_revision='a' * 64,
                       route_digest=route)['status'] == 'withheld'
    assert reader.read(tmp_path, {**selector, 'offset': True},
                       source_revision='a' * 64, route_digest=route)['status'] == 'invalid_selector'


def test_provenance_omitted_when_accepted_route_or_current_source_changes(tmp_path, monkeypatch):
    _reader, route, state = _fixture(tmp_path, monkeypatch)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send_gallery', lambda presentation, *_args:
                        (True, '', {'acceptance': 'accepted', 'message_id': 'provider-parent',
                                    'route_proof_version': 1,
                                    'route_digest': 'f' * 64,
                                    'payload_hash': presentation['retained_text_proof'][
                                        'gallery_payload_sha256']}))
    assert rec._deliver_text(BRIEF, 'cron:sotto-morning-brief', run_id='wrong-route',
                             source_revision_at_composition='a' * 64,
                             route_digest_at_composition=route,
                             source_text_sha256=BODY_DIGEST)
    row = json.loads((tmp_path / 'events/outbox.json').read_text())['rows'][0]
    assert 'retained_text_proof' not in row['receipt']
    state['revision'] = 'b' * 64
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send_gallery', lambda *_args:
                        (True, '', {'acceptance': 'accepted', 'message_id': 'provider-next'}))
    assert rec._deliver_text(BRIEF, 'cron:sotto-morning-brief', run_id='changed-source',
                             source_revision_at_composition='a' * 64,
                             route_digest_at_composition=route,
                             source_text_sha256=BODY_DIGEST)
    rows = json.loads((tmp_path / 'events/outbox.json').read_text())['rows']
    assert all('retained_text_proof' not in row['receipt'] for row in rows)


def test_source_change_during_composition_or_provider_contact_never_mints_proof(tmp_path, monkeypatch):
    _reader, route, state = _fixture(tmp_path, monkeypatch)

    class ChangedDuringComposition(FakeProcess):
        def communicate(self, timeout):
            state['revision'] = 'b' * 64
            return BRIEF, ''

    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *_args, **_kwargs: ChangedDuringComposition())
    job = {'id': 'changed-compose', 'kind': 'run', 'owner': 'worker', 'payload': {
        'runner': ['/fixture-runner'], 'prompt': '{}', 'label': 'cron:sotto-morning-brief'}}
    assert rec._execute_work(job)['source_revision_at_composition'] is None

    state['revision'] = 'a' * 64
    route_state = {'digest': route}
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: route_state['digest'])

    class ChangedRouteDuringComposition(FakeProcess):
        def communicate(self, timeout):
            route_state['digest'] = 'f' * 64
            return BRIEF, ''

    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *_args, **_kwargs: ChangedRouteDuringComposition())
    assert rec._execute_work(job)['route_digest_at_composition'] is None
    route_state['digest'] = route

    def changed_during_send(presentation, *_args):
        state['revision'] = 'c' * 64
        return True, '', {'acceptance': 'accepted', 'message_id': 'provider-parent',
                          'route_proof_version': 1, 'route_digest': route,
                          'payload_hash': presentation['retained_text_proof']['gallery_payload_sha256']}

    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send_gallery', changed_during_send)
    assert rec._deliver_text(BRIEF, 'cron:sotto-morning-brief', run_id='changed-contact',
                             source_revision_at_composition='a' * 64,
                             route_digest_at_composition=route,
                             source_text_sha256=BODY_DIGEST)
    row = json.loads((tmp_path / 'events/outbox.json').read_text())['rows'][0]
    assert row['status'] == 'delivered' and 'retained_text_proof' not in row['receipt']


def test_late_accepted_receipt_preserves_proof_without_resending(tmp_path, monkeypatch):
    reader, route, _state = _fixture(tmp_path, monkeypatch)
    sends = []
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send_gallery', lambda presentation, *_args:
                        sends.append(presentation) or
                        (False, '', {'acceptance': 'unknown'}))
    assert not rec._deliver_text(BRIEF, 'cron:sotto-morning-brief', run_id='late-proof',
                                 source_revision_at_composition='a' * 64,
                                 route_digest_at_composition=route,
                                 source_text_sha256=BODY_DIGEST)
    proof = sends[0]['retained_text_proof']
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'gallery_receipt', lambda _key: {
        'acceptance': 'accepted', 'message_id': 'late-parent', 'route_proof_version': 1,
        'route_digest': route, 'payload_hash': proof['gallery_payload_sha256']})
    with rec.CONNECTORS.json_transaction(rec.OUTBOX.path(), default={'rows': []}) as outbox:
        outbox['rows'][0]['next_at'] = 0
    rec.OUTBOX.drain()
    assert len(sends) == 1
    receipt = json.loads((tmp_path / 'events/outbox.json').read_text())['rows'][0]['receipt']
    assert receipt['retained_text_proof'] == proof
    selector = {'artifact_id': receipt['visual_artifact_id'], 'offset': 0, 'limit': 80}
    assert reader.read(tmp_path, selector, source_revision='a' * 64,
                       route_digest=route)['status'] == 'found'


def test_requested_cache_reuses_actual_markdown_gallery_only_with_original_current_proof(tmp_path, monkeypatch):
    _, route, state = _fixture(tmp_path, monkeypatch)
    text = BRIEF.replace('Needs Attention Now', '**Needs Attention Now**').replace('Reply to', '*Reply* to')
    ident = 'e' * 32
    assert rec._deliver_text(text, 'cron:sotto-morning-brief', run_id=ident,
                            source_revision_at_composition=state['revision'], route_digest_at_composition=route,
                            source_text_sha256=hashlib.sha256(rec._delivery_body(text).encode()).hexdigest())
    cached = {'brief_text': text, '_delivery_run_ids': [ident]}
    assert rec._requested_cache_current(cached, {'source_revision': state['revision'], 'route_digest': route})
    assert not rec._requested_cache_current(cached, {'source_revision': 'b' * 64, 'route_digest': route})
    assert not rec._requested_cache_current(cached, {'source_revision': state['revision'], 'route_digest': 'c' * 64})


def test_discovery_uses_full_proved_text_owner_date_and_original_retention(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo
    reader, route, state = _fixture(tmp_path, monkeypatch)
    for run, text in [('first', BRIEF + '\n' + 'x' * 5000 + '\nCedar match'),
                      ('second', BRIEF + '\nCedar match')]:
        digest = hashlib.sha256(rec._delivery_body(text).encode()).hexdigest()
        assert rec._deliver_text(text, 'cron:sotto-morning-brief', run_id=run,
                                 source_revision_at_composition=state['revision'],
                                 route_digest_at_composition=route, source_text_sha256=digest)
    path = tmp_path / 'events/outbox.json'
    outbox = json.loads(path.read_text())
    stamp = datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc).timestamp()
    for index, row in enumerate(outbox['rows']):
        row['receipt']['accepted_at'] = stamp + index
    path.write_text(json.dumps(outbox))
    selector = {'query': 'CEDAR MATCH', 'offset': 0, 'limit': 80}
    kwargs = {'source_revision': state['revision'], 'route_digest': route,
              'zone': ZoneInfo('America/Los_Angeles'), 'now': stamp + 60}
    result = reader.read(tmp_path, selector, **kwargs)
    assert result['status'] == 'ambiguous' and len(result['matches']) == 2
    assert {row['date'] for row in result['matches']} == {'2026-10-04'}
    assert result['omitted_matches'] == 0
    assert reader.read(tmp_path, {**selector, 'date': '2026-10-05'}, **kwargs)['status'] == 'not_found'
    assert reader.read(tmp_path, selector, **{**kwargs, 'now': stamp + 8 * 86400})['status'] == 'not_found'
    assert reader.read(tmp_path, selector, **{**kwargs, 'now': stamp - 1})['status'] == 'not_found'
    outbox['rows'][1]['receipt']['retained_text_proof']['source_revision'] = 'b' * 64
    path.write_text(json.dumps(outbox))
    eligible = reader.read(tmp_path, selector, **kwargs)
    assert eligible['status'] == 'found'
    assert eligible['id'] == outbox['rows'][0]['receipt']['visual_artifact_id']
    assert 'matches' not in eligible
    assert _request({'query': 'CEDAR MATCH', 'offset': 1, 'limit': 80})[0] == 400
    monkeypatch.setattr(reader, 'MAX_DISCOVERY_BYTES', 1)
    assert reader.read(tmp_path, selector, **kwargs) == {'status': 'unavailable'}


def test_discovery_closed_selectors():
    reader, _ = _libraries()
    for extra in ({'date': '2026-02-30'}, {'query': ' bad'}, {'query': 'a\nb'},
                  {'query': 'x' * 201}, {'kind': 'other'}, {'date': '2026-10-05', 'offset': 1},
                  {'latest': True, 'query': 'a'}, {'artifact_id': 'a' * 24, 'kind': 'prep'}):
        with pytest.raises(ValueError):
            reader.validate_selector({'offset': 0, 'limit': 80, **extra})


def _synthetic_galleries(root, proof, count, *, now, revision='b' * 64, route='a' * 64, step=3600,
                         text=lambda index: f'Brief {index}\nAlex — waiver'):
    """Write `count` current accepted galleries, newest first, with real retained-text proofs."""
    import os
    rows = []
    for index in range(count):
        artifact = f'{index:024x}'
        folder = root / 'cache/visual-briefs' / artifact
        folder.mkdir(parents=True)
        images = []
        for page in range(1, 5):
            image = folder / f'{page:02d}.png'
            image.write_bytes(b'\x89PNG synthetic %d %d' % (index, page))
            os.chmod(image, 0o600)
            images.append(hashlib.sha256(image.read_bytes()).hexdigest())
        manifest = {'id': artifact, 'kind': 'brief' if index % 3 else 'prep', 'preview': False,
                    'title': f'Brief {index}', 'summary': f'Summary {index}', 'full_text': text(index)}
        manifest['retained_text_proof'] = proof.stamp(manifest, revision, route, images)
        (folder / 'manifest.json').write_text(json.dumps(manifest))
        os.chmod(folder / 'manifest.json', 0o600)
        rows.append({'id': f'r{index}', 'status': 'delivered', 'acceptance': 'accepted',
                     'receipt': {'visual_artifact_id': artifact, 'accepted_at': now - index * step,
                                 'message_id': f'provider-{index}', 'route_proof_version': 1,
                                 'route_digest': route,
                                 'payload_hash': manifest['retained_text_proof']['gallery_payload_sha256'],
                                 'retained_text_proof': manifest['retained_text_proof']}})
    (root / 'events').mkdir(exist_ok=True)
    (root / 'events/outbox.json').write_text(json.dumps({'rows': rows}))
    os.chmod(root / 'events/outbox.json', 0o600)
    return rows


def test_busy_week_discovery_scans_newest_within_bound_instead_of_failing(tmp_path):
    import time
    reader, proof = _libraries()
    now = time.time()
    count = reader.MAX_DISCOVERY_ARTIFACTS + 8  # two briefs/day + several preps/day for a week
    rows = _synthetic_galleries(tmp_path, proof, count, now=now)
    kwargs = {'source_revision': 'b' * 64, 'route_digest': 'a' * 64, 'now': now}
    result = reader.read(tmp_path, {'query': 'Alex', 'offset': 0, 'limit': 100}, **kwargs)
    assert result['status'] == 'ambiguous', result
    assert [match['id'] for match in result['matches']] == [
        row['receipt']['visual_artifact_id'] for row in rows[:reader.MAX_MATCHES]]  # newest first
    assert result['omitted_matches'] == count - reader.MAX_MATCHES
    newest = reader.read(tmp_path, {'kind': 'brief', 'offset': 0, 'limit': 100}, **kwargs)
    assert newest['status'] == 'found' and newest['id'] == rows[1]['receipt']['visual_artifact_id']
    # The date filter needs only the receipt, so an older day is found even past the artifact bound.
    oldest = rows[-1]['receipt']
    day = datetime.fromtimestamp(oldest['accepted_at'], timezone.utc).date().isoformat()
    dated = reader.read(tmp_path, {'date': day, 'query': f'Brief {count - 1}', 'offset': 0, 'limit': 100}, **kwargs)
    assert dated['status'] == 'found' and dated['id'] == oldest['visual_artifact_id']
    # An unscanned older artifact is never reported as absent.
    assert reader.read(tmp_path, {'query': f'Brief {count - 1}', 'offset': 0, 'limit': 100},
                       **kwargs)['status'] == 'withheld'


def test_discovery_withholds_one_unreadable_artifact_and_stops_at_byte_bound(tmp_path, monkeypatch):
    import time
    reader, proof = _libraries()
    now = time.time()
    rows = _synthetic_galleries(tmp_path, proof, 3, now=now)
    kwargs = {'source_revision': 'b' * 64, 'route_digest': 'a' * 64, 'now': now}
    (tmp_path / 'cache/visual-briefs' / rows[0]['receipt']['visual_artifact_id'] / 'manifest.json').unlink()
    result = reader.read(tmp_path, {'query': 'Alex', 'offset': 0, 'limit': 100}, **kwargs)
    assert result['status'] == 'ambiguous' and len(result['matches']) == 2
    one = len((tmp_path / 'cache/visual-briefs' / rows[1]['receipt']['visual_artifact_id'] / 'manifest.json').read_bytes())
    monkeypatch.setattr(reader, 'MAX_DISCOVERY_BYTES', one + 4 * 64)  # room for one artifact only
    bounded = reader.read(tmp_path, {'query': 'Alex', 'offset': 0, 'limit': 100}, **kwargs)
    assert bounded['status'] == 'found' and bounded['id'] == rows[1]['receipt']['visual_artifact_id']


@pytest.mark.parametrize('delivery_format', ['gallery', 'text'])
def test_prep_grounding_survives_worker_recovery_accepted_outbox_and_exact_read(tmp_path, monkeypatch, delivery_format):
    reader, route, state = _fixture(tmp_path, monkeypatch)
    if delivery_format == 'text':
        monkeypatch.setenv('SOTTO_VISUAL_BRIEFS', '0')
        proof = rec._load_shared_lib('retained_brief_proof')
        monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda text, *a, **k: (True, '', {
            'acceptance': 'accepted', 'message_id': 'text-parent', 'route_proof_version': 1,
            'route_digest': route, 'payload_hash': proof.text_payload_hash(text)}))
    body = ('Meeting with Dana\nYour thread\nMet on Friday.\nWhat Sample builds\nSoftware for clinics.\n'
            'The founder\nWas an engineer.\nTraction & signals\nProvider claimed an investment.\n'
            'Angles\nAsk about the product plan.')
    if delivery_format == 'gallery':
        # A real gallery needs four substantive sections and over 200 total words;
        # keep the text variant thin to continue exercising its accepted text proof.
        sections = {
            'Your thread': (
                'Dana met you Friday to discuss how clinics record patient follow-ups. '
                'You agreed to review the product plan before deciding whether to join the pilot. '
                'The next conversation should establish which workflow the team will test first, '
                'who will own implementation, and what evidence would make the pilot useful '
                'for the participating clinic staff.'),
            'What Sample builds': (
                'Sample builds software for clinics to organize patient follow-ups and shared tasks. '
                'The product plan describes a single queue for the care team with assignments '
                'and clear completion records. The proposed pilot starts with one workflow at '
                'one clinic. Dana wants feedback on whether that scope is practical before '
                'the team commits engineering time to broader integrations.'),
            'Traction & signals': (
                'The provider claimed an investment, but its source points to a different portfolio. '
                'Treat that claim as unverified and ask Dana directly about financing if it matters. '
                'The supplied material does not establish revenue, paying customers, or deployment dates. '
                'Those gaps should remain visible when assessing how much support the company '
                'can provide during the proposed clinic pilot.'),
            'Angles': (
                'Ask which workflow will enter the first pilot and which clinic employee owns it. '
                'Ask what measurable result would justify continuing after the initial trial period. '
                'Confirm who will answer implementation questions and how quickly they can respond. '
                'Before agreeing to participate, establish the time commitment expected from your team '
                'and the decision that Dana needs from this meeting.'),
        }
        assert all(len(paragraph.split()) <= 200 for paragraph in sections.values())
        body = 'Meeting with Dana\n' + '\n'.join(
            heading + '\n' + paragraph for heading, paragraph in sections.items())
        assert len(body.split()) > 200
    record = {'provider': 'gemini', 'stage': 'focus', 'subjects': ['dana@sample.test'],
              'response_json': '{"claim":"investment"}', 'response_sha256': 'c' * 64,
              'grounding_json': '{"uri":"https://different.test/portfolio"}', 'truncated': False}
    evidence = {'status': 'retained', 'input_sha256': 'd' * 64, 'records': [record], 'omitted_records': 0}
    monkeypatch.setattr(rec, '_read_delivery_effects', lambda _: {'research_evidence': evidence})
    class PrepProcess(FakeProcess):
        def communicate(self, timeout): return body, ''
    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *a, **k: PrepProcess())
    job = {'id': 'prep-run', 'kind': 'run', 'owner': 'worker', 'payload': {
        'runner': ['/fixture-runner'], 'prompt': '{}', 'label': 'meeting-prep:synthetic'}}
    result = rec._execute_work(job)
    assert result['research_evidence'] == evidence
    rec.WORK_QUEUE.enqueue(str(tmp_path), 'run', job['payload'], key='prep-run')
    leased = rec.WORK_QUEUE.claim(str(tmp_path), 'worker')
    rec.WORK_QUEUE.save_result(str(tmp_path), leased['id'], 'worker', result)
    saved = rec.WORK_QUEUE.get(str(tmp_path), leased['id'])['result']
    args = dict(run_id=leased['id'], source_revision_at_composition=saved['source_revision_at_composition'],
                route_digest_at_composition=saved['route_digest_at_composition'],
                source_text_sha256=saved['source_text_sha256'], research_evidence=saved['research_evidence'])
    assert rec._deliver_text(saved['text'], saved['label'], **args)
    assert not rec._deliver_text(saved['text'], saved['label'], **args)
    rows = json.loads((tmp_path / 'events/outbox.json').read_text())['rows']
    assert len(rows) == 1  # a recovered handoff never repeats an accepted effect
    receipt = rows[0]['receipt']
    assert receipt['retained_text_proof']['version'] == (2 if delivery_format == 'gallery' else 3)
    artifact = receipt['visual_artifact_id' if delivery_format == 'gallery' else 'text_artifact_id']
    selector = {'artifact_id': artifact, 'offset': 0, 'limit': 4000}
    found = reader.read(tmp_path, selector, source_revision=state['revision'], route_digest=route)
    assert found['status'] == 'found' and found['research_evidence'] == evidence
    restarted, _ = _libraries()
    assert restarted.read(tmp_path, selector, source_revision=state['revision'], route_digest=route) == found
    path = tmp_path / 'cache/visual-briefs' / artifact / 'manifest.json'
    original = path.read_bytes()
    for change in ('modify', 'delete'):
        manifest = json.loads(original)
        if change == 'modify':
            manifest['research_evidence']['records'][0]['grounding_json'] = '{"uri":"https://sample.test"}'
        else:
            del manifest['research_evidence']
        path.write_text(json.dumps(manifest))
        assert reader.read(tmp_path, selector, source_revision=state['revision'], route_digest=route)['status'] == 'withheld'
    path.write_bytes(original)
    state['revision'] = 'b' * 64
    assert reader.read(tmp_path, selector, source_revision=state['revision'], route_digest=route)['status'] == 'withheld'
    assert reader.read(tmp_path, selector, source_revision='a' * 64, route_digest='f' * 64)['status'] == 'withheld'


@pytest.mark.parametrize('fallback', ['disabled', 'gallery_unavailable', 'text_layout'])
def test_plain_prep_retains_accepted_evidence_with_restart_and_authority_checks(tmp_path, monkeypatch, fallback):
    reader, route, state = _fixture(tmp_path, monkeypatch)
    if fallback == 'disabled':
        monkeypatch.setenv('SOTTO_VISUAL_BRIEFS', '0')
    else:
        monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(gallery_available=lambda: False if fallback == 'gallery_unavailable' else True))
    body = 'Prep with Dana\nAsk about the clinic notebook.'
    record = {'provider': 'gemini', 'stage': 'profile', 'subjects': ['dana@sample.test'],
              'response_json': '{"claim":"clinic notebook"}', 'response_sha256': 'c' * 64,
              'grounding_json': '{"uri":"https://sample.test"}', 'truncated': False}
    evidence = {'status': 'retained', 'input_sha256': 'd' * 64, 'records': [record], 'omitted_records': 0}
    proof = rec._load_shared_lib('retained_brief_proof')
    sends = []
    def send(text, *_args, **_kwargs):
        sends.append(text)
        return True, '', {'acceptance': 'accepted', 'message_id': 'text-parent',
                          'route_proof_version': 1, 'route_digest': route,
                          'payload_hash': proof.text_payload_hash(text)}
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', send)
    args = dict(source_revision_at_composition=state['revision'], route_digest_at_composition=route,
                source_text_sha256=hashlib.sha256(body.encode()).hexdigest(), research_evidence=evidence)
    assert rec._deliver_text(body, 'meeting-prep:plain', **args)
    assert sends == [body]
    receipt = json.loads((tmp_path/'events/outbox.json').read_text())['rows'][0]['receipt']
    assert receipt['retained_text_proof']['version'] == 3
    artifact = receipt['text_artifact_id']
    selector = {'artifact_id': artifact, 'offset': 0, 'limit': 4000}
    found = reader.read(tmp_path, selector, source_revision=state['revision'], route_digest=route)
    assert found['status'] == 'found' and found['text'] == body and found['research_evidence'] == evidence
    assert reader.read(tmp_path, {'kind': 'prep', 'query': 'clinic notebook', 'offset': 0, 'limit': 4000},
                       source_revision=state['revision'], route_digest=route)['id'] == artifact
    restarted, _ = _libraries()
    assert restarted.read(tmp_path, selector, source_revision=state['revision'], route_digest=route) == found
    assert reader.read(tmp_path, {'latest': True, 'offset': 0, 'limit': 4000}, source_revision=state['revision'], route_digest=route) == found
    for revision, selected_route in [('f'*64, route), (state['revision'], 'e'*64)]:
        assert reader.read(tmp_path, selector, source_revision=revision, route_digest=selected_route)['status'] == 'withheld'
    path = tmp_path/'cache/visual-briefs'/artifact/'manifest.json'
    original = path.read_bytes()
    manifest = json.loads(original)
    manifest['research_evidence']['records'][0]['grounding_json'] = 'tampered'
    path.write_text(json.dumps(manifest))
    assert reader.read(tmp_path, selector, source_revision=state['revision'], route_digest=route)['status'] == 'withheld'
    path.write_bytes(original)
    manifest = json.loads(original)
    manifest['full_text'] += 'tampered'
    path.write_text(json.dumps(manifest))
    assert reader.read(tmp_path, selector, source_revision=state['revision'], route_digest=route)['status'] == 'withheld'


@pytest.mark.parametrize('receipt_change', [{'payload_hash': 'f'*64}, {'route_digest': 'e'*64}, {'route_proof_version': None}])
def test_plain_prep_requires_matching_provider_acceptance_proof(tmp_path, monkeypatch, receipt_change):
    reader, route, state = _fixture(tmp_path, monkeypatch)
    monkeypatch.setenv('SOTTO_VISUAL_BRIEFS', '0')
    body = 'Prep with Dana\nAsk about the clinic notebook.'
    proof = rec._load_shared_lib('retained_brief_proof')
    receipt = {'acceptance': 'accepted', 'message_id': 'text-parent', 'route_proof_version': 1,
               'route_digest': route, 'payload_hash': proof.text_payload_hash(body), **receipt_change}
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda *a, **k: (True, '', receipt))
    assert rec._deliver_text(body, 'meeting-prep:plain', source_revision_at_composition=state['revision'],
                            route_digest_at_composition=route,
                            source_text_sha256=hashlib.sha256(body.encode()).hexdigest(),
                            research_evidence={'status': 'unavailable'})
    row = json.loads((tmp_path/'events/outbox.json').read_text())['rows'][0]
    assert 'retained_text_proof' not in row['receipt']
    assert reader.read(tmp_path, {'latest': True, 'offset': 0, 'limit': 4000},
                       source_revision=state['revision'], route_digest=route)['status'] != 'found'


@pytest.mark.parametrize('failure', [ImportError, IndexError, KeyError, RuntimeError])
def test_optional_text_retention_failure_never_blocks_owner_text(tmp_path, monkeypatch, capsys, failure):
    _reader, route, state = _fixture(tmp_path, monkeypatch)
    monkeypatch.setenv('SOTTO_VISUAL_BRIEFS', '0')
    visual = rec._load_shared_lib('visual_delivery')
    monkeypatch.setattr(visual, 'retain_text', lambda *a, **k: (_ for _ in ()).throw(failure('fixture')))
    load_shared = rec._load_shared_lib
    monkeypatch.setattr(rec, '_load_shared_lib', lambda name: visual if name == 'visual_delivery' else load_shared(name))
    sent = []
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda text, *a, **k: sent.append(text) or
                        (True, '', {'acceptance': 'accepted', 'message_id': 'text-parent'}))
    body = 'Prep with Dana\nAsk about the clinic notebook.'
    assert rec._deliver_text(body, 'meeting-prep:plain', source_revision_at_composition=state['revision'],
                            route_digest_at_composition=route,
                            source_text_sha256=hashlib.sha256(body.encode()).hexdigest(),
                            research_evidence={'status': 'unavailable'})
    assert sent == [body]
    diagnostics = capsys.readouterr().err
    assert f'text_retention_fallback error={failure.__name__}' in diagnostics
    assert 'fixture' not in diagnostics and body not in diagnostics


def test_retained_proof_version_matches_text_or_gallery_carrier():
    base = {'source_revision': 'a'*64, 'route_digest': 'b'*64, 'content_sha256': 'c'*64,
            'gallery_payload_sha256': 'd'*64}
    for version, images in [(1, ['e'*64]*4), (2, ['e'*64]*4), (3, [])]:
        proof = {**base, 'version': version, 'image_sha256s': images}
        gallery = {'presentation': {'retained_text_proof': proof}}
        text = {'retained_text_artifact': {'retained_text_proof': proof}}
        assert bool(rec.OUTBOX._retained_candidate(gallery)) == (version in (1, 2))
        assert bool(rec.OUTBOX._retained_candidate(text)) == (version == 3)


def test_text_retention_cold_module_uses_explicit_proof_dependency(tmp_path, monkeypatch):
    import sys
    import stat
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delitem(sys.modules, 'retained_brief_proof', raising=False)
    shared_lib = str(Path(rec.__file__).resolve().parents[2] / 'sotto-chief-of-staff/_shared/lib')
    monkeypatch.setattr(sys, 'path', [path for path in sys.path if path != shared_lib])
    visual = rec._load_shared_lib('visual_delivery')
    proof = rec._load_shared_lib('retained_brief_proof')
    retained = visual.retain_text('Prep with Dana\nAsk about the notebook.', {'status': 'unavailable'},
                                  'a'*64, 'b'*64, stamp=proof.stamp)
    assert 'retained_brief_proof' not in sys.modules
    path = tmp_path/'cache/visual-briefs'/retained['id']/'manifest.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    manifest = json.loads(path.read_text())
    assert proof.valid(manifest, retained['retained_text_proof'])
