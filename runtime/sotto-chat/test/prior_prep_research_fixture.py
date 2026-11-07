"""Synthetic accepted prep for cross-runtime company continuation acceptance."""
import hashlib
import json
import os
import sys
from pathlib import Path


def configure(receiver):
    root = Path(receiver.DATA)
    pack = Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff'
    sys.path.insert(0, str(pack / '_shared/knowledge'))
    import knowledge_update  # noqa: PLC0415 - needs the synthetic pack path
    knowledge_update.apply({'company_updates': [{'company_name': 'Cedar Labs', 'domain': 'cedar.example',
                                               'about': 'Builds clinic tools.'}]})
    route = receiver._candidate_retained_route_digest()
    body = 'Meeting prep for Dana at Cedar Labs. Ask about clinic procurement.'
    record = {'provider': 'gemini', 'stage': 'focus', 'subjects': ['dana@cedar.example'],
              'response_json': '{"company":"Cedar Labs"}', 'response_sha256': 'c' * 64,
              'grounding_json': '{"uri":"https://cedar.example"}', 'truncated': False}
    evidence = {'status': 'retained', 'input_sha256': 'd' * 64, 'records': [record], 'omitted_records': 0}
    os.environ['SOTTO_VISUAL_BRIEFS'] = '0'
    assert receiver._deliver_text(body, 'meeting-prep:connected-prior', source_revision_at_composition='a'*64,
                                  route_digest_at_composition=route,
                                  source_text_sha256=hashlib.sha256(body.encode()).hexdigest(), research_evidence=evidence)
    receipt = json.loads((root/'events/outbox.json').read_text())['rows'][0]['receipt']
    (root/'synthetic-prior-prep.json').write_text(json.dumps({'route': route, 'body': body, 'message_id': receipt['message_id']}))
    provider_path = root/'synthetic-provider/sitecustomize.py'
    provider = provider_path.read_text().replace("'Cedar Labs' in body['prompt']", "'cedar.example' in body['prompt']")
    provider = provider.replace("assert 'Cedar Labs' in body['contents'][0]['parts'][0]['text']", "assert 'cedar.example' in body['contents'][0]['parts'][0]['text']")
    provider = provider.replace("assert 'skill' not in body,", "assert 'Who are his competitors?' in body['prompt'] and 'Juniper Systems' in body['prompt'] and 'https://cedar.example/market' in body['prompt']\n        assert 'skill' not in body,")
    provider = provider.replace("'market':'Clinic operations'", "'market':'Juniper Systems competes in clinic operations (https://cedar.example/market).' ")
    provider = provider.replace("Cedar Labs builds clinic tools. Company-only grounded research is saved.", "Cedar Labs competitor: Juniper Systems (https://cedar.example/market).")
    provider_path.write_text(provider)
