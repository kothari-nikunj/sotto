---
name: sotto-triage
description: 'Use when the user says "triage my inbox" / "let''s clear what needs me" / "process my messages" / "inbox zero" / "what do I owe people" / "clear my queue" — walk the cross-channel needs-a-reply queue (email + iMessage + WhatsApp) one item at a time: draft a reply, archive/label email, or mark a thread handled so it stops re-surfacing. This is THE way to run a triage pass — never hand-improvise a list.'
metadata:
  hermes:
    tags: [chief-of-staff, sotto, triage]
    category: productivity
    requires_toolsets: [sotto-local, google-workspace]
    requires_tools: [execute_code]
required_environment_variables:
  - name: SOTTO_DATA
    prompt: Path to the Sotto exhaust volume (e.g. /data)
    required_for: continuity (marking threads handled)
---

# Sotto — Triage (clear what needs you, across channels)

A focused working session to clear the cross-channel **needs-a-reply** queue — email AND local
(iMessage/WhatsApp). Unlike the brief (informational), triage is **action**: for each item, draft/send,
archive/label (email only), or mark it handled so tomorrow's brief doesn't renag.

## Procedure

> **Script paths:** absolute, e.g. `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/triage_queue.py"`.

1. **Gather** (deterministic — don't hand-fetch):
   - `read_local(since_hours=72)` → `/tmp/sotto_local.json` (or the staged payload).
   - `execute_code` → `gather_google.py --skip-calendar` → `/tmp/sotto_gmail.json` (recent + unread email). **Host-agnostic fallback:** if it reports the CLI isn't this host's Google path (`google_api.py not found … FALLBACK …`), fetch `newer_than:1d` with the host's Gmail **MCP** tool, dump raw to `/tmp/sotto_gmail_raw.json`, then `gather_google.py --skip-calendar --from-mcp-gmail /tmp/sotto_gmail_raw.json`.
2. **Build the queue** — `execute_code` → `triage_queue.py` → JSON `{email[], imessage[], whatsapp[], counts}`. Each item is name-resolved with a `last_snippet`. It logs counts to `/debug/brief-log`. Walk it in priority order: **email (important/unread first) → iMessage → WhatsApp**.
3. **Per item, propose ONE action** (fast pass — no narrative):
   - **Reply** → hand to `sotto-draft-reply` (drafts in the user's voice + a one-tap send link, or true send per channel). Email/Calendar can send cloud-side; iMessage/SMS need the Bridge live (else deep link); WhatsApp is deep-link only.
   - **Email housekeeping** (no reply needed) → archive or label via the google-workspace tools.
   - **Skip / snooze** → leave it; it stays in the queue.
   - Honor `sotto-approval-tiers` before sending or archiving anything.
4. **Record handled** — collect `{identifier, channel}` for every thread the user replied to or dismissed, plus replied email `threadId`s, then `execute_code` → `continuity_resolve.py` with `signals.handled` + `signals.replied_thread_ids` so those loops CLOSE and don't re-surface in the next brief.
5. **Wrap** — one short line: "Cleared N (X email, Y texts). M left — say *continue triage* to keep going."

## Notes
- **Local has no archive.** You can't archive/label a text — for iMessage/WhatsApp the only triage actions are **reply** or **mark-handled** (continuity). Archive/label is **email only**.
- Keep it fast and skimmable — triage is a clearing pass, not a brief. No prose, no calendar.
- Local data comes from the Bridge; if it's unreachable, triage **email only** and say so in one line.
- Deliver as **Sotto**, never "Hermes Agent".
