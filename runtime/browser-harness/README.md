# Browser Harness driver foundation

This standalone worker uses Browser Use's **Browser Harness driver**, not its autonomous agent.
Mastra remains Sotto's reasoning owner. The worker needs no browser-service or model key. It is
**not registered with Sotto's live runtime**, included in its Docker image, or an authenticated
browser handoff. Each request starts and destroys its own headless Chromium profile and harness
daemon. Batch actions share that one fresh browser; separate requests do not retain sessions.

## Why this package

[`browser-harness==0.1.13`](https://github.com/browser-use/browser-harness/tree/c24e5072ee66f8499bacd663f4f4bcb089bc4492)
is a published, MIT-licensed Python package. Its own metadata labels it alpha. Pinning and tests
make this integration reproducible; they do not establish a long upstream stability history.
The current [JavaScript variant](https://github.com/browser-use/browser-harness-js) is a Bun/CDP
skill/REPL rather than a versioned Node library. Its installer can install Bun automatically.
We use the packaged Python driver, without the Browser Use agent, MCP extra, cloud account,
editable helper files, or installer. No runtime dependency download or automatic upgrade occurs.

`requirements.lock` pins and hashes all 12 resolved Python packages. The four upstream direct
dependencies are cdp-use, fetch-use, Pillow, and websockets. Fetch-use is an upstream dependency;
this adapter does not call its fetching helpers. The existing trusted image must provision
Python 3.12 and Chromium plus its system libraries. Chromium is intentionally **not downloaded
by this worker**. Production packaging must pin its image digest/browser build and keep browser
security updates current. The executable argument is operator configuration, never model input.

## Install and run standalone

From this directory, create a private environment and install the exact lock:

```sh
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.lock
printf '%s' '{"actions":[{"type":"navigate","url":"https://example.com"},{"type":"snapshot"}]}' |
  .venv/bin/python adapter.py --chromium /usr/bin/chromium
```

This is a developer/operator command, not the final customer onboarding flow. No global skill,
Chrome settings, real browser profile, or existing browser daemon is modified. Chrome's own
sandbox stays enabled; the worker never supplies `--no-sandbox`.

The input is one JSON object with `actions`, then EOF within five seconds. Supported actions:

| Action | Input | Meaning |
| --- | --- | --- |
| Navigate | `{"type":"navigate","url":"https://example.com"}` | Navigate the isolated tab; must be first |
| Snapshot | `{"type":"snapshot"}` | Return page URL, title and rendered visible text |
| Scroll | `{"type":"scroll","dy":720}` | Scroll the viewport, at most 1,440 pixels per action |
| Click | `{"type":"click","x":50,"y":200}` | Coordinate click; requires operator `--allow-interactions` |

No model-controlled Python, JavaScript, CDP method, shell, selector, filesystem path, browser
flag, credential, or profile is accepted. Clicks are disabled by default because they can submit
forms or perform writes; the runtime must eventually authorize actual requested effects before
enabling them. This switch is not that authorization system. Snapshots and page text are
untrusted source material, never instructions for the agent.

Requests are capped at 64 KiB and 12 actions. Captured worker output is capped at 2 MB before further allocation. Normal snapshots return at most 16,000 UTF-16 code
units of text, a 512-unit title and 4,096-unit URL, with explicit truncation and
`coverage: "rendered_page_only"`. This is not whole-document or complete-deck coverage. Browser
startup has a 15-second deadline, batch operations a 90-second deadline, and each owned process
group gets up to three seconds to terminate before forced termination, including descendants
that survive the group leader. Cleanup attempts every owned group even if another group fails. A 200 ms bounded retry reaps
the owned leader when macOS briefly reports permission failure for a zombie-only group;
only a successful signal or confirmed group disappearance resolves the retry, and persistent
permission failure remains an error. SIGTERM/cancellation and normal
completion both clean up worker, daemon and browser process groups and private temporary files.
Downloads are denied. No recordings or screenshots are produced. Browser/service credentials
are not inherited, telemetry is disabled, and an empty private harness workspace prevents loading
the user's editable helpers. No incoming secrets or page bodies are logged by the adapter.

The browser User-Agent is `OpenAI File Downloader, XaiImageApiFetch/1.0`.

## Containment gate before runtime registration

**This is not an SSRF or hostile-browser sandbox.** Initial URL validation rejects local names,
IP literals, credentials, non-HTTP schemes and unusual ports. It does not enforce DNS resolution,
redirect destinations, subresources, JavaScript requests, popups or WebSockets. A page can
contact another origin before the next snapshot. Chromium flags that reduce background traffic
also do not provide network isolation. CDP and harness IPC are local machine control surfaces.

Before making this a tenant tool, provide and verify:

1. A disposable non-root container with no host mounts/secrets and private CDP/IPC, CPU/RAM/PID/
   disk ceilings, cancellation and profile cleanup after failures.
2. Enforced outbound networking that blocks private/link-local/metadata destinations after DNS
   and on redirects, subresources and alternate transports. Browser process isolation alone is
   insufficient. Output limits in this adapter do not cap hostile page memory or browser disk.
3. Receiver-owned source/contact authority, click/write approval, bounded session ownership,
   audit receipts and per-tenant concurrency. Never retry a tracked DocSend view without the
   existing authority. A rendered-page snapshot cannot assert full slide coverage.
4. An explicit authenticated-session handoff design with consent and encrypted session storage.
   Do not attach the default user profile or copy its cookies to this worker.
5. Linux amd64/arm64 image smoke tests and browser-build/license inventory. The supplied smoke
   proves the driver and a fresh browser work on the tested machine; it does not prove these gates.

For synthetic local testing only, an operator may set `--fixture-origin http://127.0.0.1:PORT`.
The input JSON cannot enable this exception. It permits exactly that origin in initial navigation
validation; it is not an egress allowlist.

## Verification and updates

```sh
python3.12 -m unittest discover -s tests -v
.venv/bin/python smoke.py --chromium /usr/bin/chromium
```

The smoke serves a synthetic loopback page, verifies JavaScript rendering and an approved fixture
click through the real driver, checks the requested User-Agent, then closes its server/browser.
No user profile or external website is needed. The unit suite runs without the driver installed.
Observed validation: all fourteen contracts passed; the real render/click smoke passed using
Python 3.12.11, Browser Harness 0.1.13 and Google Chrome 154.0.8037.98 on macOS arm64.
Linux containers and authenticated sessions have not been exercised.

Update `requirements.in`, regenerate `requirements.lock` with `uv pip compile --python-version
3.12 --generate-hashes`, review all dependency changes and upstream daemon/helper behavior, install
with `--require-hashes`, and run contracts plus the real fixture smoke on supported images. Review
upstream telemetry, automatic discovery, helper loading and CDP defaults on each upgrade. Ship the
lock, image digest and observed browser version together; rollback by restoring that tested set.
Do not use `uvx`, a floating Git ref, `latest`, or runtime self-updates in deployed workers.

License inventory: Browser Harness, cdp-use, fetch-use, anyio, h11, httpcore, httpx, idna and
websockets use permissive MIT/BSD-family licenses; Pillow uses MIT-CMU, certifi MPL-2.0 and
typing-extensions PSF-2.0. Chromium/Chrome is separately provisioned with its own third-party
notices; no browser binaries are redistributed here. Preserve the installed distribution license
files when building the production image, and verify its exact binary/package inventory.

Files: `adapter.py` is the bounded launcher/driver, `smoke.py` exercises a real isolated browser,
`tests/test_browser_harness_adapter.py` checks input/process contracts, and the requirements files
define the reproducible Python environment.
