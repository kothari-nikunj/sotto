"""Graph fact hygiene: a record shows each assertion once, only about its own person.
Losing a true fact is worse than a duplicate, so every rule here is narrow."""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import fact_hygiene as fh
import knowledge as kg
import knowledge_query as kq
import knowledge_update as ku

NOW = datetime(2026, 11, 9, 9, 0, 0)
QUERY = os.path.join(os.path.dirname(__file__), "..", "_shared", "knowledge", "knowledge_query.py")


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_USER_EMAIL", "owner@fund.example")
    fh._people_cache.clear()


def _person(name, ident, text="Known contact.", ref="gmail:seed", source="observed_message"):
    return {"person_updates": [{"person_name": name, "identifier": ident,
            "facts": [{"fact": text, "memory_type": "context", "confidence": 0.65, "source": source,
                       "source_ref": ref, "evidence_refs": [ref], "observed_date": "2026-11-01"}]}]}


def _dana_fact(text, ref, source="observed_message"):
    return _person("Dana Roe", "dana@acme.example", text, ref, source)


def _dana():
    return kg.parse_person_file(open(kg.find_person_file(identifier="dana@acme.example")).read())


@pytest.mark.parametrize("a,b", [
    ("Dana works with Acme Ventures.", "Dana works with Beta Ventures."),
    ("Dana is the CEO of Acme Robotics.", "Dana is the CFO of Acme Robotics."),
    ("Acme raised a $10M Series A led by Northwind", "Acme raised a $10M Series B led by Northwind"),
    ("Term sheet signed via DocuSign on Monday.", "Term sheet scheduled to be signed via DocuSign on Monday."),
    ("Term sheet signed via DocuSign on Monday.", "Term sheet unsigned via DocuSign on Monday."),
    ("Dana didn't sign the term sheet with Acme.", "Dana signed the term sheet with Acme."),
    ("Dana declined the Acme Ventures offer.", "Dana accepted the Acme Ventures offer."),
    ("Dana lives in Boston with her family.", "Dana lives in Austin with her family."),
    ("Burn is $250k/month at Acme.", "Burn is $250k/year at Acme."),
    ("Dana is leading the Acme seed round.", "Dana passed on leading the Acme seed round."),
    ("Term sheet sent via DocuSign dated for Monday with agreed preferred voting threshold clause.",
     "Term sheet sent via DocuSign dated for Monday while finalizing majority preferred voting thresholds."),
    ("Dana is associated with Acme Ventures.", "Dana is an investor associated with Acme Ventures."),
    ("Dana is associated with Acme Ventures.", "Dana Roe is associated with Acme Ventures (dana@acme.example)."),
    ("Works with Dana", "Works with Dana and Lee"),
    ("Invested in Acme", "Invested in Acme (exited)"),
    ("CEO of Acme", "CEO of Acme (former)"),
    ("Sam reports to Lee", "Lee reports to Sam Smith"),
    ("Introduced Sam to Lee", "Lee introduced Sam to Kim"),
    ("Is a Stanford alum", "Is NOT a Stanford alum"),
    ("Raising a seed round", "Raising a seed round (paused)"),
    ("Reports to Jane", "Reports to Jane Smith's successor"),
    ("Board seat at Acme", "Board seat at Acme (declined)"),
    ("Joined Acme in 2024", "Joined Acme in 2024 (not 2023)"),
])
def test_distinct_claims_are_never_merged(a, b):
    assert not fh.same_assertion(a, b) and not fh.same_assertion(b, a)
    assert not fh.same_assertion(a, b, "Dana Roe") and not fh.same_assertion(b, a, "Dana Roe")


@pytest.mark.parametrize("a,b", [
    ("Raised a Series A", "Raised a Series"),                # a capital mid-sentence "A" is a name
    ("Backup is Plan A at Acme", "Backup is Plan at Acme"),
    ("Partner at Acme & Co.", "Partner at Beta & Co."),
    ("Sam Lee is CEO of Acme", "Is CEO of Acme"),             # only this person's own name is dropped
])
def test_normalizing_never_merges_different_claims(a, b):
    assert not fh.same_assertion(a, b, "Dana Roe")


