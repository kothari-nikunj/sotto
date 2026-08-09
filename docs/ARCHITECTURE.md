# Runtime architecture

One page on what runs inside the container, who owns what, and which files cross between them. The
code is the source of truth; this is the map you read first. For *why* a nudge fired, see
[HOW-SOTTO-DECIDES.md](HOW-SOTTO-DECIDES.md); for every env var, [RAILWAY.md](../RAILWAY.md).

> **Prefer to click than to read?** [playground-architecture.html](playground-architecture.html) is
> this page made explorable — the module table, the thread table and the shared-file table are the
> same rows, and the funnel at the bottom will walk a real event (a friend's ask · an automated
> email · a second channel within 45 min · a message during a meeting · the fifth nudge of the day ·
> a post-meeting tap) through its gates and show the verdict with its reason.
> [playground-feedback-loops.html](playground-feedback-loops.html) does the same for the seven
> self-improvement loops, each badged CLOSED / RECORDING / PLANNED. Both are single self-contained
> files: open them from disk, or visit `/static/playground-architecture.html` on a running deploy.

## The spine — read this first

Every Sotto behaviour — morning brief, evening brief, meeting prep, the proactive watcher, the
midday digest — is the same five steps: **gather → compose → validate → deliver → learn.** One file
owns each of them, and you can enter the system at any stage:

