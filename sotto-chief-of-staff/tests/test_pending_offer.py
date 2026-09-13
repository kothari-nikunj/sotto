"""test_pending_offer.py — the question Sotto asked survives into the session that gets the answer.

THE BUG (real WhatsApp transcript, Aug 2026): the 11:15 proactive tick asked "You're meeting
Shivani in ~44 min at Sightglass — want me to pull full prep on her?". At 11:44 the user replied
"Sure" and got an answer about an unrelated group chat, because the nudge was delivered by a
detached run and the gateway session that received "Sure" had never seen the question. The fix is
not prompt wording; it is writing the question to the volume so the other process can read it.

These tests pin the four properties that make that safe to act on: a fresh offer round-trips, a
stale one NEVER does (a "sure" three hours later is not a yes), the newest question is the only
one on file, and a set racing a get can't hand the gateway a torn half-question.
"""
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import pytest
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
HERMES = os.path.dirname(ROOT)
SCRIPT = os.path.join(ROOT, "_shared", "scripts", "pending_offer.py")

_spec = importlib.util.spec_from_file_location("pending_offer", SCRIPT)
po = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(po)

QUESTION = ("You're meeting Shivani in ~44 min at Sightglass — "
            "want me to pull full prep on her?")


def _data(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    return os.path.join(str(tmp_path), "proactive", "pending_offer.json")


# ── the round trip ──────────────────────────────────────────────────────────────────────────────

def test_set_then_get_returns_the_question_as_delivered(tmp_path, monkeypatch):
    _data(tmp_path, monkeypatch)
    po.set_offer("meeting_prep", QUESTION, person="Shivani", detail="12:00 PM, Sightglass")
    got = po.get_offer()
    assert got["kind"] == "meeting_prep"
    assert got["question"] == QUESTION          # verbatim — the gateway shows it back to the user
    assert got["person"] == "Shivani"
    assert got["detail"] == "12:00 PM, Sightglass"


def test_the_stored_shape_is_exactly_the_ten_ephemeral_fields(tmp_path, monkeypatch):
    """Ephemeral state, not memory: nothing here could be used by a brief three months from now,
    so nothing beyond the question, its clock, the loop it is about and the hash of what it
    offered is allowed to accumulate."""
    path = _data(tmp_path, monkeypatch)
    po.set_offer("chase", "any word on the deck?", person="Maya", anchor_key="email:waiting_on:id:maya")
    with open(path, encoding="utf-8") as f:
        stored = json.load(f)
    assert set(stored) == {"ts", "kind", "question", "person", "detail", "payload_sha256",
                           "expires_at", "anchor_key", "action", "offer_id"}
    assert stored["payload_sha256"] == ""       # an offer with no real effect binds to nothing
    assert stored["anchor_key"] == "email:waiting_on:id:maya"   # so "done" lands on THAT loop
    assert po.get_offer()["anchor_key"] == "email:waiting_on:id:maya"
    # the dismissal vocabulary is stated once, for the persona to read
    assert "done" in po.DISMISS_RESOLVED and "let it go" in po.DISMISS_DROPPED
    assert "mute" not in po.KINDS


def test_get_on_a_missing_file_is_an_empty_object(tmp_path, monkeypatch):
    _data(tmp_path, monkeypatch)
    assert po.get_offer() == {}


def test_default_ttl_is_three_hours(tmp_path, monkeypatch):
    _data(tmp_path, monkeypatch)
    offer = po.set_offer("meeting_prep", QUESTION, person="Shivani")
    span = po._parse(offer["expires_at"]) - po._parse(offer["ts"])
    assert span == timedelta(minutes=po.DEFAULT_TTL_MIN) == timedelta(minutes=180)


# ── expiry: checked at read, nothing daemonic ───────────────────────────────────────────────────

def test_an_expired_offer_reads_as_empty(tmp_path, monkeypatch):
    """A "sure" that arrives after the window is not a yes to a meeting that already started."""
    _data(tmp_path, monkeypatch)
    po.set_offer("meeting_prep", QUESTION, person="Shivani", ttl_min=-1)
    assert po.get_offer() == {}


def test_a_stale_file_on_disk_never_returns(tmp_path, monkeypatch):
    """The file outlives its answer window — expiry is enforced by the READER, so a file left
    behind by yesterday's tick (no sweeper ever ran) still answers nothing today."""
    path = _data(tmp_path, monkeypatch)
    old = datetime.now(timezone.utc) - timedelta(days=1)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": old.isoformat(), "kind": "meeting_prep", "question": QUESTION,
                   "person": "Shivani", "detail": "",
                   "expires_at": (old + timedelta(minutes=180)).isoformat()}, f)
    assert po.get_offer() == {}
    assert os.path.exists(path)                 # read-only path: it doesn't delete, it declines


