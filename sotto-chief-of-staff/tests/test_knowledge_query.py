"""knowledge_query.py — the READ side of the graph.

Two things it is on the hook for, both of which were silently missing:
  1. `company_knowledge` — render_local has always had a "### Company Context" block and the
     companies/*.md files have always been written; nothing ever packed them.
  2. WHO packs — today's inputs (email + calendar), with the file-mtime window demoted to the
     fallback cohort used only when no inputs are supplied.
"""
import json
import os
import subprocess
import sys
import time

import knowledge as kg
import knowledge_update as ku

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
QUERY = os.path.join(ROOT, "_shared", "knowledge", "knowledge_query.py")


def _run(tmp_path, *args):
    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    proc = subprocess.run([sys.executable, QUERY, *args], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _cal(tmp_path, *emails):
    p = tmp_path / "cal.json"
    p.write_text(json.dumps([{"summary": "Sync", "attendees": [{"email": e} for e in emails]}]))
    return str(p)


def _gmail(tmp_path, *addresses, name="gmail.json"):
    p = tmp_path / name
    p.write_text(json.dumps([{"from": f"Someone <{a}>", "to": "me@mine.com", "subject": "hi"}
                             for a in addresses]))
    return str(p)


def _age_people(days=21):
    old = time.time() - days * 86400
    for name in os.listdir(kg.people_dir()):
        os.utime(os.path.join(kg.people_dir(), name), (old, old))


# ── 1. company knowledge finally reaches the brief ───────────────────────────

def test_company_packs_for_a_calendar_attendees_domain(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({
        "person_updates": [{"person_name": "Sarah Chen", "identifier": "sarah@acme.com",
                            "profile_patch": {"company": "Acme Corp"},
                            "facts": [{"fact": "Runs platform", "memory_type": "context",
                                       "confidence": 0.9}]}],
        "company_updates": [{"company_name": "Acme Corp", "domain": "acme.com",
                             "about": "Acme builds developer tools.",
                             "news": [{"text": "Older thing"}, {"text": "Hired a CFO"},
                                      {"text": "Launched v2"}, {"text": "Raised a Series B"}]}],
    })
    out = _run(tmp_path, "--calendar", _cal(tmp_path, "Sarah@Acme.com"))
    ck = out["company_knowledge"]
    # ONE entry: the attendee's domain and her `company` field resolve to the same file (dedup).
    assert list(ck) == ["acme"]
    assert "Acme builds developer tools." in ck["acme"]
    # the 3 NEWEST news lines (knowledge_update stores news newest-first), and only those
    assert ck["acme"].count("\n- ") == 3
    assert "Raised a Series B" in ck["acme"] and "Older thing" not in ck["acme"]


def test_company_packs_for_a_packed_persons_employer(tmp_path, monkeypatch):
    # No calendar at all: the company comes off the person's `company` field.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({
        "person_updates": [{"person_name": "Dana Roe", "identifier": "dana@x.io",
                            "profile_patch": {"company": "Globex"},
                            "facts": [{"fact": "Runs ops", "memory_type": "context",
                                       "confidence": 0.9}]}],
        "company_updates": [{"company_name": "Globex", "about": "Globex makes turbines."}],
    })
    out = _run(tmp_path, "--gmail", _gmail(tmp_path, "dana@x.io"))
    assert "Globex makes turbines." in out["company_knowledge"]["globex"]
    # A person who does NOT pack takes their employer with them.
    out2 = _run(tmp_path, "--gmail", _gmail(tmp_path, "nobody@nowhere.com"))
    assert "company_knowledge" not in out2


