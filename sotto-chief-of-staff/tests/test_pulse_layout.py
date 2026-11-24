"""How the relationship pulse's commitment list reads on the owner's phone."""
import importlib.util
import os
from datetime import datetime, timezone

spec = importlib.util.spec_from_file_location("pulse_layout_rp", os.path.join(
    os.path.dirname(__file__), "..", "relationship-pulse", "scripts", "relationship_pulse.py"))
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


def test_commitment_list_reads_as_heading_list_and_closing_paragraphs():
    # Nov 16, 2026: the heading, list and closing lines ran together, and one stored ask began
    # lowercase and without final punctuation.
    rows = [dict(name='Ellen Kim', identifier='ellen@example.com', channel='gmail', what='Asked about the podcast'),
            dict(name='Rae Diaz', identifier='rae@example.com', channel='gmail',
                 what='asking if any fund operators come to mind', deadline='2026-11-21')]
    rows += [dict(name=f'Person {i}', identifier=f'p{i}@example.com', channel='gmail', what=f'Item {i}')
             for i in range(5)]
    now = datetime(2026, 11, 16, 10, tzinfo=timezone.utc)
    text = rp.with_commitments({'attention_queue': []}, {'you_owe': rows}, now=now)['pulse_markdown']
    head, entries, closing = text.split('\n\n')
    assert head == '*Follow-ups*'
    lines = entries.split('\n')
    assert lines[0] == '- *Ellen Kim* - Asked about the podcast.'
    assert lines[1] == '- *Rae Diaz* - Asking if any fund operators come to mind (due Nov 21).'
    assert len(lines) == rp.MAX_QUEUE and all(line.startswith('- *') for line in lines)
    assert closing == '2 other items are on your list.\nI can draft a reply to Ellen Kim.'


def test_quoted_relative_ask_keeps_its_verbatim_wording():
    result = rp.with_commitments({'attention_queue': [], 'pulse_markdown': ''}, {'you_owe': [
        {'name': 'Dana', 'what': 'send it tomorrow', 'channel': 'imessage'}]})
    assert 'Earlier request (date unknown): “send it tomorrow”' in result['pulse_markdown']
