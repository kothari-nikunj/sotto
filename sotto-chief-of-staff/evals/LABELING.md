# The labeling session — one hour, once per corpus version

Everything else in the Golden Corpus is machine-made. This is the part only the owner can do: say
what the right answer *was*. The build already drafted every label; you are correcting a draft, not
authoring one, because correction is roughly five times faster than authorship.

Budget **one hour**. Do it in the same sitting as the Editor's interrupt-budget tuning — the "which
of these deserved a nudge" question is literally the same question, asked once.

## Before you sit down

```bash
# On the machine with the volume (the container, or a Mac with $SOTTO_DATA):
# 1. Backfill Gmail + Calendar for the whole window (the daily gather only keeps 1 day; wide
#    windows are fetched in date slices past the CLIs' page caps). Dedicated output files —
#    the daily brief cron overwrites the default /tmp paths underneath you:
python3 _shared/scripts/gather_google.py --window-days 42 --max 500 --bodies 60 \
    --gmail-out /tmp/corpus_gmail.json --cal-out /tmp/corpus_cal.json
# 2. Build from those files:
SOTTO_DATA=/data python3 tools/build_golden_corpus.py --version corpus-v1 --days 42 \
    --gmail /tmp/corpus_gmail.json --calendar /tmp/corpus_cal.json \
    --user-email you@yourdomain --draft-labels-llm
```

Message depth is different: iMessage/WhatsApp history comes from the dated snapshot archive
(`knowledge/snapshots/`), which accumulates one file per day from the moment this version deploys.
A corpus built before the archive has grown holds only ~2 days of messages — that first corpus is
still worth labeling (email + calendar carry most `needs_attention` truth), and a later
corpus-v2 gets the full message window for free.

That writes `evals/corpus/corpus-v1/` (days + manifest + a drafted `labels.yaml`) and the identity
map to `$SOTTO_DATA/corpus-keys/corpus-v1.map.json`. Check the run said `LEAK SCAN: PASS` — if it
didn't, nothing was written. Re-run with `--leak-context` to see each surviving string in place
(real data — your terminal only): a hit is either a person the graph doesn't know yet (add them and
rebuild) or a field the scrub genuinely missed (a bug — report it). Never `--allow-leaks`. A
stop-listed bare first name ('Grace', 'Mark') surviving in prose is by design and is not scanned.

If a thread is genuinely too sensitive to keep even pseudonymized, name it and it never enters the
corpus at all — `--drop-sender them@theirdomain` or `--drop-thread <gmail-threadId>`, repeatable,
matched against the real identifiers before the scrub runs.

Keep the map. Rebuilding with the same key produces the same aliases, so **scores stay comparable
across rebuilds of the same corpus version**. Losing it means the next corpus is a new version.

## The hour

**The easy way: the dashboard's Labels page** (`/app` → Labels). It reads the corpus straight off
the volume and shows one card per item with tap buttons — "Needed me / Fine in the brief" for each
email, "Nudge / Queue / Drop" for each interrupt call — saving back into `labels.yaml` as you go
and stamping each day labeled. Work newest-first, stop whenever, then tap **Mark corpus reviewed**.
Everything below describes the same fields for anyone who prefers the file.

Open `evals/corpus/corpus-v1/labels.yaml`. It has one block per day. Work newest-first and stop when
the hour is up — **an unlabeled day is simply unscored**, and ten well-labeled days beat forty
guessed ones.

```yaml
days:
  day-00:
    needs_attention: [em-01, em-04]        # ← the things you'd have wanted interrupted about
    not_needs_attention: [em-02, em-03]    # ← explicit negatives: this is what makes precision real
    entity_count: 12                       # ← already ground truth. Leave it alone.
    open_loops_after: null                 # ← you fill this: the loops still open at end of day
    chases_after: null                     # ← you fill this: {anchor_key: how many chases by now}
    nudge:
      im-01: nudge                         # ← nudge | queue | drop
      cl-01: nudge
      em-01: queue
```

Five fields, five rules:

1. **`needs_attention` / `not_needs_attention`** — move ids between the two lists until they read
   right. Every id must be in exactly one of them; an id in neither is unscored.
2. **`nudge`** — for each inbound item: `nudge` (this should have buzzed my phone *then*),
   `queue` (fine to wait for the digest or the brief), `drop` (I never needed to see this). This is
   the Editor's interrupt-budget tuning set. Be honest about `queue` — the whole product bet is
   that most things queue.
3. **`entity_count`** — do not edit. It comes from the identity map, not from a guess: it is how
   many distinct humans that day actually involved, which is exactly the number the pipeline's
   identity resolution has to reproduce.
4. **`open_loops_after`** — the `anchor_key`s you'd expect still open at the end of that day. Skip
   it (`null`) on days where you don't have a strong opinion; only fill the days where a loop
   clearly should — or clearly should not — have survived.
5. **`chases_after`** — `{anchor_key: chases}`: how many times Sotto should have chased that loop
   by the end of that day (0 is a real answer, and often the right one). **Only meaningful at
   `--sample 0`.** The resolver stamps at most one chase per replayed day, so a sampled replay
   skips the days in between and reaches the day with fewer chases behind it than reality would
   have; label this field on a full replay or leave it `null`.

To see what a day contains while you label it:

```bash
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); \
  [print(e['_gid'], '|', e.get('from'), '|', e.get('subject')) for e in d['inputs']['google']['emails']]" \
  evals/corpus/corpus-v1/days/day-00.json
```

## When you're done

Set the top of the file to `reviewed: true`. The harness refuses to score against an unreviewed
draft — an LLM's guesses are not a baseline.

```bash
python3 evals/run_golden.py --sample 0                      # offline: funnel, loops, dedup
python3 evals/run_golden.py --live --judge --sample 5        # real briefs + the pinned judge (~$1-2)
python3 evals/run_golden.py --live --judge --update-baseline # record the baseline you'll defend
```

Then "run the golden evals" before any judgment-touching merge. Until the baseline is recorded,
every run ends `RESULT: NO BASELINE` — the gate never passes on nothing to compare against. Later
runs must match the baseline's shape (mode and `--sample`): a metric the baseline holds that a run
didn't produce is a FAIL, not a skip.

## What ages

Labels encode your preferences **at label time**. When you retune the interrupt budget or the voice
on purpose, re-read the affected labels before treating a score drop as a regression — sometimes the
label is what changed. Scores are only comparable *within* a corpus version; a rebuild with new data
or a new key is a new version and a new baseline.
