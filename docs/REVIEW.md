# Corpus review

This document reports what the pipeline actually does on real input. Every number
below is taken from a run's `--stats-json` output (the run telemetry described in
[observability.md](observability.md)) or computed by the analytics tool over the
written JSONL — none of it is hand-tuned. It covers three independent sites plus a
JavaScript-rendered site, the structural-cleaning fix, conditional GET, and the
honest limitations that remain.

## How this was produced

```bash
# Three structurally different sites
scrape_site --start-url=http://books.toscrape.com/                         --max-pages=20 --stats-json=books-stats.json  --output=books.jsonl
scrape_site --start-url=https://quotes.toscrape.com/                       --max-pages=15 --stats-json=quotes-stats.json --output=quotes.jsonl
scrape_site --start-url=https://developer.mozilla.org/en-US/docs/Web/HTML/Element/ --max-pages=15 --stats-json=mdn-stats.json --output=mdn.jsonl

# A JavaScript-rendered site, without and with rendering
scrape_site --start-url=https://quotes.toscrape.com/js/ --include=/js --max-pages=8           --stats-json=qjs-static-stats.json --output=qjs-static.jsonl
scrape_site --start-url=https://quotes.toscrape.com/js/ --include=/js --max-pages=8 --render  --stats-json=qjs-render-stats.json --output=qjs-render.jsonl
```

## Per-site results

| Site | Fetched | Kept | Dropped | New records | Fetch errors |
|---|---:|---:|---:|---:|---:|
| books.toscrape.com | 20 | 19 | 1 | 19 | 0 |
| quotes.toscrape.com | 15 | 9 | 6 | 9 | 0 |
| developer.mozilla.org (HTML element subtree) | 15 | 4 | 11 | 4 | 0 |
| quotes.toscrape.com/js (no `--render`) | 7 | 0 | 7 | 0 | 0 |
| quotes.toscrape.com/js (`--render`) | 7 | 1 | 6 | 1 | 0 |

The pipeline is generic: the same code, with no per-site selectors, produces a
clean product catalog from books.toscrape, prose records from quotes.toscrape, and
long documentation pages from MDN. No run recorded a single fetch error.

## Extraction-layer distribution

Telemetry counts the winning cascade layer over **every processed page** (kept or
dropped); analytics counts it over the **kept corpus** only. A run weighted toward
`semantic`/`library` is extracting cleanly.

| Site | Telemetry (all processed) | Analytics (kept only) |
|---|---|---|
| books | semantic 19, library 1 | semantic 19 |
| quotes | library 14, crude 1 | library 9 |
| MDN | semantic 14, library 1 | semantic 4 |
| quotes/js (`--render`) | library 7 | library 1 |

Every kept body came from the two high-confidence layers (`semantic` or
`library`); no kept record fell through to the `density` or `crude` floor. The
single `crude` page on quotes was a dropped listing, so it never reached the
corpus — visible only in telemetry, which is the point of measuring both.

## Drop-reason histogram

Drops are the keep-gate's two reasons. Every drop in these runs was an `index`
(listing) page — the crawl-vs-keep filter following navigation for its links
without emitting it:

| Site | index | too_short |
|---|---:|---:|
| books | 1 | 0 |
| quotes | 6 | 0 |
| MDN | 11 | 0 |
| quotes/js (no render) | 7 | 0 |

## Structural cleaning: before / after

Navigational furniture that lives *inside* the selected main block — link-list
footers, "related" strips — is removed structurally (link density high **and**
non-link prose low), not by matching wording. On the MDN reference page the change
removes a trailing link-footer while leaving the article intact:

```
page: developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements   (won on the semantic layer)
body word count:  4558  ->  4522     (only the footer furniture removed)

TAIL before:  "...allow 80 characters per line.  See also  Element interface
               Help improve MDN  Learn how to contribute  This page was last
               modified on Feb 6, 2026 by MDN contributors .  View this page on
               GitHub • Report a problem with this content"

TAIL after:   "...recommended that it should be rendered wide enough to allow 80
               characters per line."
```

The body shrinks by ~0.8% — the furniture, not the content. A preserve-this case
is pinned by a test fixture: a legitimately link-heavy content block (a curated
list with real commentary) survives, because its prose clears the floor.

## JavaScript rendering: before / after

quotes.toscrape.com/js builds its quotes with client-side JavaScript, so a plain
HTTP GET returns an empty shell:

```
without --render:  7 /js pages fetched, every one word_count = 0, 0 kept
with    --render:  the same pages now carry their rendered quote text
                   (e.g. the /js landing page: 192 words, kept as content;
                    paginated /js/page/N: 165–513 words each)
```

