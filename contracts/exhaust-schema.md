# Exhaust schema (the Sotto data, on `$SOTTO_DATA` — Railway volume)

Byte-compatible with today's iCloud `Sotto/` layout (PORT SOURCE: knowledge_files.rs, continuity.rs,
style-profile.ts) so existing files migrate as-is. Encrypted at rest, per-tenant.

Every row names its **owning writer** — the one script allowed to write that shape. Readers are
many; writers are one. Paths are relative to `$SOTTO_DATA/`; scripts are relative to `sotto-chief-of-staff/`.

| Path | What it is | Written by |
|---|---|---|
| `knowledge/people/<slug>.md` | person — frontmatter + body (below) | `_shared/knowledge/knowledge_update.py` (`_shared/knowledge/knowledge_edit.py` routes user edits through its `apply()`; `_shared/knowledge/knowledge.py` is the model + serializer both use) |
| `knowledge/companies/<slug>.md` | company — frontmatter + About/News/Context (below) | `_shared/knowledge/knowledge_update.py` (`meeting-prep/scripts/persist_prep.py` writes company research and `_shared/knowledge/knowledge_edit.py --op company-about` writes user corrections — both *through* its `apply()`; `company_knowledge()` is the one read side) |
| `knowledge/continuity/<anchor>.md` | open loop — frontmatter only | `morning-brief/scripts/continuity_resolve.py` (`_shared/scripts/retune_apply.py` and `_shared/knowledge/knowledge_edit.py --op loop*` mutate through its loader/persister; `_shared/scripts/ledger_io.py` is the shared READ side) |
| `knowledge/relationship_state.json` | attention queue + insights + per-contact history | `relationship-pulse/scripts/relationship_pulse.py` |
| `style.json` | writing-style fingerprint (buckets + per_person) | `_shared/scripts/style_extract.py` |
| `preferences.json` | User-stated `explicit` instructions; legacy inferred fields preserved but ignored | `_shared/scripts/preferences.py` — chat and dashboard invoke the same writer; Learn never rewrites it |
| `outcomes.jsonl` | action outcomes + explicit item usefulness examples (one JSON per line) | `_shared/scripts/log_outcome.py` |
| `events/surfaced.jsonl` · `events/queue.jsonl` | the Record: one row per verdict, and the work list the digest/valve consume | `event-triage/scripts/triage_event.py` |
| `briefs/<date>_<type>.json` | delivered briefs | `_shared/scripts/compose_brief.py` (`_archive_brief`) |
| `briefs/<date>.<type>.delivered` | per-day delivery flag (the deliver-once claim) | `_shared/scripts/brief_marker.py` |

## person `<slug>.md`
```yaml
---
schema: 1
canonical_id: c_a8f3e2
name: Sarah Chen
company: Acme Corp          # optional
title: CTO                  # optional
identifiers: ["+15551234567", "sarah@acme.com"]
linkedin: https://…        # optional
last_researched: 2026-06-20 # optional
updated_at: 2026-06-23T07:00:00Z
updated_by: brief_extraction
summary_refs: [f_a3e8c1b2f0]  # optional Dreamer selection; absent in legacy files
relations:                   # optional; omitted entirely when there are none
- type: introduced_by        # CLOSED vocabulary — see below
  slug: c_9f21ab             # the OTHER person's file stem (their canonical_id)
  name: Vishnu Sharma        # their display name when the edge was written
  date: 2026-05-14           # optional — when it happened
  source: brief_extraction   # brief_extraction | user_edit
  confidence: 0.95
facts:
  f_a3e8c1b2f0:
    text: "CTO at Acme Corp"
    type: milestone          # milestone|relationship_change|working_style|context|interest|communication_pattern|…
    status: active           # active|archived
    seen: 3
    conf: 0.95               # 0..1, decays 0.08/wk, floor 0.4
    source: brief_extraction
    source_ref: ""
    evidence_refs: ["gmail:message-id"]  # optional, at most 64 independent references
    first: 2026-01-15
    last: 2026-02-18
    # archived_text: "<old>"  # only when superseded
---

## Summary
…

## Facts
- CTO at Acme Corp        # rendered: active facts, conf DESC, last DESC, first ASC, id ASC

## Talking Points
- …

## Recent Activity
- …

## Notes
…
```

**Relations** — one sentence: *a relation is a typed edge between two people Sotto knows, stored on
both ends, readable as a sentence.* The vocabulary is closed (an open one is how graphs rot); each
type names its inverse, and the writer stores both halves together so the two sides cannot drift:

