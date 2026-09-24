import hashlib
import json
from datetime import datetime, timezone

import pytest
import brief_detail as detail

NOW = datetime(2026, 9, 24, 5, tzinfo=timezone.utc).timestamp()
IDENT = 'a' * 24


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', 'owner')
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    monkeypatch.setattr(detail, 'permission_fingerprint', lambda: 'current')


def saved(root, ident=IDENT, *, interactive=False, target='photon:owner', status='accepted',
          fingerprint='current', stamp=NOW-60, text='Dana: Send the revised deck.', preview=False):
    folder = root / 'cache/visual-briefs' / ident
    folder.mkdir(parents=True)
    (folder / 'manifest.json').write_text(json.dumps({'kind': 'brief', 'title': 'Morning brief',
        'preview': preview, 'full_text': text, 'source_permission_fingerprint': fingerprint,
        'owner_channel_hash': hashlib.sha256(b'owner').hexdigest()}))
    receipt = {'message_id': 'message', 'target_hash': hashlib.sha256(target.encode()).hexdigest(),
               'accepted_at': stamp, 'recorded_at': stamp, 'acceptance': status, 'visual_artifact_id': ident}
    if interactive:
        (folder / 'delivery-fixture.json').write_text(json.dumps(receipt))
    else:
        path = root / 'events/outbox.json'
        path.parent.mkdir(exist_ok=True)
        old = json.loads(path.read_text()) if path.exists() else {'rows': []}
        old['rows'].append({'status': 'delivered' if status == 'accepted' else 'pending', 'receipt': receipt})
        path.write_text(json.dumps(old))
    return folder


@pytest.mark.parametrize('interactive', [False, True])
def test_accepted_gallery_text_found_by_local_day_and_topic_without_writes(tmp_path, interactive):
    saved(tmp_path, interactive=interactive)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = detail.query(data_root=tmp_path, day='2026-09-23', text='revised deck', now=NOW)
    assert result['status'] == 'found' and result['text'] == 'Dana: Send the revised deck.'
    assert result['next_offset'] is None
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('options', [dict(status='unknown'), dict(target='photon:stranger'),
    dict(stamp=NOW-8*86400), dict(stamp=NOW+60), dict(preview=True)])
def test_unknown_cross_target_expired_future_or_preview_never_returns_text(tmp_path, options):
    saved(tmp_path, **options)
    result = detail.query(data_root=tmp_path, now=NOW)
    assert result['status'] == 'not_found' and 'text' not in result


@pytest.mark.parametrize('fingerprint', [None, 'old'])
def test_revoked_or_legacy_permissions_withhold_even_saved_content(tmp_path, fingerprint):
    saved(tmp_path, fingerprint=fingerprint)
    result = detail.query(data_root=tmp_path, now=NOW)
    assert result['status'] == 'withheld' and 'Dana' not in json.dumps(result)


def test_multiple_topic_matches_request_choice_then_explicit_id_pages_exact_text(tmp_path):
    text = 'A long deck note. ' * 1000
    saved(tmp_path, text=text)
    saved(tmp_path, ident='b'*24, text='Another deck note.')
    result = detail.query(data_root=tmp_path, text='deck', now=NOW)
    assert result['status'] == 'ambiguous' and len(result['matches']) == 2
    first = detail.query(data_root=tmp_path, artifact=IDENT, now=NOW)
    second = detail.query(data_root=tmp_path, artifact=IDENT, offset=first['next_offset'], now=NOW)
    assert first['text'] + second['text'] == text
    assert second['next_offset'] is None


def test_no_path_escape_or_symlink_read(tmp_path):
    folder = saved(tmp_path)
    original = folder / 'manifest.json'
    original.rename(tmp_path / 'private.json')
    original.symlink_to(tmp_path / 'private.json')
    assert detail.query(data_root=tmp_path, now=NOW)['status'] == 'not_found'
    assert detail.query(data_root=tmp_path, artifact='../private', now=NOW)['status'] == 'invalid_selector'


def test_missing_timezone_uses_utc_not_host_zone(tmp_path, monkeypatch):
    monkeypatch.setattr(detail.tzchain, 'configured_tz_name', lambda root: None)
    saved(tmp_path)
    assert detail.query(data_root=tmp_path, day='2026-09-24', now=NOW)['status'] == 'found'


def test_default_channel_receipt_does_not_follow_owner_reconfiguration(tmp_path, monkeypatch):
    saved(tmp_path, target='photon')
    assert detail.query(data_root=tmp_path, now=NOW)['status'] == 'found'
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', 'different-owner')
    result = detail.query(data_root=tmp_path, now=NOW)
    assert result['status'] == 'not_found' and 'text' not in result


@pytest.mark.parametrize('rows', [None, {}, [{'status': 'delivered', 'receipt': 'broken'}]])
def test_malformed_outbox_does_not_hide_valid_interactive_receipt(tmp_path, rows):
    saved(tmp_path, interactive=True)
    events = tmp_path / 'events'
    events.mkdir()
    (events / 'outbox.json').write_text(json.dumps({'rows': rows}))
    assert detail.query(data_root=tmp_path, now=NOW)['status'] == 'found'
