---
name: sotto-people
description: Use when the user asks Sotto about the people in their life — who needs attention, who they haven't talked to in a while, birthdays, or a profile of a specific person.
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
- **"What do I know about X?"** → `execute_code` → `python3 "$HOME/.hermes/skills/sotto/morning-brief/scripts/knowledge_query.py" --person "<name|id>"` → identity + top facts + talking points + recent activity.
- **"Who do I owe / who's slipping?"** → two real sources, both deterministic:
  - open loops: `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/loops_query.py"` (the continuity ledger, split `you_owe` / `waiting_on_them`);
  - relationship drift: read `$SOTTO_DATA/knowledge/relationship_state.json` (the weekly pulse's `attention_queue`: `waiting_on_you` / `losing_touch` / `lapsed`). If it's missing or stale, offer to run `sotto-relationship-pulse` — don't recompute cadence by hand.
- **Birthdays / check-ins** → from contacts (Bridge `get_contacts`) + the graph.

## Output format
- Person profile: one identity line (**Name** — title, company, if known), then ≤5 fact bullets and any talking points, each traceable to the graph/ledger output. Offer a draft at the end when a reply is owed.
- Attention answers: lead with counts ("3 you owe, 2 going quiet"), then name + one-line *why* per person (e.g. "no reply in 8 days to her question about the contract"), with the source signal.

## Rules
- **Grounded only:** state only facts found in the knowledge graph, the continuity ledger, or live Bridge/Google results. If it isn't there, say "I don't have that on X" — never guess a role, company, or reason.
- **No data:** empty graph + empty ledger → one honest line ("I don't have anything on the people front yet — briefs build this up over time"), not a padded answer.
- Surface *why* someone needs attention, then offer to draft (hand to `sotto-draft-reply`, under `sotto-approval-tiers`).
