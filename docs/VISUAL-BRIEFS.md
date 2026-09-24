# Visual briefs and meeting backgrounds

One shared renderer turns an already composed brief or focused meeting prep into exactly four
1080 × 1920 PNG cards. Cloud and self-host ship identical templates, fonts, and content selection.
The first version quotes complete source paragraphs: no extra model, research, hosted renderer,
or new memory. It does not improve a weak relevance decision by disguising it as a card.

Four attachments form the compact native gallery; fewer can cascade down the chat.
Short updates with too little content stay text, without filler or duplicate images.
The composer rebuilds the calendar preview from its source snapshot after the final model pass;
a model omitting the schedule cannot turn a meeting-filled day into a thin text-only brief.
The five-line preview includes the nearest remaining meetings and a slot for the nearest birthday,
with further birthdays filling spare lines. Calendar dates and birthdays use the brief's local date.
Explicit calendar mutes and source permissions are honored before this final pass.

Morning and evening cards cover needs-you, today, and calendar highlights. Every action, calendar event, and follow-up paragraph in the composed brief
must fit; otherwise the whole brief stays text. Filtered-message tallies remain text-only. Meeting-prep cards quote relationship/founder background, business, signals/context, and question paragraphs. Each card allows up to 200 words, subject to measured readable-height validation; type never shrinks to fit. Briefs include a Follow-ups card with outstanding items before handled items when present. Individual nudges, pending consent offers,
and unsupported formats/channels remain text. There is no arbitrary text truncation or font
shrinking. Layouts that already fit keep their existing section pages. A crowded brief is repacked
at complete paragraph boundaries using the same font and height calculation as rendering. Adjacent
needs-you and today sections can share a labelled card to leave room for follow-ups on two cards;
calendar rows stay together in their own layout. Every selected paragraph and source offset survives.
If the result still cannot form exactly four readable cards, the original text is sent instead.

## Try it without changing scheduled messages

Render a saved composition (JSON with brief_text/prep_markdown, or a text file):

```sh
python3 sotto-chief-of-staff/_shared/scripts/render_cards.py /tmp/example.json \
  --kind brief --preview --out /tmp/sotto-card-preview
```

Use `--kind prep` for meeting prep. The command returns a private manifest path and image paths.
The manifest retains the original plain text and paragraph offsets so excerpts can be checked.
Historical previews are labelled on every card. It never sends or updates the ledger.

## Default iMessage delivery

Galleries are the default for Photon morning/evening briefs and focused meeting backgrounds. Set `SOTTO_VISUAL_BRIEFS=0` to keep all updates as text. Startup applies the
small gallery addition to the pinned Photon sidecar. If its anchor changed or the endpoint is
absent, keep normal text. The capability check is read-only. With support available the existing
receiver sends one native Spectrum group containing the short selectable text summary followed
by the PNG attachments. One parent receipt covers that multipart message; child IDs are recorded
when supplied. This relies on the pinned SDK's native iMessage multipart implementation.

The canonical body remains the outbox identity and validity input. Source validity, expiry,
brief ownership and post-acceptance effects stay in the existing outbox. A send with unknown
acceptance (including a process crash) is held and ultimately expires; it is never blindly
resent. Proven not-attempted/rejected sends can retry. A missing renderer/unsupported layout
falls back before enqueue. The receiver logs content-free fallback reasons (`gallery_unavailable`,
`text_layout`, or a fixed layout reason plus card number), so an intentional short update can be
distinguished from a failed render without recording private prose. The same presentation metadata
rides the existing outbox retry and becomes plain-English detail on the accepted delivery receipt.
Activity says Sent. Its expandable details name four photos or the text fallback reason and
connect the send to recorded prior decisions by identity. Missing or later evidence is never
guessed, and acceptance never claims device display. An ambiguous provider result must not
trigger a second text send.

The shared Photon adapter recognizes the focused meeting-prep output before upstream text truncation and uses the same renderer and native gallery transport. It sends one group, records the returned parent/child IDs, and bypasses upstream retries on unknown acceptance. A private delivery receipt beside the card manifest is written before dispatch; matching gateway retries/restarts reuse accepted receipts or hold uncertain sends. The inbound reply anchor distinguishes a new request; without an anchor, identical cached content is deduplicated. Receipts expire with the seven-day artifact cache. The pinned gateway’s recovery markers are checked against existing manifests and target-scoped receipts before any plain-text resend; uncertain acceptance remains a failure, never a fabricated delivery receipt. Proven unsent media falls back to the original text. Ordinary chat and compact multi-meeting sweeps stay text. Ask Sotto now routes “expand that item” to `brief_detail.py`, a read-only lookup of the original
composed text. Scheduled acceptance receipts retain the opaque gallery ID and owner-target hash
before erasing the payload; interactive prep uses its existing adjacent receipt. Only accepted,
non-preview owner galleries inside seven days qualify. The manifest records the source-permission
fingerprint at render time. Changed or absent permission metadata withholds archived content and
asks for a fresh consented read. Multiple date/topic matches require a choice. Text pages expose
an explicit continuation offset; the response distinguishes composed text from original source
transcripts. No extra model call, store, scheduler or new outward send is involved.

