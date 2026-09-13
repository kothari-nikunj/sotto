# Managed Cloud pilot

This is an engineering pilot, not a cohort-ready Cloud product. Self-host remains the default. Both tiers run the same Hermes source pin; the managed mode uses Photon iMessage and keeps the native Gemini pipeline unchanged.

## Implemented contracts

- The Docker build passes `adapters/hermes/hermes.commit` to the vendored installer, then verifies it. Managed boot checks the same pin. Installer refresh alone does not upgrade Hermes.
- `managed_config.py` reconciles the local Photon plugin, the owner's E.164 allowlist, main/auxiliary model configuration, and stale credentials. The tenant process does not receive the upstream Google model key. Explicit `terminal.env_passthrough` carries Sotto state and the tenant proxy pair into the code kernel; it excludes Bridge/setup roots.
- One tenant proxy URL/token pair serves two paths: native Gemini for compose, triage, research and DocSend; OpenAI-compatible chat for the Hermes agent. Native JSON schemas, grounding fields and image payloads are passed through unchanged. Missing managed credentials fail closed.
- The proxy authenticates expiring tenant tokens, fixes the allowed upstream paths/models, reserves admission allowance in SQLite before every call, and records content-free usage. Native thinking tokens count toward output usage. Multiple candidates and shared provider cache references are refused.
- Owner DMs activate the managed channel. Groups, strangers and unknown space types are rejected before the model; adapter sends can address only the owner or the recorded owner DM. This adapter policy is not an OS sandbox against an agent with shell access.
- Managed scheduled/wake-triggered briefs wait for activation and a consented connected source. Zero-source tenants get one nudge-kind status notice through the outbox. Sleeping Macs and zero event counts do not revoke established capabilities.
- Managed scheduled and wake-triggered briefs execute `brief_runner.py` directly: isolated input files, Google/local gathers, cached attendee research, continuity resolution and composer. A composer failure withholds delivery; once a valid artifact is staged, essential and ancillary learning continue durably after delivery. Hermes remains the interactive model/tool loop and outbound transport.
- Calendar gathering uses the existing Google credential to preserve attendee identities, RSVP status and the complete paginated event window. The Hermes CLI projection drops attendees and stops at 25 events; it remains only the fallback for hosts without that credential file.
- The shared composer rejects missing, non-text or empty extraction output and retries once with the full original inputs. A second invalid output fails the run before critic repair, post-compose learning and delivery. Repairs of valid drafts receive the original source context, so omitted calendar and conversation evidence can be recovered. Short quiet-day briefs remain valid.
- Managed outbox acceptance requires `hermes send --json` success with a provider message ID and no skipped flag. Acceptance is not confirmation that a device received the message.

## Live findings

The selected Hermes commit is `245e48008fa814b3251f50755eb656bd9fb86cb1`, matching the existing deployed runtime. Spectrum is pinned to 12.7.0 by its lockfile. Isolated pilot tests verified the native structured-output route, the chat route and a no-recipient Photon upstream probe.

The stock probe discards Photon's server-side GUID validation response and therefore cannot establish liveness. This is upstream [issue 101618](https://github.com/NousResearch/hermes-agent/issues/101618), with an open [proposed fix](https://github.com/NousResearch/hermes-agent/pull/101624). `photon_probe_compat.py` applies a narrower, hash-checked adaptation: the exact observed validation message plus gRPC status 3 proves a server round trip. Auth, transport and other failures stay inconclusive. Re-review/remove this adaptation when changing the pin. The owner has confirmed receiving the first chat reply and one-time source notice; Railway records the matching inbound and send path.

## Operator configuration

The instance requires `SOTTO_DEPLOYMENT_MODE=managed`, a stable `SOTTO_TENANT_ID`, `SOTTO_MODEL_PROXY_URL`, `SOTTO_MODEL_PROXY_TOKEN`, and the Photon project/owner settings listed in the environment template. The proxy is a separate service built from `cloud/model-proxy/`; only it holds `GOOGLE_AI_API_KEY`. `SOTTO_PROXY_TENANTS` contains tenant IDs, token SHA-256 hashes, enabled flags, expiry epochs and admission budgets in cents. Give each service its own persistent `/data` volume.

The proxy records conservative admission reservations, not invoice costs. Numeric budgets enforce a cutoff with an explicit HTTP 402 Sotto budget error; an explicit null budget disables that cutoff while retaining metering. Each tenant is also limited to 60 admitted requests in a rolling 60-second window, atomically counted in the same ledger. Parsed requests reject duplicate JSON keys, and the proxy forwards a canonical JSON encoding of the validated object. Real upstream HTTP 429 remains a provider rate limit. At the owner's request, this personal pilot is uncapped. Its credential has a 30-day lease; the receiver renews it initially and within 72 hours of expiry, with hourly retry after failure.