def test_a_corrupt_or_questionless_file_reads_as_empty(tmp_path, monkeypatch):
    """Never partial: the gateway gets a whole question it can act on, or nothing it could mistake
    for one."""
    path = _data(tmp_path, monkeypatch)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert po.get_offer() == {}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"kind": "meeting_prep"}, f)
    assert po.get_offer() == {}


# ── binding a yes to the bytes it was given ─────────────────────────────────────────────────────

DRAFT = "hey — any word on the deck? no rush if not."


def test_a_payload_file_stores_only_its_hash(tmp_path, monkeypatch):
    """The offer names WHICH bytes the user is being asked about; it never keeps them. Storing the
    draft here would put a second copy of everything Sotto offers on the volume, for a file whose
    whole point is that it is ephemeral."""
    path = _data(tmp_path, monkeypatch)
    payload = tmp_path / "draft.txt"
    payload.write_text(DRAFT, encoding="utf-8")

    digest = po.payload_hash(payload.read_bytes())
    po.set_offer("chase", "Want this in your Gmail drafts?", person="Maya",
                 payload_sha256=digest)

    raw = open(path, encoding="utf-8").read()
    assert digest in raw
    assert DRAFT not in raw and "deck" not in raw          # the hash, never the draft
    assert po.get_offer()["payload_sha256"] == digest      # and the acting side reads it back


def test_the_hash_is_over_the_exact_bytes(tmp_path):
    """The offering lane hashes a file and the acting verb hashes a string — they must land on the
    same digest or `--offer-bound` refuses everything it should allow."""
    assert po.payload_hash(DRAFT.encode("utf-8")) == po.payload_hash(DRAFT.encode("utf-8"))
    assert po.payload_hash(DRAFT.encode("utf-8")) != po.payload_hash((DRAFT + " ").encode("utf-8"))
    assert len(po.payload_hash(b"")) == 64


def test_an_expired_bound_offer_still_reads_as_empty(tmp_path, monkeypatch):
    """Binding does not extend the window: a yes outside it has no offer to match against."""
    _data(tmp_path, monkeypatch)
    po.set_offer("chase", "Want this in your Gmail drafts?", ttl_min=-1,
                 payload_sha256=po.payload_hash(DRAFT.encode("utf-8")))
    assert po.get_offer() == {}


def test_cli_payload_file_round_trips(tmp_path):
    payload = tmp_path / "body.txt"
    payload.write_text(DRAFT, encoding="utf-8")
    _cli(tmp_path, "set", "--kind", "chase", "--question", "Want this in your Gmail drafts?",
         "--payload-file", str(payload))
    got = json.loads(_cli(tmp_path, "get"))
    assert got["payload_sha256"] == po.payload_hash(DRAFT.encode("utf-8"))


def test_cli_set_without_a_payload_file_binds_to_nothing(tmp_path):
    """"Want me to pull prep?" has no bytes to bind — no ceremony where there is no payload."""
    _cli(tmp_path, "set", "--kind", "meeting_prep", "--question", QUESTION, "--person", "Shivani")
    assert json.loads(_cli(tmp_path, "get"))["payload_sha256"] == ""


# ── one offer at a time ─────────────────────────────────────────────────────────────────────────

def test_a_second_set_replaces_the_first(tmp_path, monkeypatch):
    """Newest wins — the newer question is the one on the user's screen, and two stacked questions
    is a user who should be asked which they meant, not guessed at."""
    _data(tmp_path, monkeypatch)
    po.set_offer("meeting_prep", QUESTION, person="Shivani")
    po.set_offer("handoff", "I've nudged Maya twice about the contract — nudge again, or let it go?",
                 person="Maya")
    got = po.get_offer()
    assert got["kind"] == "handoff" and got["person"] == "Maya"
    assert "Shivani" not in json.dumps(got)


def test_clear_removes_the_offer_and_is_idempotent(tmp_path, monkeypatch):
    path = _data(tmp_path, monkeypatch)
    po.set_offer("retune_offer", "Want me to run a quick cleanup?")
    assert po.clear_offer() is True
    assert not os.path.exists(path)
    assert po.get_offer() == {}
    assert po.clear_offer() is False            # nothing there; still not an error


# ── the CLI the skills actually call ────────────────────────────────────────────────────────────

