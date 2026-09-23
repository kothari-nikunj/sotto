"""Owner boundary, private media, explicit acceptance, and pin compatibility."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from PIL import Image


def load(name):
    spec = importlib.util.spec_from_file_location('gallery_test_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def media(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', '+15555550100')
    p = tmp_path / 'cache/visual-briefs/deck/01.png'
    p.parent.mkdir(parents=True)
    for index in range(1, 5):
        p.with_name(f'{index:02d}.png').write_bytes(b'PNG fixture')
    return p


@pytest.mark.parametrize('mode', ['managed', 'self-host'])
def test_gallery_receipt_is_one_multipart_send(mode, media, monkeypatch):
    g = load('gallery')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', mode)
    calls = []
    monkeypatch.setattr(g, '_call', lambda *a: calls.append(a) or
        (True, 'accepted', {'ok': True, 'messageId': 'parent', 'messageIds': ['part0', 'part1']}))
    ok, _, receipt = g.send([str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)], 'Historical preview', 'photon')
    assert ok and receipt['message_id'] == 'parent'
    assert len(calls) == 1 and calls[0][1]['summary'] == 'Historical preview'


def test_managed_other_recipient_and_escaped_file_never_send(media, monkeypatch, tmp_path):
    g = load('gallery')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setattr(g, '_call', lambda *a: pytest.fail('must not touch network'))
    assert not g.send([str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)], 'Preview', 'photon:+15555550101')[0]
    outside = tmp_path / 'secret.png'
    outside.write_bytes(b'secret')
    link = media.parent / '02.png'
    link.unlink()
    link.symlink_to(outside)
    assert not g.send([str(link), str(media), str(media.with_name('03.png')), str(media.with_name('04.png'))], 'Preview', 'photon')[0]
    assert not g.send([str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)], 'Preview', 'photon-other')[0]


def test_unconfirmed_receipts_never_become_accepted(media, monkeypatch):
    g = load('gallery')
    for result in ({'ok': True}, [], {'ok': False, 'messageId': 'id'}):
        monkeypatch.setattr(g, '_call', lambda *a: (True, 'accepted', result))
        assert g.send([str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)], 'Preview', 'photon')[2]['acceptance'] == 'unknown'


def test_focused_background_reaches_four_image_transport(tmp_path, monkeypatch):
    """Real skill headings and renderer must reach one gallery dispatch, not just a mocked deck."""
    g = load('gallery')
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_VISUAL_BRIEFS', '1')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'hermes'))
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', '+15555550100')
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    skill = tmp_path / 'hermes/skills/sotto'
    skill.parent.mkdir(parents=True)
    skill.symlink_to(Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff')
    calls = []

    def transport(endpoint, payload, timeout=60):
        calls.append((endpoint, payload))
        if endpoint == 'gallery-capability':
            return True, 'accepted', {'galleryVersion': 1}
        return True, 'accepted', {'ok': True, 'messageId': 'parent', 'messageIds': ['a', 'b', 'c', 'd']}
    monkeypatch.setattr(g, '_call', transport)
    body = ('Sam (Example)\nYour thread\nPriya introduced you after Alex suggested a conversation.\n'
            'What Example builds\nSoftware for clinics.\nThe founder\nPreviously led engineering.\n'
            'Traction & signals\nThree pilots are signed.\nThe space & why now\nClinics want faster reviews.\n'
            'Angles\nAsk how pilots convert to paid contracts.')
    deck = g.prepare_prep(body)
    assert deck and len(deck['images']) == 4
    for path in deck['images']:
        with Image.open(path) as photo:
            assert photo.size == (1080, 1920)
    first = g.send_once(deck['images'], deck['summary'], 'photon', 'request-1')
    assert first[0] and first[2]['acceptance'] == 'accepted'
    assert g.send_once(deck['images'], deck['summary'], 'photon', 'request-1')[0]
    assert [endpoint for endpoint, _ in calls] == ['gallery-capability', 'send-gallery']
    assert len(calls[-1][1]['paths']) == 4


def test_patch_is_exact_idempotent_and_fails_on_pin_drift(tmp_path):
    compat = load('photon_gallery_compat')
    p = tmp_path / 'sidecar.mjs'
    p.write_text('before\n' + compat.ANCHOR + '\nafter')
    compat.patch(p)
    first = p.read_text()
    compat.patch(p)
    assert p.read_text() == first
    assert 'group(text(summary), ...paths.map(p => attachment(p)))' in first
    p.write_text('changed upstream')
    with pytest.raises(RuntimeError):
        compat.patch(p)


def test_patch_executes_one_group_with_text_and_attachments(tmp_path):
    import json  # noqa: PLC0415 — test-only harness
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the sidecar contract')
    compat = load('photon_gallery_compat')
    patch = compat.PATCH.replace('await import("spectrum-ts")',
            '({group: (...items) => ({items}), text: value => ({text:value})})')
    fixture = """
    const calls = [];
    const attachment = path => ({path});
    const resolveSpace = async () => ({send: async content => {
      calls.push(content); return {id:'parent',content:{items:[{id:'part'}]}};
    }});
    const ok = (res, value) => value;
    const badRequest = () => {throw new Error('invalid input');};
    async function handler(req, res, body) {
    """ + patch + """
    }
    const receipt = await handler({url:'/send-gallery'}, {},
      {spaceId:'owner',summary:'Preview',paths:['01.png','02.png','03.png','04.png']});
    console.log(JSON.stringify({receipt,calls}));
    """
    proc = subprocess.run([node, '--input-type=module', '-e', fixture], capture_output=True, text=True, check=True)
    result = json.loads(proc.stdout)
    assert result['receipt']['messageId'] == 'parent'
    assert result['calls'] == [{'items': [{'text':'Preview'}, {'path':'01.png'}, {'path':'02.png'}, {'path':'03.png'}, {'path':'04.png'}]}]


@pytest.mark.parametrize('acceptance', ['accepted', 'unknown'])
def test_interactive_receipt_survives_reload_without_resend(media, monkeypatch, acceptance):
    paths = [str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)]
    first = load('gallery')
    calls = []
    monkeypatch.setattr(first, 'send', lambda *a: calls.append(a) or
                        (acceptance == 'accepted', '', {'acceptance': acceptance, 'message_id': 'parent'}))
    first.send_once(paths, 'Preview', 'photon', 'inbound-1')
    restarted = load('gallery')
    monkeypatch.setattr(restarted, 'send', lambda *a: pytest.fail('must not resend across restart'))
    assert restarted.send_once(paths, 'Preview', 'photon', 'inbound-1')[0] == (acceptance == 'accepted')
    assert len(calls) == 1


def test_interactive_crash_is_held_and_new_request_can_send(media, monkeypatch):
    paths = [str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)]
    g = load('gallery')
    def crash(*a):
        raise KeyboardInterrupt()
    monkeypatch.setattr(g, 'send', crash)
    with pytest.raises(KeyboardInterrupt):
        g.send_once(paths, 'Preview', 'photon', 'inbound-1')
    monkeypatch.setattr(g, 'send', lambda *a: (True, '', {'acceptance': 'accepted', 'message_id': 'new'}))
    assert g.send_once(paths, 'Preview', 'photon', 'inbound-1')[2]['acceptance'] == 'unknown'
    assert g.send_once(paths, 'Preview', 'photon', 'inbound-2')[0]


def test_prep_preflight_keeps_ordinary_chat_and_opt_out_as_text(monkeypatch):
    g = load('gallery')
    monkeypatch.setattr(g, 'available', lambda: pytest.fail('ordinary text needs no capability probe'))
    assert g.prepare_prep('I am researching the company now.') is None
    monkeypatch.setenv('SOTTO_VISUAL_BRIEFS', '0')
    assert g.prepare_prep('Your thread\nWe met.\nWhat Example builds\nSoftware\nAngles\nAsk about growth.') is None


def test_gateway_recovery_finds_receipt_after_layout_version_changes(media, monkeypatch):
    g = load('gallery')
    g.__package__ = 'recovery_gallery'
    fmt = types.ModuleType('recovery_gallery.chatfmt')
    fmt.to_imessage = lambda content, **kw: content
    monkeypatch.setitem(sys.modules, fmt.__name__, fmt)
    ledger = types.ModuleType('gateway.delivery_ledger')
    ledger.RECOVERED_MARKER = 'Recovered reply\n\n'
    ledger.RECONNECTED_MARKER = 'Reconnected reply\n\n'
    monkeypatch.setitem(sys.modules, ledger.__name__, ledger)
    (media.parent / 'manifest.json').write_text(json.dumps({'version': 1, 'full_text': 'Original prep'}))
    paths = [str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)]
    monkeypatch.setattr(g, 'send', lambda *a: (False, '', {'acceptance': 'unknown'}))
    g.send_once(paths, 'Company', 'photon:owner', 'old-inbound')
    assert g.recovery_receipt('Ordinary chat', 'photon:owner') is None
    assert g.recovery_receipt(ledger.RECOVERED_MARKER + 'Original prep', 'photon:other') is None
    for marker in (ledger.RECOVERED_MARKER, ledger.RECONNECTED_MARKER):
        assert g.recovery_receipt(marker + 'Original prep', 'photon:owner')['acceptance'] == 'unknown'
