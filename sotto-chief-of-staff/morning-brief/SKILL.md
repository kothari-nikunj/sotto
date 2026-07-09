---
name: sotto-morning-brief
description: Use when the user says "good morning", asks for their morning brief / daily brief / "what's on today" / "what needs my attention", when it's morning-brief time, or when the Bridge pushes a morning_ready trigger — produce the user's morning briefing. This is THE way to produce any morning brief — never hand-write a calendar/email summary instead of running this skill.
metadata:
  hermes:
    tags: [brief, chief-of-staff, sotto]
    category: productivity
    requires_toolsets: [sotto-local, google-workspace, granola]
    requires_tools: [execute_code]
required_environment_variables:
  - name: GOOGLE_AI_API_KEY
    prompt: Gemini API key (for the brief extraction)
    help: https://aistudio.google.com/apikey
    required_for: brief extraction
  - name: SOTTO_DATA
    prompt: Path to the Sotto exhaust volume (e.g. /data)
    required_for: knowledge graph + continuity
---

# Sotto — Morning Brief

Produce the user's morning brief: what needs attention, what you've already handled, who's in their world today, and the actions they can take.

> **CRITICAL — do not improvise the brief.** The brief's quality IS the product. You MUST gather the
> inputs and then run the FLEX extraction prompt in `references/extraction-prompt.md` over them (step 3),
> and deliver **its** output verbatim. Do NOT write your own "Your Day at a Glance" calendar/email
> summary — a brief that is *mostly* a calendar/email recap is a failure. The real brief leads with
> communications (Needs Attention Now / Should Handle Today / Already Handled / FYI), weaves each
> person's signals across channels, carries tap-to-act items, and includes one **short Coming Up**
> schedule section (≤5 lines). Deliver as **Sotto**, in Sotto's voice — never as "Hermes Agent".

## Inputs
- **Local context** — either the `local_data` payload from the `morning_ready` trigger, OR call the Bridge tool `read_local(since_hours=24)` if not provided (cold start: `since_hours=168`).
- **Google** — Gmail (last 24h, prioritized) + Calendar (next 3 days) via the native Google Workspace tools.
- **Granola** — recent meeting notes via the Granola MCP.
- **Prior knowledge** — load with `knowledge_query.py` (people/companies relevant to today).
- **Attendee research** — public background on the external people in today's meetings (see step 2).

## Procedure

