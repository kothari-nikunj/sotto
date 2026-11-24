"""read:all grants resolve to every current read-only tool at request time (owner, Nov 9 2026)."""
import json
import os

import pytest

import client_mcp as mcp
import client_mcp_catalog as catalog


def _token_and_row(root, names):
    token, row = mcp.create_grant(str(root), 'Test client', names, 1)
    return token, row


def test_a_read_all_grant_sees_a_new_read_tool_without_a_new_grant(tmp_path, monkeypatch):
    token, row = _token_and_row(tmp_path, [mcp.READ_ALL])
    assert row['tools'] == [mcp.READ_ALL]
    assert mcp.authenticate(str(tmp_path), token)['tools'] == mcp.read_tools()
    monkeypatch.setitem(mcp.TOOLS, 'sotto_future', mcp.read_tool('Future', 'A new read tool'))
    assert 'sotto_future' not in mcp.authenticate(str(tmp_path), token)['tools']  # not reviewed yet
    monkeypatch.setattr(catalog, 'REVIEWED_READ_TOOLS', catalog.REVIEWED_READ_TOOLS | {'sotto_future'})
    assert 'sotto_future' in mcp.authenticate(str(tmp_path), token)['tools']


def test_an_explicit_grant_keeps_exactly_its_tools(tmp_path, monkeypatch):
    token, _ = _token_and_row(tmp_path, ['sotto_person'])
    monkeypatch.setitem(mcp.TOOLS, 'sotto_future', mcp.read_tool('Future', 'A new read tool'))
    monkeypatch.setattr(catalog, 'REVIEWED_READ_TOOLS', catalog.REVIEWED_READ_TOOLS | {'sotto_future'})
    assert mcp.authenticate(str(tmp_path), token)['tools'] == ['sotto_person']


def test_read_all_never_includes_a_tool_that_is_not_read_only(tmp_path, monkeypatch):
    monkeypatch.setitem(mcp.TOOLS, 'sotto_write', mcp.tool('Write', 'A write tool', read_only=False))
    monkeypatch.setattr(catalog, 'REVIEWED_READ_TOOLS', catalog.REVIEWED_READ_TOOLS | {'sotto_write'})
    token, _ = _token_and_row(tmp_path, [mcp.READ_ALL])
    assert 'sotto_write' not in mcp.authenticate(str(tmp_path), token)['tools']


@pytest.mark.parametrize('names', [['read:some'], [mcp.READ_ALL, 'sotto_person'], ['sotto_unknown']])
def test_unknown_or_mixed_scopes_are_refused(tmp_path, names):
    with pytest.raises(ValueError):
        mcp.create_grant(str(tmp_path), 'Test client', names, 1)
    token, row = _token_and_row(tmp_path, ['sotto_person'])
    path = os.path.join(str(tmp_path), 'config', 'mcp-clients', row['client_id'] + '.json')
    with open(path, 'w') as stream:   # a hand-edited grant file is refused, not widened
        json.dump({**row, 'tools': names}, stream)
    assert mcp.authenticate(str(tmp_path), token) is None


def test_every_catalog_tool_is_read_only_and_reviewed_for_read_all():
    """Adding a tool fails here until it is added to REVIEWED_READ_TOOLS on purpose."""
    assert set(mcp.TOOLS) == catalog.REVIEWED_READ_TOOLS
    assert all(entry['annotations']['readOnlyHint'] is True for entry in mcp.TOOLS.values())


def test_a_tool_must_say_whether_it_is_read_only():
    with pytest.raises(TypeError):
        catalog.tool('Title', 'No declaration')  # pylint: disable=missing-kwoa
