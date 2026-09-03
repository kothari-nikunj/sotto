# Where your data goes

Sotto reads your messages, mail, calendar, contacts, notes and reminders. Software that asks for
that owes you a precise answer about where it all ends up, so this page names **every** destination,
**every** file it writes, and **how long each one stays**. If something here reads worse than you
expected, that is the point of writing it down.

The one-line version: **there is no Sotto-operated server, and your data still leaves your machine
— it goes to the model provider and the host that *you* chose.** "Self-hosted" removes a third party
of ours from the path. It does not mean local.

## The three parties in a normal install

| Who | What they see | Why |
|---|---|---|
| **Your host** (Railway, or your own Mac in local mode) | Everything, at rest, on the volume you own | It runs the container and holds `$SOTTO_DATA` |
| **Your model provider** (Gemini — the brief pipeline is Gemini-only today, see [MODELS.md](MODELS.md); the chat layer is switchable) | The **text of the brief prompt** — message bodies, email bodies, calendar events, notes, and the facts already in your graph | A brief is one model call, and the material is the prompt |
| **Google** (if you connect Gmail/Calendar) | Nothing new — it is already their data | OAuth read scopes |

Optional, only if you enable them: your **search provider** (Exa / Parallel / Gemini grounding) sees
the *names and companies* of people you are meeting — and, when you ask Sotto to read a specific
link, that link's URL (the provider fetches the page on your behalf) — never your message content.
**Browser Use Cloud** (only with `BROWSER_USE_API_KEY` set — the last rung of the link-reading
ladder, for pages the crawler rungs can't render): their hosted browser sees the URL you asked
about and the rendered page. It is never given credentials or your message content, and without the
key that rung simply doesn't exist.
**X** is optional and read-only in Phase 1. With your own X credentials, Sotto sends exact handles
for confirmation, then requests recent public Posts only for upcoming attendees. If you also grant
`bookmark.read`, X returns the newest bookmarks so Sotto can retain only those authored by an
upcoming attendee. It never calls X people-search, reads Chat content, mirrors a feed, or sends.
**Granola** sees nothing new — Sotto reads *from* it. **DocSend** is its own case: asking Sotto to
read a deck submits **your own email** to the deck's gate, and the sender sees the view (your email,
the timestamp, per-page time) in their DocSend analytics — which is why deck-reading only works when
you ask in chat and refuses to run unattended (a deck already read answers from its saved copy under
`decks/` — a re-ask, or a brief mentioning it, never logs a second view).

**Nothing goes anywhere else.** There is no telemetry, no analytics, no crash reporting, no update
ping that carries content. The only outbound call Sotto makes on its own behalf is a daily
unauthenticated GET of a `VERSION` file on GitHub to see whether a newer release exists.

## What the model provider actually receives

This is the part most easily misread, so concretely: `compose_brief.py` renders your gathered
material into one prompt and posts it. That prompt contains, for the window the brief covers:

- iMessage / WhatsApp **message text**, with sender names resolved from your contacts — a daily
  read carries only the cards the day touched, plus every card with a note or a birthday in the
  next 7 days; the whole address book travels only on the first brief and the weekly pulse
- **email bodies** (trimmed) and subjects
- **calendar events**, titles and attendees
- **Apple Notes and Reminders** in the window (last 7 days / next 3)
- recent **file names** and **browser history titles**, when those sources are enabled
- the **facts already in your knowledge graph** about the people involved
- recent public X Posts and your matching bookmarks for upcoming attendees, only when you connected X

It is sent under your API key, to your provider, and is subject to that provider's retention and
training terms — not ours. Check them. For Google AI Studio keys in particular, free-tier and paid
terms differ on whether prompts may be used to improve their models.

**Local mode does not change this.** `LOCAL-SETUP.md` removes the cloud *host*; the model call is
identical. Fully local would need a local model, which Sotto does not ship — see the last section.

## Everything written to disk, and for how long

All durable state lives under `$SOTTO_DATA` on the volume you own; the one X prep handoff in
`/tmp` is called out below. Delete `$SOTTO_DATA` and Sotto has no memory. The full writer/reader map
is in [ARCHITECTURE.md](ARCHITECTURE.md); this is the **retention** view.

Two things enforce it. `forget.py` (below) deletes a named category **when you ask**. A **daily
sweep at 3:30 AM local** — `runtime/trigger-receiver/retention.py`, fired from the receiver's own
clock — ages out the exhaust **without being asked**, which is why the "Retention" column below is a
number and not a hope. The sweep's table is the single source for that column; anything it does not
name is either aged by its own writer (said so in the row) or never auto-deleted at all.