@pytest.mark.parametrize("a,b", [
    ("Acme is leading Loopco's Series A round.", "acme is leading  Loopco’s Series A round"),
    ("Dana is associated with Acme Ventures.", "DANA is associated with Acme Ventures!"),
    ("Partner at Northstar & Co.", "Partner at Northstar and Co."),
    ("Partner at Northstar and Co.", "Partner at the Northstar and Co."),
    ("The CEO of Acme Robotics.", "CEO of Acme Robotics"),
    ("A founder of an AI lab.", "Founder of AI lab"),
])
def test_only_exact_copies_merge(a, b):
    assert fh.same_assertion(a, b) and fh.same_assertion(b, a)


@pytest.mark.parametrize("a,b", [
    ("Dana Roe is CEO of Acme.", "Is CEO of Acme"),
    ("Dana is CEO of Acme.", "Dana Roe is CEO of Acme"),
])
def test_a_leading_own_name_is_the_same_assertion(a, b):
    assert fh.same_assertion(a, b, "Dana Roe") and not fh.same_assertion(a, b)


def test_the_writer_bumps_a_near_copy_instead_of_adding_it():
    ku.apply(_dana_fact("Partner at Northstar & Co.", "gmail:a"), NOW)
    ku.apply(_dana_fact("Partner at the Northstar and Co.", "gmail:b"), NOW)
    ku.apply(_dana_fact("Dana Roe is CEO of Acme.", "gmail:c"), NOW)
    ku.apply(_dana_fact("Is CEO of Acme", "gmail:d"), NOW)
    assert [f.text for f in _dana().facts.values()] == ["Partner at Northstar & Co.", "Dana Roe is CEO of Acme."]


@pytest.mark.parametrize("text", [
    "Kira serves on the Delta Labs board.",
    "Kira, MD, founded Delta Labs.",
])
def test_a_fact_leading_with_another_persons_first_name_is_rejected_and_hidden(text):
    ku.apply(_person("Kira Holt", "kira@example.net"), NOW)
    fh._people_cache.clear()
    result = ku.apply(_dana_fact(text, "gmail:b"), NOW)
    assert any(o.get("reason") == "about_someone_else" for o in result["fact_outcomes"]["items"])
    path = kg.find_person_file(identifier="dana@acme.example")
    p = kg.parse_person_file(open(path).read())
    p.facts["f_legacy"] = kg.FactMeta(text=text, type="context", source="observed_message",
                                      source_ref="gmail:c", first="2026-11-01", last="2026-11-01")
    kg.write_person_file(path, p, NOW)
    assert "Delta Labs" not in kq.pack_person(_dana(), True, NOW)
    assert "f_legacy" in _dana().facts                       # hidden, never deleted


@pytest.mark.parametrize("name,text", [
    ("Will Turner", "Will join the Acme board in 2027."),    # a first name that is a common word
    ("Mark Lowe", "Mark the Acme renewal as urgent."),
    ("Kira Holt", "Kira introduced her to Delta Labs."),     # a pronoun refers back to this person
    ("Kira Holt", "Kira and Dana cofounded Delta Labs."),    # this person's own name appears
    ("Kira Holt", "Mira serves on the Delta Labs board."),   # an unknown first name is never judged
    ("Dana Holt", "Dana serves on the Delta Labs board."),   # this person's own first name
])
def test_a_one_word_lead_is_kept_unless_it_names_someone_else(name, text):
    ku.apply(_person(name, "other@example.net"), NOW)
    assert fh.unreadable(text, "Dana Roe") is None


def test_the_owners_first_name_is_never_another_person():
    ku.apply(_person("Owen Park", "owner@fund.example"), NOW)
    assert fh.unreadable("Owen is leading the Acme round.", "Dana Roe") is None


@pytest.mark.parametrize("text", [
    "Offered Tue 10:00, got 10:15 instead.",
    "Call moved to Nov 14 at 3pm.",
    "Meeting on Thursday at 2:30 pm with Acme.",
    "Rescheduled the Acme sync to 4:30pm.",
    "Countered with 11am for the founder call.",
    "Pitch set for 2026-11-20 at 09:00.",
    "Offered Monday or Wednesday at 4pm.",
    "Office hours Monday and Wednesday next week at 4pm.",
    "Pitch set for 24/7/2026 at 09:00.",
])
def test_a_one_off_time_arrangement_is_rejected_and_hidden(text):
    result = ku.apply(_dana_fact(text, "gmail:b"), NOW)
    assert any(o.get("reason") == "one_off_arrangement" for o in result["fact_outcomes"]["items"])
    assert fh.unreadable(text, "Dana Roe") == "one_off_arrangement"


