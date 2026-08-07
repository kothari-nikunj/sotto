# iMessage with a separate Sotto identity (BlueBubbles) — optional power-user setup

**Status:** documented, not automated, not a priority. Hermes' gateway natively supports iMessage
via [BlueBubbles](https://bluebubbles.app); this walkthrough gives Sotto its **own blue bubble**
(a dedicated Apple ID) instead of the message-to-self thread. Nothing in Sotto's installers touches
this — it is a hand-wired recipe for one machine, yours.

**Read the risk first:** Apple aggressively flags *fresh* Apple IDs that send automated iMessages —
accounts get deactivated with no useful appeal path. Mitigations: use an aged Apple ID if you have
one, warm it up manually (days of normal texting) before wiring it to Hermes, keep volume low
(Sotto's nudges/briefs are naturally low-volume), and never mass-message. If the ID gets flagged
anyway, nothing else in Sotto is affected — delivery falls back to WhatsApp.

## What you're building

```
you (iPhone) ⇄ iMessage ⇄ Apple ⇄ Messages.app (2nd macOS user, Sotto's Apple ID)
                                        ⇅ BlueBubbles server (that same macOS user)
                                        ⇅ tunnel (ngrok / Cloudflare)
                                        ⇅ Hermes gateway on Railway
```

The Bridge is NOT involved — this is a parallel channel. The Bridge keeps reading your own
account's chat.db exactly as before.

## Steps

1. **Dedicated Apple ID.** Create (or reuse) an Apple ID for Sotto — e.g. `sotto.yourname@icloud.com`.
   Give it a phone-number-free iMessage identity (email-based is fine and avoids a SIM). Sign in to
   appleid.apple.com once and complete any verification while it's fresh.
2. **Second macOS user account** on an always-on Mac (a Mac mini is ideal; your main Mac works if it
   stays awake — System Settings → Energy → prevent sleep, or `caffeinate`). Log into that account,
   open **Messages**, sign in with the Sotto Apple ID, enable iMessage. Send a few manual messages
   to yourself over several days (the warm-up).
3. **BlueBubbles server** in that same macOS user: download from bluebubbles.app, grant Full Disk
   Access + Accessibility when prompted, set a strong server password. Skip the "Private API" setup
   (it needs SIP disabled for typing indicators/reactions — not worth it here).
4. **Push/Firebase:** BlueBubbles' setup wizard walks you through creating a free Google Firebase
   project (it uses it for its own notifications/registration). Follow the wizard verbatim.
5. **Tunnel:** BlueBubbles has built-in ngrok/Cloudflare integration in its settings — enable one so
   the server gets a stable public URL. (This is the inbound-connection requirement the Bridge's
   dial-out design avoids; there's no way around it for BlueBubbles.)
6. **Wire Hermes** (on Railway): `hermes gateway setup` → BlueBubbles → paste the tunnel URL + server
   password (see CHANNELS.md). Then allowlist yourself and optionally make it the home channel:
   the BlueBubbles equivalents of the WhatsApp env lines in `adapters/hermes/start.sh:337-371` —
   set the allowed handle to YOUR phone/Apple ID so only you can talk to it.
7. **Keep both macOS users logged in** (fast user switching) — Messages only sends while its user
   session is alive. Add BlueBubbles to that user's Login Items.
8. **Test:** text Sotto's Apple ID from your phone → the reply should come back as its own thread,
   `*Sotto*`-prefixed persona and all. Then flip `SOTTO_CRON_DELIVER=imessage` (or whatever channel
   name `hermes gateway setup` registered) if you want briefs there instead of WhatsApp.

## When to prefer what

| | This (BlueBubbles + 2nd ID) | Bridge self-thread (planned) | WhatsApp (today) |
|---|---|---|---|
| Feels like texting a person | ✓✓ | ✓ (pinned self-thread) | ✓ |
| Setup | hours, fiddly, per-machine | zero (rides the Bridge) | done |
| Uptime | Mac + BlueBubbles + tunnel | Mac awake (WhatsApp fallback) | always (cloud) |
| Ban risk | real (fresh Apple IDs) | none | none |
| Distributable | no — recipe only | yes (future default) | yes |
