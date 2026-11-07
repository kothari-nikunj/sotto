# Advance meeting preparation

Source-backed preparation for an external meeting 24–72 hours ahead can earn Should Handle
Today when concrete work is worth starting now. A routine nearer meeting stays in Coming Up.
A date alone creates neither urgency nor a saved obligation. There is no new scheduler, model
stage or receipt type; the existing calendar horizon and T−45-minute prep notification remain.

Ordinary brief gathering also supplies older meeting conversation background within its existing
Gmail input envelope. Owner-accepted or owner-organized upcoming meetings with external work
attendees choose one aggregate
30-day search: at most 15 attendee addresses, five results and two exact thread reads in total,
with a shared 15-second best-effort deadline and remaining-time transport timeouts. No new read
starts after that deadline; an in-flight read is bounded per socket operation, so network phases,
authentication or client setup can exceed the aggregate target. Existing exact thread context
is reused when available. The complete added context is capped at 24,000 characters. It remains a
partial sample, with omitted attendee/event counts, capped search, failed reads and deadline state
reported; it never establishes absence. Calendar and Gmail jointly gate reads, projection, prompt
rendering and delivery. Current person/sender mutes are rechecked; changed mute state or a newly
resolved muted attendee discards the cached mixed background rather than parsing its prose. Stale occurrences or removed attendees
cannot reuse their old background. Optional helper exceptions retain the ordinary inbox/calendar
files, and the renderer omits complete rows with a note at its character limit.
Native email rows and evidence IDs are unchanged; the background cannot mint task evidence or
owner authorship. The existing writer and critic may use it to judge useful preparation or choose
none. It is not supplied to public research and adds no model pass.

The renderer resolves a uniquely identified calendar event from the action's exact source ID.
Its authoritative start and title come from that source, never from human-readable meetingTime.
Complete successful calendar coverage must span now through the event. Missing permission,
conflicting owner identity, ambiguous source IDs, invalid times or an ineligible target withhold
only the optional offer. Brief offers retain the existing external work-address attendee policy;
explicit owner-requested prep keeps its wider scope. A full visible source attendee name and complete ask must associate
exactly one action with its event marker or source attendee email; a different event marker is
not overridden. Same-person or same-title meetings do not redirect an exact occurrence. Duplicate titles
remain ambiguous only for the older title-only focus path without exact occurrence proof.

The optional sentence is concise: “I can prepare a briefing for Cedar rollout decision review.”
At most one prep offer appears in a brief, within the existing three-help-offer budget; other
draft/research offers and all task paragraphs retain their existing behavior. The renderer
strips model-supplied internal offer fields and creates metadata only for the sentence it renders.
Multiple actionable inline offers mark the prep record ambiguous, so a generic yes cannot select
it by omission. Other registered questions in the same accepted message remain ambiguous too.

The existing saved brief artifact retains the exact prepared pending-offer record, including its
original expiry. Delivery retries restage that record unchanged. Activation happens only through
the existing accepted-provider effect with its message/route proof, current source checks and
three-hour read-time expiry. The meeting is attached directly to pending_offer; no early-offer
meeting_prep_delivered stamp suppresses the normal imminent notification. Unaccepted delivery
activates nothing. Optional staging failures leave the visible brief deliverable without creating
an accepted offer; diagnostics contain only the exception type. Registration requires the exact
question in both saved Markdown and delivered brief_text. Source-titled suffix cleanup removes only
that exact renderer sentence and preserves following obligations. Existing accepted-offer
confirmations queue the same exact event/start through
the original offer-keyed queue and worker revalidation, with its usual ambiguity and replay rules.

For an explicit contextual prep request, the existing run_sotto_script meeting-prep command can
also carry optional typed meeting {event_id,start,calendar_id?} alongside --focus.
Typed reads preserve the provider event ID and exact calendar ID; gathered offers retain the
existing calendar-scoped internal ID. The worker accepts a raw provider ID only with the exact
calendar scope, or with complete coverage and one unique match. It resolves the existing internal
identity before composition and delivery effects. An unscoped raw ID cannot select through a
partial calendar read. The model resolves the
current subject/calendar; the signed owner request and consent/route checks still govern admission.
The worker independently re-reads and matches the exact occurrence before research or composition;
a missing, moved or duplicate occurrence cannot fall back to another meeting. A supplied person
focus must attend that exact occurrence. An exact occurrence present in a partial calendar read
retains the existing accepted-offer behavior; title-only selection still needs complete coverage. Ordinary brief text
supplies context, not new read authority or an immutable accepted-offer receipt. Explicit named
prep retains its soonest-meeting behavior; explicit prep also retains free owner-organized events.
One shared prep eligibility function rejects cancelled, declined and nonparticipant rows without
applying the stricter unsolicited active-participation policy to explicit prep.

