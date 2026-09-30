"""Owner boundary, private media, explicit acceptance, and pin compatibility."""
import importlib.util
import json
import os
import re
import sqlite3
import stat
import sys
import time
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
    ok, _, receipt = g.send([str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)], 'Historical preview', 'photon', dispatch_id='run-1')
    assert ok and receipt['message_id'] == 'parent'
    assert len(calls) == 1 and calls[0][1]['summary'] == 'Historical preview'


def test_managed_other_recipient_and_escaped_file_never_send(media, monkeypatch, tmp_path):
    g = load('gallery')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setattr(g, '_call', lambda *a: pytest.fail('must not touch network'))
    assert not g.send([str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)], 'Preview', 'photon:+15555550101', dispatch_id='boundary-1')[0]
    outside = tmp_path / 'secret.png'
    outside.write_bytes(b'secret')
    link = media.parent / '02.png'
    link.unlink()
    link.symlink_to(outside)
    assert not g.send([str(link), str(media), str(media.with_name('03.png')), str(media.with_name('04.png'))], 'Preview', 'photon', dispatch_id='boundary-2')[0]
    assert not g.send([str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)], 'Preview', 'photon-other', dispatch_id='boundary-3')[0]


def test_unconfirmed_receipts_never_become_accepted(media, monkeypatch):
    g = load('gallery')
    for result in ({'ok': True}, [], {'ok': False, 'messageId': 'id'}):
        monkeypatch.setattr(g, '_call', lambda *a: (True, 'accepted', result))
        assert g.send([str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)], 'Preview', 'photon', dispatch_id='run-1')[2]['acceptance'] == 'unknown'


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


