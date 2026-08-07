# Exhaust schema (the Sotto data, on `$SOTTO_DATA` — Railway volume)

Byte-compatible with today's iCloud `Sotto/` layout (PORT SOURCE: knowledge_files.rs, continuity.rs,
style-profile.ts) so existing files migrate as-is. Encrypted at rest, per-tenant.

```
$SOTTO_DATA/
  knowledge/people/<slug>.md         # person — frontmatter + body (below)
  knowledge/companies/<slug>.md      # company — frontmatter + About/News/Context
  knowledge/continuity/<anchor>.md   # open loop — frontmatter only
  style.json                         # writing-style fingerprint (buckets + per_person)
  preferences.json                   # learned rules (deprioritization, edit_heavy, analytics)
  people/attention.json              # attention queue
  people/profiles.json               # profile array
  outcomes.jsonl                     # action outcomes (one JSON per line)
  briefs/<date>_<type>.json          # delivered briefs
  briefs/<date>.<type>.delivered     # per-day delivery flag (dedup for the trigger)
```

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
