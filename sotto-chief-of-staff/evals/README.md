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

The offline fixture and tracking probes write only under temporary sandboxes and use the pack's
Python dependencies.

## Layout

```
evals/
  run_evals.py          the fixture harness (both modes) + the invariant checks
  run_golden.py         the Golden Corpus replay harness (§ Golden Corpus below)
  replay_tracking.py    offline continuous month: current tracking vs the proposed contract
  run_memory_evidence.py  isolated current/grounded Dreamer comparison; scripted default, explicit live mode
  memory_evidence.py    experiment-only evidence binding, never imported by production Dreamer
  inspect_source_coverage.py  offline receiver/state/prompt coverage inspection with forbidden external calls
  LABELING.md           optional maintainer judgment-evaluation runbook
  fixtures/
    rich_day.json       busy day: 6+ meetings, 20+ emails, messages, loops, birthday, group chat
    quiet_day.json      1 meeting, 3 routine emails — the critic-auto-skip regime
    early_prep_day.json  54-hour substantive prep versus a routine nearer meeting; existing offer/action path
    edge_day.json       adversarial: malformed ledger, unicode/emoji, phone-only id, empty Google, muted person, expired loop
    memory_evidence.json  invented fact/source pairs with separately held verdict labels
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
  test_replay_tracking.py  continuous tracking probe isolation, determinism and honest exit status
  test_memory_evidence_eval.py  source/fact binding, protected owner facts and isolated curation readback
```

## Run it

Start from the Sotto source root (`sotto-agent/` in the monorepo, or the standalone repository root):

```bash
cd sotto-chief-of-staff

# offline invariant scorecard (default; exits nonzero on any failure)
python3 evals/run_evals.py            # or: --deterministic

# the same checks under pytest
python3 -m pytest tests/test_evals.py -q
```

## Fixtures — deterministic time

### Source-grounded memory experiment

```bash
# Offline contract rehearsal; no source access or model calls.
python3 evals/run_memory_evidence.py --output /tmp/memory-contracts.json
python3 -m pytest tests/test_memory_evidence_eval.py -q

# Explicit paid comparison, using an already configured native provider credential.
python3 evals/run_memory_evidence.py --live --model gemini/gemini-3.8-flash \
  --repeat 3 --output /tmp/memory-live.json
```

This compares the real Dreamer and `knowledge_update.consolidate` writer against an experimental
model seam that also receives the exact cited **invented** messages. Each arm/case/sample gets a
fresh temporary data directory, ignoring inherited `SOTTO_DATA`. Sources are a fixed fixture
catalog, never Gmail, Bridge or a user's transcripts. The harness reads the committed graph,
checks factual/provenance preservation, and repeats curation to check that unchanged work makes
no new model call or person-file write. This is same-process readback, not crash/restart proof.

The grounded arm requires supported, contradicted, inconclusive, unavailable or owner-protected
judgments for every fact. Checks bind to exact fact versions and source-record hashes with
nonempty verbatim quotes; code requires a unique exact match, including overlapping occurrences, and derives Unicode-code-point
offsets, retaining offsets rather than duplicated source text in the report. Missing, revoked,
wrong-person, obsolete, partial, ambiguous
or over-budget evidence is withheld whole. A mixed-support fact is unavailable if any required
source cannot be supplied. Owner corrections remain protected. Facts assessed as contradicted,
inconclusive or unavailable cannot enter summary selections or duplicate groups; the experiment
**does not archive or rewrite them**.
An empty summary does not suppress those facts from other graph readers.

`memory_evidence.py` holds only experiment contracts. It is not imported by the receiver or
production Dreamer, and no verification status is persisted as canonical truth. The model sees
no expected verdicts or fixture labels. Binding validates citations, not semantic entailment;
the separately held fixture labels grade the model's judgments and selections. Source reference
and fact IDs do not encode the expected verdict. Reports retain validated versions and spans.

The default adapter is explicitly scripted, so its passing result proves machinery, not improved
accuracy. `judgment_measured` is false, aggregate `quality` is null, and latency/usage are unknown.
In live mode both arms use the same explicit native model, one attempt per arm/case, without
fallback or a separate judge. Record every failed sample. Reports include actual call latency,
prompt/system UTF-8 bytes at the experiment seam, before the transport adds writing style
(excluding that style suffix, schema and transport overhead), response bytes and shared native
usage receipts when available; absent receipts remain null. Semantic judgments are counted
separately from unavailable/owner-protection policy checks. Correct policy counts are validator
invariants for completed samples, not measured model accuracy. The report fingerprints its fixture,
experiment, Dreamer, canonical writer/reader, native client, transport, writing style, model-work
accounting and metrics files; these are selected-file hashes,
not a full repository/configuration snapshot. Selected inputs are copied once and fingerprinted
for all repeats. Repeated samples alternate arm order.
`--case` selects fixture IDs; `--repeat` accepts 1–10. Output files must be new. Credential preflight precedes exclusive file creation; creation still
precedes paid contact. Reports are written only after all samples finish. An interruption can
leave an empty file and lose completed samples; there is no automatic resumption or retry of paid
attempts. Preserve any report and reconcile attempted calls before manually starting a new run. A full one-repeat
run makes at most two model requests per fixture; an unchanged within-arm repeat makes none.

