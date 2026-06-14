# Crawling

This document describes how the pipeline discovers pages: the breadth-first
frontier and its bounds, the URL canonicalization rule that underpins identity
and deduplication, the two filters that decide what to crawl and what to keep,
the politeness policy (including `robots.txt`), the typed fetch outcomes and
error-handling taxonomy that guarantee one bad page never crashes a run,
conditional GET on re-crawls, and the static and rendering fetcher modes. Crawling
is part of the dirty orchestration layer; it touches the network and crawl state,
and it hands raw HTML to the pure extraction engine.

## Frontier: breadth-first from the seed

The crawler walks the site **breadth-first (BFS)** from the seed URL, using a
queue plus a depth counter.

BFS is chosen over depth-first because, under a fixed page budget, the pages
nearest the seed are the most relevant ones, and BFS spends the budget on a broad
band of relevant content. Depth-first can pour the entire budget down a single
deep branch before it ever sees breadth. A queue with a depth counter also bounds
the crawl cleanly.

**Intra-layer ordering: content before listings.** Within a single depth layer,
the frontier visits content-looking URLs before listing-looking ones (a path that
matches the same `category` / `tag` / `page-N` / `search` shape used elsewhere is
ranked second). The reason is concrete: a seed home page typically links its whole
category sidebar *and* its content leaves at the same depth, with the sidebar
first in source order. Under a small `--max-pages` budget, naive insertion order
would spend the entire budget on listing pages — which are crawled-but-not-kept —
and emit nothing. Ranking content first means a bounded budget is spent on
emittable pages, directly serving the "broad band of relevant content" goal above.
The crawl is still breadth-first by depth; this is only a tiebreak *within* a
depth. The insertion sequence is the final tiebreak, so ordering is fully
deterministic and re-runs stay idempotent.

### Bounds

Two independent bounds, with different jobs:

| Bound | Flag | Default | Role |
|---|---|---|---|
| Max pages | `--max-pages` | 100 | Hard stop. A resource circuit-breaker: the crawl never fetches more than this many pages, full stop. |
| Max depth | `--max-depth` | 5 | Relevance bound. Limits how far from the seed the crawl wanders. |

Max-pages is the circuit breaker; max-depth is the relevance knob. Both are
honored; whichever is reached first ends (or prunes) the crawl.

## URL canonicalization

Every discovered URL is reduced to a single **canonical** form before anything
else happens to it. The canonical string is what gets hashed into the document
`id` (`uuid5(canonical_url)`) and what fills the seen-set. This is critical:
without it, `/p`, `/p#section`, and `/p?ref=email` would produce three different
ids and therefore three duplicate records for one page — the no-duplicates
guarantee would break at the front door, before any deduplication logic even runs.

The rule is **ordered**, and order matters:

1. **Lowercase the scheme and host**, and strip a leading `www.` (treat `www.`
   and the bare host as the same site).
2. **Drop the `#fragment`** — fragments address positions within a page, not
   distinct pages.
3. **Normalize query parameters with a denylist, not strip-all.** Drop keys that
   match the `utm_*` prefix or a small tracking denylist (`ref`, `referrer`,
   `gclid`, `fbclid`, `msclkid`, `mc_eid`, `_ga`, `igshid`, `sessionid`,
   `phpsessid`, ...). **Keep every other parameter**, then **sort the remaining
   parameters by key** so that equivalent URLs hash identically regardless of
   parameter order.
4. **Normalize the default document and trailing slash.** Strip default index
   documents (`index.html`/`htm`/`php`, `default.html`/`htm`) down to the
   directory root, and strip a trailing slash except on the bare root `/`.

Then the canonical string is reassembled and used for the seen-set and the `id`.

### Why a denylist, not strip-all

Stripping *all* query parameters would work on a purely path-based site, but it
collapses genuinely distinct pages on any query-param-driven catalog: `?id=1000`
and `?id=1001` would become the same canonical URL and therefore the same record.
A denylist removes only known tracking noise, behaves identically to strip-all on
path-based sites, and stays correct on query-param sites — at the cost of about
ten strings. This keeps canonicalization consistent with the project's generic,
site-agnostic stance rather than overfitting to one sandbox.

## Link discovery

After a page is fetched, the crawler parses its `<a href>` links, resolves each
relative URL against the page's own URL, canonicalizes it (the rule above), and
applies the scope filter. URLs that are in scope and not already in the seen-set
are enqueued at depth + 1. Link discovery runs for **every** fetched page,
including pages that are crawled but not kept — an index page is followed for its
links even though it is never emitted as a document.

## Two filters: scope, and crawl-vs-keep

These are **two distinct decisions** and are deliberately not fused.

### 1. Scope filter — "is this our site?"

Decides whether to crawl a URL at all.

- In scope: the **exact netloc** of the seed, with `www.` treated as an alias.
- **Subdomains are not auto-included.** `blog.` and `shop.` can host entirely
  different content and are excluded unless explicitly opted in.
- **Optional path filter:** `--include` / `--exclude` accept a path regex (for
  example, restrict the crawl to `/catalogue/` or `/docs/`).

Out-of-scope and externally-linked URLs are simply not enqueued.

### 2. Crawl-vs-keep filter — "do we emit this page as a document?"

Decides, separately from whether we follow a page's links, whether the page is
**kept** (emitted as a document object).

- **Index / listing / login / search pages:** crawl-maybe, **keep-no**. They are
  navigational — links with little prose — and emitting them would poison
  downstream embeddings. We may still follow their links to discover content, but
  we do not keep them.
- **Leaf content pages:** **keep-yes**.

