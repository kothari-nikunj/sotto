"""Optional gallery uses the existing outbox validity/gate/receipt contract."""
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('visual_test_receiver', HERE / 'receiver.py')
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    box = rec.OUTBOX
    for key in ('record', 'on_invalid'):
        monkeypatch.setitem(box.HOOKS, key, lambda *a, **k: None)
    monkeypatch.setitem(box.HOOKS, 'valid', lambda payload: True)
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda payload: True)
    monkeypatch.setitem(box.HOOKS, 'send', lambda *a: pytest.fail('must use grouped transport'))
    return box


def payload():
    return {'label': 'preview:prep', 'body': 'Canonical full prep', 'target': 'photon',
            'run_id': 'preview-once', 'presentation': {'summary': 'Preview', 'images': ['/fixture.png']}}


def retry(box):
    with rec.CONNECTORS.json_transaction(box.path(), default={}) as doc:
        doc['rows'][0]['next_at'] = 0
    box.drain()


def test_acknowledged_gallery_never_repeats(box, monkeypatch):
    calls = []
    monkeypatch.setitem(box.HOOKS, 'send_gallery', lambda *a: calls.append(a) or
                        (True, '', {'message_id': 'parent', 'message_ids': ['part']}))
    assert box.deliver(payload())
    box.deliver(payload())
    box.drain()
    assert len(calls) == 1
    row = json.loads(Path(box.path()).read_text())['rows'][0]
    assert row['receipt']['message_id'] == 'parent'
    assert 'presentation' not in row['payload']


@pytest.mark.parametrize('acceptance', ['unknown', 'not_attempted'])
def test_uncertain_gallery_held_but_proven_unsent_can_retry(box, monkeypatch, acceptance):
    calls = []
    monkeypatch.setitem(box.HOOKS, 'send_gallery', lambda *a: calls.append(a) or
                        (False, 'test failure', {'acceptance': acceptance}))
    assert not box.deliver(payload())
    retry(box)
    assert len(calls) == (1 if acceptance == 'unknown' else 2)


def test_crash_after_dispatch_does_not_replay_gallery(box, monkeypatch):
    def crash(*a):
        raise KeyboardInterrupt()
    monkeypatch.setitem(box.HOOKS, 'send_gallery', crash)
    with pytest.raises(KeyboardInterrupt):
        box.deliver(payload())
    monkeypatch.setitem(box.HOOKS, 'send_gallery', lambda *a: pytest.fail('ambiguous send repeated'))
    retry(box)


def test_revoked_source_prevents_gallery(box, monkeypatch):
    monkeypatch.setitem(box.HOOKS, 'valid', lambda payload: False)
    monkeypatch.setitem(box.HOOKS, 'send_gallery', lambda *a: pytest.fail('revoked content sent'))
    assert not box.deliver(payload())


def test_opt_out_and_ordinary_nudges_do_not_load_renderer(monkeypatch):
    v = rec._load_shared_lib('visual_delivery')
    forbidden = lambda: pytest.fail('should not load optional renderer')
    monkeypatch.setenv('SOTTO_VISUAL_BRIEFS', '0')
    assert v.prepare('body', 'morning-brief', 'photon', forbidden, forbidden) is None
    monkeypatch.setenv('SOTTO_VISUAL_BRIEFS', '1')
    for label, target in [('event', 'photon'), ('morning-brief', 'telegram'), ('weekly-brief', 'photon')]:
        assert v.prepare('body', label, target, forbidden, forbidden) is None


@pytest.mark.parametrize('label', ['cron:sotto-morning-brief', 'cron:sotto-evening-brief', 'event:sotto-meeting-prep'])
def test_gallery_default_and_capability_fallback(monkeypatch, tmp_path, label):
    import types
    v = rec._load_shared_lib('visual_delivery')
    monkeypatch.delenv('SOTTO_VISUAL_BRIEFS', raising=False)
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    seen = []
    visual = types.SimpleNamespace(build=lambda body, kind, preview: seen.append(kind) or {},
                                   render=lambda *a: pytest.fail('empty deck must fall back'))
    assert v.prepare('body', label, 'photon', lambda: visual, lambda: True) is None
    assert seen == ['prep' if 'prep' in label else 'brief']
    assert v.prepare('body', label, 'photon', lambda: pytest.fail('missing capability'), lambda: False) is None


def test_render_failure_logs_metadata_without_source_content(monkeypatch, caplog):
    import types
    v = rec._load_shared_lib('visual_delivery')
    monkeypatch.delenv('SOTTO_VISUAL_BRIEFS', raising=False)

    def overflow(*args):
        raise ValueError('private source content /data/private-file')

    visual = types.SimpleNamespace(build=lambda *args, **kwargs: {'cards': []}, render=overflow)
    assert v.prepare('private brief', 'cron:sotto-morning-brief', 'photon', lambda: visual, lambda: True) is None
    assert 'visual_brief_fallback kind=brief error=ValueError' in caplog.text
    assert 'private' not in caplog.text


