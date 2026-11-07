---
name: sotto-setup
description: Use when setting up Sotto for the first time, when the user says "set up Sotto" / "get started" / "onboard me", or when local data seems unavailable — runs the whole guided first-run (check connections, seed memory, schedule briefs, offer the first one).
metadata:
  tags: [chief-of-staff, sotto, setup]
  category: productivity
---

# Sotto — Setup (guided first run)

Get the user from zero to their first brief in one short, friendly conversation. Be warm and brief —
one line per step, no walls of text. Do the work; don't make them read a manual.

> **How this runs on Sotto:** every check below is a read through a native tool (`health`,
> `capabilities`, `gmail_search`, `calendar_list`) or one `run_sotto_script` command — never a
> shell, a file listing or an environment probe.

## Procedure

**0. Establish whether there is a Mac before giving setup instructions.** Call the Bridge `health()`
tool when it is available. Only skip local sources when no Bridge is linked and the user confirms
they have no Mac or do not want local sources. Gmail and Calendar alone are a supported deployment.
The connection remedies below describe the receiver's `/setup` page. A missing relay token alone
never means that local data is unavailable.

**0b. If the `health()` tool isn't available** on a deploy that DOES have a Mac, STOP and say in one line — pick the right message:
> - **If the Mac recently woke from sleep:** "Your Mac just woke up — I reconnect to the Bridge automatically within ~60 seconds. Give it a moment and ask me again."
> - **Otherwise:** "Your Sotto Bridge isn't linked yet. Open the **Sotto Bridge** menu bar app on your Mac (it relaunches itself on login) — it dials out to me automatically, there's no tunnel to run — then say *set up Sotto* again. If it's been up a while and I still can't see it, restart the Sotto host (Railway service) once."
Do NOT explore the filesystem or packages — a missing tool means "not connected," nothing to discover.

**1. Open with one sentence.** "I'm Sotto — your chief of staff. I'll pull your messages, email, and calendar into one brief each morning and evening, learn the people in your world, and help you reply. Let's get you set up — takes a few minutes (up to ~8 the very first time — it's researching the people in your calendar), and I'll tell you what I'm doing as I go."

**2. Check EVERY connection — Bridge, Google, delivery channel, AND Granola.** Actually verify each one (a probe, not an assumption); the results feed the step-5 checklist. Narrate briefly while things run so silence never reads as a hang.
   - **Bridge** — call `health()`:
     - Health reports the connection and enabled source permissions. Most source checks are capability checks, not content reads; say “enabled” until a current content read succeeds, and retain any partial/unavailable coverage.
     - `fda != ok` → "Open **Sotto Bridge** in your menu bar → grant **Full Disk Access** (the panel links you there). Tell me when done." Then `health()` again.
     - `link != ok` → "The Bridge can't reach me — open the **Sotto Bridge** menu bar app on your Mac. It dials out to me automatically; there's no tunnel to run."
     - a `source: needs_fda/unavailable` → note which signal is missing (e.g. WhatsApp not installed) and proceed without it.
   - **Google** — REQUIRED to verify, don't just ask. Call `capabilities` for the actual grants, then probe with ONE tiny read (`gmail_search` for one recent message, or `calendar_list` for today). Any successful read (even 0 results) = **connected**; a missing grant or failed read = **not connected** → "Google isn't linked yet — open your `/setup` page to connect Gmail + Calendar."
   - **Delivery channel** — briefs and nudges land in this conversation: Sotto's channel is Photon
     (iMessage). That this chat reached you is the probe. Do not send the user to another
     channel's link step.
   - **Granola** — OPTIONAL. Probe with one small read: `run_sotto_script granola-history --days 1`. A result (even with no meetings) = **connected**; an unavailable-authority error = not connected → mark it "optional, skipped" and move on (one line: "No Granola — optional; one-click connect lives on your `/setup` page under **Connected services**."). A connected Granola whose read fails → "Granola is linked but the fetch failed — reconnect it on your `/setup` page (Connected services)." Never block setup on it.
   Don't claim full capability until **FDA + the Bridge connection are green**; report Google, the delivery channel and Granola honestly as found.

**3. First useful look and progressive learning.** The receiver handles this automatically once a
context source and delivery channel are ready. It seeds observed voice and people from recent
context, composes a short first look through the normal pipeline, and sends it through the existing
outbox. `config/onboarding.json` records delivery/retry state. Do not run a second seed, queue
another welcome, or make the user answer a profile questionnaire first.