The keep decision is realized as the **quality gate** in
[enrichment.md](enrichment.md), and it is informed by the `content_type` the
engine assigns. A page can therefore be crawled for its links and still excluded
from the output.

## Politeness

The crawler is built to behave like a well-mannered bot:

- **Fixed, configurable delay** between requests (`--delay`, default ~0.5s), to
  avoid hammering the origin.
- **A single worker** — no concurrency beyond what the task needs.
- **An honest bot User-Agent** (`--user-agent`), identifying the crawler.
- **Respect for `robots.txt` and `Crawl-delay`.** The `robots.txt` is fetched
  once per host and cached (via the standard library robots parser). Disallowed
  URLs are skipped and logged. If the **seed itself is disallowed**, the crawler
  logs an error and exits rather than proceeding. An `--ignore-robots` escape
  hatch exists for cases where the operator has authority to override (for
  example, a site they own).

On the chosen sandbox site this policy is effectively a no-op, but it is present
deliberately: correct robots handling is part of being production-minded.

## Non-HTML and request limits

- **Binary and non-HTML resources** are filtered out two ways: the frontier
  pre-filters obvious binary extensions, and the fetcher treats the response
  `Content-Type` as authoritative — it processes only `text/html` and
  `application/xhtml+xml`, skipping and logging anything else.
- **Encoding** is left to the parser: raw bytes are handed to the HTML library,
  which sniffs the charset. There is no custom decode logic to get wrong.
- **Request timeout** defaults to 10s (configurable).
- **Optional response-size cap** (`max_page_bytes`, ~5MB) streams the response
  and aborts with a log entry if the page is implausibly large.

## Conditional GET

On a re-crawl, the fetcher avoids downloading pages that have not changed. Before
requesting a URL it looks up that page's previously stored `modified_at` from the
existing output state and, when one exists, sends an `If-Modified-Since` header
derived from it. If the origin replies `304 Not Modified`, the page is unchanged:
its body is never transferred, and the fetch resolves to a `not_modified` skip
(an intentional skip, not an error — see [observability.md](observability.md)).

Conditional GET sits **in front of** the insert/skip/update idempotency logic in
[storage-and-idempotency.md](storage-and-idempotency.md); it makes a re-crawl
cheaper by not fetching unchanged bodies, but it does **not** replace that logic.
A page that returns `200` still flows through the usual `content_hash` comparison.
It is enabled by default and can be turned off (see
[configuration.md](configuration.md)); richer conditional variants (`ETag`,
sitemap `lastmod`) remain future work — see [future-work.md](future-work.md).

## Fetcher modes: static and rendering

The fetcher comes in two forms behind a common base. The base owns all the shared
behavior described above — politeness delay, `robots.txt`, retry and backoff,
throttle, the typed-reason taxonomy, conditional GET. The two concrete fetchers
differ **only in how they load a URL**:

- **Static HTTP fetcher (default).** Issues an HTTP `GET` and returns the response
  bytes. This is the default mode and needs no extra dependencies.
- **Rendering fetcher (opt-in).** Drives a headless browser and returns the page's
  **rendered DOM** as HTML, for sites whose content is injected by client-side
  JavaScript. Selected with `--render` (see [configuration.md](configuration.md)),
  it ships as an optional install extra and a separate container image, and is
  bounded by a render timeout (`--render-timeout`).

The **extraction engine is unchanged by the choice of fetcher**: the renderer
simply produces HTML the same way the static fetcher does, and the pure engine
extracts from that HTML without knowing which fetcher loaded it. This is the same
pure-core / dirty-orchestration boundary the rest of the system follows.

**Scope.** Rendering handles client-**rendered content** — pages where the markup
is built by JavaScript but the links are still real `<a href>` anchors the crawler
can discover. Sites built on pure client-side **routing**, where navigation
produces no crawlable links at all, are not covered and remain future work — see
[future-work.md](future-work.md).

## Typed fetch outcomes

The fetcher does not return a bare page-or-`None`. Every fetch resolves to one of
two outcomes: a **fetched result** (the bytes plus response metadata), or a
**typed skip carrying a reason**. The reason is a value drawn from a closed set:

| Reason | Kind |
|---|---|
| `robots_disallowed` | Intentional skip |
| `non_html` | Intentional skip |
| `oversized` | Intentional skip |
| `not_modified` | Intentional skip |
| `timeout` | Error |
| `connection_error` | Error |
| `http_4xx` | Error |
| `http_5xx` | Error |
| `rate_limited` | Error |

Carrying the reason as a typed value rather than a log line is what lets the
telemetry layer count outcomes precisely and keep **genuine errors** separate from
**intentional skips** — see [observability.md](observability.md). One bad page
never crashes the run: the reason flows up as data and the loop continues.

## Error-handling taxonomy

The error reasons above are produced by a **taxonomy**, not a single bare
`try/except`. The guiding invariant is that one bad page never crashes the run,
and every skip is also logged with the URL and the reason.

| Condition | Action | Reason |
|---|---|---|
| Timeout / connection error | Retry with backoff, then skip. | `timeout` / `connection_error` |
| `429 Too Many Requests` | Back off and honor the `Retry-After` header, then skip. | `rate_limited` |
| `5xx` server error | Retry with backoff, then skip. | `http_5xx` |
| `4xx` / `404` | Do **not** retry; skip immediately. | `http_4xx` |

The distinction between `5xx` (transient — worth a retry) and `4xx` (the resource
is genuinely absent or forbidden — retrying is wasted effort) is the point of the
taxonomy. Skips are logged at `WARNING` with the URL and reason, so a run's log is
a usable audit of what was dropped and why.
