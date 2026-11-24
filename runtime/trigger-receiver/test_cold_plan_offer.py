"""Cold-brief source and offer-binding probe with a scripted model."""

import json
from pathlib import Path

import pytest

import brief_runner
from test_brief_runner import PACK, procedure

__all__ = ['procedure']


def test_plan_sources_reach_composer_but_inline_offer_has_no_delivery_binding(procedure, monkeypatch):
    from datetime import datetime
    import delivery_effects
    import work_queue

    monkeypatch.syspath_prepend(str(PACK / 'evals'))
    import cross_source_plan as probe

    fixture = json.loads(probe.FIXTURE.read_text())
    case = fixture['cases']['footbridge']
    cutoff = datetime.fromisoformat(fixture['cutoff'])
    snapshot = probe.inputs(case, cutoff, 'combined')
    request, _ = procedure
    Path(request['payload_path']).write_text(json.dumps(snapshot['local']))
    original = brief_runner.subprocess.run
    prompts, staged = [], []
    monkeypatch.setattr(work_queue, 'enqueue', lambda *_a, **_kw: 'synthetic-job')
    monkeypatch.setattr(delivery_effects, 'stage', lambda effects: staged.extend(effects))

    def invoke(argv, **kwargs):
        response = original(argv, **kwargs)
        name = Path(argv[1]).name

        def path(flag):
            return Path(argv[argv.index(flag) + 1])

        if name == 'gather_google.py':
            path('--gmail-out').write_text(json.dumps(snapshot['google']['emails']))
        if name == 'compose_brief.py' and '--gmail' in argv:
            google = {**snapshot['google'], 'emails': json.loads(path('--gmail').read_text())}
            local = json.loads(path('--local').read_text())
            assert [row['rowid'] for row in local['whatsapp']] == ['plan', 'unrelated', 'namesake']
            result = probe.golden.cb.compose({**snapshot, 'google': google, 'local': local},
                llm=probe.scripted(case, 'combined', prompts), critic=False)
            response.stdout = json.dumps(result)
        return response

    monkeypatch.setattr(brief_runner.subprocess, 'run', invoke)
    assert 'I can draft a reply.' in brief_runner.run(request)
    prompt, = prompts
    assert case['email_text'] in prompt and case['whatsapp_text'] in prompt
    assert not any(effect['kind'] == 'pending_offer' for effect in staged)
    assert any({'gmail', 'whatsapp'} <= set(effect['sources'])
               for effect in staged if effect['kind'] == 'source_permissions')


@pytest.mark.parametrize('name', ['footbridge', 'greenhouse'])
@pytest.mark.parametrize('mode', ['selfhost', 'managed'])
def test_plan_probe_distinguishes_cutoff_and_source_coverage(name, mode, monkeypatch):
    from datetime import datetime

    monkeypatch.syspath_prepend(str(PACK / 'evals'))
    import cross_source_plan as probe

    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', mode)
    monkeypatch.setenv('SOTTO_TENANT_ID', 'fictional-plan-test')
    fixture = json.loads(probe.FIXTURE.read_text())
    case = fixture['cases'][name]
    cutoff = datetime.fromisoformat(fixture['cutoff'])
    arms = {arm: probe.run(case, cutoff, arm) for arm in probe.ARMS}
    combined = arms['combined']
    assert combined['source_ids']['gmail'] == ['fixture-plan']
    assert combined['source_ids']['whatsapp'] == ['plan', 'unrelated', 'namesake']
    assert case['email_text'] in combined['prompt']
    assert case['whatsapp_text'] in combined['prompt']
    assert case['unrelated_text'] in combined['prompt']
    assert case['namesake_text'] in combined['prompt']
    assert case['later_text'] not in combined['prompt']
    assert case['sent_text'] not in combined['prompt']
    assert case['sent_text'] in arms['completed']['prompt']
    assert all(row['cutoff'] == fixture['cutoff'] for row in arms.values())
    for arm in ('email-only', 'unavailable', 'revoked'):
        assert arms[arm]['source_ids']['whatsapp'] == []
        assert case['whatsapp_text'] not in arms[arm]['prompt']
    assert 'Unavailable on this device: WhatsApp' in arms['unavailable']['prompt']
    assert 'Unavailable on this device: WhatsApp' not in arms['email-only']['prompt']
    assert arms['revoked']['whatsapp_status'] == 'disabled'


def test_plan_probe_preserves_failure_record_without_error_contents(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(PACK / 'evals'))
    import cross_source_plan as probe

    target = tmp_path / 'result.json'
    monkeypatch.setattr('sys.argv', ['cross_source_plan.py', '--case', 'footbridge', '--arm',
                                   'combined', '--live', '--output', str(target)])

    calls = []

    def fail(*_args, **_kwargs):
        calls.append(True)
        raise RuntimeError('private-request-secret')

    monkeypatch.setattr(probe, 'run', fail)
    with pytest.raises(RuntimeError):
        probe.main()
    result = json.loads(target.read_text())
    assert result['error_type'] == 'RuntimeError'
    assert result['case'] == 'footbridge' and result['arm'] == 'combined'
    assert result['mode'] == 'live'
    assert 'private-request-secret' not in target.read_text()
    with pytest.raises(FileExistsError):
        probe.main()
    assert calls == [True]


@pytest.mark.parametrize('name', ['footbridge', 'greenhouse'])
def test_cross_source_prompt_hint_requires_exact_identity(name, monkeypatch):
    from datetime import datetime

    monkeypatch.syspath_prepend(str(PACK / 'evals'))
    import cross_source_plan as probe

    fixture = json.loads(probe.FIXTURE.read_text())
    case = fixture['cases'][name]
    cutoff = datetime.fromisoformat(fixture['cutoff'])
    hint = f"- {case['name']}: WhatsApp, Email"
    namesake = probe.run(case, cutoff, 'namesake-only')['prompt']
    assert case['namesake_text'] in namesake
    assert hint not in namesake
    assert '## Cross-Channel Contact Activity' not in namesake
    combined = probe.run(case, cutoff, 'combined')['prompt']
    assert hint in combined
    assert '## Cross-Channel Contact Activity' in combined
    assert f"WhatsApp ({case['phone'][1:]}@s.whatsapp.net)" in combined
    assert 'WhatsApp (12025550142@s.whatsapp.net)' not in combined
