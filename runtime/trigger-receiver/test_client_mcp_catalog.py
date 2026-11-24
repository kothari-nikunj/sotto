"""The client tool list carries directory-ready metadata and stays within a token budget."""
import json

import client_mcp as mcp

# tools/list for every tool was 13,947 bytes before titles, hints and field descriptions.
TOOL_LIST_BYTES = 19_000
HINTS = {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}


def test_every_tool_has_a_title_explicit_hints_and_described_fields():
    for name, entry in mcp.TOOLS.items():
        assert entry['title'].strip() and entry['title'] != name
        assert entry['annotations'] == HINTS
        for field, schema in entry['inputSchema']['properties'].items():
            assert schema['description'].strip(), (name, field)


def test_the_full_tool_list_stays_within_its_size_budget():
    listed = mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                          {'tools': sorted(mcp.TOOLS)}, None)['result']
    assert len(listed['tools']) == len(mcp.TOOLS)
    assert len(json.dumps(listed).encode()) <= TOOL_LIST_BYTES
