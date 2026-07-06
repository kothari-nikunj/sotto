---
name: sotto-setup
description: Use when setting up Sotto for the first time, when the user says "set up Sotto" / "get started" / "onboard me", or when local data seems unavailable — runs the whole guided first-run (check connections, seed memory, schedule briefs, offer the first one).
metadata:
  hermes:
    tags: [chief-of-staff, sotto, setup]
    category: productivity
    requires_toolsets: [sotto-local]
    requires_tools: [execute_code]
---

# Sotto — Setup (guided first run)

Get the user from zero to their first brief in one short, friendly conversation. Be warm and brief —
one line per step, no walls of text. Do the work; don't make them read a manual.

## Procedure

**0. If the `health()` tool isn't available** (the `sotto-local` toolset isn't connected), STOP and say in one line — pick the right message:
> - **If the Mac recently woke from sleep:** "Your Mac just woke up — I reconnect to the Bridge automatically within ~60 seconds. Give it a moment and ask me again." (The host binds the Bridge connection at startup; a watchdog bounces it to reconnect shortly after your Mac comes back online.)
> - **Otherwise:** "Your Sotto Bridge isn't linked yet. Open the **Sotto Bridge** menu bar app on your Mac (it relaunches itself on login) — it dials out to me automatically, there's no tunnel to run — then say *set up Sotto* again. If it's been up a while and I still can't see it, restart the Sotto host (Railway service) once."
Do NOT explore the filesystem / packages / Hermes internals or run `hermes tools list` — a missing tool means "not connected," nothing to discover.

**1. Open with one sentence.** "I'm Sotto — your chief of staff. I'll pull your messages, email, and calendar into one brief each morning and evening, learn the people in your world, and help you reply. Let's get you set up — takes a few minutes, and I'll tell you what I'm doing as I go."

**2. Check EVERY connection — Bridge, Google, AND Granola.** Actually verify each one (a probe, not an assumption); the results feed the step-5 checklist. Narrate briefly while things run so silence never reads as a hang.
   - **Bridge** — call `health()`:
     - `fda != ok` → "Open **Sotto Bridge** in your menu bar → grant **Full Disk Access** (the panel links you there). Tell me when done." Then `health()` again.
     - `link != ok` → "The Bridge can't reach me — open the **Sotto Bridge** menu bar app on your Mac. It dials out to me automatically; there's no tunnel to run."
     - a `source: needs_fda/unavailable` → note which signal is missing (e.g. WhatsApp not installed) and proceed without it.
   - **Google** — REQUIRED to verify, don't just ask. Run `execute_code`:
     `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py" --ensure-deps` first — it heals the Google client library NOW (one-time; **can take up to ~4 minutes on first run** — say "checking your Google connection — first time takes a few minutes" while it installs). Then probe with ONE tiny fetch:
     `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py" --max 1 --bodies 0` and read its `[gather_google] N emails, M events` line (or, if this host reaches Google via a Gmail/Calendar MCP instead of the CLI, call the host's Gmail tool for 1 result). Any successful fetch (even 0 results with no WARNING) = **connected**; a WARNING/failure = **not connected** → "Google isn't linked yet — open the `/setup` link from your deploy logs to connect Gmail + Calendar."
   - **Granola** — OPTIONAL. If the Granola MCP is configured (a Granola tool is in your toolset — it's wired via `GRANOLA_MCP_CMD`), try one `list meetings` call: success = connected. If no Granola tool exists, it's simply not set up — mark it "optional, skipped" and move on (one line: "No Granola — that's optional, skipping meeting-notes import."). Never block setup on it.
   Don't claim full capability until **FDA + the Bridge connection are green**; report Google and Granola honestly as found.

**3. Seed memory + voice (so day 1 isn't cold).** Once the Bridge is green, do a one-time seed so the first brief already knows people and writes like the user:
   1. `read_local(since_hours=1008)` (≈6 weeks) → `/tmp/sotto_seed.json`.
   2. `execute_code`:
      ```bash
      python3 "$HOME/.hermes/skills/sotto/_shared/scripts/style_extract.py" /tmp/sotto_seed.json      # learn their writing voice
      python3 "$HOME/.hermes/skills/sotto/_shared/scripts/prewarm_graph.py" /tmp/sotto_seed.json      # pre-warm the graph: who they talk to most
      python3 "$HOME/.hermes/skills/sotto/relationship-pulse/scripts/relationship_pulse.py" /tmp/sotto_seed.json   # seed who's waiting / going quiet
      ```
   `prewarm_graph.py` creates identity stubs for the user's most-frequent contacts so the FIRST brief
   already recognizes the people in their world — it does NOT invent roles/companies (that's earned
   later via the Learn step). Safe to run with no output to read. *(Power users: `SOTTO_PREWARM_RESEARCH=1`
   also background-researches emailed contacts, stored as low-confidence "per web search" notes.)*
   If the Bridge is slow, skip this and let the first brief seed it. `style_extract.py` prints
   `{"messages_analyzed": N, ...}` — only claim the seed worked if N > 0. Say one line: "Learned your
   writing style and the people you talk to most." (If N = 0, say the seed was thin and the briefs
   will learn as they run — don't announce a learned style that doesn't exist.)

**4. Schedule the briefs — dedup first, ALWAYS.** Run `hermes cron list` FIRST and check which of the three jobs below already exist (match by name/skill — the installer usually creates them). Create ONLY the missing ones; never create a job whose name already appears in the list (a second "set up Sotto" must not double-schedule — duplicate crons have caused 429 storms before):
   - Morning brief — `hermes cron create "30 6 * * *" "Run my morning brief" --skill sotto-morning-brief`
   - Evening brief — `hermes cron create "30 17 * * *" "Run my evening brief" --skill sotto-evening-brief`
   - Weekly relationship pulse — `hermes cron create "0 9 * * 1" "Run my relationship pulse" --skill sotto-relationship-pulse`
   Tell the user the times and that they can change them ("want different times? just tell me").

**5. Close with an HONEST per-connection checklist** — one line per connection, using what step 2 actually verified (✓ = probed OK, ✗ = failed + the one-line fix, – = optional and skipped). Never print a blanket "all set" over a red row. The shape:
   > Here's where you stand:
   > - **Bridge** (Mac: messages, calls, contacts) — ✓ connected *(or ✗ — open the Sotto Bridge menu bar app on your Mac; it dials out — no tunnel)*
   > - **Google** (Gmail + Calendar) — ✓ connected *(or ✗ — open the `/setup` link from your deploy logs to connect)*
   > - **Granola** (meeting notes) — ✓ connected *(or – optional, skipped)*
   > Briefs are scheduled for 6:30am and 5:30pm.
   Then offer the first brief: if Bridge AND Google are ✓ → "You're all set ✅ — want your first brief right now? Just say *good morning*. Otherwise I'll have it ready at 6:30am." If anything required is ✗ → "Once that's fixed, say *set up Sotto* again and I'll re-check — you can still say *good morning* for a partial brief from what I can see." If they say yes → run `sotto-morning-brief`.

## Notes
- Keep it to ~5 short exchanges total. During step 2, don't narrate every green check one by one — the step-5 checklist is the summary; only surface a check mid-flow when it's red and needs the user.
- Never assume a grant; always verify via `health()`. Never retry a failed tool call in a loop.
- If they come back later with "is Sotto working?", just run steps 2 + (if green) 5.
