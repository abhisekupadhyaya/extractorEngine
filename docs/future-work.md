# Future work

This document describes how the pipeline would evolve toward a continuously
operated production collector. Everything here is **out of scope for this
version** and is recorded as a deliberate boundary, not an omission. The current
version is a single-pass, single-site scraper that produces a clean JSONL
collection; the items below are the natural next increments, most of which are
cheap precisely because the engine is already a pure library with a single data
contract.

## Richer incremental re-crawling

Conditional GET via `If-Modified-Since` is already part of normal operation (see
[crawling.md](crawling.md)) and sits in front of the insert/skip/update logic in
[storage-and-idempotency.md](storage-and-idempotency.md). Two richer variants
remain future work:

- **`ETag` validators** alongside `If-Modified-Since`, for origins that serve
  entity tags but not (or in addition to) a `Last-Modified` date.
- **Sitemap `lastmod`** to discover what changed since the last crawl and visit
  only those URLs, rather than re-walking the whole frontier.

Like conditional GET, these sit *in front of* the existing insert/skip/update
logic; they make re-crawls cheaper, they do not replace the idempotency model.

## Scheduling and monitoring

A continuous collector needs to run on a schedule and be observable:

- **Scheduling** of recurring crawls per source.
- **Monitoring** of crawl freshness (how stale is each source), extraction-quality
  metrics (rejection rates per cascade layer, share of pages hitting the crude
  fallback), and **alerting** when those metrics regress — an early signal that a
  site's markup has drifted.

## Richer storage and cross-source dedup

- A **normalized, multi-table Postgres store** as the primary collector backend,
  rather than the optional state role it plays today.
- **Cross-source deduplication**: identical content reached through different URLs
  (same `content_hash`, different `url`) is not deduplicated today, because that
  would require a separate `content_hash → id` index that complicates the upsert.
  A real collector ingesting many sources would add that index and collapse
  duplicates across sources.

## Distributed crawling

For large sites or many sources, the single-worker frontier would become a
**distributed frontier** with multiple workers, and the extractor — already a pure
function — would run as a separately scaled pool of workers consuming fetched HTML.

## The service adapter

The extraction engine is a pure library, so exposing it over HTTP is a thin
adapter, not a rewrite: a small service with an `/extract` endpoint that takes HTML
and a URL and returns a document object, reusing the engine unchanged. This lets
extraction be called as a service and scaled independently of crawling. It was
deliberately left out of this version because the required deliverable is a CLI;
the clean library boundary is what keeps it cheap to add. See
[architecture.md](architecture.md).

## Broader content coverage

- **Pure client-side-routing single-page apps.** Client-*rendered* sites are
  already handled by the opt-in rendering fetcher (see [crawling.md](crawling.md)),
  which returns the rendered DOM with its links intact. Sites built on pure
  client-side *routing* — where navigation produces no crawlable `<a href>` links
  at all — are still out of reach, because the frontier has nothing to discover.
  Supporting them would require driving the app's in-page navigation rather than
  following links.
- **Authentication-walled content.** Not handled today; would require credential
  and session management in the fetcher.
- **Richer dates and structured data.** `published_at`, `modified_at`, `author`,
  and `extra` are extracted from standard machine-readable sources but are empty on
  sites (like the current sandbox) that do not publish them. On date- and
  structured-data-rich sites the same code populates them; no schema change is
  needed.
- **Multiple co-authors.** The `author` field records a single **primary** author
  (the first declared). Pages with several co-authors are common, and capturing the
  full list would mean promoting `author` to an array — a schema change deferred
  until a consumer needs it. See [enrichment.md](enrichment.md).

## Chunking

Records are whole-page by design; splitting `body_text` into embedding chunks is
left to the consumer, because the right chunk size, overlap, and boundary policy
depend on the consumer's embedding model and retrieval strategy. This is a
deliberate boundary, covered in [data-model.md](data-model.md) — the whole-page
record plus the stable `content_hash` is re-chunkable without re-crawling.

## Summary of deliberate boundaries

| Boundary | Status |
|---|---|
| Client-*rendered* sites | Handled via opt-in `--render` |
| Pure client-side-*routing* SPAs (no crawlable links) | Not handled |
| Authentication-walled content | Not handled |
| Cross-URL / cross-source content deduplication | URL dedup only |
| Richer incremental re-crawl (`ETag`, sitemap `lastmod`) | `If-Modified-Since` only |
| Multiple co-authors | Primary author only |
| Embedding chunking | Left to the consumer |
| Distributed / scheduled crawling | Single-pass, single worker |
| Network service surface | CLI only; adapter is Future Work |
| Populated dates on date-poor sites | `null` where no date exists |