Rendering flips the JS pages from empty to full of extractable content, with the
engine code unchanged — the renderer is just another source of HTML. The paginated
`/js/page/N` pages are classified `index` (they are listings, the same as the
static site's pagination), so the landing page is the kept record; see Limitations.

## Conditional GET

On a re-crawl the fetcher sends `If-Modified-Since` derived from each page's stored
`modified_at`. Against books.toscrape (which serves `Last-Modified` and honors the
conditional), a warm re-run skips unchanged pages without transferring their
bodies:

```
run 1 (cold): fetched=8 kept=7 insert=7
run 2 (warm): 7 × "304 Not Modified" -> fetch intentional-skips: not_modified=7
              (bodies never transferred; corpus unchanged)
```

## Example records

A product page (books), with a real served `modified_at` and breadcrumb tags:

```json
{
  "url": "http://books.toscrape.com/catalogue/a-light-in-the-attic_1000",
  "title": "A Light in the Attic",
  "author": null,
  "tags": ["Books", "Poetry"],
  "published_at": null,
  "modified_at": "2023-02-08T21:02:32Z",
  "signals": {"word_count": 202, "language": "en", "content_type": "product_page",
              "extraction_layer": "semantic", "is_mostly_code": false}
}
```

A documentation page (MDN), high word count, tags from the breadcrumb trail:

```json
{
  "url": "https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements",
  "tags": ["Web", "HTML", "Reference", "Elements"],
  "signals": {"word_count": 4522, "language": "en", "content_type": "doc_page",
              "extraction_layer": "semantic", "is_mostly_code": false}
}
```

A prose page (quotes), tags lifted generically from the quote metadata:

```json
{
  "url": "https://quotes.toscrape.com/",
  "title": "Quotes to Scrape",
  "tags": ["change", "deep-thoughts", "thinking", "world", "abilities", "..."],
  "signals": {"word_count": 212, "language": "en", "content_type": "other",
              "extraction_layer": "library", "is_mostly_code": false}
}
```

## Limitations

Stated honestly rather than hidden:

- **Link-only cross-reference lists can be over-pruned.** A pure "See also" list of
  links with no surrounding prose clears the structural prune's dual gate and is
  removed. The thresholds are tuned to favor keeping content, and substantial
  bodies are unaffected (the MDN body lost ~0.8%), but a content block that is both
  link-dominated and genuinely thin on prose is a known, documented risk.
- **Conditional GET is keyed on the requested URL.** A site that redirects (for
  example books.toscrape redirects `https`→`http`) stores the record under the
  post-redirect URL, so a re-crawl that starts from the pre-redirect URL computes a
  different id and does not match — the page is re-fetched rather than answered with
  a `304`. Conditional GET fires when the requested and final URLs agree.
- **Paginated content pages are classified as listings.** `…/page/N` URLs are
  treated as `index` even when they carry real (rendered) content, consistent with
  how the static site's pagination is handled. The landing page is kept; deeper
  pages are followed for links but not emitted.
- **Rendering covers client-*rendered* content, not client-side *routing*.** Pages
  whose links are real `<a href>` anchors injected by JavaScript are handled; apps
  whose navigation produces no crawlable links are out of scope (see
  [future-work.md](future-work.md)).
- **A `304` page contributes no link discovery on a warm re-crawl.** Conditional GET
  skips the body of an unchanged page, so its outbound links are not re-discovered.
  This is harmless here because listing/index pages — which drive discovery — are
  never kept, so they carry no stored `modified_at`, are always fully re-fetched, and
  keep re-seeding the frontier. On a site where a *kept* page is the only path to
  others, a changed descendant reachable only through it could be missed on a warm
  run; caching outbound links per URL so `304` pages still contribute discovery is
  future work.
- **Source-level repetition survives cleaning, and it matters for RAG.** The cleaner
  removes boilerplate and link-dense furniture but does not de-duplicate repeated
  *prose*. On the sandbox book pages the description appears twice in the source —
  once as a teaser truncated mid-word and once in full — so both land in `body_text`
  (visible in the example record). This is not merely cosmetic: duplicated text skews
  embeddings and can distort retrieval, which matters for an AI-ready collection. It
  is **deliberately not fixed**, for two reasons. First, it is a property of *this
  sandbox's* markup (the source serves the same description twice); a real
  documentation site or blog does not exhibit it. Second — and decisively — a cleaner
  that detected and stripped this specific teaser-then-full pattern would be a
  **site-specific heuristic**, exactly the overfitting the generic-extraction design
  rejects (see [design-decisions.md](design-decisions.md)): it would help this one
  sandbox while risking the removal of legitimately repeated content elsewhere. A
  *generic*, content-agnostic near-duplicate-passage collapse (e.g. shingling across
  blocks) is the correct production answer and is recorded as future work, rather than
  a one-site patch.