| File | Contains | Retention |
|---|---|---|
| `knowledge/last_local_snapshot.json` | **The complete raw Bridge payload** — every message, call, note, reminder, file and contact from the last pull | **Overwritten each brief, never auto-deleted.** The 24h TTL only stops it being *reused*, not *stored*. Delete it by hand or with `forget.py --snapshot` (below). |
| `knowledge/people/*.md` · `companies/*.md` | Facts about people and companies, with provenance; a person may also carry immutable `x_user_id`, handle alias history, and the 90-day X resolution cache | **Never auto-deleted** — this is the memory. Superseded facts are archived, not deleted |
| `knowledge/x_link_suggestions.json` | The X resolver's own notes about people the graph has no file for: metadata-only identity candidates too weak (or too conflicted) to link, and the negative results that stop tomorrow's brief re-asking X the same question | **Never auto-deleted** — bounded to 100 suggestions, and negatives self-prune at 90 days on each write. A negative never creates a person file; confirmation UI is a later phase |
| `knowledge/master.md` | The master memory file: who you are, the people around you, your standing rules — **your own stated words**, confirmed before writing, included in every brief and prep prompt | **Never auto-deleted** — editable on the dashboard's Learned page, in chat, or by hand; delete anytime |
| `knowledge/continuity/*.md` | Open loops | Terminal items pruned after 30 days by the resolver, never by the sweep |
| `knowledge/snapshots/<date>.json` | Dated archive copies of the payload, for the golden corpus | 60 days, pruned by the brief that writes them |
| `briefs/<date>_<kind>.json` · `<date>.<kind>.named.json` · `.claim` · `.delivered` | Delivered briefs, which loops each named, and the per-day markers | **60 days** |
| `briefs/<date>.<kind>.payload.json` | The staged wake payload a brief was built from | **7 days** |
| `events/surfaced.jsonl` · `queue.jsonl` | One line per triage verdict | Rotates at 4 MB / 4,000 lines, **and lines older than 90 days are dropped** |
| `events/delivery.jsonl` | Whether each nudge actually landed | **90 days** |
| `events/sends.jsonl` | One metadata-only line per real-effect **attempt** (send, reply, calendar create/delete/RSVP), allowed or refused, carrying `payload_sha256` — the hash of the exact bytes that left, never the bytes | **180 days** — the authorization trail, kept twice as long on purpose |
| `events/drafts.jsonl` | Every draft Sotto offered you, **including its text** | **30 days** |
| `events/outbox.json` | One row per message Sotto composed, **carrying its text only while that text might still have to be sent** | The words are dropped the moment the row closes (delivered, gave up, or aged out); the closed row — id, kind, attempts, reason — is pruned after 7 days |
| `events/delivery-effects-<run>.json` | Run-scoped chase/handoff effects awaiting the host send result | Deleted immediately after that run succeeds or fails; a crashed run's leftover goes at **7 days** |
| `events/bundle-<random>.json` | One staged event bundle per spawned agent run | 7 days, swept by the receiver that stages them |
| `style.json` | Verbatim samples of things **you** wrote | Self-capped by its writer (30/25/25 canonical, 30 recent, 500 keys); the sweep exempts it on that strength |
| `outcomes.jsonl` | What you did with drafts | **90 days** — the learning loop re-reads this whole file after every brief, and a quarter is all it can use |
| `logs/compose_brief.log` | Diagnostics, **including contact identifiers** | Rotates at 4 MB; the sweep's **5 MB** truncation is a ceiling above that, defence in depth |
| `hermes/sessions/` | Hermes' own chat transcripts — one archived per day by the nightly session archive (which keeps transcripts; `/resume` reopens them), plus one per deploy | **Known gap:** Hermes' state is exempt from the sweep, and whether Hermes bounds its own store is not knowable from this repo. Order 10–100 KB/day |
| `cache/research_<date>.json` | Attendee research render cache | 7 days, pruned by the research run |
| `connectors/*.json` | OAuth tokens for connected services | Until you disconnect; never swept |
| `decks/<view_id>.pdf` · `.json` | A DocSend deck you asked Sotto to read — the pages as one PDF, plus the extracted text (the cache that stops a re-ask logging a second view with the sender) | Yours — user-requested artifacts, **never auto-deleted**; kept until you delete the files |
| `config/settings.json` | Setup choices, including the Google account email Sotto excludes from attendee research | Until you change them; never swept |
| `proactive/<date>.json` | The watcher's once-per-day nudge dedup stamps | **30 days** |
| `proactive/pending_offer.json` | The one standing question Sotto last asked you (it can name a person), plus `payload_sha256` when a yes to it would send or write — the hash of the offered content, never the content | Expires 180 min after it is written, at read |
| `cache/meeting_taps.json` · `events/seen.json` | Exactly-once records: which meeting-ends were tapped, which events were already triaged | Bounded rings, overwritten in place |
| `dashboard_sessions.json` | Dashboard login sessions | Expire on idle; pruned on every read |
| `dashboard_audit.jsonl` | One line per dashboard write | **90 days** |