| Stage | Owner | In one sentence |
|---|---|---|
| **gather** | [`_shared/scripts/gather_google.py`](../sotto-chief-of-staff/_shared/scripts/gather_google.py) (+ `gather_granola.py`, the Bridge's `read_local`) | Deterministic Python pulls the raw material; no model is involved. |
| **compose** | [`_shared/scripts/compose_brief.py`](../sotto-chief-of-staff/_shared/scripts/compose_brief.py) | One Gemini call turns the gathered payload into prose and actions, plus the optional critic/revise pass. |
| **validate** | [`_shared/lib/brief_validate.py`](../sotto-chief-of-staff/_shared/lib/brief_validate.py) | Deterministic checks reject a malformed or hallucinated brief, and back the still-open appendix, before you ever see it. |
| **deliver** | [`_shared/scripts/brief_marker.py`](../sotto-chief-of-staff/_shared/scripts/brief_marker.py) (the deliver-once claim; the host's gateway sends) | Exactly one process wins the claim and delivers; the loser discards its draft. |
| **learn** | [`_shared/knowledge/knowledge_update.py`](../sotto-chief-of-staff/_shared/knowledge/knowledge_update.py) (+ `continuity_resolve.py`, `learn_preferences.py`, `style_extract.py`) | What the brief found is written back into memory, so tomorrow starts from today. |

**The LLM writes prose — it never decides *whether* to interrupt you.** That decision is the
event-triage funnel, documented rule by rule in [HOW-SOTTO-DECIDES.md](HOW-SOTTO-DECIDES.md), and
the six things that can start a nudge are the producer table at the top of that page.

## The two processes

Two processes run side by side: **Hermes** (the agent loop, the chat gateway, the cron scheduler)
and the **trigger receiver** (a stdlib HTTP server on `$PORT`). `adapters/hermes/start.sh` starts
the receiver first so Railway's `/health` answers within seconds, then boots Hermes. They share
exactly one thing: the `$SOTTO_DATA` volume.

## The five modules

All under `runtime/trigger-receiver/`, all stdlib-only. `receiver.py` loads the other four with
`importlib` and injects `HOOKS` — late-bound lambdas over its own globals — so no module ever
imports the receiver back.

| Module | Owns |
|---|---|
| `receiver.py` | The HTTP surface (`/health`, `/trigger`, `/bridge/*`, `/mcp`, `/setup*`, `/google/*`, `/connect/*`, `/debug/*`), brief trigger dedup, the event funnel's dispatch half, the setup wizard page, and every skills-tree subprocess it forks |
| `dashboard.py` | The Window: `/app`, `/app/login`, `/static/*`, `/api/*` — sessions, CSRF, CSP, lockout, the JSON API, and every write lever (facts, loops, prefs, cadence, graph, voice, run-now), each of which shells out to the CLI chat uses |
| `calcache.py` | The ONE calendar cache — the `gather_google.py --skip-gmail` fork, its 10-min TTL, the refresh thread that writes `cache/calendar_today.json`, and the post-meeting tap detector |
| `connectors.py` | Remote-MCP OAuth 2.1 (discovery → DCR → PKCE → token file) for the Connect tiles — **and `write_json`, the one atomic-write helper the whole image uses** |
| `relay.py` | The reverse-MCP relay: the Mac long-polls `/bridge/poll`, Hermes calls `/mcp` locally, no tunnel |

## The four daemon threads

Every one is a `daemon=True` loop that swallows its own exceptions — a thread must never die and
never take the server with it.

| Thread | Cadence | What it does |
|---|---|---|
| Gmail poll (`receiver.start_gmail_poll_thread`) | `SOTTO_EMAIL_POLL_SECS`, default 90s | Forks `poll_gmail.py`, feeds new mail through the same funnel as Bridge events |
| Release valve (`receiver.start_valve_thread`) | `receiver.VALVE_INTERVAL_SECS_DEFAULT` = 900s | Forks `triage_event.py --valve` so a nudge held during cooldown/quiet/catchup can still get out |
| Update check (`receiver.start_update_check_thread`) | daily | One GitHub fetch → `cache/update_check.json` (the ONE writer); silent on an unstamped dev build |
| Calendar refresh (`calcache.start_refresh_thread`) | `SOTTO_CALENDAR_REFRESH_SECS`, default 900s | Refreshes the snapshot, rewrites `cache/calendar_today.json`, and asks `tap_tick()` which meetings just ended |

## The five subprocess boundaries

The receiver image carries no PyYAML and cannot import the skills tree; everything that needs the
tree is a fork. All five run on `sys.executable` with `SOTTO_DATA` passed explicitly — one policy,
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

A sixth boundary is different in kind: `run_skill` / `_spawn_event_agent` fire `$SOTTO_RUN_SKILL`
(`hermes -z "<prompt>"`) fire-and-forget — the skill delivers through the gateway, not back here.
The Hermes `google-workspace` skill's `setup.py` is a seventh, forked for Google auth only.

**Adapter/plumbing variables** (script-to-script, never a user setting — they are deliberately
absent from RAILWAY.md's table): `SOTTO_DATA` (the volume path, `/data` in the image),
`SOTTO_RUN_SKILL` and `SOTTO_SKILLS_ROOT` (above), `SOTTO_MCP_TOKEN` (the reverse-MCP relay bearer,
which `start.sh` sets from `BRIDGE_TOKEN`), and `SOTTO_TRIGGER_PORT` / `SOTTO_TRIGGER_BIND` (the
receiver's port and bind address, used only when Railway's `PORT` is absent — local runs get `8787`
on `127.0.0.1`).

## The shared `$SOTTO_DATA` files

Everything that crosses a process boundary crosses as a file on the volume. Every JSON write goes
through `connectors.write_json` (tmp at 0600 → `os.replace`), so a crash mid-write can never leave a
torn file. **"skills" below means the `sotto-chief-of-staff` scripts, running in a different
process.**

| File | Writer | Reader |
|---|---|---|
| `setup_code` | receiver (boot) | receiver, `start.sh` |
| `config/settings.json` | receiver (`/setup/timezone`) | receiver, dashboard, `start.sh`, skills (`timeutil`) |
| `briefs/<date>.<kind>.claim` / `.delivered` | receiver | receiver (trigger dedup) |
| `briefs/<date>.<kind>.payload.json` | receiver | skills (`compose_brief.py`) |
| `briefs/<date>_<kind>.json` | skills | dashboard |
| `events/seen.json` | receiver | receiver (idempotency ring) |
| `events/last.stamp` | receiver | receiver (`/setup` liveness line) |
| `events/bundle-<ms>.json` | receiver | skills (the `sotto-event` one-shot) |
| `cache/calendar_today.json` | calcache | skills (`triage_event.py` in-meeting hold) |
| `cache/meeting_taps.json` | calcache | calcache (exactly-once tap record) |
| `cache/hermes-version.json` | `start.sh` | receiver (Integrations page) |
| `cache/update_check.json` | receiver (daily update check — the ONE writer) | receiver (`/setup` line, `/api/overview` banner), skills (`compose_brief.py` update line) |
| `cache/update_notice.json` | skills (`compose_brief.py`) | skills (`compose_brief.py` — the once-per-version marker) |
| `connectors/<service>.json` | connectors | skills (`connector_tokens.py`), receiver (presence only) |
| `connectors/<service>.error` | skills (the gather) | receiver (`/setup` reconnect hint) |
| `dashboard_sessions.json` · `dashboard_audit.jsonl` | dashboard | dashboard |
| `knowledge/**` · `style.json` · `outcomes.jsonl` | skills | dashboard (read), skills |
| `logs/compose_brief.log` | skills | receiver (`/debug/brief-log`) |
| `whatsapp-pairing.txt` · `google-auth-url.txt` | `wa_pair.py` / `start.sh` | receiver |
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

**Nudge producers** — six, and only six, things can start a nudge. (Full version, with the gate each
one rejoins: [HOW-SOTTO-DECIDES.md § Who can produce a nudge](HOW-SOTTO-DECIDES.md#who-can-produce-a-nudge).)

| Producer | Entry point | Cadence |
|---|---|---|
| Bridge events | `receiver.handle_events` | push, `SOTTO_EVENTS_TICK_SECS` on the Mac |
| Gmail poll | `receiver._poll_gmail_once` | `SOTTO_EMAIL_POLL_SECS` (90s) |
| Release valve | `receiver._valve_tick` → `triage_event.release_valve` | `receiver.VALVE_INTERVAL_SECS_DEFAULT` (900s) |
| Post-meeting tap | `calcache.tap_tick` → `receiver._dispatch_meeting_tap` | on the calendar refresh tick |
| Proactive watcher | `proactive_scan.main` → `triage_event.triage` (in process, one bundle) | the `*/15` cron |
| "Nudge me now" | `dashboard._post_cadence` → `receiver.run_promote` → `triage_event.promote_one` | you, on the Cadence page |

**Memory owners** — every durable thing Sotto remembers has exactly one writer. (Shapes:
[contracts/exhaust-schema.md](../contracts/exhaust-schema.md), which names the owning script per row.)

| Memory | Lives at | Owner (the only writer) |
|---|---|---|
| The people/company graph | `knowledge/*.md` | `_shared/knowledge/knowledge_update.py` (`knowledge.py` is its model + serializer) |
| User-initiated graph edits | same files | `_shared/knowledge/knowledge_edit.py` — which routes *through* `knowledge_update.apply()`, so a dashboard edit and a texted correction are byte-identical |
| Open loops (the continuity ledger) | `knowledge/continuity/*.md` | `morning-brief/scripts/continuity_resolve.py` (`ledger_io.py` is the shared read side) |
| Learned preferences | `preferences.json` (behavioral lists) | `approval-tiers/scripts/learn_preferences.py` |
| Stated preferences | `preferences.json` (`explicit` block) | `_shared/scripts/preferences.py` |
| Writing style | `style.json` | `_shared/scripts/style_extract.py` |
| Relationship analytics | `relationship_state.json` | `relationship-pulse/scripts/relationship_pulse.py` |
| The Record (every verdict) | `events/surfaced.jsonl` · `events/queue.jsonl` | `event-triage/scripts/triage_event.py` |
| Outcomes | `outcomes.jsonl` | `_shared/scripts/log_outcome.py` |

Reading is unrestricted; writing is not. One writer per file is what lets two processes share the
volume with no lock.

## Where the schedule lives

`adapters/hermes/crons.json` is the ONE source for the cron jobs. Four registrars read it —
`adapters/hermes/start.sh` (cloud boot), `adapters/hermes/install.sh`, `adapters/openclaw/install.sh`
and `receiver._sotto_cron_jobs` (the re-registration that fires when the setup wizard sets a
timezone). See [adapters/README.md](../adapters/README.md) for its field contract.