@pytest.mark.parametrize("text,kind", [
    ("Meets founders Tuesdays at 10:00.", "context"),
    ("Runs a standup every Monday at 9am.", "context"),
    ("Prefers Monday 9am calls.", "preference"),
    ("Closed the Acme round on Tuesday.", "context"),
    ("Birthday is May 3.", "context"),
    ("Offered a board seat at Acme.", "context"),
    ("Burn is $250k/month at Acme.", "context"),
])
def test_recurring_times_preferences_and_plain_dates_are_kept(text, kind):
    assert fh.unreadable(text, "Dana Roe", kind) is None


def test_identical_saved_copies_show_once_and_every_other_wording_stays():
    ku.apply(_dana_fact("Dana is associated with Acme Ventures.", "gmail:0"), NOW)
    path = kg.find_person_file(identifier="dana@acme.example")
    p = kg.parse_person_file(open(path).read())
    for i in range(5):
        p.facts[f"f_copy{i}"] = kg.FactMeta(text="Dana is associated with Acme Ventures.", type="context",
                                            source="observed_message", source_ref=f"gmail:c{i}",
                                            first=f"2026-10-0{i + 1}", last=f"2026-10-0{i + 1}")
    p.facts["f_fuller"] = kg.FactMeta(text="Dana Roe is associated with Acme Ventures (dana@acme.example).",
                                      type="context", source="observed_message", source_ref="gmail:f",
                                      first="2026-10-01", last="2026-10-01")
    kg.write_person_file(path, p, NOW)
    packed = kq.pack_person(_dana(), True, NOW)
    assert packed.count("Dana is associated with Acme Ventures") == 1
    assert "Dana Roe is associated with Acme Ventures (dana@acme.example)" in packed
    shown = [fid for fid, _ in fh.readable_facts(list(_dana().facts.items()), _dana())]
    assert "f_copy4" not in shown or "f_copy0" not in shown
    editable = [f["text"] for f in kq.editable_person(_dana(), NOW)["facts"]]
    assert editable.count("Dana is associated with Acme Ventures.") == 1   # visible == correctable
    assert sorted(editable) == ["Dana Roe is associated with Acme Ventures (dana@acme.example).",
                                "Dana is associated with Acme Ventures."]


def test_the_newest_copy_is_shown():
    ranked = [("old", kg.FactMeta(text="Raising a seed round.", source="observed_message", last="2026-01-01")),
              ("new", kg.FactMeta(text="raising a seed round", source="observed_message", last="2026-11-05"))]
    assert [fid for fid, _ in fh.readable_facts(ranked, "Dana Roe")] == ["new"]


def test_a_qualified_fact_is_never_hidden_by_a_plainer_one():
    for ref, text in (("a", "Invested in Acme"), ("b", "Invested in Acme (Series A)"),
                      ("c", "Invested in Acme (exited 2023)")):
        ku.apply(_dana_fact(text, f"gmail:{ref}"), NOW)
    p = _dana()
    shown = {f.text for _, f in fh.readable_facts(list(p.facts.items()), p)}
    assert shown == {"Invested in Acme", "Invested in Acme (Series A)", "Invested in Acme (exited 2023)"}
    assert {f["text"] for f in kq.editable_person(p, NOW)["facts"]} == shown


def test_grouping_does_not_depend_on_order():
    texts = ["Dana is associated with Acme Ventures.", "dana is associated with acme ventures",
             "Dana is an investor at Acme Ventures."]
    for order in ([0, 1, 2], [2, 0, 1], [1, 2, 0]):
        ranked = [(f"f{i}", kg.FactMeta(text=texts[i], source="observed_message", last=f"2026-11-0{i + 1}"))
                  for i in order]
        assert sorted(fid for fid, _ in fh.readable_facts(ranked, "Dana Roe")) == ["f1", "f2"]


