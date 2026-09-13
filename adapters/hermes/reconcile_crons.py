#!/usr/bin/env python3
"""Converge Hermes' Sotto system crons to adapters/hermes/crons.json.

Both container boot and live timezone changes call this file. Personal ``user-*`` routines are a
separate namespace and are never removed, matched, or recreated here.

A row marked ``"runner": "receiver"`` is scheduled by the trigger receiver, not by Hermes: it is
never registered here, but it IS in the removal markers, so an existing deployment's Hermes copy of
that job is stripped on the next boot.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess

USER_PREFIX = "user-"
RETIRED_MARKERS = ("sotto-followup", "Run my followup")
# crons.json's optional `runner`. "receiver" = the trigger receiver schedules AND delivers this job
# through its outbox (retries, the deliver-once gate); absent = Hermes runs it. The briefs are
# receiver-run so a brief has ONE delivery lane.
HERMES_RUNNER = "hermes"
RECEIVER_RUNNER = "receiver"
ID = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{12,})\b")
USER_FENCE = re.compile(r"(?<![A-Za-z0-9_-])user-[a-z0-9]")


def _spec(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, list):
        raise ValueError("cron spec must be a JSON array")
    return [row for row in value if isinstance(row, dict)]


def active_jobs(path: str, runner: str | None = None) -> list[tuple[str, str, str, str]]:
    """The enabled system jobs as (name, schedule, prompt, skill), honoring `gate`/`schedule_env`.

    `runner` selects which half of the spec you want: None = every enabled job (the receiver's
    read-only views), HERMES_RUNNER = the jobs Hermes registers, RECEIVER_RUNNER = the jobs the
    trigger receiver fires on its own clock. This is the ONE gate/override implementation — every
    consumer filters the same list rather than re-reading the file its own way."""
    jobs = []
    for row in _spec(path):
        name = str(row.get("name") or "")
        if not name or name.startswith(USER_PREFIX):
            continue
        managed = os.environ.get('SOTTO_DEPLOYMENT_MODE') == 'managed'
        job_runner = str((row.get('managed_runner') if managed else None)
                         or row.get('runner') or HERMES_RUNNER).strip()
        # Managed system jobs use the receiver's activation/source gates and outbox.
        # A managed override preserves the existing self-host scheduler assignment.
        if managed and job_runner != RECEIVER_RUNNER:
            continue
        if runner and job_runner != runner:
            continue
        gate = row.get("gate")
        if gate and os.environ.get(str(gate), "1") != "1":
            continue
        schedule = os.environ.get(str(row.get("schedule_env") or ""), "") or row["schedule"]
        jobs.append((name, schedule, row["prompt"], row["skill"]))
    return jobs


def blocks(text: str):
    matches = list(ID.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        yield match.group(1), text[match.start():end]


def reconcile(path: str, deliver: str) -> bool:
    """Remove all managed/retired registrations, then recreate the enabled spec exactly once.

    `deliver` has no default on purpose: the channel is resolved ONCE (start.sh step 0.4, mirrored by
    receiver._deliver_target) and passed in, so a fallback here could only ever be a second, wrong
    answer to a question already decided."""
    try:
        rows = _spec(path)
        listed = subprocess.run(["hermes", "cron", "list"], capture_output=True, text=True,
                                timeout=60)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"[sotto] cron reconcile skipped: {exc}", flush=True)
        return False
    if listed.returncode != 0:
        print(f"[sotto] cron reconcile skipped: `hermes cron list` exited {listed.returncode}", flush=True)
        return False

    # Markers come from EVERY non-user row, including the receiver-run ones this reconciler will
    # not register: leaving them here is what removes an existing deployment's stale Hermes brief
    # crons on the next boot, so the migration to the single delivery lane needs no manual step.
    markers = [*RETIRED_MARKERS]
    for row in rows:
        name, prompt = str(row.get("name") or ""), str(row.get("prompt") or "")
        if name and not name.startswith(USER_PREFIX):
            markers.extend((name, prompt))

    managed_ids, fenced = [], 0
    for job_id, block in blocks(listed.stdout):
        if USER_FENCE.search(block):
            fenced += 1
        elif any(marker and marker in block for marker in markers):
            managed_ids.append(job_id)

    print(f"[sotto] cron reconcile: replacing {len(managed_ids)} system job(s)"
          + (f"; leaving {fenced} user routine(s) alone" if fenced else ""), flush=True)
    for job_id in dict.fromkeys(managed_ids):
        try:
            removed = subprocess.run(["hermes", "cron", "remove", job_id], input="y\ny\n",
                                     capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[sotto] cron reconcile stopped: remove {job_id} failed: {exc}", flush=True)
            return False
        if removed.returncode != 0:
            print(f"[sotto] cron reconcile stopped: remove {job_id} exited {removed.returncode}", flush=True)
            return False

    ok = True
    for name, schedule, prompt, skill in active_jobs(path, HERMES_RUNNER):
        try:
            created = subprocess.run(
                ["hermes", "cron", "create", schedule, prompt, "--skill", skill,
                 "--name", name, "--deliver", deliver],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[sotto] cron create {name} failed: {exc}", flush=True)
            ok = False
            continue
        if created.returncode != 0:
            print(f"[sotto] cron create {name} exited {created.returncode}: {created.stderr[:200]}",
                  flush=True)
            ok = False
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    # Required, not defaulted: boot exports the resolved channel and passes it here explicitly.
    parser.add_argument("--deliver", required=True)
    args = parser.parse_args()
    return 0 if reconcile(args.spec, args.deliver) else 1


if __name__ == "__main__":
    raise SystemExit(main())
