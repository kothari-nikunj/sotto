"""Admitted proactive source restriction narrows original retained readers."""
import importlib.util
import json
from pathlib import Path

import pytest
from test_runtime_owner_loop_tool import context,rows,ke
from test_runtime_source_loop_tool import source,actual_writer_module

__all__=['context','actual_writer_module']
PACK=Path(__file__).parents[2]/'sotto-chief-of-staff'


def test_captured_sources_withhold_cached_local_and_original_loop_reader(context,monkeypatch):
    source(context)
    row=rows()[0];row['deadline']='2026-10-01'
    ke._load_continuity()._persist(row)
    spec=importlib.util.spec_from_file_location('admitted_source_scanner',PACK/'proactive/scripts/proactive_scan.py')
    scanner=importlib.util.module_from_spec(spec);spec.loader.exec_module(scanner)
    import source_context
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE','self-host')
    monkeypatch.setenv('SOTTO_PROACTIVE_CAPTURED_SOURCES','["gmail"]')
    assert scanner._open_loops()
    monkeypatch.setenv('SOTTO_PROACTIVE_CAPTURED_SOURCES','[]')
    assert scanner._open_loops()==[]
    projected=source_context.project_local({'imessage':[{'text':'private'}],'contacts':[{'name':'Dana'}]})
    assert 'imessage' not in projected and 'contacts' not in projected
    assert not source_context.allowed('calendar') and not source_context.allowed('gmail')


@pytest.mark.parametrize('value',['null','["gmail","gmail"]','["unknown"]','{}','invalid'])
def test_malformed_captured_list_never_grants(value,context,monkeypatch):
    import source_context
    monkeypatch.setenv('SOTTO_PROACTIVE_CAPTURED_SOURCES',value)
    assert not any(source_context.allowed(source) for source in source_context.CONTEXT_SOURCES)


def test_captured_membership_never_broadens_managed_grant(context,monkeypatch):
    import source_context
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE','managed')
    monkeypatch.setenv('SOTTO_TENANT_ID','synthetic')
    path=context[0]/'config/managed-capabilities.json';path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({'tenant_id':'synthetic','sources':{'gmail':{'consented':False,'connected':True}}}))
    monkeypatch.setenv('SOTTO_PROACTIVE_CAPTURED_SOURCES','["gmail"]')
    assert not source_context.allowed('gmail')
