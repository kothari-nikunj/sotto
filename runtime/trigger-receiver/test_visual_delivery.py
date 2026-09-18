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
