# Brief-quality evals

An eval harness for the morning/evening brief pipeline. It exercises the **real** pipeline pieces
(`normalize → continuity resolve → build_prompt → compose → tap-link post-processing`) against
self-contained golden "day" fixtures and asserts **invariants** — structural guarantees, not exact
prose — so brief quality can't silently rot.

Two modes:

| Mode | Network | Speed | What it does |
|------|---------|-------|--------------|
| `--deterministic` (default) | none | <5s | Runs the pipeline with a stub LLM into a throwaway `$SOTTO_DATA` sandbox and asserts named invariants. Exits nonzero on any failure. Mirrored by `tests/test_evals.py` so it's CI-guarded by default. |
| `--live` | Gemini | ~1 min | Runs the **real** Gemini extraction per fixture and scores each brief with the pack's own brief critic. Compares to a stored baseline and fails on a regression. Human-invoked only — never in CI. |

Everything writes only under a temp sandbox; the harness never mutates repo files. Stdlib only.

## Layout

```
evals/
  run_evals.py          the harness (both modes) + the invariant checks
  fixtures/
    rich_day.json       busy day: 6+ meetings, 20+ emails, messages, loops, birthday, group chat
    quiet_day.json      1 meeting, 3 routine emails — the critic-auto-skip regime
    edge_day.json       adversarial: malformed ledger, unicode/emoji, phone-only id, empty Google, muted person, expired loop
  baselines/
    scores.json         written by `--live --update-baseline` (absent until you record one)
  README.md
tests/
  test_evals.py         the same invariants under pytest (CI-by-default)
```

## Run it

```bash
cd sotto-hermes/sotto-chief-of-staff

# offline invariant scorecard (default; exits nonzero on any failure)
python3 evals/run_evals.py            # or: --deterministic

# the same checks under pytest
python3 -m pytest tests/test_evals.py -q
```

## Fixtures — deterministic time

Fixtures never hard-code wall-clock dates. Timestamps are **relative tokens** resolved at load
against a single base datetime (noon UTC "today", captured once), so a fixture produces the **same
invariants every run** regardless of when it runs — the same determinism `test_continuity_resolve`
gets by pinning `today` + a fixed `now`.

Tokens (optional `±Nh` / `±Nd` / `±Nm` offset):

| Token | Renders | Used for |
|-------|---------|----------|
| `{{D}}`, `{{D-8d}}` | `YYYY-MM-DD` | dates, `created_at`, reminder due dates |
| `{{TS}}`, `{{TS-5h}}` | `YYYY-MM-DD HH:MM:SS` | Bridge-style message / ledger timestamps |
| `{{ISO}}`, `{{ISO+3h}}` | `YYYY-MM-DDTHH:MM:SS+00:00` | Gmail dates, calendar event starts |
| `{{MD+3d}}` | `MM-DD` | Apple-Contacts birthdays |

## Invariants asserted

The exact set is the `CHECKS` table in `run_evals.py`; the scorecard prints every one. Highlights:

- **muting** — muted senders and muted people are absent from the rendered prompt sections (with a
  positive control that a real email survives); the muted person is restated in the "do NOT surface"
  instruction so the model can't re-add them.
- **continuity** — the loop that was answered on another channel is `resolved`; the fresh one is
  `active`; the 10-day-old one is `expired`; the malformed ledger file is **byte-identical** after
  the run (skipped, never persisted over).
- **tap-links** — every action link uses a safe universal scheme (`mailto:` / `sms:+` / `tel:+` /
  `wa.me` / Gmail / Google Calendar / Meet) and a group chat action **never** gets an invented deep
  link.
- **coverage** — the coverage line names exactly the sources that have data (empty Google → "Link
  Gmail + Calendar").
- **critic decision** — `SOTTO_CRITIC=auto` runs the critic on the busy brief and skips it on the
  quiet one.
- **robustness** — unicode/emoji/CJK survive rendering; a phone-only contact resolves to a name;
  the adversarial `edge_day` completes with no exception.

## Add a fixture

1. Copy an existing `fixtures/*.json`. A fixture has:
   - `inputs` — a full compose input (`type`, `google`, `granola`, `local`, `first_run`, …) using
     the time tokens above.
   - `preferences` — the `explicit` mute/tone block (written to `preferences.json` in the sandbox).
   - `continuity_ledger` — seed ledger files: `{ "filename", "frontmatter": {...} }`, or a raw
     malformed one: `{ "filename", "malformed": true, "raw": "..." }`.
   - `signals` / `new_actions` — passed to `continuity_resolve.resolve`.
   - `stub_response` — the canned extraction the stub LLM returns (`brief_markdown`, `actions[]`,
     `extracted_knowledge`). Give `actions` the channels/identifiers you want tap-links checked on.
2. Add the fixture name to `FIXTURES` in `run_evals.py` and a `CHECKS[name]` list of invariant
   functions (reuse the common ones; write fixture-specific ones alongside them).
3. `python3 evals/run_evals.py` and `python3 -m pytest tests/test_evals.py -q`.

## Live baseline workflow

`--live` needs `GOOGLE_AI_API_KEY` (the host's native Gemini key). It reuses `compose_brief`'s own
`run_critic` for scoring — no separate scorer.

```bash
# record the first baseline (deliberate)
GOOGLE_AI_API_KEY=… python3 evals/run_evals.py --live --update-baseline

# later: score again and fail if any fixture drops > threshold points (default 1.0)
GOOGLE_AI_API_KEY=… python3 evals/run_evals.py --live --threshold 1.0
```

The baseline `scores.json` records `{fixture: {score, date, model}}`. Re-run `--update-baseline`
whenever you intentionally accept a new quality level (e.g. after a model or prompt change).

**Where it lives:** when `SOTTO_DATA` is set (the cloud container), the baseline is written to
`$SOTTO_DATA/evals/baselines/scores.json` — the skills tree there is read-only and `start.sh` wipes
it every boot, so a repo-local baseline would never survive to arm the regression gate. In a local
dev checkout (`SOTTO_DATA` unset) it falls back to the repo-local `evals/baselines/scores.json` shown
in the layout above, so the dev workflow is unchanged.
