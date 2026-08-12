"""
Tests for knowledge_edit.py — The Window M2 write CLI.

The invariant under test: a dashboard edit is indistinguishable from texting the correction.
`correct`/`add` ride knowledge_update.apply() (force_correction → SUPERSEDE in knowledge.py), so
these tests assert the exact chat-path outcomes — archived_text on the superseded fact, conf-1.0
user_edit provenance on the new one — plus the CLI's own guarantees (targeted fact never survives
a correction; archive flips status only; loop writes the resolver's terminal fields verbatim).
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime

import pytest
import yaml

import knowledge as kg
import knowledge_edit as ke

HERE = os.path.dirname(__file__)
CLI = os.path.join(HERE, "..", "_shared", "knowledge", "knowledge_edit.py")

NOW = datetime(2026, 8, 6, 7, 0, 0)
TODAY = "2026-08-06"
CID = "c_a1b2c3"


def _setup(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    os.makedirs(kg.people_dir(), exist_ok=True)
    p = kg.PersonFile(
        canonical_id=CID, name="Sarah Chen", company="Acme Corp", title="CTO",
        identifiers=["sarah@acme.com"], updated_at="2026-08-01T07:00:00Z",
        updated_by="brief_extraction",
        facts={
            "f_old": kg.FactMeta(text="CTO at Acme Corp", type="milestone", status="active",
                                 seen=3, conf=0.95, source="brief_extraction",
                                 first="2026-01-15", last="2026-08-01"),
            "f_other": kg.FactMeta(text="Enjoys hiking on weekends", type="interest",
                                   status="active", seen=2, conf=0.7, source="brief_extraction",
                                   first="2026-07-01", last="2026-08-01"),
        })
    with open(os.path.join(kg.people_dir(), f"{CID}.md"), "w", encoding="utf-8") as f:
        f.write(kg.serialize_person_file(p, NOW))
    return tmp_path


def _person():
    with open(os.path.join(kg.people_dir(), f"{CID}.md"), encoding="utf-8") as f:
        return kg.parse_person_file(f.read())


def _ledger(tmp_path, anchor, status="open"):
    d = os.path.join(str(tmp_path), "knowledge", "continuity")
    os.makedirs(d, exist_ok=True)
    fm = {"anchor_key": anchor, "action_type": "reply", "channel": "email",
          "contact_name": "Sarah Chen", "contact_identifier": "sarah@acme.com",
          "status": status, "created_at": "2026-08-01", "times_surfaced": 2,
          "summary": "Reply about the deck"}
    path = os.path.join(d, "loop_test.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\n{yaml.safe_dump(fm, sort_keys=False)}---\n")
    return path


# ── correct ───────────────────────────────────────────────────────────────────

def test_correct_supersedes_via_the_chat_path(tmp_path):
    """Similar correction text (>0.5 overlap) → force_correction SUPERSEDE, exactly as if the
    user had texted 'that's wrong'."""
    _setup(tmp_path)
    r = ke.op_correct(CID, "f_old", "No longer CTO at Acme, now advising startups", now=NOW)
    assert r == {"ok": True, "slug": CID, "fact_count": 2}
    p = _person()
    old = p.facts["f_old"]
    assert old.status == "archived" and old.archived_text == "CTO at Acme Corp"
    new = next(f for f in p.facts.values()
               if f.text == "No longer CTO at Acme, now advising startups")
    assert new.status == "active" and new.conf == 1.0 and new.source == "user_edit"
    assert new.type == "milestone"            # inherits the corrected fact's memory_type
    assert new.first == TODAY and new.last == TODAY
    assert p.facts["f_other"].status == "active"   # untouched


