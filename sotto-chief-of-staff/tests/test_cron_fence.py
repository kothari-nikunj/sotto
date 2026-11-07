"""The one system schedule (adapters/sotto/crons.json), its single reader and the user-routine fence."""
import importlib.util
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
AGENT_ROOT = os.path.dirname(ROOT)
CRONS_JSON = os.path.join(AGENT_ROOT, "adapters", "sotto", "crons.json")
RECEIVER = os.path.join(AGENT_ROOT, "runtime", "trigger-receiver", "receiver.py")

_spec = importlib.util.spec_from_file_location("cron_fence_receiver", RECEIVER)
rec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rec)

SYSTEM_JOBS = {"sotto-morning-brief", "sotto-evening-brief", "sotto-proactive",
               "sotto-midday-digest", "sotto-relationship-pulse"}


def test_the_spec_is_exactly_the_five_receiver_fired_system_jobs(monkeypatch):
    """The briefs, the proactive watcher, the midday digest and the weekly pulse all fire on the
    receiver's own clock (the silence seam is receiver-side, so a lane that can end with nothing
    has to be a receiver lane). No row names a second runner, and none is a personal routine."""
    for key in ("SOTTO_PROACTIVE", "SOTTO_DIGEST", "SOTTO_PROACTIVE_CRON"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SOTTO_CRONS_JSON", CRONS_JSON)
    spec = json.load(open(CRONS_JSON, encoding="utf-8"))
    assert {row["name"] for row in spec} == SYSTEM_JOBS
    for row in spec:
        assert set(row) <= {"name", "schedule", "prompt", "skill", "gate", "schedule_env"}, row
        # The receiver reads fixed daily/weekly times and */N intervals.
        assert re.match(r"\A(\d+ \d+|\*/\d+ \*) \* \* (\*|[0-6])\Z", row["schedule"]), row
    assert {name for name, *_ in rec._sotto_cron_jobs()} == SYSTEM_JOBS


def test_gates_and_schedule_source_are_honored_by_the_one_reader(monkeypatch):
    """`gate` and `schedule_env` are honored by the ONE reader every consumer shares — the
    receiver's tick and the dashboard's run-now list read exactly this list."""
    monkeypatch.setenv("SOTTO_CRONS_JSON", CRONS_JSON)
    monkeypatch.setenv("SOTTO_DIGEST", "0")
    monkeypatch.setenv("SOTTO_PROACTIVE_CRON", "*/30 * * * *")
    jobs = {name: schedule for name, schedule, _p, _s in rec._sotto_cron_jobs()}
    assert "sotto-midday-digest" not in jobs
    assert jobs["sotto-proactive"] == "*/30 * * * *"


def test_managed_and_self_host_read_the_same_system_jobs(monkeypatch):
    monkeypatch.setenv("SOTTO_CRONS_JSON", CRONS_JSON)
    monkeypatch.setenv("SOTTO_DEPLOYMENT_MODE", "managed")
    managed = rec._sotto_cron_jobs()
    monkeypatch.delenv("SOTTO_DEPLOYMENT_MODE")
    assert managed == rec._sotto_cron_jobs()
    assert {name for name, *_ in managed} == SYSTEM_JOBS


# ── the channel is resolved ONCE: no literal channel name may stand in for the answer ────────────

# Every place a delivery channel used to be guessed. Each takes the resolved value from the ONE
# decider (receiver._deliver_target) — a `whatsapp` fallback here was how a Telegram deploy could
# be delivered onto WhatsApp.
NO_FALLBACK = [
    os.path.join(AGENT_ROOT, "runtime", "trigger-receiver", "receiver.py"),
    os.path.join(AGENT_ROOT, "sotto-chief-of-staff", "setup", "SKILL.md"),
    os.path.join(AGENT_ROOT, "sotto-chief-of-staff", "routines", "SKILL.md"),
    os.path.join(AGENT_ROOT, "sotto-chief-of-staff", "relationship-pulse", "SKILL.md"),
]


def test_no_delivery_default_is_spelled_as_a_channel_name():
    for path in NO_FALLBACK:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for bad in (':-whatsapp', ':-telegram',
                    'SOTTO_CRON_DELIVER", "whatsapp"', "SOTTO_CRON_DELIVER', 'whatsapp'",
                    'deliver: str = "whatsapp"', 'deliver="whatsapp"'):
            assert bad not in text, f"{os.path.relpath(path, AGENT_ROOT)} still falls back to {bad!r}"
