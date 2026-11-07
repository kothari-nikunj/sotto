"""Synthetic cross-owner acceptance through retained research and canonical memory."""
# ruff: noqa: PLR0915, PLR2004
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_ask_research_pipeline import PACK, ask, seed  # noqa: F401


def test_real_partial_recovery_then_owner_correction_and_fresh_process_read(ask, monkeypatch):  # noqa: F811
    pipeline, kg, _, _, invoke, root = ask
    import persist_prep as pp
    import knowledge_update as ku
    import knowledge_edit as edit
    import jsonstore
    seed(kg)
    source = {'attendees': [{'email': 'dana@cedar.com', 'title': 'CEO', 'company': 'Cedar',
        'summary': 'Leads the platform team.', 'company_summary': 'Cedar builds team tools.',
        'conversation_hooks': ['Ask about a deal next Tuesday.'],
        'recent_activity': [{'when': '2026-11-01', 'what': 'Published a platform guide.',
                             'source_url': 'https://cedar.example/guide'}]}]}
    stages, answers, receipts = [], [], []
    company_writer = ku._write_company
    checkpoint_writer = jsonstore.write_atomic

    def unavailable(*args, **kwargs):
        raise OSError('synthetic company volume unavailable')

    def command(script, *args):
        if script.endswith('research_attendees.py'):
            stages.append('research')
            return source
        stages.append('persist')
        previous = (json.loads(Path(args[args.index('--previous-receipt') + 1]).read_text())
                    if '--previous-receipt' in args else None)
        result = pp.persist(json.loads(Path(args[1]).read_text()),
                            json.loads(Path(args[3]).read_text()), previous_receipt=previous)
        receipts.append(result)
        return result

    def stop_after_receipt(path, value, *args, **kwargs):
        checkpoint_writer(path, value, *args, **kwargs)
        if value.get('persistence', {}).get('status') == 'completed' and 'answer' not in value:
            raise InterruptedError('synthetic process stop after durable receipt')

    def answer(prompt, _job):
        answers.append(prompt)
        assert '"complete": true' in prompt
        return {'text': 'Dana leads Cedar.', 'usage': {}}

    monkeypatch.setattr(ku, '_write_company', unavailable)
    monkeypatch.setattr(jsonstore, 'write_atomic', stop_after_receipt)
    with pytest.raises(InterruptedError):
        invoke(command=command, answer=answer)
    assert receipts[0]['complete'] is False and receipts[0]['persisted'] == 1
    assert receipts[0]['outcomes']['companies'][0]['status'] == 'failed'
    assert not pp.profile_is_fresh('Dana', 'dana@cedar.com')
    monkeypatch.setattr(ku, '_write_company', company_writer)
    monkeypatch.setattr(jsonstore, 'write_atomic', checkpoint_writer)
    result = invoke(command=command, answer=answer)
    assert receipts[1]['complete'] is True and receipts[1]['companies'] == ['Cedar']
    assert stages == ['research', 'persist', 'persist'] and len(answers) == 1
    assert invoke(command=command, answer=answer) == result
    assert stages == ['research', 'persist', 'persist']
    path = Path(kg.find_person_file(name='Dana', identifier='dana@cedar.com'))
    person = kg.parse_person_file(path.read_text())
    assert len(person.facts) == 2 and all(f.seen == 1 for f in person.facts.values())
    activity = next(f for f in person.facts.values() if 'platform guide' in f.text)
    assert activity.source_ref == 'https://cedar.example/guide'
    assert activity.source == 'web_research'
    assert 'next Tuesday' not in path.read_text()
    bio_id = next(fid for fid, fact in person.facts.items() if 'CEO at Cedar' in fact.text)
    assert person.identity_support['title']['fact_id'] == bio_id
    edit.op_correct(path.stem, bio_id, 'Dana is CTO at Cedar, not CEO.')
    corrected = kg.parse_person_file(path.read_text())
    assert corrected.title is None and corrected.company is None
    assert corrected.identity_support['title']['source'] == 'user_edit'
    replay = pp.persist(source, [{'name': 'Dana', 'email': 'dana@cedar.com'}])
    assert any(item.get('reason') == 'protected_owner_correction'
               for item in replay['outcomes']['people'][0]['fact_outcomes']['items'])
    corrected = kg.parse_person_file(path.read_text())
    assert corrected.title is None and corrected.company is None
    assert len(corrected.facts) == 3
    with pytest.raises(RuntimeError, match='context changed'):
        invoke(command=command, answer=answer)
    assert stages == ['research', 'persist', 'persist'] and len(answers) == 1
    code = '''import sys,json
from datetime import datetime,timezone
sys.path[:0]=sys.argv[1:3]
import knowledge as kg,knowledge_query as query
p=kg.parse_person_file(open(sys.argv[3]).read())
print(json.dumps({'title':p.title,'company':p.company,'pack':query.pack_person(p,True,datetime.now(timezone.utc))}))
'''
    restarted = subprocess.run([sys.executable, '-I', '-c', code,
        str(PACK / '_shared/knowledge'), str(PACK / '_shared/lib'), str(path)],
        env=dict(os.environ), capture_output=True, text=True, timeout=20)
    assert restarted.returncode == 0, restarted.stderr
    packed = json.loads(restarted.stdout)
    assert packed['title'] is None and packed['company'] is None
    assert 'CEO @ Cedar' not in packed['pack'] and 'Dana is CTO at Cedar, not CEO.' in packed['pack']
    assert 'Cedar builds team tools.' in ku.company_knowledge(company_name='Cedar')['about']
