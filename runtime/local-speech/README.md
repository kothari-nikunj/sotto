# Local speech foundation

This optional, standalone worker turns explicitly supplied text into a local WAV using
sherpa-onnx and an operator-installed Kokoro pack. It is **not connected to Sotto's
runtime, brief preferences, dashboard, message delivery, or installer**. Installing this
directory does not change the live agent. There are no model API keys, HTTP endpoints,
remote fallbacks, asset downloads, or first-request installation in the worker.

Python 3.12 on macOS 14+ arm64 or glibc 2.28+ Linux amd64/arm64 is the initial target.
Alpine/musl and Windows are not supported by this wrapper. The native runtime has its own venv
and hash-locked dependencies; nothing is added to the receiver's Python environment.
The implementation uses the upstream native inference API; it does not fork Kokoro,
implement a phonemizer, or include a second agent loop. Only CPU inference is enabled.

## Explicit setup

Run from this directory. Use a dedicated, local install directory for your pack and
private output directory; never place voice assets or generated audio in the repository.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: --require-hashes -r requirements.lock
```

Install a model pack **before** starting the worker. The tested example is the official
[`kokoro-int8-en-v0_19.tar.bz2`](https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-int8-en-v0_19.tar.bz2)
(103,248,205 bytes). Its reviewed archive SHA-256 is:

```text
c9f0dd393615805b0bab050c340834d5e684e732aec91c0e860cd30e982c08bd
```

Verify that checksum before extraction and preserve the pack's notices. This is a small,
qualified English baseline, not a claim that v0.19 is the newest or best voice model.
Use a safe archive extractor, e.g. Python 3.12's `tarfile.extractall(filter="data")`;
reject unexpected members rather than extracting arbitrary archives over existing files.
Place the example JSON outside the pack. After you trust the origin and license inventory:

```sh
.venv/bin/python speech.py seal --pack /absolute/path/kokoro-int8-en-v0_19 \
  --config kokoro-en-v0_19.example.json
.venv/bin/python speech.py check --pack /absolute/path/kokoro-int8-en-v0_19
printf '%s\n' '{"text":"Your morning brief is ready.","voice":"default","speed":1.0}' |
  .venv/bin/python speech.py synthesize --pack /absolute/path/kokoro-int8-en-v0_19 \
    --output /absolute/path/private-audio-cache
