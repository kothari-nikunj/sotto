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
| **Your model provider** (Gemini by default; any of the [supported models](MODELS.md)) | The **text of the brief prompt** — message bodies, email bodies, calendar events, notes, and the facts already in your graph | A brief is one model call, and the material is the prompt |
| **Google** (if you connect Gmail/Calendar) | Nothing new — it is already their data | OAuth read scopes |

Optional, only if you enable them: your **search provider** (Exa / Parallel / Gemini grounding) sees
the *names and companies* of people you are meeting — and, when you ask Sotto to read a specific
link, that link's URL (the provider fetches the page on your behalf) — never your message content.
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

- iMessage / WhatsApp **message text**, with sender names resolved from your contacts
- **email bodies** (trimmed) and subjects
- **calendar events**, titles and attendees
- **Apple Notes and Reminders** in the window (last 7 days / next 3)
- recent **file names** and **browser history titles**, when those sources are enabled
- the **facts already in your knowledge graph** about the people involved

It is sent under your API key, to your provider, and is subject to that provider's retention and
training terms — not ours. Check them. For Google AI Studio keys in particular, free-tier and paid
terms differ on whether prompts may be used to improve their models.

**Local mode does not change this.** `LOCAL-SETUP.md` removes the cloud *host*; the model call is
identical. Fully local would need a local model, which Sotto does not ship — see the last section.

## Everything written to disk, and for how long

All of it lives under `$SOTTO_DATA` on the volume you own. Delete the directory and Sotto has no
memory. The full writer/reader map is in [ARCHITECTURE.md](ARCHITECTURE.md); this is the
**retention** view.

| File | Contains | Retention |
|---|---|---|
| `knowledge/last_local_snapshot.json` | **The complete raw Bridge payload** — every message, call, note, reminder, file and contact from the last pull | **Overwritten each brief, never deleted.** The 24h TTL only stops it being *reused*, not *stored*. Delete it by hand or with `forget.py --snapshot` (below). |
| `knowledge/people/*.md` · `companies/*.md` | Facts about people and companies, with provenance | Indefinite by design — this is the memory. Superseded facts are archived, not deleted |
| `knowledge/master.md` | The master memory file: who you are, the people around you, your standing rules — **your own stated words**, confirmed before writing, included in every brief and prep prompt | Indefinite by design — editable on the dashboard's Learned page, in chat, or by hand; delete anytime |
| `knowledge/continuity/*.md` | Open loops | Terminal items pruned after 30 days |
| `briefs/*.json` · `*.payload.json` | Delivered briefs and the payload each was built from | Indefinite |
| `events/surfaced.jsonl` · `queue.jsonl` | One line per triage verdict | Rotates at 4 MB / 4,000 lines |
| `events/delivery.jsonl` | Whether each nudge actually landed | Rotates with the above |
| `events/delivery-effects-<run>.json` | Run-scoped chase/handoff effects awaiting the host send result | Deleted immediately after that run succeeds or fails; leftovers after a crash are never reused |
| `style.json` | Verbatim samples of things **you** wrote | Per-bucket TTL, 30–90 days |
| `outcomes.jsonl` | What you did with drafts | Indefinite |
| `logs/compose_brief.log` | Diagnostics, **including contact identifiers** | Rotates at 4 MB |
| `cache/research_<date>.json` | Attendee research render cache | 7 days |
| `connectors/*.json` | OAuth tokens for connected services | Until you disconnect |
| `decks/<view_id>.pdf` · `.json` | A DocSend deck you asked Sotto to read — the pages as one PDF, plus the extracted text (the cache that stops a re-ask logging a second view with the sender) | Yours — user-requested artifacts, kept until you delete the files |

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