Exit 1 means a contract/error occurred in either arm, or a live grounded judgment/selection missed
its label. Baseline judgment quality is diagnostic, so the current arm need not pass the grounded
quality gate. Eligible summary omissions are diagnostic, since Dreamer may choose fewer facts
or an empty summary. Every grounded status must still match its label, and unsupported selections
still fail. A report has no end-to-end answer-quality, production-speed or statistical claim.
Offline socket attempts and all subprocess attempts fail the run, including caught exceptions;
live sockets are admitted only during the explicit native model call.


### Source coverage inspection

```bash
python3 evals/inspect_source_coverage.py > /tmp/sotto-source-coverage.json
python3 -m pytest tests/test_inspect_source_coverage.py -q
```

The JSON exposes invented receipts, retained state and the actual brief-model coverage input,
including partial usable parent data, a failed companion, failed-read and health-probe freshness,
empty authorized Google reads, learner read-versus-extraction timestamps, and revocation.
The script ignores inherited `SOTTO_DATA`, uses a temporary store, and fails on any network or
subprocess attempt, even if production code catches the exception. The companion TS authority
test passes these receiver-inspected receipts through the production chat-tool projection.
This verifies synthetic boundaries; it does not call a model, a provider or a Mac.


### Continuous tracking-contract probe

```bash
python3 evals/replay_tracking.py
python3 -m pytest tests/test_replay_tracking.py -q
```

This separate probe advances every day from 0 through 28 in one throwaway data directory per run,
and reports persisted-state assertions at days 0, 1, 7, 15 and 28. It calls the real resolver, draft
grader, reminder finalizer and reply handler with a fixed clock. The inputs are invented examples
based on failure shapes from the September 4 investigation, not an exported or pseudonymized
private corpus. It ignores inherited `SOTTO_DATA`, restores the environment and makes no network
calls. No labeling session is needed.

**Exit 1 means a contract is unmet.** The report lists each expected and observed result; it is
a diagnostic, not a quality score. The pytest gate covers every replay contract, including the
first-week reminder-versus-chase distinction. No known failure is exempted. There is no legacy mode: seeding old rows
without running a migration adds no migration assurance.
The JSON report includes every expected/observed result and five persisted-state snapshots. It
does not evaluate extraction quality, onboarding, actual brief/dashboard rendering, adapter reply
IDs, or future migration. The reminder selection budget is bypassed to isolate the meaning of a
delivered reminder.


### Single-day fixture clocks

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
| `SOTTO_JUDGE_MODEL` | `evals/run_golden.py` | Overrides the pinned LLM-judge model for a `--live` golden run (blank means the pinned default). Pinning matters: judge drift is score drift. The judge call ignores `SOTTO_BRIEF_MODEL` and any fallback, a live compose runs without `SOTTO_FALLBACK_MODEL` (a quota blip fails the day rather than scoring another model), and a live baseline records its compose and judge models: a run on other models is reported as a mismatch, so baselines recorded before this need re-recording once. |
| `SOTTO_CORPUS_KEY` | `tools/build_golden_corpus.py` | The hex key that pseudonymizes real identities into the corpus. Lives on the volume (`$SOTTO_DATA/corpus-keys/`), never in a checkout. |

The optional browser label editor remains at `/app#labels`; it is a maintainer tool, absent from
the main dashboard navigation. It is not setup or a recurring task for Sotto users.


## Shared relevance judgment

`python3 evals/run_relevance.py --live --output /tmp/relevance-results.json` evaluates the actual
event classifier, digest review/selection, and native brief extraction against eight invented
examples modeled on reported failures: an outstanding preschool waiver, a donation blast, VIP
chatter, an unanswered invitation, and contrasting completed/accepted/vague/personal-commitment
cases. These are authored fixtures, not an exported or pseudonymized inbox. Without `--live`, the
command lists cases and explicitly reports that judgment was not evaluated; there is no fake
accuracy score from stubbed responses.

The probe pins the same clock for every surface and uses a temporary state directory, sends no messages, performs no learning or account
writes, and uses the existing provider settings. It makes ten model calls (eight event judgments,
one batched digest review, one brief extraction). It exits nonzero on any mismatch. Brief scoring
checks the rendered attention sections, allowing substantial completed outcomes in Already Handled. The event
counterexamples explicitly supply thread context: this tests judgment given evidence, not the
completeness of realtime context gathering. It does not replace the broader private Golden Corpus.

### Phase-2 proof and first-week checks

`python3 _shared/scripts/release_one_proof.py --days 30` reads retained volume state without a model
call or write. It reports coverage and unavailable correlations explicitly. Capture this baseline
before deploying ranking changes; a receipt lacking the new exact proposal field is unknown.

