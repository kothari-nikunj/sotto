"""Onboarding's sticky ambiguity holds lift only on a definite outbox answer."""
import json

import onboarding


def test_ambiguity_flags_clear_only_on_a_definite_outbox_answer(tmp_path):
    """The flags never authorize a replay, but they also must not outlive a definite answer:
    once the send they guarded is on file as refused, the welcome may be composed."""
    state = tmp_path / 'config/onboarding.json'
    state.parent.mkdir()
    outbox = tmp_path / 'events/outbox.json'
    outbox.parent.mkdir()
    started = []
    # Scheduled: flagged by its row, evidence pruned → still waiting; that row refused → cleared.
    brief = {'id': 'a' * 32, 'day': '2026-09-26', 'status': 'failed', 'acceptance': 'unknown',
             'acceptance_uncertain': True, 'payload': {'label': 'cron:sotto-morning-brief'}}
    (tmp_path / 'briefs').mkdir()
    (tmp_path / 'briefs/2026-09-26.morning.delivered').write_text('a' * 32)
    outbox.write_text(json.dumps({'rows': [brief]}))
    assert onboarding.tick(tmp_path, True, lambda: started.append(1), 99) == 'waiting'
    assert json.loads(state.read_text())['scheduled_ambiguous_ids'] == ['a' * 32]
    outbox.write_text(json.dumps({'rows': []}))
    assert onboarding.tick(tmp_path, True, lambda: started.append(1), 100) == 'waiting'
    definite = {**brief, 'acceptance': 'rejected', 'acceptance_uncertain': False}
    outbox.write_text(json.dumps({'rows': [definite]}))
    saved = state.read_text()
    state.write_text(json.dumps({'scheduled_ambiguous': True}))  # set before ids were stored
    assert onboarding.tick(tmp_path, True, lambda: started.append(1), 100) == 'waiting'
    state.write_text(saved)
    assert onboarding.tick(tmp_path, True, lambda: started.append(1), 101) == 'started'
    assert started == [1] and 'scheduled_ambiguous' not in json.loads(state.read_text())
    # Welcome: an uncertain row keeps the flag; once the row is a definite refusal it clears.
    welcome = {'id': 'w' * 32, 'status': 'failed', 'payload': {'label': onboarding.LABEL}}
    state.write_text('{}')
    outbox.write_text(json.dumps({'rows': [{**welcome, 'acceptance': 'unknown',
                                            'acceptance_uncertain': True}]}))
    assert onboarding.tick(tmp_path, True, lambda: started.append(2), 200000) == 'waiting'
    assert onboarding.status(tmp_path, 200000) == 'waiting'
    outbox.write_text(json.dumps({'rows': [{**welcome, 'acceptance': 'rejected',
                                            'acceptance_uncertain': False}]}))
    assert onboarding.status(tmp_path, 200001) == 'retrying'
    assert onboarding.tick(tmp_path, True, lambda: started.append(2), 200001) == 'started'
    assert started == [1, 2] and 'welcome_ambiguous' not in json.loads(state.read_text())


def test_a_flag_whose_own_row_was_pruned_holds_beside_other_definite_rows(tmp_path):
    """Another definite row is not an answer about the send that set the flag."""
    state = tmp_path / 'config/onboarding.json'
    state.parent.mkdir()
    outbox = tmp_path / 'events/outbox.json'
    outbox.parent.mkdir()
    started = []
    brief = {'id': 'b' * 32, 'day': '2026-09-26', 'status': 'failed', 'acceptance': 'unknown',
             'acceptance_uncertain': True, 'payload': {'label': 'cron:sotto-morning-brief'}}
    (tmp_path / 'briefs').mkdir()
    (tmp_path / 'briefs/2026-09-26.morning.delivered').write_text('b' * 32)
    outbox.write_text(json.dumps({'rows': [brief]}))
    assert onboarding.tick(tmp_path, True, lambda: started.append(1), 100) == 'waiting'
    assert json.loads(state.read_text())['scheduled_ambiguous'] is True
    # The flagged row is pruned; a later brief is on file as superseded.
    outbox.write_text(json.dumps({'rows': [{**brief, 'id': 'c' * 32, 'day': '2026-09-27',
                                            'status': 'superseded', 'acceptance': 'not_attempted',
                                            'acceptance_uncertain': False}]}))
    assert onboarding.tick(tmp_path, True, lambda: started.append(1), 101) == 'waiting'
    assert onboarding.status(tmp_path, 101) == 'waiting'
    assert started == [] and json.loads(state.read_text())['scheduled_ambiguous'] is True
    # Welcome: the uncertain welcome row is pruned; an earlier refusal remains.
    welcome = {'id': 'w' * 32, 'status': 'failed', 'acceptance': 'unknown',
               'acceptance_uncertain': True, 'payload': {'label': onboarding.LABEL}}
    state.write_text('{}')
    outbox.write_text(json.dumps({'rows': [welcome]}))
    assert onboarding.tick(tmp_path, True, lambda: started.append(2), 200000) == 'waiting'
    outbox.write_text(json.dumps({'rows': [{**welcome, 'id': 'v' * 32, 'acceptance': 'rejected',
                                            'acceptance_uncertain': False}]}))
    assert onboarding.status(tmp_path, 200001) == 'waiting'
    assert onboarding.tick(tmp_path, True, lambda: started.append(2), 200001) == 'waiting'
    assert started == [] and json.loads(state.read_text())['welcome_ambiguous'] is True


def test_scheduled_refusal_during_hold_records_the_original_send(tmp_path, monkeypatch):
    state = tmp_path / 'config/onboarding.json'
    state.parent.mkdir()
    outbox = tmp_path / 'events/outbox.json'
    outbox.parent.mkdir()
    marker = tmp_path / 'briefs/2026-09-26.morning.delivered'
    marker.parent.mkdir()
    key = 'a' * 32
    marker.write_text(key)
    brief = {'id': key, 'day': '2026-09-26', 'status': 'failed', 'acceptance': 'unknown',
             'acceptance_uncertain': True, 'payload': {'label': 'cron:sotto-morning-brief'}}
    outbox.write_text(json.dumps({'rows': [brief]}))
    transaction = onboarding.json_transaction

    def during_save(*args, **kwargs):
        outbox.write_text(json.dumps({'rows': [
            {**brief, 'acceptance': 'rejected', 'acceptance_uncertain': False}]}))
        marker.unlink()
        return transaction(*args, **kwargs)

    started = []
    with monkeypatch.context() as m:
        m.setattr(onboarding, 'json_transaction', during_save)
        assert onboarding.tick(tmp_path, True, lambda: started.append(1), 100) == 'waiting'
    assert json.loads(state.read_text())['scheduled_ambiguous_ids'] == [key]
    assert onboarding.tick(tmp_path, True, lambda: started.append(1), 101) == 'started'
    assert started == [1]