def test_a_fact_leading_with_another_known_person_is_rejected_and_hidden():
    ku.apply(_person("Sam Lee", "sam@blockworks.example"), NOW)
    ku.apply(_dana_fact("Dana Roe works with Acme Ventures.", "gmail:a"), NOW)
    fh._people_cache.clear()
    result = ku.apply(_dana_fact("Sam Lee is the CEO of Blockworks.", "gmail:b"), NOW)
    assert [f.text for f in _dana().facts.values()] == ["Dana Roe works with Acme Ventures."]
    assert any(o.get("reason") == "about_someone_else" for o in result["fact_outcomes"]["items"])
    path = kg.find_person_file(identifier="dana@acme.example")
    p = kg.parse_person_file(open(path).read())
    p.facts["f_legacy"] = kg.FactMeta(text="Sam Lee, MD, founded Blockworks.", type="context",
                                      source="observed_message", source_ref="gmail:c",
                                      first="2026-11-01", last="2026-11-01")
    kg.write_person_file(path, p, NOW)
    packed = kq.pack_person(_dana(), True, NOW)
    assert "Blockworks" not in packed and "Acme Ventures" in packed


@pytest.mark.parametrize("text", [
    "Stanford GSB, Class of 2015.",
    "Harvard Business School, MBA 2014.",
    "Northwind Labs is her current company.",
    "Acme Ventures has $200M AUM.",
    "New York is home base.",
    "Jane Doe, executive assistant, handles scheduling.",   # an unknown name is never judged
    "Founded Analytical Engines in 1843.",
    "Sam Lee introduced Dana to the Acme team.",             # names this person
    "Sam Lee is her husband",                                # a pronoun refers back to this person
    "Sam Lee introduced her to FPV",
    "Sam Lee and she cofounded Acme",
])
def test_facts_about_the_subject_or_unknown_names_are_kept(text):
    ku.apply(_person("Sam Lee", "sam@blockworks.example"), NOW)
    assert not fh.names_someone_else(text, "Dana Roe")


def test_the_owner_is_never_another_person():
    ku.apply(_person("Owen Park", "owner@fund.example"), NOW)
    assert not fh.names_someone_else("Owen Park is leading the Acme round.", "Dana Roe")


@pytest.mark.parametrize("source", ["user_edit", "apple_contacts"])
def test_owner_typed_facts_are_never_judged(source):
    ku.apply(_person("Sam Lee", "sam@blockworks.example"), NOW)
    ku.apply(_dana_fact("Sam Lee is Dana's cofounder at Acme.", "owner", source), NOW)
    ku.apply(_dana_fact("Sam Lee is the CEO of Blockworks.", "owner-2", source), NOW)
    assert [f.source for f in _dana().facts.values()] == [source, source]


def test_company_context_keeps_distinct_lines_blank_lines_and_headings(tmp_path):
    lines = ["Term sheet sent via DocuSign dated for Monday with agreed preferred voting threshold clause.",
             "Term sheet sent via DocuSign dated for Monday while finalizing majority preferred voting thresholds."]
    ku.apply({"company_updates": [{"company_name": "Loopco", "context_updates": [lines[0]]}]}, NOW)
    ku.apply({"company_updates": [{"company_name": "Loopco", "context_updates": [lines[1], lines[1]]}]}, NOW)
    stored = open(os.path.join(kg.companies_dir(), "loopco.md")).read()
    assert stored.count(lines[1]) == 1 and lines[0] in stored       # exact repeat skipped; history kept
    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    out = subprocess.run([sys.executable, QUERY, "--company", "Loopco", "--company-page", "first"],
                         capture_output=True, text=True, env=env, check=True).stdout
    shown = json.loads(out)["company_knowledge"]["loopco"]
    assert lines[0] in shown and lines[1] in shown                  # different claims both stay
    assert fh.distinct_context("A b c d.\n\n## Notes\nA b c d\n\n## Notes") == "\n## Notes\nA b c d\n\n## Notes"  # the newer copy wins a tie


def test_company_news_shows_one_copy_of_a_repeated_line():
    news = ["Acme raised a $10M Series A led by Northwind", "acme raised a $10M Series A led by Northwind."]
    entry = kq._company_entry({"name": "Acme", "news": news})
    assert entry.count("Series A") == 1 and news[0] in entry


DANA = "c_111111111111"
SAM = "c_222222222222"


def _saved_fact(text, last):
    return kg.FactMeta(text=text, source="observed_message", source_ref="gmail:fixture",
                       type="context", conf=0.9, first=last, last=last)


def _write_fixture(cid, name, facts=None, relations=None):
    path = Path(kg.people_dir()) / f"{cid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    p = kg.PersonFile(canonical_id=cid, name=name, identifiers=[f"{name.split()[0].lower()}@example.test"],
                      facts=facts or {}, relations=relations or [])
    kg.write_person_file(str(path), p, NOW)
    return path