The continuous replay now adds two positive evidence-bound completion cases and a first-week
gate under `tests/test_replay_tracking.py`. A delivered reminder is described as a reminder to the
user, not evidence of contacting the other person. All first-week and full-month contracts must
pass. These synthetic checks do not measure live model quality.


### Golden replay coverage

`run_golden.py` reuses production consent projection, the protected current ledger, critic policy
in live mode, and the canonical resolver for reviewed task updates. Dry mode blocks model work
and measures deterministic contracts only. Use `--sample 0` to replay every intervening day;
a sampled run preserves selected-day ordering but is not full longitudinal coverage.

Every run reports `COVERAGE` with expected/executed/failed/unlabeled case IDs and the cases scored
for each metric. An execution failure (including an unavailable requested judge), missing metric,
changed case coverage, mode, effective live critic policy, or compose/judge model cannot yield
quality PASS; the mismatch lists each run field that differs from the baseline's. Dry replay
records critic as disabled regardless of the environment. Failed or scoreless runs cannot overwrite
a baseline. Older baselines without case coverage require an explicit `--update-baseline` after
review; the harness never silently upgrades them. Synthetic scripted-critic regression tests are
contract evidence, not measured current-model accuracy.
The regression `--threshold` must be finite and between 0 and 1; an invalid value is rejected
before replay or baseline comparison.


Golden metric policy `active-states-and-absolute-entity-error-v2` includes open, waiting, blocked
and failed obligations in remaining-work scoring. Entity deduplication reports mean absolute daily
count error, so opposite errors cannot cancel across days. Baselines created under earlier metric
semantics require explicit reviewed replacement; synthetic regression results are not model quality.

## Attended conversation journeys

The [actual-model conversation runner](../../runtime/sotto-chat/evals/README.md) exercises multi-turn chat, skill loading, receiver admission, queued research and final delivery with synthetic source/transport boundaries. It complements these brief evals; neither claims unexercised capabilities passed.

### Chronological retained-history replay

Build with `tools/build_golden_corpus.py --chronological` (alongside the existing required
arguments) to replay all retained days in order at day end. This mode omits today's
ledger, preferences, Contacts cards and reminders. It refuses day sampling and nonempty
current-state seeds; the ordinary snapshot mode remains available for output comparisons.

This removes current-state seeding, not every historical limitation: retained messages
are their current versions, not an edit/unsend archive, and current identifiers are
used only to pseudonymize identities. Review labels before calling a run acceptance.
Never publish the corpus or identity map.

Chronological event replay runs before that day's composition and learning, sorts inbound
events across sources by their observed timestamps, and bounds conversation context to
each event's time. Later outbound replies become context only for later events. Missing
inbound timestamps fail the replay instead of guessing an order. The run signature records
this replay policy; baselines from the previous ordering require reviewed replacement.

## Historical source correction inspection

The synthetic automatic-admission/worker inspection was retired with that dormant runtime.
Versioned native reads, source authority fences and graph provenance projections retain their
unit coverage. Historical ledgers remain untouched; no automatic edit/unsend withdrawal is enabled.

### Advance meeting preparation fixture

`early_prep_day` supplies two invented external calendar events: a routine check-in one hour
away and a decision review 54 hours away whose invitation explicitly requests comparing three
rollout plans and preparing migration-risk questions. The existing useful-help writer and critic
are instructed to consider concrete advance work within the current 72-hour calendar window.
The deterministic fixture supplies its extraction; the real composer must preserve its exact
calendar action/source, attach the concise briefing offer to the source meeting title beside the visible full attendee
name and task clause, keep the nearer meeting only under the source-generated Coming Up
heading without a prep offer or optional action sectionType, and create no
ledger obligation from either calendar action. It verifies rendering and continuity machinery,
not whether a model chooses the right meeting or writes useful preparation. The production
renderer also withholds a prep offer unless complete calendar coverage proves one exact source
event/start; equal titles do not override that occurrence proof, and human-readable action time is display text. The action and
preparation paragraph remain visible when the offer cannot be proved safe. A single rendered
prep offer can join the existing accepted-delivery offer mechanism; mixed actionable offers
remain ambiguous to a generic yes, and retry preserves the original expiry.

Actual-model acceptance remains pending. Use this same fixture input and the existing brief
composer/critic with the approved candidate model configuration and allowance; inspect the
complete resulting narrative/actions, not only a critic score. The meaningful judgment is that
the evidenced comparison earns advance attention while the routine nearer meeting does not.
Require faithful dates, concrete work/reason, source/occurrence binding, no invented urgency or
commitment inventory, and a usable existing prep offer. The existing `--live` mode scores all
fixtures and uses its configured native model path; it does not by itself qualify the approved
proxy configuration, an owner request, provider delivery or a device's presentation. No model
was called to author or validate this fixture.
