"""Location prep route handoff through the existing receiver worker harness."""
import json
import types
import pytest
import test_receiver as harness

rec = harness.rec
PROCEDURE = harness.PROCEDURE
_capture_oneshot = harness._capture_oneshot
_run_oneshot = harness._run_oneshot


def test_prep_location_update_requires_same_current_route(monkeypatch):
    effects = [{'kind': 'prep_location_update', 'baseline': {'route_digest': 'a' * 64}}]
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: 'b' * 64)
    monkeypatch.setattr(rec, '_shared_effects', lambda: types.SimpleNamespace(valid=lambda *_: True))
    assert not rec._delivery_valid({'label': 'event', 'effects': effects})
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: 'a' * 64)
    assert rec._delivery_valid({'label': 'event', 'effects': effects})


@pytest.mark.parametrize('label', ['meeting-prep:' + 'd' * 32, 'skill:' + 'd' * 64 + ':meeting-prep'])
def test_meeting_prep_child_receives_existing_composition_route_proof(tmp_path, monkeypatch, label):
    rec.DATA = str(tmp_path)
    proof = {'source_revision': 'a' * 64, 'route_digest': 'b' * 64}
    monkeypatch.delenv('SOTTO_SKILL_COMPOSITION_PROOF', raising=False)
    monkeypatch.setattr(rec, '_composition_evidence', lambda _: proof)
    monkeypatch.setattr(rec, '_retained_proof_current', lambda _: True)
    runs = _capture_oneshot(monkeypatch, rec)
    _run_oneshot(rec, PROCEDURE, label)
    assert json.loads(runs[0]['env']['SOTTO_SKILL_COMPOSITION_PROOF']) == proof