| type | inverse | reads as |
|---|---|---|
| `introduced_by` | `introduced` | "Introduced to you by Vishnu Sharma (May 2026)" |
| `introduced` | `introduced_by` | "Introduced Priya Patel to you" |
| `works_with` | `works_with` | "Works with Dana Reed" |
| `family_of` | `family_of` | "Family of Dana Reed" |
| `partner_of` | `partner_of` | "Partner of Dana Reed" |
| `met_through` | `connected` | "Met through Dana Reed" |
| `connected` | `met_through` | "Connected you with Priya Patel" |

Sentences read from the USER's vantage. The one writer is
`_shared/knowledge/knowledge_update.py` (`link_relation` / `unlink_relation`, and
`merge_person_files`, which repoints every back-reference when two files become one); a type
outside the table is dropped on read and refused on write. `knowledge_query.py` packs them as the
`&` line of a person block; the dashboard's `GET /api/people/<slug>` returns them as
`[{type, slug, name, sentence}]`. Because an edge is one fact stored on two files, that write is
journaled: see the graph's crash-safety paragraph in
[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) — an interrupted graph update finishes the next
time anything touches the graph.

## company `<slug>.md`
```yaml
---
schema: 1
normalized: commenda           # the slug; the file's own name
aliases: ["Commenda", "Commenda Inc."]   # every name form seen — the anti-fork key
domain: commenda.io            # optional; the second resolution key
updated_at: 2026-08-11T07:00:00Z
updated_by: web_research       # who owns the ABOUT — brief_extraction | web_research | user_edit
last_news_update: 2026-08-11   # optional
last_researched: 2026-08-11    # optional — set by research, the company's `last_researched`
---

## About
Builds: Global tax filing tied to each payment.

Built after a misfiled quarter in three countries.

The post-EOR shift; competes with Deel and Remote.

## News
- [Series A led by Nexus, June 2026](https://tc.com/a)      # deduped by URL, capped at MAX_NEWS_ITEMS

## Context
…                                                            # append-only, capped at MAX_COMPANY_CONTEXT_CHARS
```

A company deliberately has **no facts map** — the shape stays byte-compatible with the Mac app's
`knowledge_files.rs`, so it carries per-FILE provenance instead of per-fact confidence. `about` is
the only field a rewrite can destroy (news dedupes by URL, context appends), which is why
`updated_by` names *who owns the About*: an update that doesn't touch it leaves the stamp alone, so
a `user_edit` correction survives every later news write and research declines to overwrite it.
`about` is REPLACED, never appended. Correction lane: `knowledge_edit.py --op company-about`.

## continuity `<anchor>.md` (frontmatter only)
```yaml
---
anchor_key: "thread:abc123"      # thread:{id}  OR  {channel}:{family}:{contact}
action_type: reply
channel: email
contact_name: Sarah Chen
status: open                      # open|waiting|failed|blocked | resolved|dismissed|expired
created_at: 2026-06-20
resolved_at: 2026-06-23           # when terminal
resolution: replied              # replied|meeting_passed|…
times_surfaced: 2
summary: "…"
meeting_time: "Tomorrow 3pm"     # optional
---
```
Terminal items pruned after 30 days. Active = open|waiting|failed|blocked.

## Bridge `read_local` → LocalData (the 16-source on-device contract)

The Sotto Bridge's `read_local` MCP tool returns this payload (full JSON Schema in
`contracts/local_data.schema.json`). Field names + per-item shapes are byte-compatible with the Mac
app's `extract_local_data` (PORT SOURCE: app/src-tauri/src/commands/brief.rs) and with what the
consumer reads (`sotto-chief-of-staff/_shared/scripts/compose_brief.py`). Messages are **flat
per-message arrays** — the consumer groups them into threads. `granola_meetings` is intentionally
**OUT** of the Bridge: Hermes owns Granola via its own MCP.