def test_schedule_omitted_by_both_models_still_delivers_one_four_photo_gallery(box, monkeypatch, tmp_path):
    """Regression for Sep 21: real composition + renderer + outbox, fake model/provider only."""
    pack = HERE.parents[1] / 'sotto-chief-of-staff'
    monkeypatch.syspath_prepend(str(pack / '_shared/scripts'))
    monkeypatch.syspath_prepend(str(pack / '_shared/lib'))
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_CRITIC', 'always')
    monkeypatch.delenv('SOTTO_TENANT_ID', raising=False)
    monkeypatch.delenv('SOTTO_VISUAL_BRIEFS', raising=False)
    spec = importlib.util.spec_from_file_location('calendar_delivery_composer', pack / '_shared/scripts/compose_brief.py')
    composer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(composer)
    visual = rec._load_shared_lib('visual_brief')
    delivery = rec._load_shared_lib('visual_delivery')
    calls = []
    def llm(prompt, inputs, **kwargs):
        if inputs.get('_critic'):
            return json.dumps({'score': 45, 'summary': 'Missing calendar', 'patches': [
                {'severity': 'moderate', 'detail': 'Restore the schedule'}]})
        return json.dumps({'brief_markdown': '## Needs Attention Now\nReview the agreement.\n\n'
                           '## Already Handled\nAlex: Confirmed the booking.', 'actions': []})
    long_location = ('1355 Market Street, Suite 900, San Francisco, California 94103, '
                     'Entrance on José Plaza near Café München')
    inputs = {'type': 'morning', 'first_run': False, 'now': '2026-09-21T13:30:00Z',
              'google': {'userTimezone': 'America/Los_Angeles', 'events': [
                  {'id': f'event-{h}', 'summary': f'Planning {h}', 'start': f'2026-09-21T{h:02}:00:00-07:00',
                   'location': long_location if h == 8 else '14 Main St'} for h in range(8, 16)]}, 'local': {}}
    out = composer.compose(inputs, llm=llm, critic=True)
    presentation = delivery.prepare(out['brief_text'], 'cron:sotto-morning-brief', 'photon',
                                     lambda: visual, lambda: True)
    assert presentation and len(presentation['images']) == 4
    for path in presentation['images']:
        assert Path(path).read_bytes().startswith(b'\x89PNG')
    assert '8:00 AM: Planning 8 | Location: ' + long_location in out['brief_text']
    monkeypatch.setitem(box.HOOKS, 'send_gallery', lambda *args: calls.append(args) or
                        (True, '', {'message_id': 'accepted', 'message_ids': ['1', '2', '3', '4']}))
    monkeypatch.setattr(composer, '_user_local_date', lambda tz: '2026-09-21')
    monkeypatch.setitem(box.HOOKS, 'local_today', lambda: '2026-09-21')
    composer._archive_brief(out, 'morning')
    item = {'label': 'cron:sotto-morning-brief', 'body': out['brief_text'], 'target': 'photon',
            'run_id': '2026-09-21.morning', 'presentation': presentation}
    assert box.deliver(item)
    box.deliver(item)
    box.drain()
    assert len(calls) == 1
    row = json.loads(Path(box.path()).read_text())['rows'][0]
    assert row['receipt']['message_ids'] == ['1', '2', '3', '4']
    assert not row.get('acceptance_unknown')


def test_expected_gallery_fallbacks_have_content_free_reasons(monkeypatch, tmp_path, caplog):
    import logging
    import types
    v = rec._load_shared_lib('visual_delivery')
    monkeypatch.delenv('SOTTO_VISUAL_BRIEFS', raising=False)
    caplog.set_level(logging.INFO)
    visual = types.SimpleNamespace(build=lambda *a, **kw: None)
    assert v.prepare('private short brief', 'morning-brief', 'photon', lambda: visual, lambda: True) is None
    assert 'reason=text_layout' in caplog.text
    assert v.prepare('private short brief', 'morning-brief', 'photon', lambda: visual, lambda: False) is None
    assert 'reason=gallery_unavailable' in caplog.text
    assert 'private' not in caplog.text