The delivery eligibility gate rejects calendar absence only when a fresh, complete, matching day
projection establishes it. That is not proof that a 54-hour event is still present at send time;
the worker independently re-reads its exact ID/start before paid work. The existing calendar
gather reads the next 72 hours, including the advance occurrence. Prebound offers skip only legacy
occurrence attachment during finalization, not delivery eligibility or source permission checks.

`evals/fixtures/early_prep_day.json` is entirely synthetic. The normal deterministic harness runs
real composition/rendering and continuity with scripted extraction: a substantive review 54 hours
ahead versus a routine check-in one hour away. The routine action omits optional sectionType;
there is no preview enum. The narrative date and source event use the same +54-hour token offset.
Pinned naive replay clocks use the existing UTC interpretation; IANA event offsets are compared
as instants across daylight-saving changes. These tests prove machinery, not model judgment.

An initial actual Gemini 3.8 Flash run correctly surfaced the advance work but failed acceptance:
it honored the existing display-time contract, while the first guard incorrectly expected ISO
meetingTime; its paragraph used an attendee email marker and the routine action was incorrectly
should_handle. Those failures are retained. The source-occurrence correction and consistent
optional sectionType instructions require fresh actual-model and conversation acceptance before
any live claim. Full queued prep, provider delivery and device presentation remain separate checks.

Focused final-tree checks passed: 645 Python tests across brief rendering/evals, calendar/prep,
pending offers, receiver admission, shared worker and saved brief runner; 106 Node tests across
owner skill admission, native tool handoff and command drift. Typecheck, changed-file Ruff and
`git diff --check` also passed. Logs are retained by the local qualification task. These are
synthetic checks and do not replace the full shared gate or new actual-model acceptance.

The later bounded meeting-context change passed 733 scoped Python tests in
`test_brief_meeting_context`, `test_gather_google`, `test_source_context`, `test_loop_context`,
`test_compose_brief`, `test_continuity_evidence` and `test_brief_help_offers`, plus changed-file
Ruff and diff checks. Its 17 synthetic production-path cases cover older exact-thread background,
unrelated participants, aggregate caps/deadline, read failures, grant revocation, existing context
reuse, missing occurrence IDs, stale occurrence/attendee filtering and file-to-delivery source
dependencies. This does not qualify natural model selection or the live brief-to-prep journey.

The subsequent review correction passed 767 scoped Python checks across those files plus
`test_calendar_context`, with changed-file Ruff and diff checks. It adds explicit acceptance and
organizer controls for unsolicited context, mute-change/native-header controls, optional-failure
and receipt checks, complete-row rendering, safe query-address cases, late cached-context reuse
and a native positive evidence control. The cache-wide mute regression checks retained participant
and sender identifiers before event selection, including separate events and changed calendar names,
so an unmuted attendee cannot carry a muted person's thread into composition.
Explicit owner-requested preparation is unchanged.

## October 27 correction evidence

An isolated actual-model conversation exposed the raw-provider/scoped-event mismatch in the
worker. The first run selected the intended meeting but failed before composition. After the
identity correction, the second run completed but semantic review rejected its unsupported
"first contact" claim when private conversation sources were unavailable. The prep prompt now
describes unavailable context without claiming there was no prior exchange.

The third run completed the correct later meeting with Gemini 3.8 Flash, one native composition,
four inspected cards, and a three-question follow-up without new Calendar or public reads.
It took 34.8 seconds for prep and 18.1 seconds for the follow-up in this isolated run. Its
automatic caption-only assertion failed because delivery used media; the original failed report
is retained. Gallery content verification must bind to the accepted artifact and actual media.
These are synthetic source/delivery checks, not current-pilot, phone, natural early-offer,
or all-skills acceptance. The earlier published image is withheld pending a corrected build.

A fourth fresh actual-model run passed the corrected gallery checks and manual inspection of
all four captured transport images. Exact meeting selection, completed job, provider receipt,
ordered image hashes and rendered purpose all agreed. It took 35.6 seconds for prep and 35.5
seconds for the follow-up; the latter missed the conversational target and read the same retained
artifact through three calls. The tool had not explained that limit counts characters or that
query is a literal phrase. Those descriptions are clarified without changing read authority or
limits; acceptance of the resulting latency remains separate.

The related introduction-outcome scenario still fails semantic review: the model inferred unseen
coordination from a BCC move, despite receiving the source scope and current evidence guidance.
The original failed result is retained. Positive delivery-evidence and revoked-source cases passed
manual review, but they do not establish reliability across arbitrary histories.

A fifth run completed the same exact worker and delivered inline text, with a useful contextual
three-question follow-up and no new source reads. Prep took 38.1 seconds and follow-up 8.1 seconds.
The expected entrance detail was missing from the prep, so the original automatic result remains
failed. Target selection and continuity worked; logistical completeness did not. The quicker
follow-up reused inline text and is not a comparable gallery-speed improvement. Review found
conflicting prompt guidance that excluded explicit calendar agenda from meeting purpose; that
contradiction is corrected without mandating a particular phrase or copying the entire description.
The final correction still needs exact-candidate live acceptance.
