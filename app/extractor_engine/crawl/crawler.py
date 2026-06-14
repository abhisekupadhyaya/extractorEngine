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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import Settings
from ..engine.enricher import enrich, quality_gate
from ..engine.extractor import extract
from ..storage.base import Store, StoreAction
from .fetcher import Fetcher, SeedDisallowedError
from .frontier import Frontier, canonicalize_url

logger = logging.getLogger("extractor_engine.crawler")


def _utc_now_iso() -> str:
    """Current time as tz-aware UTC ISO8601 with a ``Z`` suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RunStats:
    """A tally of what a crawl did, for the end-of-run report."""

    pages_fetched: int = 0
    kept: int = 0
    dropped: int = 0
    actions: dict[StoreAction, int] = field(
        default_factory=lambda: {action: 0 for action in StoreAction}
    )

    @property
    def new_records(self) -> int:
        """Records added or changed this run (the idempotency metric)."""
        return self.actions[StoreAction.INSERT] + self.actions[StoreAction.UPDATE]


class Crawler:
    """Ties the fetcher, the pure engine, and the store into one BFS run."""

    def __init__(
        self,
        settings: Settings,
        fetcher: Fetcher,
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

        while frontier and stats.pages_fetched < settings.max_pages:
            url, depth = frontier.pop()
            result = self._fetcher.fetch(url)
            if result is None:
                continue  # fetch skipped/failed; already logged with the reason.
            stats.pages_fetched += 1

            # A redirect may have moved us; the page actually lives at result.url.
            final_url = result.url
            canonical = canonicalize_url(final_url)
            if not frontier.in_scope(canonical):
                logger.info("skip %s: redirected out of scope -> %s", url, final_url)
                continue
            # Don't re-fetch this target if another page links to it later.
            frontier.mark_seen(canonical)

            # Link discovery runs for every fetched page, kept or not; resolve the
            # page's relative links against its actual (post-redirect) URL.
            frontier.discover(final_url, result.html, depth)
            self._process(canonical, result.html, result.last_modified, stats)

        self._store.finalize()
        logger.info(
            "crawl complete: fetched=%d kept=%d dropped=%d (insert=%d update=%d skip=%d)",
            stats.pages_fetched,
            stats.kept,
            stats.dropped,
            stats.actions[StoreAction.INSERT],
            stats.actions[StoreAction.UPDATE],
            stats.actions[StoreAction.SKIP],
        )
        return stats

    def _process(self, url: str, html: str, last_modified: str | None, stats: RunStats) -> None:
        """Run one page through the engine, the gate, and the store."""
        settings = self._settings
        extraction = extract(
            html,
            url,
            min_word_count=settings.min_word_count,
            link_density_threshold=settings.link_density_threshold,
        )
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
