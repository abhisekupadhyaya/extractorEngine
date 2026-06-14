# Future work

This document describes how the pipeline would evolve toward a continuously
operated production collector. Everything here is **out of scope for this
version** and is recorded as a deliberate boundary, not an omission. The current
version is a single-pass, single-site, static-HTML scraper that produces a clean
JSONL collection; the items below are the natural next increments, most of which
are cheap precisely because the engine is already a pure library with a single
data contract.

## Incremental re-crawling

Today a re-run fetches every page and decides insert/skip/update from the
`content_hash` after extraction. A production collector would avoid downloading
unchanged pages at all:

- **Conditional GET** using `If-Modified-Since` / `ETag`, so the origin returns
  `304 Not Modified` and the body is never transferred.
- **Sitemap `lastmod`** to discover what changed since the last crawl and visit
  only those URLs.

These sit *in front of* the existing insert/skip/update logic; they make re-crawls
cheaper, they do not replace the idempotency model in
[storage-and-idempotency.md](storage-and-idempotency.md).

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

- **JavaScript-rendered / single-page sites.** This version handles static HTML
  only. Sites that render content client-side would need a headless browser to
  produce the HTML before extraction; the engine downstream would be unchanged.
- **Authentication-walled content.** Not handled today; would require credential
  and session management in the fetcher.
- **Richer dates and structured data.** `published_at`, `modified_at`, and
  `extra` are extracted from standard machine-readable sources but are empty on
  sites (like the current sandbox) that do not publish them. On date- and
  structured-data-rich sites the same code populates them; no schema change is
  needed.
- **Main-content-node extraction + chunk-level cleaning.** The extractor currently
  consumes the extraction library's *flattened text*, so cleanup that needs DOM
  structure (e.g. removing a related-content carousel by element, or separating a
  teaser that is glued mid-word onto a duplicated full description) can't reach it
  — heading/word-gated heuristics handle the common cases, but some intra-block
  duplication survives. Extracting the main-content **node** (HTML), then applying
  the structural cleaner, and de-duplicating at chunk boundaries downstream, is the
  robust fix.

## Summary of deliberate v1 boundaries

| Boundary | Status |
|---|---|
| JavaScript-rendered / SPA sites | Static HTML only |
| Authentication-walled content | Not handled |
| Cross-source content deduplication | URL dedup only |
| Intra-page duplicated text (teaser + full) | Heuristically trimmed; some residue |
| Distributed / scheduled crawling | Single-pass, single worker |
| Network service surface | CLI only; adapter is Future Work |
| Populated dates on date-poor sites | `null` where no date exists |
