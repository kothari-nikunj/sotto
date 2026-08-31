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

**Learned overrides.** Every brief's Learn step runs `approval-tiers/scripts/learn_preferences.py`,
which tallies `$SOTTO_DATA/outcomes.jsonl` into `preferences.json` → `approval_defaults` (keyed
`contact|action_type`, emitted only after ≥3 accepted outcomes at ≥80% acceptance). Honor them
narrowly: a learned default may relax `review` → `one_tap` for that exact contact + action_type and
nothing else. It never relaxes anything into `auto`, never overrides `forbidden`, and never
overrides the user's own stated preferences (the reserved `explicit` block in `preferences.json` —
user's word wins). When a learned default is *stricter* than the table, take the stricter one.

**A decline is `review` forever.** When a draft says *no* on the user's behalf
(`sotto-draft-reply`'s no-draft rule), no amount of learning may relax it: not after 3 accepted
declines, not after 30. Sending an unread "no" is the one outcome you cannot walk back. The guard
lives in code — `learn_preferences.py`'s `NEVER_RELAX = {"decline"}`, matched per word against the
`action_type` half of the key, so `decline`, `decline_reply` and `reply_decline` all pin to
`review` whatever tier the user accepted at. So **log a no with `action_type: "decline"`** — that
token is the entire mechanism, and a decline logged as a plain `reply` walks straight out of the
guard. Any future outbound class that is equally unrecoverable joins the set; nothing leaves it.

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
(`pending_offer.py set --payload-file <file holding the exact bytes>`) and the acting session passes
`--offer-bound` to the verb. The verb then requires a **fresh** offer whose `payload_sha256` equals
what it is about to do, refuses with exit 2 and a named reason on absence, expiry or mismatch,
records the refusal in the receipt, and clears the offer only after the act succeeds — a refused or
failed attempt leaves the user's yes unspent. Approve-then-mutate hits a wall instead of a prompt.
Offers whose yes only runs a read ("want me to pull prep?") carry no payload and pass no file;
binding is for offers whose yes causes an outbound send or a calendar write.

**Say the limit out loud: in-session approval is not machined this way.** When the user says yes in
the same conversation and the agent immediately calls the verb, the agent computes both sides of any
check it could run — so `--offer-bound` there would prove nothing, and claiming otherwise would be
worse than the gap. What the hash buys in that lane is **disputability, not prevention**: afterwards
you can prove which bytes were sent against the draft that was shown, and a mismatch is visible
rather than deniable. The preventive property exists exactly where the approval and the act live in
different processes.
