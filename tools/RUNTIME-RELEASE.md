# Immutable Railway runtime release

The manual `release-railway-runtime.yml` workflow creates an image and descriptor for a separately
reviewed signed Bridge build. It does not update Railway, a pilot, the public source repository,
existing deployments or a Bridge release. Adding this lane does not publish an image.

A maintainer selects a reviewed full source commit, the intended Google chat/media model IDs,
and the explicit `publish-runtime` confirmation. The workflow checks out that exact commit and:

1. Runs the shared backend/release gate with locked dependencies.
2. Generates the public distribution through the existing publication guards, without pushing source.
3. Builds and loads its root self-host Dockerfile for native Linux AMD64, labelled with that source SHA.
4. Runs the packaged runtime/Hermes-retirement smoke against the candidate.
5. Builds fixture overlays from that exact candidate and runs the existing fresh Photon Railway
   prepare/run lifecycle and Telegram setup/runtime lifecycle on new ext4 Docker volumes. Both
   lifecycle suites use synthetic provider/model/Bridge responses and disable container networking.
6. Only after all checks succeed, authenticates to GHCR and pushes
   `ghcr.io/kothari-nikunj/sotto-selfhost:source-<full-source-sha>`.
7. Reads the real registry digest, anonymously pulls that immutable reference, and checks its image ID,
   architecture and source label against the qualified image before emitting `railway-runtime.json`.

The descriptor contains exactly `version`, immutable `image`, `sourceCommit`, `chatModelId` and
`nativeMediaModelId`. No placeholder digest, floating tag, model probe result or fabricated provider
receipt is accepted. Qualification receipts and registry inspection accompany the descriptor artifact;
fixture logs survive failed runs. The descriptor is only available if qualification and registry
verification both finish. A failed push/readback may leave a published source tag but creates no
approved descriptor; inspect the registry before re-running. A source tag is not an immutable claim:
Bridge consumes only the descriptor's digest reference.

`runtime_release_artifact.py` provides the checked qualification and descriptor steps. It requires a
clean exact checkout, refuses an existing qualification directory/volume and never overwrites an
existing descriptor. The workflow uses an ephemeral Ubuntu runner with Docker at
`unix:///var/run/docker.sock`. A host without the production-required ext4 mount or private PID
namespace fails qualification; those checks are never relaxed to make a release pass. It publishes
AMD64 only, not an untested multi-architecture manifest.

After reviewing the workflow evidence, download its `railway-runtime-<source-sha>` artifact and
supply the descriptor to the separate signed Bridge build. Code signing binds those bytes to the app;
this workflow does not sign or promote Bridge and does not produce a Railway template URL. Ensure the
GHCR package is public before running this lane; package visibility is a registry administration step.
The anonymous pull intentionally prevents descriptor emission for a privately published package.

## What qualification does and does not establish

The packaged checks establish the tested image contents; the lifecycle fixtures establish actual
supervisor, mount, ownership, fresh preparation, restart and unknown-send behavior with synthetic
external boundaries. Telegram's packaged lifecycle uses the existing operator handshake; native
Telegram evidence admission is covered by the shared Python contract tests, not a live native
onboarding session. Fixture overlays add test instrumentation and are never published.

This does not establish live Railway provisioning, a real Telegram pairing/delivery, real Google
model access, Mac Keychain/TCC acceptance, or first-user content quality. Those remain separate
release acceptance checks. The new lane and tests were authored without publishing an image; on the
development Mac Docker was unavailable, so the Linux build/lifecycle execution still needs an actual
workflow run before any descriptor can be used for promotion.