def _read_fixture(path):
    return kg.parse_person_file(path.read_text())


def _record_conflict(tmp_path, cid):
    (tmp_path / "knowledge" / "conflicts.json").write_text(json.dumps({
        "people": {cid: {"pairs": [["f_a", "f_b"]]}}}))


def test_disputed_copy_does_not_escape_into_related_context(tmp_path):
    primary = _write_fixture(DANA, "Dana Roe", relations=[
        kg.Relation(type="works_with", slug=SAM, name="Sam Lee")])
    sam = _write_fixture(SAM, "Sam Lee", {
        "f_a": _saved_fact("Clinic procurement is approved.", "2026-11-01"),
        "f_b": _saved_fact("Clinic procurement is not approved.", "2026-11-01"),
        "f_duplicate": _saved_fact("clinic procurement is approved", "2026-11-02"),
    })
    _record_conflict(tmp_path, SAM)
    packed = kq.pack_person(_read_fixture(primary), True, NOW, "clinic procurement")
    assert "procurement is approved" not in packed.casefold()
    direct = kq.pack_person(_read_fixture(sam), True, NOW, "clinic procurement")
    assert "Unresolved memory conflict" in direct
    assert direct.casefold().count("procurement is approved") == 1


def test_wrong_person_cannot_reappear_as_conflict(tmp_path):
    _write_fixture(SAM, "Sam Lee")
    primary = _write_fixture(DANA, "Dana Roe", {
        "f_a": _saved_fact("Sam Lee is the CEO of Blockworks.", "2026-11-01"),
        "f_b": _saved_fact("Runs Acme Ventures.", "2026-11-01"),
    })
    _record_conflict(tmp_path, DANA)
    packed = kq.pack_person(_read_fixture(primary), True, NOW)
    assert "Blockworks" not in packed
    assert "Runs Acme Ventures" in packed


@pytest.mark.parametrize("text", ["Dana Roe prefers Monday 9am calls", "Prefers Monday 9am calls."])
def test_visible_preference_does_not_inherit_hidden_legacy_type(text):
    legacy = "Prefers Monday 9am calls."
    ku.apply(_dana_fact("Works with Acme.", "gmail:seed"), NOW)
    path = kg.find_person_file(identifier="dana@acme.example")
    p = _dana()
    legacy_id = kg.generate_fact_id(p.canonical_id, legacy, "2026-11-01")
    p.facts[legacy_id] = _saved_fact(legacy, "2026-11-01")
    kg.write_person_file(path, p, NOW)

    proposal = _dana_fact(text, "gmail:preference")
    proposal["person_updates"][0]["facts"][0]["memory_type"] = "preference"
    result = ku.apply(proposal, NOW)
    assert result["fact_outcomes"]["items"][0]["disposition"] == "added"
    updated = _dana()
    assert updated.facts[legacy_id].type == "context"
    visible = fh.readable_facts(list(updated.facts.items()), updated)
    assert [(fact.text, fact.source_ref) for _, fact in visible if fact.type == "preference"] == [
        (text, "gmail:preference")
    ]
    assert {fact.source_ref for fact in updated.facts.values()} == {
        "gmail:seed", "gmail:fixture", "gmail:preference"
    }
    assert ku.apply(proposal, NOW)["fact_outcomes"]["items"][0]["disposition"] == "duplicate"
    assert _dana().facts.keys() == updated.facts.keys()


@pytest.mark.parametrize("name,text", [
    ("Dean Thomas", "Dean of Engineering at Stanford."),
    ("Angel Ruiz", "Angel investor in Delta Labs."),
    ("Georgia Smith", "Georgia Tech alum and advisor."),
    ("Austin Shah", "Austin office lead for Delta Labs."),
    ("", "Hosts a weekly founder office hour on Monday at 9am."),
    ("", "Office hours Monday and Wednesday at 4pm."),
    ("", "Available Mon–Fri, 9am–5pm."),
    ("", "Offers founder office hours at 4pm."),
    ("", "Reachable 24/7; standup at 9am."),
])
def test_durable_roles_and_availability_survive_write_and_read(name, text):
    if name:
        ku.apply(_person(name, "contact@other.example"), NOW)
    result = ku.apply(_dana_fact(text, "gmail:b"), NOW)
    assert result["fact_outcomes"]["items"][0]["disposition"] == "added"
    assert text in kq.pack_person(_dana(), True, NOW)
