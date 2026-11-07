---
name: sotto-morning-brief
description: Use when the user says "good morning", asks for their morning brief / daily brief / "what's on today" / "what needs my attention", when it's morning-brief time, or when the Bridge pushes a morning_ready trigger — produce the user's morning briefing. This is THE way to produce any morning brief — never hand-write a calendar/email summary instead of running this skill.
metadata:
  tags: [brief, chief-of-staff, sotto]
  category: productivity
---

# Sotto — Morning Brief

Produce the user's morning brief: what needs attention, what you've already handled, who's in their world today, and the actions they can take.

> **CRITICAL — do not improvise the brief.** The brief's quality IS the product. On Sotto the brief
> is **receiver-owned**: the receiver gathers the inputs, runs the composer over them and delivers
> **its** output verbatim. Do NOT write your own "Your Day at a Glance" calendar/email summary — a
> brief that is *mostly* a calendar/email recap is a failure. The real brief leads with
> communications (Needs Attention Now / Should Handle Today / Already Handled / Filtered), weaves
> each person's signals across channels, carries tap-to-act items, and includes one **short Coming
> Up** schedule section (≤5 lines: time + title + key attendees). Deliver as **Sotto**, in Sotto's
> voice.

## How this runs on Sotto

- **Scheduled briefs** run on the receiver's morning schedule (and the Mac wake-push), through the
  pipeline below. Nothing in chat starts them.
- **A request in chat** uses `owner_brief_request` with kind morning/evening/auto. Set refresh
  only when the owner requests a fresh or updated brief; otherwise the receiver may reuse a current
  accepted brief. Natural wording is fine. Call the tool, then stop after its receipt: the runtime
  acknowledges the real admission and the existing pipeline delivers the result separately.
  Never claim a brief is queued just because you loaded this guide. If the tool is unavailable,
  say you cannot start it here. Answer ordinary calendar questions with calendar reads instead.
- **Expanding a delivered brief** ("more on Dana in this morning's brief") is `sotto-ask`'s
  `accepted_brief_detail` read — not a new brief.

## The pipeline (receiver-owned — for your understanding, not for you to run)

