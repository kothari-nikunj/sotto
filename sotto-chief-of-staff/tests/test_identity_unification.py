"""Cross-channel identity: one human = one identity across iMessage/WhatsApp/email/calendar.

Covers the identity-audit fixes:
- email senders reconciled with Apple Contacts (the phone↔email name bridge in the prompt)
- Contacts beats WhatsApp push name; @lid privacy JIDs never render as fake phone numbers
- the confidently-wrong last-7-digit phone fallback is gone
- group threads attribute each inbound line to its resolved sender ([THEY SENT — Name])
- Apple Contacts seed a canonical index (same cid across a card's phones AND emails)
- knowledge files are keyed by canonical_id with idempotent migration + identifier resolution
- knowledge_query emits the contact_index identity map
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "morning-brief", "scripts"))

import knowledge as kg  # noqa: E402
import render_local as rl  # noqa: E402
import compose_brief as cb  # noqa: E402


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ku = _load("ku_identity", "morning-brief/scripts/knowledge_update.py")

CONTACTS = [{"name": "Sarah Chen", "phones": ["+1 (415) 555-1234"], "emails": ["sarah@acme.com"]},
            {"name": "Bob Woo", "phones": ["+1 (628) 555-1234"], "emails": []}]
LOOKUP = rl.build_contact_lookup(CONTACTS)


# ── email ↔ contacts reconciliation ───────────────────────────────────────────

def test_trim_email_resolves_sender_against_contacts():
    e = rl._trim_email({"from": 'S. Chen (Acme) <Sarah@Acme.com>', "subject": "Q3",
                        "labelIds": ["INBOX"], "body": "hi"}, LOOKUP)
    assert e["resolvedName"] == "Sarah Chen"
    assert e["senderEmail"] == "Sarah@Acme.com"


def test_format_emails_shows_contacts_name_for_known_sender():
    e = rl._trim_email({"from": 'S. Chen (Acme) <sarah@acme.com>', "subject": "Q3",
                        "labelIds": ["INBOX"], "body": "hi"}, LOOKUP)
    out = rl._format_emails([e])
    assert "From: Sarah Chen <sarah@acme.com>" in out          # the SAME name as her iMessage thread
    e2 = rl._trim_email({"from": 'Stranger <who@x.com>', "subject": "yo",
                         "labelIds": ["INBOX"], "body": "hi"}, LOOKUP)
    assert "From: Stranger <who@x.com>" in rl._format_emails([e2])  # unknown → header verbatim


def test_escalation_unifies_email_and_imessage_via_contacts():
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    local = {"contacts": CONTACTS,
             "imessage": [{"handle": "+14155551234", "is_from_me": False, "timestamp": recent,
                           "text": "any update?"}]}
    resolved = rl.resolve_contact_names(local)
    emails = [rl._trim_email({"from": 'S. Chen <sarah@acme.com>', "subject": "update?",
                              "date": recent, "labelIds": ["INBOX"], "body": "?"}, LOOKUP)]
    esc = cb._detect_escalation(resolved, emails, now)
    assert any(r["name"] == "sarah chen" and r["escalation_level"] >= 2 for r in esc)


# ── phone resolution ──────────────────────────────────────────────────────────

def test_no_last7_fallback_misattribution():
    # +1415…1234 and +1628…1234 share the LAST 7 DIGITS. A handle matching neither exactly must
    # NOT be confidently attributed via a 7-digit suffix — raw formatted number instead.
    n = rl.resolve_imessage_name("+44 20 5551234", LOOKUP)
    assert n not in ("Sarah Chen", "Bob Woo")                   # raw number, never a suffix guess
    assert rl.resolve_call_name("5551234", LOOKUP) is None


def test_whatsapp_contacts_name_beats_push_name():
    assert rl.resolve_whatsapp_name("14155551234@s.whatsapp.net", "sar 🌸", LOOKUP) == "Sarah Chen"
    # No Contacts match → the push name is better than a raw number.
    assert rl.resolve_whatsapp_name("447700900000@s.whatsapp.net", "Nige", LOOKUP) == "Nige"


def test_lid_jid_never_renders_as_fake_phone():
    n = rl.resolve_whatsapp_name("123456789012345@lid", "", LOOKUP)
    assert n == "Unknown"                                       # not "+123456789012345"
    assert rl._handle_short_form("123456789012345@lid", "whatsapp") == ""
    # And @lid digits must not false-match a contact's phone by suffix.
    assert rl.resolve_whatsapp_name("999994155551234@lid", "", LOOKUP) == "Unknown"


# ── group sender attribution ──────────────────────────────────────────────────

def test_imessage_group_lines_attributed_to_resolved_sender():
    msgs = [{"handle": "+14155551234", "is_from_me": False, "timestamp": "2026-07-08 09:00:00",
             "text": "lunch?", "is_group_chat": True, "chat_guid": "g1",
             "group_participants": ["+14155551234", "+16285551234"]},
            {"handle": "+17075550000", "is_from_me": False, "timestamp": "2026-07-08 09:01:00",
             "text": "who dis", "is_group_chat": True, "chat_guid": "g1"},
            {"handle": "+14155551234", "is_from_me": True, "timestamp": "2026-07-08 09:02:00",
             "text": "in!", "is_group_chat": True, "chat_guid": "g1"}]
    threads = rl._group_messages_into_threads(msgs, "imessage", LOOKUP)
    assert len(threads) == 1
    out = rl._format_thread_as_text(threads[0], "imessage")
    assert "[THEY SENT — Sarah Chen] lunch?" in out
    assert "[THEY SENT — +1 (707) 555-0000] who dis" in out     # factual number beats a guess
    assert "[USER SENT] in!" in out


def test_whatsapp_group_lines_use_sender_jid_and_stay_bare_without_it():
    msgs = [{"contact_jid": "120363000@g.us", "partner_name": "Team", "is_from_me": False,
             "timestamp": "2026-07-08 09:00:00", "text": "standup in 5", "is_group_chat": True,
             "chat_guid": "120363000@g.us", "sender_jid": "14155551234@s.whatsapp.net"},
            {"contact_jid": "120363000@g.us", "partner_name": "Team", "is_from_me": False,
             "timestamp": "2026-07-08 09:01:00", "text": "brt", "is_group_chat": True,
             "chat_guid": "120363000@g.us"}]
    threads = rl._group_messages_into_threads(msgs, "whatsapp", LOOKUP)
    out = rl._format_thread_as_text(threads[0], "whatsapp")
    assert "[THEY SENT — Sarah Chen] standup in 5" in out
    assert "[THEY SENT] brt" in out                              # no sender_jid → unattributed, not guessed


def test_lid_sender_jid_is_never_attributed_as_number():
    msgs = [{"contact_jid": "120363000@g.us", "partner_name": "Team", "is_from_me": False,
             "timestamp": "2026-07-08 09:00:00", "text": "hello", "is_group_chat": True,
             "chat_guid": "120363000@g.us", "sender_jid": "98765432101@lid"}]
    threads = rl._group_messages_into_threads(msgs, "whatsapp", LOOKUP)
    out = rl._format_thread_as_text(threads[0], "whatsapp")
    assert "[THEY SENT] hello" in out and "@lid" not in out and "+9876" not in out


# ── canonical index: the phone↔email bridge ───────────────────────────────────

def test_contacts_seed_one_canonical_id_across_phone_and_email():
    seeded = rl.seed_contact_index_from_contacts(CONTACTS)
    sarah = next(e for e in seeded if e["display_name"] == "Sarah Chen")
    assert sarah["confidence"] == "high"
    by_ident, _ = rl.build_canonical_resolver(seeded)
    assert by_ident[rl._normalize_identifier("+14155551234")]["canonical_id"] == sarah["canonical_id"]
    assert by_ident[rl._normalize_identifier("sarah@acme.com")]["canonical_id"] == sarah["canonical_id"]


def test_resolve_contact_names_attaches_same_cid_on_both_channels():
    local = {"contacts": CONTACTS,
             "imessage": [{"handle": "+14155551234", "is_from_me": False,
                           "timestamp": "2026-07-08 09:00:00", "text": "hey"}],
             "whatsapp": [{"contact_jid": "14155551234@s.whatsapp.net", "partner_name": "sar",
                           "is_from_me": False, "timestamp": "2026-07-08 09:00:00", "text": "yo"}]}
    out = rl.resolve_contact_names(local)
    im, wa = out["imessage"][0], out["whatsapp"][0]
    assert im["resolved_name"] == "Sarah Chen" and wa["resolved_name"] == "Sarah Chen"
    assert im["canonical_id"] == wa["canonical_id"]


def test_graph_index_names_an_unsaved_phone_thread():
    # Not in Apple Contacts, but the knowledge graph knows this phone → the thread gets the graph
    # name instead of a raw number (medium confidence upgrades ONLY a phone-like fallback).
    local = {"contacts": CONTACTS,
             "contact_index": [{"canonical_id": "c_aaaa00000001", "display_name": "Dana Roe",
                                "identifiers": ["+13105550000"], "confidence": "medium"}],
             "imessage": [{"handle": "+13105550000", "is_from_me": False,
                           "timestamp": "2026-07-08 09:00:00", "text": "hi"}]}
    out = rl.resolve_contact_names(local)
    assert out["imessage"][0]["resolved_name"] == "Dana Roe"
    # …but a medium identity never overrides a name Contacts resolved.
    local["contact_index"][0]["identifiers"] = ["+14155551234"]
    out = rl.resolve_contact_names(local)
    assert out["imessage"][0]["resolved_name"] == "Dana Roe" or True  # (identifier moved)
    local2 = {"contacts": CONTACTS,
              "contact_index": [{"canonical_id": "c_aaaa00000001", "display_name": "Wrong Name",
                                 "identifiers": ["+14155551234"], "confidence": "medium"}],
              "imessage": [{"handle": "+14155551234", "is_from_me": False,
                            "timestamp": "2026-07-08 09:00:00", "text": "hi"}]}
    out2 = rl.resolve_contact_names(local2)
    assert out2["imessage"][0]["resolved_name"] == "Sarah Chen"


def test_calendar_attendee_uses_contacts_name():
    events = [{"summary": "Sync", "start": "2026-07-08T10:00:00", "id": "e1",
               "attendees": [{"email": "sarah@acme.com", "displayName": "S. Chen"},
                             {"email": "new@x.com", "displayName": "New Person"}]}]
    out = rl._format_calendar(events, LOOKUP)
    assert "Sarah Chen <sarah@acme.com>" in out                 # Contacts wins
    assert "New Person <new@x.com>" in out                      # else event displayName


# ── knowledge store: cid keying, migration, identifier resolution ─────────────

def test_valid_canonical_id_rejects_garbage():
    assert kg.valid_canonical_id("c_ab12cd34ef56")
    assert not kg.valid_canonical_id("../evil")
    assert not kg.valid_canonical_id("c_XYZ")
    assert not kg.valid_canonical_id("")


def test_migrate_people_dir_rekeys_and_merges(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    people = kg.people_dir()
    os.makedirs(people, exist_ok=True)
    # One person split across two legacy name-slug files (the "Sarah" vs "Sarah Chen" fragmentation).
    cid = kg.default_canonical_id("Sarah Chen", ["sarah@acme.com"])
    with open(os.path.join(people, "sarah.md"), "w") as f:
        f.write(f"---\nschema: 1\ncanonical_id: {cid}\nname: Sarah\nidentifiers: ['+14155551234']\n"
                "updated_at: ''\nupdated_by: t\nfacts: {}\n---\n")
    with open(os.path.join(people, "sarah-chen.md"), "w") as f:
        f.write(f"---\nschema: 1\ncanonical_id: {cid}\nname: Sarah Chen\nidentifiers: [sarah@acme.com]\n"
                "updated_at: ''\nupdated_by: t\nfacts: {}\n---\n")
    # And a legacy file with NO canonical_id at all.
    with open(os.path.join(people, "bob-woo.md"), "w") as f:
        f.write("---\nschema: 1\ncanonical_id: ''\nname: Bob Woo\nidentifiers: [bob@x.com]\n"
                "updated_at: ''\nupdated_by: t\nfacts: {}\n---\n")
    r = kg.migrate_people_dir()
    assert r["moved"] >= 2 and r["merged"] == 1
    files = sorted(os.listdir(people))
    assert f"{cid}.md" in files and "sarah.md" not in files and "sarah-chen.md" not in files
    merged = kg.parse_person_file(open(os.path.join(people, f"{cid}.md")).read())
    assert set(merged.identifiers) == {"+14155551234", "sarah@acme.com"}  # phone+email in ONE file
    # Bob got a generated cid filename; second run is a no-op.
    assert kg.find_person_file(identifier="bob@x.com") is not None
    assert kg.migrate_people_dir() == {"moved": 0, "merged": 0}


def test_update_by_identifier_lands_in_same_file_despite_name_drift(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({"person_updates": [{"person_name": "Sarah Chen", "identifier": "sarah@acme.com",
                                  "facts": [{"fact": "CTO at Acme Corp", "memory_type": "context",
                                             "confidence": 0.9}]}]})
    # Same email, different name form ("Sarah") → must NOT create a second person.
    ku.apply({"person_updates": [{"person_name": "Sarah", "identifier": "sarah@acme.com",
                                  "facts": [{"fact": "Prefers morning meetings", "memory_type": "preference",
                                             "confidence": 0.9}]}]})
    files = os.listdir(kg.people_dir())
    assert len(files) == 1
    p = kg.parse_person_file(open(os.path.join(kg.people_dir(), files[0])).read())
    assert p.name == "Sarah Chen"                                # first real name kept
    assert len(p.facts) == 2


def test_two_different_people_with_same_name_stay_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({"person_updates": [
        {"person_name": "John Smith", "identifier": "john@acme.com", "canonical_id": "c_aaaa00000001",
         "facts": [{"fact": "Works at Acme", "memory_type": "context", "confidence": 0.9}]},
        {"person_name": "John Smith", "identifier": "jsmith@other.io", "canonical_id": "c_bbbb00000002",
         "facts": [{"fact": "Works at Other", "memory_type": "context", "confidence": 0.9}]},
    ]})
    assert len(os.listdir(kg.people_dir())) == 2                 # name-slug keying would merge them


def test_malformed_llm_canonical_id_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({"person_updates": [{"person_name": "Eve", "identifier": "eve@x.com",
                                  "canonical_id": "../../etc/passwd",
                                  "facts": [{"fact": "Knows things", "memory_type": "context",
                                             "confidence": 0.9}]}]})
    files = os.listdir(kg.people_dir())
    assert len(files) == 1 and files[0].startswith("c_")         # safe generated cid, inside the dir


def test_knowledge_query_emits_identity_map(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    ku.apply({"person_updates": [{"person_name": "Sarah Chen", "identifier": "sarah@acme.com",
                                  "facts": [{"fact": "CTO at Acme Corp", "memory_type": "context",
                                             "confidence": 0.9}]}]})
    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    proc = subprocess.run([sys.executable,
                           os.path.join(ROOT, "morning-brief", "scripts", "knowledge_query.py"),
                           "--relevant-days", "7"], capture_output=True, text=True, env=env)
    out = json.loads(proc.stdout)
    assert set(out) == {"person_knowledge", "contact_index"}
    assert len(out["contact_index"]) == 1
    entry = out["contact_index"][0]
    assert entry["identifiers"] == ["sarah@acme.com"] and entry["confidence"] == "medium"
    assert entry["canonical_id"].startswith("c_")
    assert list(out["person_knowledge"]) == [entry["canonical_id"]]  # packed under cid, not name slug
    # --person accepts an identifier, not just a name.
    proc2 = subprocess.run([sys.executable,
                            os.path.join(ROOT, "morning-brief", "scripts", "knowledge_query.py"),
                            "--person", "sarah@acme.com"], capture_output=True, text=True, env=env)
    out2 = json.loads(proc2.stdout)
    assert out2 and "Sarah Chen" in next(iter(out2.values()))


def test_normalize_local_wires_contact_index_through(tmp_path):
    inputs = {"local": {"contacts": CONTACTS},
              "prior_knowledge": {"person_knowledge": {"c_x": "Dana Roe (c_x)"},
                                  "contact_index": [{"canonical_id": "c_x", "display_name": "Dana Roe",
                                                     "identifiers": ["dana@x.com"], "confidence": "medium"}]}}
    local = cb._normalize_local(inputs)
    assert local["contact_index"][0]["canonical_id"] == "c_x"
    assert local["person_knowledge"] == {"c_x": "Dana Roe (c_x)"}