```jsonc
{
  "generated_at": "2026-06-24T07:00:00Z",   // RFC3339 UTC
  "window_hours": 24,

  // --- messages (flat) ---
  "imessage": [
    { "handle": "+15551234567", "is_from_me": false, "timestamp": "2026-06-24 06:55:01",
      "text": "are we still on?", "is_group_chat": false }
  ],
  "whatsapp": [
    { "contact_jid": "15551234567@s.whatsapp.net", "partner_name": "Sarah Chen",
      "is_from_me": false, "timestamp": "2026-06-24 06:40:00", "text": "ping", "is_group_chat": false }
  ],
  "deferred_unread_imessage": [
    { "handle": "+15551234567", "timestamp": "2026-06-19 09:00:00", "text": "you around?", "days_old": 5 }
  ],
  "deferred_unread_whatsapp": [
    { "contact_jid": "…@s.whatsapp.net", "partner_name": "Sarah Chen",
      "timestamp": "2026-06-19 09:00:00", "text": "ping", "unread_count": 2, "days_old": 5 }
  ],

  // --- people + tasks ---
  // Contacts: on a DAILY read (≤48h) only the cards today's messages/calls/groups touched, plus
  // every card with a note or a birthday in the next 7 days; a wide read (first brief, weekly
  // pulse) carries them all. `contacts_total` is the Mac's card count either way.
  "contacts": [ { "name": "Sarah Chen", "phones": ["+15551234567"], "emails": ["sarah@acme.com"], "notes": "met at conf" } ],
  "contacts_total": 2102,
  // Reminders: incomplete only, LAST 7 DAYS ∪ NEXT 3 (see "The local-source windows" below).
  // `created_date` is set ONLY on undated ones — it is why they are here. ≤50 per store.
  "reminders": [ { "title": "Call dentist", "due_date": "2026-06-24 15:00:00" },
                 { "title": "Look at Northwind", "created_date": "2026-06-22 21:04:00" } ],

  // --- calls ---
  "calls": [
    { "phone": "+15551234567", "timestamp": "2026-06-23 14:00:00", "is_outgoing": true,
      "is_answered": true, "call_type": "phone", "duration_seconds": 2700 }
  ],
  "whatsapp_calls": [
    { "jid": "15551234567@s.whatsapp.net", "timestamp": "2026-06-23 13:00:00", "is_outgoing": false, "is_missed": true }
  ],

  // --- on-device signals ---
  "apple_notes": [ { "title": "Plan", "snippet": "…", "modified_date": "2026-06-23 22:10:00", "folder": "Work" } ],
  "recent_files": [
    { "filename": "deck.pdf", "path": "/Users/me/Downloads/deck.pdf", "last_used": null,
      "date_added": "2026-06-23 18:00:00", "file_type": "pdf", "status": "unopened", "source_url": "https://…" }
  ],
  "screen_time": {
    "top_apps": [ { "app_bundle_id": "com.tinyspeck.slackmacgap", "app_name": "slackmacgap", "minutes": 92.0 } ],
    "first_active": "2026-06-23 07:12:00", "total_minutes": 410.0
  },

  // --- browsers ---
  "chrome_history": [ { "domain": "github.com", "visit_count": 12, "top_titles": ["…"] } ],
  "search_queries": ["rust sqlite immutable"],
  "safari_history": [ { "domain": "news.ycombinator.com", "visit_count": 4, "top_titles": ["…"] } ],
  "safari_search_queries": ["rust sqlite"],

  // --- per-source liveness ---
  "source_status": { "imessage": "ok", "whatsapp": "unavailable", "screen_time": "degraded" }
}
```

`source_status` values: `ok` (clean read) | `needs_fda` (read errored — usually missing Full Disk
Access) | `unavailable` (DB/source not present on this device) | `degraded` (the reader hit its
per-source 15s timeout and the field was left empty). `recent_files` and `screen_time` are
best-effort macOS-runtime sources: on Linux / when the Spotlight CLI or knowledgeC DB is absent they
return empty rather than erroring.

### The local-source windows

