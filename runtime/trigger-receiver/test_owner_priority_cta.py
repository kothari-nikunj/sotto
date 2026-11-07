"""Original exact weekly priority CTA, one event and original writer semantics."""
from pathlib import Path

import pytest
import runtime_owner_preference_tool as tool
from test_runtime_owner_preference_tool import context, request, call

__all__ = ['context']

@pytest.fixture
def cta(context, monkeypatch):
    writer = tool._preferences()
    monkeypatch.setattr(tool, '_preferences', lambda: writer)
    from runtime_owner_loop import _load_writer
    _load_writer()
    import master_file as master
    return writer, master


def body(name='Maya', **kwargs):
    return request({'operation': 'prioritize', 'name': name}, text='prioritize staying in touch with '+name, **kwargs)


def test_original_add_preserves_priorities_and_receipt_without_review(cta, context):
    writer, master = cta
    master.set_section('Priorities', '1. Fundraising\n- Hiring')
    master.append_section('About', 'Owner context')
    result = call(body(), context)
    assert result['status'] == 'committed'
    assert result['outcome'] == {'operation': 'prioritize', 'section': 'Priorities', 'name': 'Maya', 'text': 'Staying in touch with Maya', 'status': 'saved'}
    assert [r['text'] for r in master.priorities()['priorities']] == ['Fundraising', 'Hiring', 'Staying in touch with Maya']
    assert 'Owner context' in master.read()
    import compose_brief
    assert 'Staying in touch with Maya' in compose_brief._format_master_context()
    saved = writer.jsonstore.read(writer._path())['owner_preference_receipts']
    writer.add('vip_people', 'Dana')
    assert writer.jsonstore.read(writer._path())['owner_preference_receipts'] == saved
    Path(master.path()).unlink()
    assert call(body(operation='inspect'), context) == result


def test_original_duplicate_at_capacity_success_new_name_refused(cta, context):
    _, master = cta
    master.set_section('Priorities', '- Fundraising\n2. Staying in touch with MAYA\nHiring')
    result = call(body(), context)
    assert result['status'] == 'committed'
    assert len(master.priorities()['priorities']) == 3
    assert master.read().count('MAYA') == 1
    before = Path(master.path()).read_bytes()
    rejected = call(body('Dana', event='d' * 64), context)
    assert rejected == {'version': 1, 'status': 'rejected', 'code': 'priorities_full'}
    assert Path(master.path()).read_bytes() == before
    assert call(body('Dana', event='d' * 64, operation='inspect'), context) == rejected


@pytest.mark.parametrize('text', ['yes', 'Please prioritize staying in touch with Maya', '"prioritize staying in touch with Maya"', 'The message says prioritize staying in touch with Maya', 'prioritize staying in touch with Dana', 'prioritize staying in touch with Maya\nand Dana'])
def test_only_whole_current_exact_owner_command(cta, context, text):
    _, master = cta
    result = call(request({'operation': 'prioritize', 'name': 'Maya'}, text=text), context)
    assert result['code'] == 'owner_span_unavailable'
    assert not Path(master.path()).exists()


def test_uncertain_original_add_never_reapplied(cta, context, monkeypatch):
    _, master = cta
    original = master.add_priority
    writes = []
    def lost(text):
        writes.append(text)
        original(text)
        raise OSError('lost after original priority write')
    monkeypatch.setattr(master, 'add_priority', lost)
    assert call(body(), context)['status'] == 'unknown'
    assert call(body(operation='inspect'), context)['status'] == 'unknown'
    assert writes == ['Staying in touch with Maya']
    assert master.read().count('Staying in touch with Maya') == 1


def test_original_file_capacity_and_no_arbitrary_input(cta, context):
    _, master = cta
    master.set_section('About', 'x' * 7990)
    before = Path(master.path()).read_bytes()
    result = call(body(), context)
    assert result['code'] == 'master_capacity'
    assert result['capacity']['largest_section']['name'] == 'About'
    assert Path(master.path()).read_bytes() == before
    with pytest.raises(ValueError):
        call(request({'operation': 'prioritize', 'name': 'Maya', 'text': 'Fundraising'}, text='prioritize staying in touch with Maya'), context)
