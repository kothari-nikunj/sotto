# Local reader foundation

This independent package extracts article text from **HTML supplied by its caller**.
It is not connected to the Sotto runtime. It does not fetch URLs, browse, search,
read DocSend, register an agent tool, or replace an existing web research route.
No API key, browser download, service, native build, or model is required.

It uses pinned [Mozilla Readability](https://github.com/mozilla/readability)
(`0.6.0`, Apache-2.0) with [LinkeDOM](https://github.com/WebReflection/linkedom)
(`0.18.13`, ISC). The latter supplies an inert HTML DOM without a browser's
script execution or resource loader. A lockfile pins transitive dependencies.
The package is separate from `sotto-chat` so upgrades do not change chat's dependency tree.

## Install and verify

Requires Node `>=22.18.0 <27` and npm. From this directory:

```sh
npm ci --ignore-scripts
npm run check
npm test
```

`.npmrc` also disables installation scripts, enforces exact direct version pins,
and rejects unsupported Node engines. The tests use a temporary loopback HTTP
server to detect unwanted script/resource requests; the test runner therefore
needs permission to listen on loopback. Ordinary extraction opens no socket.

## One-job interface

Pass exactly one JSON object through stdin and close the stream. Use `node`
directly when consuming stdout, since npm's command banner is not JSON.

```sh
node --input-type=module -e '
  import {readFileSync} from "node:fs";
  console.log(JSON.stringify({
    html: readFileSync("test/fixtures/article.html", "utf8"),
    sourceUrl: "https://example.com/news/library"
  }));
' | node src/cli.js
```

Success is one JSON line with exit code `0`:

```json
{
  "ok": true,
  "article": {
    "sourceUrl": "https://example.com/news/library",
    "title": "A library opens its doors",
    "text": "Extracted article text..."
  },
  "coverage": {
    "kind": "extracted_article_text",
    "truncated": false,
    "originalTextChars": 25,
    "returnedTextChars": 25,
    "titleTruncated": false
  }
}
```

The text and lengths above are illustrative. Counts are Unicode code points.
Coverage describes the extracted article only, not completeness of the source,
hidden content, authenticity, or accuracy. Navigation, short pages, and some
legitimate articles can produce `NO_ARTICLE`; extraction uses Readability's
readerability heuristic and does not silently return the entire page.

Failures use `{"ok":false,"error":{"code":"NO_ARTICLE","message":"..."}}`
and exit code `1`. Codes are `INVALID_INPUT`, `INVALID_SOURCE_URL`,
`INPUT_TOO_LARGE`, `INPUT_TIMEOUT`, `HTML_TOO_LARGE`, `DOM_TOO_LARGE`,
`NO_ARTICLE`, `EXTRACTION_TIMEOUT`, `EXTRACTION_FAILED`, or `WORKER_FAILED`.
Messages do not include submitted HTML or URL contents. No diagnostic logging
or telemetry is added. Callers must treat extracted text as untrusted content.

An existing Node caller can also `import { readArticle } from './src/index.js'`
and await the same result for `{html, sourceUrl}`. Callers own scheduling and
should serialize requests or enforce their own bounded concurrency.

## Limits and isolation

- Stdin JSON is capped at 2 MiB and must finish within 10 seconds. Decoded HTML
  is capped at 1 MiB; source URLs at 4,096 UTF-16 code units.
- Each extraction uses a disposable Node child with a 128 MiB V8 old-space
  limit, 10-second wall deadline, empty environment, and captured stdout.
  This is resource containment, **not an OS security sandbox or a total RSS cap**.
- Documents over 20,000 elements are rejected after initial parsing. Initial
  parsing is still covered by the input, process memory, and deadline limits.
- Article text is capped at 16,000 Unicode code points and title at 512, with
  explicit truncation flags. Worker output is capped at 256 KiB.
- Scripts, styles, templates, and noscript elements are removed before article
  extraction. HTML is never returned. The parser does not execute scripts or
  load images, stylesheets, frames, or other external resources.
- `sourceUrl` is metadata only. It must be an HTTP(S) URL with a dotted hostname,
  no embedded credentials, no literal IP, and no known local hostname suffix.
  It is never resolved or fetched, and a document's `<base>` cannot change it.
  This syntax check is **not SSRF protection for a future network fetcher**.

## Safe integration and updates

Integrate only after owner/source authorization, outbound-contact semantics,
DNS and redirect validation, egress restrictions, and receipt provider types
have a separately reviewed implementation. Do not hand this package an arbitrary
URL and assume it has been read. A future browser can supply rendered HTML to
this same interface; no browser dependency is needed here.

Upgrade Readability/LinkeDOM through a reviewed version-and-lockfile change,
then run clean installation, syntax checks, and the fixture suite on supported
Node versions. Keep script/resource suppression, malformed document handling,
bounded input, paragraph separation, and truncation tests in that gate. Rollback
is a code/lockfile revert; this package writes no persistent state.

Files: `src/contracts.js` owns shared limits and result construction;
`src/index.js` validates jobs and supervises extraction; `src/worker.js` parses
and extracts without importing the launcher; `src/cli.js` implements bounded stdin/stdout;
`test/reader.test.js` and `test/fixtures/article.html` verify behavior.
