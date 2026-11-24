---
name: sotto-setup
description: Use when setting up Sotto for the first time, when the user says "set up Sotto" / "get started" / "onboard me", or when local data seems unavailable — checks connections, guides the owner through setup, and explains how the first brief arrives.
metadata:
  tags: [chief-of-staff, sotto, setup]
  category: productivity
---

# Sotto — Setup (guided first run)

Check what is ready and give the owner one concrete step for each connection that needs attention.
Use the native tools advertised in this turn. Use a single `run_sotto_script` call for the Granola
check. Keep tool names and internal diagnostics out of the reply.

## Procedure

**1. Start with what you can verify.** For example, “I’ll check what I can reach and show you
what still needs setup.” Leave first-brief timing open.

**2. Check the connections independently.**
   - **Bridge** — call `health` when available. Check link, Full Disk Access, and enabled local
     sources separately. Health can confirm that a source is enabled. Say that content was
     readable only after a current read succeeds. If Full Disk Access is missing, direct the
     owner to the Sotto Bridge menu bar app to grant it, then offer to recheck. If health is
     unavailable, say you couldn’t verify the Bridge from this chat. If they use a Mac and want
     its sources, ask them to open the Sotto Bridge menu bar app and check its connection. Keep
     checking the other connections. The Mac is optional on the invited Cloud path.
   - **Google** — use `capabilities` to check the Gmail and Calendar read grants separately.
     Where a grant and tool are available, make a small read for that source with `gmail_search`
     or `calendar_list`. A successful empty read confirms access. If the grant is missing, point
     to Google on the setup page. If a granted read fails, say which source you couldn’t read
     and ask the owner to check its connection on the setup page. Cloud requires Gmail or
     Calendar. Self-host can also use other connected context sources.
   - **Conversation** — the current owner chat shows that chat is reachable. Use its actual
     channel when known. Invited Cloud uses iMessage. Self-host can also use a paired Telegram
     bot. The first brief arriving on the device is the check for brief delivery.
   - **Granola** — optional. If the command is advertised, use
     `run_sotto_script granola-history --days 1`. A successful empty read confirms access. A
     missing grant or unavailable read leaves access unverified. If they want to connect or
     check it, point to **Connected services** on the setup page. Continue with other sources.

**3. First useful look and progressive learning.** The receiver handles this automatically once a
context source and delivery channel are ready. It seeds observed voice and people from recent
context, composes a short first look through the normal pipeline, and sends it through the existing
outbox. `config/onboarding.json` records delivery/retry state. Do not run a second seed, queue
another welcome, or make the user answer a profile questionnaire first.

Progressive learning needs the budgeted model proxy, or an explicit self-host
`SOTTO_BACKGROUND_UNMETERED=true` opt-in to direct-key background spending. Never set that opt-in
for the user. If a verified hold is `background_budget_not_configured`, say “Background history learning is
waiting for its spending setup.” For other verified holds, explain the recorded reason. Report
learning progress only when you have a current status.

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

**5. Summarize the result in short lines.** Report the Bridge, Gmail, Calendar, the current
conversation, and optional Granola from the checks above. Use simple statuses such as “read
verified,” “enabled,” “needs setup,” or “couldn’t verify.” For each connection that needs action,
give one fix. Report brief delivery and schedule only when verified. Point the owner to Activity
to check the schedule and to the setup page to change it.

At first setup, state these three facts once:
   > Sotto reads meeting notes through Granola. No Sotto bot joins your calls.
   > Replies start as drafts. Sending to someone else requires your explicit approval and an enabled sending capability.
   > The dashboard’s Record view explains triage decisions.

The first brief is automatic once the receiver’s readiness checks pass. If the owner asks for
the full setup steps, point them to the project’s **ONBOARDING.md**. For “is Sotto working?”,
run the relevant checks and report what you observed.