```

`seal` inventories every file (model, voices, tokens, phonemizer data, and notices) and
writes `manifest.json` once. It does not authenticate an arbitrary model: it establishes
an integrity baseline for files the operator already trusts. `check` verifies this exact
inventory and the installed dependency versions. A changed, added, missing, linked, or
special asset fails validation. To upgrade a pack, install and seal a new directory;
do not mutate a pack that a worker is using. Mount the pack read-only in a future service.

## Contract and limits

One CLI invocation reads one EOF-delimited JSON object from stdin and returns one JSON
object on stdout. Exit 0 means success; exit 1 includes a stable `error` code. Native
runtime diagnostics may appear on stderr; callers should handle stderr as sensitive.
No request-supplied filenames, model paths, URLs, languages, shell arguments, or provider
options are accepted. The operator chooses `--pack` and `--output`.

| Input | Contract |
| --- | --- |
| `text` | Required nonempty plain text, at most 6,000 Unicode characters |
| `voice` | Required alias from the installed pack's explicit speaker map |
| `speed` | Optional finite number from 0.5 through 2.0; default 1.0 |
| Request size | At most 32,768 bytes; unknown fields and duplicate JSON keys rejected |
| Input time | 30 seconds to reach EOF; SIGINT/SIGTERM cancels an unfinished pipe |
| Inference | Separate native process, at most 180 seconds; SIGINT/SIGTERM cancels |
| Output | Mono 16-bit PCM WAV, 8–48 kHz, at most 600 seconds |
| Concurrency | One active request per private output directory; another returns `busy` |

The timeout covers native model loading and synthesis, after bounded pack integrity
validation. Input validation and hashing run in the parent. The pack is capped at
10,000 files / 4 GB and manifest at 2 MB. These are corruption and operational bounds,
not a claim that every allowed model fits a particular machine's RAM. A future service
must also enforce process memory/disk limits and terminate the entire process group on
forced shutdown; a SIGKILL cannot run Python cleanup.

A success returns `artifact` (a generated basename), `cache_id`, `cached`, `pack_id`,
`voice`, and `audio` metadata: rate, frames, duration, channels, encoding, byte length,
and audio SHA-256. It never sends or plays the result. The private directory is mode
0700 and owned by the worker user. Filenames derive only from the cache identity.
Results are published by atomic rename after validation; failed/cancelled inference
removes partial scratch files and never publishes an audio receipt.

The cache identity includes the **entire sealed pack**, exact text/voice/speed, pinned
runtime versions and dependency lock, worker source hash, Python/platform, CPU/thread
settings, and encoding. Cache hits revalidate audio checksums and metadata. Output
samples need not be bit-identical across independent synthesis runs; a cache hit reuses
the already-produced artifact. No input text is stored in metadata. Audio remains
sensitive tenant data: this standalone tool has no retention policy or deletion daemon;
the future owning runtime must set quotas, retention, consent revocation, and cleanup.

## Licenses and distribution

The example's inventory separates Kokoro weights/voices (pack's Apache-2.0 license),
sherpa-onnx (Apache-2.0), and eSpeak NG phonemizer/data (GPL-3.0-or-later plus upstream
third-party data notices). **The speech bundle is not Apache-only.** The upstream
archive does not identify an exact eSpeak source revision in its top-level README.
No model archive, binary, or eSpeak data is redistributed here. Publishing a prebuilt
image/pack requires a complete notices and corresponding-source inventory for those
exact artifacts, including native libraries and NumPy's bundled dependencies; this
example is not that release inventory. See [eSpeak's license](https://github.com/espeak-ng/espeak-ng/blob/master/COPYING)
and [sherpa's third-party dependencies](https://github.com/k2-fsa/sherpa-onnx/tree/v1.13.8/cmake).

## Verification and upgrades

Contract tests use small synthetic assets and a child process; they do not prove neural
inference. They cover malformed manifests, whole-pack integrity, symlinks, traversal,
request bounds, cache identity/integrity, concurrency, deadline cleanup, and cancellation
both before inference and while a child is running. They need only the standard library:

```sh
python3.12 -m unittest discover -s tests -v
```

Explicit real inference check, after installing a pack, using a new empty output directory:

```sh
.venv/bin/python smoke.py --pack /absolute/path/kokoro-int8-en-v0_19 \
  --output /absolute/path/new-private-smoke-cache
```

Observed on macOS 26.5.2 arm64 / Python 3.12.15 with sherpa-onnx 1.13.8 and NumPy 2.3.4:
a two-sentence example produced ~4.90 seconds of nonempty 24 kHz PCM audio in ~5.8
seconds for the first measured cold worker (a repeat was ~4.6 seconds), then verified a checksum-preserving cache hit. An independent
native reference call without the streaming callback produced the same approximate
length with nonzero RMS; its peak RSS was ~378 MB. These are single local smoke runs,
not a throughput, intelligibility, long-brief, or low-memory benchmark. Hash-verified Python 3.12 binary wheels
were downloaded successfully for Linux amd64 and arm64. Linux inference and Docker
images have not been run here; no Docker daemon was available.

The pinned Kokoro C++ implementation uses callback return **1 to continue and 0 to stop**;
its Python docstring currently describes the inverse. The bounded callback follows the
[pinned implementation](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.8/sherpa-onnx/csrc/offline-tts-kokoro-impl.h).
Recheck this behavior and multi-sentence completion when upgrading.

Upgrade only this directory's dependency lock, in a separate PR:

```sh
uv pip compile requirements.in --python-version 3.12 --generate-hashes --output-file requirements.lock
```

Run contract tests, clean binary-only installation on Linux amd64/arm64 and macOS arm64,
real synthesis and cancellation, license inventory, and a representative long brief before
publishing a release. Roll back the venv and pack together; different identities preserve
previous audio without confusing it with the new runtime. A dependency bot must not
silently replace a deployed model pack or install packages while serving a request.

## Integration still required

A future Sotto adapter must consume only the already validated brief, apply an explicit
audio preference, and reference the resulting artifact through the existing consent,
retention, and delivery owner. It must report unsupported voice/language capabilities,
provide queue and resource bounds, and validate channel audio formats. Dashboard WAV
playback, native voice-note encoding, message retries, and an audio outbox are separate
work. This directory intentionally owns none of those surfaces, so it can be reviewed
without modifying the open runtime/bootstrap/delivery PRs.
