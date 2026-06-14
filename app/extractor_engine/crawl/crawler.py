"""The orchestration loop: fetch -> extract -> enrich -> keep -> store.

The crawler drives the pipeline. For each URL popped from the frontier it fetches
the page, feeds discovered in-scope links back into the frontier, hands the raw
HTML to the pure engine, applies the quality gate, and persists kept documents
idempotently. It owns the clock (``fetched_at``) so the engine stays pure. The
loop ends when the frontier is empty or ``--max-pages`` is reached. See
``docs/architecture.md`` and ``docs/crawling.md``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import Settings
from ..engine.enricher import enrich, quality_gate
from ..engine.extractor import extract
from ..engine.models import ContentType
from ..storage.base import Store, StoreAction
from .fetcher import BaseFetcher, FetchSkip, FetchSkipReason, SeedDisallowedError
from .frontier import Frontier, canonicalize_url

logger = logging.getLogger("extractor_engine.crawler")


def _utc_now_iso() -> str:
    """Current time as tz-aware UTC ISO8601 with a ``Z`` suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bump(counter: dict[str, int], key: str) -> None:
    """Increment ``counter[key]``, treating a missing key as zero."""
    counter[key] = counter.get(key, 0) + 1


def _to_http_date(iso_timestamp: str) -> str | None:
    """Convert a stored ISO8601 UTC timestamp to an RFC-1123 HTTP date, or ``None``."""
    try:
        dt = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")


@dataclass
class RunStats:
    """A tally of what a crawl did, for the end-of-run report.

    The three distribution dicts are the run telemetry described in
    ``docs/observability.md`` — measured over **run-state** (every fetched page,
    including dropped ones), as distinct from the analytics over the kept corpus.
    """

    pages_fetched: int = 0
    kept: int = 0
    dropped: int = 0
    actions: dict[StoreAction, int] = field(
        default_factory=lambda: {action: 0 for action in StoreAction}
    )
    #: Extraction-layer counts over every processed page (semantic/library/...).
    layers: dict[str, int] = field(default_factory=dict)
    #: Gate drop-reason histogram (index / too_short).
    drops: dict[str, int] = field(default_factory=dict)
    #: Typed fetch-skip reasons, errors and intentional skips alike.
    fetch_outcomes: dict[str, int] = field(default_factory=dict)

    @property
    def new_records(self) -> int:
        """Records added or changed this run (the idempotency metric)."""
        return self.actions[StoreAction.INSERT] + self.actions[StoreAction.UPDATE]

    def _split_fetch_outcomes(self) -> tuple[dict[str, int], dict[str, int]]:
        """Partition fetch outcomes into (errors, intentional skips) by reason."""
        errors: dict[str, int] = {}
        skips: dict[str, int] = {}
        for reason, count in self.fetch_outcomes.items():
            target = errors if FetchSkipReason(reason).is_error else skips
            target[reason] = count
        return errors, skips

    def to_dict(self) -> dict[str, object]:
        """Serialize the run statistics for ``--stats-json`` (see observability.md)."""
        errors, skips = self._split_fetch_outcomes()
        return {
            "pages_fetched": self.pages_fetched,
            "pages_kept": self.kept,
            "pages_dropped": self.dropped,
            "new_records": self.new_records,
            "store_actions": {action.value: count for action, count in self.actions.items()},
            "extraction_layers": dict(self.layers),
            "drop_reasons": dict(self.drops),
            "fetch_outcomes": {"errors": errors, "intentional_skips": skips},
        }