## Remaining release gates

The owner-restricted account broker now supports Google web OAuth, encrypted temporary credential handoff and device-bound Bridge pairing into the existing tenant. The native start returns no confirmation code. After Google succeeds, the browser callback displays an eight-character code; the user enters it in Bridge, which submits it with its polling bearer. Five attempts fit in the 15-minute session, and no raw Google authorization URL is returned to the Mac. The same signed Bridge app offers Cloud and self-host and stores connection credentials in Keychain. Source toggles and authenticated capability reporting support all 11 local readers; fresh users must explicitly select sources. The assigned Sotto identity is excluded from iMessage, phone/FaceTime and WhatsApp reads, and Bridge sending remains disabled in managed mode. Destination changes clear the old credential before removing its mode-0600 policy.

Anonymous Mac starts use a 50-row FIFO of unverified pending sessions. The broker caps all sign-in sessions at 500 and evicts only anonymous pending rows, preserving device-confirmed, provisioning, ready and browser-authenticated work. These are capacity bounds, not DDoS protection. Device-signed pairing redemption intentionally remains exactly retryable for ten minutes after a lost response; revocation prevents a revoked grant from being replayed.

Before a cohort: verify the implemented runtime isolation and credential separation in the actual
built Linux image, complete all-chunk delivery receipts, concurrency/restart and ambiguous-send
tests, source exclusion/retention, deletion/suspension/recovery, accurate metering and budget
enforcement, corpus parity, and the operator/Photon commercial requirements. The dashboard exchange
and iCloud mirror remain post-cohort conveniences. Do not infer launch readiness from a successful
health probe. Receiver and gateway are designed to run as the same unprivileged `sotto` UID; the
receiver is nondumpable, removes the control credential from the process environment, and resolves
managed imports from root-owned application code. This protects that credential and code boundary,
but same-UID tenant data access and denial of service remain possible: it is an availability and
credential boundary, not an OS sandbox.

Managed system jobs all use the ordinary `runner: receiver` field. The weekly relationship pulse runs at 9 a.m. Monday in the user’s timezone. All managed scheduled jobs wait for messaging activation and a connected context source, then share the receiver’s outbox and silence handling.

## Personal instance first

The pilot targets one owner; automatic multi-user provisioning is deferred. Gmail reading/drafts and Calendar reading use actual OAuth grants. Granola has its own owner-approved connection. All 11 Mac readers are enabled for this owner's comparison with Telegram and verified through the Cloud-to-Mac relay. Telegram knowledge, explicit preferences, writing style and USER memory were imported while preserving newer Cloud records. The Sotto persona, Gemini 3.8 Flash model and agent reasoning/turn settings match the existing Telegram instance. Original Telegram production was only read for the comparison.

Morning and evening briefs, the midday digest, proactive checks and the weekly relationship pulse route to iMessage. Plain-text formatting and native processing Tapbacks adapt the same experience to Messages.

Live verification: the owner received a conversational reply and the one-time no-source status. Railway recorded the inbound Photon message, one model call, the response send and an outbox row delivered on its first attempt. The inbound provider ID is persisted; the chat response has no outbound provider ID in Hermes session storage, so device receipt is confirmed by the owner. Three idle watchdog reconnects recovered successfully before the first inbound; long idle stability remains under observation.

The Google CLI decoder recognizes the exact `No messages found.` sentinel only for Gmail searches; other invalid output stays a source error.


### Photon idle-connection correction (September 7)

The pinned sidecar's silence watchdog classifies a quiet inbound stream plus a successful
independent unary read as a dead subscription. A personal line with no messages meets that
condition every ten minutes. The probe compatibility patch made the unary response correctly
count as alive, exposing this false inference; it did not prove that the subscription was dead.

Photon startup now fixes `PHOTON_STREAM_SILENCE_PROBE_MS=0` in both modes, and reconciliation removes a stale
copy from Hermes' persisted environment. This disables the silence-only heuristic, not the SDK's
re-subscription on stream interruption, iterator-end/error handling, or degraded-stream recovery.
The probe compatibility patch remains for explicit, non-messaging liveness checks. Self-host
startup is unchanged.

A unary read cannot prove inbound subscription health. A truly half-open subscription without
errors may remain undetected until meaningful subscription-level heartbeat or sequence evidence
is available. Monitor actual inbound/delivery behavior; do not label ordinary idle silence an
outage or claim the general HTTP health endpoint proves messaging works.


Channel fixes now live in `adapters/hermes/sotto_photon/` and are installed for self-hosted Photon as well as Cloud. Cloud-only policy remains conditional in that adapter. Both deployment modes receive activity-backed VIP/VVIP gift eligibility through the shared skill pack. The old Telegram deployment is not automatically upgraded or kept synchronized; promotion of the shared release brings these changes to self-host installations without a second code fork.