def test_patch_upgrades_exact_previous_gallery_patch(tmp_path):
    compat = load('photon_gallery_compat')
    p = tmp_path / 'sidecar.mjs'
    p.write_text('before\n' + compat.LEGACY_PATCH + compat.ANCHOR + '\nafter')
    compat.patch(p)
    upgraded = p.read_text()
    assert compat.LEGACY_PATCH not in upgraded and upgraded.count(compat.PATCH) == 1
    assert 'import fsPromises from "node:fs/promises";' in upgraded
    compat.patch(p)
    assert p.read_text() == upgraded


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
    import fsPromises from 'node:fs/promises';
    import nodePath from 'node:path';
    import crypto from 'node:crypto';
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
      {dispatchId:'run-1',spaceId:'owner',summary:'Preview',paths:['01.png','02.png','03.png','04.png']});
    console.log(JSON.stringify({receipt,calls}));
    """
    proc = subprocess.run([node, '--input-type=module', '-e', fixture], capture_output=True, text=True,
                          check=True, env={**os.environ, 'SOTTO_DATA': str(tmp_path)})
    result = json.loads(proc.stdout)
    assert result['receipt']['messageId'] == 'parent'
    assert result['calls'] == [{'items': [{'text':'Preview'}, {'path':'01.png'}, {'path':'02.png'}, {'path':'03.png'}, {'path':'04.png'}]}]


def test_sidecar_receipt_reconciles_late_acceptance_without_resend(tmp_path):
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the sidecar contract')
    compat = load('photon_gallery_compat')
    patch = compat.PATCH.replace('await import("spectrum-ts")',
            '({group: (...items) => ({items}), text: value => ({text:value})})')
    fixture = """
    import fsPromises from 'node:fs/promises';
    import nodePath from 'node:path';
    import crypto from 'node:crypto';
    let release;
    let calls = 0;
    const attachment = path => ({path});
    const resolveSpace = async () => ({send: async content => {
      calls++; await new Promise(resolve => { release = resolve; });
      return {id:'parent',content:{items:[{id:'part'}]}};
    }});
    const ok = (res, value) => value;
    const badRequest = () => {throw new Error('invalid input');};
    async function handler(req, res, body) {
    """ + patch + """
    }
    const body={dispatchId:'late-1',spaceId:'owner',summary:'PRIVATE SUMMARY',
      paths:['PRIVATE/01.png','PRIVATE/02.png','PRIVATE/03.png','PRIVATE/04.png']};
    const pending=handler({url:'/send-gallery'}, {}, body);
    while (!release) await new Promise(resolve => setTimeout(resolve, 1));
    const during=await handler({url:'/gallery-receipt'}, {}, {dispatchId:'late-1'});
    release();
    const sent=await pending;
    const after=await handler({url:'/gallery-receipt'}, {}, {dispatchId:'late-1'});
    const duplicate=await handler({url:'/send-gallery'}, {}, body);
    const disk=await fsPromises.readFile(nodePath.join(process.env.SOTTO_DATA,'events/gallery-receipts/late-1.json'),'utf8');
    console.log(JSON.stringify({during,sent,after,duplicate,calls,disk}));
    """
    proc = subprocess.run([node, '--input-type=module', '-e', fixture], capture_output=True, text=True,
                          check=True, env={**os.environ, 'SOTTO_DATA': str(tmp_path)})
    result = json.loads(proc.stdout)
    assert result['during']['receipt']['acceptance'] == 'in_flight'
    assert result['sent']['acceptance'] == result['after']['receipt']['acceptance'] == 'accepted'
    assert result['duplicate']['messageId'] == 'parent' and result['calls'] == 1
    assert 'PRIVATE' not in result['disk'] and set(json.loads(result['disk'])) == {
        'dispatchId', 'recordedAt', 'phase', 'acceptance', 'messageId', 'messageIds'}
    receipt_path = tmp_path / 'events/gallery-receipts/late-1.json'
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt_path.parent.stat().st_mode) == 0o700


def test_sidecar_provider_throw_stays_unknown(tmp_path):
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the sidecar contract')
    compat = load('photon_gallery_compat')
    patch = compat.PATCH.replace('await import("spectrum-ts")',
            '({group: (...items) => ({items}), text: value => ({text:value})})')
    fixture = """
    import fsPromises from 'node:fs/promises';
    import nodePath from 'node:path';
    import crypto from 'node:crypto';
    let calls=0;
    const attachment = path => ({path});
    const resolveSpace = async () => ({send: async () => {calls++; throw new Error('UNAVAILABLE');}});
    const ok = (res, value) => value;
    const badRequest = () => {throw new Error('invalid input');};
    async function handler(req, res, body) {
    """ + patch + """
    }
    const body={dispatchId:'failed-1',spaceId:'owner',summary:'Preview',paths:['1.png','2.png','3.png','4.png']};
    try { await handler({url:'/send-gallery'}, {}, body); } catch {}
    const receipt=await handler({url:'/gallery-receipt'}, {}, {dispatchId:'failed-1'});
    const duplicate=await handler({url:'/send-gallery'}, {}, body);
    const dir=nodePath.join(process.env.SOTTO_DATA,'events/gallery-receipts');
    await fsPromises.writeFile(nodePath.join(dir,'corrupt-1.json'),
      JSON.stringify({dispatchId:'corrupt-1',acceptance:'accepted',private:'must-not-return'}));
    const corrupt=await handler({url:'/send-gallery'}, {}, {...body,dispatchId:'corrupt-1'});
    const corruptLookup=await handler({url:'/gallery-receipt'}, {}, {dispatchId:'corrupt-1'});
    console.log(JSON.stringify({receipt,duplicate,corrupt,corruptLookup,calls}));
    """
    proc = subprocess.run([node, '--input-type=module', '-e', fixture], capture_output=True, text=True,
                          check=True, env={**os.environ, 'SOTTO_DATA': str(tmp_path)})
    result = json.loads(proc.stdout)
    assert result['receipt']['receipt']['acceptance'] == result['duplicate']['acceptance'] == 'unknown'
    assert result['corrupt']['acceptance'] == result['corruptLookup']['receipt']['acceptance'] == 'unknown'
    assert 'private' not in result['corruptLookup']['receipt']
    assert result['calls'] == 1


def test_unresolved_receipt_capacity_fails_closed_without_pruning(tmp_path):
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the sidecar contract')
    compat = load('photon_gallery_compat')
    patch = compat.PATCH.replace('2048', '3').replace('await import("spectrum-ts")',
            '({group: (...items) => ({items}), text: value => ({text:value})})')
    fixture = """
    import fsPromises from 'node:fs/promises';
    import nodePath from 'node:path';
    import crypto from 'node:crypto';
    let calls=0;
    const attachment = path => ({path});
    const resolveSpace = async () => ({send: async () => {calls++; return {id:'parent',content:{items:[]}};}});
    const ok = (res, value) => value;
    const badRequest = () => {throw new Error('invalid input');};
    async function handler(req, res, body) {
    """ + patch + """
    }
    const dir=nodePath.join(process.env.SOTTO_DATA,'events/gallery-receipts');
    await fsPromises.mkdir(dir,{recursive:true});
    for (let i=0;i<3;i++) await fsPromises.writeFile(nodePath.join(dir,`held-${i}.json`),
      JSON.stringify({dispatchId:`held-${i}`,acceptance:i===2?'in_flight':'unknown',recordedAt:1}));
    const body={dispatchId:'new-1',spaceId:'owner',summary:'Preview',paths:['1.png','2.png','3.png','4.png']};
    let failed=false;
    try { await handler({url:'/send-gallery'}, {}, body); } catch { failed=true; }
    const names=(await fsPromises.readdir(dir)).sort();
    await fsPromises.unlink(nodePath.join(dir,'held-2.json'));
    const old=nodePath.join(dir,'accepted-old.json');
    await fsPromises.writeFile(old,JSON.stringify({dispatchId:'accepted-old',acceptance:'accepted',
      messageId:'old',messageIds:[],recordedAt:1}));
    await fsPromises.utimes(old,new Date(0),new Date(0));
    const recovered=await handler({url:'/send-gallery'}, {}, {...body,dispatchId:'new-2'});
    const after=(await fsPromises.readdir(dir)).sort();
    console.log(JSON.stringify({failed,calls,names,recovered,after}));
    """
    proc = subprocess.run([node, '--input-type=module', '-e', fixture], capture_output=True, text=True,
                          check=True, env={**os.environ, 'SOTTO_DATA': str(tmp_path)})
    result = json.loads(proc.stdout)
    assert result['failed'] and result['names'] == ['held-0.json', 'held-1.json', 'held-2.json']
    assert result['calls'] == 1 and result['recovered']['messageId'] == 'parent'
    assert result['after'] == ['held-0.json', 'held-1.json', 'new-2.json']


def test_capacity_never_prunes_active_reserved_dispatch(tmp_path):
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the sidecar contract')
    compat = load('photon_gallery_compat')
    patch = compat.PATCH.replace('2048', '1').replace('await import("spectrum-ts")',
            '({group: (...items) => ({items}), text: value => ({text:value})})')
    fixture = """
    import fsPromises from 'node:fs/promises';
    import nodePath from 'node:path';
    import crypto from 'node:crypto';
    let enteredResolve;
    let releaseResolve;
    let calls=0;
    const waiting=new Promise(resolve => { enteredResolve=resolve; });
    const held=new Promise(resolve => { releaseResolve=resolve; });
    const attachment = path => ({path});
    const resolveSpace = async spaceId => {
      if (spaceId === 'first') { enteredResolve(); await held; }
      return {send: async () => {calls++; return {id:'parent-'+spaceId,content:{items:[]}};}};
    };
    const ok = (res, value) => value;
    const badRequest = () => {throw new Error('invalid input');};
    async function handler(req, res, body) {
    """ + patch + """
    }
    const base={summary:'Preview',paths:['1.png','2.png','3.png','4.png']};
    const first=handler({url:'/send-gallery'}, {}, {...base,dispatchId:'active-1',spaceId:'first'});
    await waiting;
    let secondFailed=false;
    try { await handler({url:'/send-gallery'}, {}, {...base,dispatchId:'active-2',spaceId:'second'}); }
    catch { secondFailed=true; }
    const dir=nodePath.join(process.env.SOTTO_DATA,'events/gallery-receipts');
    const during=JSON.parse(await fsPromises.readFile(nodePath.join(dir,'active-1.json'),'utf8'));
    releaseResolve();
    const accepted=await first;
    const after=JSON.parse(await fsPromises.readFile(nodePath.join(dir,'active-1.json'),'utf8'));
    console.log(JSON.stringify({secondFailed,calls,during,accepted,after,names:await fsPromises.readdir(dir)}));
    """
    proc = subprocess.run([node, '--input-type=module', '-e', fixture], capture_output=True, text=True,
                          check=True, env={**os.environ, 'SOTTO_DATA': str(tmp_path)})
    result = json.loads(proc.stdout)
    assert result['secondFailed'] and result['during']['acceptance'] == 'in_flight'
    assert result['calls'] == 1 and result['accepted']['messageId'] == 'parent-first'
    assert result['after']['acceptance'] == 'accepted' and result['names'] == ['active-1.json']


def test_sidecar_receipt_failure_prevents_provider_invocation(tmp_path):
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the sidecar contract')
    blocked = tmp_path / 'blocked'
    blocked.write_text('not a directory')
    compat = load('photon_gallery_compat')
    patch = compat.PATCH.replace('await import("spectrum-ts")',
            '({group: (...items) => ({items}), text: value => ({text:value})})')
    fixture = """
    import fsPromises from 'node:fs/promises';
    import nodePath from 'node:path';
    import crypto from 'node:crypto';
    let calls=0;
    const attachment = path => ({path});
    const resolveSpace = async () => ({send: async () => {calls++; return {id:'must-not-send'};}});
    const ok = (res, value) => value;
    const badRequest = () => {throw new Error('invalid input');};
    async function handler(req, res, body) {
    """ + patch + """
    }
    const body={dispatchId:'blocked-1',spaceId:'owner',summary:'Preview',paths:['1.png','2.png','3.png','4.png']};
    let failed=false;
    try { await handler({url:'/send-gallery'}, {}, body); } catch { failed=true; }
    console.log(JSON.stringify({failed,calls}));
    """
    proc = subprocess.run([node, '--input-type=module', '-e', fixture], capture_output=True, text=True,
                          check=True, env={**os.environ, 'SOTTO_DATA': str(blocked)})
    assert json.loads(proc.stdout) == {'failed': True, 'calls': 0}


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


def test_recovery_does_not_confuse_identical_prep_replies_for_one_target(media, monkeypatch):
    g = load('gallery')
    g.__package__ = 'recovery_gallery'
    fmt = types.ModuleType('recovery_gallery.chatfmt')
    fmt.to_imessage = lambda content, **kw: content
    monkeypatch.setitem(sys.modules, fmt.__name__, fmt)
    ledger = types.ModuleType('gateway.delivery_ledger')
    ledger.RECOVERED_MARKER = 'Recovered reply\n\n'
    ledger.RECONNECTED_MARKER = 'Reconnected reply\n\n'
    monkeypatch.setitem(sys.modules, ledger.__name__, ledger)
    (media.parent / 'manifest.json').write_text(json.dumps({'full_text': 'Identical prep'}))
    paths = [str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)]
    monkeypatch.setattr(g, 'send', lambda *a: (True, '',
                        {'acceptance': 'accepted', 'message_id': 'first'}))
    assert g.send_once(paths, 'Company', 'photon:owner', 'first-inbound')[0]
    monkeypatch.setattr(g, 'send', lambda *a: (False, '', {'acceptance': 'unknown'}))
    assert not g.send_once(paths, 'Company', 'photon:owner', 'second-inbound')[0]

    # The recovered content has no inbound anchor, so an old accepted receipt
    # cannot prove acceptance of the second reply.
    assert g.recovery_receipt(ledger.RECOVERED_MARKER + 'Identical prep',
                              'photon:owner') == {'acceptance': 'unknown'}


def _recovering_gallery(monkeypatch, stale_after=24 * 60 * 60):
    g = load('gallery')
    g.__package__ = 'recovery_gallery'
    fmt = types.ModuleType('recovery_gallery.chatfmt')
    fmt.to_imessage = lambda content, **kw: content
    monkeypatch.setitem(sys.modules, fmt.__name__, fmt)
    ledger = types.ModuleType('gateway.delivery_ledger')
    ledger.RECOVERED_MARKER = 'Recovered reply\n\n'
    ledger.RECONNECTED_MARKER = 'Reconnected reply\n\n'
    ledger.STALE_AFTER_SECONDS = stale_after
    monkeypatch.setitem(sys.modules, ledger.__name__, ledger)
    return g, ledger


def test_recovery_ignores_accepted_receipt_older_than_the_ledger_window(media, monkeypatch):
    g, ledger = _recovering_gallery(monkeypatch)
    (media.parent / 'manifest.json').write_text(json.dumps({'full_text': 'Identical prep'}))
    paths = [str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)]
    monkeypatch.setattr(g, 'send', lambda *a: (True, '', {'acceptance': 'accepted', 'message_id': 'first'}))
    assert g.send_once(paths, 'Company', 'photon:owner', 'first-inbound')[0]
    recovered = ledger.RECOVERED_MARKER + 'Identical prep'
    # A fresh receipt still cannot identify a recovered obligation by its words.
    assert g.recovery_receipt(recovered, 'photon:owner') == {'acceptance': 'unknown'}
    # Two days later a crashed identical request has no receipt of its own; the ledger would have
    # abandoned any obligation that old, so the earlier accepted prep cannot confirm it.
    [receipt_path] = media.parent.glob('delivery-*.json')
    receipt = json.loads(receipt_path.read_text())
    receipt_path.write_text(json.dumps({**receipt, 'recorded_at': receipt['recorded_at'] - 2 * 86400}))
    assert g.recovery_receipt(recovered, 'photon:owner') == {'acceptance': 'unknown'}


def test_recovery_skips_unreadable_unrelated_deck(media, monkeypatch):
    g, ledger = _recovering_gallery(monkeypatch)
    torn = media.parent.parent / 'torn-deck'
    torn.mkdir()
    (torn / 'manifest.json').write_text('')  # an unrelated deck, torn by power loss
    assert g.recovery_receipt(ledger.RECOVERED_MARKER + 'Sure, moved it to 3pm.', 'photon:owner') is None
    (media.parent / 'manifest.json').write_text(json.dumps({'full_text': 'Original prep'}))
    paths = [str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)]
    monkeypatch.setattr(g, 'send', lambda *a: (True, '', {'acceptance': 'accepted', 'message_id': 'kept'}))
    assert g.send_once(paths, 'Company', 'photon:owner', 'inbound')[0]
    recovered = ledger.RECOVERED_MARKER + 'Original prep'
    assert g.recovery_receipt(recovered, 'photon:owner') == {'acceptance': 'unknown'}
    # Only the matching deck's own unreadable receipt leaves acceptance unknown.
    [receipt_path] = media.parent.glob('delivery-*.json')
    receipt_path.write_text('{')
    assert g.recovery_receipt(recovered, 'photon:owner') == {'acceptance': 'unknown'}


@pytest.mark.parametrize('manifest_text', ['Original prep', 'Old renderer output'])
def test_recovery_requires_exact_gateway_obligation_even_after_formatter_change(media, monkeypatch,
                                                                               manifest_text):
    g, ledger = _recovering_gallery(monkeypatch)
    database = media.parent.parent.parent.parent / 'ledger.sqlite'
    with sqlite3.connect(database) as conn:
        conn.execute('''CREATE TABLE delivery_obligations
                        (obligation_id TEXT PRIMARY KEY, platform TEXT, chat_id TEXT,
                         content TEXT, state TEXT, created_at REAL)''')
        for identity in ('a' * 24, 'b' * 24):
            conn.execute('INSERT INTO delivery_obligations VALUES (?,?,?,?,?,?)',
                         (identity, 'photon', 'owner', 'Original prep', 'attempting', time.time()))
    ledger._db_path = lambda: database
    (media.parent / 'manifest.json').write_text(json.dumps({'full_text': manifest_text}))
    paths = [str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)]
    monkeypatch.setattr(g, 'send', lambda *a: (True, '', {'acceptance': 'accepted', 'message_id': 'own'}))
    assert g.send_once(paths, 'Company', 'photon:owner', 'first-inbound', 'a' * 24, 'Original prep')[0]
    # The manifest may change across versions; the receipt is found by gateway identity.
    assert g.recovery_receipt(ledger.RECOVERED_MARKER + 'Original prep', 'photon:owner',
                              'a' * 24)['message_id'] == 'own'
    assert g.recovery_receipt('Original prep', 'photon:owner', 'a' * 24)['message_id'] == 'own'
    assert g.recovery_receipt(ledger.RECOVERED_MARKER + 'Original prep', 'photon:owner',
                              'b' * 24) is None
    assert g.recovery_receipt('Original prep', 'photon:owner', 'b' * 24) is None
    # An arbitrary or forged ID does not authorize a receipt lookup or a new gallery send.
    assert g.recovery_receipt('Original prep', 'photon:owner',
                              'c' * 24) == {'acceptance': 'unknown'}
    assert g.send_once(paths, 'Company', 'photon:owner', None, 'c' * 24,
                       'Original prep')[2]['acceptance'] == 'unknown'
    # An uncertain receipt still holds its own obligation, but not a distinct known one.
    [receipt_path] = media.parent.glob('delivery-*.json')
    receipt = json.loads(receipt_path.read_text())
    receipt['acceptance'] = 'unknown'
    receipt_path.write_text(json.dumps(receipt))
    assert g.recovery_receipt(ledger.RECOVERED_MARKER + 'Original prep', 'photon:owner',
                              'a' * 24)['acceptance'] == 'unknown'
    assert g.recovery_receipt(ledger.RECOVERED_MARKER + 'Original prep', 'photon:owner',
                              'b' * 24) is None
    # Without a receipt identity, identical words remain ambiguous even for a valid obligation.
    receipt.pop('obligation_id')
    receipt_path.write_text(json.dumps(receipt))
    if manifest_text == 'Original prep':
        assert g.recovery_receipt(ledger.RECOVERED_MARKER + 'Original prep', 'photon:owner',
                                  'b' * 24) == {'acceptance': 'unknown'}


@pytest.mark.parametrize('acceptance', ['rejected', 'not_attempted'])
def test_proven_unsent_exact_obligation_can_retry_as_text(media, monkeypatch, acceptance):
    g, ledger = _recovering_gallery(monkeypatch)
    database = media.parent.parent.parent.parent / 'ledger.sqlite'
    with sqlite3.connect(database) as conn:
        conn.execute('''CREATE TABLE delivery_obligations
                        (obligation_id TEXT PRIMARY KEY, platform TEXT, chat_id TEXT,
                         content TEXT, state TEXT, created_at REAL)''')
        conn.execute('INSERT INTO delivery_obligations VALUES (?,?,?,?,?,?)',
                     ('a' * 24, 'photon', 'owner', 'Original prep', 'failed', time.time()))
    ledger._db_path = lambda: database
    (media.parent / 'manifest.json').write_text(json.dumps({'full_text': 'Original prep'}))
    paths = [str(media.with_name(f'{i:02d}.png')) for i in range(1, 5)]
    monkeypatch.setattr(g, 'send', lambda *a: (False, '', {'acceptance': acceptance}))
    assert not g.send_once(paths, 'Company', 'photon:owner', None, 'a' * 24, 'Original prep')[0]
    assert g.recovery_receipt(ledger.RECOVERED_MARKER + 'Original prep',
                              'photon:owner', 'a' * 24) is None


def run_sidecar(tmp_path, setup, script):
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the sidecar contract')
    patch = load('photon_gallery_compat').PATCH.replace('await import("spectrum-ts")',
            '({group: (...items) => ({items}), text: value => ({text:value})})')
    fixture = """
    import fsPromises from 'node:fs/promises';
    import nodePath from 'node:path';
    import crypto from 'node:crypto';
    const sent=[];
    const attachment = path => ({path});
    const ok = (res, value) => value;
    const badRequest = () => {throw new Error('invalid input');};
    """ + setup + """
    async function handler(req, res, body) {
    """ + patch + """
    }
    """ + script
    proc = subprocess.run([node, '--input-type=module', '-e', fixture], capture_output=True, text=True,
                          check=True, env={**os.environ, 'SOTTO_DATA': str(tmp_path)})
    return json.loads(proc.stdout)


def test_different_prep_decks_to_one_anchor_each_reach_the_provider(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    g = load('gallery')
    bodies = []
    monkeypatch.setattr(g, '_call', lambda endpoint, body, timeout=60: bodies.append(body) or
                        (True, 'accepted', {'ok': True, 'messageId': f'm{len(bodies)}'}))
    decks = []
    for name in ('deck-a', 'deck-b'):
        directory = tmp_path / 'cache/visual-briefs' / name
        directory.mkdir(parents=True)
        for index in range(1, 5):
            (directory / f'{index:02d}.png').write_bytes(b'PNG fixture')
        decks.append([str(directory / f'{index:02d}.png') for index in range(1, 5)])
    for reply_to in (None, 'inbound-1'):
        bodies.clear()
        assert g.send_once(decks[0], 'Prep for Sam', 'photon:+15555550100', reply_to)[0]
        assert g.send_once(decks[1], 'Prep for Priya', 'photon:+15555550100', reply_to)[0]
        ids = [body['dispatchId'] for body in bodies]
        assert len(set(ids)) == 2 and all(re.fullmatch(r'[A-Za-z0-9._:-]{1,160}', i) for i in ids)
    result = run_sidecar(tmp_path / 'sidecar',
        "const resolveSpace = async () => ({send: async c => {sent.push(c.items[0].text); "
        "return {id:'p'+sent.length,content:{items:[]}};}});",
        f"""const out=[]; for (const body of {json.dumps(bodies)}) out.push(await handler({{url:'/send-gallery'}}, {{}}, body));
        console.log(JSON.stringify({{out, sent}}));""")
    assert result['sent'] == ['Prep for Sam', 'Prep for Priya']


def test_receipt_lookup_while_resolving_is_held_not_proven_unsent(tmp_path, monkeypatch):
    result = run_sidecar(tmp_path,
        "let release; const resolveSpace = async spaceId => { if (spaceId === 'gone') throw new Error('gone');"
        " await new Promise(resolve => { release = resolve; });"
        " return {send: async () => {sent.push(1); return {id:'parent',content:{items:[]}};}}; };",
        """const dir=nodePath.join(process.env.SOTTO_DATA,'events/gallery-receipts');
        const base={spaceId:'owner',summary:'S',paths:['1.png','2.png','3.png','4.png']};
        const pending=handler({url:'/send-gallery'}, {}, {...base,dispatchId:'run-1'});
        while (!release) await new Promise(resolve => setTimeout(resolve, 1));
        const during=await handler({url:'/gallery-receipt'}, {}, {dispatchId:'run-1'});
        const reserved=JSON.parse(await fsPromises.readFile(nodePath.join(dir,'run-1.json'),'utf8'));
        release(); const done=await pending;
        try { await handler({url:'/send-gallery'}, {}, {...base,dispatchId:'run-2',spaceId:'gone'}); } catch {}
        const unresolved=await handler({url:'/gallery-receipt'}, {}, {dispatchId:'run-2'});
        await fsPromises.writeFile(nodePath.join(dir,'crashed-1.json'),
          JSON.stringify({dispatchId:'crashed-1',phase:'resolve',acceptance:'in_flight',recordedAt:1}));
        const crashed=await handler({url:'/send-gallery'}, {}, {...base,dispatchId:'crashed-1'});
        console.log(JSON.stringify({during, reserved, done, unresolved, crashed, sent}));""")
    assert result['during']['receipt']['acceptance'] == result['reserved']['acceptance'] == 'in_flight'
    assert result['done']['acceptance'] == 'accepted' and result['sent'] == [1]
    # Only a failed chat resolution proves the provider was never called.
    assert result['unresolved']['receipt']['acceptance'] == 'not_attempted'
    # A reservation orphaned by a crash mid-resolution fails closed.
    assert result['crashed']['acceptance'] == 'in_flight' and result['sent'] == [1]
    g = load('gallery')
    monkeypatch.setattr(g, '_call', lambda *a, **k: (True, 'accepted', result['during']))
    assert g.receipt('run-1') == {'acceptance': 'unknown'}