def _cli(tmp_path, *args):
    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    p = subprocess.run([sys.executable, SCRIPT, *args], env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-2000:]
    return p.stdout.strip()


def test_cli_set_get_clear(tmp_path):
    assert json.loads(_cli(tmp_path, "get")) == {}
    _cli(tmp_path, "set", "--kind", "meeting_prep", "--person", "Shivani", "--question", QUESTION)
    assert json.loads(_cli(tmp_path, "get"))["question"] == QUESTION
    assert _cli(tmp_path, "clear") == "cleared"
    assert json.loads(_cli(tmp_path, "get")) == {}


def _tracked_loop(tmp_path):
    path = tmp_path / "knowledge" / "continuity" / "maya.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    anchor = "waiting_on:id:maya@example.test"
    path.write_text("---\n" + yaml.safe_dump({
        "anchor_key": anchor, "status": "open", "contact_name": "Maya",
        "contact_identifier": "maya@example.test", "action_type": "waiting_on",
        "summary": "The signed contract", "created_at": "2026-09-04",
    }) + "---\n")
    return path, anchor


@pytest.mark.parametrize("kind", ["chase", "commitment", "handoff"])
@pytest.mark.parametrize("reply", ["no", "No!", "no .", "no thanks", "not yet", "skip it", "forget it"])
def test_negative_reply_declines_offer_without_changing_obligation(tmp_path, kind, reply):
    path, anchor = _tracked_loop(tmp_path)
    before = path.read_bytes()
    _cli(tmp_path, "set", "--kind", kind, "--question", "Is this done?",
         "--person", "Maya", "--anchor-key", anchor)
    result = json.loads(_cli(tmp_path, "dismiss-reply", "--text", reply))
    assert result["action"] == "declined" and result["loop_changed"] is False
    assert path.read_bytes() == before
    assert json.loads(_cli(tmp_path, "get")) == {}


@pytest.mark.parametrize("reply,status", [("Done!", "resolved"), ("Done…", "resolved"),
                                          ("drop it", "dismissed")])
def test_explicit_reply_changes_only_the_anchored_loop(tmp_path, reply, status):
    path, anchor = _tracked_loop(tmp_path)
    other = path.with_name("other.md")
    other.write_text(path.read_text().replace("maya@example.test", "other@example.test"))
    before = other.read_bytes()
    _cli(tmp_path, "set", "--kind", "handoff", "--question", "Keep waiting or drop it?",
         "--anchor-key", anchor)
    result = json.loads(_cli(tmp_path, "dismiss-reply", "--text", reply))
    assert result["action"] == status and result["loop_changed"] is True
    assert yaml.safe_load(path.read_text().split("---")[1])["status"] == status
    assert other.read_bytes() == before
    assert json.loads(_cli(tmp_path, "get")) == {}


@pytest.mark.parametrize("reply", ["no, it is not done", "yes", "done with the draft",
                                   "drop it tomorrow", "done?", "drop it?"])
def test_ambiguous_reply_preserves_offer_and_obligation(tmp_path, reply):
    path, anchor = _tracked_loop(tmp_path)
    before = path.read_bytes()
    _cli(tmp_path, "set", "--kind", "commitment", "--question", "Is this done?",
         "--anchor-key", anchor)
    result = json.loads(_cli(tmp_path, "dismiss-reply", "--text", reply))
    assert result["action"] == "clarify" and result["loop_changed"] is False
    assert path.read_bytes() == before
    assert json.loads(_cli(tmp_path, "get"))["anchor_key"] == anchor


def test_completion_without_an_anchor_does_not_clear_the_offer(tmp_path):
    _cli(tmp_path, "set", "--kind", "commitment", "--question", "Is this done?")
    assert json.loads(_cli(tmp_path, "dismiss-reply", "--text", "done"))["action"] == "clarify"
    assert json.loads(_cli(tmp_path, "get"))["question"] == "Is this done?"


def test_completion_to_expired_offer_does_not_change_loop(tmp_path):
    path, anchor = _tracked_loop(tmp_path)
    before = path.read_bytes()
    _cli(tmp_path, "set", "--kind", "commitment", "--question", "Is this done?",
         "--anchor-key", anchor, "--ttl-min", "-1")
    assert json.loads(_cli(tmp_path, "dismiss-reply", "--text", "done"))["action"] == "no_offer"
    assert path.read_bytes() == before


