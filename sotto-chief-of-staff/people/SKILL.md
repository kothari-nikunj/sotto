---
name: sotto-people
description: Use when the user asks Sotto about the people in their life — who needs attention, who they haven't talked to in a while, birthdays, a profile of a specific person, or finding, adding, editing or deleting a Google contact.
metadata:
  hermes:
    tags: [chief-of-staff, sotto, people]
    category: productivity
    requires_toolsets: [sotto-local]
    requires_tools: [execute_code]
---

# Sotto — People

The People tab, as a conversation. PORT SOURCE: people analytics + attention queue (people.rs / pipeline).

> Script paths are absolute under `$HOME/.hermes/skills/sotto/`.

## Capabilities
- **"What do I know about X?"** → `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/knowledge/knowledge_query.py" --person "<name|id>"` → identity + top facts + talking points + recent activity.
- **"Who do I owe / who's slipping?"** → two real sources, both deterministic:
  - open loops: `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/loops_query.py"` (the continuity ledger, split `you_owe` / `waiting_on_them`);
  - relationship drift: read `$SOTTO_DATA/knowledge/relationship_state.json` (the weekly pulse's `attention_queue`: `waiting_on_you` / `losing_touch` / `lapsed`). If it's missing or stale, offer to run `sotto-relationship-pulse` — don't recompute cadence by hand.
- **Birthdays / check-ins** → from contacts (Bridge `get_contacts`) + the graph.
- **"Research X for me"** → not a graph read; it's the ad-hoc research lane in `sotto-ask`
  (`research_attendees.py --focus` → `persist_prep.py`). Use it, so what you find is on file next
  time instead of only in this chat.

## Output format
- Person profile: one identity line (**Name** — title, company, if known), then ≤5 fact bullets and any talking points, each traceable to the graph/ledger output. Offer a draft at the end when a reply is owed.
- Attention answers: lead with counts ("3 you owe, 2 going quiet"), then name + one-line *why* per person (e.g. "no reply in 8 days to her question about the contract"), with the source signal.

## Rules
- **Grounded only:** state only facts found in the knowledge graph, the continuity ledger, or live Bridge/Google results. If it isn't there, say "I don't have that on X" — never guess a role, company, or reason.
- **No data:** empty graph + empty ledger → one honest line ("I don't have anything on the people front yet — briefs build this up over time"), not a padded answer.
- Surface *why* someone needs attention, then offer to draft (hand to `sotto-draft-reply`, under the approval tiers).

## Google address book

Use `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/google_action.py" capabilities` to check actual grants. Google Contacts is separate from the Mac Contacts reader and Sotto's relationship graph.

- Find: `contacts-search --query "<name, email or phone prefix>"`. Read the exact match with `contacts-get --resource-name people/c...` before editing. Ask which contact if ambiguous; never invent a resource name.
- Add on request: `contacts-create --fields-json '{"names":[{"givenName":"Alex","familyName":"Example"}],"emailAddresses":[{"value":"alex@example.com"}]}'`.
- Edit on request: `contacts-update --resource-name people/c... --fields-json '{"phoneNumbers":[{"value":"+15555550123","type":"mobile"}]}'`. Each supplied field replaces its entire array: preserve existing numbers/emails when adding another. Omitted fields remain unchanged. Allowed fields are names, emailAddresses, phoneNumbers, organizations, birthdays, addresses and biographies.
- Delete only when the user explicitly asks to delete that exact contact: `contacts-delete --resource-name people/c...`.

All writes use the shared approval/unattended gate. Never bypass it through the upstream CLI or direct API. A connection grant permits tools to act when asked; it does not authorize background edits. Confirm completion only after the tool returns a successful result with the contact ID. If access is missing, ask the user to reconnect Google; do not claim it was saved locally or to their Mac.

## Who matters most

For "who is important / who are my VIPs", run `python3 "$HOME/.hermes/skills/sotto/_shared/lib/relationship_importance.py"`; add `--person "<name>"` to explain one person. This reads the shared relationship history and applies the current rolling window, rather than ranking by overdue replies or raw message volume. Report the returned tier, active days/weeks and two-way evidence. The user's `preferences.py vip` choice overrides activity; names that resolve ambiguously need clarification. This importance controls proactive gift offers, not automatic permission to send, buy or bypass quiet hours. Explicitly requested gift help is always allowed.