`window_hours` (the caller's `since_hours`, 24 for the morning brief) governs the conversation
sources — messages, calls, browsing. The two "what did you write down" sources have their OWN
window, because a note you wrote Tuesday is still the thing on your mind on Friday:

> **Sotto sees the last 7 days and the next 3: a reminder counts if it's due in that span, or if
> you wrote it in the last 7 days.**

- **`apple_notes`** — modified within the last 7 days (a floor: a wider `since_hours` still wins).
  Capped at 30 notes, newest first.
- **`reminders`** — incomplete, and either due between 7 days ago and 3 days out, or undated but
  created in the last 7 days. Overdue first, then soonest-due, then most-recently-written undated;
  capped at 50 per store. Undated ones carry `created_date`; dated ones carry `due_date`.

The caps did **not** widen with the windows — a flood is the failure mode on the other side.
`created_date` is additive and omitted when absent, so an older Bridge build's payload still
validates; the consumer renders those as a bare `(no date)`.

The windows are named constants in the Bridge (`LOCAL_LOOKBACK_DAYS`, `NOTES_MIN_LOOKBACK_HOURS`,
`REMINDERS_LOOKAHEAD_DAYS`) — no env var. Rendering: `_shared/lib/render_local.py`
(`_format_reminders`), against the brief's injected instant, never a second clock.

## First-use and background memory

`config/onboarding.json` is owned by receiver `onboarding.py`: phase (`composing`, `queued`,
`delivered`, `existing`), lease/retry times, UTC attempt day/count and completion time. Waiting for
sources does not claim delivery. Welcome archives use the ordinary brief shape with type `welcome`.

`knowledge/history-state.json` is owned by `memory_cycle.py`: `sources[source]` carries initial
and current window bounds, cursor, completion, pages/rows/reviewed counts, last success and optional
sanitized retry/error and rejected-request revision; `next_source`, `last_run`, `receipt` govern
bounded work. Retired daily accounting fields are removed when the cycle next runs. It stores no message bodies. `knowledge/dreamer.json` is owned by `dreamer.py`:
`reviewed` maps canonical person IDs to the exact file hash reviewed, plus `last_run`.

Historical learning writes durable facts through the existing graph writer. The retired episode
store is not read; the next memory cycle deletes it without promoting summaries into facts or tasks.
`knowledge/conflicts.json` is owned by `knowledge_update.consolidate`: `people[id] = {pairs: [[fact_id, fact_id]], at}`, at most three pairs
per person and 100 people. Readers only show pairs whose facts are still active.

Usefulness rows in `outcomes.jsonl` retain the existing timestamp/action shape and add
`source: user_feedback`, `outcome: useful|not_useful`, `reference`, `excerpt`, `reason`. The
`action_id` starts `feedback:` and cannot collide with draft grading keys. `log_outcome.py` remains
the sole writer. These examples never mutate `preferences.json` or approval tiers.


### Durable work and observation ownership

| File | Single writer | Readers |
|---|---|---|
| `events/work.sqlite3` (+ SQLite WAL/SHM) | `_shared/lib/work_queue.py` | Receiver admission/worker recovery; triage input ownership; health metadata |
| `events/work-inputs/brief-*/` | `brief_runner.py` | The same procedure and its ancillary learning follow-up; retention sweep |
| `cache/brief-granola.json` | `brief_runner.py` | Brief preparation and composition; one-day retention sweep |
| `.sotto-volume.json` | adapter `managed_volume.py` | Managed boot identity verification and recovery |
| `.sotto-runtime.lock` | adapter `runtime_lock.py` | Process-lifetime writer exclusion |
| `.sotto-recovery-hold.json` | adapter `recovery.py` | Restore delivery hold and explicit release |
| `config/model-lease.json` | adapter `model_lease.py` | Model credential expiry diagnostics and renewal |
| `config/source-state.json` | `_shared/lib/source_context.py` | Source projection, contextual memory, delivery validity |
| `events/delivery-effects-<run>.json` | `_shared/lib/delivery_effects.py` (merged transaction) | Receiver result commit and outbox handoff |

`work.sqlite3` owns execution and result recovery; `events/outbox.json` owns sending. A completed
job result has the same stable ID at both stages. Provider acceptance requires structured success
and a provider message ID. It leaves replayable `effects_pending` metadata; finalization retries do
not send again. Invalidation uses a separate effect phase to retry replacement admission without
activating offers or other acceptance effects. Raw text leaves terminal rows immediately.

`config/source-state.json` stores `sources[id] = {status, observed_at, observed_epoch}` from
authenticated Bridge observations. `disabled` revokes cached-source use; availability failures do
not revoke permission. Managed capability state remains the authority for managed consent.
Google's separate source-result receipt carries `{status, observed_at, complete, coverage:{since,until}}`
with status `ok`, `partial`, `unavailable`, `disabled` or `skipped`. Legacy Gmail/Calendar data shapes
are unchanged. A missing receipt is not equivalent to a successful empty read.

Brief artifacts retain `_source_permissions`, `_source_cutoff` and `_calendar_eligibility`.
The delivery sidecar merges those facts with other effects. Calendar eligibility carries exact
provider ID/start and source observation time; absence is actionable only in a complete, fresh,
matching-day projection observed no earlier than the candidate. `coverage_until` remains the
original reviewed cutoff when an artifact is replayed. It advances the digest window only after
acceptance, preserving later incoming context.

`cache/brief-granola.json` contains `{observed_at, data}`. The data is a gathered-meetings envelope, reused for at most 24 hours with a warning beyond 30 minutes. The retention sweep applies `NOTES_CACHE_DAYS = 1`; expiry does not delete canonical meeting facts.
