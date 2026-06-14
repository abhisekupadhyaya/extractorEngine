# Testing strategy

This document describes how the pipeline is tested. The strategy follows directly
from the architecture: because the extraction engine is pure and the only dirty
layer is the fetcher, the parts that determine output quality can be tested
exhaustively and deterministically on static fixtures, and the network is mocked
exactly once. Tests mirror the package layout, and fixtures (saved HTML and the
expected JSON for golden tests) live alongside them.

## What gets tested where

| Layer | How it is tested | Why this works |
|---|---|---|
| Pure functions (canonicalization, cleaning, classification, signals, gate) | Table-driven unit tests | No I/O, fully deterministic. |
| The extractor cascade | Validation tests on crafted HTML | Asserts that bad layers are rejected and the cascade falls through. |
| End-to-end extraction | Golden-file tests on saved real pages | One high-leverage regression guard over the whole transform. |
| Fetcher | Mocked-network tests | The single dirty layer, isolated and exercised against simulated responses. |
| Idempotency | Re-run assertion | Proves the no-duplicates guarantee. |

## Pure-function unit tests

The pure functions are the core of the suite and are tested table-driven (many
input → expected pairs):

- **`canonicalize_url()`** — the highest-value table. Each canonicalization step
  from [crawling.md](crawling.md) gets cases: lowercasing, `www` stripping,
  fragment removal, tracking-param denylisting, param sorting, default-document
  and trailing-slash normalization. Because canonicalization underpins `id` and
  deduplication, a regression here is a regression in identity, so this table is
  kept thorough.
- **`clean_text()`** — boilerplate-laden HTML in, clean text out; asserts chrome
  removal, entity decoding, and whitespace normalization.
- **`classify_content_type()`**, **`is_mostly_code()`**, **`compute_signals()`**,
  **`quality_gate()`**, and the **tags / dates** extractors — each tested against
  representative inputs including the empty and missing cases (so the
  null/empty-safe contracts in [data-model.md](data-model.md) are enforced).

## Extractor validation tests

These tests target the validate-then-cascade robustness in
[extraction.md](extraction.md). The key case feeds **navigation-heavy HTML** to a
layer that would over-extract and asserts that its output is **rejected** (on
link density) and the cascade **falls through** to a better layer. A
complementary case feeds a too-short fragment and asserts rejection on length.
These tests verify the robustness layer directly, since that layer is the
critical contribution.

## Golden-file tests

A small set of **real saved pages** — for example, a book product page and an
index page — are committed as fixtures, each paired with the expected document
object. The test runs the full extract-and-enrich transform and asserts the
output matches the golden record. This is the single highest-leverage regression
guard: it pins the entire pipeline's behavior on representative real input, so any
change that alters output is caught immediately.

## Mocked-network fetcher tests

The fetcher is the only layer that touches the network, so it is mocked (with an
HTTP mocking library) and tested against simulated responses. These assert the
error taxonomy from [crawling.md](crawling.md):

- **Retry on `5xx`**, then skip.
- **No retry on `404`** — skip immediately.
- **Honor `Retry-After`** on `429`.
- **Respect `robots.txt`** — disallowed URLs are skipped.

## The idempotency assertion

A crawler integration test runs the pipeline twice against the same fixtures and
asserts that the **second run produces zero new records** — the concrete proof of
the across-run insert/skip/update behavior in
[storage-and-idempotency.md](storage-and-idempotency.md). This is the test that
guarantees re-runs do not duplicate the corpus.

## Determinism

Language detection is made deterministic in the test configuration (the detector
is seeded), so golden-file and signal tests produce identical results on every
machine and every run.

## Scope

Tests are treated as a quality-supporting layer rather than a deliverable in
themselves. The core parsing/transform unit tests and the golden-file test
directly support correctness and are kept; an exhaustive suite beyond that is the
part that would be trimmed first under time pressure.
