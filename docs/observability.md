# Observability

This document describes how a run reports what it did: the summary printed at the
end of every crawl, the optional machine-readable statistics file, and the
distributions that let an operator see *where* pages went and *why*. The goal is a
run that explains itself — a reviewer should be able to tell, from the summary
alone, how many pages were kept, how many were dropped and for what reason, and
how much of the corpus came from the high-confidence extraction layers versus the
fallbacks. Observability lives entirely in the dirty orchestration layer; the pure
engine never logs (see the boundary note below).

## The run summary

At the end of every run the crawler prints a summary. It begins with the headline
counts and then three distributions.

**Headline counts.**

- **Pages fetched** — pages actually downloaded and handed to the engine.
- **Pages kept** — documents that passed the quality gate and were written.
- **Pages dropped** — pages fetched but not kept.

The three distributions break those totals down along three independent axes.

### 1. Extraction-layer distribution

How many kept bodies came from each cascade layer:

| Layer | Meaning |
|---|---|
| `semantic` | Semantic HTML5 (`main` / `article` / `role=main`). |
| `library` | The bought main-content extractor. |
| `density` | The text-to-link density heuristic. |
| `crude` | The crude floor fallback. |

A run weighted toward `semantic` and `library` is extracting cleanly; a run
leaning on `density` and `crude` is a signal that the target site's markup is
unusual or has drifted. This distribution is sourced from the
`extraction_layer` the engine records on each result (see
[extraction.md](extraction.md) and [data-model.md](data-model.md)).

### 2. Drop-reason histogram

Why fetched pages were *not* kept. The keep decision is the quality gate in
[enrichment.md](enrichment.md), so the reasons mirror it:

| Reason | Meaning |
|---|---|
| `index` | Filtered as a listing / index page (`content_type == "index"`). |
| `too_short` | Below the minimum word count. |

### 3. Fetch-outcome counts

What happened to each fetch attempt. This axis keeps **two genuinely different
kinds of outcome separate**, because conflating them would make the numbers lie —
a page intentionally skipped for being non-HTML is not a failure, and counting it
as one would understate how healthy a run was.

| Outcome | Kind |
|---|---|
| `timeout` | Error |
| `connection_error` | Error |
| `http_4xx` | Error |
| `http_5xx` | Error |
| `rate_limited` | Error |
| `robots_disallowed` | Intentional skip |
| `non_html` | Intentional skip |
| `oversized` | Intentional skip |
| `not_modified` | Intentional skip |

**Errors** are things that went wrong (the origin timed out, refused, or
misbehaved). **Intentional skips** are things the crawler chose not to fetch or
process by policy (disallowed by `robots.txt`, not HTML, too large, or unchanged
since the last crawl). The outcomes come directly from the typed fetch reasons in
[crawling.md](crawling.md).

## Machine-readable statistics: `--stats-json`

The same statistics that appear in the printed summary can be written to a file as
JSON with `--stats-json <path>` (see [configuration.md](configuration.md)). The
file is written **atomically** — to a temp file, then moved into place — so a
crash mid-write never leaves a half-written stats file, mirroring the output
write in [storage-and-idempotency.md](storage-and-idempotency.md).

This exists so a corpus review can ingest a run's statistics directly, without
scraping them back out of log text. Logs remain a human-readable audit trail; the
stats file is the structured counterpart.

## Where the numbers come from: the pure/dirty boundary

The reporting respects the same boundary as the rest of the system (see
[architecture.md](architecture.md)). The **pure engine never logs and never
counts**. It records the winning cascade layer as **data on its result object**
and nothing more. The **dirty crawler** is what aggregates: it tallies the
extraction layers off the results, plus its own drop reasons and fetch outcomes,
and emits the summary. Keeping the counting in the orchestration layer is what
lets the engine stay a pure, side-effect-free library that is unit-testable on
static fixtures.

## Two instruments: telemetry vs analytics

There are two separate measurement tools, and they measure different populations.
Confusing them gives misleading numbers, so the distinction is explicit:

| Instrument | Measures | Population |
|---|---|---|
| **Telemetry** (this document) | The run summary / stats file emitted by the crawler. | **Run-state** — every fetched page, *including dropped ones*. |
| **Analytics** ([the analytics tool](architecture.md)) | Corpus statistics computed by reading the JSONL deliverable. | **Deliverable-state** — only the *kept* pages. |

The analytics tool reads the finished JSONL and reports, among other things, the
**extraction-layer distribution over the kept corpus** — the same axis as
telemetry's layer distribution, but restricted to pages that were actually
emitted. Telemetry answers "what did this run do?" (kept and dropped); analytics
answers "what is in the deliverable?" (kept only). Both report extraction layers,
but over different populations, and a reviewer should read each as such.
