# Data model — the AI document object

This document defines the single data contract the pipeline produces: the **AI
document object**. One object is emitted per kept page, serialized as one line of
JSONL. The schema is designed so that every field answers a concrete downstream
decision — a consumer building RAG, search, fine-tuning, or analytics can filter,
rank, and embed records directly off these fields without re-fetching or
re-parsing the source page. The model is the authority on shape: the documented
JSON Schema is generated from the code, so the two can never drift.

## Shape: hybrid (identity flat, signals nested)

The object is **hybrid**. Identity and extracted-content fields live at the top
level; derived quality signals are grouped under a nested `signals` block.

```jsonc
{
  "id":           "f1e2d3c4-...-uuid5",                  // stable identity: uuid5 of the canonical URL
  "url":          "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
  "title":        "A Light in the Attic",                // resolved by a precedence cascade
  "body_text":    "clean main content, no nav/footer",   // extracted + cleaned
  "tags":         ["Books", "Poetry"],                   // standard web sources only; [] if none
  "published_at": null,                                  // ISO8601 UTC, or null if no date exists
  "modified_at":  null,                                  // ISO8601 UTC, or null
  "fetched_at":   "2026-06-14T12:00:00Z",                // when we scraped it (required)
  "content_hash": "sha256-hex-of-body_text",             // change detection across re-crawls
  "signals": {
    "word_count":     412,
    "char_count":     2380,
    "language":       "en",                              // ISO 639-1, or "und"
    "content_type":   "product_page",                    // controlled vocabulary (see below)
    "is_mostly_code": false
  },
  "extra": { }                                           // optional structured bag; {} if none
}
```

Nesting `signals` is a deliberate contract choice, not a performance one (each
JSONL record loads whole, so nested access is a free dict lookup). The reasons
are: the contract is more legible with derived metrics visually separated from
identity; the `signals` block can be versioned independently without disturbing
the identity contract; and a consumer can grab the whole "filter panel" as one
semantic unit.

## Field reference

| Field | Type | Required / Nullable | Meaning | Downstream decision it enables |
|---|---|---|---|---|
| `id` | string (UUID) | Required | `uuid5` of the canonical URL. Same URL always yields the same id. | Dedupe / upsert key (idempotency). |
| `url` | string | Required | The canonical URL of the page (the exact string hashed for `id`). | Provenance; re-fetch. |
| `title` | string | Required (may be `""`) | Page title, resolved by precedence cascade. | The content itself (search, RAG, training). |
| `body_text` | string | Required | Clean main content, with navigation, header, sidebar, and footer removed. | The content itself (search, RAG, training). |
| `tags` | string[] | Required (may be `[]`) | Topical labels from standard web sources. | Topical filtering / ranking (e.g. "all Poetry docs"). |
| `published_at` | string \| null | Nullable | Publication timestamp, ISO8601 UTC, or `null` if none exists. | Recency ranking. |
| `modified_at` | string \| null | Nullable | Last-modified timestamp, ISO8601 UTC, or `null`. | Recency ranking. |
| `fetched_at` | string | Required | When the current version of the content was captured (not refreshed on an unchanged re-crawl); tz-aware UTC, ISO8601 with `Z`. | Freshness / audit. |
| `content_hash` | string | Required | `sha256` hex of `body_text`. | Detect whether a page changed since last crawl. |
| `signals.word_count` | int ≥ 0 | Required | Whitespace-delimited token count of `body_text`. | Drop stubs; length-filter the corpus. |
| `signals.char_count` | int ≥ 0 | Required | Character length of `body_text`. | Drop stubs; length-filter the corpus. |
| `signals.language` | string | Required | ISO 639-1 code, or `"und"` when undetermined. | Route to the right model; filter the corpus. |
| `signals.content_type` | enum | Required | Page kind (controlled vocabulary). | Filter corpus by page kind. |
| `signals.is_mostly_code` | bool | Required | Whether the page is predominantly code. | Include/exclude for prose-vs-code training. |
| `extra` | object | Required (may be `{}`) | Optional structured attributes (e.g. from JSON-LD). | Optional structured attributes (price, rating, ...). |

## Controlled vocabulary: `content_type`

`content_type` is a closed enum so that a consumer's filter never silently misses
a typo'd or unexpected variant. An unfamiliar page is classified as `other`
rather than crashing the model or inventing a new label.

| Value | Meaning |
|---|---|
| `product_page` | A catalog leaf describing one item (e.g. a book detail page). |
| `doc_page` | A documentation page. |
| `article` | A prose article / blog post. |
| `index` | A listing, category, search, or pagination page — links, little prose. |
| `other` | Mandatory fallback for anything the rules do not match. |

Classification rules are described in [enrichment.md](enrichment.md). Note that
`index` pages are generally **crawled but not kept** (see the quality gate in
[enrichment.md](enrichment.md) and the crawl-vs-keep filter in
[crawling.md](crawling.md)), so they rarely appear in output.

## Missing vs empty

The model distinguishes "this collection has no members" from "this scalar has no
value", so consumers can rely on a consistent convention:

- **Collections default to empty, never null.** `tags` is `[]` and `extra` is
  `{}` when nothing is found. A consumer can always iterate without a null check.
- **Genuinely optional scalars default to null.** `published_at` and
  `modified_at` are `null` when no such date exists. `null` here means "no date
  exists", which is distinct from an empty string.
- **`title` may be `""`** when even the slug-derived last resort yields nothing
  usable; it is still a required string, never null.
- **`language` is never null**; it is `"und"` when detection is not possible
  (for example, text too short to classify reliably).

## Why some plausible fields are excluded

The schema deliberately omits fields that a consumer can derive from what is
already present, or that no consumer has a distinct action on:

- **`reading_time`** — derivable downstream as `word_count / 200`. Its input
  survives into the record, so it can be computed later; keeping it out avoids a
  redundant field.
- **`quality_score`** — no consumer named an action that differs between, say,
  0.7 and 0.6. The raw signals are kept so each consumer can compose its own
  score.
- **`source`** — derivable from the `url` domain; redundant.

The guiding rule is "does the input survive into the record?" If a value can be
recomputed from fields that are present, it is left out. `is_mostly_code` is the
instructive counter-example: its input (HTML `<pre>`/`<code>` density) does **not**
survive into `body_text`, so it must be computed at extraction time and stored.

## Example record

A real `books.toscrape.com` product page produces a record of this shape (line
breaks added for readability; in the file it is a single line):

```json
{
  "id": "5f2b9c1e-7a4d-5e6f-8b9a-0c1d2e3f4a5b",
  "url": "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
  "title": "A Light in the Attic",
  "body_text": "A Light in the Attic\n\nIt's hard to imagine a world without A Light in the Attic. This now-classic collection of poetry and drawings from Shel Silverstein celebrates its 20th anniversary ...",
  "tags": ["Books", "Poetry"],
  "published_at": null,
  "modified_at": null,
  "fetched_at": "2026-06-14T12:00:00Z",
  "content_hash": "9b74c9897bac770ffc029102a200c5de...",
  "signals": {
    "word_count": 187,
    "char_count": 1124,
    "language": "en",
    "content_type": "product_page",
    "is_mostly_code": false
  },
  "extra": {}
}
```

On this sandbox site `published_at`/`modified_at` are `null` and `extra` is `{}`,
because the source pages carry no machine-readable dates or JSON-LD. The fields
exist regardless so that the same code produces richer records on date- and
structured-data-rich sites without schema changes.
