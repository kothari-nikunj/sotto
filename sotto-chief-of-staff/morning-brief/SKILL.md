---
name: sotto-morning-brief
description: Use when the user says "good morning", asks for their morning brief / daily brief / "what's on today" / "what needs my attention", when it's morning-brief time, or when the Bridge pushes a morning_ready trigger — produce the user's morning briefing. This is THE way to produce any morning brief — never hand-write a calendar/email summary instead of running this skill.
metadata:
  hermes:
    tags: [brief, chief-of-staff, sotto]
    category: productivity
    requires_toolsets: [sotto-local, google-workspace]
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

> **CRITICAL — do not improvise the brief.** The brief's quality IS the product. Gather the inputs,
> run `compose_brief.py` over them (step 3), and deliver **its** output verbatim. Do NOT write your
> own "Your Day at a Glance" calendar/email summary — a brief that is *mostly* a calendar/email recap
> is a failure. The real brief leads with communications (Needs Attention Now / Should Handle Today /
> Already Handled / Filtered), weaves each person's signals across channels, carries tap-to-act
> items, and includes one **short Coming Up** schedule section (≤5 lines: time + title + key
> attendees). Deliver as **Sotto**, in Sotto's voice — never as "Hermes Agent".

## Procedure

> **Script paths:** every script this skill runs lives under `$HOME/.hermes/skills/sotto/`. Use the
> **absolute** path when invoking them (relative paths won't resolve from your working dir), e.g.
> `python3 "$HOME/.hermes/skills/sotto/morning-brief/scripts/select_attendees.py"` and
> `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/compose_brief.py"`.

1. **Gather ALL inputs — Google AND local. The brief's value is *marrying* the two; a local-only brief is a failure when Google is connected.**
   - **Gmail (24h) + Calendar (3d)** — REQUIRED, and **deterministic — run ONE command, don't hand-fetch:**
     `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py"`
     It calls the host's google-workspace `google_api.py` CLI and writes `/tmp/sotto_gmail.json` + `/tmp/sotto_cal.json` in the shape `compose_brief` expects. It prints `[gather_google] N emails (K sent), M events …`. Doing the gather by hand was the #1 cause of 0-email briefs; use the script. (The gather runs a **small `in:sent newer_than:1d` lane** — default 15, `--skip-sent`/`--sent-max` to tune — merged into the same file and flagged `isSent`. That's what step 4.4's `style_extract.py --gmail` learns the user's email voice from; don't skip it.)
     - **Host-agnostic fallback (the CLI isn't the only way to reach Google).** If the output says the CLI **isn't this host's Google path** (`google_api.py not found … FALLBACK …`), Google may still be connected here as a **Gmail/Calendar MCP** (common on OpenClaw and some Hermes setups). Then: call the host's Gmail tool for `newer_than:1d` (≤25), again for `in:sent newer_than:1d` (≤15), and the Calendar tool for the next 3 days, **dump the raw tool results** to `/tmp/sotto_gmail_raw.json` + `/tmp/sotto_sent_raw.json` + `/tmp/sotto_cal_raw.json`, and normalize them deterministically (don't hand-map fields):
       `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py" --from-mcp-gmail /tmp/sotto_gmail_raw.json --from-mcp-sent /tmp/sotto_sent_raw.json --from-mcp-calendar /tmp/sotto_cal_raw.json`
       (`--from-mcp-sent` is optional — omit it if the host has no sent search; you just lose the email-voice lane.)
     - **Only if NEITHER the CLI nor a Google MCP exists** is Google genuinely unavailable — then proceed local-only and say so honestly (don't fabricate email/calendar). A non-zero `N`/`M` from either path means Google is wired; carry on.
   - **Local** — the staged `local_data`, or `read_local(since_hours=24)` → `/tmp/sotto_local.json`. If the Bridge is unreachable, still run — the script falls back to the **last cached snapshot** (flagged stale), so you degrade to yesterday's messages rather than dropping local entirely.
   - **Granola** — deterministic, same one-command pattern as Gmail (don't hand-fetch via MCP tools):
     `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_granola.py"`
     It reads the Granola connector token (`$SOTTO_DATA/connectors/granola.json`, written when the user connects Granola on `/setup` — falls back to `GRANOLA_API_KEY` REST if set), lists the last ~14 days of meetings with `ai_summary`/`your_notes` (+ transcripts for meetings that ended in the last ~36h), and writes `/tmp/sotto_granola.json` as `{"meetings":[…]}` — exactly the file step 3 passes as `--granola`. It prints `[gather_granola] N meetings (T with transcripts, K with notes) …`. This powers "when you met last week you discussed X" next to that person's entry. If it warns Granola isn't connected, it writes an empty file and the brief proceeds without meeting notes — don't try to fetch Granola any other way.
   - **Prior knowledge** — `python3 "$HOME/.hermes/skills/sotto/_shared/knowledge/knowledge_query.py" --calendar /tmp/sotto_cal.json` → `/tmp/sotto_know.json`, run AFTER the calendar gather: recently-updated people, plus everyone on today's calendar regardless of staleness, plus the contact_index identity map. (Company knowledge is written by the Learn step, not loaded here.)
   - **Attendee research** — step 2.
2. **Research attendees — ONE batched, grounded command** (ported from the Mac's `gemini-research.ts`). `execute_code` → pipe the gathered `{google, local}` inputs into `python3 "$HOME/.hermes/skills/sotto/morning-brief/scripts/select_attendees.py"` (a file path as argv[1], or stdin) to get the external attendees worth researching (excludes you, same-domain colleagues, known contacts; capped at 25). Write that list to `/tmp/sotto_research_in.json`, then **skip anyone the graph already knows fresh** (researched < 30 days ago — the same filter `sotto-meeting-prep` runs, so the two skills share one cache instead of each paying daily):
   `python3 "$HOME/.hermes/skills/sotto/meeting-prep/scripts/persist_prep.py" --filter-fresh /tmp/sotto_research_in.json`
   (rewrites the file in place, dropping only people whose graph profile carries a `last_researched` stamp < 30 days old. Prints `{kept, skipped_fresh}` — skipped people still reach the brief through the knowledge graph.) Then research whoever's left:
   `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/research_attendees.py" --attendees /tmp/sotto_research_in.json --context /tmp/sotto_cal.json --comms /tmp/sotto_gmail.json --out /tmp/sotto_research.json` (the script writes `--out` itself — no shell redirect; `--comms` feeds the already-gathered emails in as per-attendee relationship context — disambiguation + sharper relevance; the script tolerates a missing file.)
   **Run it even when `kept` is 0** — with an empty list it writes a fresh `{"attendees":[]}`, which is what stops this brief from reusing an EARLIER run's research file as stale bios.
   It **batches 5 attendees per grounded research call** (25 people → ~5 calls, run concurrently — far cheaper than one agent/search per person), dedupes, caps at 25, and returns `{attendees:[{email,title,company,relevance,summary}]}` grounded in real web results. The provider is whichever research key is set — Parallel, then Exa, then Gemini grounding on the Google key you already have — so no extra key is needed and no sub-agents are involved. If the list is empty or it returns none, pass `attendee_research: []`. Research only — never draft/schedule/send here. (The research rules are inlined in the script; `references/research-prompt.md` documents the original ported prompt. For an ad-hoc single lookup use `_shared/scripts/web_research.py`.)
   Then **persist what you just researched, so the next run (brief or prep) doesn't re-pay for it:**
   ```bash
   python3 "$HOME/.hermes/skills/sotto/meeting-prep/scripts/persist_prep.py" \
     --research /tmp/sotto_research.json --attendees /tmp/sotto_research_in.json
   ```
   (writes each researched attendee's title/company/summary into the graph as clearly-sourced, LOW-confidence "Per web search: …" facts plus the `last_researched` stamp the filter above reads — grounded in the research output only, never invented. Idempotent; a no-op when research was empty.) **One rule, now global: an external attendee is researched at most once per 30 days, and every research run feeds the graph.**
3. **Extract — this step IS the brief. Run ONE command; do not write the brief yourself.**
   **First, close what's already closed** (one command, before composing — so the brief reasons about
   today's open loops, not last night's). Write `{ "today": "<YYYY-MM-DD>", "signals": { "replied_thread_ids": [<ids you replied to today>] }, "local": <the read_local JSON from /tmp/sotto_local.json>, "events": <calendar events from /tmp/sotto_cal.json> }` to `/tmp/sotto_cont.json`, then:
   `execute_code` → `python3 "$HOME/.hermes/skills/sotto/morning-brief/scripts/continuity_resolve.py" --resolve-only /tmp/sotto_cont.json`
   (`--resolve-only` closes what today's data shows was delivered or answered and drops what has
   aged out — it does NOT record anything new, so it is safe to run before the brief exists. The
   Learn step below adds the brief's own items with `--merge-only`. Without this split, a loop that
   today's messages already closed is still presented to the model as open.)
   The brief MUST come from the extraction script — running the full extraction prompt by hand drifts into a generic agenda recap (calendar listing, no markers, Google-only). The script is easy: **save each source you gathered to its own file, then run one command.** No hand-assembled JSON.
   1. Write each gathered source to a temp file (skip any you don't have — only `--local` is required):
      - `read_local` result → `/tmp/sotto_local.json`  ← **REQUIRED** (your iMessage/WhatsApp/calls/notes/etc.; omitting it = a Google-only brief, the exact failure). **Write the tool result AS-IS** — `compose_brief` unwraps the MCP wrapper itself, so do NOT reshape it with an inline `python3 -c` (that trips the dangerous-command gate and silently kills headless/cron runs).
      - Gmail (last 24h) → `/tmp/sotto_gmail.json`  ·  Calendar (next 3d) → `/tmp/sotto_cal.json`
      - Granola → `/tmp/sotto_granola.json` (already written by `gather_granola.py` in step 1 — pass it as-is)  ·  `knowledge_query.py --calendar /tmp/sotto_cal.json` output → `/tmp/sotto_know.json`  ·  attendee research (step 2) → `/tmp/sotto_research.json`
   2. Run the script via `execute_code` (use the **absolute path** — relative paths won't resolve):
      ```bash
      python3 "$HOME/.hermes/skills/sotto/_shared/scripts/compose_brief.py" --type morning \
        --local /tmp/sotto_local.json --gmail /tmp/sotto_gmail.json --calendar /tmp/sotto_cal.json \
        --granola /tmp/sotto_granola.json --knowledge /tmp/sotto_know.json --attendee-research /tmp/sotto_research.json
      ```
      (Interactive runs need `execute_code` approved once — `/approve always`. **Cron/scheduled runs
      hard-block `execute_code`** (no user present; upstream hermes-agent#38585) — there, run the
      IDENTICAL command through the **`terminal` tool** instead: same script, same file args. The
      boot config raises `terminal.timeout` to 600s so the 2–4 min composition isn't killed mid-run.)
      It prints JSON: `brief_markdown`, `brief_text` (the chat-ready version — markers stripped,
      WhatsApp-safe bold), `actions[]`, `meetings_needing_prep[]`, `extracted_knowledge`, `_critic`.
      The script runs a **second-pass critic** (a port of the Mac's brief-critic) gated by `SOTTO_CRITIC=auto|always|off` (default `auto` — a quiet-day brief below the size/action thresholds in `compose_brief.py` skips the critic+revise pass): it checks the draft against a data manifest for missed threads, attribution errors, mis-prioritization, wrong "Already Handled", action coverage, and weak cross-channel synthesis, then **revises** the brief to fix any critical/moderate issues. `brief_markdown` is always the final result; `_critic` is `{score, summary, patches, actionable}` when the critic ran, or `{skipped, reason}` when it was skipped. (Pass `--no-critic` to force-skip for a fast draft.)
   3. Deliver **`brief_text` verbatim** on chat channels (WhatsApp/Telegram/SMS) — it's the chat-ready rendering: markers stripped, `*bold*` WhatsApp syntax. Do not rewrite, re-summarize, or re-format it. `brief_markdown` is the same brief WITH the `<!--…-->` markers — records and continuity only, never a chat channel (the markers show as literal clutter).
   - **HARD GATE — if you did not run `compose_brief.py` this run, you have not produced a valid brief.** Don't hand-write one. Tells that you improvised, all failures, stop and run the script: the WHOLE brief reading as a "Your Day"/calendar agenda (a *short* Coming Up is fine, a meeting-by-meeting agenda is not); deep links like `sms:arnav_sahu` or `sms:group_…` (the script emits `sms:+<digits>`, `https://wa.me/…`, `mailto:`, `tel:` — and **never** a link for a group chat); WhatsApp contacts shown with `sms:` links; the user's own name listed as someone to reply to; names or attributions you inferred rather than read from the data.
   - **If you genuinely cannot run `compose_brief.py` by EITHER transport** (`execute_code` blocked AND the `terminal` fallback also failed): do NOT fabricate a brief. Deliver a one-line honest status instead — "I couldn't run the brief composer (code execution unavailable); ask me to retry once it's approved." A hand-written brief is worse than no brief.
   - *Native fallback (ONLY if `execute_code` is truly unavailable AND you can `read_file`):* run `references/extraction-prompt.md` with your model over the same inputs — this still runs the real extraction prompt (with its guardrails), unlike free-handing. The script is strongly preferred. **Key mapping:** the raw prompt emits the FLEX camelCase keys — treat `markdown` as `brief_markdown`, `actionItems` as `actions`, and `extractedKnowledge` (with `person_updates`/`company_updates`) as `extracted_knowledge` in every later step (the script's normalizer does this rename for you; on the fallback you must do it yourself). There is no critic pass and no `brief_text` on this path — strip any `<!--…-->` markers before sending to a chat channel.
4. **Learn — run ALL FOUR scripts below (knowledge, continuity, preferences, style) so Sotto's memory actually accumulates; skipping any of them means the next brief starts that much colder.** Use absolute paths.
   1. **Knowledge** — write the extraction's `extracted_knowledge` (the `{person_updates:[…], company_updates:[…]}` from step 3) to `/tmp/sotto_know_out.json` — a **distinct** file; do NOT reuse `/tmp/sotto_know.json`, which holds the `knowledge_query.py` output and is re-read as prior knowledge by later runs/skills — then:
      `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/knowledge/knowledge_update.py" /tmp/sotto_know_out.json`
      (the script takes the payload as a file argument or on stdin; writes/updates `$SOTTO_DATA/knowledge/people/*.md` + `$SOTTO_DATA/knowledge/companies/*.md`).
      **The same run does the memory's own housekeeping — deterministically, no judgment from you:** person files that share an exact identifier (the same email or phone) are one human and are merged automatically; person files that merely have *similar names* are never merged, only written to `$SOTTO_DATA/knowledge/merge_suggestions.json` for a human to confirm; and a company that matches an existing file's alias or domain resolves INTO that file instead of forking a second one. If the brief mentions a possible duplicate and the user says "yes, merge them", that confirmation is one command — `python3 "$HOME/.hermes/skills/sotto/_shared/knowledge/knowledge_edit.py" --op merge --from <slug> --into <slug>` (it refuses when the two files carry conflicting identifiers). **Never merge person files by hand**, and never merge two people because their names look alike.
   2. **Continuity** — write `{ "today": "<YYYY-MM-DD>", "signals": { "replied_thread_ids": [<gmail/thread ids you replied to today>] }, "new_actions": <the brief's `actions[]` verbatim>, "local": <the read_local JSON from /tmp/sotto_local.json>, "events": <calendar events from /tmp/sotto_cal.json> }` to `/tmp/sotto_cont.json`, then:
      `execute_code` → `python3 "$HOME/.hermes/skills/sotto/morning-brief/scripts/continuity_resolve.py" --merge-only /tmp/sotto_cont.json`
      (pass the brief's `actions[]` **as-is** — the script reads the FLEX camelCase fields directly. `--merge-only` records the brief's new items; step 3 already ran `--resolve-only` over the same payload, which is what closed anything today's data settled — **include `local` + `events` there** so it can do **cross-channel reply detection**: an open loop closes when you answered the person on ANY channel (outgoing iMessage/WhatsApp/call, or a calendar event now on the books), not just the original thread. Optional `signals.handled: [{identifier, channel}]` on the resolve pass additionally closes loops for anyone you marked Already-Handled. Running the script with NO flag does both halves at once — that is the right form for an on-demand catch-up run, not for a brief. **Not every action becomes a loop:** the ledger holds DEBTS, so the merge silently refuses an action with no summary, one that names nobody, one whose counterpart is a no-reply/notifications/billing address, and any calendar shadow — each refusal is printed as `[continuity_resolve] not a debt` with its reason. That is expected, not an error.)
   3. **Preferences (behavioral learner)** — `execute_code` →
      `python3 "$HOME/.hermes/skills/sotto/approval-tiers/scripts/learn_preferences.py"`
      (tallies `$SOTTO_DATA/outcomes.jsonl` → `preferences.json`: deprioritization hints + the per-(contact, action_type) `approval_defaults` that `_shared/references/approval-tiers.md` governs. Fast and idempotent; a missing/empty outcomes log is a no-op, and the user's `explicit` preferences block is never touched.)
   4. **Writing voice (style accumulation)** — `execute_code` →
      `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/style_extract.py" /tmp/sotto_local.json --gmail /tmp/sotto_gmail.json`
      (accumulates the user's style fingerprint from today's sent messages into `$SOTTO_DATA/style.json` —
      caps/TTL make repeat runs safe. `--gmail` feeds the SENT emails from the gather into the
      work_email bucket so drafts learn the user's email voice, not just their texting voice; the
      script tolerates a missing file. This is also the safety net for setups that skipped the voice seed:
      without it, style.json never exists and drafts can't sound like the user.)
5. **Voice** (optional, **Hermes-native TTS** — no Inworld/Parallel key). Generate the spoken version when EITHER the user just asked for audio ("read me my brief") OR their standing preference says so — check `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/preferences.py" show` for `brief_audio` being `both` or matching this brief's type (`morning`/`evening`); the sotto-feedback skill sets it from "send my briefs as voice notes". When it applies: generate a tight spoken version via `_shared/references/audio-script-prompt.md` (shorter + conversational — start from `brief_text`, which already has the `<!--…-->` markers stripped; also drop tap-link URLs, which don't read aloud) and let Hermes voice it. Voice output uses the configured `tts.provider` (default `edge`, free); delivery shape is per-platform (WhatsApp gets an audio file). Deliver the **text** brief regardless — voice is in addition, so the user can both read and listen. Hermes also transcribes voice notes the user sends, so they can reply to Sotto by voice.
6. **Deliver** — **claim the deliver-once gate FIRST, then send** as **Sotto** (never "Hermes Agent").
   - **Deliver-once (cron ↔ wake-push coordination):** right before sending, run `execute_code` →
     `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/brief_marker.py" --claim morning` (evening brief:
     `--claim evening`). If it prints **`already`**, STOP — today's brief was already delivered by the
     other path (the cloud cron and the Mac wake-push both run this skill; the gate ensures exactly one
     delivers). Only when it prints `claimed` do you send. (Wake-push is ON by default —
     `SOTTO_WAKE_PUSH=0` disables it — so this gate is what keeps the two paths from double-delivering.)
   - Send **`brief_text`** (step 3.3), never `brief_markdown`, never your own re-formatting of either.
   - **Tap-to-act DOES work on chat.** Each action in `actions[]` carries a **`tap_link`** — a real tappable URL (`https://wa.me/…`, `mailto:…`, `tel:…`, `sms:…`, or the meeting link). WhatsApp/Telegram/iMessage render these as one-tap links. For the top 2–3 actions, attach the link to the person's name or add a short tappable line — e.g. "→ [Message Dhruv](https://wa.me/15551234567)" or "[Reply to the LOI](mailto:dhruv@acme.com?subject=Re:%20LOI)". Use the `tap_link` **verbatim**; don't invent URLs.
   - Pair the links with a one-line **conversational offer** for anything that needs drafting — "say *draft Dhruv* and I'll write the LOI reply, or *prep Berkeley* for the pitch." Mark the day delivered.
   - **The offer line is the ONLY thing you add.** Do not append extra sections, recaps, separators, or your own accountability/status blocks after the brief — the brief already contains everything (the evening's accountability lives INSIDE it as Already Handled / Still Pending, and `compose_brief.py` appends the evening-only **What moved today** receipts itself, plus — at most once per published version, morning or evening — a single housekeeping line saying a newer Sotto is available). One brief + at most one short offer line = the whole message.
   - If the brief came out **thin or the user pushes back on it**, append ONE recovery sentence so it carries its own next move: "If anything here looks off: say *that's wrong about X* and I'll fix my memory, *stop surfacing newsletters* to mute a source, or *clean up stale loops*."

## Notes
- First brief (empty graph): widen the window to 7 days and extract aggressively (seed mode). Day 2+: 24h deltas.
- **The open-items contract:** an open loop earns its own line ONLY when it is **overdue, due within
  24 hours, or already chased without an answer** (`brief_validate.is_urgent` — deterministic, read
  off the ledger row, never model-judged). Every urgent one appears exactly once — as an ask carrying
  its age, or in Already Handled when today's data shows it resolved. Every OTHER open loop is
  represented by ONE quiet line with a count and where to see them (`/app#loops`) and is worked by the
  proactive nudges, not by the brief. The extraction prompt states it, `brief_validate` measures it,
  and `compose_brief` appends the urgent misses (one line per PERSON, however many debts they carry)
  plus that single count line. On a genuinely quiet day — nothing to handle AND no open loops — the
  brief says so in one line rather than just omitting the sections.
