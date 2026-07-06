---
name: sotto-draft-reply
description: Use when the user wants Sotto to reply to / follow up on / message someone — drafting an email, iMessage, SMS, or WhatsApp in their voice, then handing them a one-tap deep link to send.
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
2. **Voice** — `execute_code` → `_shared/scripts/style_apply.py '{"recipient","channel","canonical_id"}'` → returns the user's voice guidance from `style.json`: **verbatim sample messages** (how they actually write — to this person first, then to this context bucket) plus voice guardrails (capitalization, exclamation habit, typical openers/closings). **Study the quoted samples and match their voice, length, and punctuation exactly** — they are the ground truth, not an abstract description.
3. **Verify (gate)** — before presenting, self-check: correct recipient? no fabricated facts/commitments? right channel? Fix or flag. (PORT: claude-flex.ts draft verification.)
4. **Build the send link** — `execute_code` → `_shared/scripts/action_links.py '{"channel","identifier","message","subject"}'` → returns a deep link (`imessage://` / `sms:` / `https://wa.me/…` / `mailto:`). The draft + recipient are encoded into the URL.
5. **Present in chat** as: the draft text + the tappable link, e.g.
   *"Reply to Sarah: '…'. Tap to send: <link>"* — on a gateway that supports buttons (Telegram), render it as a **"Send to Sarah" button**.
   The user taps it **on their phone** → Messages/WhatsApp/Mail opens with the draft prefilled → they hit send. (The deep link works on every channel and needs no Mac.)
6. **True send (email + calendar only — cloud-side, no Mac).** For an **email** reply, after the user approves you can send it directly instead of relying on the tap:
   `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/google_action.py" gmail-reply --message-id <gmail message id> --body "<draft>"` (reply within the thread), or `gmail-send --to <addr> --subject "<subj>" --body "<draft>"` for a fresh email. It returns `{status:"sent", id, threadId}`.
   - **iMessage/SMS:** if the Bridge advertises `send_message` (the user enabled "Let Sotto send" in the app) AND it's connected, you can send directly: call the `sotto-local` **`send_message`** tool `{channel:"imessage"|"sms"|"auto", to:<phone/email>, body:<draft>}` → `{status:"sent"}`. If it's not enabled, offline, or returns an error, **fall back to the deep link** (`sms:&body=`). The Mac must be awake.
   - **WhatsApp:** deep link only (`wa.me?text=`) — the gateway is reply-only, so there's no cloud/Bridge send.
7. **Tiers** (`sotto-approval-tiers`): NEVER send without an explicit go-ahead. `auto` → the deep link is the one-tap, or send email directly on approval. `review` → show the full draft, let the user edit, THEN send/build the link. `forbidden` → draft only, no link, no send.
8. **Record** — `_shared/scripts/log_outcome.py` (`draft_created` now; `executed`/`edited_and_sent` when the user confirms or you send directly) so preferences + continuity update.

If the user doesn't act, the item ages into the continuity queue (never left as a stranded draft).
