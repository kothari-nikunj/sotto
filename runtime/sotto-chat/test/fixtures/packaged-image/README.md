# Offline packaged-image smoke

Run this against a locally built Sotto runtime image with an explicit platform:

```sh
./run-packaged-smoke.sh sotto-chat-runtime:phase24 linux/arm64
./run-packaged-smoke.sh sotto-chat-runtime:phase24-amd64 linux/amd64
```

The launcher uses the caller's ordinary Docker environment and hashes the current
`runtime/trigger-receiver/receiver.py` from the source checkout containing this fixture.
Build the image from that same checkout before running the smoke; it uses `--pull=never`
so a missing local tag fails instead of downloading an image. It starts
no provider or model service and has no network; state lives only in container tmpfs.
The mounted fixtures are read-only. The receiver check creates synthetic managed source
consent and a fake Relay health result, with a poison token under a stale synthetic home
directory and no `SOTTO_HOME`, to verify Bridge-only source authority without ambient Google lookup.

Before runtime imports, `hermes-retirement-check.py` examines the built image's installed Python
distributions, importable Hermes CLI/agent modules, PATH executable and known retired checkout/
launcher locations. The receiver's real admission path also rejects both bare and absolute Hermes
one-shot commands before creating queue state. This checks shipped artifacts, rather than banning
the word Hermes from history, tests, or the retained MIT-attributed `routine_cursor` adaptation.
These are targeted checks for the retired integration; they do not claim to detect an arbitrarily
renamed or deliberately concealed implementation. Unit tests plant each rejected artifact shape
to keep the check sensitive. Running those unit tests alone is not a built-image verification.

The Node contract test imports compiled runtime modules from `/app` and the packaged Python
client from `/app/adapters/sotto`. It checks fixed and first-contact activation through a
loopback HTTP listener, using synthetic identities and credentials. It does not exercise a
real receiver supervisor, provider connection, live message, or deployment readiness.

Source CI and the generated public CI build a fresh AMD64 standalone image, run this smoke,
and run both native lifecycle contracts on the native Linux runner. Builds use public package
registries; runtime fixtures have no external networking. Local workflow parsing and public
generation checks passed, but remote CI execution remains unobserved until the changes are pushed.
