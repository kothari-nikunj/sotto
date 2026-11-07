"""Explicit complete Priorities set uses the original writer and immutable receipt."""
import hashlib
import json
from pathlib import Path

import pytest
import runtime_owner_preference_tool as tool
from test_runtime_owner_preference_tool import context, request, call

__all__ = ['context']

@pytest.fixture
def priorities(context, monkeypatch):
    writer = tool._preferences()
    monkeypatch.setattr(tool, '_preferences', lambda: writer)
    from runtime_owner_loop import _load_writer
    _load_writer()
    import master_file as master
    def prepare(text='Fundraising\nHiring', event='a' * 64, confirmation='d' * 64):
        proposal = request({'operation': 'propose-priorities', 'text': text}, operation='preview',
                           event=event, text='My complete priorities:\n' + text)
        preview = call(proposal, context)
        assert preview['status'] == 'preview'
        encoded = json.dumps(proposal['args'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        return request({'operation': 'replace-priorities', 'review': preview['review'], 'proposal': {
            'event_id': event, 'action_id': proposal['action_id'],
            'args_hash': hashlib.sha256(encoded.encode()).hexdigest(),
            'accepted_dispatch_id': 'accepted-priorities', 'source_revision': 'owner-only-context-v1'}},
            event=confirmation, text='Yes.')
    return writer, master, prepare


def test_original_set_concurrent_sections_receipt_and_next_context(priorities, context):
    writer, master, prepare = priorities
    master.set_section('Priorities', 'Previous priority')
    body = prepare()
    assert body['args']['review']['previous_text'] == 'Previous priority'
    master.append_section('Procedures', 'Concurrent rule')
    result = call(body, context)
    assert result['status'] == 'committed'
    assert 'Previous priority' not in master.read()
    assert 'Concurrent rule' in master.read()
    assert 'Fundraising\nHiring' in master.render_for_prompt()
    import compose_brief
    import importlib.util
    spec = importlib.util.spec_from_file_location("priorities_prep", Path(__file__).resolve().parents[2] / "sotto-chief-of-staff/meeting-prep/scripts/compose_meeting_prep.py")
    prep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prep)
    assert 'Fundraising\nHiring' in compose_brief._format_master_context()
    assert 'Fundraising\nHiring' in prep._master_context()
    saved = writer.jsonstore.read(writer._path())['owner_preference_receipts']
    writer.add('vip_people', 'Dana')
    assert writer.jsonstore.read(writer._path())['owner_preference_receipts'] == saved
    Path(master.path()).unlink()
    assert call(dict(body, operation='inspect'), context) == result


def test_reviewed_priorities_changed_refuses_without_overwrite(priorities, context):
    _, master, prepare = priorities
    body = prepare()
    master.set_section('Priorities', 'Changed current set')
    before = Path(master.path()).read_bytes()
    assert call(body, context)['code'] == 'target_changed'
    assert Path(master.path()).read_bytes() == before


@pytest.mark.parametrize('text', ['four\nexplicit\npriority\nlines', 'one\n\ntwo', '## About', 'one\n## People', ' a ', 'x\tbad', 'x\u2028y'])
def test_invalid_complete_set_no_write(priorities, context, text):
    _, master, _ = priorities
    with pytest.raises(ValueError):
        call(request({'operation': 'propose-priorities', 'text': text}, operation='preview', text=text), context)
    assert not Path(master.path()).exists()


def test_exact_words_distinct_yes_and_unknown_never_repeat(priorities, context, monkeypatch):
    writer, master, prepare = priorities
    assert call(request({'operation': 'propose-priorities', 'text': 'Invented'}, operation='preview', text='My words'), context)['code'] == 'owner_span_unavailable'
    body = prepare()
    assert call(request(body['args'], text='Maybe later', event=body['event_id']), context)['code'] == 'target_unavailable'
    original = master.set_section
    writes = []
    def lost(name, text):
        writes.append(text)
        original(name, text)
        raise OSError('lost after canonical replace')
    monkeypatch.setattr(master, 'set_section', lost)
    assert call(body, context)['status'] == 'unknown'
    assert call(dict(body, operation='inspect'), context)['status'] == 'unknown'
    assert writes == ['Fundraising\nHiring']
    assert 'Fundraising\nHiring' in master.read()


def test_original_capacity_refusal_no_trim(priorities, context):
    _, master, prepare = priorities
    master.set_section('About', 'x' * 7900)
    body = prepare('y' * 200)
    before = Path(master.path()).read_bytes()
    result = call(body, context)
    assert result['code'] == 'master_capacity'
    assert result['capacity']['largest_section']['name'] == 'About'
    assert Path(master.path()).read_bytes() == before
    assert call(dict(body, operation='inspect'), context) == result
