# Runtime architecture

One page on what runs inside the container, who owns what, and which files cross between them. The
code is the source of truth; this is the map you read first. For *why* a nudge fired, see
[HOW-SOTTO-DECIDES.md](HOW-SOTTO-DECIDES.md); for every env var, [RAILWAY.md](../RAILWAY.md).

> **Prefer to click than to read?** [playground-architecture.html](playground-architecture.html) is
> this page made explorable — a layered node map of the same modules, threads and shared files,
> with six saved views, a clickable drawer per node, and the shared-file sheet; every number on it
> is interpolated from the same rules island the drift guard checks against code.
> [playground-feedback-loops.html](playground-feedback-loops.html) does the same for the six
> self-improvement loops, each badged CLOSED, RECORDING or PLANNED. Both are single self-contained
> files: open them from disk, or visit `/static/playground-architecture.html` on a running deploy.

## The spine — read this first

Every Sotto behaviour — morning brief, evening brief, meeting prep, the proactive watcher, the
midday digest — is the same five steps: **gather → compose → validate → deliver → learn.** One file
owns each of them, and you can enter the system at any stage:

| Stage | Owner | In one sentence |
|---|---|---|
| **gather** | [`_shared/scripts/gather_google.py`](../sotto-chief-of-staff/_shared/scripts/gather_google.py) (+ `gather_granola.py`, `_shared/lib/attachments.py`, the Bridge's `read_local`) | Deterministic Python pulls the raw material; no model is involved. |
| **compose** | [`_shared/scripts/compose_brief.py`](../sotto-chief-of-staff/_shared/scripts/compose_brief.py) | One Gemini call turns the gathered payload into prose and actions, plus the optional critic/revise pass. |
| **validate** | [`_shared/lib/brief_validate.py`](../sotto-chief-of-staff/_shared/lib/brief_validate.py) | Deterministic checks reject a malformed or hallucinated brief, and back the still-open appendix, before you ever see it. |
| **deliver** | [`_shared/scripts/brief_marker.py`](../sotto-chief-of-staff/_shared/scripts/brief_marker.py) (the deliver-once claim; the host's gateway sends) | Exactly one process wins the claim and delivers; the loser discards its draft. On the receiver lane the claim is no longer taken on trust — `outbox.py` claims the marker at the send seam, so a run that skipped its own claim is superseded rather than sent. The seam also asks the one question a prompt cannot fake before it claims: is `compose_brief.py`'s archive of today's brief (`briefs/<date>_<kind>.json`) on the volume? A brief-kind body with no archive behind it was never composed — an improvised recap, or the run's "I could not run the composer" — and is receipted `failed` without claiming, so the day stays open for the lane that can (Sep 2, 2026: exactly that claimed the day with `calls=0`). |
| **learn** | [`_shared/knowledge/knowledge_update.py`](../sotto-chief-of-staff/_shared/knowledge/knowledge_update.py) (+ `continuity_resolve.py`, `learn_preferences.py`, `style_extract.py`, `granola_graph.py`, `prewarm_graph.py --sync-contacts`) | What the brief found is written back into memory, so tomorrow starts from today. |

**The LLM writes prose — it never decides *whether* to interrupt you.** That decision is the
event-triage funnel, documented rule by rule in [HOW-SOTTO-DECIDES.md](HOW-SOTTO-DECIDES.md), and
the seven things that can start a nudge are the producer table at the top of that page.

**Two reads bypass the host CLI, on one shared client.** The Hermes `google-workspace`
`google_api.py` is installed from upstream, not from this repo, and it lacks two verbs this system
needs: a Gmail *draft* (`google_action.py`), and message *attachments* — its `gmail get` flattens
the message to a body and discards the MIME part tree, so filenames aren't reachable through it at
any price. Both go straight to the Gmail API on the SAME `google_token.json` the CLI authenticates
with, through one builder — `gather_google._gmail_service`, which `google_action.py` imports rather
than copying. The attachment half converts what it fetched through
[`_shared/lib/attachments.py`](../sotto-chief-of-staff/_shared/lib/attachments.py), the owner of the
lane's three caps for both the fetch side and the prompt side: *an attachment Sotto can read becomes
text under its email; one it can't is named, never guessed.* Conversion is in-process and local —
no hosted OCR, no API key, no env var (see [HOW-SOTTO-DECIDES.md](HOW-SOTTO-DECIDES.md) §
*Attachments*).

### The open-loop path — input to outcome

This is the complete path for a Granola commitment. It deliberately has one model judgment and
small deterministic guards around it, rather than a second workflow engine:

```text
Granola notes / summary / transcript
  → gather_granola: meeting_id + exact start/end + source text
  → compose_followup: owner_is_user + verbatim source_snippet + optional existing_anchor_key
  → apply_commitments:
       meeting id + copied quote + named owner + action/deliverable words must match source (or drop)
       same meeting + direction + source snippet → same occurrence
       validated live same-direction anchor     → merge with that open loop
       otherwise                                → create a new open loop
  → continuity ledger → brief / Loops page / proactive watcher
  → triage verdict with decision_id
  → receiver send → delivery receipt with the same decision_id
       delivered → count “already nudged” and finalize a chase/handoff
       failed     → neither
```

Capture does **not** wait for a confirmation turn: private bookkeeping is written when the grounded
extraction succeeds. Both composition and the actual write recheck four mechanical model claims
against the gathered meeting: its id exists, the supporting quote was copied, that quote names the
claimed owner, and it contains the obligation's action/deliverable words. Ambiguity drops the
item instead of opening a loop. Exact occurrence identity handles reruns; the model may suggest at
most one semantic merge, and code accepts it only if that anchor is live and points in the same
direction. There is no fuzzy-matching subsystem. Source-backed Granola commitments close explicitly,
so an ordinary reply, an old creation date, or the user's own chase cannot silently erase them.

## The two processes

Two processes run side by side: **Hermes** (the agent loop, the chat gateway, and the scheduler for
the pulse, the watcher and the digest) and the **trigger receiver** (a stdlib HTTP server on
`$PORT`, which schedules and delivers the two briefs itself). `adapters/hermes/start.sh` resolves the
**delivery channel** first and exports it — *Telegram, unless the volume already holds a paired
WhatsApp session and no `TELEGRAM_BOT_TOKEN` is set* — so the receiver, the cron reconciler and the
gateway can never disagree about where a brief goes; then it starts the receiver (so Railway's
`/health` answers within seconds) and boots Hermes. Before the gateway comes up it links that
channel: the Telegram chat-id capture (`telegram_link.py --boot`, bounded by that module's own
five-minute constant, and accepting only a message carrying this deploy's setup code — a bot's
username is discoverable, so an unauthenticated capture would link a stranger) or, with
`WHATSAPP_ENABLED=true`, the QR pairing (`wa_pair.py`). Both waits are non-fatal — a boot that can't
link still composes briefs — but an unlinked Telegram does **not** start `hermes gateway`: it could
not deliver anyway, and its `getUpdates` poll would consume the pairing message the next boot's
capture needs. The two processes share exactly one thing: the `$SOTTO_DATA` volume.

## The six modules

All under `runtime/trigger-receiver/`, all stdlib-only. `receiver.py` loads the other six with
`importlib` and injects `HOOKS` — late-bound lambdas over its own globals — so no module ever
imports the receiver back.

| Module | Owns |
|---|---|
| `receiver.py` | The HTTP surface (`/health`, `/trigger`, `/bridge/*`, `/mcp`, `/setup*`, `/google/*`, `/connect/*`, `/debug/*`), brief trigger dedup, the brief schedule (`crons.json`'s `runner: receiver` jobs), the event funnel's dispatch half, the setup wizard page, and every skills-tree subprocess it forks |
| `dashboard.py` | The Window: `/app`, `/app/login`, `/static/*`, `/api/*` — sessions, CSRF, CSP, lockout, the JSON API, and every write lever (facts, loops, prefs, cadence, graph, voice, run-now, golden labels); Cadence also shows scheduled one-shots and read-only `user-*` Hermes routines |
| `calcache.py` | The ONE calendar cache — the `gather_google.py --skip-gmail` fork, its 10-min TTL, the refresh thread that writes `cache/calendar_today.json`, the post-meeting tap detector, and the calendar-diff detector (declines, last-minute invites, moves, cancellations → `calendar_change` events into the funnel) |
| `connectors.py` | The connector registry, both kinds: remote-MCP OAuth 2.1 (discovery → DCR → PKCE → token file) for the Connect tiles, and the key-based search providers it renders read-only beside them — **and `write_text`/`write_json`, the one atomic-write helper the whole image uses** |
| `outbox.py` | The durable delivery outbox — `events/outbox.json`, the idempotency key, the retry backoff, the per-kind expiry, and the drain heartbeat. **Nothing Sotto says is marked delivered until the channel says so; what fails waits its turn instead of dying.** |
| `retention.py` | THE table of what the volume keeps and for how long — every TTL, the three policies that apply them, and the guard that keeps the graph, the ledger and the corpus off every rule. It owns no clock: `receiver._retention_tick` fires it once a local day from the cron thread. **Retention is machinery, not an instruction a run can decline.** |
| `sessions.py` | The ONE session-archive implementation: reads the ID column of `hermes sessions list`, skips `cron_*` one-shots, runs `hermes sessions archive <id>` for the rest. Two callers — `start.sh` at boot (fresh per deploy) and `receiver._session_archive_tick` nightly — and it exists because both used to grep for hex/uuid ids, which Hermes' ids (`20260903_033026_65967d`) are not: a week of boots archived nothing, no session ever reset, and replies grew a transcript to copy from (Sep 1–3, 2026). |
| `relay.py` | The reverse-MCP relay: the Mac long-polls `/bridge/poll`, Hermes calls `/mcp` locally, no tunnel |

An eighth file sits in that directory and is **not** a module: `keys.py` is a byte-identical vendored
copy of `_shared/lib/keys.py`. The two runtimes must compute the same ids (`queue_key`,
`sample_hash`) for "nudge me now" and the Voice card to address the right row, and the receiver
image has to render those surfaces with no skills tree on the box — so it copies rather than
imports, and `tests/test_docs_drift.py` fails the suite the moment the copies diverge.

## The six daemon threads

Every one is a `daemon=True` loop that swallows its own exceptions — a thread must never die and
never take the server with it.

| Thread | Cadence | What it does |
|---|---|---|
| Gmail poll (`receiver.start_gmail_poll_thread`) | `SOTTO_EMAIL_POLL_SECS`, default 90s | Claims nothing while polling; feeds new mail through the same funnel as Bridge events, then acknowledges ids only after the receiver durably accepts them |
| Release valve (`receiver.start_valve_thread`) | `receiver.VALVE_INTERVAL_SECS_DEFAULT` = 900s | Forks `triage_event.py --valve` so a nudge held during cooldown/quiet/catchup can still get out |
| Sotto cron (`receiver.start_cron_thread`) | `receiver.CRON_TICK_SECS` = 60s | Fires every `crons.json` job marked `"runner": "receiver"` — the morning and evening briefs — when its minute arrives, down the same spawn → outbox path every other lane takes. One fire per job per local day in memory — and the durable `.delivered` marker is consulted first, so a restart inside the window never recomposes a day that already went out; a brief run that DIES is re-fired once the same day (`_retry_failed_brief`), and the wake-push's "the cron is composing, fold instead" decision reads `_RUNS_INFLIGHT`, so it never stands down for a cron run that is already dead. A timezone change clears the in-memory fired-today stamps (`set_timezone`) — the zone is re-read every tick, so nothing needs re-registration. Fixed daily `M H * * *` schedules only. **The same tick also runs the nightly housekeeping** at `retention.SWEEP_LOCAL` (3:30 AM local, one fired-today map): the retention sweep (one log line every day, including a zero), and the daily chat-session archive (`sessions.archive_all`, the same call the boot path makes — silent `hermes sessions archive`, so a chat session lasts a day or a deploy, whichever comes first, and interactive replies have no week-old transcript to copy from) on its own daemon thread, so an external CLI can never hold the thread that fires the briefs |
| Delivery outbox drain (`outbox.start_drain_thread`) | `outbox.DRAIN_INTERVAL_SECS` = 60s | Retries every message the channel hasn't acknowledged — backoff doubling from 60s to a 900s cap, then `failed` (a brief, loudly, when its local day ends) or `expired` (a nudge past 240 min, the same window the valve refuses to promote in). `SOTTO_OUTBOX=0` turns the heartbeat off; nothing is ever sent unrecorded either way |
| Update check (`receiver.start_update_check_thread`) | daily | One GitHub fetch → `cache/update_check.json` (the ONE writer); silent on an unstamped dev build |
| Calendar refresh (`calcache.start_refresh_thread`) | `SOTTO_CALENDAR_REFRESH_SECS`, default 900s | Refreshes the snapshot, rewrites `cache/calendar_today.json`, asks `tap_tick()` which meetings just ended, and `change_tick()` what changed about the imminent calendar |

## The six subprocess boundaries

The receiver image carries no PyYAML and cannot import the skills tree; everything that needs the
tree is a fork. All six run on `sys.executable` with `SOTTO_DATA` passed explicitly — one policy,
because interpreter ambiguity has bitten before (`receiver._skill_env`). The tree is located by
`receiver._find_sotto_script`: `SOTTO_SKILLS_ROOT` → the Hermes layout → the repo-relative source
tree, resolved once per script name.

| Caller | Script | Shape |
|---|---|---|
| `receiver.run_triage` | `event-triage/scripts/triage_event.py` | events JSON on stdin → verdict JSON on stdout, synchronous, 30s |
| `receiver.run_valve` | `event-triage/scripts/triage_event.py --valve` | verdict JSON on stdout, 30s |
| `receiver._poll_gmail_once` | `event-triage/scripts/poll_gmail.py` | event list on stdout, 180s |
| `dashboard._run_skill_cli` | `_shared/knowledge/knowledge_edit.py` · `preferences.py` · `style_extract.py --confirm` · `event-triage/scripts/triage_event.py --promote` (via `receiver.run_promote`) | `{"ok": …}` on stdout, 30s — ONE subprocess policy for the whole write surface, so every dashboard edit rides the identical code path the same instruction typed in chat would |
| `calcache._run_calendar_gather` | `_shared/scripts/gather_google.py --skip-gmail` | writes a temp JSON file, 60s |
| `receiver._seed_snapshot_from` | `_shared/scripts/compose_brief.py --seed-snapshot` | daemon thread, 120s — a wake-push that arrives after the day's brief already delivered, or within `receiver.BRIEF_CRON_WINDOW_MIN` (10) minutes of its cron while that run is still composing, composes nothing; its payload is folded into the local snapshot by the brief's own snapshot writer, and the funnel surfaces the catch-up |

A seventh boundary is different in kind: every lane that runs a SKILL goes through
`receiver._spawn_and_deliver` — `$SOTTO_RUN_SKILL` (`hermes -z "<prompt>"`) on a daemon thread,
whose final text is then piped to `hermes send --to $SOTTO_CRON_DELIVER`, the same home channel
Hermes' own crons deliver to. Since the briefs moved onto the receiver's clock, EVERY scheduled
thing Sotto says goes through this one seam — there is no second lane that composes and delivers a
brief on its own. It used to be fire-and-forget on the belief that the skill
delivered itself; `-z` prints to stdout, so five of the six nudge producers were writing to a
sink (Aug 2026). Starting it is synchronous — a missing runner still raises, because
`handle_trigger` releases its brief claim on that — and only the outcome is asynchronous, which
is why it leaves a receipt in `events/delivery.jsonl`. That send is the ONE path outward, so it is
also where the outbox sits: `receiver._deliver_text` writes the row and its idempotency key BEFORE
the first attempt and flips it to `delivered` only on the channel's ack (`hermes send` exiting 0 —
the strongest ack this seam has; `receiver._send_via_channel` is the one function a channel with a
real receipt would strengthen). A failed send is now queued, not lost. It is also where deliver-once
stopped being a prompt: a brief-kind row proves it owns `briefs/<date>.<kind>.delivered` before the
channel is asked — no marker and the row claims it with its own run id, another run's id and the row
goes `superseded`, receipted and never sent (Aug 30, 2026: the evening brief went out twice because
the skill's own claim step was skipped). Before it claims at all, the row must have a composed brief
behind it — `briefs/<date>_<kind>.json`, which `compose_brief.py` archives before printing a word —
or it goes `failed` on the spot as `not_a_brief`, unclaimed, so the lane that can still compose is
not stood down by an improvised recap (Sep 2, 2026). Silence is a token, not a hope: a spawned
run with nothing to deliver replies the `NO_NUDGES` sentinel, which the seam records as an empty
run instead of sending — "say nothing and end the turn" was an instruction models reliably
ignored, and three "all clear" messages in one evening proved it (Aug 2026).
The Hermes `google-workspace` skill's `setup.py` is an eighth, forked for Google auth only.

**Adapter/plumbing variables** (script-to-script, never a user setting — they are deliberately
absent from RAILWAY.md's table): `SOTTO_DATA` (the volume path, `/data` in the image),
`SOTTO_RUN_SKILL` and `SOTTO_SKILLS_ROOT` (above), `SOTTO_UNATTENDED` (set to `1` on every skill run the receiver spawns — the seam `google_action.py`'s send gate reads; the interactive gateway never carries it), `SOTTO_MCP_TOKEN` (the ROOT bearer,
which `start.sh` sets from `BRIDGE_TOKEN`; the Bridge's lanes — relay dial-in, event ingestion,
wake-push — and the operator surfaces take the root, while `/mcp` takes only
`receiver.derive_mcp_token(root)`, the one-way HMAC bearer `configure_mcp.py --derive-mcp` hands
Hermes at boot, so the prompt-injectable agent never holds the trust anchor), `SOTTO_TRIGGER_PORT` / `SOTTO_TRIGGER_BIND` (the
receiver's port and bind address, used only when Railway's `PORT` is absent — local runs get `8787`
on `127.0.0.1`), and `SOTTO_BRIDGE_BIN` (read once by `adapters/hermes/install.sh`: an explicit path
to a `sotto-bridged` engine, overriding both locations it probes for local mode — the built binary
and the one bundled in `/Applications/Sotto Bridge.app`).

## The shared `$SOTTO_DATA` files

Everything that crosses a process boundary crosses as a file on the volume. Receiver-owned JSON
snapshots use `connectors.write_json` (tmp at 0600 → `os.replace`); skill-owned transaction files
use the same temp-then-replace pattern, and the continuity ledger adds one cross-process lock around
read/modify/write. JSONL records are append-only and bounded. **"skills" below means the
`sotto-chief-of-staff` scripts, running in a different process.**

| File | Writer | Reader |
|---|---|---|
| `setup_code` | receiver (boot) | receiver, `start.sh` |
| `config/settings.json` | receiver (`/setup/timezone`) | receiver, dashboard, `start.sh`, skills (`timeutil`) |
| `briefs/<date>.<kind>.claim` · `briefs/<date>.<kind>.delivered` | receiver (the `.claim`; and the `.delivered` when the send seam's gate claims it), skills (`brief_marker.py --claim`) | receiver (trigger dedup, the cron-window fold, and the outbox's deliver-once gate — the `.delivered` file's CONTENT is the claiming run's id), skills (`proactive_scan.py`) |
| `briefs/<date>.<kind>.payload.json` | receiver | skills (`compose_brief.py`) |
| `briefs/<date>_<kind>.json` | skills | dashboard |
| `briefs/<date>.<kind>.named.json` | skills (`compose_brief.py`) | skills (`proactive_scan.py` — which open loops that brief NAMED, so a chase is held only for a genuine double-tell) |
| `events/seen.json` | receiver | receiver (idempotency ring — Bridge events, keyed `(source,rowid)`) |
| `events/gmail_seen.json` | receiver, through `poll_gmail.py --ack` after accepted ingest | skills (`poll_gmail.py`) — a provider fetch alone never advances the cursor, so a receiver failure is retried rather than lost |
| `events/last.stamp` | receiver | receiver (`/setup` liveness line) |
| `events/bundle-<random>.json` | receiver | skills (the `sotto-event` one-shot); atomically staged with thread/process-unique names and seven-day cleanup |
| `events/last_digest.txt` | skills (`digest_check.py --stamp`; on an in-agent install the brief that wins the deliver-once claim), receiver (`_on_delivered` — the brief the channel ACKED, not the one that claimed: a claim whose send fails and retries into the afternoon must not hide the morning from the 12:30 digest) | skills (`digest_check.py` window), dashboard (`/api/cadence` context line) |
| `events/queue.jsonl` · `events/surfaced.jsonl` | skills (`triage_event.py`) | dashboard (the Record + the waiting room), skills (`compose_brief.py` reads only verdicts whose `decision_id` has a delivered receipt) |
| `events/drafts.jsonl` | skills (`action_links.py` — every tap link built with a draft) | skills (`draft_outcomes.py`, run inside `learn_preferences.py` each brief: matched against the queue's `is_from_me` signals → outcomes.jsonl + style confirms) |
| `events/delivery.jsonl` | receiver (the ONE writer) | dashboard (the Record, source `delivery`), skills (`compose_brief.py`) — closing rows carry `usage` and correlated `decision_ids` |
| `events/outbox.json` | `outbox.py` (the ONE writer) | receiver (the retry drain), dashboard (`/api/runs` — the pending/failed line) — one row per message Sotto composed, written BEFORE the first send attempt and flipped to `delivered` only on the channel's ack |
| `events/delivery-effects-<run>.json` | skills (`proactive_scan.py`, one receiver-scoped run) | receiver — ephemeral chase/handoff effects, carried on the outbox row and applied only once the message is acknowledged, then deleted |
| `events/sends.jsonl` | skills (`google_action.py`) | you — one metadata-only line per real-effect **attempt** (send, reply, calendar create/delete/RSVP), allowed or refused, carrying `payload_sha256` so "what did Sotto send?" isn't answered by a prompt's promise |
| `cache/calendar_today.json` | calcache | skills (`triage_event.py` in-meeting hold) |
| `cache/meeting_taps.json` | calcache | calcache (exactly-once tap record) |
| `cache/research_<date>.json` | skills (`research_attendees.py`) | dashboard (`/api/research` cards), skills (`compose_brief.py` joins it) |
| `cache/hermes-version.json` | `start.sh` | receiver (Integrations page) |
| `cache/update_check.json` | receiver (daily update check — the ONE writer) | receiver (`/setup` line, `/api/overview` banner), skills (`compose_brief.py` update line) |
| `cache/update_notice.json` | skills (`compose_brief.py`) | skills (`compose_brief.py` — the once-per-version marker) |
| `connectors/<service>.json` | connectors (OAuth write; `/setup` Disconnect deletes) | skills (`connector_tokens.py`), receiver (presence only) |
| `connectors/<service>.error` | skills (the gather; `/setup` Disconnect deletes) | receiver (`/setup` reconnect hint) |
| `dashboard_sessions.json` · `dashboard_audit.jsonl` | dashboard | dashboard |
| `decks/<view_id>.pdf` · `decks/<view_id>.json` | skills (`docsend_fetch.py` — the pdf is the deck's pages assembled, the json is the read cache that stops a re-ask logging a second view) | you (dashboard `GET /api/decks/<id>.pdf`), skills (cache hits, incl. unattended) |
| `knowledge/master.md` | `_shared/knowledge/master_file.py` (the ONE writer — user-stated words; gateway confirms, dashboard edits shell out to it) | skills (`compose_brief.py`, `compose_meeting_prep.py` — always in the prompt), gateway chat, dashboard (Learned page card) |
| `knowledge/last_local_snapshot.json` | skills (`compose_brief.py`) | skills — the RAW Bridge payload, overwritten each brief and never deleted; its 24h TTL stops reuse, not storage ([DATA-FLOW.md](DATA-FLOW.md)) |
| `knowledge/snapshots/<date>.json` | skills (`compose_brief.py` — dated archive copy, last write of the day wins, pruned after 60 days) | `tools/build_golden_corpus.py` — the corpus's message history; one live snapshot only holds ~a day |
| `corpus/<version>/` | `tools/build_golden_corpus.py` (`--out`; refuses to write on a leak), dashboard Labels page (`labels.yaml` only — the owner's judgments) | `evals/run_golden.py` (scoring), dashboard Labels page. CONFIDENTIAL REGARDLESS — never leaves the volume |
| `knowledge/relationship_state.json` | skills (`relationship_pulse.py`) | skills (`compose_brief.py`, `triage_event.py`'s VIP floor), dashboard (the attention queue) |
| `knowledge/<kind>/*.md` · `style.json` · `outcomes.jsonl` | skills | dashboard (read), skills |
| `logs/compose_brief.log` | skills | receiver (`/debug/brief-log`) |
| `proactive/<date>.json` | skills (`proactive_scan.py`) | skills (`proactive_scan.py`) — the once-per-day nudge dedup; a read-modify-write, so both producers take `triage_event._locked` on it |
| `proactive/wake_run.last` | receiver (`handle_proactive_wake`) | receiver — its *mtime* is the sleep→wake throttle, nothing is read from inside it |
| `proactive/retune_offer.last` | skills (`proactive_scan.py`) | skills (`proactive_scan.py`) — the retune-offer cooldown stamp |
| `proactive/pending_offer.json` | skills (`pending_offer.py set` — the ONE writer, called by the proactive lane right after it delivers a push that ENDED in a question) | the gateway (`pending_offer.py get`, then `clear`) — a nudge is delivered by a detached run, so the user's bare "sure" lands in a session that never saw the question; this file is where it is written down. One offer at a time, newest wins, expires after 180 min at read. When a yes to it would send or write, it also carries `payload_sha256` — the hash of the offered content, which `google_action.py --offer-bound` must match before acting |
| `intentions.jsonl` | skills (`schedule_wakeup.py`) | skills (`proactive_scan.py`), dashboard (`/api/cadence`) — append-only one-shot recipes, folded by id; an optional loop anchor cancels the recipe when the loop closes |
| `hermes/platforms/whatsapp/session/creds.json` | the Hermes gateway (**not** Sotto) | receiver (`_whatsapp_status`) — the positive "this account is linked" probe; `start.sh` (the same file decides whether a redeploy keeps WhatsApp as the channel) |
| `whatsapp-pairing.txt` · `google-auth-url.txt` | `wa_pair.py` / `start.sh` | receiver |
| `telegram-link.json` | `telegram_link.py` (the ONE owner of the Telegram handshake — run by `start.sh` at boot, or by hand: [CHANNELS.md](../CHANNELS.md) § Telegram setup) | `start.sh` (`--boot` reuses the captured id for this token, else captures it, and forwards it to Hermes as `TELEGRAM_ALLOWED_USERS` + `TELEGRAM_HOME_CHANNEL`), receiver (`_telegram_status` — the "this chat is linked" probe behind the wizard tile and the nudge gate). It holds the bot token, so 0600 |
| **`preferences.json`** | **skills *and* dashboard** | skills, dashboard |

Every row but the last is **one-way**: exactly one writer, and readers that never write. That is the
property that keeps the two processes from needing a lock.

`preferences.json` is the one shared-write file — two writers: `learn_preferences.py` (rebuilds the
behavioral lists from `outcomes.jsonl` after every brief) and `preferences.py` (the user's stated
`explicit` block — mutes, VIPs, tone notes, the nudge snooze). The dashboard is not a third: an
`explicit` change from `POST /api/prefs` or `POST /api/cadence` forks `preferences.py`, so muting a
sender or snoozing nudges from the web is byte-for-byte what saying it in chat does. The one direct
write it keeps is a delete from a *learned* list, which has no CLI verb — and that carries the rule
for the overlap: **a rule you delete stays deleted**, because the delete also records
`{list, value}` in the top-level `suppressed` array, which the learner filters out when it rebuilds.

## Who produces a nudge, and who owns a memory

The container half above is the *plumbing*; the spine at the top of this page is the product. Two
small tables finish the map — the same writer/reader treatment as the `$SOTTO_DATA` table, applied
to the two questions it doesn't answer.

**Nudge producers** — seven, and only seven, things can start a nudge. (Full version, with the gate each
one rejoins: [HOW-SOTTO-DECIDES.md § Who can produce a nudge](HOW-SOTTO-DECIDES.md#who-can-produce-a-nudge).)

| Producer | Entry point | Cadence |
|---|---|---|
| Bridge events | `receiver.handle_events` | push, `SOTTO_EVENTS_TICK_SECS` on the Mac |
| Gmail poll | `receiver._poll_gmail_once` | `SOTTO_EMAIL_POLL_SECS` (90s) |
| Release valve | `receiver._valve_tick` → `triage_event.release_valve` | `receiver.VALVE_INTERVAL_SECS_DEFAULT` (900s) |
| Post-meeting tap | `calcache.tap_tick` → `receiver._dispatch_meeting_tap` | on the calendar refresh tick |
| Calendar diff | `calcache.change_tick` → `receiver._dispatch_synthetic` | the same refresh tick — declines, last-minute invites, moves, cancellations of imminent meetings |
| Proactive watcher | `proactive_scan.main` → `triage_event.triage` (in process, one bundle; due one-shot intentions are one input to this producer) | the `*/15` cron |
| "Nudge me now" | `dashboard._post_cadence` → `receiver.run_promote` → `triage_event.promote_one` | you, on the Cadence page |

**Memory owners** — every durable thing Sotto remembers has exactly one writer. (Shapes:
[contracts/exhaust-schema.md](../contracts/exhaust-schema.md), which names the owning script per row.)

| Memory | Lives at | Owner (the only writer) |
|---|---|---|
| The people/company graph | `knowledge/*.md` | `_shared/knowledge/knowledge_update.py` (`knowledge.py` is its model + serializer) |
| Grounded research (people **and** companies) | same files | `meeting-prep/scripts/persist_prep.py` and `_shared/scripts/prewarm_graph.py` — both *through* `knowledge_update.apply()`; there is no second writer for either file type |
| Verified X identity + public-profile provenance | person files above | `_shared/scripts/x_connectivity.py` — exact handle lookup only, then *through* `knowledge_update.apply()`; immutable `x_user_id`, handle alias history, and the 90-day negative cache are durable, while Posts/bookmarks stay in the run's temporary prep payload |
| Ambiguous X identity suggestions | `knowledge/x_link_suggestions.json` | `_shared/scripts/x_connectivity.py`; a weak or already-owned match is proposed here instead of being silently attached |
| Meeting attendance + Apple Contacts identity | same files | `_shared/scripts/granola_graph.py` (who you sat with) and `_shared/scripts/prewarm_graph.py --sync-contacts` (every email/phone as an identifier, the card's notes + birthday) — both *through* `knowledge_update.apply()` |
| User-initiated graph edits | same files | `_shared/knowledge/knowledge_edit.py` — which routes *through* `knowledge_update.apply()`, so a dashboard edit and a texted correction are byte-identical |
| Open loops (the continuity ledger) | `knowledge/continuity/*.md` | `morning-brief/scripts/continuity_resolve.py` owns the locked, atomic write API; brief extraction, `apply_commitments.py`, and user edits in `knowledge_edit.py` all write through it (`ledger_io.py` is the shared read side) |
| The master memory file (who the user is, the people around them, their standing Procedures — always in every brief/prep prompt; the gateway reads it in chat; seeded by setup's four questions; editable on the dashboard's Learned page) | `knowledge/master.md` | `_shared/knowledge/master_file.py` — user-stated words only, gateway confirms before writing, dashboard edits shell out to the same CLI; size-capped so "always in context" stays honest |
| Learned preferences | `preferences.json` (behavioral lists) | `approval-tiers/scripts/learn_preferences.py` |
| Stated preferences | `preferences.json` (`explicit` block) | `_shared/scripts/preferences.py` |
| Writing style | `style.json` | `_shared/scripts/style_extract.py` |
| Relationship analytics | `knowledge/relationship_state.json` | `relationship-pulse/scripts/relationship_pulse.py` |
| The Record (every verdict) | `events/surfaced.jsonl` · `events/queue.jsonl` | `event-triage/scripts/triage_event.py` |
| Outcomes | `outcomes.jsonl` | `_shared/scripts/log_outcome.py` |
| One-shot intentions | `intentions.jsonl` | `_shared/scripts/schedule_wakeup.py` |

Reading is unrestricted; writing is not. One writer per file is what lets two processes share the
volume with no lock.

**An interrupted graph update finishes the next time anything touches the graph.** Every write under
`knowledge/` goes through one temp-then-`os.replace` primitive (`knowledge.write_text_atomic`), so a
crash can never leave a person file truncated — `open(path, "w")` truncates first, and that window is
somebody's whole memory. Single files are only half of it: a relation is stored on BOTH people, a
merge writes the survivor and deletes the loser and repoints everyone who pointed at it, and a
re-key renames a file every other file's edges name. Those batches write their op list to
`knowledge/.journal.json` before touching the first file and remove it after the last;
`knowledge_update.graph_lock()` — the critical section every writer entry point opens with — replays
whatever it finds there first. Every journaled op is idempotent, so replaying a batch that already
finished changes no bytes, and a journal nobody can parse is logged and cleared rather than left to
block every future write. Nothing is said unless a repair actually happens.

### The research loop — what a spent token has to leave behind

> **Every token spent on research must leave behind a durable, structured, correctable fact — and
> nothing situational is ever stored.** (CLAUDE.md § Standing bars.)

Research is the most expensive thing Sotto does, so it is the one loop that must compound. Three
grounded call shapes go out through the ONE search seam (`_shared/scripts/web_research.py` — per
capability, the first provider with a key present wins; the three ladders are in
[MODELS.md §5c](MODELS.md)); all three come back through
`persist_prep.py` into `knowledge_update.apply()`:

| Call | What it buys | Where it lands | Read back as |
|---|---|---|---|
| Pass A — profile (5 attendees/call) | title, company, bio, one-line company summary | person fact, conf 0.55, `source: web_research`, + `last_researched` | the person's `known` line, and `profile_is_fresh` skips them for 30 days |
| Pass B — 90-day recency sweep (3/call) | dated, source-URLed activity + public personal texture | person facts, conf 0.6, `source_ref` = the page | the person's `known` line, injected as "do NOT repeat any of this" |
| Focus — `--focus`, ONE person, ONE call | what the company builds, the founder story, the market, traction | **the COMPANY file**: `## About` (replaced) + `## News` (URL-deduped), `updated_by: web_research`, `last_researched` | `knowledge_update.company_knowledge()` → the focus prompt's "already on file, do NOT re-derive it" block |

The company half is why the deep dive is worth its call twice: it is knowledge about an
*organization*, so parking it on whichever human was researched that morning means the next person
from that company arrives cold. `company_knowledge()` is its one read side — for the next research
run, and (through `knowledge_query.py`) for the brief's **Company Context** block.

### What the brief reads back — `knowledge_query.py`

The read side of all of the above, and the one place the retrieval question is answered:

| Emits | From | Gated by |
|---|---|---|
| `person_knowledge` — the compact packed block per person | `knowledge/people/*.md` | **today's inputs**: a person packs when they appear in `--gmail` (today's From/To) or on `--calendar`; the `--relevant-days 7` file-mtime cohort is the fallback, used only when no inputs were supplied |
| `company_knowledge` — About + the 3 newest news lines, ≤5 companies | `knowledge/companies/*.md`, via `knowledge_update.company_knowledge()` | today's attendee email domains + the packed people's `company`, deduped by file |
| `contact_index` — the identity map | EVERY person file | ungated: it is what resolves a phone and an email to one person |

`mtime` says when a file was last *rewritten*, which was never the same question as "does this
person matter today" — under it, someone who emailed you this morning packed nothing and the model
re-derived what the graph already knew.

Two things deliberately do NOT persist, and both are correct:

- **Situational output** — talking points, openers, angles. The meeting-prep prompt writes them
  fresh from durable facts every run, for free. The research prompts no longer *ask* for them
  (`conversation_hooks` was deleted at the schema, not hidden at render time), because inventing
  advice costs output tokens and restates the fact it points at.
- **`$SOTTO_DATA/cache/research_<date>.json`** — a 7-day render cache for the dashboard's research
  cards. The durable half of that same output already went to the graph.
- **Recent X Posts and matching owner bookmarks** — fetched only for upcoming attendees and handed
  to the current brief/prep through mode-0600 `/tmp/sotto_x_context.json`. They never enter the
  graph, research cache, relationship state, or event ledger; the next run overwrites the handoff.

**Correctability** is the constraint that keeps the loop honest. A person fact is corrected with
`knowledge_edit.py --op correct`, and the one thing research can never learn — that an unsaved phone
number belongs to a person — is supplied with `--op add-identifier`, which refuses an identifier
already on someone else's file rather than moving it (two files sharing one identifier is what the
auto-merge reads as proof they are one human). A company has no facts map (its on-disk shape stays
byte-compatible with the Mac app's `knowledge_files.rs`), so its one correctable field is the
About paragraph — `--op company-about`, through the same `apply()` lane research writes through,
from chat or from the dashboard's company page. The write stamps `updated_by: user_edit`, and
research declines to overwrite it: a correction you made stays made. The same rule covers the
*name-merge suggestions*: `--op merge-dismiss` ("these really are two people") tombstones the pair
in `knowledge/merge_suggestions.json`'s `dismissed` list, because the suggestions are recomputed
from the files on disk on every apply and would otherwise ask again tomorrow.

## Where the schedule lives

`adapters/hermes/crons.json` is the ONE schedule declaration and
`adapters/hermes/reconcile_crons.py` is the ONE Hermes desired-state implementation. Cloud boot and
the receiver's live timezone change both call that reconciler; it replaces Sotto system jobs by
parsed job id and always fences `user-*` routines. The install adapters consume the same declaration
for their host-specific setup. See [adapters/README.md](../adapters/README.md) for its field contract.

**Who runs a job is a field, not a second file.** A row marked `"runner": "receiver"` — today the
morning and evening briefs — is never registered with Hermes; `receiver._cron_tick` fires it on the
60s heartbeat instead, so a brief is composed and delivered down the receiver's one lane (spawn →
outbox → deliver-once gate) and inherits its retries and receipts. Hermes' scheduler still runs the
pulse, the watcher and the digest. The reconciler still lists the receiver-run rows among its
REMOVAL markers, which is how an already-deployed box sheds its old Hermes brief crons on the next
boot — the migration needs no manual step.
