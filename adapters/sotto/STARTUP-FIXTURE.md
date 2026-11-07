# Offline Sotto runtime startup fixture

`startup_config.py` renders an explicit, versioned private manifest into the
Node conversation service's strict JSON config and a receiver URL/token
projection. It does not discover credentials,
start a provider, or change `runtime_readiness()`.

The trusted caller supplies every credential and stable identity from its own
deployment and pairing stores. The renderer checks shape and cross-field
binding; it **does not** establish that consent is current, that a provider is
reachable, or that an activation receipt was written by the genuine receiver.
The manifest must be a regular, current-UID, private file of at most 64 KiB.
Duplicate JSON keys, symlinks, FIFOs, and world/group-readable files fail.

The manifest schema is the exact set of fields in the offline tests:
`version`, `deployment` (`managed` or `self_host`), `channel` (`photon`, or
`telegram` for self-host), `tenantId`, `owner`, `lineKey`, `target`,
`stateDirectory`, `mediaRoot`, `instructions`, `runtime`, `relay`,
`credentials`, exactly one of `modelProxy` (managed) or `modelDirect`
(self-host, provider `google`), and either `telegramLink` or
`activationReceipt` (fixed mode) or `firstContact` (fresh Photon Cloud mode).
`providerThreadId` is required in fixed mode and forbidden in first-contact
mode. `skillPackRoot` and `photonLineNumber` are optional, but if
`photonLineNumber` accompanies first contact it must match
`firstContact.expectedProviderLine` exactly.
`runtime.bearerToken` must be a separately generated private bearer; it is
copied only to Node config and receiver projection. `relay.rootBearer` is used
only to derive the read-only `/mcp` HMAC bearer; the root is never emitted.

Run `python3 startup_config.py private-manifest.json` for a redacted validity
preview. Add `--stage-directory /private/new-bundle` to create a new 0700
directory with `runtime-config.json` and `receiver-projection.json`, both 0600.
An identical rerun preserves bytes and repeats the parent-directory durability barrier; different content at that path is refused.
The staging caller must run as the **same UID that will run Node** (UID 10000
in the runtime image). The receiver runs as UID 10001; the root supervisor
stages its separate private projection through `role_stager.py` without
weakening the Node config owner check. The two JSON files are sensitive and
must not be logged or published.

Fixed Photon Cloud-project rendering requires a caller-supplied, already-activated
receipt with the exact tenant, owner, project fingerprint, and chat GUID. The
SDK thread must be `imessage:<guid>` or, if the caller supplies the assigned
line number, `imessage:<guid>~<line>`. Explicit fresh Photon Cloud rendering
instead requires `firstContact: {"mode":"owner_dm", "expectedProviderLine":"+..."}`,
an E.164 physical line distinct from the logical `lineKey`, and **no** receipt
or provider thread. The Node gateway alone can bind the first authenticated
owner DM to a thread in its durable journal; the receiver remains the sole
receipt writer. Rendering does not activate a line, unlock scheduled delivery,
grant any source, or validate live owner consent. The trusted caller must
populate `expectedProviderLine` from its assigned physical line (the setup
helper's `photon_line_number`, or managed `SOTTO_IMESSAGE_NUMBER`); the renderer
never looks up that environment value. Telegram uses the existing sole pairing
writer's matching bot token and numeric owner ID.

The immutable runtime identity (`accountId`, `ownerId`, `lineKey`, `target`,
`providerThreadId`) must come from a persistent trusted manifest; changing it
against an existing Node journal fails closed. Proxy chat and native media model
IDs must be explicitly supplied from the authorized proxy contract; the Python
brief's `SOTTO_GEMINI_MODEL` is not that contract. Starting Node and the
receiver from this output, in order and as the only consumers of the channel, is
the root supervisor's job ([docs/ARCHITECTURE.md § Boot](../../docs/ARCHITECTURE.md#boot)).


## Local process handoff projection

`process_handoff.build_child_handoff()` reuses `startup_config.render()` and accepts managed
Photon, self-host Photon and self-host Telegram manifests, a separately authenticated stable
identity (including the project or bot fingerprint), explicit receiver authorities and optional
owner-selected overrides. It rejects mismatched tenant/route/capabilities, reused secrets and
non-HTTPS managed proxy origins. The helper reads no ambient environment or credentials. Missing
override keys mean the caller declared no override; they do not request discovery. The allowlist
preserves `SOTTO_BRIEF_MODEL`, `SOTTO_GEMINI_MODEL`, `SOTTO_FALLBACK_MODEL`, `SOTTO_RESEARCH_DEEP`
and `SOTTO_TIMEZONE` without substituting Node's native-media model for the Python brief model.
Shape validation does not prove that the proxy supports a model or that its lease is current.

The result contains direct-exec argv, read-only environment mappings, Node launch UID 10000,
receiver wrapper UID 0 and receiver workload UID 10001. These mappings are the children's whole
environments. The existing `managed_exec.py` wrapper must capture the control credential before
dropping privilege/importing the receiver. Node gets provider/proxy/read-only Bridge capabilities
through its private config; the receiver also needs the current Photon project/secret and owner
allowlist (or the Telegram bot token and owner id) for its existing activation and channel checks.
The root MCP and lease-renewal/control authorities never enter Node's environment.

The returned SHA-256 covers the renderer's exact canonical Node config bytes. The caller still
must authenticate the inputs, securely install those bytes with matching UID ownership, hold the
runtime lock, launch the children and enforce their lifecycles. UID fields and a digest do none
of that themselves. Environment mappings contain secrets and must never be logged; result repr
and validation failures are redacted.

Without `public_receiver_authority` the projection is loopback-only, using
`SOTTO_TRIGGER_PORT` without public `PORT`; with a separately trusted attachment it also projects
`PORT` and `RAILWAY_PUBLIC_DOMAIN` so the receiver's public setup, Google callback and Bridge relay
share one listener. Node's local lifecycle may precede the receiver, but activation ACK cannot be
a prerequisite for starting the process that writes it. No process, provider, rollout or
production-startup acceptance is claimed by this helper.