def test_negative_to_non_loop_offer_only_clears_offer(tmp_path):
    _cli(tmp_path, "set", "--kind", "procedure", "--question", "Save this rule?")
    assert json.loads(_cli(tmp_path, "dismiss-reply", "--text", "no"))["action"] == "declined"
    assert json.loads(_cli(tmp_path, "get")) == {}
    assert not (tmp_path / "knowledge").exists()


def test_legacy_mute_offer_cannot_supply_a_fresh_approval(tmp_path):
    _cli(tmp_path, "set", "--kind", "procedure", "--question", "Mute Maya?", "--person", "Maya")
    path = tmp_path / "proactive" / "pending_offer.json"
    old = json.loads(path.read_text())
    path.write_text(json.dumps({**old, "kind": "mute"}))
    assert json.loads(_cli(tmp_path, "get")) == {}
    result = json.loads(_cli(tmp_path, "dismiss-reply", "--text", "done"))
    assert result["action"] == "no_offer" and result["loop_changed"] is False
    assert not (tmp_path / "preferences.json").exists()


def test_cli_rejects_a_kind_no_lane_produces(tmp_path):
    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    p = subprocess.run([sys.executable, SCRIPT, "set", "--kind", "birthday",
                        "--question", "?"], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode != 0
    # Only active producers can set an offer. Legacy mute files are handled on read.
    assert set(po.KINDS) == {"meeting_prep", "commitment", "chase", "handoff", "retune_offer",
                             "procedure", "intention"}


@pytest.mark.parametrize("reply", ["no", "done", "skip it"])
def test_no_pending_offer_routes_back_to_the_visible_conversation(tmp_path, reply):
    result = json.loads(_cli(tmp_path, "dismiss-reply", "--text", reply))
    assert result["action"] == "no_offer" and result["loop_changed"] is False
    assert not (tmp_path / "knowledge").exists()


@pytest.mark.parametrize("failure", [OSError("disk unavailable"), yaml.YAMLError("bad sibling")])
def test_loop_write_failure_returns_json_and_preserves_offer(tmp_path, monkeypatch, capsys, failure):
    _data(tmp_path, monkeypatch)
    path, anchor = _tracked_loop(tmp_path)
    before = path.read_bytes()
    po.set_offer("commitment", "Done?", anchor_key=anchor)
    sys.path.insert(0, os.path.join(ROOT, "_shared", "knowledge"))
    import knowledge_edit

    def fail(*args):
        raise failure

    monkeypatch.setattr(knowledge_edit, "op_loop", fail)
    monkeypatch.setattr(sys, "argv", [SCRIPT, "dismiss-reply", "--text", "done"])
    po.main()
    result = json.loads(capsys.readouterr().out)
    assert result["action"] == "clarify" and result["loop_changed"] is None
    assert result["anchor_key"] == anchor
    assert path.read_bytes() == before
    assert po.get_offer()["anchor_key"] == anchor


def test_a_new_offer_can_be_written_during_loop_write_and_is_not_cleared(tmp_path, monkeypatch):
    """A real second process must be able to acquire the offer lock inside the ledger write.
    This reproduces the old nested-lock stall and also checks conditional cleanup afterwards.
    """
    _data(tmp_path, monkeypatch)
    path, anchor = _tracked_loop(tmp_path)
    po.set_offer("commitment", "Done?", anchor_key=anchor)
    sys.path.insert(0, os.path.join(ROOT, "_shared", "knowledge"))
    import knowledge_edit
    original = knowledge_edit.op_loop

    def write_while_a_new_question_arrives(anchor, target):
        p = subprocess.run([sys.executable, SCRIPT, "set", "--kind", "procedure",
                            "--question", "Save this newer rule?"], env=os.environ,
                           capture_output=True, text=True, timeout=5)
        assert p.returncode == 0, p.stderr
        return original(anchor, target)

    monkeypatch.setattr(knowledge_edit, "op_loop", write_while_a_new_question_arrives)
    result = po.dismiss_reply("done")
    assert result["action"] == "resolved" and result["loop_changed"] is True
    assert result["offer_cleared"] is False
    assert yaml.safe_load(path.read_text().split("---")[1])["status"] == "resolved"
    assert po.get_offer()["question"] == "Save this newer rule?"


def test_cleanup_failure_does_not_hide_a_successful_loop_write(tmp_path, monkeypatch):
    _data(tmp_path, monkeypatch)
    path, anchor = _tracked_loop(tmp_path)
    po.set_offer("commitment", "Done?", anchor_key=anchor)

    def fail_remove(*args):
        raise OSError("cannot remove offer")

    monkeypatch.setattr(po.os, "remove", fail_remove)
    result = po.dismiss_reply("done")
    assert result["action"] == "resolved" and result["loop_changed"] is True
    assert result["offer_cleared"] is False
    assert yaml.safe_load(path.read_text().split("---")[1])["status"] == "resolved"
    assert po.get_offer()["anchor_key"] == anchor


# ── the two processes, at the same moment ───────────────────────────────────────────────────────

_RACER = textwrap.dedent("""
    import importlib.util, json, sys, time
    spec = importlib.util.spec_from_file_location("pending_offer", {script!r})
    po = importlib.util.module_from_spec(spec); spec.loader.exec_module(po)
    mode, n = sys.argv[1], int(sys.argv[2])
    for i in range(n):
        if mode == "set":
            po.set_offer("meeting_prep", "q%d" % i, person="p%d" % i, detail="d" * 400)
        else:
            got = po.get_offer()
            if got:
                assert set(got) == {{"ts", "kind", "question", "person", "detail", "anchor_key",
                                     "payload_sha256", "expires_at", "action", "offer_id"}}, got
                assert got["question"] == "q" + got["person"][1:], got   # one whole write, not two halves
        time.sleep(0.001)
""")


def test_a_set_racing_a_get_never_hands_back_a_torn_offer(tmp_path):
    """The writer is the proactive lane and the reader is the gateway — different PROCESSES, so
    this races real ones. Same posture as the preferences.json race: jsonstore's flock across the
    read and the atomic write means a reader sees the previous whole offer or the next one, never
    a half-written question."""
    script = tmp_path / "racer.py"
    script.write_text(_RACER.format(script=SCRIPT))
    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    procs = [subprocess.Popen([sys.executable, str(script), mode, "60"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for mode in ("set", "get", "set", "get")]
    for p in procs:
        _out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err.decode()[-2000:]
    assert po.get_offer.__module__                      # sanity: the module under test loaded


def test_the_lock_is_jsonstores_and_not_a_second_one(tmp_path, monkeypatch):
    """One concept, one implementation: the sidecar this script locks is the one jsonstore names."""
    path = _data(tmp_path, monkeypatch)
    po.set_offer("meeting_prep", QUESTION, person="Shivani")
    sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
    import jsonstore                                    # noqa: PLC0415
    assert os.path.exists(jsonstore.lock_path(path))


# ── the surfaces that have to carry the same instruction ────────────────────────────────────────

def test_the_proactive_skill_tells_the_model_to_record_the_question(tmp_path):
    """A writer nobody calls is a file that is always empty — the SKILL.md is the only thing that
    calls `set`, so the invocation has to be IN it, with the reason."""
    with open(os.path.join(ROOT, "proactive", "SKILL.md"), encoding="utf-8") as f:
        skill = f.read()
    assert "pending_offer.py" in skill
    assert "set \\\n     --kind meeting_prep --person" in skill or \
           "--kind meeting_prep --person" in skill
    assert "--question" in skill
    assert "different session" in skill or "never saw your question" in skill
    assert "--payload-file" in skill and "--offer-bound" in skill
    assert 'pending_offer.py dismiss-reply --text "<actual user reply>"' in skill
    assert 'run **`sotto-loops`** §B `dismiss`' not in skill


def test_the_gateway_carries_the_standing_instruction(tmp_path):
    """The other half. The gateway loads SOUL.md, and start.sh appends sotto-persona.md to it every
    boot — so the bare-affirmative rule lives there, in the one file that already reaches it."""
    with open(os.path.join(HERMES, "adapters", "hermes", "sotto-persona.md"), encoding="utf-8") as f:
        persona = f.read()
    assert "pending_offer.py" in persona
    for affirmative in ('"yes"', '"sure"', '"ok"', '"go ahead"', '"do it"'):
        assert affirmative in persona
    assert "clear" in persona
    assert "sotto-meeting-prep" in persona
    assert "payload_sha256" in persona and "--offer-bound" in persona
    assert 'dismiss-reply --text "<actual user reply>"' in persona
    assert "Never bypass this handler" in persona
    assert '`no_offer` uses the existing **`{}` fallback above**' in persona
    assert 'inspect the anchored loop before any retry' in persona

    with open(os.path.join(HERMES, "adapters", "hermes", "start.sh"), encoding="utf-8") as f:
        start = f.read()
    assert "sotto-persona.md" in start and "pending_offer.py" in start


def test_start_sh_still_parses():
    """The persona block reaches the gateway only if the boot script runs at all."""
    p = subprocess.run(["bash", "-n", os.path.join(HERMES, "adapters", "hermes", "start.sh")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-2000:]
