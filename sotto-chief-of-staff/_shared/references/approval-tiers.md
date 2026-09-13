# Approval tiers — what Sotto may do without asking

The autonomy policy every skill honors before it sends, schedules, or executes anything.
PORT SOURCE: `api/src/services/approval-policy.ts`. **Never exceed a tier without explicit user
say-so.**

| Tier | Meaning | Default actions |
|---|---|---|
| `auto` | run immediately, no confirmation; just log it | meeting info, meeting prep, opening a meeting link, copying talking points, calls (`tel:`) |
| `one_tap` | one confirmation, then run | iMessage / SMS, WhatsApp, calendar RSVP |
| `review` | show full content, allow edits, confirm, then run | email drafts, **saving an email into Gmail drafts** (`google_action.py gmail-draft`), follow-ups, calendar create/reschedule, **every decline draft** |
| `forbidden` | never auto-execute; surface only | anything destructive, financial, or irreversible |

## Applying it

Default an action to its tier above. When in doubt, escalate — treat it as `review`.

**Draft usage does not grant permission.** Apply the fixed defaults above and the user's explicit
instructions. Historical `approval_defaults` in `preferences.json` are ignored; the behavioral
learner has been removed because accepted drafts are not consent to future actions.

**A decline is `review` forever.** Show its full text for review every time. No amount of previous
acceptance relaxes that rule. Log it with `action_type: "decline"` so its outcome remains identifiable.

**Attention is not a decision.** An unanswered invitation, pitch, funding request or favor does
not tell you whether the user wants to accept or decline. A relevance score, a due date, or a
pattern of previous passes does not choose this answer. When the direction is still open, ask
which way they want to go; if supplying drafts, label both alternatives as `Accept:` and `Decline:`
as specified in `sotto-draft-reply`. Never present an unsolicited pass as the selected reply or
invent a reason such as "not investing in this space." A single directional draft requires the
user's instruction or a clear commitment in the supplied conversation; factual replies remain
grounded in their evidence. This applies equally to briefs, digests, nudges and attended chat.

**A Gmail draft is not a send — and still not automatic.** `gmail-draft` writes to the user's own
drafts folder, so it can never leave the house without them pressing send; that is why an email
offer says "want this in your Gmail drafts?" instead of pasting a `mailto:`. It is still `review`:
the draft is created only after the user answers yes, in that conversation. No cron, proactive tick
or scheduled run may create one silently, and no learned default relaxes it below `review` — a
drafts folder filling itself up while the user sleeps is exactly the busywork theater a chief of
staff doesn't do.

**The tiers are policy; every real effect has an enforcement seam.** Everything above is a rule an
agent honors — and an agent running with approvals auto-bypassed (`hermes -z`, which is how every
cron, proactive and event-triage run executes) honors nothing by construction. So the acts that
write the user's account are gated in code rather than in prose: the receiver sets
`SOTTO_UNATTENDED` in the environment of every skill it spawns, and `google_action.py` refuses
**`gmail-draft`, `gmail-send`, `gmail-reply`, `calendar-create`, `calendar-delete` and
`calendar-rsvp`** when it is set, before any network call, exiting non-zero with
`fallback: "propose_in_brief"`. The draft verb is in that list because the paragraph above means
it: "no cron, proactive tick or scheduled run may create one silently" was prose-only until Aug 31
(external review) — this page simultaneously claimed drafts "stay available unattended", and the
code sided with the weaker sentence. Now the wall does. **A refusal is the policy working, not an
error to route around** — never retry it, never reach for another send path; propose the item in
the brief or queue it for the next conversation. Read verbs stay available unattended; everything
still needs its tier's approval when attended.

**Unattended runs.** A scheduled or unattended send may only use `auto` — plus `one_tap` for a
recipient the user has pre-approved, and that allowance covers **message drafts**
(iMessage/SMS/WhatsApp) only. It does not cover the `one_tap` calendar write: an RSVP runs on the
user's explicit in-chat instruction in the same conversation, never from a scheduled, proactive, or
cron context — and since Aug 2026 that is the code's answer too, not just this page's. When
unattended and an action needs `review` (or is any calendar write), queue it for the next
interaction instead of sending.

## The receipt, and what an approval is actually bound to

**Every real effect leaves one metadata-only receipt in `$SOTTO_DATA/events/sends.jsonl`, carrying
`payload_sha256` — the hash of the exact bytes that left.** The body of a mail, the canonical form
of an event, the id and response of an RSVP: hashed, never stored. So "what did Sotto send?" can be
checked against what the user was shown — the same text hashes to the same digest — while the ledger
still holds not one readable word of anyone's mail.

**When a yes crosses a process boundary, bind it to those bytes.** A nudge delivered by a detached
run asks the question; the gateway session that receives "sure" three processes later never saw it.
There, the offering lane writes the payload's hash into the pending offer
(`google_action.py <verb and args> --print-payload > payload.json`, then
`pending_offer.py set --action <verb> --payload-file payload.json`)
and the acting session passes `--offer-bound --offer-id <id>` to the verb. The verb then requires
that exact **fresh** offer, its named action, and a `payload_sha256` equal to
what it is about to do, refuses with exit 2 and a named reason on absence, expiry or mismatch,
records the refusal in the receipt, and atomically consumes the offer before the effect begins.
A payload mismatch leaves it unspent; once the effect starts, success, failure, timeout and crash
all require fresh approval because the provider outcome may be uncertain. Approve-then-mutate hits a wall.
Offers whose yes only runs a read ("want me to pull prep?") carry no payload and pass no file;
binding is for offers whose yes causes an outbound send or a calendar write.

**Say the limit out loud: in-session approval is not machined this way.** When the user says yes in
the same conversation and the agent immediately calls the verb, the agent computes both sides of any
check it could run — so `--offer-bound` there would prove nothing, and claiming otherwise would be
worse than the gap. What the hash buys in that lane is **disputability, not prevention**: afterwards
you can prove which bytes were sent against the draft that was shown, and a mismatch is visible
rather than deniable. The preventive property exists exactly where the approval and the act live in
different processes.

### Google inbox, event updates and address book

`gmail-modify`, `calendar-update`, `contacts-create`, `contacts-update` and `contacts-delete`
are account writes and use the same unattended refusal and optional `--offer-bound` contract.
A Google read/write grant is a capability, not approval for an individual change. The user must
request the change in conversation, with the exact message, event or contact resolved first.
Contact deletion requires an explicit request to delete that contact. Scheduled jobs may propose
these changes but cannot perform them. These are Google Contacts operations; Bridge's Mac Contacts
reader and Sotto's relationship graph remain separate.
