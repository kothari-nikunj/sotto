"""Contact-field seed bytes remain compatible with existing graph and style identities."""
import hashlib

import knowledge
import render_local
import style_extract


def test_contact_seed_keeps_first_normalized_email_field_even_when_legacy_malformed(monkeypatch):
    monkeypatch.setattr(style_extract, '_adopt_graph_cids', lambda *args: None)
    name = 'Dana Example'
    for emails in ([' Legacy-Local-ID ', 'Dana@Example.Test'], [' Legacy-Local-ID '], []):
        contact = {'name': name, 'emails': emails, 'phones': ['+14155550123']}
        first = emails[0].strip().lower() if emails else None
        seed = f'kf:{name}|{first}' if first else f'kf:{name}'
        expected = 'c_' + hashlib.sha256(seed.encode()).hexdigest()[:12]
        assert render_local.seed_contact_index_from_contacts([contact])[0]['canonical_id'] == expected
        index, _ = style_extract._seed_contact_index([contact])
        assert index[0]['canonical_id'] == expected
    # The graph's identifier selector is intentionally different from a contact email-field array.
    assert knowledge.default_canonical_id(name, ['legacy-local-id', 'Dana@Example.Test']) == (
        'c_' + hashlib.sha256(f'kf:{name}|dana@example.test'.encode()).hexdigest()[:12])
