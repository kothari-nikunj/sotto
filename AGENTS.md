# Sotto (sotto-agent) — working rules

## Installation and account setup
When a user asks to install or configure Sotto from this repository, use
[`docs/AGENT-ONBOARDING.md`](docs/AGENT-ONBOARDING.md) as the single agent-facing playbook.
Follow only its selected self-host or invited-Cloud path and the checks in that guide; do not
assume a hosted operator workflow is available to end users. Treat credentials, cloud account
selection, Google consent, macOS source grants, and provider purchase approval as user-owned steps.
Do not send messages or start a separate chat process as a probe; the supervised Sotto runtime
includes the Photon connection it needs. Report only checks actually observed, and preserve the
protected setup state for resume.

These working rules apply in the source subtree and the generated standalone tree.
Directory-relative commands and document paths start at the directory containing this `AGENTS.md`.
Installation or account-setup tasks still follow only the selected path and observed checks in
`docs/AGENT-ONBOARDING.md`.

This directory contains Sotto's shared Python skills, trigger receiver, Bridge integration,
and runtime adapters. Sotto runs the Node chat service under the root supervisor; the root
`Dockerfile` builds the self-host image. The receiver keeps source authority, scheduling,
accepted work, canonical memory, and delivery ownership. In a source checkout that contains
`docs/plans/ROADMAP.md`, that file is the ONE plan. A generated standalone tree omits maintainer
plans; do not invent a second roadmap or treat an absent maintainer document as an installation prerequisite.

## The docs-stay-true rule (owner directive, Aug 2026)

**Every behavior change ships with its documentation in the same commit.** The reader-facing
surfaces that must never drift from code:

| Surface | Update when |
|---|---|
| `docs/HOW-SOTTO-DECIDES.md` | any funnel gate, budget, cap, window, exemption set, or cadence changes |
| `docs/ARCHITECTURE.md` | a module, daemon thread, subprocess boundary, or shared `$SOTTO_DATA` file is added/removed/redirected |
| `docs/playground-architecture.html` | same triggers as the two docs above — the gate chain, simulator scenarios, and every numeric rule in it are claims about code |
| `docs/playground-feedback-loops.html` | a feedback loop's stations change, or a badge flips (CLOSED / RECORDING / PLANNED) — badges are computed claims; flipping one requires code evidence |
| `RAILWAY.md` env table + `adapters/sotto/.env.template` | any `SOTTO_*` env var is added, removed, changes default, or changes who sets it (Railway, the launch envelope, or nobody) |
| `adapters/sotto/crons.json` | the one system schedule changes; the fixed-five profiles admit only these five procedures |
| README docs table + `sotto-chief-of-staff/README.md` layout tree | a doc or script is added/removed |
| ROADMAP (when present in the source checkout) | mark shipped lines when merging; never leave a "still open" list unverified |

A drift-guard test (`sotto-chief-of-staff/tests/test_docs_drift.py`) compares the playgrounds'
embedded rules island — `<script type="application/json" id="sotto-rules">`, one shape in both
files, which the page JS interpolates everywhere a number appears — and HOW-SOTTO-DECIDES' stated
numbers against the code constants, field by field. A stale doc is a failing suite, not a hope:
change a knob and update the island in the same commit. The guard covers the numbers and the
exemption sets; the rest of this table is still yours to keep true, and a doc-less behavior change
is an incomplete change.

## Standing bars (from the owner, restated once)

- Every rule explainable in ONE sentence; the bar for complexity is high — this is open source and
  people should understand why they are nudged or not.
- One concept, one implementation, one writer. Docstrings that say "MUST mirror X" are bugs —
  extract the single source instead.
- Defaults matter: a new env var needs a reason a default can't serve. Credentials and real user
  choices only; tuning lives in named constants (owner, Aug 2026).
- Draft by default. External sends require a complete review and a distinct owner confirmation through the attended tools; never send autonomously. Declines are `review` tier forever.
- A chief of staff does the work quietly and cares about OUTCOMES — it never performs activity.
  No labeling, no busywork theater, no counting emails as accomplishment (owner, Aug 2026: "a CoS
  doesn't data label"). Report what moved: chased, resolved, held, prepped.
- **Every token spent on research must leave behind a durable, structured, correctable fact — and
  nothing situational is ever stored.** Durable = who someone is, what a company builds, how two
  people are connected: it goes in the graph, with provenance, editable from chat and the dashboard
  through one path. Situational = talking points, angles, openers, "what to say": it is regenerated
  free from durable facts every time and is stored NEVER — and where it was being *requested*, we
  stopped asking. Not everything gets wired up (owner, Aug 2026): a path that produces situational
  output is rightly ephemeral, not a leak. The test for storing a fact: **could a brief or a prep
  three months from now USE it?** A launch announcement that ages out in a week could not.
- Fail toward silence in pipelines, but a source that breaks must SAY so in the brief.
- Verification: `python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt` once, then
  `.venv/bin/python sotto-chief-of-staff/tools/verify.py` from the directory containing this `AGENTS.md`:
  `sotto-agent/` in the source checkout, or the repository root in the generated standalone tree
  (source checkout: `ship.sh` uses `.venv` too). This is the shared backend/release
  gate used by local shipping and CI: Ruff, pipeline/receiver/adapter/proxy tests and available account tests, skill
  validation, shell parsing and available publication guards. It must pass before a merge touching
  distributable files. Bridge/macOS verification remains a separate platform step. Run publication
  guard suites serially with source edits paused: their synthetic planted files exercise the guard.
