# Native Railway setup configuration boundary

`selfhost_configuration.py` implements the **offline configuration compiler**, not a setup
server. The native provisioning owner supplies protected Railway attachments and, for Telegram,
captured owner evidence. Compilation performs no provider operation, volume write, message send
or model probe. There is no new public setup HTTP route. Existing deployment and fresh-volume
checks are unchanged. The product setup paths remain Sotto Cloud and Railway through Bridge.

## Implemented contract

`parse_native_request(bytes)` accepts at most 64 KiB of UTF-8 JSON and rejects duplicate keys,
unknown fields, invalid primitive types and unsupported channels. Full model, token, owner,
timezone and runtime validation occurs in `compile_native` through the existing
`startup_config.render` and `process_handoff.build_child_handoff` implementations.

The version-1 request has these exact fields; there are no optional arbitrary overrides:

| Field | Type / purpose |
| --- | --- |
| `version` | Integer `1` |
| `channel` | Literal `photon` (iMessage) or `telegram` |
| `tenantId` | Stable application tenant identity |
| `railway` | `{projectId, environmentId, provisionRequest}`: the explicitly selected Railway target and saved setup request identity |
| `owner` | Photon: `{accountId, phone}` with exact E.164 owner number. Telegram: `{accountId}` only; the owner ID is captured, never typed |
| `lineKey` | Stable application line identity |
| `instructions` | Reviewed application instructions; the native UI need not expose this as an editable field |
| `photon` | `{projectId, projectSecret, lineNumber}`: explicit Photon credentials and expected physical line |
| `telegram` | `{botToken}` for Telegram only; exactly one channel credential object is allowed |
| `model` | `{apiKey, chatModelId, nativeMediaModelId}`: current direct Google contract; no implicit credential discovery |
| `timezone` | IANA timezone validated by the existing handoff |
| `backgroundUnmetered` | Explicit boolean; `false` does not grant unmetered background model access |

`compile_native(request, attachment, generated_secrets, telegram_capture=None)` returns the protected
`SOTTO_SELFHOST_LAUNCH` envelope. It does not return a public receipt or authorization grant.
The native input has no runtime paths, command arguments, service selector, claim mode,
port overrides, runtime bearer or control bearer fields.

The independently verified `attachment` has exact fields `projectId`, `environmentId`,
`serviceId`, `volumeId`, `domain`, `provisionRequest`, and
`volumeCreationReceipt: {service_id, volume_id}`. IDs must be canonical, nonzero UUIDs.
Project/environment/request must match the native selection, and the creation receipt must
bind the exact service and volume. Attachment input is capped at 16 KiB. **Shape validation
cannot authenticate a provider receipt.** The trusted provisioner must obtain and reconcile
these facts through Railway before calling the compiler; accepting this object from an
unauthenticated client would not be sufficient.

`new_setup_secrets()` generates separate 256-bit runtime, Bridge and control secrets. The
provisioning owner persists these once, outside `/data`, before any resource mutation and
passes them as `{runtimeBearer, bridgeToken, controlToken}`. All three must be distinct;
they cannot come from editable native request fields. The generated-secrets input is capped
at 16 KiB. Returned envelopes contain credentials and must be stored/transported privately,
never logged or exposed in status responses. Compilation is repeatable with the same saved
inputs and does not mutate them or read credentials from the environment.

The existing Railway helper now delegates its envelope assembly to this same module. Its
source-pin checks, private resumable state, exact resource attachment reconciliation,
unknown-outcome behavior, deployment order and bootstrap gates remain in their existing
owners. The low-level `build_launch_envelope` is assembly for that already-validated caller;
it is not a public validation or authorization entry point.

## Telegram capture and launch admission

Telegram capture completes **before** the Railway runtime starts. The native owner is the only
setup consumer of Bot API updates; it stops consuming once evidence is saved, then the supervised
runtime owns polling. It uses authenticated HTTPS responses from `getMe`, `getWebhookInfo` and
`getUpdates`; it never deletes a webhook or sends a probe message. An active webhook requires a
dedicated Sotto bot. Other accounts' pending updates cannot block pairing: they are ignored, and
are consumed only by the acknowledgement that follows the owner's own Start.

`telegram_link.validate_native_capture` is the canonical backend evidence validator and reuses
the existing private-human message filter. The native capture contract adds exact phrase matching,
safe positive owner IDs, exact private chat/sender identity, bot-token prefix identity and a confirmed
offset. Its bounded version-1 evidence has these exact fields:

| Field | Meaning |
| --- | --- |
| `version`, `provisionRequest` | Integer `1` and the selected setup's saved request marker |
| `phrase` | 32–64 URL-safe random characters, persisted privately before polling |
| `bot`, `webhook` | Raw authenticated `getMe.result` and `getWebhookInfo.result`; webhook `url` must be empty |
| `updates`, `update` | The observed `getUpdates.result` batch and its first exact owner Start; ascending unique update IDs, at most 100 items |
| `acknowledgedOffset` | Exactly the batch's last `update_id + 1` |
| `acknowledgement` | Successful zero-wait acknowledgement result, at most 100 ascending updates, each at or above the offset |
| `requestDigest` | SHA-256 of UTF-8 `sotto-native-telegram-pair-v1` + NUL + request marker + NUL + bot token + NUL + phrase |