1. **Gather ALL inputs — Google AND local.** The brief's value is *marrying* the two; a local-only
   brief is a failure when Google is connected. Gmail (24h, plus a small sent lane that teaches the
   user's email voice) and Calendar (3 days) are read deterministically; local messages come from
   the Bridge (`read_local`, 24h), falling back to the **last cached snapshot** (flagged stale) when
   the Bridge is unreachable; Granola contributes ~14 days of notes plus transcripts for meetings
   that ended in the last ~36h ("when you met last week you discussed X"). Prior knowledge packs
   **everyone in today's inputs** — on the calendar, in current threads, in an email you got or
   sent, or attached to an active commitment — plus the identity map and the **company knowledge**
   for today's attendee domains and the packed people's employers.
2. **Research attendees — ONE batched, grounded pass** (ported from the Mac's `gemini-research.ts`):
   external attendees worth researching (excludes you, same-domain colleagues, known contacts;
   capped at 25), skipping anyone the graph researched in the last 30 days (the same filter
   `sotto-meeting-prep` uses, so the two share one cache). It batches 5 attendees per grounded call,
   grounded in real web results (`references/research-prompt.md` documents the original ported
   prompt), and persists what it found as clearly-sourced, LOW-confidence "Per web search: …"
   facts. **One rule, global: an external attendee is researched at most once per 30 days, and
   every research run feeds the graph.** A bounded, read-only X lane adds exact-lookup identity
   only: WRITING an X identity down needs their full name plus the profile agreeing about their
   company/domain; a handle a page published for them rides ONE prep marked UNCONFIRMED; public
   Post/bookmark text stays temporary.
3. **Extract — this step IS the brief.** First the resolver closes what's already closed (what
   today's data shows was delivered or answered, and what aged out — it records nothing new), so
   the brief reasons about today's open loops, not last night's. Then the composer runs the real
   extraction prompt (`references/extraction-prompt.md`) over the gathered sources and a
   **second-pass critic** (a port of the Mac's brief-critic, `SOTTO_CRITIC=auto|always|off`; quiet
   days below the size/action thresholds skip it) that checks the draft against a data manifest for
   missed threads, attribution errors, mis-prioritization, wrong "Already Handled", action coverage
   and weak cross-channel synthesis, then **revises** the brief. Only a completed review stamps the
   `criticReceipt` that lets a proposed closure apply. It produces `brief_markdown` (records and
   continuity only — it carries `<!--…-->` markers), `brief_text` (the chat-ready version — markers
   stripped), `actions[]`, `loop_updates[]`, `meetings_needing_prep[]` and `extracted_knowledge`.
   - **Tells that a brief was improvised — all failures:** the WHOLE brief reading as a
     "Your Day"/calendar agenda (a *short* Coming Up is fine, a meeting-by-meeting agenda is not);
     deep links like `sms:arnav_sahu` or `sms:group_…` (the composer emits `sms:+<digits>`,
     `https://wa.me/…`, `mailto:`, `tel:` — and **never** a link for a group chat); WhatsApp
     contacts shown with `sms:` links; the user's own name listed as someone to reply to; names or
     attributions inferred rather than read from the data. A hand-written brief is worse than no
     brief.
4. **Learn — ONE step runs all seven memory writers** (knowledge, continuity, draft outcomes, style,
   meeting commitments, meeting attendance, contacts) and leaves a receipt; a brief that delivers without it is logged as
   a brief that did not learn.
   - **Knowledge** — writes/updates the people and company files. Person files that share an exact
     identifier (the same email or phone) are one human and merge automatically; files that merely
     have *similar names* are never merged, only suggested for a human to confirm; a company that
     matches an existing file's alias or domain resolves INTO that file. **Never merge person files
     by hand**, and never merge two people because their names look alike.
   - **Continuity** — records distinct obligations and applies the brief's `loop_updates`. Each
     update must name its exact `loopId` and `loopVersion`, explain the specific obligation change
     in `reason`, cite a completion or promise message's source ID and verbatim snippet, and match
     the counterpart, direction and original timestamp. A generic reply, Already Handled name,
     unrelated meeting, or old deadline cannot close an obligation. Ambiguous evidence leaves work
     open; explicit-resolution commitments require the user's correction. Presentation groups one
     person in prose while the ledger keeps each ask separate. Unsupported/no-counterpart/no-reply
     actions and calendar shadows are refused with a logged reason.
   - **Draft outcomes** — matches offered drafts against observable sends, logs outcomes, and
     confirms verbatim voice samples. It does not infer permission or rewrite preferences.
     Historical `dismissed` means unmatched in an active lane, not explicit rejection.
   - **Writing voice** — accumulates the user's style fingerprint from today's sent messages and
     sent email into `style.json` (caps/TTL make repeat runs safe), so drafts sound like the user.
   - **Meeting attendance** — one dated, sourced `Met on <date>: <title>` fact on each attendee of
     every meeting with 8 or fewer attendees (a bigger event is not a relationship); a blank
     `company` fills from a corporate email domain.
   - **Contacts** — re-syncs identity, notes and birthdays from the snapshot the brief already
     saved; only cards Sotto already knows or that carry a note/birthday sync.
5. **Deliver — deliver-once, then send** as **Sotto**.
   - **Deliver-once (schedule ↔ wake-push coordination):** the receiver claims the day's brief
     marker at its send seam, so the schedule, the wake-push and the dashboard's button deliver
     exactly one brief; a second copy is receipted `superseded` and never sent.
   - It sends **`brief_text`**, never `brief_markdown`, never a re-formatting of either.
   - **Tap-to-act DOES work on chat.** Each action carries a **`tap_link`** — a real tappable URL (`https://wa.me/…`, `mailto:…`, `tel:…`, `sms:…`, or the meeting link). For the top 2–3 actions the link rides the person's name or a short tappable line — e.g. "→ [Message Dhruv](https://wa.me/15551234567)". The `tap_link` is used **verbatim**; no URL is invented.
     - **Email is the exception: offer wording rather than a `mailto:` link.** A percent-encoded URL is most of the line, and on a phone it opens whatever mail app the OS picks. The brief can say "I can draft a reply" beside a grounded email item. If the owner asks, show the wording in chat; saving it in Gmail requires a separate explicit request and full draft review, followed by the owner's approval.
   - The composer pairs up to three grounded items with an inline **conversational offer** for drafting, research or prep. A draft offer belongs to an actual message the owner could review, not an owner-only task such as coding expenses. The offer sits beside its item in both text and photos; do not add another offer list. Thin context is not a reason to invent a third task. Drafts remain for owner review.
   - A valid model-selected calendar prep is the brief’s **single help offer**, phrased as one question beside its exact meeting. Other tasks remain visible without competing draft/research offers, so a bare yes can use the existing accepted-delivery receipt. Unsupported prep leaves ordinary grounded offers available; it never hides a task or invents preparation.
   - **Do not append another offer line.** No extra sections, recaps, separators, or accountability/status blocks after the brief — the brief already contains everything (the evening's accountability lives INSIDE it as Already Handled / Still Pending, and the composer appends the evening-only **What moved today** receipts itself, plus — at most once per published version, morning or evening — a single housekeeping line saying a newer Sotto is available). One brief, with its assistance offers inline, is the whole message.
   - If the brief came out **thin or the user pushes back on it**, ONE recovery sentence carries its own next move: "If anything here looks off: say *that's wrong about X* and I'll fix my memory, *stop surfacing newsletters* to mute a source, or *clean up stale loops*."

## Notes
- First brief (empty graph): the local window widens to 7 days (`read_local(since_hours=168)`); everything else is identical — the composer takes no window, it composes whatever was gathered. Day 2+: the default (24h) is the delta.
- **The open-items contract:** an open loop earns its own line ONLY when it is **overdue, due within
  24 hours, or already chased without an answer** (`brief_validate.is_urgent` — deterministic, read
  off the ledger row, never model-judged). Every urgent one appears exactly once — as an ask carrying
  its age, or in Already Handled when today's data shows it resolved. Every OTHER open loop is
  represented by ONE quiet line with a count and “Ask me what's still open” and is worked by the
  proactive nudges, not by the brief. The extraction prompt states it, `brief_validate` measures it,
  and `compose_brief` appends the urgent misses (one line per PERSON, however many debts they carry)
  plus that single count line. On a genuinely quiet day — nothing to handle AND no open loops — the
  brief says so in one line rather than just omitting the sections.

### Evidence consistency for loop reasoning

Stored open tasks are prior bookkeeping, not proof that nothing changed. Reconcile the original
ask against the supplied timeline; a new promise and a completed deliverable are different, as
are two obligations involving the same person. The model judges that distinction with source
evidence. Code validates identity, direction, time, quotation and the exact ledger revision; it
does not decide fulfillment with an English keyword checklist.

The receiver's runner shares one effective local snapshot across composition and Learn, preserving
cached observation times and current source consent, so cited fallback evidence is available to the
writer too. Its bounded Gmail loop-context refresh reads original threads or verified counterpart
addresses without another model pass. A partial refresh is not proof of absence. Cached timestamps
are never replaced with the time the cache was read.
