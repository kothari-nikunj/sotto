"""Tests for the setup style seed: style_extract.py must accept a raw Bridge read_local snapshot
(the /tmp/sotto_seed.json that setup/SKILL.md step 3 feeds it) and seed a NON-empty style.json —
this was the silent no-op where setup announced "learned your writing style" over an empty profile.
The legacy `sent_messages` payload (cloud/brief callers) must keep working unchanged."""
import json
import os
import subprocess
import sys
import importlib.util

HERE = os.path.dirname(__file__)
SCRIPT = os.path.join(HERE, "..", "_shared", "scripts", "style_extract.py")
sys.path.insert(0, os.path.join(HERE, "..", "_shared", "lib"))


def _load():
    spec = importlib.util.spec_from_file_location("style_extract_seed", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


se = _load()

# A read_local snapshot per contracts/local_data.schema.json: flat per-message arrays with
# is_from_me, plus the Bridge contacts list. No `sent_messages` key anywhere.
SNAPSHOT = {
    "generated_at": "2026-06-24T07:00:00Z",
    "window_hours": 1008,
    "source_status": {"imessage": "ok", "whatsapp": "ok"},
    "contacts": [
        {"name": "Sarah Chen", "phones": ["+1 (415) 555-1234"], "emails": ["sarah@acme.com"]},
        {"name": "Mom", "phones": ["+1 (415) 555-9999"], "emails": []},
    ],
    "imessage": [
        {"handle": "+14155551234", "is_from_me": True, "timestamp": "2026-06-20 09:00:00",
         "text": "Hey Sarah, sounds great — can you send the deck by Friday?", "is_group_chat": False},
        {"handle": "+14155551234", "is_from_me": False, "timestamp": "2026-06-20 09:05:00",
         "text": "Will do, sending it over this afternoon!", "is_group_chat": False},
        {"handle": "+14155551234", "is_from_me": True, "timestamp": "2026-06-21 10:00:00",
         "text": "Thanks for the update, will review tonight and circle back tomorrow.",
         "is_group_chat": False},
        {"handle": "chat123", "is_from_me": True, "timestamp": "2026-06-21 11:00:00",
         "text": "This is a group chat message with no single recipient here.", "is_group_chat": True},
    ],
    "whatsapp": [
        {"contact_jid": "14155559999@s.whatsapp.net", "partner_name": "Mom", "is_from_me": True,
         "timestamp": "2026-06-21 19:00:00", "text": "running a bit late, be there around 7",
         "is_group_chat": False},
        {"contact_jid": "14155559999@s.whatsapp.net", "partner_name": "Mom", "is_from_me": True,
         "timestamp": "2026-06-22 18:30:00", "text": "can you grab milk on the way home",
         "is_group_chat": False},
        {"contact_jid": "14155559999@s.whatsapp.net", "partner_name": "Mom", "is_from_me": False,
         "timestamp": "2026-06-22 18:35:00", "text": "sure thing, see you soon honey",
         "is_group_chat": False},
    ],
}


def test_read_local_snapshot_seeds_nonempty_style(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    res = se.extract(json.loads(json.dumps(SNAPSHOT)))
    style = json.load(open(os.path.join(str(tmp_path), "style.json")))
    # Only the 4 usable is_from_me 1:1 messages are ingested (inbound + group excluded).
    assert res["messages_analyzed"] == 4
    assert sum(res["canonical_counts"].values()) == 4
    # Master style is derived from real samples, not the empty default.
    assert style["master"]["greetings"] or style["master"]["patterns"]


def test_read_local_work_vs_personal_classification(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    se.extract(json.loads(json.dumps(SNAPSHOT)))
    style = json.load(open(os.path.join(str(tmp_path), "style.json")))
    # Sarah has a non-personal email domain (acme.com) → her texts land in work_message.
    work_texts = " ".join(s["text"] for s in style["canonical"]["work_message"])
    assert "send the deck by Friday" in work_texts
    # Mom has no email → resolved contact without work signal → personal_message.
    personal_texts = " ".join(s["text"] for s in style["canonical"]["personal_message"])
    assert "grab milk" in personal_texts


def test_read_local_seeds_per_person_samples(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    se.extract(json.loads(json.dumps(SNAPSHOT)))
    style = json.load(open(os.path.join(str(tmp_path), "style.json")))
    names = {p["name"] for p in style["per_person"].values()}
    assert names == {"Sarah Chen", "Mom"}          # >=2 samples each → per-person profiles
    # canonical_id follows the knowledge-graph seed scheme so it lines up with prewarm stubs.
    assert all(k.startswith("c_") for k in style["per_person"])


def test_sent_messages_payload_still_works(tmp_path):
    # The legacy cloud/brief shape must behave exactly as before the adapter.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    res = se.extract({"sent_messages": [
        {"text": "Hey Sarah, sounds great — can you send the deck by Friday?", "channel": "imessage",
         "recipient": "sarah@acme.com", "canonical_id": "c_sarah", "work": True},
        {"text": "Thanks for the update, will review tonight and circle back tomorrow.",
         "channel": "imessage", "recipient": "sarah@acme.com", "canonical_id": "c_sarah", "work": True},
    ]})
    style = json.load(open(os.path.join(str(tmp_path), "style.json")))
    assert res["messages_analyzed"] == 2
    assert len(style["canonical"]["work_message"]) == 2
    assert "c_sarah" in style["per_person"]


def test_adapt_passthrough_when_sent_messages_present():
    payload = {"sent_messages": [], "imessage": [{"is_from_me": True, "text": "x"}]}
    assert se._adapt_read_local(payload) is payload   # byte-identical path for existing callers


def test_cli_accepts_snapshot_file_path(tmp_path):
    # setup/SKILL.md invokes `style_extract.py /tmp/sotto_seed.json` — a PATH, not inline JSON.
    seed = tmp_path / "sotto_seed.json"
    seed.write_text(json.dumps(SNAPSHOT))
    env = {**os.environ, "SOTTO_DATA": str(tmp_path)}
    out = subprocess.run([sys.executable, SCRIPT, str(seed)],
                         capture_output=True, text=True, env=env, check=True)
    res = json.loads(out.stdout)
    assert res["messages_analyzed"] == 4
    assert res["people"] == 2
