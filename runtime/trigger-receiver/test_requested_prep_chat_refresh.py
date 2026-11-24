from datetime import datetime, timedelta, timezone
from pathlib import Path

from test_shared_meeting_prep_pipeline import PACK, pipeline  # noqa: F401 — shared worker fixture


def test_actual_requested_worker_uses_private_chat_correction_only_at_composition(pipeline, monkeypatch):  # noqa: F811
    import compose_meeting_prep
    import source_context
    module, command, _, _, _, models, _, _ = pipeline
    jid = '14155550107@s.whatsapp.net'
    row = {'contact_jid': jid, 'is_from_me': False, 'timestamp': datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(source_context, 'read_local', lambda _: {
        'contacts': [{'name': 'Dana', 'emails': ['dana@outside.com'], 'phones': ['+14155550107']}],
        'source_status': {'whatsapp': 'ok', 'deferred_unread_whatsapp': 'partial'}, 'whatsapp': [{**row, 'text': 'See you there.'}]})
    correction = 'Saturday is staff only. Invite guests Sunday.'
    reads = []

    def bridge(name, args, timeout):
        reads.append((name, args['identifier']))
        return {'source': 'whatsapp', 'identifier': jid, 'messages': [
            {**row, 'text': correction, 'timestamp': (datetime.now(timezone.utc)-timedelta(days=8)).isoformat()}]}

    def provider(script, *args, **kwargs):
        if script.endswith('research_attendees.py'):
            for option in ('--attendees', '--context', '--comms'):
                assert correction not in Path(args[args.index(option) + 1]).read_text()
        return command(script, *args, **kwargs)

    monkeypatch.setattr(source_context, 'bridge_call', bridge)
    result = module.run(PACK, {'mode': 'focus', 'focus': 'Dana'}, provider)
    assert reads == [('get_messages', jid)]
    assert {'contacts', 'whatsapp'}.issubset(result['sources'])
    assert correction in compose_meeting_prep.build_context(models[0])[0]
