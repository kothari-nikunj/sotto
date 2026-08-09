---
name: sotto-feedback
description: Use when the user gives feedback or a correction about a brief, or asks for fewer interruptions right now — "stop surfacing newsletters" / "don't show me X anymore" / "mute Bob" / "stop flagging <person>" / "keep my briefs terse" / "that's wrong about <person>" / "<person> isn't the founder" / "you got <fact> wrong" / "quieter today" / "be quiet until 3" / "quiet for 2 hours" / "back to normal". Records the preference (so future briefs and nudges honor it) or corrects the knowledge graph. Never sends anything.
metadata:
  hermes:
    tags: [chief-of-staff, sotto, preferences, feedback]
    category: productivity
    requires_tools: [execute_code]
required_environment_variables:
  - name: SOTTO_DATA
    prompt: Path to the Sotto exhaust volume (e.g. /data)
    required_for: persisting preferences + corrections
---

# Sotto — Feedback & corrections

When the user pushes back on a brief, **make it stick** — deterministically, in the volume Sotto reads
every morning. Three kinds of feedback, three destinations: a preference, a correction, or a cadence
change. All are write-only-to-disk; **never send a message, email, or calendar change from this
skill.** Confirm in ONE short line, as Sotto.

> **CRITICAL — ground everything in what the user said.** Never invent a correction or guess a name. If
> you're unsure which person/sender they mean, ask one short clarifying question instead of writing.

## A · Preferences (mute / tone) — "stop surfacing X", "keep it terse"

Use `preferences.py` (it stores these under the `explicit` block of `preferences.json`, which the brief
reads and the behavioral learner preserves). Pick the right command from what they said:

```bash
P="$HOME/.hermes/skills/sotto/_shared/scripts/preferences.py"
python3 "$P" mute-sender  news@example.com      # a newsletter / noisy sender (email OR @domain)
python3 "$P" mute-sender  @marketing.acme.com   # mute a whole sending domain
python3 "$P" mute-person  "Bob Smith"           # stop flagging this person in briefs
python3 "$P" mute-section birthdays             # drop a whole brief section (e.g. birthdays, screen_time)
python3 "$P" tone         "keep briefs terse — bullet points, no preamble"
python3 "$P" vip          "Sarah Chen"           # their missed calls reach you even in quiet hours
python3 "$P" show                               # read back the current preferences
```
- **Newsletters / "stop showing me emails from X"** → `mute-sender` with the address or `@domain`.
  (Find the exact address from the brief/thread if the user named a sender by display name.)
- **"don't surface / stop flagging <person>"** → `mute-person "<their display name>"`.
- **"drop the <X> section" / "I don't care about birthdays"** → `mute-section <id>`.
- **Tone/length/format** ("more terse", "no emojis", "lead with what needs me") → `tone "<note>"`.
- **"<person> is important / always let them through"** → `vip "<their display name>"`. VIP is narrow
  and honest: it clears the quiet-hours bar for their **missed calls**, nothing else.
- **Undo** ("show me Bob again") → `unmute-person "Bob Smith"` (same for `unmute-sender` /
  `unmute-section` / `unvip` / `clear-tone`).

## B · Corrections (the graph got a fact wrong) — "Peyton isn't the founder"

Route factual corrections about a PERSON to the knowledge graph as a **correction** fact (this
supersedes the wrong fact rather than piling on). State the truth the user gave you — or, if they only
told you what's wrong, the negation. **Do not invent the replacement fact.**

