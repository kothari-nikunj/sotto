#!/usr/bin/env python3
"""Converge Hermes' Sotto system crons to adapters/hermes/crons.json.

Both container boot and live timezone changes call this file. Personal ``user-*`` routines are a
separate namespace and are never removed, matched, or recreated here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess

USER_PREFIX = "user-"
RETIRED_MARKERS = ("sotto-followup", "Run my followup")
ID = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{12,})\b")
USER_FENCE = re.compile(r"(?<![A-Za-z0-9_-])user-[a-z0-9]")


def _spec(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, list):
        raise ValueError("cron spec must be a JSON array")
    return [row for row in value if isinstance(row, dict)]


def active_jobs(path: str) -> list[tuple[str, str, str, str]]:
    jobs = []
    for row in _spec(path):
        name = str(row.get("name") or "")
        if not name or name.startswith(USER_PREFIX):
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


def reconcile(path: str, deliver: str = "whatsapp") -> bool:
    """Remove all managed/retired registrations, then recreate the enabled spec exactly once."""
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
    for name, schedule, prompt, skill in active_jobs(path):
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
    parser.add_argument("--deliver", default=os.environ.get("SOTTO_CRON_DELIVER", "whatsapp"))
    args = parser.parse_args()
    return 0 if reconcile(args.spec, args.deliver) else 1


if __name__ == "__main__":
    raise SystemExit(main())
