"""No credentials, servers or provider calls; exercise the receiver transport boundary."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import urllib.error

import pytest

SPEC = importlib.util.spec_from_file_location('sotto_chat_client', Path(__file__).with_name('client.py'))
client = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = client
SPEC.loader.exec_module(client)
TOKEN = 'local-test-bearer-not-a-secret-0000000'


class Opener:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return io.BytesIO(json.dumps(self.result).encode())


def make(result):
    opener = Opener(result)
    return client.ChatClient('http://127.0.0.1:8789', TOKEN, opener=opener), opener


@pytest.mark.parametrize('url', [
    'https://127.0.0.1:8789', 'http://localhost:8789', 'http://evil.test:8789',
    'http://127.0.0.1:8789@evil.test', 'http://127.0.0.1:8789/?token=x',
    'http://127.0.0.1:8789/v1', 'http://127.0.0.1',
])
def test_rejects_non_loopback_or_ambiguous_endpoints(url):
    with pytest.raises(ValueError):
        client.ChatClient(url, TOKEN)


def test_send_requires_provider_id_and_preserves_dispatch_identity():
    service, opener = make({'acceptance': 'accepted', 'message_ids': ['provider-1', 'provider-2']})
    ok, _, receipt = service.send_receipt('private content', 'photon:owner', 'dispatch-1')
    assert ok and receipt['message_id'] == 'provider-1'
    assert receipt['message_ids'] == ['provider-1', 'provider-2']
    request, _ = opener.requests[0]
    assert request.full_url == 'http://127.0.0.1:8789/v1/send'
    assert json.loads(request.data)['dispatch_id'] == 'dispatch-1'
    assert request.get_header('Authorization') == f'Bearer {TOKEN}'
    assert TOKEN not in repr(service)


def test_gallery_byte_transport_uses_ordered_exact_bytes_without_paths():
    service, opener = make({'acceptance': 'accepted', 'message_ids': ['group-1']})
    png = b'\x89PNG\r\n\x1a\nfixture'
    images = [{'name': f'{index:02d}.png', 'bytes': png} for index in range(1, 5)]
    ok, _, receipt = service.send_gallery_bytes(images, 'Résumé 📨 / 東京', 'owner', 'gallery-1')
    assert ok and receipt['message_id'] == 'group-1'
    request = opener.requests[0][0]
    assert request.full_url == 'http://127.0.0.1:8789/v1/send-gallery-bytes'
    body = json.loads(request.data)
    assert body['dispatch_id'] == 'gallery-1' and body['summary'] == 'Résumé 📨 / 東京'
    assert [item['name'] for item in body['images']] == [f'{index:02d}.png' for index in range(1, 5)]
    assert all(item['base64'] == 'iVBORw0KGgpmaXh0dXJl' for item in body['images'])
    assert 'paths' not in body
    service, opener = make({'acceptance': 'accepted', 'message_ids': ['group-1']})
    assert service.send_gallery_bytes(images[1:], 'brief', 'owner', 'gallery-1')[2] == {
        'acceptance': 'not_attempted'}
    assert opener.requests == []


@pytest.mark.parametrize('value', [{'acceptance': 'accepted'}, {'acceptance': 'in_flight'}, {'success': True}, []])
def test_unproven_acceptance_is_unknown(value):
    service, _ = make(value)
    assert service.send_receipt('text', 'owner', 'd')[2]['acceptance'] == 'unknown'


@pytest.mark.parametrize('error,expected', [
    (urllib.error.URLError(ConnectionRefusedError()), 'not_attempted'),
    (urllib.error.URLError(TimeoutError()), 'unknown'),
    (TimeoutError(), 'unknown'),
    (urllib.error.HTTPError('http://127.0.0.1', 403, 'refused', {}, None), 'not_attempted'),
    (urllib.error.HTTPError('http://127.0.0.1', 500, 'provider secret', {}, None), 'unknown'),
    (urllib.error.HTTPError('http://127.0.0.1', 302, 'redirect', {}, None), 'unknown'),
])
def test_error_classification_does_not_claim_timeout_means_unsent(error, expected):
    service, _ = make(error)
    ok, detail, receipt = service.send_receipt('text', 'owner', 'd')
    assert not ok and receipt['acceptance'] == expected
    assert 'provider secret' not in detail


def test_receipt_lookup_cannot_resend():
    service, opener = make({'found': True, 'receipt': {'acceptance': 'accepted', 'message_id': 'p1'}})
    assert service.receipt('d')['message_id'] == 'p1'
    assert [r.full_url for r, _ in opener.requests] == ['http://127.0.0.1:8789/v1/receipt']


def test_run_constructs_unattended_context_without_arbitrary_environment():
    service, opener = make({'text': 'done', 'usage': {'input_tokens': 10}})
    assert service.run('prompt', 'run-1', skill='morning-brief')['text'] == 'done'
    payload = json.loads(opener.requests[0][0].data)
    assert payload['env'] == {'SOTTO_UNATTENDED': '1', 'SOTTO_DELIVERY_RUN_ID': 'run-1'}


def test_only_typed_local_run_capacity_refusal_is_admission_busy():
    busy = urllib.error.HTTPError('http://127.0.0.1:8789/v1/run', 429, 'private body',
                                  {'X-Sotto-Admission': 'busy'}, None)
    service, _ = make(busy)
    with pytest.raises(client.ModelAdmissionBusyError, match='local model admission busy'):
        service.run('prepare', 'job-1')
    assert service.send_receipt('text', 'owner', 'd')[2]['acceptance'] == 'unknown'
    unverified = urllib.error.HTTPError('http://127.0.0.1:8789/v1/run', 429, 'other', {}, None)
    service, _ = make(unverified)
    with pytest.raises(RuntimeError, match='chat unattended run failed'):
        service.run('prepare', 'job-1')


def test_no_proxy_or_redirect_fallback():
    service = client.ChatClient('http://127.0.0.1:8789', TOKEN)
    assert not any(isinstance(h, client.urllib.request.ProxyHandler) and h.proxies for h in service._opener.handlers)
    assert any(isinstance(h, client._NoRedirect) for h in service._opener.handlers)


def test_session_rotation_uses_only_explicit_maintenance_route():
    service, opener = make({'rotated': True, 'count': 2})
    assert service.sessions('rotate')['count'] == 2
    request = opener.requests[0][0]
    assert request.full_url.endswith('/v1/sessions')
    assert json.loads(request.data) == {'action': 'rotate'}
    with pytest.raises(ValueError):
        service.sessions('shell')
    with pytest.raises(ValueError):
        service.sessions('purge_before')
    with pytest.raises(ValueError):
        service.sessions('rotate', before='2026-01-01T00:00:00Z')
    assert len(opener.requests) == 1


def test_activation_operations_use_fixed_post_routes_and_bounded_ack_identity():
    service, opener = make({'version': 1, 'pending': None})
    assert service.activation_evidence() is None
    request = opener.requests[0][0]
    assert request.full_url.endswith('/v1/activation-evidence')
    assert request.get_method() == 'POST' and json.loads(request.data) == {}
    assert request.get_header('Authorization') == f'Bearer {TOKEN}'
    service, opener = make({'acknowledged': True})
    event_id = 'a' * 64
    assert service.activation_ack(event_id) is True
    assert opener.requests[0][0].full_url.endswith('/v1/activation-ack')
    assert json.loads(opener.requests[0][0].data) == {'event_id': event_id}
    with pytest.raises(ValueError):
        service.activation_ack('bad')
    assert len(opener.requests) == 1


def test_activation_transport_errors_never_claim_ack_or_evidence():
    service, _ = make(urllib.error.URLError(TimeoutError()))
    with pytest.raises(RuntimeError):
        service.activation_evidence()
    with pytest.raises(RuntimeError):
        service.activation_ack('b' * 64)
    service, _ = make({'acknowledged': 1})
    with pytest.raises(RuntimeError):
        service.activation_ack('b' * 64)
    for body in ({'version': 1}, {'version': True, 'pending': None},
                 {'version': 1, 'pending': [], 'extra': 1}):
        service, _ = make(body)
        with pytest.raises(RuntimeError):
            service.activation_evidence()


def test_scheduled_text_carries_exact_optional_composition_revision():
    service, opener = make({'acceptance':'accepted','message_id':'accepted'})
    revision = 'a' * 64
    assert service.send_receipt('A pulse offer.', 'owner', 'pulse', source_revision=revision)[0]
    assert json.loads(opener.requests[0][0].data)['source_revision'] == revision
    with pytest.raises(ValueError):
        service.send_receipt('A pulse offer.', 'owner', 'pulse', source_revision='today')
    assert len(opener.requests) == 1
