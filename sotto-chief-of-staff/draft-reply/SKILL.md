---
name: sotto-draft-reply
description: Use when the user wants Sotto to reply to / follow up on / message someone — drafting an email, iMessage, SMS, or WhatsApp in their voice, then (on their yes) saving an email into their Gmail drafts, or handing them a one-tap deep link for every other channel.
metadata:
  hermes:
    tags: [chief-of-staff, sotto, drafting]
    category: productivity
    requires_toolsets: [sotto-local, google-workspace]
    requires_tools: [execute_code]
---

# Sotto — Draft a Reply (+ one-tap send link)

## Procedure
1. **Context** — pull the thread: Gmail (native) for email, or Bridge `get_messages(identifier)` for iMessage/SMS/WhatsApp. Pull the person's facts via `knowledge_query.py`.
2. **Voice** — `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/style_apply.py" '{"recipient": "<name>", "channel": "<imessage|whatsapp|email>", "canonical_id": "<phone/email if known>"}'` → returns the user's voice guidance from `style.json`: **verbatim sample messages** (how they actually write — to this person first, then to this context bucket) plus voice guardrails (capitalization, exclamation habit, typical openers/closings). **Study the quoted samples and match their voice, length, and punctuation exactly** — they are the ground truth, not an abstract description.
3. **Verify (gate)** — before presenting, self-check: correct recipient? no fabricated facts/commitments? right channel? Fix or flag. (PORT: claude-flex.ts draft verification.)
4. **THE RULE — email asks, every other channel links.** For an **email** draft, show the text and *ask* ("want this in your Gmail drafts?"); for iMessage/SMS/WhatsApp/call, and for email on a host where Google isn't connected, build the deep link as before.
   - **Email (the default now):** do NOT build a `mailto:` — a 300-character percent-encoded URL is most of the message, and on a phone it opens whatever mail app the OS picks. Present the draft text and one plain question. On the user's **yes**, `execute_code` →
     ```bash
     python3 "$HOME/.hermes/skills/sotto/_shared/scripts/google_action.py" gmail-draft \
       --to <addr> --body "<draft>" [--subject "<subj>"] [--thread-id <gmail thread id>]
     ```
     It returns `{status:"drafted", draft_id, thread_id, threaded, reply_headers}` — confirm in **one line**: *"Drafted in Gmail — it's in your drafts, ready to send."* Nothing is sent; the draft is reviewable and sendable from any device, which is what a `mailto:` never was.
     - **Pass `--thread-id` whenever a thread is known** (a brief action's `emailThreadId`, a ledger loop's `thread_id` from `loops_query.py`, the `threadId` on the email you're replying to). A reply that starts a NEW thread is worse than a link; with the thread id it lands in the thread, `Re:` subject and `In-Reply-To` included. No thread id → it is a fresh email, so say so if the user thinks they're replying.
     - **If it returns `{status:"error", fallback:"deep_link"}`** (Google not connected on this host, or the API refused), fall back to the `mailto:` below and say plainly that Gmail isn't connected. Never show the raw error.
   - **Every other channel:** `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/action_links.py" '{"channel": "<imessage|sms|whatsapp|email>", "identifier": "<phone/email>", "message": "<draft>", "subject": "<subj, email only>", "action_type": "reply"}'` → a deep link (`imessage://` / `sms:` / `https://wa.me/…` / `mailto:`). The draft + recipient are encoded into the URL. `action_type` is optional but say which draft this is (`"reply"`, `"decline"`, …) — it is how two drafts offered for one thread stay tellable apart later.
5. **Present in chat** as: the draft text, then the ask (email) or the tappable link (everything else), e.g.
   *"Reply to Sarah: '…'. Want this in your Gmail drafts?"* / *"Reply to Sarah: '…'. Tap to send: <link>"* — on a gateway that supports buttons (Telegram), render the link as a **"Send to Sarah" button**.
   For a link, the user taps it **on their phone** → Messages/WhatsApp opens with the draft prefilled → they hit send. (The deep link works on every channel and needs no Mac.)
6. **True send (email + calendar only — cloud-side, no Mac).** A Gmail draft is the default for email; a real send is a *further* step and needs its own explicit go-ahead. When the user says send it rather than draft it:
   `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/google_action.py" gmail-reply --message-id <gmail message id> --body "<draft>"` (reply within the thread), or `gmail-send --to <addr> --subject "<subj>" --body "<draft>"` for a fresh email. It returns `{status:"sent", id, threadId}`.
   - **A send happens in the attended chat lane only — and that is enforced in code.** The user's yes, in this conversation, is the gate. A scheduled brief, a proactive tick or an event-triage run has no such yes, so `google_action.py` **refuses** `gmail-send` and `gmail-reply` outright in those runs (the receiver sets `SOTTO_UNATTENDED` in the environment of every skill it spawns): you get `{status:"error", error:"refused: unattended run …", fallback:"gmail-draft"}` and a non-zero exit, before anything reaches Google. If you ever see that refusal, **do not retry it and do not work around it** — offer `gmail-draft` instead (it stays allowed unattended, because a draft sits in the user's own drafts folder), or leave the item for the next conversation. Every send/reply attempt, allowed or refused, is recorded as one metadata line in `$SOTTO_DATA/events/sends.jsonl` — recipient and result, never the subject or the body.
   - **iMessage/SMS:** if the Bridge advertises `send_message` (the user enabled "Let Sotto send" in the app) AND it's connected, you can send directly: call the `sotto-local` **`send_message`** tool `{channel:"imessage"|"sms"|"auto", to:<phone/email>, body:<draft>}` → `{status:"sent"}`. If it's not enabled, offline, or returns an error, **fall back to the deep link** (`sms:&body=`). The Mac must be awake.
   - **WhatsApp:** deep link only (`wa.me?text=`) — the gateway is reply-only, so there's no cloud/Bridge send.
