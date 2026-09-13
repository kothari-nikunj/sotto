"""granola_graph.py — the meetings you sat in become dated, sourced facts on the people you sat with."""
import json
import os

import granola_graph as gg
import knowledge as kg


def _meeting(emails, title="Harbor term sheet", date="2026-06-25", mid="m1", summary=None):
    m = {"meeting_id": mid, "title": title, "date": date, "attendee_emails": list(emails)}
    if summary:
        m["ai_summary"] = summary
    return m


def _person(email, name=""):
    path = kg.find_person_file(name=name, identifier=email)
    assert path and os.path.exists(path), f"no person file for {email}"
    with open(path, encoding="utf-8") as f:
        return kg.parse_person_file(f.read())


def test_small_meeting_writes_stub_fact_and_company(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@mine.com")
    out = gg.run({"meetings": [_meeting(
        ["priya@acmecorp.com", "sarah.chen@acmecorp.com", "bob@northstar.io"],
        summary="Priya wants the redline back by Friday. Then a second sentence.")]})
    assert out == {"meetings": 1, "people": 3, "facts": 3, "skipped_large": 0}

    priya = _person("priya@acmecorp.com")
    assert priya.name == "Priya"
    assert "priya@acmecorp.com" in priya.identifiers
    assert priya.company == "Acmecorp"             # dumb domain inference, capitalized
    assert priya.updated_by == "granola"
    facts = [f for _fid, f in kg.sorted_active_facts(priya.facts)]
    assert len(facts) == 1
    assert facts[0].text == ("Met on 2026-06-25: Harbor term sheet — "
                             "Priya wants the redline back by Friday.")   # ONE clause of the summary
    assert facts[0].type == "milestone" and facts[0].conf == 0.7
    assert facts[0].source == "granola" and facts[0].source_ref == "m1"
    assert len(facts[0].text) <= gg.MAX_FACT_CHARS

    assert _person("sarah.chen@acmecorp.com").name == "Sarah Chen"   # derived from the local part
    assert _person("bob@northstar.io").company == "Northstar"


def test_large_meeting_is_not_a_relationship(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@mine.com")
    webinar = _meeting([f"guest{i}@acmecorp.com" for i in range(20)], title="Q3 all-hands webinar")
    out = gg.run({"meetings": [webinar]})
    assert out["facts"] == 0 and out["skipped_large"] == 1
    assert kg.find_person_file(identifier="guest0@acmecorp.com") is None


def test_owner_and_automated_addresses_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@mine.com")
    out = gg.run({"meetings": [_meeting(
        ["me@mine.com", "noreply@calendar.acmecorp.com", "priya@acmecorp.com"])]})
    assert out["facts"] == 1
    assert kg.find_person_file(identifier="me@mine.com") is None
    assert kg.find_person_file(identifier="noreply@calendar.acmecorp.com") is None


def test_freemail_domain_gets_no_company_patch(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@mine.com")
    gg.run({"meetings": [_meeting(["dhruv@yahoo.com", "priya@acmecorp.com"])]})
    assert not _person("dhruv@yahoo.com").company     # a freemail domain says nothing about work
    assert _person("priya@acmecorp.com").company == "Acmecorp"


def test_existing_company_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@mine.com")
    import knowledge_update as ku
    ku.apply({"person_updates": [{"person_name": "Priya Patel", "identifier": "priya@acmecorp.com",
                                  "profile_patch": {"company": "Acme Corporation"}}]})
    gg.run({"meetings": [_meeting(["priya@acmecorp.com"])]})
    p = _person("priya@acmecorp.com")
    assert p.company == "Acme Corporation"             # the file knew better than the domain guess
    assert p.name == "Priya Patel"                     # and the real name survives the derived one


def test_same_meeting_replay_does_not_strengthen_or_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@mine.com")
    payload = {"meetings": [_meeting(["priya@acmecorp.com"], summary="Redline by Friday.")]}
    gg.run(payload)
    gg.run(payload)
    facts = [f for _fid, f in kg.sorted_active_facts(_person("priya@acmecorp.com").facts)]
    assert len(facts) == 1                             # same source reference is not independent confirmation
    assert facts[0].seen == 1


def test_untitled_and_idless_meeting_still_carries_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@mine.com")
    m = {"title": "Coffee", "date": "2026-06-25", "attendee_emails": ["priya@acmecorp.com"]}
    gg.run({"meetings": [m]})
    fact = next(f for _fid, f in kg.sorted_active_facts(_person("priya@acmecorp.com").facts))
    assert fact.source_ref == "Coffee:2026-06-25"      # title:date when Granola gave no id


def test_missing_file_is_a_silent_no_op(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["granola_graph.py", "--granola", str(tmp_path / "nope.json")])
    gg.main()
    assert json.loads(capsys.readouterr().out)["facts"] == 0


def test_main_reads_the_gathered_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "me@mine.com")
    src = tmp_path / "granola.json"
    src.write_text(json.dumps({"meetings": [_meeting(["priya@acmecorp.com"])]}))
    monkeypatch.setattr("sys.argv", ["granola_graph.py", "--granola", str(src)])
    gg.main()
    assert json.loads(capsys.readouterr().out) == {
        "meetings": 1, "people": 1, "facts": 1, "skipped_large": 0}
