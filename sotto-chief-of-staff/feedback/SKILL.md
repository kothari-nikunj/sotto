---
name: sotto-feedback
description: Use when the user gives feedback or a correction about a brief — "stop surfacing newsletters" / "don't show me X anymore" / "mute Bob" / "stop flagging <person>" / "that's wrong about <person>" / "<person> isn't the founder" / "keep my briefs terse" / "you got <fact> wrong". Records the preference (so future briefs honor it) or corrects the knowledge graph. Never sends anything.
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
every morning. Two kinds of feedback, two destinations. Both are write-only-to-disk; **never send a
message, email, or calendar change from this skill.** Confirm in ONE short line, as Sotto.

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
python3 "$P" show                               # read back the current preferences
```
- **Newsletters / "stop showing me emails from X"** → `mute-sender` with the address or `@domain`.
  (Find the exact address from the brief/thread if the user named a sender by display name.)
- **"don't surface / stop flagging <person>"** → `mute-person "<their display name>"`.
- **"drop the <X> section" / "I don't care about birthdays"** → `mute-section <id>`.
- **Tone/length/format** ("more terse", "no emojis", "lead with what needs me") → `tone "<note>"`.
- **Undo** ("show me Bob again") → `unmute-person "Bob Smith"` (same for `unmute-sender` / `unmute-section` / `clear-tone`).

## B · Corrections (the graph got a fact wrong) — "Peyton isn't the founder"

Route factual corrections about a PERSON to the knowledge graph as a **correction** fact (this
supersedes the wrong fact rather than piling on). State the truth the user gave you — or, if they only
told you what's wrong, the negation. **Do not invent the replacement fact.**

```bash
echo '{"person_updates":[{"person_name":"Peyton Lewis","facts":[
  {"fact":"Peyton is NOT the founder of Alive; correct her role per the user.",
   "change_type":"correction","confidence":0.95,"memory_type":"context",
   "source_ref":"user-correction"}]}]}' \
| python3 "$HOME/.hermes/skills/sotto/morning-brief/scripts/knowledge_update.py"
```
- Use the person's real display name (as it appears in the brief / graph) so it maps to the right file.
- If the user gave the corrected fact ("she's actually the COO"), write THAT as the fact text.
- Company-name fixes work the same way via the fact text (e.g. "Company is Alive, not Alive Ventures").

## Deliver
One line, as Sotto: e.g. *"Done — I'll stop surfacing newsletters from example.com."* or *"Got it —
fixed Peyton's record; I won't repeat that."* Nothing else; no message is sent anywhere.

## Notes
- These are the **explicit** half of preferences; the **behavioral** half (which tiers/contacts the user
  accepts) is learned automatically by `approval-tiers/learn_preferences.py` and lives in the same file —
  this skill never touches that block, and the learner never touches this one.
- Mutes take effect on the **next brief** (the composer reads `preferences.json` each run). No restart.
