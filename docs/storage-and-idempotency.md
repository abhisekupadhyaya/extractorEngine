# Storage and idempotency

This document describes how documents are persisted and how the pipeline
guarantees that re-runs do not produce duplicate or stale records. There are two
separate layers: the **deliverable** (a JSONL file) and an **internal state
store** keyed by document id. Idempotency itself is two distinct problems —
avoiding duplicate fetches *within* a run, and avoiding duplicate or stale records
*across* runs — and the design keeps them separate. Storage is pluggable: the
default requires zero infrastructure, and optional backends activate only when
their environment variables are set.

## Two layers: deliverable vs state

- **Deliverable layer = JSONL.** The output is a JSONL file — one document object
  per line, conforming to the schema in [data-model.md](data-model.md). This is
  the artifact a downstream team consumes.
- **State layer = a store keyed by `id`.** The pipeline maintains state keyed on
  `id` (= `uuid5(canonical_url)`) so it can recognize a page it has seen before
  and decide whether to insert, skip, or update. In the default backend this
  state is reconstructed from the existing output file; optional backends can hold
  it externally.

These are deliberately separate. A database, where used, serves as **crawl
state** for resumability and production-mindedness — not as the deliverable. The
deliverable is always the JSONL.

## Why JSONL, not a single JSON array

The deliverable is JSON Lines — one document object per line — rather than one
big JSON array wrapping all records. The reasons are all about how a downstream
consumer reads it:

- **Streamable / bounded memory.** A reader processes one record per line and
  never has to parse or hold the whole file at once, so memory stays flat no
  matter how large the corpus grows. A single JSON array forces a whole-file parse
  before the first record is available.
- **Append- and bulk-indexer-friendly.** New records are appended as lines, and
  the line-per-document shape is exactly what bulk indexers and dataset loaders
  expect to ingest.
- **Crash-resilient at line granularity.** If a file is truncated, only the last
  partial line is lost; every complete line before it is still valid and usable. A
  truncated JSON array is unparseable in its entirety.

The trade-off, stated honestly: a JSONL file is **not itself a single valid JSON
document**, so a consumer must use a line-oriented reader rather than handing the
whole file to a JSON parser. For a corpus meant to be streamed into an AI workflow
that trade is worth it.

## Idempotency: two distinct problems

### 1. Within a run — crawler dedup

A `seen` set of **canonical URLs** ensures the crawler never enqueues or fetches
the same page twice in one run. This is what satisfies "avoid duplicate pages",
and it needs no content hash — URL identity is enough. See URL canonicalization
in [crawling.md](crawling.md); the canonical string is the seen-set key and also
the basis of `id`.

### 2. Across runs — insert / skip / update

When a previously-seen page is encountered on a later run, the action is decided
by comparing `content_hash` (= `sha256(body_text)`):

| On encounter | id seen before? | content_hash | Action |
|---|---|---|---|
| New page | No | — | **insert** |
| Re-crawl, unchanged | Yes | same | **skip** |
| Re-crawl, changed | Yes | differs | **update** (replace; `fetched_at` refreshes) |

Because a changed page keeps the **same `id`** (same URL → same `uuid5`) but a
**different `content_hash`**, the natural outcome is an upsert: unchanged pages
skip, changed pages replace. This is what makes re-running the tool against the
same site produce **zero new records** when nothing has changed — the
definition-of-done check for idempotency.

### Conditional GET sits in front of this

On a re-crawl the fetcher can short-circuit a page before it is ever downloaded:
it looks up the page's previously stored `modified_at` from the existing output
state and sends `If-Modified-Since`. A `304 Not Modified` means the body is
unchanged and is never transferred — the page is treated as a skip without
re-extracting or re-hashing it. This is a network optimization layered **in front
of** the insert/skip/update decision above; it does not replace it. A page that
returns `200` still flows through the `content_hash` comparison as normal. See
[crawling.md](crawling.md).

## Semantics worth noting

- **`content_hash` is computed over the *cleaned* `body_text`.** A change to the
  cleaning logic itself therefore changes the hash and will re-write an otherwise
  unchanged page as an update; hashes are comparable only within a fixed
  extraction version.
- **`fetched_at` is not refreshed on a skip.** When an unchanged page is
  re-crawled it is skipped and its record is left untouched, so `fetched_at` means
  *when the current version of the content was captured*, not *when the page was
  last visited*.

## The default JSONL store (zero-infra)

The default `JSONLStore` requires no database and no object storage:

1. **Seed state from the existing file.** On start, if the output file exists, it
   is read into an in-memory map `{ id: (content_hash, doc) }`.
2. **Apply the three-case decision** per document as it arrives (insert /
   skip-if-hash-same / update-if-hash-differs).
3. **Write once, atomically, at the end.** The full result is written to a temp
   file and then moved into place with an atomic replace. A crash mid-write
   therefore never leaves a half-written or corrupt output file — the previous
   file stays intact until the new one is complete.

This whole-file rewrite holds the corpus in memory and suits the single-site,
bounded-page scope here; for very large corpora it would be replaced by a
streaming/append writer or a database backend (see
[future-work.md](future-work.md)).

## Pluggable backends

Backends are selected by a small factory off the settings object. The default is
the zero-infra JSONL store; optional backends activate **only when their
environment variables are present**, so there is no hard infrastructure
dependency.

| Backend | Activated by | Role |
|---|---|---|
| JSONL (default) | (always) | The deliverable; holds state in-file; zero infra. |
| Postgres | `POSTGRES_DSN` | State store with `UPSERT` on `id` — resumable, production-style crawl state. |
| Object storage | `MINIO_*` | Stores raw HTML for provenance. |

When none of the optional variables are set, the pipeline runs as pure JSONL with
no external services — the default path is intentionally the simplest one. See
[configuration.md](configuration.md) for the exact variables.

## Cross-URL content duplicates

Two different URLs that produce identical `body_text` (and therefore the same
`content_hash`) are **not** currently deduplicated against each other. URL-based
deduplication satisfies the requirement to avoid duplicate pages; catching
identical content under different URLs would require a separate
`content_hash → id` index that complicates the upsert path. This is left as
Future Work — see [future-work.md](future-work.md).
