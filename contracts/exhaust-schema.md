# Exhaust schema (the Sotto data, on `$SOTTO_DATA` — Railway volume)

Byte-compatible with today's iCloud `Sotto/` layout (PORT SOURCE: knowledge_files.rs, continuity.rs,
style-profile.ts) so existing files migrate as-is. Encrypted at rest, per-tenant.

Every row names its **owning writer** — the one script allowed to write that shape. Readers are
many; writers are one (`preferences.json` is the single documented exception, and it carries its own
rule). Paths are relative to `$SOTTO_DATA/`; scripts are relative to `sotto-chief-of-staff/`.

| Path | What it is | Written by |
|---|---|---|
| `knowledge/people/<slug>.md` | person — frontmatter + body (below) | `_shared/knowledge/knowledge_update.py` (`_shared/knowledge/knowledge_edit.py` routes user edits through its `apply()`; `_shared/knowledge/knowledge.py` is the model + serializer both use) |
| `knowledge/companies/<slug>.md` | company — frontmatter + About/News/Context | `_shared/knowledge/knowledge_update.py` |
| `knowledge/continuity/<anchor>.md` | open loop — frontmatter only | `morning-brief/scripts/continuity_resolve.py` (`_shared/scripts/retune_apply.py` and `_shared/knowledge/knowledge_edit.py --op loop*` mutate through its loader/persister; `_shared/scripts/ledger_io.py` is the shared READ side) |
| `knowledge/relationship_state.json` | attention queue + insights + per-contact history | `relationship-pulse/scripts/relationship_pulse.py` |
| `style.json` | writing-style fingerprint (buckets + per_person) | `_shared/scripts/style_extract.py` |
| `preferences.json` | learned rules (deprioritization, edit_heavy, analytics) **and** the user's stated `explicit` block | **two writers, deliberately:** `approval-tiers/scripts/learn_preferences.py` (learned lists) and `_shared/scripts/preferences.py` (`explicit`). See ARCHITECTURE.md — a rule you delete stays deleted. |
| `outcomes.jsonl` | action outcomes (one JSON per line) | `_shared/scripts/log_outcome.py` |
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
`[{type, slug, name, sentence}]`.

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
  "contacts": [ { "name": "Sarah Chen", "phones": ["+15551234567"], "emails": ["sarah@acme.com"], "notes": "met at conf" } ],
  "reminders": [ { "title": "Call dentist", "due_date": "2026-06-24 15:00:00" } ],

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