class Crawler:
    """Ties the fetcher, the pure engine, and the store into one BFS run."""

    def __init__(
        self,
        settings: Settings,
        fetcher: BaseFetcher,
        store: Store,
        *,
        now: Callable[[], str] = _utc_now_iso,
    ) -> None:
        self._settings = settings
        self._fetcher = fetcher
        self._store = store
        self._now = now

    def run(self) -> RunStats:
        """Crawl from the seed and persist the kept corpus; return run stats.

        Raises:
            SeedDisallowedError: if robots.txt disallows the seed itself.
            ValueError: if no start URL is configured.
        """
        settings = self._settings
        if not settings.start_url:
            raise ValueError("no start URL configured (set --start-url or SCRAPER_START_URL)")

        seed = canonicalize_url(settings.start_url)
        if not self._fetcher.is_allowed(seed):
            raise SeedDisallowedError(f"robots.txt disallows the seed URL: {seed}")

        frontier = Frontier(
            seed,
            max_depth=settings.max_depth,
            include=settings.include,
            exclude=settings.exclude,
        )
        stats = RunStats()

        # The cap is a circuit breaker on origin load and runtime: every fetch
        # *attempt* consumes a slot, whatever its outcome (a success, a 304, or a
        # skipped 4xx / timeout / non-HTML), so --max-pages bounds the number of
        # requests, not just the pages kept. A stale queue entry already handled
        # via a redirect is discarded before fetching and costs nothing. See
        # docs/crawling.md and docs/storage-and-idempotency.md.
        budget_used = 0
        while frontier and budget_used < settings.max_pages:
            url, depth = frontier.pop()
            if frontier.visited(url):
                continue  # already handled (e.g. via a redirect); not a fetch.
            budget_used += 1
            result = self._fetcher.fetch(url, if_modified_since=self._conditional_header(url))
            if isinstance(result, FetchSkip):
                # Bucket the typed reason (errors vs intentional skips are split
                # at report time); already logged with the URL and reason.
                _bump(stats.fetch_outcomes, result.reason.value)
                continue
            stats.pages_fetched += 1

            # A redirect may have moved us; the page actually lives at result.url.
            final_url = result.url
            canonical = canonicalize_url(final_url)
            if not frontier.in_scope(canonical):
                logger.info("skip %s: redirected out of scope -> %s", url, final_url)
                continue
            if frontier.visited(canonical):
                # The redirect target was already handled via its own queue entry
                # (or another redirect); don't re-process or re-discover it.
                continue
            # Mark seen (don't enqueue it again) and visited (don't re-fetch its
            # own stale queue entry when it later pops).
            frontier.mark_seen(canonical)
            frontier.mark_visited(canonical)

            # Link discovery runs for every fetched page, kept or not; resolve the
            # page's relative links against its actual (post-redirect) URL.
            frontier.discover(final_url, result.html, depth)
            self._process(canonical, result.html, result.last_modified, stats)

        self._store.finalize()
        self._log_summary(stats)
        return stats

    def _conditional_header(self, url: str) -> str | None:
        """The ``If-Modified-Since`` value for a re-crawl of ``url``, or ``None``.

        Looks up the page's previously stored ``modified_at`` (keyed by the same
        ``uuid5`` the document id uses) and derives an HTTP-date from it. Disabled
        by ``--no-conditional-get``. See ``docs/crawling.md``.
        """
        if not self._settings.conditional_get:
            return None
        candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
        prior = self._store.previous(candidate_id)
        if not prior:
            return None
        modified_at = prior.get("modified_at")
        if not isinstance(modified_at, str):
            return None
        return _to_http_date(modified_at)

    def _process(self, url: str, html: str, last_modified: str | None, stats: RunStats) -> None:
        """Run one page through the engine, the gate, and the store."""
        settings = self._settings
        extraction = extract(
            html,
            url,
            min_word_count=settings.min_word_count,
            link_density_threshold=settings.link_density_threshold,
            prune_link_density=settings.prune_link_density,
            prune_min_prose_words=settings.prune_min_prose_words,
        )
        # Tally the winning cascade layer for EVERY processed page (run-state).
        _bump(stats.layers, extraction.body_layer)

        doc = enrich(
            extraction,
            url=url,
            fetched_at=self._now(),
            min_word_count=settings.min_word_count,
            link_density_threshold=settings.link_density_threshold,
            code_ratio_threshold=settings.code_ratio_threshold,
            last_modified=last_modified,
        )
        if not quality_gate(doc, settings.min_word_count):
            stats.dropped += 1
            # Index is dropped regardless of length, so it takes precedence.
            reason = "index" if doc.signals.content_type is ContentType.INDEX else "too_short"
            _bump(stats.drops, reason)
            logger.info(
                "drop %s: content_type=%s word_count=%d",
                url,
                doc.signals.content_type.value,
                doc.signals.word_count,
            )
            return

        action = self._store.handle(doc)
        stats.actions[action] += 1
        stats.kept += 1
        logger.debug("%s %s", action.value, url)

    def _log_summary(self, stats: RunStats) -> None:
        """Emit the end-of-run summary described in ``docs/observability.md``.

        Headline counts followed by the three distributions — extraction layers,
        drop reasons, and fetch outcomes (errors kept separate from intentional
        skips). The pure engine never counts; this aggregation lives here, in the
        dirty orchestration layer.
        """
        logger.info(
            "run summary: fetched=%d kept=%d dropped=%d (insert=%d update=%d skip=%d)",
            stats.pages_fetched,
            stats.kept,
            stats.dropped,
            stats.actions[StoreAction.INSERT],
            stats.actions[StoreAction.UPDATE],
            stats.actions[StoreAction.SKIP],
        )
        logger.info("  extraction layers: %s", _format_counts(stats.layers))
        logger.info("  drop reasons: %s", _format_counts(stats.drops))
        errors, skips = stats._split_fetch_outcomes()
        logger.info("  fetch errors: %s", _format_counts(errors))
        logger.info("  fetch intentional-skips: %s", _format_counts(skips))


def _format_counts(counts: dict[str, int]) -> str:
    """Render a count distribution as ``key=count`` pairs, or ``(none)`` if empty."""
    present = {key: value for key, value in counts.items() if value}
    if not present:
        return "(none)"
    return ", ".join(f"{key}={value}" for key, value in sorted(present.items()))
