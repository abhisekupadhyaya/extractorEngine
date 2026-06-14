# Design decisions

This document records the load-bearing decisions behind the pipeline and the
reasoning that produced them. Each is presented as **Context → Decision →
Rationale → Alternatives considered**, so a reader can see not just what was built
but why, and what was weighed and set aside. These are the choices that shaped the
architecture; the component documents describe the mechanics.

---

## 1. Generic extraction over site-specific selectors

**Context.** The pipeline must extract clean main content from arbitrary pages,
and the requirement explicitly asks for robustness to minor site changes. The
extraction stage runs per-page at runtime and determines the whole shape of the
extract stage.

**Decision.** Extract `body_text` with a **generic, site-agnostic main-content
heuristic** rather than hardcoded CSS selectors for the target site's exact HTML.

**Rationale.** This is the one fork in the design that is both hard and
irreversible. The other choices are comparatively cheap to get wrong — a bad
crawl just yields fewer pages (recoverable), and the schema is a design-time
artifact (changeable). Extraction is different: it runs at runtime, per page, and
the generic-versus-specific choice dictates the entire extract-stage
architecture. Site-specific selectors would be flawless on one site but require a
**full rewrite for any second site**, and would break the moment that site's
markup drifts — the robustness requirement would fail on day one. A generic
approach is fuzzier on any single page but is built once, survives markup drift,
and generalizes. The stakes are asymmetric: dirty `body_text` silently poisons
every downstream embedding, and the consumer cannot tell which records to
distrust, so robustness is worth more than a perfect fit to one layout.

**Alternatives considered.** Hardcoded per-site CSS selectors — rejected as
non-generalizing and brittle to markup changes. A hybrid (generic with
per-site overrides) — rejected as premature for a single-site v1 and a slope back
toward site-specificity.

---

## 2. Buy the extractor, build the robustness

**Context.** Having chosen a generic approach, the question is whether to
hand-write the main-content algorithm or use an existing library.

**Decision.** **Buy** a proven, maintained main-content extractor (trafilatura,
readability-style) as the workhorse layer, and **build** the robustness around it
— a layered cascade with per-layer validation.

**Rationale.** Main-content extraction is a solved problem with mature
implementations; re-implementing the algorithm would be reinventing the wheel and
would almost certainly be worse. The contribution that actually matters is making
the result **robust**: auditing the library's output and recovering when it
misfires. So the workhorse is bought, and the engineering effort goes into the
validate-then-cascade layer — semantic HTML5 first, the library second, a density
heuristic third, and a crude floor that never returns empty. Each layer's output
is validated (rejected on high link density, the over-extraction symptom, or on
being too short, the under-extraction symptom) before it is trusted. That
validation layer is the load-bearing, owned piece of the system. Mechanics are in
[extraction.md](extraction.md).

**Alternatives considered.** Hand-rolling a readability algorithm from scratch —
rejected as wasted effort on a solved problem. Trusting the library's output
blindly — rejected because its two known failure modes (pulling navigation, or
bailing early) would pass straight through to the corpus undetected.

---

## 3. JSONL deliverable with an optional state store

**Context.** The output must be realistically feedable into an AI system, and
re-runs must not duplicate records. There is also an instinct toward standing up a
database for "production-mindedness".

**Decision.** The **deliverable is JSONL** — one document object per line. A
separate **internal state store keyed by `id`** handles idempotency. The state
store defaults to the JSONL file itself (zero infrastructure); a database is an
**optional** backend, used as crawl state only, never as the deliverable.

**Rationale.** Keeping the deliverable as a plain JSONL file means a consumer can
read the output with no infrastructure at all, and it makes the tool trivial to
run and grade. Idempotency is a separate concern from the output format, so it is
modeled separately: a within-run `seen` set prevents duplicate fetches, and an
across-run insert/skip/update keyed on `id` (comparing `content_hash`) keeps
re-runs from duplicating or staling records. A database earns its place only as
resumable crawl state — and even then it is optional, activated by an environment
variable, so the default path requires zero services. This avoids over-building
while leaving a clean upgrade path. Mechanics are in
[storage-and-idempotency.md](storage-and-idempotency.md).

**Alternatives considered.** A database as the primary deliverable — rejected as
over-built and as imposing infrastructure on the consumer for no benefit. Fusing
within-run and across-run deduplication into one mechanism — rejected because they
are genuinely different problems (URL identity vs content change) and conflating
them muddies both.

---

## 4. Schema shape: hybrid, with every field justified

**Context.** The schema is the contract the downstream team builds against, and
it is easy to either bloat it with derivable fields or flatten away useful
structure.

**Decision.** A **hybrid** shape: identity and extracted-content fields at the top
level, derived signals grouped under a nested `signals` block. Every field is
included only if it answers a concrete consumer decision and its input does not
survive into the record for later recomputation.