Progressive learning needs the budgeted model proxy, or an explicit self-host
`SOTTO_BACKGROUND_UNMETERED=true` opt-in to direct-key background spending. Never set that opt-in
for the user. For a `background_budget_not_configured` hold, say "Your briefs work, but background
history learning hasn't started; its spending configuration is missing." For other holds, explain
the recorded reason instead. Report actual coverage.

The receiver heartbeat reviews paginated iMessage, WhatsApp and Gmail history progressively. Its
`knowledge/history-state.json` holds the actual per-source progress: frozen initial bounds, pages
and rows fetched, messages reviewed, completion and sanitized failures. A bounded window is not a
claim that all history has been reviewed. Mac sources need the Bridge online; Google can continue
while the Mac sleeps. Other sources retain their existing brief/prep learning paths. Historical
pages never enter the live nudge queue. Keep progress quiet unless the user asks or a connection
needs their attention.

**3b. Preferences are optional corrections after useful work.** Learn voice from real sends and
use explicit ratings as examples. Never turn observed activity into a confirmed family role,
standing rule or permission. If the user gives a priority or rule, use `sotto-feedback` and the
existing master-file writer. Ask a single specific question only when its answer would change a
real next action; there is no mandatory “tell me about yourself” form.

**4. Check the installed schedule; do not create system jobs from chat.**
   The receiver owns the five system jobs (morning and evening brief, weekly relationship pulse,
   proactive check, midday digest), their times, feature gates and timezone. There is no chat
   command that reads or edits that schedule: the dashboard's Activity page shows it to the user,
   and `/setup` changes it. Report only a schedule you actually verified, in the configured
   timezone; otherwise say scheduling is unverified and don't promise delivery times. Missing jobs
   are an installation problem for the deployment's own setup, never something to recreate as
   routines. The user's separate `user-*` routines still belong to `sotto-routines`.

**5. Close with an HONEST per-connection checklist** — one line per connection, using what step 2 actually verified (✓ = probed OK, ✗ = failed + the one-line fix, – = optional and skipped). Never print a blanket "all set" over a red row. The shape:
   > Here's where you stand:
   > - **Bridge** (Mac: messages, calls, contacts) — ✓ connected *(or ✗ — open the Sotto Bridge menu bar app on your Mac; it dials out — no tunnel)*
   > - **Google** (Gmail + Calendar) — ✓ connected *(or ✗ — open your `/setup` page to connect)*
   > - **Delivery** (where briefs land) — ✓ this iMessage conversation
   > - **Granola** (meeting notes) — ✓ connected *(or – optional, skipped)*
   > Briefs — <verified times and timezone from step 4, or “schedule not yet verified”>.
   **Then say the posture out loud — three plain lines, once, right here** (this is the only moment
   the user is guaranteed to read it; state it, don't sell it — no marketing, no emphasis stacking):
   > - No bot ever joins your calls. Meeting notes come from Granola reading the notes you already
   >   take — nothing of mine dials in or records.
   > - Replies default to drafts. Sending to someone else requires your explicit approval and an
   >   enabled sending capability; connecting a channel alone does not grant that permission.
   > - The dashboard's Record view explains triage decisions.
   If they want the full rules, point them at the project's **HOW-SOTTO-DECIDES** doc on GitHub
   (`docs/HOW-SOTTO-DECIDES.md` in the repo they deployed from — it isn't installed locally) — one
   line, then move on.
   When the receiver and channel are ready, the first useful look arrives automatically;
   don't make the user ask for it. Failed connections get the one concrete fix, and intentionally
   skipped sources never prevent using the others.

## Notes
- **Photon (iMessage) is the supported channel today; Telegram is planned and limited.** If they ask about another channel or a non-Gemini model, don't improvise — point them at the project's **CHANNELS.md** ("Choosing your channel and model") on GitHub, in the repo they deployed from (it isn't installed locally). One line, then move on.
- Keep it to ~5 short exchanges total. During step 2, don't narrate every green check one by one — the step-5 checklist is the summary; only surface a check mid-flow when it's red and needs the user.
- Never assume a grant; always verify via `health()` and `capabilities`. Never retry a failed tool call in a loop.
- If they come back later with "is Sotto working?", just run steps 2 + (if green) 5.
