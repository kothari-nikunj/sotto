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


def test_wrapped_read_local_snapshot_still_seeds(tmp_path):
    # The SKILL saves the read_local tool result AS-IS, which may be the raw MCP wrapper. The
    # adapter must unwrap it — it used to ingest 0 messages while printing the old healthy total.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    wrapped = {"content": [{"type": "text", "text": json.dumps(SNAPSHOT)}]}
    res = se.extract(wrapped)
    assert res["messages_analyzed"] == 4               # same as the bare-snapshot seed
    assert sum(res["canonical_counts"].values()) == 4


def test_gmail_sent_messages_feed_work_email_bucket(tmp_path):
    # Sent Gmail (isSent / SENT label) → work_email canonical samples. Without --gmail the bucket
    # stayed 0 forever and drafts never learned the user's email voice.
    os.environ["SOTTO_DATA"] = str(tmp_path)
    gmail = {"emails": [
        {"isSent": True, "to": "Sarah Chen <sarah@acme.com>", "date": "2026-06-23",
         "body": "Hi Sarah,\n\nAttached is the revised deck — the pricing slide now reflects "
                 "the pilot terms we discussed. Let me know if anything looks off.\n\nBest,\nNik",
         "labelIds": ["SENT"]},
        {"isSent": False, "from": "Sarah Chen <sarah@acme.com>", "to": "me@yahoo.com",
         "body": "Looks great, thanks!", "labelIds": ["INBOX"]},       # inbound — never ingested
        {"labelIds": ["SENT"], "to": "", "body": "no recipient — skipped"},
    ]}
    res = se.extract(json.loads(json.dumps(SNAPSHOT)), gmail=gmail)
    style = json.load(open(os.path.join(str(tmp_path), "style.json")))
    work_email_texts = " ".join(s["text"] for s in style["canonical"]["work_email"])
    assert "revised deck" in work_email_texts
    assert len(style["canonical"]["work_email"]) == 1                  # only the real sent mail
    assert res["messages_analyzed"] == 5                               # 4 local + 1 sent email
    assert "Looks great" not in json.dumps(style)                      # inbound mail never learned


def test_gmail_absent_leaves_extraction_unchanged(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    res = se.extract(json.loads(json.dumps(SNAPSHOT)), gmail=None)
    assert res["messages_analyzed"] == 4
    style = json.load(open(os.path.join(str(tmp_path), "style.json")))
    assert style["canonical"]["work_email"] == []


def test_cli_gmail_flag(tmp_path):
    # Learn step 4.4 invocation: style_extract.py /tmp/sotto_local.json --gmail /tmp/sotto_gmail.json
    seed = tmp_path / "sotto_local.json"
    seed.write_text(json.dumps(SNAPSHOT))
    gmail = tmp_path / "sotto_gmail.json"
    gmail.write_text(json.dumps({"emails": [
        {"isSent": True, "to": "Dhruv <dhruv@acme.com>", "date": "2026-06-23",
         "body": "Hi Dhruv,\n\nSending over the LOI redline now — flagging the exclusivity "
                 "clause for your read before we countersign.\n\nBest,\nNik"}]}))
    env = {**os.environ, "SOTTO_DATA": str(tmp_path)}
    out = subprocess.run([sys.executable, SCRIPT, str(seed), "--gmail", str(gmail)],
                         capture_output=True, text=True, env=env, check=True)
    res = json.loads(out.stdout)
    assert res["messages_analyzed"] == 5
    assert res["canonical_counts"]["work_email"] == 1


# ── Confirmation: the `confirmed` bucket's first (and only) writer ────────────────────────────────
# style_apply.py has always READ style.json's `confirmed` list ("Drafts the user has shipped before
# (highest signal)"). Nothing ever wrote it. `--confirm` is that writer — the deterministic seed of
# the voice loop, and the Voice card's one control.

def _some_sample(tmp_path):
    style = json.load(open(os.path.join(str(tmp_path), "style.json")))
    for bucket in se.ALL_BUCKETS:
        pool = style["canonical"][bucket]
        if pool:
            return style, pool[0]
    raise AssertionError("the fixture produced no canonical samples")


def test_confirm_copies_one_sample_and_floors_its_quality(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    se.extract(json.loads(json.dumps(SNAPSHOT)))
    style, sample = _some_sample(tmp_path)
    before = len(style["canonical"][sample["bucket"]])
    out = se.confirm_sample(se.sample_hash(sample))
    assert out["ok"] is True and out["already"] is False and out["confirmed"] == 1
    after = json.load(open(os.path.join(str(tmp_path), "style.json")))
    entry, = after["confirmed"]
    assert entry["text"] == sample["text"] and entry["source"] == "confirmed"
    assert entry["quality"] >= 0.95 and entry["confirmed_at"]
    # COPIED, not moved: the pools the next Learn run rebuilds are untouched
    assert len(after["canonical"][sample["bucket"]]) == before


def test_confirm_is_idempotent_and_reports_unknown_samples(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    se.extract(json.loads(json.dumps(SNAPSHOT)))
    _, sample = _some_sample(tmp_path)
    key = se.sample_hash(sample)
    se.confirm_sample(key)
    again = se.confirm_sample(key)
    assert again["ok"] is True and again["already"] is True
    assert len(json.load(open(os.path.join(str(tmp_path), "style.json")))["confirmed"]) == 1
    assert se.confirm_sample("0" * 16)["ok"] is False
    assert se.confirm_sample("")["ok"] is False


def test_confirm_survives_the_next_extraction_and_reaches_the_drafter(tmp_path):
    """The point of confirming: the sample stops aging out, and style_apply quotes it first."""
    os.environ["SOTTO_DATA"] = str(tmp_path)
    se.extract(json.loads(json.dumps(SNAPSHOT)))
    _, sample = _some_sample(tmp_path)
    se.confirm_sample(se.sample_hash(sample))
    se.extract(json.loads(json.dumps(SNAPSHOT)))              # a later Learn run
    style = json.load(open(os.path.join(str(tmp_path), "style.json")))
    assert [s["text"] for s in style["confirmed"]] == [sample["text"]]
    sa_spec = importlib.util.spec_from_file_location(
        "style_apply_confirm", os.path.join(HERE, "..", "_shared", "scripts", "style_apply.py"))
    sa = importlib.util.module_from_spec(sa_spec)
    sa_spec.loader.exec_module(sa)
    # Ask the drafter for the register this sample belongs to (style_apply keys `confirmed` by
    # bucket) with an unknown recipient, so the per-person pool can't stand in for it.
    channel = "email" if sample["bucket"] == "work_email" else "imessage"
    out = sa.apply({"channel": channel, "recipient": "nobody@example.invalid"})
    assert out["bucket"] == sample["bucket"]
    assert "shipped before" in out["guidance"] and sample["text"][:40] in out["guidance"]


def test_confirm_cli_contract(tmp_path):
    os.environ["SOTTO_DATA"] = str(tmp_path)
    se.extract(json.loads(json.dumps(SNAPSHOT)))
    _, sample = _some_sample(tmp_path)
    env = {**os.environ, "SOTTO_DATA": str(tmp_path)}
    r = subprocess.run([sys.executable, SCRIPT, "--confirm", se.sample_hash(sample)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and json.loads(r.stdout)["ok"] is True
    r2 = subprocess.run([sys.executable, SCRIPT, "--confirm", "0" * 16],
                        capture_output=True, text=True, env=env)
    assert r2.returncode == 2 and json.loads(r2.stdout)["ok"] is False
