"""Shared source envelopes preserve Google address variants and optional CLI input defaults."""
from jsonfiles import load, pick_list
from textutil import first_nonempty, format_address


def test_google_address_envelope_preserves_order_and_aliases():
    assert format_address(['Owner <owner@example.com>', {'name': 'Dana', 'address': 'dana@acme.example'},
                           None, [{'email': 'team@acme.example'}]]) == (
        'Owner <owner@example.com>, Dana <dana@acme.example>, <team@acme.example>')
    assert first_nonempty({'missing': None, 'zero': 0, 'later': 'later'}, 'missing', 'zero', 'later') == 0
    assert first_nonempty({'off': False, 'later': 'later'}, 'off', 'later') is False


def test_optional_cli_json_defaults_do_not_coerce_wrong_envelopes(tmp_path):
    bad = tmp_path / 'bad.json'
    bad.write_text('{bad')
    assert load(bad, {'fallback': True}) == {'fallback': True}
    assert pick_list({'items': 'wrong', 'events': [{'id': 'event'}]}, 'items', 'events') == [{'id': 'event'}]
    assert pick_list({'items': 'wrong'}, 'items') == []
