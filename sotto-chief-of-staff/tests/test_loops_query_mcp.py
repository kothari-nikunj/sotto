"""Client commitment page boundaries and metadata."""
import pytest

from test_loops_query import lq


def test_client_commitments_reports_an_unpageable_row_instead_of_restarting(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(lq, 'CLIENT_PAGE_BYTES', 512)
    row = {'anchor_key': 'one', 'name': 'a' * 512}
    with pytest.raises(ValueError, match='commitment row exceeds'):
        lq._client_commitments({'you_owe': [row], 'waiting_on_them': []}, [], lq._now_local('+00:00'), (1, 'first', 'both', 7))


def test_client_commitment_preserves_the_reaction_kind_for_a_visible_hint():
    entry = {'check': {'basis': 'reacted', 'kind': 'like', 'source': 'imessage'}}
    assert lq._client_entry(entry, {})['check'] == entry['check']
