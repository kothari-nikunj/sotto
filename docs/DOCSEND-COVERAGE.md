# DocSend page coverage

A successful DocSend read means that Sotto interpreted the pages it obtained; it does not prove that it read the entire deck.

The HTTP reader retains its existing gate, page limit, source-authority checks, model route, and cache-first behavior. It makes no additional request to establish completeness. The supported viewer response provides an image URL for each requested page but no verified total-page or authoritative end signal. A later 404 or missing image URL can be an unavailable page, so neither establishes the deck's end.

The reader result and saved deck JSON include these additive fields:

| Field | Meaning |
| --- | --- |
| `pages` / `pagesRead` | Number of page images supplied to the successful vision read. The original `pages` field remains compatible. |
| `expectedPages` | `null`; the current reader has no authoritative total. |
| `complete` | `null`; full-deck completeness is unknown, including for a short deck or one that reaches the limit. It is never inferred from a missing next page. |
| `stopReason` | `page_limit`, `page_fetch_failed`, `page_end_unverified`, or `legacy_cache`. |

`page_limit` means collection reached the existing limit without probing the next page. `page_fetch_failed` covers a later request failure or invalid metadata. `page_end_unverified` covers a later 404 or metadata without an image URL. These distinctions describe how collection stopped, not whether more pages actually exist. A future complete result requires a qualified upstream count/end contract and fixtures; this change does not invent one.

Every successful result prefixes `text` with the number of pages read, unknown completeness, and the reason collection stopped. The same warning says any saved PDF contains only the pages read. This text survives the existing attended receipt projection, whose schema remains unchanged. Structured coverage fields are available in the reader/cache but are not added to that receipt schema.

Old caches remain usable with `stopReason: legacy_cache` and unknown completeness. Reading one adds the warning to the returned value without rewriting the cache or contacting DocSend. New caches preserve their coverage reason. Repeated reads do not duplicate the warning or create another view.

First-page failure and image-download failure still return errors. `DocSendHeldError` still propagates for authority changes, uncertain attended contact, deadlines, and response bounds; no partial cache or model call follows such a hold. The existing attended journal continues to prevent replay of an uncertain contact. No browser fallback or automatic retry is introduced, and fresh reads retain their existing confirmation rules.

Validation uses synthetic HTTP/image/model fixtures only: short decks with unverified endings, HTTP errors, invalid metadata, capped decks without an extra probe, cache compatibility, held contacts, and attended receipt/cache/no-replay behavior. No live deck view is needed to run these tests.
