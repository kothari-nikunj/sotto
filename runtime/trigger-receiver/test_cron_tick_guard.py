"""One failing notice must not abort the rest of the cron tick."""
from test_proactive_procedure import rec as rec  # noqa: PLC0414 — pytest fixture export


def test_no_sources_notice_failure_is_logged_and_the_tick_continues(rec, monkeypatch, capsys):
    prepared = []
    monkeypatch.setattr(rec, '_activation_tick', lambda: None)
    monkeypatch.setattr(rec, '_selected_fixed_profile', lambda: False)
    monkeypatch.setattr(rec, '_owner_reminder_tick', lambda: None)
    monkeypatch.setattr(rec, '_background_context_tick', lambda: None)
    monkeypatch.setattr(rec, '_prepare_briefs', lambda: prepared.append(True))
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda: [])
    import managed  # the receiver imports it by name inside the tick
    monkeypatch.setattr(managed, 'notice_pending', lambda data: True)

    def broken(*_args, **_kwargs):
        raise TimeoutError('outbox lock busy')
    monkeypatch.setattr(rec, '_deliver_text', broken)
    rec._cron_tick()
    assert prepared == [True]
    assert 'no-sources notice: TimeoutError' in capsys.readouterr().out
