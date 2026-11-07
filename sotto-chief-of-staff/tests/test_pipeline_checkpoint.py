"""Stage receipts must survive lost completion writes and current-authority changes."""
import json

import pytest

import jsonstore
from pipeline_checkpoint import CheckpointStages


COMPLETION_WRITE = 2


@pytest.mark.parametrize('failed_write', [1, COMPLETION_WRITE])
def test_failed_receipt_write_never_recontacts_on_restart(tmp_path, failed_write):
    path = str(tmp_path / 'checkpoint.json')
    checkpoint, calls, writes = {}, [], []

    def save():
        writes.append(1)
        if len(writes) == failed_write:
            raise OSError('lost receipt')
        jsonstore.write_atomic(path, checkpoint)

    stage = CheckpointStages(checkpoint, save, lambda: None, 'held')
    with pytest.raises(OSError):
        stage('paid', lambda: calls.append(1) or 'result')
    assert calls == ([] if failed_write == 1 else [1])
    if failed_write == COMPLETION_WRITE:
        restored = json.loads((tmp_path / 'checkpoint.json').read_text())
        stage = CheckpointStages(restored, lambda: None, lambda: None, 'held')
        with pytest.raises(RuntimeError, match='held'):
            stage('paid', lambda: pytest.fail('recontacted'))


def test_current_authority_precedes_completed_receipt_replay():
    def revoked():
        raise RuntimeError('revoked')
    stage = CheckpointStages({'paid': {'status': 'completed', 'result': 'private'}},
                             lambda: None, revoked, 'held')
    with pytest.raises(RuntimeError, match='revoked'):
        stage('paid', lambda: pytest.fail('recontacted'))