def test_correct_dissimilar_text_still_archives_the_target(tmp_path):
    """When the correction shares no words with the target (dedup ratio < 0.3, apply() files it
    as NEW), the CLI still archives the fact the user pointed at — it must not survive."""
    _setup(tmp_path)
    r = ke.op_correct(CID, "f_old", "Allergic to peanuts", now=NOW)
    assert r["ok"] is True
    p = _person()
    assert p.facts["f_old"].status == "archived"
    assert p.facts["f_old"].archived_text == "CTO at Acme Corp"
    new = next(f for f in p.facts.values() if f.text == "Allergic to peanuts")
    assert new.status == "active" and new.conf == 1.0 and new.source == "user_edit"


# ── archive ───────────────────────────────────────────────────────────────────

def test_archive_flips_status_only(tmp_path):
    _setup(tmp_path)
    r = ke.op_archive(CID, "f_old", now=NOW)
    assert r == {"ok": True, "slug": CID, "fact_count": 1}
    p = _person()
    assert p.facts["f_old"].status == "archived"
    assert p.facts["f_old"].archived_text == "CTO at Acme Corp"
    # everything else byte-stable in meaning: other fact, profile, updated_at untouched
    other = p.facts["f_other"]
    assert (other.status, other.seen, other.conf) == ("active", 2, 0.7)
    assert p.updated_at == "2026-08-01T07:00:00Z" and p.updated_by == "brief_extraction"
    assert p.name == "Sarah Chen" and p.title == "CTO"
    # idempotent — a second archive changes nothing and still reports ok
    assert ke.op_archive(CID, "f_old", now=NOW)["ok"] is True
    assert _person().facts["f_old"].status == "archived"


# ── add ───────────────────────────────────────────────────────────────────────

def test_add_creates_user_edit_fact(tmp_path):
    _setup(tmp_path)
    r = ke.op_add(CID, "Loves sailing regattas", "interest", now=NOW)
    assert r == {"ok": True, "slug": CID, "fact_count": 3}
    p = _person()
    new = next(f for f in p.facts.values() if f.text == "Loves sailing regattas")
    assert new.status == "active" and new.conf == 1.0
    assert new.source == "user_edit" and new.type == "interest"
    assert new.first == TODAY and new.last == TODAY


def test_add_duplicate_bumps_like_chat(tmp_path):
    """Adding a fact that already exists rides apply()'s dedup → BUMP, not a duplicate row."""
    _setup(tmp_path)
    r = ke.op_add(CID, "CTO at Acme Corp", "milestone", now=NOW)
    assert r["fact_count"] == 2                      # no new row
    old = _person().facts["f_old"]
    assert old.seen == 4 and old.conf == 1.0         # 0.95 + 0.1 capped


# ── loop ──────────────────────────────────────────────────────────────────────

def test_loop_resolve_writes_resolver_fields(tmp_path):
    _setup(tmp_path)
    path = _ledger(tmp_path, "email:reply:name:sarah chen")
    r = ke.op_loop("email:reply:name:sarah chen", "resolved", today=TODAY)
    assert r == {"ok": True, "anchor_key": "email:reply:name:sarah chen", "status": "resolved"}
    with open(path, encoding="utf-8") as f:
        content = f.read()
    fm = yaml.safe_load(content.split("---")[1])
    assert fm["status"] == "resolved"
    assert fm["resolution"] == "user_resolved"
    assert str(fm["resolved_at"])[:10] == TODAY
    # every other field survives the rewrite
    assert fm["anchor_key"] == "email:reply:name:sarah chen"
    assert fm["times_surfaced"] == 2 and fm["summary"] == "Reply about the deck"
    assert "_path" not in fm


def test_loop_dismiss_and_it_leaves_the_active_set(tmp_path):
    _setup(tmp_path)
    path = _ledger(tmp_path, "thread:xyz789")
    r = ke.op_loop("thread:xyz789", "dismissed", today=TODAY)
    assert r["status"] == "dismissed"
    with open(path, encoding="utf-8") as f:
        fm = yaml.safe_load(f.read().split("---")[1])
    assert fm["status"] == "dismissed" and fm["resolution"] == "user_dismissed"
    import ledger_io
    assert all(e.get("anchor_key") != "thread:xyz789" for e in ledger_io.load_active())


