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

Fixtures test **machinery**. The **Golden Corpus** (`run_golden.py`, below) tests **judgment** on the
owner's real history — different instrument, same harness.

Everything writes only under a temp sandbox; the harness never mutates repo files. Stdlib only.

## Layout

```
evals/
  run_evals.py          the fixture harness (both modes) + the invariant checks
  run_golden.py         the Golden Corpus replay harness (§ Golden Corpus below)
  LABELING.md           the owner's one-hour labeling runbook
  fixtures/
    rich_day.json       busy day: 6+ meetings, 20+ emails, messages, loops, birthday, group chat
    quiet_day.json      1 meeting, 3 routine emails — the critic-auto-skip regime
    edge_day.json       adversarial: malformed ledger, unicode/emoji, phone-only id, empty Google, muted person, expired loop
  corpus/               NEVER COMMITTED, NEVER SHIPPED — built on the owner's machine only
  baselines/
    scores.json         written by `--live --update-baseline` (absent until you record one)
    golden-<version>.json  written by `run_golden.py --update-baseline`
  README.md
tools/
  build_golden_corpus.py  builds a corpus from $SOTTO_DATA (runs where the data lives)
tests/
  test_evals.py         the same invariants under pytest (CI-by-default)
  test_golden_corpus.py  the pseudonymizer + builder, end to end on synthetic data
  test_run_golden.py     the replay harness + its refusals
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

## The Golden Corpus

Synthetic fixtures test machinery. The corpus tests **judgment** — the actual noise-to-signal ratio,
group dynamics and voice of one real inbox. It is the owner's own history, pseudonymized with
referential integrity, frozen, and replayed through the real pipeline against labels he wrote once.
Design: `docs/plans/golden-corpus.md`.

```bash
# 1. BUILD — on the machine with the volume (the container / a Mac with $SOTTO_DATA)
SOTTO_DATA=/data python3 tools/build_golden_corpus.py --version corpus-v1 --days 42 \
    --user-email you@yourdomain --draft-labels-llm

# 2. LABEL — one hour, once per version. See evals/LABELING.md.

# 3. REPLAY
python3 evals/run_golden.py --sample 0                       # offline: funnel, loops, dedup
python3 evals/run_golden.py --live --judge --sample 5         # real briefs + pinned judge (~$1-2)
python3 evals/run_evals.py --golden --live --sample 5         # same thing via the fixture harness
```

**Scrubbing model.** Three PII layers, three treatments: *identities* (names/numbers/emails) get
HMAC-keyed pseudonyms with referential integrity — one human is one fake human across every channel
and every file, and one real domain is one fake domain, so colleagues stay colleagues; *in-text
references* are rewritten from the knowledge graph's own identifier index (it already knows every
name Sotto has seen — a better entity list than generic NER on this data), then swept for any
surviving address or number; *content* is kept, because scrubbing it destroys the only signal the
corpus exists to carry — anything genuinely too sensitive to keep is named with `--drop-sender` /
`--drop-thread` and never enters the corpus. The build refuses to write a corpus whose leak scan
still finds a known real string.

**Standing rule: "scrubbed" never means "shareable."** The corpus is confidential regardless. It is
gitignored, it drops a `*`-only `.gitignore` into itself, `tools/prepare-public-repo.sh` deletes it
and then fails the publish if it finds one (CORPUS GUARD), and the identity map lives on the volume
(`$SOTTO_DATA/corpus-keys/`) — outside any checkout, and never needed to run evals.

**What gets scored.** `funnel_agreement` + `nudge_prf` (the interrupt-budget tuning signal, from the
real event funnel), `entity_dedup` (resolved-people count vs. the identity map's ground truth — the
"Ben"/"Ben Butler" split shows up as a positive delta), `open_loops` (continuity correctness), and,
in `--live`, `triage_prf` plus a pinned LLM judge on fabrication / voice / triage. Baselines land in
`evals/baselines/golden-<version>.json`. Scores are only comparable **within** a corpus version.

## Developer-only environment variables

These exist for tests, evals and corpus builds — never for a deployer, so they are deliberately
absent from `RAILWAY.md`'s table (see CLAUDE.md § standing bars: *defaults matter*).

| Variable | Used by | What it does |
|---|---|---|
| `SOTTO_LLM_STUB` | `_shared/lib/gemini.py` and every composer | Replaces the Gemini call with a canned response so the pipeline runs offline and deterministically. The whole `--deterministic` eval lane and most of `tests/` ride it. |
| `SOTTO_JUDGE_MODEL` | `evals/run_golden.py` | Overrides the pinned LLM-judge model for a `--live` golden run. Pinning matters: judge drift is score drift. |
| `SOTTO_CORPUS_KEY` | `tools/build_golden_corpus.py` | The hex key that pseudonymizes real identities into the corpus. Lives on the volume (`$SOTTO_DATA/corpus-keys/`), never in a checkout. |