7. **Tiers** (`_shared/references/approval-tiers.md`): NEVER send without an explicit go-ahead, and never create the Gmail draft before the user answers the ask. `auto` → the deep link is the one-tap. `review` (email, and every decline) → show the full draft, let the user edit, THEN create the Gmail draft / build the link. `forbidden` → draft text only, no link, no Gmail draft, no send. A **decline** (below) is `review`, always.
8. **Record** — `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/log_outcome.py" '{"outcome": "draft_created", "contact": "<name>", "action_type": "reply", "tier": "auto"}'` (one positional JSON blob — `outcome` becomes `executed`/`edited_and_sent` when the user confirms or you send directly; `action_type: "decline"` for a decline, always with `tier: "review"`) so preferences + continuity update.

## The no-draft — when the ask is declinable, draft BOTH

**The no arrives already written — you still send it.** Every draft path assumes yes; a chief of
staff doesn't.

When the message is a request the user could plausibly decline — **time** (a call, coffee, a panel,
a speaking slot, "got 30 min?"), **money**, an **intro**, a **favor**, a review — and the user
hasn't already signaled they want it, produce **two labeled drafts, not one**: `Accept:` first,
`Decline:` second. Write the decline in the **decline register** in `_shared/references/voice.md`
(warm, direct, ≤2 sentences, no fake busyness, no apology spiral, no fake-open door) and in the
user's voice from step 2, exactly as carefully as the accept.

- **Decline leads** when the ask is clearly low-value: the event bundle's Tier-1 class says so, the
  sender is cold/unknown, it's templated outreach, or `preferences.json` shows the user
  deprioritizing this contact or action type. Same two drafts, decline first.
- **One draft only** when "no" isn't a coherent answer — a colleague's factual question, a thread
  the user already committed to, a reply that isn't answering a request. Never manufacture a
  decline just to have two.
- **Never editorialize the choice.** Present both flat; the user picks. No "I'd suggest declining".
- **A decline is `review` tier forever**: show the full text, let them
  edit, get an explicit go-ahead — and build the send link (or the Gmail draft) only for the draft
  they pick. Never pre-link a decline, never pre-draft one into Gmail, never send one directly, and
  no learned preference can ever relax it.
- **Type it when you log it** — `log_outcome.py` with `action_type: "decline"`. That token is what
  the never-relax guard keys on; a decline logged as a plain `reply` is invisible to it.
- **Type it when you offer it, too** — run `action_links.py` for the decline with
  `"action_type": "decline"` alongside the accept's own `"action_type": "reply"`. It records the
  decline you presented and returns an **empty url** by design (the never-pre-link rule is enforced
  in code, not just here), so the Record holds the evidence that a no was offered and the two drafts
  for one thread stay tellable apart. Present the decline's text, not a link.

## Group chats — no deep link, ever
A group thread (iMessage/WhatsApp group) has **no linkable identifier** — triage/brief items carry
`is_group_chat: true` with `identifier: ""`. Never build a send link for a group and never invent or
substitute an address (a member's number is NOT the group). If asked to reply to a group: draft the
text and present it for the user to send **in the app themselves** (or mark the thread handled).
When drafting from a group thread, per-sender attribution lines ("Name: message") are the ground
truth — attribute quotes only to the named sender.

Deliver as **Sotto**, never "Hermes Agent".

If the user doesn't act, the item ages into the continuity queue (never left as a stranded draft).
