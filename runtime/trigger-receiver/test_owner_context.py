"""Original explicit About/People append, exact accepted review and immutable recovery."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import runtime_owner_preference_tool as tool
from test_runtime_owner_preference_tool import context, request, call

__all__ = ['context']

@pytest.fixture
def direct(context, monkeypatch):
    writer = tool._preferences()
    monkeypatch.setattr(tool, '_preferences', lambda: writer)
    from runtime_owner_loop import _load_writer
    _load_writer()
    import master_file as master
    def prepare(section='About', text='I run Cedar.', event='a' * 64, confirmation='d' * 64):
        proposal = request({'operation': 'propose-context', 'section': section, 'text': text},
                           operation='preview', event=event, text='Remember: ' + text)
        preview = call(proposal, context)
        assert preview['status'] == 'preview'
        encoded = json.dumps(proposal['args'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        return request({'operation': 'append-context', 'review': preview['review'], 'proposal': {
            'event_id': event, 'action_id': proposal['action_id'],
            'args_hash': hashlib.sha256(encoded.encode()).hexdigest(),
            'accepted_dispatch_id': 'accepted-context', 'source_revision': 'owner-only-context-v1'}},
            event=confirmation, text='Yes.')
    return writer, master, prepare


@pytest.mark.parametrize('section', ['About', 'People'])
def test_original_append_concurrent_sections_and_prompt_receipt(direct, context, section):
    writer, master, prepare = direct
    master.append_section(section, 'Earlier context.')
    body = prepare(section)
    before = Path(master.path()).read_bytes()
    master.append_section(section, 'Concurrent owner context.')
    master.append_section('Procedures', 'Concurrent rule.')
    master.set_section('Priorities', 'Fundraising')
    result = call(body, context)
    assert result['status'] == 'committed'
    assert result['outcome']['section'] == section
    assert master.read().count('I run Cedar.') == 1
    assert 'Earlier context.\nConcurrent owner context.\nI run Cedar.' in master.read()
    assert 'Concurrent rule.' in master.read() and 'Fundraising' in master.read()
    assert before != Path(master.path()).read_bytes()
    import compose_brief
    spec = importlib.util.spec_from_file_location('context_prep', Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff/meeting-prep/scripts/compose_meeting_prep.py')
    prep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prep)
    assert 'I run Cedar.' in compose_brief._format_master_context()
    assert 'I run Cedar.' in prep._master_context()
    saved = writer.jsonstore.read(writer._path())['owner_preference_receipts']
    writer.add('vip_people', 'Dana')
    assert writer.jsonstore.read(writer._path())['owner_preference_receipts'] == saved
    Path(master.path()).unlink()
    assert call(dict(body, operation='inspect'), context) == result


@pytest.mark.parametrize('section', ['Procedures', 'Priorities', 'about', 'Other'])
def test_arbitrary_section_refused(direct, context, section):
    _, master, _ = direct
    with pytest.raises(ValueError):
        call(request({'operation': 'propose-context', 'section': section, 'text': 'I run Cedar.'}, operation='preview', text='I run Cedar.'), context)
    assert not Path(master.path()).exists()


@pytest.mark.parametrize('text', ['## About', 'first\nsecond', ' leading', 'bad\tcontrol', 'bad\u2028line'])
def test_line_injection_and_nonverbatim_refused(direct, context, text):
    _, master, _ = direct
    with pytest.raises(ValueError):
        call(request({'operation': 'propose-context', 'section': 'People', 'text': text}, operation='preview', text=text), context)
    assert not Path(master.path()).exists()


@pytest.mark.parametrize('section', ['About', 'People'])
def test_postwrite_unknown_never_repeat(direct, context, monkeypatch, section):
    _, master, prepare = direct
    body = prepare(section)
    original = master.append_section
    writes = []
    def lost(name, text):
        writes.append((name, text))
        original(name, text)
        raise OSError('lost after original append')
    monkeypatch.setattr(master, 'append_section', lost)
    assert call(body, context)['status'] == 'unknown'
    assert call(dict(body, operation='inspect'), context)['status'] == 'unknown'
    assert writes == [(section, 'I run Cedar.')]
    assert master.read().count('I run Cedar.') == 1


def test_bound_owner_span_section_proof_and_capacity(direct, context):
    _, master, prepare = direct
    assert call(request({'operation': 'propose-context', 'section': 'About', 'text': 'Invented'}, operation='preview', text='My words'), context)['code'] == 'owner_span_unavailable'
    body = prepare()
    assert call(request(body['args'], text='Maybe later', event=body['event_id']), context)['code'] == 'target_unavailable'
    altered = dict(body['args']['review'], section='People')
    altered = tool._preferences().owner_context_review('People', altered['text'], master)
    with pytest.raises(ValueError, match='proposal proof'):
        call(request({**body['args'], 'review': altered}, text='Yes.'), context)
    master.set_section('People', 'x' * 7900)
    body = prepare(text='y' * 200)
    before = Path(master.path()).read_bytes()
    result = call(body, context)
    assert result['code'] == 'master_capacity'
    assert result['capacity']['largest_section']['name'] == 'People'
    assert Path(master.path()).read_bytes() == before
    assert call(dict(body, operation='inspect'), context) == result