def test_company_block_is_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    people, companies = [], []
    for i in range(8):
        people.append({"person_name": f"P{i}", "identifier": f"p{i}@co{i}.com",
                       "profile_patch": {"company": f"Co{i}"},
                       "facts": [{"fact": "Works", "memory_type": "context", "confidence": 0.9}]})
        companies.append({"company_name": f"Co{i}", "domain": f"co{i}.com",
                          "about": "x" * 900, "news": [{"text": "y" * 400}]})
    ku.apply({"person_updates": people, "company_updates": companies})
    out = _run(tmp_path, "--calendar", _cal(tmp_path, *[f"p{i}@co{i}.com" for i in range(8)]))
    ck = out["company_knowledge"]
    assert len(ck) == 5                                   # MAX_COMPANIES_PACKED
    assert all(len(v) <= 600 for v in ck.values())        # MAX_COMPANY_ENTRY_CHARS


def test_company_block_is_absent_when_nothing_is_on_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({"person_updates": [{"person_name": "Solo", "identifier": "solo@nowhere.com",
                                  "profile_patch": {"company": "Nowhere Inc"},
                                  "facts": [{"fact": "Exists", "memory_type": "context",
                                             "confidence": 0.9}]}]})
    assert not os.path.isdir(kg.companies_dir()) or not os.listdir(kg.companies_dir())
    out = _run(tmp_path, "--calendar", _cal(tmp_path, "solo@nowhere.com"))
    assert "company_knowledge" not in out                  # absent, never a crash
    assert out["person_knowledge"]                         # …and the people still packed


# ── 2. today's inputs, not file mtime ────────────────────────────────────────

def test_todays_email_packs_a_person_whose_file_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({"person_updates": [
        {"person_name": "Mailer", "identifier": "mailer@x.com",
         "facts": [{"fact": "Emails you", "memory_type": "context", "confidence": 0.9}]},
        {"person_name": "Quiet One", "identifier": "quiet@x.com",
         "facts": [{"fact": "Went quiet", "memory_type": "context", "confidence": 0.9}]},
    ]})
    _age_people()                       # BOTH files last rewritten three weeks ago
    out = _run(tmp_path, "--gmail", _gmail(tmp_path, "Mailer@X.com"))
    cid = {e["identifiers"][0]: e["canonical_id"] for e in out["contact_index"]}
    assert cid["mailer@x.com"] in out["person_knowledge"]    # in today's inputs → packs
    assert cid["quiet@x.com"] not in out["person_knowledge"]  # absent today → does not


def test_inputs_replace_the_mtime_cohort_but_only_when_supplied(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({"person_updates": [
        {"person_name": "Fresh File", "identifier": "fresh@x.com",
         "facts": [{"fact": "Touched today", "memory_type": "context", "confidence": 0.9}]},
        {"person_name": "Mailer", "identifier": "mailer@x.com",
         "facts": [{"fact": "Emails you", "memory_type": "context", "confidence": 0.9}]},
    ]})
    gm = _gmail(tmp_path, "mailer@x.com")
    out = _run(tmp_path, "--gmail", gm)
    cid = {e["identifiers"][0]: e["canonical_id"] for e in out["contact_index"]}
    # inputs supplied → a fresh mtime alone is NOT a reason to pack
    assert list(out["person_knowledge"]) == [cid["mailer@x.com"]]
    # no inputs → the 7-day mtime window is the fallback cohort, exactly as before
    out2 = _run(tmp_path, "--relevant-days", "7")
    assert set(out2["person_knowledge"]) == set(cid.values())
    # an EMPTY gather is "no inputs", not "nobody" — never a silently empty knowledge block
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    out3 = _run(tmp_path, "--gmail", str(empty))
    assert set(out3["person_knowledge"]) == set(cid.values())


def test_calendar_and_email_are_one_cohort(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({"person_updates": [
        {"person_name": "Meeting", "identifier": "meet@x.com",
         "facts": [{"fact": "On the calendar", "memory_type": "context", "confidence": 0.9}]},
        {"person_name": "Mailer", "identifier": "mailer@x.com",
         "facts": [{"fact": "Emails you", "memory_type": "context", "confidence": 0.9}]},
    ]})
    _age_people()
    out = _run(tmp_path, "--calendar", _cal(tmp_path, "meet@x.com"),
               "--gmail", _gmail(tmp_path, "mailer@x.com"))
    assert len(out["person_knowledge"]) == 2