**Rationale.** The nesting is a contract choice, not a performance one — each
JSONL record loads whole, so nested access is free. Grouping the derived signals
makes the contract legible, lets the signals block be versioned independently of
the identity contract, and lets a consumer grab the whole "filter panel" as one
unit. The inclusion test is "does the input survive into the record?" Fields whose
value can be recomputed from what is already present are left out:
`reading_time` (= `word_count / 200`), `quality_score` (no consumer acts
differently on 0.7 vs 0.6 — keep the raw signals), and `source` (derivable from
the URL domain). `is_mostly_code` is kept precisely because its input — the
`<pre>`/`<code>` markup — does **not** survive into `body_text` and must be
captured at extraction time. The full field reference is in
[data-model.md](data-model.md).

**Alternatives considered.** A fully flat schema — rejected because it loses the
legible grouping and couples the signals to the identity contract. A fully nested
schema — rejected because identity and content are the primary keys a consumer
reaches for and belong at the top. Including convenience fields like
`reading_time` and `quality_score` — rejected as derivable or actionless.

---

## 5. URL canonicalization with a tracking denylist

**Context.** The document `id` is `uuid5` of the URL, and the within-run dedup set
is keyed on the URL. The exact rule for reducing a URL to canonical form therefore
determines identity.

**Decision.** Canonicalize every URL with an **ordered rule** — lowercase
scheme/host and strip `www`, drop the fragment, remove tracking parameters by a
**denylist** while keeping and sorting the rest, normalize the default document and
trailing slash — then hash the result.

**Rationale.** Without canonicalization, `/p`, `/p#section`, and `/p?ref=email`
hash to three different ids and produce three duplicate records for one page — the
no-duplicates guarantee breaks before any deduplication logic runs. The
consequential sub-choice is the parameter rule. Stripping *all* parameters would
work on a purely path-based site but would collapse genuinely distinct pages on a
query-param catalog (`?id=1000` and `?id=1001` would merge into one record). A
denylist removes only known tracking noise, behaves identically to strip-all on
path-based sites, and stays correct on query-param sites — keeping
canonicalization consistent with the generic stance of decision 1. Mechanics are
in [crawling.md](crawling.md).

**Alternatives considered.** Strip all query parameters — rejected as a
single-site overfit that silently merges distinct pages elsewhere. Keep all
parameters unsorted — rejected because parameter order would make equivalent URLs
hash differently. Using a URL type that re-normalizes independently — rejected
because it would desync from the single canonical string the `id` is hashed from
and cause id drift.

---

## 6. Scope and the crawl-vs-keep cut line

**Context.** The crawler must avoid external domains and must not emit obvious
non-content pages, but "what we follow" and "what we keep" are not the same
question.

**Decision.** Use **two separate filters**. A **scope filter** decides whether a
URL is crawled at all (exact seed netloc, `www` aliased, subdomains excluded by
default, optional path regex). A **crawl-vs-keep filter** decides, separately,
whether a crawled page is emitted as a document — index, listing, login, and
search pages may be crawled for their links but are not kept.

**Rationale.** Fusing the two would force a false choice: either we follow a
listing page's links *and* emit the listing (which poisons the corpus with
navigation), or we skip listing pages entirely (and miss the content they link
to). Separating them lets the crawler follow an index for discovery while
excluding it from output. The keep decision is realized as the quality gate
(minimum word count, and `content_type != index`), embodying the principle that a
smaller corpus of trustworthy documents beats a larger one a consumer cannot
trust. Mechanics are in [crawling.md](crawling.md) and the quality gate in
[enrichment.md](enrichment.md).

**Alternatives considered.** A single filter that both gates crawling and gates
emission — rejected because it cannot both follow and exclude a listing page.
Keeping index pages in the output — rejected because their navigational text
poisons downstream embeddings.

---

## 7. Pure-library engine plus CLI, no service in v1

**Context.** The deliverable is a command-line tool. A network service over the
engine is tempting as a "production" surface.

**Decision.** Build the extraction engine as a **pure library** (plain functions,
no I/O) and have the **CLI orchestrator import it directly**. Ship **no web
service** in this version; treat a service adapter as Future Work.

**Rationale.** The required deliverable is a CLI, so a service would be
gold-plating. Keeping the engine pure has concrete payoffs now: the quality-
critical code is unit-testable on static fixtures with no network, and the one
dirty layer (fetching) is mocked exactly once. It also makes the service cheap
*later* — because the engine is already a clean library with a single data
contract, a network adapter over it is thin glue rather than a rewrite. So the
boundary is drawn now, and the surface is deferred. Mechanics are in
[architecture.md](architecture.md); the adapter is in
[future-work.md](future-work.md).

**Alternatives considered.** Shipping a service in v1 — rejected as gold-plating
beyond the required deliverable. Coupling extraction logic into the CLI directly
(no library boundary) — rejected because it would make the engine hard to test and
hard to reuse behind a future service.
