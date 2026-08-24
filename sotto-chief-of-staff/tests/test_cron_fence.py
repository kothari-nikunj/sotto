"""System cron reconciliation and the user-routine fence."""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
HERMES = os.path.dirname(ROOT)
START_SH = os.path.join(HERMES, "adapters", "hermes", "start.sh")
CRONS_JSON = os.path.join(HERMES, "adapters", "hermes", "crons.json")
RECONCILER = os.path.join(HERMES, "adapters", "hermes", "reconcile_crons.py")
INSTALL_SH = os.path.join(HERMES, "adapters", "hermes", "install.sh")
CONFIG_TEMPLATE = os.path.join(HERMES, "adapters", "hermes", "config.template.yaml")
RAILWAY_MD = os.path.join(HERMES, "RAILWAY.md")

with open(START_SH, encoding="utf-8") as f:
    START = f.read()
with open(INSTALL_SH, encoding="utf-8") as f:
    INSTALL = f.read()
with open(CONFIG_TEMPLATE, encoding="utf-8") as f:
    CONFIG = f.read()
with open(RAILWAY_MD, encoding="utf-8") as f:
    RAILWAY = f.read()

CRON_LIST_FIXTURE = """Scheduled jobs (5):

  a1b2c3d4e5f6  sotto-morning-brief
      Prompt: Run my morning brief
  b1b2c3d4e5f6  sotto-morning-brief
      Prompt: Run my morning brief
  c1b2c3d4e5f6  sotto-followup
      Prompt: Run my followup
  d1b2c3d4e5f6  user-open-loops-friday
      Prompt: Summarize my open loops grouped by person.
  e1b2c3d4e5f6  user-morning-echo
      Prompt: Run my morning brief again on Saturday.
"""
ID_RE = r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{12,})\b"

FAKE_HERMES = """#!/usr/bin/env python3
import os, re, sys
state, log = os.environ["FAKE_CRON_STATE"], os.environ["FAKE_CRON_LOG"]
ids = re.compile(%r)
argv = sys.argv[1:]
if argv[:2] == ["cron", "list"]:
    sys.stdout.write(open(state, encoding="utf-8").read())
elif argv[:2] == ["cron", "remove"]:
    jid = argv[2]
    with open(log, "a", encoding="utf-8") as f:
        f.write("remove " + jid + "\\n")
    text = open(state, encoding="utf-8").read()
    matches = list(ids.finditer(text))
    head = text[:matches[0].start()] if matches else text
    keep = [text[m.start():(matches[i + 1].start() if i + 1 < len(matches) else len(text))]
            for i, m in enumerate(matches) if m.group(1) != jid]
    open(state, "w", encoding="utf-8").write(head + "".join(keep))
elif argv[:2] == ["cron", "create"]:
    with open(log, "a", encoding="utf-8") as f:
        f.write("create " + argv[argv.index("--name") + 1] + "\\n")
else:
    sys.exit(2)
""" % ID_RE


def _run_reconcile(tmp_path, listing=CRON_LIST_FIXTURE, extra_env=None):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "hermes"
    fake.write_text(FAKE_HERMES, encoding="utf-8")
    fake.chmod(0o755)
    state = tmp_path / "crons.txt"
    state.write_text(listing, encoding="utf-8")
    log = tmp_path / "calls.txt"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
               FAKE_CRON_STATE=str(state), FAKE_CRON_LOG=str(log))
    env.update(extra_env or {})
    proc = subprocess.run([sys.executable, RECONCILER, "--spec", CRONS_JSON],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout, log.read_text().splitlines(), state.read_text()


def test_reconciler_leaves_user_routines_alone(tmp_path):
    out, calls, remaining = _run_reconcile(tmp_path)
    assert not [call for call in calls if call in {
        "remove d1b2c3d4e5f6", "remove e1b2c3d4e5f6"}]
    assert "user-open-loops-friday" in remaining and "user-morning-echo" in remaining
    assert "leaving 2 user routine(s) alone" in out


def test_reconciler_replaces_system_jobs_and_retired_jobs(tmp_path):
    _, calls, remaining = _run_reconcile(tmp_path)
    assert {call for call in calls if call.startswith("remove ")} == {
        "remove a1b2c3d4e5f6", "remove b1b2c3d4e5f6", "remove c1b2c3d4e5f6"}
    assert "sotto-morning-brief" not in remaining and "sotto-followup" not in remaining
    created = {call.removeprefix("create ") for call in calls if call.startswith("create ")}
    assert created == {row["name"] for row in json.load(open(CRONS_JSON))}


def test_reconciler_honors_gates_and_schedule_source(tmp_path):
    _, calls, _ = _run_reconcile(tmp_path, listing="(no scheduled jobs)\n",
                                  extra_env={"SOTTO_DIGEST": "0", "SOTTO_PROACTIVE_CRON": "*/30 * * * *"})
    created = {call.removeprefix("create ") for call in calls if call.startswith("create ")}
    assert "sotto-midday-digest" not in created
    assert "sotto-proactive" in created


def test_start_sh_calls_the_shared_reconciler():
    assert "python3 /app/adapters/hermes/reconcile_crons.py" in START
    assert "--spec \"$CRONS_JSON\" --deliver \"$SOTTO_CRON_DELIVER\"" in START



def test_hermes_cron_delivers_sotto_copy_without_the_scheduler_envelope():
    """Cloud boot, local install, and the copyable config template all disable Hermes' default
    `Cronjob Response: ... (job_id: ...)` wrapper. Otherwise the native proactive fallback is the
    one Sotto lane that reads like a scheduler notification while Bridge-fired messages read clean."""
    assert "hermes_set_if_supported cron.wrap_response false" in START
    assert "hermes config get cron.wrap_response" in INSTALL
    assert "hermes config set cron.wrap_response false" in INSTALL
    assert re.search(r"(?m)^cron:\n\s+wrap_response:\s+false(?:\s|#|$)", CONFIG)
    assert "disables Hermes' generic `Cronjob Response` envelope" in RAILWAY
