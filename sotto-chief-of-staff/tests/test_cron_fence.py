"""THE USER-ROUTINE FENCE, enforced against the real boot script.

`adapters/hermes/start.sh` removes every existing SYSTEM cron at boot and recreates it (the fix for
the duplicate-cron 429 storm). Personal routines created by the `sotto-routines` skill are crons
too — named `user-<slug>` — and a redeploy must leave them exactly where they are. That is a claim
about a shell script's embedded Python, so these tests run THAT python (extracted from start.sh, not
a copy of it) against a fake `hermes` CLI whose `cron list` / `cron remove` mutate a fixture.

Two directions, both load-bearing:
  - a `user-` job is never removed, even when its PROMPT quotes a system job's prompt verbatim;
  - a system job (including the RETIRED sotto-followup) still is — the fence must not neuter dedup.
Plus: the recreate guard downstream sees a system-only view of the list, so a routine can never
suppress the recreation of a system job (that guard also matches on prompt text).
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))            # sotto-chief-of-staff/
HERMES = os.path.dirname(ROOT)                              # sotto-hermes/
START_SH = os.path.join(HERMES, "adapters", "hermes", "start.sh")
CRONS_JSON = os.path.join(HERMES, "adapters", "hermes", "crons.json")

with open(START_SH, encoding="utf-8") as f:
    START = f.read()

# The fixture is shaped like `hermes cron list`: a hex job id opens each block, everything up to the
# next id belongs to it. Deliberately nasty: duplicate system jobs (the historical pile), the retired
# followup, and TWO user routines — one of which quotes "Run my morning brief" inside its prompt.
CRON_LIST_FIXTURE = """Scheduled jobs (6):

  a1b2c3d4e5f6  sotto-morning-brief
      Schedule: 30 6 * * *   Next run: 2026-08-10 06:30
      Prompt: Run my morning brief
      Skill: sotto-morning-brief   Deliver: whatsapp

  b1b2c3d4e5f6  sotto-morning-brief
      Schedule: 30 6 * * *   Next run: 2026-08-10 06:30
      Prompt: Run my morning brief
      Skill: sotto-morning-brief   Deliver: local

  c1b2c3d4e5f6  sotto-followup
      Schedule: 45 16 * * *   Next run: 2026-08-09 16:45
      Prompt: Run my followup
      Skill: sotto-followup   Deliver: whatsapp

  d1b2c3d4e5f6  user-open-loops-friday
      Schedule: 0 16 * * 5   Next run: 2026-08-14 16:00
      Prompt: Summarize my open loops grouped by person - who I owe, who I am waiting on.
      Deliver: whatsapp

  e1b2c3d4e5f6  user-morning-echo
      Schedule: 0 9 * * 6   Next run: 2026-08-15 09:00
      Prompt: Run my morning brief again on Saturday, short version.
      Deliver: whatsapp
"""

ID_RE = r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{12,})\b"

FAKE_HERMES = '''#!/usr/bin/env python3
"""Stand-in for the hermes CLI: `cron list` prints the state file, `cron remove <id>` drops that
block from it (so a second `cron list` in the same run reflects the removal, as the real one does)."""
import os, re, sys
STATE = os.environ["FAKE_CRON_STATE"]
LOG = os.environ["FAKE_CRON_LOG"]
ID = re.compile(%(id_re)s)
argv = sys.argv[1:]
if argv[:2] == ["cron", "list"]:
    sys.stdout.write(open(STATE, encoding="utf-8").read())
elif argv[:2] == ["cron", "remove"]:
    jid = argv[2]
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(jid + "\\n")
    text = open(STATE, encoding="utf-8").read()
    ms = list(ID.finditer(text))
    head = text[:ms[0].start()] if ms else text
    keep = [text[m.start():(ms[i + 1].start() if i + 1 < len(ms) else len(text))]
            for i, m in enumerate(ms) if m.group(1) != jid]
    open(STATE, "w", encoding="utf-8").write(head + "".join(keep))
else:
    sys.exit(0)
''' % {"id_re": repr(ID_RE)}


def _dedup_python() -> str:
    """The dedup step's Python, lifted out of start.sh verbatim (one heredoc, `PY`-fenced)."""
    m = re.search(r'python3 - "\$CRONS_JSON" "\$CRON_LIST_SYSTEM" <<\'PY\'[^\n]*\n(.*?)\nPY\n',
                  START, re.S)
    assert m, "start.sh no longer has the PY-fenced cron dedup block"
    return m.group(1)


