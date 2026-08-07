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
import subprocess
import sys
from datetime import datetime

import yaml

import knowledge as kg
import knowledge_edit as ke

HERE = os.path.dirname(__file__)
CLI = os.path.join(HERE, "..", "_shared", "scripts", "knowledge_edit.py")

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