One situational handoff lives outside `$SOTTO_DATA`: `/tmp/sotto_x_context.json` contains recent
Posts and matching bookmarks for the current upcoming-attendee prep. It is mode 0600, overwritten
on every run, and subject to the host OS/container's temporary-file lifetime. Its contents never
enter the graph, caches, event ledgers, briefs archive, or relationship state; a new prep fetches
them again.

**What the sweep will never delete:** `knowledge/` (the graph, `master.md`, style, outcomes, the
continuity ledger), `corpus/`, `decks/`, `connectors/`, `config/`, `preferences.json`,
`intentions.jsonl` and `setup_code`. That guard is checked per path, below every rule in the table,
so a future mistake in the table still cannot reach your memory. A missed sweep — a restart, a box
that was down — costs nothing: every rule is an age, so the next day's sweep removes exactly what
the missed one would have.

## Deleting it — `forget.py`

Deleting the whole `$SOTTO_DATA` directory works and leaves Sotto with no memory at all. When you
want less than that, `sotto-chief-of-staff/tools/forget.py` removes the **exhaust** one named
category at a time and prints a JSON summary of exactly what went, with byte counts:

```bash
SOTTO_DATA=~/SottoData python3 sotto-chief-of-staff/tools/forget.py --snapshot
```

| Verb | Removes |
|---|---|
| `--snapshot` | `knowledge/last_local_snapshot.json` — the raw payload described above |
| `--caches` | `cache/research_*.json`, `cache/calendar_today.json` — both rebuilt on the next run |
| `--logs` | truncates `logs/compose_brief.log` (truncated, not unlinked: a running process holds it open) |
| `--receipts` | `events/delivery.jsonl`, `events/sends.jsonl` |
| `--all` | every one of the above |

**It never touches `knowledge/people/`, `knowledge/companies/` or `knowledge/continuity/`.** That is
the memory — who someone is, what a company builds, what you still owe whom. Deleting *that* is a
decision you make about your own graph, not hygiene a script performs, so it stays with the graph's
own editor rather than a bulk tool. Nothing to delete exits 0: already clean is a success.

`forget.py` and the daily sweep are two views of one answer: every family a verb above deletes is
either swept automatically or listed in `retention.py` with the reason nothing has to (`--caches`
and `--snapshot` are the latter — the research cache prunes its own older siblings, and the snapshot
is overwritten by every brief). A test binds the two, so a verb that grows a new target without a
retention answer fails the suite.

**The snapshot is the one to know about.** It is the rawest, widest file Sotto keeps, it holds
material from sources you may have since turned off, and until Aug 2026 it was absent from the
architecture docs entirely — a reviewer found it before we documented it. Two behaviours worth
stating plainly:

1. **Turning a source off does not erase what was already captured.** The snapshot keeps the last
   payload that included it.
2. **Contacts carry forward.** If a pull returns no contacts, the previous contacts are retained so
   name resolution doesn't collapse. That is deliberate — and it means disabling Contacts leaves the
   previously captured ones on disk until the file is deleted.

## What we do not do

- No Sotto server, account, or hosted component of any kind.
- No telemetry or analytics, of any kind, ever.
- Sotto never sends a message on your behalf. Every outbound message is a draft behind a tap.
- No credential is written to the volume by the dashboard. Provider keys live in your host's
  environment.

## Known gaps, stated rather than hidden

- **The Bridge is distributed as a signed binary and its source is not in this repo.** It is the
  component with Full Disk Access. You are trusting a binary you cannot read. If that is not
  acceptable to you, that is a reasonable place to stop.
- **No local-model mode.** See below.
- **The setup cookie's value is the setup code itself** — no expiry, no logout; rotating the
  code is the revocation story. A deliberate simplicity trade-off (owner, Aug 2026) on a
  single-admin surface defended by per-client lockout, `Secure`, and `no-store`.
- **The `$SOTTO_DATA` volume is not encrypted at rest by Sotto.** It inherits whatever your host
  provides.

## What fully local would take

Three things, none of which exist today: a local model with a large enough context to hold a brief's
payload, a `compose_brief` path that targets it, and an honest quality comparison against the hosted
models so you know what you are trading. Until all three exist, no configuration of Sotto keeps your
message content on your machine, and this page will keep saying so.
