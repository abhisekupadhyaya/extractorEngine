"""The HTTP fetcher: the one layer that touches the network.

It is the *only* dirty layer downstream of the frontier, so it concentrates all
the production-mindedness — robots.txt, retry/backoff, throttling, an honest
User-Agent, a Content-Type guard, a timeout, and an optional size cap — behind a
single ``fetch`` method that returns clean HTML or ``None``. The guiding
invariant: one bad page never crashes the run, and every skip is logged with the
URL and reason. See ``docs/crawling.md``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger("extractor_engine.fetcher")

# Response Content-Types the engine can process (the header is authoritative).
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


class SeedDisallowedError(RuntimeError):
    """Raised when ``robots.txt`` disallows the seed URL itself.

    The crawler treats this as fatal — there is nowhere to start — and exits,
    rather than silently producing an empty corpus.
    """


@dataclass
class FetchResult:
    """A successfully fetched, HTML page.

    Attributes:
        url: The final URL after any redirects — where the page actually lives.
        html: The decoded HTML body.
        last_modified: The HTTP ``Last-Modified`` header, if present (a date
            source for enrichment).
    """

    url: str
    html: str
    last_modified: str | None


class Fetcher:
    """Polite, resilient HTTP GET with robots, retries, throttling, and guards.

    Args:
        user_agent: The honest bot identity sent on every request.
        delay: Minimum seconds between requests (politeness throttle).
        timeout: Per-request timeout in seconds.
        max_retries: Retry attempts for transient failures (timeouts, 5xx).
        max_page_bytes: Abort a response larger than this (0 disables the cap).
        ignore_robots: Escape hatch that bypasses robots.txt entirely.
        client: An ``httpx.Client`` (injected so tests can mock the transport).
        sleep: The sleep function (injected so tests run without real delays).
    """

    def __init__(
        self,
        *,
        user_agent: str = "scraper-bot/1.0",
        delay: float = 0.5,
        timeout: float = 10.0,
        max_retries: int = 2,
        max_page_bytes: int = 5 * 1024 * 1024,
        ignore_robots: bool = False,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_page_bytes = max_page_bytes
        self.ignore_robots = ignore_robots
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )
        # robots.txt parser cached per (scheme, netloc); None means "unavailable".
        self._robots_cache: dict[tuple[str, str], RobotFileParser | None] = {}
        self._last_request_at: float | None = None

    # -- context manager so the owned client is always closed ----------------- #
    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- politeness ----------------------------------------------------------- #
    def is_allowed(self, url: str) -> bool:
        """Whether robots.txt permits fetching ``url`` (always True if ignored)."""
        if self.ignore_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True  # robots.txt unreachable: be permissive but proceed.
        return parser.can_fetch(self.user_agent, url)

    def _robots_for(self, url: str) -> RobotFileParser | None:
        """Fetch and cache the host's robots.txt parser (once per host)."""
        parts = urlsplit(url)
        key = (parts.scheme, parts.netloc)
        if key in self._robots_cache:
            return self._robots_cache[key]
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        parser = RobotFileParser()
        try:
            response = self._client.get(robots_url)
        except httpx.HTTPError:
            self._robots_cache[key] = None  # could not fetch: treat as permissive.
            return None
        if response.status_code >= 400:
            parser.parse([])  # no robots.txt: allow everything.
        else:
            parser.parse(response.text.splitlines())
        self._robots_cache[key] = parser
        return parser

    def crawl_delay(self, url: str) -> float:
        """Effective delay: the larger of the configured delay and robots' Crawl-delay."""
        if self.ignore_robots:
            return self.delay
        parser = self._robots_for(url)
        if parser is None:
            return self.delay
        declared = parser.crawl_delay(self.user_agent)
        return max(self.delay, float(declared)) if declared is not None else self.delay

    def _throttle(self, url: str) -> None:
        """Sleep so successive requests are at least the crawl delay apart."""
        wait = self.crawl_delay(url)
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            remaining = wait - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = time.monotonic()

    # -- fetch ---------------------------------------------------------------- #
    def fetch(self, url: str) -> FetchResult | None:
        """Fetch one page, returning HTML or ``None`` (skip, already logged).

        Applies the error-handling taxonomy from docs/crawling.md: retry timeouts
        and 5xx with backoff, honor ``Retry-After`` on 429, never retry 4xx, and
        skip non-HTML responses. Robots disallowance is checked first.
        """
        if not self.is_allowed(url):
            logger.warning("skip %s: disallowed by robots.txt", url)
            return None

        for attempt in range(self.max_retries + 1):
            self._throttle(url)
            try:
                result = self._attempt(url)
            except httpx.TimeoutException:
                if not self._backoff(url, attempt, "timeout"):
                    return None
                continue
            except httpx.HTTPError as exc:
                if not self._backoff(url, attempt, f"connection error: {exc}"):
                    return None
                continue

            outcome, retry_after = result
            if outcome is not None or retry_after is None:
                return outcome  # a result, or a non-retryable skip already logged.
            # 429 / 5xx asked us to wait, then retry.
            if attempt >= self.max_retries:
                logger.warning("skip %s: still failing after %d retries", url, self.max_retries)
                return None
            self._sleep(retry_after)
        return None

    def _attempt(self, url: str) -> tuple[FetchResult | None, float | None]:
        """One HTTP attempt.

        Returns ``(result, retry_after)`` where exactly one is meaningful:
        a non-None ``result`` is a success or a terminal skip (``None`` html);
        a non-None ``retry_after`` requests a backoff-and-retry.
        """
        with self._client.stream("GET", url) as response:
            status = response.status_code

            if status == 429:
                wait = self._retry_after_seconds(response)
                logger.warning("429 for %s: backing off %.1fs", url, wait)
                return None, wait
            if 500 <= status < 600:
                logger.warning("%d server error for %s: will retry", status, url)
                return None, self._backoff_seconds(0)
            if status >= 400:
                logger.warning("skip %s: HTTP %d (not retried)", url, status)
                return None, None  # terminal: 4xx is genuinely absent/forbidden.

            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type and not content_type.startswith(_HTML_CONTENT_TYPES):
                logger.warning("skip %s: non-HTML Content-Type %r", url, content_type)
                return None, None

            body = self._read_capped(response, url)
            if body is None:
                return None, None  # oversized: already logged.

            html = body.decode(response.encoding or "utf-8", errors="replace")
            # Use the FINAL URL after any redirects — it is where the page actually
            # lives, so it (not the requested URL) is the correct basis for the
            # document id and for resolving the page's relative links.
            final_url = str(response.url)
            last_modified = response.headers.get("last-modified")
            return FetchResult(url=final_url, html=html, last_modified=last_modified), None

    def _read_capped(self, response: httpx.Response, url: str) -> bytes | None:
        """Stream the body, aborting if it exceeds ``max_page_bytes``."""
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if self.max_page_bytes and total > self.max_page_bytes:
                logger.warning("skip %s: response exceeds %d bytes", url, self.max_page_bytes)
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    def _backoff(self, url: str, attempt: int, reason: str) -> bool:
        """Log a transient failure and sleep; return whether to retry again."""
        if attempt >= self.max_retries:
            logger.warning("skip %s: %s (after %d retries)", url, reason, self.max_retries)
            return False
        wait = self._backoff_seconds(attempt)
        logger.warning("%s for %s: retrying in %.1fs", reason, url, wait)
        self._sleep(wait)
        return True

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff schedule: 0.5s, 1s, 2s, ..."""
        return 0.5 * (2**attempt)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float:
        """Parse the ``Retry-After`` header (seconds form), defaulting to 1s."""
        header = response.headers.get("retry-after", "")
        try:
            return max(0.0, float(header))
        except ValueError:
            return 1.0