def _run_dedup(tmp_path, listing=CRON_LIST_FIXTURE):
    """Run start.sh's dedup python with a fake hermes on PATH. Returns (stdout, removed_ids,
    remaining listing, system-only view written for the recreate guard)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "hermes"
    fake.write_text(FAKE_HERMES, encoding="utf-8")
    fake.chmod(0o755)
    state = tmp_path / "crons.txt"
    state.write_text(listing, encoding="utf-8")
    log = tmp_path / "removed.txt"
    log.write_text("", encoding="utf-8")
    script = tmp_path / "dedup.py"
    script.write_text(_dedup_python(), encoding="utf-8")
    sysview = tmp_path / "system-only.txt"
    sysview.write_text("", encoding="utf-8")          # mktemp's empty file, as start.sh creates it
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
               FAKE_CRON_STATE=str(state), FAKE_CRON_LOG=str(log))
    proc = subprocess.run([sys.executable, str(script), CRONS_JSON, str(sysview)],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    removed = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln]
    return (proc.stdout, removed, state.read_text(encoding="utf-8"),
            sysview.read_text(encoding="utf-8"))


def test_boot_cleanup_leaves_user_routines_alone(tmp_path):
    """The fence: neither `user-` job is removed — not even the one whose prompt quotes a system
    job's prompt word for word (that is exactly how a routine used to get swept away)."""
    out, removed, remaining, _ = _run_dedup(tmp_path)
    assert "d1b2c3d4e5f6" not in removed and "e1b2c3d4e5f6" not in removed
    assert "user-open-loops-friday" in remaining and "user-morning-echo" in remaining
    assert "leaving 2 user routine(s) alone" in out


def test_boot_cleanup_still_removes_every_system_job(tmp_path):
    """Positive control — the fence must not neuter dedup: both duplicate morning briefs AND the
    retired sotto-followup registration still go, which is the 429-storm fix this loop exists for."""
    _, removed, remaining, _ = _run_dedup(tmp_path)
    assert set(removed) == {"a1b2c3d4e5f6", "b1b2c3d4e5f6", "c1b2c3d4e5f6"}
    assert "sotto-morning-brief" not in remaining and "sotto-followup" not in remaining


def test_recreate_guard_sees_a_system_only_list(tmp_path):
    """start.sh's recreate `case` matches on name OR PROMPT text. Handed the raw post-dedup list, a
    routine quoting "Run my morning brief" would suppress the morning brief's recreation forever —
    so the dedup writes a system-only view and the guard reads that."""
    _, _, remaining, sysview = _run_dedup(tmp_path)
    assert "Run my morning brief" in remaining          # the routine is still there (fenced)
    assert sysview.startswith("# sotto:")               # always written, so `[ -s ]` can tell
    body = sysview.split("\n", 1)[1]
    assert "user-" not in body and "Run my morning brief" not in body


def test_recreate_guard_marker_line_keeps_the_fallback_honest(tmp_path):
    """"No crons at all" must not read as "the dedup never ran": the view is written with a marker
    line even when it holds nothing, so start.sh's `[ -s ]` test distinguishes the two — and when the
    dedup really does fail (nothing written, file empty) the guard falls back to the live list, so
    the anti-duplicate backstop survives the fence."""
    _, _, _, sysview = _run_dedup(tmp_path, listing="(no scheduled jobs)\n")
    assert sysview.startswith("# sotto:") and sysview.strip().count("\n") == 0
    assert re.search(r'if \[ -s "\$CRON_LIST_SYSTEM" \]; then\n\s+crons="\$\(cat "\$CRON_LIST_SYSTEM"\)"'
                     r'\nelse\n\s+crons="\$\(hermes cron list', START)


def test_start_sh_row_parsing_is_untouched(tmp_path):
    """The registrar's OWN parsing (crons.json → name/schedule/prompt/skill rows) is not what the
    fence changed: run start.sh's `cron_rows` python verbatim and get the five system jobs back,
    gates and schedule_env still honored."""
    m = re.search(r"cron_rows\(\) \{.*?python3 -c '(.*?)' \"\$CRONS_JSON\"", START, re.S)
    assert m, "start.sh's cron_rows helper changed shape"
    script = tmp_path / "rows.py"
    script.write_text(m.group(1), encoding="utf-8")
    env = dict(os.environ)
    env.pop("SOTTO_PROACTIVE", None)
    env.pop("SOTTO_DIGEST", None)
    env.pop("SOTTO_PROACTIVE_CRON", None)
    rows = subprocess.run([sys.executable, str(script), CRONS_JSON], capture_output=True,
                          text=True, env=env, timeout=30, check=True).stdout.strip().splitlines()
    parsed = [r.split("\t") for r in rows]
    assert [p[0] for p in parsed] == ["sotto-morning-brief", "sotto-evening-brief",
                                      "sotto-relationship-pulse", "sotto-proactive",
                                      "sotto-midday-digest"]
    assert all(len(p) == 4 for p in parsed)
    assert not [p for p in parsed if p[0].startswith("user-")]      # crons.json is SYSTEM-only
    env["SOTTO_DIGEST"] = "0"
    env["SOTTO_PROACTIVE_CRON"] = "*/30 * * * *"
    rows = subprocess.run([sys.executable, str(script), CRONS_JSON], capture_output=True,
                          text=True, env=env, timeout=30, check=True).stdout.strip().splitlines()
    parsed = {r.split("\t")[0]: r.split("\t")[1] for r in rows}
    assert "sotto-midday-digest" not in parsed and parsed["sotto-proactive"] == "*/30 * * * *"