```bash
echo '{"person_updates":[{"person_name":"Peyton Lewis","facts":[
  {"fact":"Peyton is NOT the founder of Alive; correct her role per the user.",
   "change_type":"correction","confidence":0.95,"memory_type":"context",
   "source_ref":"user-correction"}]}]}' \
| python3 "$HOME/.hermes/skills/sotto/_shared/knowledge/knowledge_update.py"
```
- Use the person's real display name (as it appears in the brief / graph) so it maps to the right file.
- If the user gave the corrected fact ("she's actually the COO"), write THAT as the fact text.
- Company-name fixes work the same way via the fact text (e.g. "Company is Alive, not Alive Ventures").
- **"Vishnu didn't introduce us"** is a RELATION, not a fact — relations live in their own block and
  a correction fact won't touch one. Remove it (both people's files, one call), passing the two file
  slugs (`canonical_id`s — the parenthetical in a person's knowledge block):
  ```bash
  python3 "$HOME/.hermes/skills/sotto/_shared/knowledge/knowledge_edit.py" \
    --slug c_priya01 --op relation-remove --other-slug c_vishnu1   # add --type to narrow it
  ```
  The reverse — "Vishnu introduced me to Priya" said in chat — is `--op relation-add --type
  introduced_by --other-slug <slug>` (types: `introduced_by`, `introduced`, `works_with`,
  `family_of`, `partner_of`, `met_through`, `connected`). Never guess a slug: if you can't see both people's
  ids, ask.

## B2 · Voice briefs — "send my briefs as voice notes" / "text only please"

One sentence: *your briefs arrive as voice notes too, whenever you say so — the text is always sent
regardless.* Run `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/preferences.py" brief-audio
both` (or `morning` / `evening` for just one, `off` to stop). Confirm in one line: "Voice notes on —
your briefs will arrive spoken and written." The brief skills read this on every cron run.

## C · Cadence — "quieter today", "quiet until 3", "back to normal"

When the user wants *fewer interruptions for a while* (not a permanent mute), set the nudge snooze.
It writes `nudge_snooze_until` in the same explicit block, and both the real-time event funnel and
the `*/15` proactive watcher hold every nudge while it's in the future — held events still land in
the queue and surface in the midday digest / next brief, so nothing is lost.

```bash
P="$HOME/.hermes/skills/sotto/_shared/scripts/preferences.py"
python3 "$P" snooze-nudges tomorrow    # "quieter today" — lifts when quiet hours end (7am default)
python3 "$P" snooze-nudges "+2h"       # "quiet for 2 hours" (also "90m")
python3 "$P" snooze-nudges 15:00       # "quiet until 3" (also 3pm; a past time means tomorrow)
python3 "$P" snooze-nudges 2026-08-08T06:00   # an explicit local date-time, if they named one
python3 "$P" unsnooze-nudges           # "back to normal" — clears the snooze
```
- **Let the script do the clock math** — pass the user's words as one of the specs above (`tomorrow`,
  `+2h`, `15:00`, `3pm`, an ISO stamp). Never compute the timestamp yourself; the script resolves it
  in the user's timezone and prints the stored value back. **Say the lift time it printed**, not a
  remembered one: **a snooze lifts when quiet hours do** (`SOTTO_QUIET_END`, 7am by default), so a
  box with a different quiet-end resolves `tomorrow` to a different hour.
- **"too many notifications" / "you're being noisy" without a time** → default to `tomorrow`
  ("quieter today") and say when it lifts, so the user can shorten it.
- **A permanent "stop pinging me about X"** is a *mute* (§A), not a snooze — snoozes always expire.
- The snooze silences nudges only. Scheduled morning/evening briefs still arrive.

Confirm in ONE line with the lift time the script printed, as Sotto — e.g. "Quiet until 7am
tomorrow — I'll hold everything and catch you up then." Plain chat text, no markdown headings/bold
(the chatfmt rule — chat clients render them literally).

## Deliver
One line, as Sotto: e.g. *"Done — I'll stop surfacing newsletters from example.com."* or *"Got it —
fixed Peyton's record; I won't repeat that."* Nothing else; no message is sent anywhere.

## Notes
- These are the **explicit** half of preferences; the **behavioral** half (which tiers/contacts the user
  accepts) is learned automatically by `approval-tiers/scripts/learn_preferences.py` and lives in the same file —
  this skill never touches that block, and the learner never touches this one.
- Mutes take effect on the **next brief** (the composer reads `preferences.json` each run). No restart.
- The **snooze** takes effect immediately — the event funnel and the proactive watcher read
  `preferences.json` on every tick — and expires on its own. Nothing held during it is deleted: it
  queues and rides the midday digest or the next brief.
