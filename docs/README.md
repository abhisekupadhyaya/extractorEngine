# Documentation

This directory contains the design and reference documentation for the scraping
pipeline — a command-line tool that crawls a single public website and turns it
into a collection of clean, schema-consistent, metadata-rich JSON documents that
a downstream AI workflow (RAG, search, fine-tuning, analytics) can filter, rank,
and embed without re-processing.

The pipeline is delivered as a CLI:

```
scrape_site --start-url=https://books.toscrape.com/ --max-pages=100 --output=output.jsonl
```

It emits **JSONL** — one self-describing document object per line. There is no
web service in this version; the extraction core is a pure library, and a service
adapter over it is described in [future-work.md](future-work.md).

## How to read this set

If you are new to the project, read in this order: **architecture →
data-model → design-decisions**. Those three cover what the system is, what it
produces, and why it is shaped the way it is. The remaining documents are
component-level references.

## Navigation

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | System overview, the pipeline data flow, components and responsibilities, and the pure-core vs dirty-orchestration boundary. |
| [data-model.md](data-model.md) | The AI document object: full annotated schema, field-by-field reference, controlled vocabularies, missing-vs-empty conventions, and an example record. |
| [crawling.md](crawling.md) | Crawler design: the BFS frontier and bounds, URL canonicalization, the scope and crawl-vs-keep filters, politeness/robots.txt, and the error-handling taxonomy. |
| [extraction.md](extraction.md) | Main-content extraction and cleaning: the layered cascade, validate-then-cascade robustness, the cleaning pipeline, and title resolution. |
| [enrichment.md](enrichment.md) | Signals and metadata: counts, language, content-type classification, code detection, tags, dates, the structured `extra` bag, and the quality gate. |
| [storage-and-idempotency.md](storage-and-idempotency.md) | The JSONL output format, within-run vs across-run idempotency, pluggable storage backends, and atomic writes. |
| [configuration.md](configuration.md) | CLI flags and environment variables, their defaults, and configuration precedence. |
| [observability.md](observability.md) | Run telemetry: the end-of-run summary, the extraction-layer / drop-reason / fetch-outcome distributions, the optional JSON statistics file, and telemetry-vs-analytics. |
| [testing.md](testing.md) | The testing strategy: pure-function unit tests, golden-file tests, mocked-network fetcher tests, and the idempotency assertion. |
| [design-decisions.md](design-decisions.md) | The critical design decisions and their rationale, in Context → Decision → Rationale → Alternatives form. |
| [future-work.md](future-work.md) | Production evolution: richer incremental re-crawl, scheduling, monitoring, cross-source dedup, distributed crawling, and the service adapter. |

## Terminology

- **Document object** — the single data contract of the system: a clean,
  enriched JSON record representing one kept web page. Defined in
  [data-model.md](data-model.md).
- **Pure core** — the extraction engine: plain functions with no network, disk,
  or clock side effects. See [architecture.md](architecture.md).
- **Dirty orchestration** — the crawler, fetcher, storage, and CLI: everything
  that touches the outside world.
- **Keep** — whether a crawled page is emitted as a document. A page can be
  crawled (followed for its links) without being kept. See [crawling.md](crawling.md).
- **Telemetry** — the run-state instrument: the end-of-run summary (and optional
  JSON statistics) the crawler emits over *every* fetched page, including dropped
  ones. Distinct from **analytics**, which measures only the kept pages in the
  JSONL deliverable. See [observability.md](observability.md).
