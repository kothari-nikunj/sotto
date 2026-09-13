import json
import pytest
from google_cli import decode_output


def test_empty_search_sentinel_is_not_a_failed_source():
    assert decode_output('No messages found.\n', ['gmail', 'search', 'newer_than:1h']) == []
    assert decode_output('[{"id":"one"}]', ['gmail', 'search']) == [{'id': 'one'}]


@pytest.mark.parametrize('text,args', [('Authentication required', ['gmail', 'search']),
                                     ('No messages found.', ['gmail', 'get'])])
def test_unexpected_output_remains_an_error(text, args):
    with pytest.raises(json.JSONDecodeError):
        decode_output(text, args)
