"""Optional identity corruption cannot withhold a meeting prep."""
from datetime import datetime, timezone

def test_malformed_optional_name_data_does_not_drop_prep(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    import knowledge as kg
    path = tmp_path / 'corrupt.md'
    path.write_text('---\nname: [unterminated\n---\n')
    monkeypatch.setattr(kg, 'find_person_file', lambda **kw: str(path))
    from prep_identity import PrepIdentityResolver
    assert PrepIdentityResolver({}).resolve({'email': 'person@acme.example'}, datetime.now(timezone.utc)) == ('', '')



def test_name_source_is_explicit_and_current_permission_is_rechecked(tmp_path, monkeypatch):
    from prep_identity import PrepIdentityResolver
    import personal_context
    import preferences
    import source_context
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    grants = {'gmail'}
    monkeypatch.setattr(source_context, 'allowed', lambda source: source in grants)
    monkeypatch.setattr(preferences, 'load_explicit', lambda: {})
    monkeypatch.setattr(personal_context, 'conversation_snapshot', lambda: [
        ({'source': 'email', 'from': 'Dana Example <dana@acme.example>'}, '', '')])
    attendee = {'email': 'dana@acme.example'}
    resolver = PrepIdentityResolver({})
    now = datetime.now(timezone.utc)
    assert resolver.resolve(attendee, now) == ('Dana Example', 'gmail')
    assert attendee == {'email': 'dana@acme.example'}
    grants.clear()
    assert resolver.resolve(attendee, now) == ('', '')