def test_crowded_brief_reflows_and_records_one_gallery_through_receiver(box, monkeypatch, tmp_path):
    """Sep 22 shape: eleven wrapped follow-ups, spare space on two action cards."""
    import types
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_VISUAL_BRIEFS', raising=False)
    monkeypatch.setattr(rec, '_deliver_target', lambda: 'photon')
    monkeypatch.setattr(rec, '_hermes_adapter', lambda _: types.SimpleNamespace(gallery_available=lambda: True))
    monkeypatch.setattr(rec, '_shared_effects', lambda: None)
    pack = HERE.parents[1] / 'sotto-chief-of-staff'
    monkeypatch.syspath_prepend(str(pack / '_shared/lib'))
    import visual_brief
    body = ('Good evening - Tuesday, September 22\nNeeds Attention Now\n'
            'Alex: Send the signed agreement before tomorrow.\nShould Handle Today\n'
            'Jordan: Reply to the lunch invitation.\nComing Up\n'
            'Wed Sep 23 10:00 AM: Planning | Location: 14 Main St\nStill open\n'
            + '\n'.join(f'Alexandra Montgomery {i}: Review the revised agreement with the project team before next week.'
                        for i in range(5)) + '\nAlready Handled\n'
            + '\n'.join(f'Christopher Alexander {i}: Confirmed the planning meeting and shared the revised notes with the team.'
                        for i in range(6)))
    original = visual_brief.build(body)
    assert len(original['cards']) == 4
    assert len(original['cards'][-1]['blocks']) == 11
    assert sum(len(b['text'].split()) for b in original['cards'][-1]['blocks']) < 200
    with pytest.raises(visual_brief.LayoutError, match='readable height'):
        visual_brief._measure(original, original['cards'])
    sent, receipts = [], []
    monkeypatch.setitem(box.HOOKS, 'send_gallery', lambda *args: sent.append(args) or
                        (True, '', {'message_id': 'accepted', 'message_ids': ['1', '2', '3', '4']}))
    monkeypatch.setitem(box.HOOKS, 'record', lambda label, status, detail='', **kw: receipts.append((status, detail, kw)))
    monkeypatch.setitem(box.HOOKS, 'local_today', lambda: '2026-09-22')
    monkeypatch.syspath_prepend(str(pack / '_shared/scripts'))
    import compose_brief
    monkeypatch.setattr(compose_brief, '_user_local_date', lambda tz: '2026-09-22')
    compose_brief._archive_brief({'brief_text': body, 'brief_markdown': body}, 'evening')
    assert rec._deliver_text(body, 'cron:sotto-evening-brief', run_id='evening-crowded', decision_ids=['decision-1'])
    rec._deliver_text(body, 'cron:sotto-evening-brief', run_id='evening-crowded')
    box.drain()
    assert len(sent) == 1
    accepted = [r for r in receipts if r[0] == 'delivered']
    assert len(accepted) == 1
    assert accepted[0][1] == 'Sent as four photos. Device display is not confirmed.'
    assert accepted[0][2]['run_id'] == 'evening-crowded'
    assert accepted[0][2]['decision_ids'] == ['decision-1']
    manifest = json.loads(next((tmp_path / 'cache/visual-briefs').glob('*/manifest.json')).read_text())
    assert len(manifest['images']) == 4
    assert manifest['full_text'] == body
    before = [b for c in original['cards'] for b in c['blocks']]
    after = [b for c in manifest['cards'] for b in c['blocks']]
    assert [(b['line'], b['text']) for b in before] == [(b['line'], b['text']) for b in after]
    assert len({b['line'] for b in after}) == len(after)
    assert {b['section'] for b in manifest['cards'][0]['blocks']} == {'Needs you', 'Today'}
    assert sum(c['title'] == 'Follow-ups' for c in manifest['cards']) == 2


def test_layout_failure_sends_canonical_text_once_and_records_reason(box, monkeypatch, tmp_path):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_VISUAL_BRIEFS', raising=False)
    visual = rec._load_shared_lib('visual_brief')
    delivery = rec._load_shared_lib('visual_delivery')
    body = 'Morning\nNeeds Attention Now\n' + 'W' * 150 + '\nA: reply.\nB: reply.\nC: reply.'
    diagnostics = {}
    presentation = delivery.prepare(body, 'morning-brief', 'photon', lambda: visual, lambda: True,
                                    diagnostics=diagnostics)
    assert presentation is None
    assert diagnostics == {'format': 'text', 'reason': 'word_too_wide', 'card_index': 1}
    assert not list(tmp_path.rglob('*.png'))
    sent, receipts = [], []
    monkeypatch.setitem(box.HOOKS, 'send', lambda *args: sent.append(args) or (True, '', {'message_id': 'text-1'}))
    monkeypatch.setitem(box.HOOKS, 'send_gallery', lambda *a: pytest.fail('failed layout sent as gallery'))
    monkeypatch.setitem(box.HOOKS, 'record', lambda label, status, detail='', **kw: receipts.append((status, detail)))
    item = {'label': 'morning-brief', 'body': body, 'target': 'photon', 'presentation': presentation,
            'presentation_status': diagnostics, 'run_id': 'layout-fallback'}
    assert box.deliver(item)
    box.deliver(item)
    box.drain()
    assert len(sent) == 1 and body in sent[0]
    accepted = [r for r in receipts if r[0] == 'delivered']
    assert accepted == [('delivered', 'Sent as text. A word or link was too wide for the photos. Device display is not confirmed.')]


@pytest.mark.parametrize('status', [None, 'private', {}, {'reason': []}, {'reason': {}}, {'reason': '/private/path'}])
def test_receipt_diagnostics_ignore_unrecognized_data(status):
    delivery = rec._load_shared_lib('visual_delivery')
    assert delivery.delivery_detail(status) == ''
