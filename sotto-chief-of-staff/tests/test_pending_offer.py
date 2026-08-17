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


def test_the_stored_shape_is_exactly_the_six_ephemeral_fields(tmp_path, monkeypatch):
    """Ephemeral state, not memory: nothing here could be used by a brief three months from now,
    so nothing beyond the question and its clock is allowed to accumulate."""
    path = _data(tmp_path, monkeypatch)
    po.set_offer("chase", "any word on the deck?", person="Maya")
    with open(path, encoding="utf-8") as f:
        stored = json.load(f)
    assert set(stored) == {"ts", "kind", "question", "person", "detail", "expires_at"}


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


def test_cli_rejects_a_kind_no_lane_produces(tmp_path):
    env = dict(os.environ, SOTTO_DATA=str(tmp_path))
    p = subprocess.run([sys.executable, SCRIPT, "set", "--kind", "birthday",
                        "--question", "?"], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode != 0
    # Every kind has a producing lane: the proactive watcher's five, plus the evening brief's
    # "make that a standing rule?" confirmation (compose_brief._append_procedure_offer).
    assert set(po.KINDS) == {"meeting_prep", "commitment", "chase", "handoff", "retune_offer",
                             "procedure"}


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
                assert set(got) == {{"ts", "kind", "question", "person", "detail", "expires_at"}}, got
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

    with open(os.path.join(HERMES, "adapters", "hermes", "start.sh"), encoding="utf-8") as f:
        start = f.read()
    assert "sotto-persona.md" in start and "pending_offer.py" in start


def test_start_sh_still_parses():
    """The persona block reaches the gateway only if the boot script runs at all."""
    p = subprocess.run(["bash", "-n", os.path.join(HERMES, "adapters", "hermes", "start.sh")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-2000:]