Evidence is capped at 64 KiB. Polling without an offset leaves results pending, and a batch with
no exact private human phrase or `/start <phrase>` is left unacknowledged while setup keeps
waiting. Any other update in the batch is ignored, whoever sent it and whatever it says. If the
exact phrase arrives from two different accounts, pairing is refused instead of choosing by
order; a repeated Start from the same owner is one claimant. The observed batch is saved in the
native protected record before the zero-wait acknowledgement request; an uncertain
acknowledgement can repeat that exact offset on resume, without advancing past later updates. The
phrase and bot binding cannot change mid-setup.

As with Railway receipts, JSON shape is not cryptographic proof of origin. The trusted native
provisioning owner must obtain these responses directly from Telegram and protect the saved record;
this validator is not suitable as an unauthenticated owner-claim API. The request digest binds the
saved attempt's inputs and prevents accidental cross-setup reuse; it is not a signature.

The compiler derives `owner.ownerId`, `providerThreadId: telegram:<owner>`, the canonical
`telegramLink: {bot_token, allowed_user}` and the token's `botFingerprint` from this evidence.
Telegram launch envelopes carry a separate top-level `telegramCapture`; `_railway_envelope`
independently validates it against the actual manifest and provider request marker before staging.
The evidence is discarded from the staged bundle: the runtime receives only its canonical private
link and owner identity, never the setup phrase or raw pairing message. Its existing Telegram
fixed-five profile, receiver authority and bot/owner protections remain in force.

[`fixtures/native-telegram-setup.json`](fixtures/native-telegram-setup.json) is the synthetic golden
request/evidence/envelope used to keep the native producer and Python admission contract aligned.

## Why first-boot claim/resume is not added here

`prepare_fresh._fresh_entries` admits only an empty ext4 volume's formatter directory and
the fresh initializer's own lock. A setup record on `/data` before preparation correctly
makes that volume nonempty. The preparation completion marker is written last; a crash
before it deliberately leaves a nonempty volume that is refused, not silently adopted.
Writing a setup claim to `/tmp` avoids contaminating `/data` but loses durable claim ownership
on container replacement. Neither approach meets the required claim/resume contract.

A complete Bridge-led setup must supply this privileged protocol outside the compiler:

1. Authenticate the selected Railway account/project/environment without treating a pasted
   runtime URL as proof of ownership. Confirm provider purchases in the native user flow.
2. Keep one protected setup record in the owner's native Keychain (with nonsensitive progress
   references separately if needed), binding the selected target, reviewed source/image,
   setup request ID, generated secrets, resource IDs and every mutation attempt/receipt.
   Do not persist this record on the unprepared tenant volume.
3. Create and reconcile only resources carrying that request identity. Unknown create/upload
   outcomes become observation-only resume; never automatically repeat a possible mutation.
4. Compile the private envelope only after authenticating exact service/volume/domain
   attachments. Store it through the protected provider configuration path, then run existing
   `--prepare`, observe its completed volume marker, and transition to existing `--run` on
   the same attachment. Compilation alone cannot skip mount, namespace, fresh-volume, owner,
   source-pin or lineage checks.
5. Complete explicit Google/Bridge consent, configured owner/channel activation and visible
   delivery acceptance. An HTTP status or compiled envelope is not first-user acceptance.

This external durable-record design can reuse the existing strict fresh-volume protocol.
A server-side setup listener that stores resumable control state on the same volume instead
needs a separately reviewed **versioned** setup-state/volume contract, trusted claim authority,
atomic ownership transfer, crash/replay handling and fresh-image tests. Do not add a blanket
`.setup` exception or erase partial data to make the current freshness check pass.

## Verification boundary

The new tests exercise malformed/oversized inputs, target and receipt mismatches, separated
secrets, existing model/owner/timezone/public-domain validation, ambient-key refusal, stable
output, unchanged inputs, and refusal to cross the prepare/start boundary. The valid native
result passes the existing Railway envelope gate. Existing helper, fresh-preparation and
bootstrap suites cover the unchanged downstream gates with synthetic provider facts. Telegram
tests also cover malformed/private-owner/bot/phrase/offset evidence, existing webhooks, unrelated
backlog, raw-evidence exclusion from runtime state and refusal of typed owner surrogates. They
exercise real manifest/handoff/preparation admission with synthetic mount facts; the native host's
missing root-owned `/app` skill pack is replaced only at that filesystem check.

No paid provider call, real Railway provisioning, kernel ext4/PID-namespace acceptance,
native Keychain flow, fresh-user installation or live Telegram pairing is demonstrated by these
compiler tests. Those remain separate integration/release evidence.