# ── validation ────────────────────────────────────────────────────────────────

def test_invalid_inputs_rejected(tmp_path):
    _setup(tmp_path)
    import pytest
    with pytest.raises(ke.EditError, match="invalid slug"):
        ke.op_archive("../etc/passwd", "f_old")
    with pytest.raises(ke.EditError, match="invalid slug"):
        ke.op_archive("Sarah Chen", "f_old")
    with pytest.raises(ke.EditError, match="invalid fact id"):
        ke.op_archive(CID, "f old!!")
    with pytest.raises(ke.EditError, match="fact not found"):
        ke.op_archive(CID, "f_zzz")
    with pytest.raises(ke.EditError, match="person not found"):
        ke.op_add("nobody-here", "some text")
    with pytest.raises(ke.EditError, match="empty text"):
        ke.op_add(CID, "   ")
    with pytest.raises(ke.EditError, match="too long"):
        ke.op_add(CID, "x" * 501)
    with pytest.raises(ke.EditError, match="invalid memory type"):
        ke.op_add(CID, "fine text", "Not A Type")
    with pytest.raises(ke.EditError, match="invalid anchor key"):
        ke.op_loop("bad\x00anchor", "resolved")
    with pytest.raises(ke.EditError, match="loop not found"):
        ke.op_loop("email:reply:name:nobody", "resolved")
    with pytest.raises(ke.EditError, match="invalid target status"):
        ke.op_loop("thread:abc", "closed")


# ── CLI contract (what the receiver's dashboard actually invokes) ─────────────

