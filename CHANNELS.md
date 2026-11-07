# Choosing your channel and model

Sotto delivers to one owner channel per installation and runs its model calls on Gemini. This page
says what each channel needs, how complete it is today, and what the model choice actually covers.
It is written for a human *or* an agent (Codex, Claude Code) setting this repo up; the install
steps themselves are in [ONBOARDING.md](ONBOARDING.md#agent-guided-setup).

**Your channel** — where the brief lands and where you chat with Sotto:

- **iMessage through Photon** — *the supported channel.* Cloud delivery using your own Photon
  Spectrum Cloud project and line. Native Bridge setup configures it from your protected
  input; your Mac does not have to stay awake for delivery. [Details below](#photon-imessage).
- **Telegram** — *a limited self-host profile.* A linked owner bot with the core owner tasks,
  reminders and routines; several attended features remain Photon-only. Native Bridge setup captures the owner before provisioning; requested briefs and accepted-brief
  readback are supported, while several other attended tools remain deferred. [Details below](#telegram-limited-self-host-profile).

WhatsApp, Signal, Discord, Slack and BlueBubbles are not delivery channels. (WhatsApp on your Mac
is still a *source*: the Bridge can read it locally, like Messages.)

**Your model** — the full study is [docs/MODELS.md](docs/MODELS.md):

- **Gemini** is what the supervised runtime calls: one direct Gemini key from your setup config
  covers chat, briefs, triage and research. The chat model and the media model are the
  `chat_model_id` and `native_media_model_id` values in that config; the runtime accepts only the
  `google` provider.
- **Research** uses Gemini grounding. The Exa, Parallel and Browser Use keys the research code can
  read are not passed by the supervised launch ([RAILWAY.md](RAILWAY.md#environment-variables--full-reference)).

Replies default to drafts or links you send yourself. An explicit approved send requires a
configured sending capability; Bridge sends additionally require `--allow-send`. The channel
that delivers your brief does not grant permission to message another person.
Without Google connected, email falls back to a `mailto:` link like everything else.

## The honest status of each channel

| Channel | What you supply | Delivery while your Mac sleeps | Photo briefs | Setup |
|---|---|---|---|---|
| iMessage / Photon | Your own working Photon project and line, its credentials, and the exact owner phone | Yes | Four photos when the provider and layout support it | Native Bridge (qualified release required) |
| Telegram | A BotFather token and a one-time pairing message from the owner | Yes | Text | Native Bridge private pairing (qualified release required) |

The channel is fixed by the installation's launch input for the life of its volume: a different
channel, owner or bot is a fresh installation, not a setting. The Sotto Bridge is an optional
source reader, not a messaging provider.

## Photon iMessage

You need your **own Photon project and working iMessage line**, including the number people message
to reach it. Creating a Railway service or installing the Mac Bridge does not allocate that line.
Check provider access and pricing before starting; no Photon account or number is included with
this repository.

1. Sign in at [Photon](https://app.photon.codes/), prepare a Spectrum Cloud project, register your
   iMessage line, and note the assigned line number plus the project ID and secret. If your phone
   already uses another Photon project, confirm separate routing with Photon before registering it
   again: separate credentials alone do not prove that an existing shared-line connection will keep
   working.
2. Enter those values in Bridge’s iMessage setup: Photon project ID and secret, Sotto line
   (the physical line) and owner phone (your own number, `+` and country code). The owner phone identifies **you**, the person receiving
   briefs and sending requests; it is not Sotto's line. Choose that registered phone number in
   Messages' **Start new conversations from** setting — an Apple ID email never activates
   self-host. Then create the server in Bridge ([setup guide](ONBOARDING.md)).
3. From the owner phone, send a hello to the Sotto line. This first inbound message activates the
   owner route (it also lets a shared Photon line reply to you; Sotto cannot initiate that
   conversation by itself). Confirm a reply arrives. A configured channel or a healthy HTTP
   endpoint alone does not prove messaging works; check the project, line and exact sender
   identity if it stays silent. The activation receipt is bound to the Photon project, so changing
   projects needs a new owner hello.
4. Open the private setup link and connect Google, selected Mac sources or Granola
   ([ONBOARDING.md](ONBOARDING.md)). If adding the Mac Bridge, choose **Use my own server instead**,
   even though your delivery channel is iMessage.
5. Wait for the automatic first brief and confirm it appears in Messages. Eligible briefs use
   four photos; an ineligible layout falls back to text. Check the dashboard Record for send
   status and any presentation fallback. Reopening setup does not send another welcome brief.

Protect the private setup link and provider secret. Sotto admits only the owner's direct messages;
group messages and other senders are ignored. Drafting a reply does not authorize sending it to another person.

The Mac Bridge supplies local context independently of delivery. While your Mac sleeps, Photon
can still deliver using server-side sources; local context may become stale. Updating the Bridge
does not provision or change your delivery provider.

## Telegram (limited self-host profile)

The supervised runtime can run a linked Telegram owner bot with the same five scheduled procedures,
`/loop`, `/reminder` and `/quiet` commands, private tasks and routines. Linking captures your
numeric id from a `/start <phrase>` message carrying a random pairing phrase only you can read, so
a stranger who finds the bot's username cannot claim it. Bridge saves the candidate before
acknowledging the Telegram update and stops polling before the runtime starts. Long messages use
bounded entity-aware chunks with partial-delivery holds. Accepted-brief readback and fresh requested
briefs use protected owner/bot/source authority throughout admission, work, feedback and delivery.
Attended meeting prep, owner skill requests and offered replies remain separate parity work. The setup steps, file templates and every limit are
in [adapters/sotto/SELFHOST.md](adapters/sotto/SELFHOST.md#telegram-owner-bot); the evidence for
each claim is the [behavior inventory](docs/BEHAVIOR-INVENTORY.json).

## Switching the model

- **Chat and media** use the Gemini model ids in your setup config. The runtime accepts only the
  `google` provider, so there is no other chat family to switch to.
- **Briefs, prep, follow-ups and triage** use the shared pipeline's Gemini defaults. The pipeline
  code can compose with OpenAI or Anthropic models (`SOTTO_BRIEF_MODEL`, limits in
  [docs/MODELS.md](docs/MODELS.md)), but the supervised launch passes no key for those families,
  so a supervised installation composes on Gemini.
- **Background history learning** has a separate proxy/spend requirement, described in
  [RAILWAY.md](RAILWAY.md#background-history-learning-on-self-host).

The full call-site map, measured prompt sizes and a five-model comparison:
[docs/MODELS.md](docs/MODELS.md).
