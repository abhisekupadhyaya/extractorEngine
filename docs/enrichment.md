# Enrichment

This document describes how a freshly extracted document is enriched with the
derived signals and metadata that make it usable in an AI collections workflow:
the counts, language, content-type, code detection, tags, dates, and the optional
structured `extra` bag — and the **quality gate** that decides whether the
document is kept at all. Enrichment is pure: it takes a document and returns an
enriched document, with no I/O. Every rule is best-effort and null/empty-safe, so
a missing or malformed source never raises.

## Counts

Computed directly from the cleaned `body_text`:

- `word_count = len(body_text.split())` — whitespace-delimited tokens.
- `char_count = len(body_text)` — character length.

These drive corpus length-filtering and the quality gate below.

## Language

`language` is detected from `body_text` and reported as an ISO 639-1 code.

- Detection runs **only when `word_count >= 20`**; below that there is too little
  text to classify reliably. This floor concerns detector reliability and is
  intentionally lower than the quality gate's `>= 25` keep threshold (below):
  because the gate is stricter, every *emitted* document has had a genuine
  detection attempt, while the lower floor simply avoids guessing on trivially
  short text.
- The detector is wrapped in a guard: any failure (or text below the threshold)
  yields the fallback `"und"` ("undetermined").
- `language` is therefore **never null** — it is always a code or `"und"`.

For deterministic results across runs and across test machines, the language
detector is seeded in the test configuration.

## Content-type classification

`content_type` is assigned by a **first-match-wins** rule cascade into the closed
vocabulary defined in [data-model.md](data-model.md):

| Order | If the page... | Then `content_type` = |
|---|---|---|
| 1 | has JSON-LD `@type=Product`, or a price/availability element | `product_page` |
| 2 | has a `/docs/`-style path and an article-like body | `doc_page` |
| 3 | has an `<article>` element or an `article:published_time` | `article` |
| 4 | is link-dense / a listing or pagination URL | `index` |
| 5 | matches none of the above | `other` |

`other` is the mandatory fallback so that an unfamiliar page is classified rather
than crashing the model. The `index` classification feeds the quality gate: index
pages are crawled for their links but generally not kept.

## Code detection (`is_mostly_code`)

`is_mostly_code` is `true` when the ratio of code-element text to total text
exceeds a threshold:

```
code_char_ratio = chars inside <pre> and <code> / total chars
is_mostly_code  = code_char_ratio > 0.5
```

This is computed **at extraction time, while the HTML markup is still in hand**,
because its input — the `<pre>`/`<code>` markup — does not survive into the
cleaned `body_text` and could not be recomputed later. The signal lets a consumer
include or exclude a page for prose-versus-code training.

## Extraction layer (`extraction_layer`)

`signals.extraction_layer` records **which cascade layer produced `body_text`** —
`semantic`, `library`, `density`, or `crude`. The value is set by the extractor
(see [extraction.md](extraction.md)) and carried straight onto the signals block;
enrichment does not recompute it. It is a **consumer confidence signal**: a body
from the `semantic` or `library` layer is higher-confidence than one from the
`density` heuristic or the `crude` floor, so a consumer can filter or down-weight
on it. The raw layer name is stored rather than a derived score, so the basis for
trust stays transparent.

## Tags

`tags` are gathered from **standard web sources only** — never site-specific
selectors — so the same logic generalizes across sites. Sources are tried in
order, and the results are deduped and trimmed:

1. Breadcrumb navigation (schema.org `BreadcrumbList` or a breadcrumb nav).
2. `<meta name="keywords">` (comma-split).
3. The extraction library's parsed tags.
4. OpenGraph `article:tag` / `section`.
5. JSON-LD `keywords` / `genre`.

If no source yields anything, `tags` is `[]`. On the sandbox book pages, the
breadcrumb produces tags such as `["Books", "Poetry"]`.

## Dates

`published_at` and `modified_at` are gathered from **declared** and **served**
date sources only, tried in order, and parsed to **tz-aware UTC** ISO8601:

1. JSON-LD `datePublished` / `dateModified`.
2. OpenGraph `article:published_time` / `article:modified_time`.
3. A `<time datetime="...">` element (for `published_at`).
4. The HTTP `Last-Modified` response header (for `modified_at`).

If no source yields a parseable date, the field is `null` — meaning "no date
exists", which is distinct from an empty value.

**Declared and served, never guessed.** The extraction library can additionally
*guess* a date from page content, but that source is deliberately rejected. On a
site with no real per-document dates the heuristic emits a single site-wide date
stamped onto every record — which is worse than `null`, because it fabricates a
recency signal a downstream ranker would act on. Only dates a page genuinely
*declares* (JSON-LD, `article:*_time`, `<time>`) or a server genuinely *serves*
(`Last-Modified`) are accepted; an honest `null` is preferred to a manufactured
one.

On the sandbox `books.toscrape.com` this means `published_at` is `null` — the
pages declare no standard publication date — while `modified_at` is populated from
the `Last-Modified` response header. The same code populates `published_at` on a
site that declares a real one, with no schema change either way.

## Author

`author` is a **top-level, nullable** field — metadata about the page, parallel to
`published_at` / `modified_at`, not a derived signal — so it lives at the top
level of the record and not under `signals` (see [data-model.md](data-model.md)).

It is sourced by a **generic cascade**, most-authoritative first, and the first
source that yields a name wins:

1. JSON-LD `author` — the `name` of a `Person` or `Organization` (the first entry
   if `author` is a list).
2. OpenGraph `article:author`.
3. `<meta name="author">`.
4. The extraction library's parsed author.

As with tags and dates, only **standard, generic** sources are read — never
site-specific selectors — so the same logic generalizes across sites. When no
source declares an author, `author` is `null` (meaning "no author declared",
distinct from an empty string).

For a multi-author page only the **primary (first) author** is recorded;
capturing all co-authors is noted as future work (see
[future-work.md](future-work.md)).

## The `extra` bag

`extra` holds optional structured attributes lifted from JSON-LD when present
(price, rating, and similar). It is kept as an open bag rather than promoted into
first-class fields, deliberately: doing so avoids building a product-specific
schema and keeps the model generic. When no structured data is present, `extra`
is `{}`.

## The quality gate

The quality gate is where the crawl-vs-keep decision from [crawling.md](crawling.md)
is executed. It decides whether a document is **emitted** at all:

```
keep(doc) = (word_count >= 25) AND (content_type != "index")
```

- **`word_count >= 25`** drops stubs and near-empty shells that would dilute the
  corpus.
- **`content_type != "index"`** drops listing, category, search, and pagination
  pages — they are crawled for their links but contain navigation rather than
  content, and emitting them would poison downstream embeddings.

The floor is configurable. The governing principle is **fewer-clean over
more-dirty**: it is better to emit a smaller corpus of trustworthy documents than
a larger one a consumer cannot trust. A dropped page is logged; only documents
that pass the gate are written to the output.

## Metadata source, single parse

All of the metadata above (tags, dates, language as parsed by the library) is read
from a **single** invocation of the extraction library per page, independent of
which body-extraction layer won the cascade in [extraction.md](extraction.md).
Body selection and metadata extraction are separate decisions off the same parse —
keeping the work DRY and ensuring metadata is available even when body extraction
falls through to a non-library layer.