def test_cli_json_contract(tmp_path):
    _setup(tmp_path)
    env = {**os.environ, "SOTTO_DATA": str(tmp_path)}
    r = subprocess.run([sys.executable, CLI, f"--slug={CID}", "--op=archive", "--fact-id=f_old"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out == {"ok": True, "slug": CID, "fact_count": 1}
    # failure: JSON error on stdout + non-zero exit
    r = subprocess.run([sys.executable, CLI, "--slug=nobody", "--op=archive", "--fact-id=f_x"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 2
    out = json.loads(r.stdout)
    assert out["ok"] is False and "not found" in out["error"]


# ── merge (the confirmation half of entity-dedup lite) ───────────────────────
# The Learn step auto-merges ONLY on an exact shared identifier and merely SUGGESTS name-similarity
# merges. This op is how a human confirms one — and the one place a name-based merge can happen.

OTHER = "c_d4e5f6"


def _write_other(name="Ben Butler", identifiers=("ben@northstar.io",), facts=None):
    p = kg.PersonFile(canonical_id=OTHER, name=name, identifiers=list(identifiers),
                      updated_at="2026-08-02T07:00:00Z", updated_by="brief_extraction",
                      facts={fid: kg.FactMeta(text=t, type="context", status="active", seen=1,
                                              conf=0.8, first="2026-08-01", last="2026-08-01")
                             for fid, t in (facts or {"f_new": "Ben is closing the Series A"}).items()})
    path = os.path.join(kg.people_dir(), f"{OTHER}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(kg.serialize_person_file(p, NOW))
    return path


def test_merge_unions_the_files_and_deletes_the_source(tmp_path):
    _setup(tmp_path)
    src = _write_other(name="Sarah", identifiers=["+14155551234"],
                       facts={"f_new": "Sarah is running the offsite"})
    out = ke.op_merge(OTHER, CID, NOW)
    assert out == {"ok": True, "slug": CID, "fact_count": 3, "merged_from": OTHER}
    assert not os.path.exists(src)                       # the duplicate is gone, not orphaned
    p = _person()
    assert any(f.text == "Sarah is running the offsite" for f in p.facts.values())
    assert "sarah@acme.com" in p.identifiers and "+14155551234" in p.identifiers
    assert p.name == "Sarah Chen"                        # the surviving file keeps its identity


def test_merge_refuses_conflicting_identifiers(tmp_path):
    """Two John Smiths with two different emails: the merge the audit warned about, refused."""
    import pytest
    _setup(tmp_path)
    src = _write_other(name="Sarah Chen", identifiers=["sarah@globex.com"])
    with pytest.raises(ke.EditError, match="identifiers conflict"):
        ke.op_merge(OTHER, CID, NOW)
    assert os.path.exists(src)                           # nothing was written, nothing removed
    assert len(_person().facts) == 2


def test_merge_validates_both_slugs_and_refuses_a_self_merge(tmp_path):
    import pytest
    _setup(tmp_path)
    with pytest.raises(ke.EditError, match="person not found"):
        ke.op_merge("c_nosuch", CID, NOW)
    with pytest.raises(ke.EditError, match="person not found"):
        ke.op_merge(CID, "c_nosuch", NOW)
    with pytest.raises(ke.EditError, match="invalid slug"):
        ke.op_merge("../etc/passwd", CID, NOW)
    with pytest.raises(ke.EditError, match="into themselves"):
        ke.op_merge(CID, CID, NOW)


def test_merge_forgets_the_suggestion_it_confirmed(tmp_path):
    _setup(tmp_path)
    _write_other(name="Sarah", identifiers=["+14155551234"])
    path = os.path.join(str(tmp_path), "knowledge", "merge_suggestions.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"updated_at": "2026-08-06T07:00:00Z", "suggestions": [
            {"from": OTHER, "into": CID, "reason": "name containment: sarah ⊂ sarah-chen",
             "first_seen": "2026-08-05"},
            {"from": "c_x", "into": "c_y", "reason": "other pair", "first_seen": "2026-08-05"}]}, f)
    ke.op_merge(OTHER, CID, NOW)
    left = json.load(open(path, encoding="utf-8"))["suggestions"]
    assert [(s["from"], s["into"]) for s in left] == [("c_x", "c_y")]


def test_merge_cli_contract(tmp_path):
    _setup(tmp_path)
    _write_other(name="Sarah", identifiers=["+14155551234"])
    env = {**os.environ, "SOTTO_DATA": str(tmp_path)}
    r = subprocess.run([sys.executable, CLI, "--op=merge", f"--from={OTHER}", f"--into={CID}"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert json.loads(r.stdout)["merged_from"] == OTHER
    # …and the refusal still speaks JSON on stdout with a non-zero exit.
    _write_other(name="Sarah Chen", identifiers=["sarah@globex.com"])
    r2 = subprocess.run([sys.executable, CLI, "--op=merge", f"--from={OTHER}", f"--into={CID}"],
                        capture_output=True, text=True, env=env)
    assert r2.returncode == 2
    assert "identifiers conflict" in json.loads(r2.stdout)["error"]


# ── merge-dismiss ─────────────────────────────────────────────────────────────

def test_merge_dismiss_forgets_the_pair_without_touching_either_file(tmp_path):
    """"These really are two people" — the suggestion goes away, both files stay."""
    _setup(tmp_path)
    other = _write_other(name="Sarah", identifiers=["+14155551234"])
    path = os.path.join(str(tmp_path), "knowledge", "merge_suggestions.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"updated_at": "2026-08-06T07:00:00Z", "suggestions": [
            {"from": OTHER, "into": CID, "reason": "name containment"},
            {"from": "c_x", "into": "c_y", "reason": "other pair"}]}, f)
    assert ke.op_merge_dismiss(OTHER, CID, NOW) == {"ok": True, "dismissed": [OTHER, CID]}
    left = json.load(open(path, encoding="utf-8"))["suggestions"]
    assert [(s["from"], s["into"]) for s in left] == [("c_x", "c_y")]
    assert os.path.exists(other) and len(_person().facts) == 2


def test_merge_dismiss_validates_slugs(tmp_path):
    import pytest
    _setup(tmp_path)
    with pytest.raises(ke.EditError, match="invalid slug"):
        ke.op_merge_dismiss("../etc/passwd", CID, NOW)


# ── loop-add / loop-deadline (continuity_resolve's own write path) ────────────

def _loop_files(tmp_path):
    d = os.path.join(str(tmp_path), "knowledge", "continuity")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def _loop_fm(tmp_path, name):
    raw = open(os.path.join(str(tmp_path), "knowledge", "continuity", name),
               encoding="utf-8").read()
    return yaml.safe_load(raw.split("---", 2)[1])


def test_loop_add_writes_a_ledger_entry_in_the_resolver_s_shape(tmp_path):
    _setup(tmp_path)
    out = ke.op_loop_add("Send Sarah the revised deck", contact="Sarah Chen",
                         deadline="2026-08-12", now=NOW)
    assert out["ok"] is True and out["created"] is True
    assert out["anchor_key"].startswith("thread:manual:")
    files = _loop_files(tmp_path)
    assert len(files) == 1
    fm = _loop_fm(tmp_path, files[0])
    assert fm["status"] == "open" and fm["action_type"] == "follow_up"
    # channel "manual" is the whole rule: nothing in the inbox can auto-resolve a hand-added loop
    assert fm["channel"] == "manual" and fm["source"] == "user_added"
    assert fm["summary"] == "Send Sarah the revised deck"
    assert fm["contact_name"] == "Sarah Chen" and fm["deadline"] == "2026-08-12"
    assert fm["created_at"] == TODAY and fm["times_surfaced"] == 1


def test_loop_add_dedupes_the_same_ask_instead_of_forking_a_file(tmp_path):
    _setup(tmp_path)
    first = ke.op_loop_add("Send Sarah the deck", contact="Sarah Chen", now=NOW)
    again = ke.op_loop_add("Send Sarah the deck", contact="Sarah Chen",
                           deadline="2026-08-20", now=NOW)
    assert again["anchor_key"] == first["anchor_key"] and again["created"] is False
    assert len(_loop_files(tmp_path)) == 1
    fm = _loop_fm(tmp_path, _loop_files(tmp_path)[0])
    assert fm["times_surfaced"] == 2 and fm["deadline"] == "2026-08-20"
    # a DIFFERENT ask to the same person is its own loop
    other = ke.op_loop_add("Book the offsite room", contact="Sarah Chen", now=NOW)
    assert other["anchor_key"] != first["anchor_key"] and len(_loop_files(tmp_path)) == 2


def test_loop_add_never_resurrects_something_you_closed(tmp_path):
    import pytest
    _setup(tmp_path)
    out = ke.op_loop_add("Send Sarah the deck", contact="Sarah Chen", now=NOW)
    ke.op_loop(out["anchor_key"], "dismissed", today=TODAY)
    with pytest.raises(ke.EditError, match="already closed"):
        ke.op_loop_add("Send Sarah the deck", contact="Sarah Chen", now=NOW)


def test_loop_add_validates_its_inputs(tmp_path):
    import pytest
    _setup(tmp_path)
    with pytest.raises(ke.EditError, match="empty text"):
        ke.op_loop_add("   ", now=NOW)
    with pytest.raises(ke.EditError, match="YYYY-MM-DD"):
        ke.op_loop_add("do the thing", deadline="next tuesday", now=NOW)


def test_loop_deadline_sets_and_clears_the_one_field(tmp_path):
    """Snoozing a loop IS moving its deadline — the resolver expires two days past it, so a later
    date is the ledger's only "later"."""
    _setup(tmp_path)
    path = _ledger(tmp_path, "email:reply:sarah")
    assert ke.op_loop_deadline("email:reply:sarah", "2026-09-01") == {
        "ok": True, "anchor_key": "email:reply:sarah", "deadline": "2026-09-01"}
    fm = yaml.safe_load(open(path, encoding="utf-8").read().split("---", 2)[1])
    assert fm["deadline"] == "2026-09-01"
    assert fm["status"] == "open" and fm["summary"] == "Reply about the deck"   # nothing else moved
    ke.op_loop_deadline("email:reply:sarah", "")
    fm2 = yaml.safe_load(open(path, encoding="utf-8").read().split("---", 2)[1])
    assert "deadline" not in fm2


def test_loop_deadline_validates_and_reports_a_missing_loop(tmp_path):
    import pytest
    _setup(tmp_path)
    _ledger(tmp_path, "email:reply:sarah")
    with pytest.raises(ke.EditError, match="YYYY-MM-DD"):
        ke.op_loop_deadline("email:reply:sarah", "soon")
    with pytest.raises(ke.EditError, match="loop not found"):
        ke.op_loop_deadline("thread:nope", "2026-09-01")


def test_loop_cli_contract_for_the_new_ops(tmp_path):
    _setup(tmp_path)
    env = {**os.environ, "SOTTO_DATA": str(tmp_path)}
    r = subprocess.run([sys.executable, CLI, "--op=loop-add", "--text=Send the deck",
                        "--contact=Sarah Chen", "--deadline=2026-08-12"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    anchor = json.loads(r.stdout)["anchor_key"]
    r2 = subprocess.run([sys.executable, CLI, "--op=loop-deadline", f"--anchor={anchor}",
                        "--deadline=2026-09-01"], capture_output=True, text=True, env=env)
    assert r2.returncode == 0 and json.loads(r2.stdout)["deadline"] == "2026-09-01"
    r3 = subprocess.run([sys.executable, CLI, "--op=loop-add", "--text=x", "--deadline=nope"],
                        capture_output=True, text=True, env=env)
    assert r3.returncode == 2 and "YYYY-MM-DD" in json.loads(r3.stdout)["error"]


# ── relation-add / relation-remove ────────────────────────────────────────────
# "Vishnu didn't introduce us — correct it" is one CLI call, and it rides the SAME writer the
# Learn step uses, so a correction from chat and the ✕ on the dashboard leave the same bytes.

def test_relation_add_and_remove_round_trip(tmp_path):
    _setup(tmp_path)
    _write_other(name="Ben Butler")
    out = ke.op_relation(CID, OTHER, "introduced_by", True, date="2026-05-14", now=NOW)
    assert out == {"ok": True, "slug": CID, "relations": 1, "changed": 1}
    p, other = _person(), kg.parse_person_file(
        open(os.path.join(kg.people_dir(), f"{OTHER}.md"), encoding="utf-8").read())
    assert [(r.type, r.slug, r.source) for r in p.relations] == \
        [("introduced_by", OTHER, "user_edit")]
    assert [(r.type, r.slug) for r in other.relations] == [("introduced", CID)]
    assert p.relations[0].sentence() == "Introduced to you by Ben Butler (May 2026)"
    # …and the removal takes BOTH ends away again.
    out = ke.op_relation(CID, OTHER, "introduced_by", False, now=NOW)
    assert out == {"ok": True, "slug": CID, "relations": 0, "changed": 2}
    assert _person().relations == []
    assert kg.parse_person_file(
        open(os.path.join(kg.people_dir(), f"{OTHER}.md"), encoding="utf-8").read()).relations == []


def test_relation_remove_without_a_type_drops_whatever_edge_is_there(tmp_path):
    _setup(tmp_path)
    _write_other(name="Ben Butler")
    ke.op_relation(CID, OTHER, "works_with", True, now=NOW)
    assert ke.op_relation(CID, OTHER, "", False, now=NOW)["changed"] == 2
    assert _person().relations == []


def test_relation_refusals(tmp_path):
    import pytest
    _setup(tmp_path)
    _write_other(name="Ben Butler")
    with pytest.raises(ke.EditError, match="unknown relation type"):
        ke.op_relation(CID, OTHER, "nemesis_of", True, now=NOW)
    with pytest.raises(ke.EditError, match="two different people"):
        ke.op_relation(CID, CID, "works_with", True, now=NOW)
    with pytest.raises(ke.EditError, match="person not found"):
        ke.op_relation(CID, "c_nosuch", "works_with", True, now=NOW)
    with pytest.raises(ke.EditError, match="invalid slug"):
        ke.op_relation(CID, "../etc/passwd", "works_with", True, now=NOW)
    with pytest.raises(ke.EditError, match="date must be"):
        ke.op_relation(CID, OTHER, "works_with", True, date="last May", now=NOW)
    assert _person().relations == []


def test_relation_cli_contract(tmp_path):
    _setup(tmp_path)
    _write_other(name="Ben Butler")
    env = {**os.environ, "SOTTO_DATA": str(tmp_path)}
    r = subprocess.run([sys.executable, CLI, f"--slug={CID}", "--op=relation-add",
                        "--type=works_with", f"--other-slug={OTHER}"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and json.loads(r.stdout) == {
        "ok": True, "slug": CID, "relations": 1, "changed": 1}
    r2 = subprocess.run([sys.executable, CLI, f"--slug={CID}", "--op=relation-remove",
                         "--type=works_with", f"--other-slug={OTHER}"],
                        capture_output=True, text=True, env=env)
    assert r2.returncode == 0 and json.loads(r2.stdout)["relations"] == 0
    r3 = subprocess.run([sys.executable, CLI, f"--slug={CID}", "--op=relation-add",
                         "--type=nemesis_of", f"--other-slug={OTHER}"],
                        capture_output=True, text=True, env=env)
    assert r3.returncode == 2 and "unknown relation type" in json.loads(r3.stdout)["error"]


def test_re_adding_the_same_edge_is_a_quiet_no_op(tmp_path):
    _setup(tmp_path)
    _write_other(name="Ben Butler")
    ke.op_relation(CID, OTHER, "works_with", True, now=NOW)
    assert ke.op_relation(CID, OTHER, "works_with", True, now=NOW) == \
        {"ok": True, "slug": CID, "relations": 1, "changed": 0}


# ── add-identifier: the one identifier a human supplies ───────────────────────

OTHER_CID = "c_d4e5f6"


def _other_person(name="Dev Patel", identifiers=("dev@other.test",)):
    """A second, unrelated person — the stranger an incautious add would merge Sarah into."""
    p = kg.PersonFile(canonical_id=OTHER_CID, name=name, identifiers=list(identifiers),
                      updated_at="2026-08-01T07:00:00Z", updated_by="brief_extraction", facts={})
    with open(os.path.join(kg.people_dir(), f"{OTHER_CID}.md"), "w", encoding="utf-8") as f:
        f.write(kg.serialize_person_file(p, NOW))


def test_add_identifier_attaches_an_unsaved_number_to_a_known_person(tmp_path):
    """THE GAP: a text from a number in neither Contacts nor the graph reads as
    '+1 (310) 924-5269' in every brief, and nothing could say who it is. Now something can."""
    _setup(tmp_path)
    r = ke.op_add_identifier(CID, "+1 (310) 924-5269", now=NOW)
    assert r["ok"] is True and r["added"] is True
    assert "sarah@acme.com" in _person().identifiers          # the email is not disturbed
    assert any(kg.normalize_identifier(i) == "3109245269" for i in _person().identifiers)


def test_the_added_identifier_resolves_the_person_everywhere(tmp_path):
    """Attaching it is only worth doing if the resolver then names them — the same resolver that
    names a 1:1 thread, a group's label, and each sender line inside it."""
    _setup(tmp_path)
    ke.op_add_identifier(CID, "+1 (310) 924-5269", now=NOW)
    query = os.path.join(HERE, "..", "_shared", "knowledge", "knowledge_query.py")
    proc = subprocess.run([sys.executable, query, "--relevant-days", "7"],
                          capture_output=True, text=True,
                          env={**os.environ, "SOTTO_DATA": str(tmp_path)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    entry = next(e for e in json.loads(proc.stdout)["contact_index"]
                 if e["canonical_id"] == CID)
    assert any(kg.normalize_identifier(i) == "3109245269" for i in entry["identifiers"])

    import render_local as rl
    identity = rl.build_identity_resolver({"contacts": [], "contact_index": [entry]})
    assert identity("+13109245269")["name"] == "Sarah Chen"
    assert identity("(310) 924-5269")["name"] == "Sarah Chen"   # any format, one identity


def test_an_identifier_owned_by_someone_else_is_refused_not_moved(tmp_path):
    """The dangerous one. Two files sharing an identifier is exactly what auto_merge_by_identifier
    treats as proof they are one human — so a careless add here would silently merge two strangers
    on the next brief and take one of their histories with it."""
    _setup(tmp_path)
    _other_person(identifiers=["+13109245269"])
    with pytest.raises(ke.EditError) as e:
        ke.op_add_identifier(CID, "+1 (310) 924-5269", now=NOW)
    assert OTHER_CID in str(e.value) and "merge" in str(e.value)
    # …and nothing moved: both files are exactly as they were.
    assert not any(kg.normalize_identifier(i) == "3109245269" for i in _person().identifiers)
    with open(os.path.join(kg.people_dir(), f"{OTHER_CID}.md"), encoding="utf-8") as f:
        assert "3109245269" in "".join(re.sub(r"\D", "", i)
                                       for i in kg.parse_person_file(f.read()).identifiers)


def test_add_identifier_is_idempotent(tmp_path):
    _setup(tmp_path)
    ke.op_add_identifier(CID, "+1 (310) 924-5269", now=NOW)
    before = list(_person().identifiers)
    r = ke.op_add_identifier(CID, "(310) 924-5269", now=NOW)   # same human, different formatting
    assert r["added"] is False
    assert _person().identifiers == before


@pytest.mark.parametrize("bad", ["", "   ", "Haris", "the guy from dinner", "not-an@email",
                                 "555", "x" * 250])
def test_add_identifier_refuses_things_that_are_not_identifiers(tmp_path, bad):
    """A name is not an identifier. Rejected up front rather than stored and puzzled over later."""
    _setup(tmp_path)
    with pytest.raises(ke.EditError):
        ke.op_add_identifier(CID, bad, now=NOW)
    assert _person().identifiers == ["sarah@acme.com"]


def test_add_identifier_over_the_cli_is_the_same_path(tmp_path):
    """The dashboard forks this CLI; chat calls the same op. One writer, one result shape."""
    _setup(tmp_path)
    out = subprocess.run([sys.executable, CLI, "--slug", CID, "--op", "add-identifier",
                          "--identifier", "+1 (310) 924-5269"],
                         capture_output=True, text=True, env={**os.environ,
                                                              "SOTTO_DATA": str(tmp_path)})
    assert out.returncode == 0, out.stdout + out.stderr
    r = json.loads(out.stdout)
    assert r["ok"] is True and r["added"] is True and r["identifier"] == "+1 (310) 924-5269"


def test_add_identifier_cli_reports_a_refusal_as_json_not_a_crash(tmp_path):
    _setup(tmp_path)
    _other_person(identifiers=["+13109245269"])
    out = subprocess.run([sys.executable, CLI, "--slug", CID, "--op", "add-identifier",
                          "--identifier", "+13109245269"],
                         capture_output=True, text=True, env={**os.environ,
                                                              "SOTTO_DATA": str(tmp_path)})
    assert out.returncode == 2
    r = json.loads(out.stdout)
    assert r["ok"] is False and OTHER_CID in r["error"]