> **Script paths:** every script this skill runs lives under `$HOME/.hermes/skills/sotto/`. Use the
> **absolute** path when invoking them (relative paths won't resolve from your working dir), e.g.
> `python3 "$HOME/.hermes/skills/sotto/morning-brief/scripts/select_attendees.py"` and
> `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/compose_brief.py"`.

1. **Gather ALL inputs — Google AND local. The brief's value is *marrying* the two; a local-only brief is a failure when Google is connected.**
   - **Gmail (24h) + Calendar (3d)** — REQUIRED, and **deterministic — run ONE command, don't hand-fetch:**
     `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py"`
     It calls the host's google-workspace `google_api.py` CLI and writes `/tmp/sotto_gmail.json` + `/tmp/sotto_cal.json` in the shape `compose_brief` expects. It prints `[gather_google] N emails, M events …`. Doing the gather by hand was the #1 cause of 0-email briefs; use the script.
     - **Host-agnostic fallback (the CLI isn't the only way to reach Google).** If the output says the CLI **isn't this host's Google path** (`google_api.py not found … FALLBACK …`), Google may still be connected here as a **Gmail/Calendar MCP** (common on OpenClaw and some Hermes setups). Then: call the host's Gmail tool for `newer_than:1d` (≤25) and the Calendar tool for the next 3 days, **dump the raw tool results** to `/tmp/sotto_gmail_raw.json` + `/tmp/sotto_cal_raw.json`, and normalize them deterministically (don't hand-map fields):
       `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py" --from-mcp-gmail /tmp/sotto_gmail_raw.json --from-mcp-calendar /tmp/sotto_cal_raw.json`
     - **Only if NEITHER the CLI nor a Google MCP exists** is Google genuinely unavailable — then proceed local-only and say so honestly (don't fabricate email/calendar). A non-zero `N`/`M` from either path means Google is wired; carry on.
   - **Local** — the staged `local_data`, or `read_local(since_hours=24)` → `/tmp/sotto_local.json`. If the Bridge is unreachable, still run — the script falls back to the **last cached snapshot** (flagged stale), so you degrade to yesterday's messages rather than dropping local entirely.
   - **Granola** → `/tmp/sotto_granola.json`. Via the Granola MCP: list recent meetings (last ~14 days) with their `ai_summary` / `your_notes`, as `[{title, date, attendee_emails, ai_summary}]`. This powers "when you met last week you discussed X" next to that person's entry — gather it (don't skip). Granola has **no CLI** (it's an MCP), so unlike Gmail this can't be a one-command gather; if the Granola MCP isn't configured, skip it.
   - **knowledge / attendee research** as in step 2 + Inputs.
2. **Research attendees — ONE batched, grounded command** (ported from the Mac's `gemini-research.ts`). `execute_code` → `scripts/select_attendees.py` with `{google, local}` to get the external attendees worth researching (excludes you, same-domain colleagues, known contacts; capped at 25). Write that list to `/tmp/sotto_research_in.json`, then:
   `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/research_attendees.py" --attendees /tmp/sotto_research_in.json --context /tmp/sotto_cal.json` → `/tmp/sotto_research.json`.
   It **batches 5 attendees per Gemini Search-Grounding call** (25 people → ~5 calls, run concurrently — far cheaper than one agent/search per person), dedupes, caps at 25, and returns `{attendees:[{email,title,company,relevance,summary}]}` grounded in real web results. Uses the Google key you already have — no extra key, no sub-agents needed. If the list is empty or it returns none, pass `attendee_research: []`. Research only — never draft/schedule/send here. (The rules it applies are `references/research-prompt.md`; for an ad-hoc single lookup use `_shared/scripts/web_research.py`.)
3. **Extract — this step IS the brief. Run ONE command; do not write the brief yourself.**
   The brief MUST come from the extraction script — running the 557-line prompt by hand drifts into a generic agenda recap (calendar listing, no markers, Google-only). The script is easy: **save each source you gathered to its own file, then run one command.** No hand-assembled JSON.
   1. Write each gathered source to a temp file (skip any you don't have — only `--local` is required):
      - `read_local` result → `/tmp/sotto_local.json`  ← **REQUIRED** (your iMessage/WhatsApp/calls/notes/etc.; omitting it = a Google-only brief, the exact failure). **Write the tool result AS-IS** — `compose_brief` unwraps the MCP wrapper itself, so do NOT reshape it with an inline `python3 -c` (that trips the dangerous-command gate and silently kills headless/cron runs).
      - Gmail (last 24h) → `/tmp/sotto_gmail.json`  ·  Calendar (next 3d) → `/tmp/sotto_cal.json`
      - Granola → `/tmp/sotto_granola.json`  ·  `knowledge_query.py` output → `/tmp/sotto_know.json`  ·  attendee research (step 2) → `/tmp/sotto_research.json`
   2. Run the script via `execute_code` (use the **absolute path** — relative paths won't resolve):
      ```bash
      python3 "$HOME/.hermes/skills/sotto/_shared/scripts/compose_brief.py" --type morning \
        --local /tmp/sotto_local.json --gmail /tmp/sotto_gmail.json --calendar /tmp/sotto_cal.json \
        --granola /tmp/sotto_granola.json --knowledge /tmp/sotto_know.json --attendee-research /tmp/sotto_research.json
      ```
      (Needs `execute_code` approved once — `/approve always`; cron runs need it pre-approved.) It prints JSON: `brief_markdown`, `actions[]`, `meetings_needing_prep[]`, `extracted_knowledge`, `_critic`.
      The script runs a **second-pass critic** (a port of the Mac's brief-critic) gated by `SOTTO_CRITIC=auto|always|off` (default `auto` — a quiet-day brief below the size/action thresholds in `compose_brief.py` skips the critic+revise pass): it checks the draft against a data manifest for missed threads, attribution errors, mis-prioritization, wrong "Already Handled", action coverage, and weak cross-channel synthesis, then **revises** the brief to fix any critical/moderate issues. `brief_markdown` is always the final result; `_critic` is `{score, summary, patches, actionable}` when the critic ran, or `{skipped, reason}` when it was skipped. (Pass `--no-critic` to force-skip for a fast draft.)
   3. Deliver `brief_markdown` **verbatim** — do not rewrite or re-summarize. It already has the communication sections, the short Coming Up schedule, the cross-channel synthesis, Sotto's voice, and the markers.
   - **HARD GATE — what you deliver MUST be the `brief_markdown` from `compose_brief.py` (step 3.2).** If you did not run `compose_brief.py` this run, you have NOT produced a valid brief: do **not** hand-write one. These are tells that you improvised (all are failures, stop and run the script): the WHOLE brief reading as a "Your Day"/calendar agenda (a *short* Coming Up schedule is fine — a full meeting-by-meeting agenda is not); deep links like `sms:arnav_sahu` or `sms:group_…` (the script emits `sms:+<digits>`, `https://wa.me/…`, `mailto:`, `tel:` — and **never** a link for a group chat); WhatsApp contacts shown with `sms:` links; the user's own name appearing as a contact to reply to; or names/attributions you're inferring rather than reading from the data.
   - **If you genuinely cannot run `compose_brief.py`** (e.g. `execute_code` is unavailable or unapproved): do NOT fabricate a brief. Deliver a one-line honest status instead — "I couldn't run the brief composer (execute_code unavailable); ask me to retry once it's approved." A hand-written brief is worse than no brief.
   - *Native fallback (ONLY if `execute_code` is truly unavailable AND you can `read_file`):* run `references/extraction-prompt.md` with your model over the same inputs — this still runs the real extraction prompt (with its guardrails), unlike free-handing. The script is strongly preferred.
4. **Learn — run BOTH scripts so the knowledge graph + ledger actually accumulate (this is Sotto's memory; skipping it means every brief starts cold).** Use absolute paths.
   1. **Knowledge** — write the extraction's `extracted_knowledge` (the `{person_updates:[…], company_updates:[…]}` from step 3) to `/tmp/sotto_know.json`, then:
      `execute_code` → `python3 "$HOME/.hermes/skills/sotto/morning-brief/scripts/knowledge_update.py" /tmp/sotto_know.json`
      (writes/updates `/data/knowledge/people/*.md` + `/data/knowledge/companies/*.md`).
   2. **Continuity** — write `{ "today": "<YYYY-MM-DD>", "signals": { "replied_thread_ids": [<gmail/thread ids you replied to today>] }, "new_actions": <the brief's `actions[]` verbatim>, "local": <the read_local JSON from /tmp/sotto_local.json>, "events": <calendar events from /tmp/sotto_cal.json> }` to `/tmp/sotto_cont.json`, then:
      `execute_code` → `python3 "$HOME/.hermes/skills/sotto/morning-brief/scripts/continuity_resolve.py" /tmp/sotto_cont.json`
      (pass the brief's `actions[]` **as-is** — the script reads the FLEX camelCase fields directly. **Include `local` + `events`** so the script can do **cross-channel reply detection** — it resolves an open loop when you answered the person on ANY channel (outgoing iMessage/WhatsApp/call, or a calendar event now on the books), not just the original thread. It also resolves passed meetings and ages out / deadline-expires stale loops, then updates `/data/knowledge/continuity/*.md`.) Optional `signals.handled: [{identifier, channel}]` additionally closes loops for anyone you marked Already-Handled.
   3. **Preferences (behavioral learner)** — `execute_code` →
      `python3 "$HOME/.hermes/skills/sotto/approval-tiers/scripts/learn_preferences.py"`
      (tallies `$SOTTO_DATA/outcomes.jsonl` → `preferences.json`: deprioritization hints + the per-(contact, action_type) `approval_defaults` that `sotto-approval-tiers` honors. Fast and idempotent; a missing/empty outcomes log is a no-op, and the user's `explicit` preferences block is never touched.)
5. **Voice** (optional, **Hermes-native TTS** — no Inworld/Parallel key). If the user wants to *listen* (they asked for audio / "read me my brief", or `SOTTO_VOICE_BRIEF=1`): generate a tight spoken version via `_shared/references/audio-script-prompt.md` (shorter + conversational — NOT the full markdown, and strip the `<!--…-->` markers and tap-link URLs, which don't read aloud) and let Hermes voice it. Voice output uses the configured `tts.provider` (default `edge`, free); delivery shape is per-platform (WhatsApp gets an audio file). Deliver the **text** brief regardless — voice is in addition, so the user can both read and listen. Hermes also transcribes voice notes the user sends, so they can reply to Sotto by voice.
6. **Deliver** — **claim the deliver-once gate FIRST, then send** as **Sotto** (never "Hermes Agent").
   - **Deliver-once (cron ↔ wake-push coordination):** right before sending, run `execute_code` →
     `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/brief_marker.py" --claim morning` (evening brief:
     `--claim evening`). If it prints **`already`**, STOP — today's brief was already delivered by the
     other path (the cloud cron and the Mac wake-push both run this skill; the gate ensures exactly one
     delivers). Only when it prints `claimed` do you send. (This is why enabling `SOTTO_WAKE_PUSH` no
     longer double-delivers.)
   - Before sending on a **text channel** (WhatsApp/SMS/Telegram), **strip the `<!--id:…|ch:…-->` and `<!--meeting:…-->` markers** from `brief_markdown` (e.g. `sed -E 's/<!--[^>]*-->//g'`) — they were the Mac app's tap-to-expand UI plumbing and show up as literal `<!--…-->` clutter on a chat channel. Keep the bold names.
   - **Tap-to-act DOES work on chat.** Each action in `actions[]` carries a **`tap_link`** — a real tappable URL (`https://wa.me/…`, `mailto:…`, `tel:…`, `sms:…`, or the meeting link). WhatsApp/Telegram/iMessage render these as one-tap links. For the top 2–3 actions, attach the link to the person's name or add a short tappable line — e.g. "→ [Message Dhruv](https://wa.me/15551234567)" or "[Reply to the LOI](mailto:dhruv@acme.com?subject=Re:%20LOI)". Use the `tap_link` **verbatim**; don't invent URLs.
   - Pair the links with a one-line **conversational offer** for anything that needs drafting — "say *draft Dhruv* and I'll write the LOI reply, or *prep Berkeley* for the pitch." Mark the day delivered.
   - If the brief came out **thin or the user pushes back on it**, append ONE recovery sentence so it carries its own next move: "If anything here looks off: say *that's wrong about X* and I'll fix my memory, *stop surfacing newsletters* to mute a source, or *clean up stale loops* to retune."

## Notes
- First brief (empty graph): widen the window to 7 days and extract aggressively (seed mode). Day 2+: 24h deltas.
- Keep the brief tight and skimmable; lead with what genuinely needs the user.
- **The brief is communications-first, not an agenda.** It includes ONE short **Coming Up** section (≤5 lines: time + title + key attendees) — that's the schedule, and it's good. But if the *whole* brief is a meeting-by-meeting agenda, you're improvising, not running the script (step 3). Stop and run the script.