Rendered files live in `$SOTTO_DATA/cache/visual-briefs/<content-id>/` with private permissions.
They are delivery artifacts, not a second knowledge store. The receiver's existing retention
sweep removes files after seven days; `forget.py --caches` removes them immediately. No public
image URLs or additional storage service. The bundled Inter variable font retains its SIL Open Font License. A bundled wax-seal S asset, neutral surfaces, a company heading for prep, consistent section headings for briefs, and timed agenda rows replace report-style chrome. The seal was generated once at build time; no image-generation service runs for a brief.

## Pilot acceptance

1. Replay a historical morning brief and focused prep; label them historical test previews.
2. Verify exactly four images on the owner's actual iPhone and Mac. Confirm
   order, opening/swiping, text notification preview and no duplicate caption/image messages.
3. Read at normal size: no clipped text, no zoom needed, all actionable asks and qualifiers kept.
4. Confirm that replay does not create tasks, mark loops handled, consume today's brief slot,
   or call a model. A tapback is feedback, never authorization to send or close a loop.
5. Test missing capability, rendering failure, rejected send, uncertain send and crash recovery.
6. The owner approved the portrait previews for default delivery. Retain text fallback and the explicit opt-out.

Provider acceptance does not prove that a gallery looked right or reached a device. The live
phone check is a separate gate; offline tests cannot validate Apple's gallery presentation.

Pilot observation: the owner confirmed delivery and readability; Messages on Mac also exposed a single “4 attachments” group. Three images cascaded vertically. All subsequent image briefs therefore require exactly four photos. The final portrait design preserves more complete paragraphs, uses a small, low-opacity seal, and keeps company/person titles above their section subtitles.

Typography preserves explicitly bold leading names from the source, including names without a following colon or dash. Plain-text leading names followed by a separator and labels ending in a colon are also semibold. Names are never inferred from an unlabelled sentence. Unlabelled prep points use hanging bullets; source words and paragraph boundaries remain intact. Brief titles and subtitles use the same positions on every card; prep keeps its company title and section subtitle fixed while swiping, aligned to the white panel’s outer left edge. Body text retains its inset and hanging list/calendar indents. Every body starts 32px below its fixed header; sparse cards no longer float in the middle. Dense content uses the available height. The subtle wax seal lives in the bottom-left footer beside the historical-preview label. Repeated title/subtitle text is omitted.

Calendar cards start directly with their day groups and meetings. Legacy calendar-preview instructions are removed, including colon, dash and parenthesized forms.

Cards and captions use no em dashes; canonical source text remains unchanged. Calendar preview lines may carry ` | Location: <exact event location>`, rendered in smaller text under the meeting title and wrapped at measured word boundaries when needed. The shared composer receives the event location and is instructed to preserve supplied physical locations only. Missing locations remain absent. Wrapped locations consume their measured row height; the complete brief falls back to text only when the resulting page cannot fit at the normal readable sizes.

Readability: one Inter family, four sizes (76px title, 48px body/meeting title, 36px subtitle/section/time/location, 28px date/footer) and two weights (400, 650). Paragraph gaps use 24px where space permits and tighten to no less than 12px based on measured line wrapping. Open-item count lines use the 36px supporting-text size; action paragraphs stay at 48px. Section labels are separated from the preceding paragraphs. The four-card gallery remains unchanged. Content that cannot fit at those minimum gaps still falls back to text. Receiver rendering failures emit a content-free `visual_brief_fallback` diagnostic with the error class. Brief cards carry a fixed time-of-day brief label and the source date. Birthday rows use plain headings such as “Birthdays today” or “Birthdays tomorrow”, including archived parenthesized birthday formats. The decorative cake is not sent to a font that cannot render it. Rows get 44px extra separation after meetings. Text colors meet 4.5:1 against their background; small text never uses the seal’s decorative opacity.

Calendar rows recognize date-prefixed times and either colon or dash separators, as well as all-day events.
Blue time labels, black event titles, explicit day groups, and smaller address lines are preserved across
card splits. Parenthesized street addresses may move to the address line; attendee names do not.
Repeated calendar-preview disclosures are normalized once by the composer and represented by the
card's “Calendar highlights” subtitle, rather than repeated as gray body text.

The gallery caption reads “Good morning. Here's your morning brief for September 16.” (or the
source greeting's afternoon/evening and date). A source-availability warning before the brief's
sections stays in the selectable caption; an oversized caption falls back to text. Genuine web links
remain selectable. Internal dashboard-relative paths such as `/app#loops` are not chat links: new
briefs say “Ask me what's still open,” and archived paths receive the same presentation correction.
The immutable manifest still retains the exact source text. These changes are shared by Cloud and self-host.


After all mandatory sections are composed, spare gallery space can replace the quiet open-loop
count with complete named obligations from the same consented, active ledger input. Due dates
then age determine the order; already named or evening-receipt items are not repeated. Only
identified rows with an explicit person and ask qualify. An already-delivered handoff question
stays in the count until the user answers, even when space remains. The renderer measures each addition
with the delivery layout, without writing files or calling a model. It stops before another item
would exceed four readable cards, preserving all existing content and counting the remainder.
If the original brief cannot form a readable gallery, its ordinary text remains unchanged.
This expansion happens before loop attribution: canonical text, gallery, archive and delivery
receipts carry the same obligations. It does not create a second gallery-only ledger or change
urgency. Named-loop collision checks still require successful delivery.
