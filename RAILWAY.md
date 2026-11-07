# Deploying Sotto to Railway

The maintainer source-build Railway helper is `tools/setup_selfhost_railway.py`. It creates a
fresh service, `/data` volume and public domain, stores a private launch envelope as the
service's `SOTTO_SELFHOST_LAUNCH` variable, and builds the repository's root `Dockerfile` (the
supervised Sotto image: Node chat runtime, trigger receiver, skills and the root bootstrap under
Tini) from a pinned clean checkout. The step-by-step, including the exact config file and every
resume case, is the [maintainer helper reference](docs/MAINTAINER-RAILWAY-HELPER.md#self-hosted-on-railway-supervised-sotto-photon).
This page is the Railway-side reference: what the helper builds, what you do after it, every
environment variable, and troubleshooting. What happens inside the container at boot is described
once, in [docs/ARCHITECTURE.md § Boot](docs/ARCHITECTURE.md#boot).

The helper installs; it does not upgrade. It refuses any service, volume or state it did not
create, so an installed service is not updated by rerunning it (see [Staying updated](#staying-updated)).
The helper covers Photon iMessage only. A Telegram owner bot is a separate, limited self-host
profile with its own operator-file setup: [adapters/sotto/SELFHOST.md](adapters/sotto/SELFHOST.md#telegram-owner-bot).

For an existing hosted deployment, verify its explicit service target and scheduler ownership using the [bounded work runbook](docs/BOUNDED-MODEL-WORK.md#deployment-ownership-and-rollout).

The opt-in Sotto managed and self-host provisioners leave `RAILWAY_DEPLOYMENT_DRAINING_SECONDS`
unset. In the live managed pilot, adding `60` coincided with bootstrap refusing its required process
topology; the same image started after the setting was removed. Railway's mechanism is unconfirmed,
so check and remove that setting on an existing service before updating it. Railway's default
zero-second drain does not guarantee the supervisor time for graceful cleanup on redeploy; restart
recovery must handle an interrupted stop. See [Railway deployment teardown](https://docs.railway.com/deployments/deployment-teardown).
The supervisor signals Node with `SIGUSR2`. The Node Bridge client uses direct authenticated
loopback JSON-RPC without MCP SDK exit hooks. Direct bare-Node `SIGTERM`/`SIGHUP` still lacks the
supervisor's verified process drain and is not the supported shutdown path.

> **New to this? Start with [ONBOARDING.md](ONBOARDING.md)** — the self-host walkthrough.

## 0. Before you start (prerequisites)

- **A Railway account on a paid (or verified) plan.** Volumes and always-on services are not
  available on the free/trial tier, and Sotto needs both — the `/data` volume keeps your
  credentials and memory, and the 6:30/17:30 briefs need the container running around the clock.
- **A fresh, single-environment Railway project**, selected by its exact project and environment
  IDs. The helper refuses a project containing unrelated services.
- **A Gemini API key** ([aistudio.google.com](https://aistudio.google.com) → Get API key).
- **Your own Photon Spectrum Cloud project** with a working iMessage line: its project ID and
  secret, and the physical line's number. Creating a Railway service or installing the Mac Bridge
  does not allocate that line. See [CHANNELS.md](CHANNELS.md#photon-imessage).
- **The owner's phone number** in `+E.164` form — the number you message Sotto from.
- **Python 3.11 or newer, Git and a signed-in Railway CLI** that supports `railway api`.
- **A clean checkout of this repository.** The helper uploads only the pinned tracked files.

## 1. Run the setup helper

Follow [the maintainer helper reference](docs/MAINTAINER-RAILWAY-HELPER.md#self-hosted-on-railway-supervised-sotto-photon):
derive the source pins with `--inspect-source`, write the protected mode-`0600` config outside the
checkout, then run the helper with that config and a state file. Rerun the same command with the
same files after any interruption; the helper reconciles each step with what Railway shows and
never sends a create, upload or start setting twice unless you pass `--retry-unconfirmed`.

## 2. What the helper creates

| Railway object | Value |
|---|---|
| Service | one marked service, built with the `DOCKERFILE` builder from the root `Dockerfile`, `/health` health check |
| Volume | one volume mounted at `/data` (must be a truly empty ext4 filesystem) |
| Domain | one public domain targeting port 8080, created before the first deployment |
| Variables | exactly `SOTTO_SETUP_REQUEST` and `SOTTO_SELFHOST_LAUNCH`; any other non-`RAILWAY_*` variable makes the helper stop |
| Deployments | first the one-time `selfhost_bootstrap.py --prepare` start command, then `selfhost_bootstrap.py --run` on the same volume |

The launch envelope holds the owner and line identity, the generated runtime, Bridge and control
tokens, the Photon and Gemini credentials, the timezone from your config, and the Railway IDs it
is bound to. The bootstrap refuses to start when Railway's injected metadata no longer matches it.
Do not edit either variable by hand.

The `--prepare` deployment prepares the empty volume and then serves only `/health` for at most
15 minutes. The `--run` deployment starts the root supervisor, which runs Node chat and the
receiver; it is also the command for every same-volume restart. The helper never sends a message,
creates a Google OAuth client or makes a paid model probe, so its `receiver_reachable` result
proves reachability only.

## 3. Open your setup link

Use [the private setup surface](ONBOARDING.md#source-created-server-private-setup-surface).

## 4. Connect Google (Gmail + Calendar)

Follow [self-host Google recovery and diagnostics](ONBOARDING.md#self-host-google-recovery-and-diagnostics).

## 5. Connect Granola (optional, one click)

Follow [connected-service protocol and diagnostics](ONBOARDING.md#connected-service-protocol-and-diagnostics).

## 6. Say hello on Photon

Use [the source-created server's Photon receipt checks](ONBOARDING.md#source-created-server-photon-receipt).

## 7. Wait for the first brief

Use [the source-created server's first-brief checks](ONBOARDING.md#source-created-server-first-brief).

## 8. Connect the Mac Bridge (local iMessage/SMS/calls)

Follow [the source-created server's Bridge diagnostics](ONBOARDING.md#source-created-server-bridge-diagnostics).

## Railway template status

No Railway template is published, and none is a verified deployment route: the supported fresh
install is the setup helper, whose private launch envelope binds the service, volume and domain IDs
Railway assigns at creation. Before sharing a Railway template link, create it in Railway's Template
Editor from a clean service and deploy it from a fresh Railway account through first visible
delivery.

## The dashboard

Your deploy URL doubles as a private web dashboard: **`https://<your-domain>/app`** — today at a
glance, open loops with one-tap resolve, the archive of every delivered brief, everyone Sotto knows
(editable, with per-fact confidence), and its learned voice + preferences. Sign in once with your
**setup code** — the same code in the `[sotto] Setup link` line of your boot logs (also on the volume
at `/data/setup_code`) — and the session lasts **30 days**. Five wrong codes lock that visitor's login
for **60 seconds**, doubling on each repeat offense up to a 15-minute ceiling: wait it out and retry
with the code from your boot log rather than guessing again.

## Personal routines (your own scheduled jobs)

Say a recurring job in plain language — *"every Friday at 4, summarize my open loops by person"* —
and Sotto shows the full proposal back; only a distinct later yes creates it. *"What routines do I
have"* lists them, and you can adjust or cancel one the same way. The receiver stores the
definitions in `runtime/owner-routines.json` and fires them from its own minute heartbeat through
the same work queue and outbox as the system jobs.

| Rule | Why it matters |
|---|---|
| **The five system jobs are not routines** | They are declared once in `adapters/sotto/crons.json` and fire on the receiver's clock. A routine can never shadow or remove one. |
| **Cap: 10 active routines** | The 11th asks which one to drop rather than growing an unread schedule. |
| **Delivery** | Routines land on your owner channel, the same target as the briefs. Routines draft; they never send on your behalf. |
| **Each routine keeps the zone it was created in** | The routine stores its timezone with its definition, so a later zone change does not move it; recreate it to move it. |

## Environment variables — full reference

**On the supervised image, the receiver and Node never see Railway's ambient variables.** The
bootstrap clears its environment, and the supervisor builds each child's environment explicitly
from the launch envelope (`adapters/sotto/process_handoff.py`). So the table below has four groups:
the Railway variables you or the helper actually set; values the supervisor injects from the
envelope; values one receiver run hands its own child; and settings the code reads that the
supervised launch never passes, so each keeps its default. The numbers behind the behaviour — tap
windows, valve rates, escalation and staleness thresholds, research caps — are named constants in
the code that owns them, not variables.

**Railway service variables**

| Variable | Purpose | When |
|---|---|---|
| `SOTTO_SELFHOST_LAUNCH` | the supervised self-host runtime's private launch envelope (manifest, generated tokens, Photon and Gemini credentials, timezone), bound to this service, `/data` volume, domain and setup marker. Written once by `tools/setup_selfhost_railway.py` and read by `adapters/sotto/selfhost_bootstrap.py`; do not edit it — a mismatch with Railway's injected metadata refuses startup. | helper only (self-host) |
| `SOTTO_SETUP_REQUEST` | ownership marker written by the setup helper; the bootstrap requires it to match the envelope, and a resume refuses a service whose marker is missing or different ("not owned by this setup"). Leave it alone. | helper only |
| `SOTTO_MANAGED_LAUNCH` | the same kind of private launch envelope for a Sotto Cloud managed tenant, read by `adapters/sotto/managed_bootstrap.py`. Written by Sotto's own tenant provisioning; never set it on a self-host. | Sotto Cloud provisioning only |
| `SOTTO_PROXY_TENANTS` | Proxy service only: configured tenant token hashes, expiry and admission budgets; never set on a tenant instance. | proxy operator only |
| *(do not set)* `PORT` | injected by Railway; the self-host bootstrap requires `8080` and the receiver binds it. | — |
| *(do not set)* `RAILWAY_PUBLIC_DOMAIN` · `RAILWAY_PROJECT_ID` · `RAILWAY_ENVIRONMENT_ID` · `RAILWAY_SERVICE_ID` · `RAILWAY_VOLUME_MOUNT_PATH` · `RAILWAY_VOLUME_NAME` | injected by Railway; the bootstrap checks them against the envelope before starting, and the setup link and Google callback are built from the public domain. | — |
| *(do not set)* `RAILWAY_DEPLOYMENT_ID` | Railway injects the deployment revision. The model proxy records it as `proxy_deployment`; caller `application` and `deployment` come from trusted proxy tenant configuration. Authenticated tenant ID is the report owner. | — |

**Set by the supervisor from the launch envelope** (the receiver's whole environment; none of
these are Railway variables)

| Variable | Purpose | When |
|---|---|---|
| *(do not set — the supervisor injects it)* `SOTTO_DATA` | the volume path, `/data`. Skill scripts the receiver runs inherit it. | — |
| *(do not set — the supervisor injects it)* `SOTTO_HOME` | the receiver's private credential home, always `$SOTTO_DATA/sotto-home` (`/data/sotto-home`): the Google token and client secret live there, owned by UID 10001 with mode 0700. `prepare_fresh.py` creates it; the launch envelope names it and the supervisor passes only that exact path. | — |
| *(do not set — the supervisor injects it)* `SOTTO_RUNTIME_PROFILE` | the supervised runtime's fixed-five profile (`managed-photon-fixed-five-v1`, `self-host-photon-fixed-five-v1` or `self-host-telegram-fixed-five-v1`), which gates its owner tools and schedules. | — |
| *(do not set — the supervisor injects it)* `SOTTO_ROUTINE_EPOCH` | the current 32-hex replacement-epoch id that binds personal routines and owner tools to this installation; the supervisor drops any inherited value and sets only its own. | — |
| *(do not set — the supervisor injects them)* `SOTTO_CHAT_RUNTIME_URL` · `SOTTO_CHAT_RUNTIME_TOKEN` | the loopback address (`http://127.0.0.1:<runtime port>`; 8081 under the setup helper) and generated bearer the receiver uses to reach the Node chat runtime, rendered from the launch manifest. | — |
| *(do not set — the supervisor injects it)* `SOTTO_DEPLOYMENT_MODE` | `self_host` or `managed`, from the manifest. | — |
| *(do not set — the supervisor injects it)* `SOTTO_TENANT_ID` | the stable tenant identity; capability and activation receipts must match it. | — |
| *(do not set — the supervisor injects it)* `SOTTO_VOLUME_ID` | the identity of the attached volume, checked against the `.sotto-volume.json` marker `prepare_fresh.py` wrote; a missing or mismatched mount stops startup. | — |
| *(do not set — the supervisor injects it)* `SOTTO_CRON_DELIVER` | the one delivery channel (`photon` or `telegram`) for everything scheduled — briefs, midday digest, weekly pulse, proactive watcher and personal routines. It is also the nudge delivery gate: the channel's probe (Photon's authenticated owner DM, Telegram's captured chat id) must read linked. | — |
| *(do not set — the supervisor injects them)* `SOTTO_MCP_TOKEN` · `SOTTO_CONTROL_TOKEN` | the Bridge root bearer the helper generated (the Bridge's lanes and the operator surfaces take it; Node gets only the derived read bearer) and the independent control credential (bootstrap and model-lease renewal; kept out of Node and skill environments). | — |
| *(do not set — the supervisor injects them)* `SOTTO_SKILLS_ROOT` · `SOTTO_TRIGGER_PORT` | the packaged skill tree (`/app/sotto-skills`) and the receiver's port. | — |
| *(do not set — the supervisor injects it)* `SOTTO_TIMEZONE` | the IANA zone from your setup config (`sotto_runtime.timezone`). It wins over the `/setup` tile's browser detection, which only applies when no zone is configured. A conflicting timezone POST is rejected without changing saved settings or schedule stamps. The briefs run at **6:30 morning / 17:30 evening** in this zone. | — |
| *(do not set — the supervisor injects it)* `SOTTO_BACKGROUND_UNMETERED` | self-host only: `true` only when the protected helper config explicitly selects `sotto_runtime.background_unmetered: true`; otherwise `false`. Allows progressive history learning and Dreamer against the direct Gemini key without a dollar cap. Source consent remains required. Managed launches cannot use this override. | — |
| *(do not set — the supervisor injects it)* `GOOGLE_AI_API_KEY` | self-host only: the direct Gemini key from the envelope, used by the brief/prep/follow-up/triage pipeline. Managed tenants use the model proxy instead. | — |
| *(do not set — the supervisor injects them)* `PHOTON_PROJECT_ID` · `PHOTON_PROJECT_SECRET` · `PHOTON_HOME_CHANNEL` · `PHOTON_ALLOWED_USERS` · `SOTTO_IMESSAGE_NUMBER` | Photon profiles: the project credentials, the owner's number (home channel and the only allowed user) and the physical Sotto line. | — |
| *(do not set — the supervisor injects them)* `TELEGRAM_BOT_TOKEN` · `TELEGRAM_ALLOWED_USERS` · `TELEGRAM_HOME_CHANNEL` | Telegram profile only: the bot token and the captured numeric owner id. | — |
| *(do not set — the supervisor injects them)* `SOTTO_MODEL_PROXY_URL` · `SOTTO_MODEL_PROXY_TOKEN` | managed only: the model proxy root (pipeline calls use `/native/v1beta`) and the tenant bearer. The receiver renews its expiry through the proxy; bearer rotation remains an explicit operator change. | — |

The setup helper's envelope can also carry model and research overrides (`SOTTO_BRIEF_MODEL`,
`SOTTO_GEMINI_MODEL`, `SOTTO_FALLBACK_MODEL`, `SOTTO_RESEARCH_DEEP`) in its `receiverOverrides`
block; the helper writes `SOTTO_TIMEZONE` and the explicit/default-off
`SOTTO_BACKGROUND_UNMETERED` choice there.

**Internal — set by one receiver run for its child**

| Variable | Purpose | When |
|---|---|---|
| *(internal — do not set)* `SOTTO_CAPTURED_SOURCES` · `SOTTO_PROACTIVE_CAPTURED_SOURCES` | set by the receiver on one child run: the JSON list of sources the owner had granted when an attended live read (Granola history, triage queue) or a proactive run started, so that child reads only those. | — |
| *(internal — do not set)* `SOTTO_SKILL_COMPOSITION_PROOF` | set by the receiver on a meeting-prep, ask-research, follow-up, relationship-pulse or proactive skill run: the source-revision proof the child rechecks before it acts. | — |
| *(internal — do not set)* `SOTTO_READ_ONLY` | `1` on an attended read child: knowledge queries then never repair or write the graph. | — |
| *(internal — do not set)* `SOTTO_SETUP_LOG_FD` | the supervisor's one-use descriptor for the receiver's private setup-link boot line. Removed and closed before worker threads start; ordinary receiver/Node output stays suppressed. | — |
| *(internal — do not set)* `SOTTO_UNATTENDED` | `1` on every skill run the receiver spawns — the seam `google_action.py`'s send gate reads. | — |

**Defaults the supervised launch does not change.** The code still reads each name below, and
[HOW-SOTTO-DECIDES.md](docs/HOW-SOTTO-DECIDES.md) refers to the rules by these names. No launch path
passes them to the receiver, so on the supervised image each one is fixed at the default shown.

| Variable | Default and meaning | When |
|---|---|---|
| `SOTTO_USER_EMAIL` | derived from your connected Google account (the `From` of your own sent mail, persisted as `google_account_email` in `/data/config/settings.json`); used to exclude yourself from attendee research and post-meeting taps, and to list you as a guest on invites Sotto creates. | fixed |
| `SOTTO_GEMINI_MODEL` | `gemini-3.8-flash`: the compose default and the model for native grounding, url_context and DocSend vision. Must be 1M-context. | envelope override only |
| `SOTTO_BRIEF_MODEL` | Gemini; `openai/<model>` or `anthropic/<model>` would run the compose calls on another family, which also needs that family's key — and the launch passes no `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `SOTTO_OPENAI_BASE_URL`. | envelope override only |
| `SOTTO_FALLBACK_MODEL` · `SOTTO_FALLBACK_API_KEY` | `gemini-3-flash-preview`, same key: the backup model for a foreground brief on a 429/5xx/timeout. A cross-family value is refused and the backup must clear the brief's 400K context floor. Background history extraction and Dreamer use one primary attempt. | envelope override only (model) |
| `SOTTO_CRITIC` | `auto`: the brief's second-pass Gemini critic + revise gate skips its two extra calls on a small/low-risk brief (rendered source payload `<15000` chars AND `≤5` actions) and runs them otherwise. | fixed |
| `SOTTO_RESEARCH_DEEP` | `1`: run the second, recency-focused attendee-research pass. | envelope override only |
| `SOTTO_PREWARM_RESEARCH` | on: first-run seed background-researches your most-frequent contacts (stored as clearly-labeled low-confidence notes). | fixed |
| `SOTTO_TRIAGE_MODEL` | `gemini-3.5-flash-lite`: the cheap Tier-1 event-triage model. | fixed (events) |
| `SOTTO_EMAIL_POLL_SECS` | `90`: cloud-side Gmail poll interval feeding new inbox mail into the triage funnel. | fixed (events) |
| `SOTTO_EVENT_COOLDOWN_MIN` | `20`: at most one event nudge per thread per this many minutes; suppressed events queue for the digest. | fixed (events) |
| `SOTTO_NUDGE_BUDGET` | `4` unsolicited nudges per **local day**. “Fewer nudges” halves the effective cap, “no nudges” sets it to zero, and “more nudges” restores this ceiling; chat cannot exceed it. At zero every unsolicited path is suppressed, including held promotions, proactive work and pending outbox sends. Scheduled briefs, digests and direct requests remain available. | fixed (events) |
| `SOTTO_TAP_MAX_PER_DAY` | `3` post-meeting taps per **local day**, enforced once at dispatch and independent of the nudge budget. | fixed (events) |
| `SOTTO_MEETING_TAP` | `1`: the post-meeting tap ("your 2:00 PM with Sarah wrapped — want me to send the follow-up?"). | fixed (events) |
| `SOTTO_CALENDAR_NUDGES` | `1`: nudges on changes to the imminent calendar — a decline, a last-minute invite, a move, a cancellation. Exempt from the daily budget; quiet hours/snooze/mutes still hold it. | fixed (events) |
| `SOTTO_CALENDAR_REFRESH_SECS` | `900`: today's-calendar refresh. Powers the in-meeting hold, the dashboard's calendar, and post-meeting tap detection. A cache older than 2 intervals — or stamped with another day — is never used to hold a nudge. | fixed (events) |
| `SOTTO_QUIET_START` / `SOTTO_QUIET_END` | `21` / `7`: no nudges 9pm–7am, shared by the proactive watcher, the event funnel and the release valve; a missed call from a VIP is the one carve-out. `/quiet for 2h` pauses nudges for a while ([SELFHOST.md](adapters/sotto/SELFHOST.md#owner-nudge-pause)). | fixed |
| `SOTTO_CHASE_AFTER_DAYS` | `3` silent days before something you're **waiting on** becomes chase-eligible, and the gap between chases. Something you're owed **never expires on its own — not by age, and not by its deadline** (a passed deadline just makes it chase-eligible immediately): it closes when they actually deliver, gets at most **two** chases (one nudge a day, budget applies, and only a chase that was actually DELIVERED counts), and then Sotto stops and asks you by name. | fixed |
| `SOTTO_BIRTHDAY_LEAD_DAYS` | `3` days ahead of a birthday the gift-idea nudge fires. Each fires once per person per year, and the day-of one is skipped when a brief already delivered that day. | fixed |
| `SOTTO_USER_NAME` | unset: group messages **always** queue and never nudge (no @-mention detection). | fixed (events) |
| `SOTTO_VALVE` | `1`: the release valve lets an event held by cooldown/quiet hours/catch-up/budget/a meeting out as a nudge once the hold lifts. | fixed (events) |
| `SOTTO_OUTBOX` | `1`: a brief or nudge the channel didn't acknowledge is retried every minute, backing off to a 15-min cap, until it lands, ages out, or gives up loudly. Every message is written down before it is sent. | fixed (delivery) |
| `SOTTO_DIGEST_MIN` | `8` queued **signals from people you know** before the 12:30 midday digest delivers; below it the digest stays silent. The window starts at the last brief that actually delivered. | fixed (events) |
| `SOTTO_PROACTIVE` · `SOTTO_DIGEST` · `SOTTO_PROACTIVE_CRON` | on, on, `*/15 * * * *`: the proactive watcher and the midday digest always run on the receiver's schedule. The fixed-five profile refuses any other value. | fixed |
| `SOTTO_DASHBOARD` · `SOTTO_UPDATE_CHECK` | on: the `/app` dashboard, and the once-a-day "a newer Sotto is published" check (one GET of the repo's `VERSION`; silent on an unstamped build). | fixed |
| `SOTTO_VISUAL_BRIEFS` | `1`: Photon morning/evening briefs and focused meeting backgrounds use the shared card renderer ([visual briefs](docs/VISUAL-BRIEFS.md)). | fixed |
| `SOTTO_SETUP_CODE` | unset: the setup code is generated once and persisted at `/data/setup_code`. | fixed |
| `SOTTO_TRIGGER_TOKEN` | unset: the Bridge wake-push authenticates with the Bridge bearer. | fixed |
| `PHOTON_ALLOW_ALL_USERS` | unset: Photon stays restricted to the owner. Any value other than unset or `false` is treated as unrestricted, so owner activation never completes and Photon never counts as linked. | fixed |
| `GOOGLE_OAUTH_CLIENT_JSON` | unset: paste the client JSON in `/setup` instead. | fixed |
| `GRANOLA_API_KEY` | unset: Granola connects through its OAuth tile (§5). | fixed |
| `EXA_API_KEY` · `PARALLEL_API_KEY` · `BROWSER_USE_API_KEY` | unset: web search and deep research use Gemini grounding, and link reads use Gemini url_context. The Connections page lists these providers read-only. | fixed (research) |
| `X_BEARER_TOKEN` · `X_USER_ACCESS_TOKEN` · `X_OWNER_USER_ID` | unset: no X attendee context; `/api/overview` reports X as `unconfigured`. | fixed (X) |

**Bridge-side settings** are read by the Sotto Bridge on your Mac, not by Railway:

| Variable | Purpose | When |
|---|---|---|
| `SOTTO_EVENTS` | **Bridge-side** kill switch for real-time event push (`/bridge/events`). Default on; `0` stops the watcher entirely. | optional (Bridge) |
| `SOTTO_EVENTS_TICK_SECS` | **Bridge-side** event-watcher tick interval in seconds (default `3`). | optional (Bridge) |
| `SOTTO_WAKE_PUSH` | **Bridge-side** — fires the brief/nudge the moment your Mac wakes. Default **on**; `=0` (or `false`) disables (the 6:30/17:30 schedule still fires). The schedule↔wake-push deliver-once gate makes double-delivery impossible. | optional (Bridge) |
| `SOTTO_WAKE_MORNING_MIN` / `SOTTO_WAKE_MORNING_CUTOFF` | **Bridge-side** morning wake-push window in minutes-past-midnight — wake past this and before the cutoff triggers the morning brief (defaults `420` / `1080` = **7:00 – 18:00**). | optional (Bridge) |
| `SOTTO_WAKE_EVENING_MIN` / `SOTTO_WAKE_EVENING_CUTOFF` | **Bridge-side** evening wake-push window (defaults `1050` / `1380` = **17:30 – 23:00**). | optional (Bridge) |

### Removed settings

These were variables once and are now **named constants in the code** (Aug 2026 — a setting needs a
reason a default can't serve). Nothing reads them.

| Removed variable | Now the constant |
|---|---|
| `SOTTO_ESCALATION_WINDOW_MIN` | `ESCALATION_WINDOW_MIN_DEFAULT` (45 min) |
| `SOTTO_EVENT_MAX_AGE_MIN` | `EVENT_MAX_AGE_MIN` (30 min) |
| `SOTTO_VIP_PRIORITY` | nothing — the volume-based VIP rule was removed; VIP is a name you state (`vip`) or a typed `family_of` relation |
| `SOTTO_VALVE_MAX_AGE_MIN` · `SOTTO_VALVE_MAX_PER_HOUR` · `SOTTO_VALVE_INTERVAL_SECS` | `VALVE_MAX_AGE_MIN` (240) · `VALVE_MAX_PER_HOUR` (2) · `VALVE_INTERVAL_SECS_DEFAULT` |
| `SOTTO_TAP_GRACE_MIN` · `SOTTO_TAP_LOOKBACK_MIN` · `SOTTO_TAP_SKIP_INTERNAL` | `TAP_GRACE_MIN_DEFAULT` · `TAP_LOOKBACK_INTERVALS` · `TAP_SKIP_INTERNAL` |
| `SOTTO_PROACTIVE_LEAD_MIN` | `PROACTIVE_LEAD_MIN` (45 min) |
| `SOTTO_STALE_AGE_DAYS` · `SOTTO_STALE_SURFACED` | `STALE_AGE_DAYS` (4) · `STALE_SURFACED` (3) |
| `SOTTO_RESEARCH_RECENCY_DAYS` | `DEFAULT_RECENCY_DAYS` (90) |
| `SOTTO_PREWARM_MAX` | `MAX_PREWARM` (12) |
| `SOTTO_RESEARCH_CONCURRENCY` | `RESEARCH_CONCURRENCY` (5) |

## Staying updated

**The rule, in one sentence: Sotto tells you when it's out of date, and updating an installed
service is a manual operator job — no command in this repository performs it.** Your server checks
the published `VERSION` once a day and, when a newer build exists, says so. Nothing updates itself.

**Where it says so.** Three quiet places, all fed by that same daily check: one line at the foot of
`/setup` (your Integrations page), one subdued line at the top of the dashboard's Today page, and
**one line in your next brief** — morning or evening, whichever comes first, once per published
version and never again. A new version is housekeeping, so it never arrives as its own message, it
never spends your daily interrupt budget, and it never repeats.

**Why there is no upgrade command.** The setup helper only creates fresh installs and refuses a
service it did not just create, and a helper-created service has no repository source for Railway
to redeploy from. The `--run` start command is the same-volume restart path for the release that
prepared the volume; restarting does not change the code it runs.

**The runtime comes with the build.** The root Dockerfile builds the chat runtime, receiver and
skills from this repository, with base images pinned by digest and Python and npm dependencies
installed from their committed hash locks. There is nothing to hand-update on the volume.

**The Mac Bridge updates itself.** The Bridge checks
[Releases](https://github.com/kothari-nikunj/sotto/releases/latest) once a day and shows
**Update available** in its menu when a newer signed build exists; one click downloads the DMG,
verifies the signature (same Apple Team as the app you're running — anything else is refused),
installs in place and relaunches. Your pairing (host + token) and Full Disk Access persist (the
signing identity never changes, so macOS treats it as the same app). Prefer manual? Downloading the
DMG and dragging it over the old app still works exactly the same way.
**Current access policy:** choosing Railway for self-host setup requires no Bridge invitation code.
A completed self-host install also stays unlocked when a legacy code expires. Invited Sotto Cloud
continues to require its verified account and matching pairing receipt. Neither path grants Mac
source permissions; Full Disk Access and your source choices still apply. The one-time code gate
introduced in 1.2.7 is historical behavior, not a requirement for current self-host updates.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Helper stops with `create_unconfirmed`, `deployment_pending` or `preparation_unconfirmed` | Rerun the same command with the same config and state; only follow its `--retry-unconfirmed` instruction after checking Railway as it says. Do not delete the state or deploy by hand ([maintainer helper reference](docs/MAINTAINER-RAILWAY-HELPER.md#self-hosted-on-railway-supervised-sotto-photon)). |
| Deploy log: `self-host bootstrap unavailable; review tenant before retry` | The bootstrap refused its input: the envelope no longer matches Railway's service, domain, volume or setup marker, `/data` is not the expected empty ext4 mount on `--prepare`, or Tini is not PID 1. Inspect the exact service; do not edit the variables or rerun `--prepare` on a prepared volume. |
| Setup link in the logs says `localhost` | No public domain on this service. The helper creates it before the first deployment, so inspect that exact service, then use the freshly printed link. |
| Briefs never arrive (chat may still reply) | Usually a bad Gemini key or exhausted quota. Tail **`/debug/brief-log`** for the last brief attempt; it needs the Bridge bearer, which the helper keeps in its protected state file. |
| Google disconnects after ~a week (day 8) | OAuth consent screen left in **Testing** — its refresh tokens expire after ~7 days. Publish the app to **In production** (§4 — no Google verification needed for your own data), then reconnect once. |
| `/setup` (or `/google/*`) returns **403 Forbidden** | Sign in to `/app` as the self-host owner, then open Integrations. Alternatively use the full setup link from deploy logs. Expired sessions need sign-in again; Cloud lease sessions do not grant bootstrap setup access. |
| Lost the setup link | It reprints on **every boot** (restart and check the logs), or read `/data/setup_code` on the volume. |
| Photon tile never reads connected | Send the hello from the exact owner phone in your config to the configured Sotto line; an Apple ID email never activates. The receipt is bound to the Photon project. |
| The Bridge's **"Meeting notes (Granola) & more → Open…"** button opens a **403** page | You paired with an older Bridge or an older host: newer pairing links carry the setup code, and the button then opens `/setup` correctly. Meanwhile: open the `[sotto] Setup link` from your deploy logs once in the same browser — the cookie then covers it. Re-pairing (paste the current setup page's link into the Bridge) also fixes it for good. |
| Briefs fire at the wrong hour | The zone comes from `sotto_runtime.timezone` in your setup config and `/setup` tile ④ shows it. A helper-created install cannot change it in place. |
| Mac can't reach the host | Check the menu bar dot and re-pair from the current `/setup` link. |
| Local data missing from briefs (messages/calls/contacts empty) | Run `sotto-bridged --doctor` on the Mac — it names each source `ok` / `needs Full Disk Access` / `unavailable` and prints the exact FDA fix (grant Full Disk Access to the *right* app: the `.app` for GUI runs, the terminal for CLI runs). Exit `0` = all sources read. |


### Model proxy spending cutoff (operator only)

The proxy tenant configuration accepts `budget_cents: null` to disable the spending admission cutoff while retaining usage records and credential checks. Numeric budgets return HTTP 402 with `sotto_budget_exhausted` when exhausted; upstream HTTP 429 remains a separate transient rate-limit condition. The personal pilot uses no spending cutoff at the owner’s request.

### Background history learning on self-host

Progressive history learning and Dreamer run only through a model proxy with a finite
`budget_cents`, or with the explicit unmetered opt-in `SOTTO_BACKGROUND_UNMETERED=true`. The
helper's optional protected `sotto_runtime.background_unmetered` JSON boolean defaults to `false`.
Without an explicit `true` choice, background learning stays held before any source fetch and reports the hold
in `knowledge/history-state.json`. Ordinary chat, briefs and the first useful look are unaffected:
they use recent context. Managed tenants route background work through their proxy. The choice is
fixed when the volume is installed; changing it means a fresh installation.

The proxy side of that contract, for reference: a proxy-backed cycle authenticates a content-free
`GET /v1/capabilities/background-budget` before it reads history; unsupported, unauthorized,
unlimited or exhausted capability responses keep background work held. The proxy reserves a
conservative $2 maximum allowance before each upstream call and records usage in its existing
SQLite ledger; this bounds admission and is not a reconstruction of the provider invoice. Bearer
expiry is rechecked inside the reservation transaction; an expired bearer returns 401 without
reserving spend or contacting the provider.

Personal comparison pilot: all 11 Bridge source types can be enabled with explicit consent in the same source settings as self-host. The proactive and digest schedules run with output to the same Photon destination as briefs. Quiet hours, interrupt gates and duplicate suppression still apply.

## Managed operations and recovery

The runtime supervisor recycles the container if either essential process exits. Startup checks
the attached volume before launching writers. See [tenant volume operations](adapters/sotto/VOLUME.md)
for model lease renewal and scoped Bridge revocation. There is no tenant export or restore: a lost
volume is replaced by a fresh installation. Fleet control-state recovery and automated backups
still require operator verification.
