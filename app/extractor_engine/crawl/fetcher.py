"""The fetcher: the one layer that touches the network.

It is the *only* dirty layer downstream of the frontier, so it concentrates all
the production-mindedness — robots.txt, retry/backoff, throttling, an honest
User-Agent, a Content-Type guard, a timeout, conditional GET, and (on the static
path) a size cap — behind a single ``fetch`` method that returns a
:class:`FetchResult` or a typed :class:`FetchSkip`. Carrying the skip *reason* as
a value (rather than only a log line) is what lets the telemetry layer count
outcomes precisely and keep genuine errors separate from intentional skips.

All of that shared behavior lives in :class:`BaseFetcher`; the two concrete
fetchers differ **only in how they load a URL** (``_load``): the static
:class:`Fetcher` issues an HTTP GET, while the rendering fetcher drives a headless
browser. The guiding invariant: one bad page never crashes the run, and every
skip is also logged with the URL and reason. See ``docs/crawling.md`` and
``docs/observability.md``.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Self
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger("extractor_engine.fetcher")

# Response Content-Types the engine can process (the header is authoritative).
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


class SeedDisallowedError(RuntimeError):
    """Raised when ``robots.txt`` disallows the seed URL itself.

    The crawler treats this as fatal — there is nowhere to start — and exits,
    rather than silently producing an empty corpus.
    """


class FetchSkipReason(StrEnum):
    """The closed set of reasons a fetch did not yield a usable page.

    Each reason is either an **error** (the origin misbehaved) or an
    **intentional skip** (the crawler chose not to process the page by policy).
    Telemetry keeps the two kinds separate; see :attr:`is_error` and
    ``docs/observability.md``.
    """

    # Intentional skips — the crawler chose not to process the page.
    ROBOTS_DISALLOWED = "robots_disallowed"
    NON_HTML = "non_html"
    OVERSIZED = "oversized"
    NOT_MODIFIED = "not_modified"
    # Errors — something went wrong at the origin.
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    RATE_LIMITED = "rate_limited"

    @property
    def is_error(self) -> bool:
        """Whether this reason is a genuine error (vs an intentional skip)."""
        return self in _ERROR_REASONS


_ERROR_REASONS = frozenset(
    {
        FetchSkipReason.TIMEOUT,
        FetchSkipReason.CONNECTION_ERROR,
        FetchSkipReason.HTTP_4XX,
        FetchSkipReason.HTTP_5XX,
        FetchSkipReason.RATE_LIMITED,
    }
)


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


@dataclass
class FetchSkip:
    """A fetch that produced no usable page, carrying a typed reason."""

    reason: FetchSkipReason


@dataclass
class RawResponse:
    """The raw result of loading a URL, before the HTML-vs-skip interpretation.

    Produced by a fetcher's ``_load`` (static HTTP or rendered DOM) and handed to
    the shared interpretation logic, so the status taxonomy and Content-Type guard
    are identical no matter how the bytes were obtained.
    """

    status_code: int
    final_url: str
    headers: Mapping[str, str]
    text: str

    @property
    def content_type(self) -> str:
        """The bare, lowercased Content-Type (no parameters); ``""`` if absent."""
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")


@dataclass
class _Retry:
    """Internal: a transient failure that should back off and retry.

    ``reason`` is what the skip resolves to if the retries are exhausted.
    """

    after: float
    reason: FetchSkipReason


class _FetchTimeoutError(Exception):
    """Internal: ``_load`` timed out (HTTP or render). Mapped to ``timeout``."""


class _FetchConnectionError(Exception):
    """Internal: ``_load`` failed to connect. Mapped to ``connection_error``."""


class _FetchOversizedError(Exception):
    """Internal: the static body exceeded the size cap. Mapped to ``oversized``."""


class BaseFetcher(ABC):
    """Shared politeness, retries, robots, and reason-mapping for all fetchers.

    Subclasses implement only :meth:`_load` — how to turn a URL into a
    :class:`RawResponse`. Everything else (the robots gate, the throttle, the
    retry/backoff loop, the status taxonomy, the Content-Type guard, and the
    typed-skip mapping) is identical across fetcher modes and lives here.

    Args:
        user_agent: The honest bot identity sent on every request.
        delay: Minimum seconds between requests (politeness throttle).
        timeout: Per-request timeout in seconds.
        max_retries: Retry attempts for transient failures (timeouts, 5xx).
        ignore_robots: Escape hatch that bypasses robots.txt entirely.
        client: An ``httpx.Client`` for robots.txt (injected so tests can mock the
            transport); the static fetcher reuses it for content too.
        sleep: The sleep function (injected so tests run without real delays).
    """

    def __init__(
        self,
        *,
        user_agent: str = "scraper-bot/1.0",
        delay: float = 0.5,
        timeout: float = 10.0,
        max_retries: int = 2,
        ignore_robots: bool = False,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
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

    # -- the one method that differs between fetcher modes --------------------- #
    @abstractmethod
    def _load(self, url: str, *, if_modified_since: str | None = None) -> RawResponse:
        """Load a URL into a :class:`RawResponse`.

        Raises :class:`_FetchTimeoutError`, :class:`_FetchConnectionError`, or
        :class:`_FetchOversizedError` on the corresponding failure; the base class maps
        those to the typed skip reasons.
        """

    # -- context manager so owned resources are always closed ----------------- #
    def __enter__(self) -> Self:
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
    def fetch(self, url: str, *, if_modified_since: str | None = None) -> FetchResult | FetchSkip:
        """Fetch one page, returning a :class:`FetchResult` or a typed
        :class:`FetchSkip` (already logged).

        Applies the error-handling taxonomy from docs/crawling.md: retry timeouts
        and 5xx with backoff, honor ``Retry-After`` on 429, never retry 4xx, and
        skip non-HTML responses. Robots disallowance is checked first. When
        ``if_modified_since`` is given it is sent as a conditional GET, and a
        ``304`` resolves to a ``not_modified`` skip.
        """
        if not self.is_allowed(url):
            logger.warning("skip %s: disallowed by robots.txt", url)
            return FetchSkip(FetchSkipReason.ROBOTS_DISALLOWED)

        for attempt in range(self.max_retries + 1):
            self._throttle(url)
            try:
                raw = self._load(url, if_modified_since=if_modified_since)
            except _FetchTimeoutError:
                if not self._backoff(url, attempt, "timeout"):
                    return FetchSkip(FetchSkipReason.TIMEOUT)
                continue
            except _FetchConnectionError as exc:
                if not self._backoff(url, attempt, f"connection error: {exc}"):
                    return FetchSkip(FetchSkipReason.CONNECTION_ERROR)
                continue
            except _FetchOversizedError:
                return FetchSkip(FetchSkipReason.OVERSIZED)

            outcome = self._interpret(url, raw)
            if isinstance(outcome, FetchResult | FetchSkip):
                return outcome  # a result, or a non-retryable skip already logged.
            # 429 / 5xx asked us to wait, then retry.
            if attempt >= self.max_retries:
                logger.warning("skip %s: still failing after %d retries", url, self.max_retries)
                return FetchSkip(outcome.reason)
            self._sleep(outcome.after)
        return FetchSkip(FetchSkipReason.CONNECTION_ERROR)  # unreachable; loop always returns.

    def _interpret(self, url: str, raw: RawResponse) -> FetchResult | FetchSkip | _Retry:
        """Map a loaded response to a result, a typed skip, or a retry request.

        Identical regardless of how the bytes were obtained (the pure/dirty split
        the rest of the system follows — see docs/crawling.md).
        """
        status = raw.status_code
        if status == 304:
            logger.info("skip %s: not modified (304)", url)
            return FetchSkip(FetchSkipReason.NOT_MODIFIED)
        if status == 429:
            wait = self._retry_after_seconds(raw.headers)
            logger.warning("429 for %s: backing off %.1fs", url, wait)
            return _Retry(wait, FetchSkipReason.RATE_LIMITED)
        if 500 <= status < 600:
            logger.warning("%d server error for %s: will retry", status, url)
            return _Retry(self._backoff_seconds(0), FetchSkipReason.HTTP_5XX)
        if status >= 400:
            logger.warning("skip %s: HTTP %d (not retried)", url, status)
            return FetchSkip(FetchSkipReason.HTTP_4XX)  # terminal: genuinely absent/forbidden.

        content_type = raw.content_type
        if content_type and not content_type.startswith(_HTML_CONTENT_TYPES):
            logger.warning("skip %s: non-HTML Content-Type %r", url, content_type)
            return FetchSkip(FetchSkipReason.NON_HTML)

        return FetchResult(url=raw.final_url, html=raw.text, last_modified=raw.last_modified)

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
    def _retry_after_seconds(headers: Mapping[str, str]) -> float:
        """Parse the ``Retry-After`` header (seconds form), defaulting to 1s."""
        header = headers.get("retry-after", "")
        try:
            return max(0.0, float(header))
        except ValueError:
            return 1.0


class Fetcher(BaseFetcher):
    """The static HTTP fetcher: an HTTP ``GET`` with a streamed size cap.

    Adds ``max_page_bytes`` to the shared behavior; everything else is inherited
    from :class:`BaseFetcher`. This is the default fetcher and needs no extra
    dependencies.
    """

    def __init__(self, *, max_page_bytes: int = 5 * 1024 * 1024, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.max_page_bytes = max_page_bytes

    def _load(self, url: str, *, if_modified_since: str | None = None) -> RawResponse:
        """Issue one HTTP GET, streaming the body under the size cap.

        Uses the FINAL URL after any redirects — it is where the page actually
        lives, so it is the correct basis for the document id and relative links.
        """
        headers = {"If-Modified-Since": if_modified_since} if if_modified_since else None
        try:
            with self._client.stream("GET", url, headers=headers) as response:
                status = response.status_code
                final_url = str(response.url)
                resp_headers = {key.lower(): value for key, value in response.headers.items()}

                # Non-2xx needs no body; the base class maps the status to a reason.
                if not 200 <= status < 300:
                    return RawResponse(status, final_url, resp_headers, "")
                # Non-HTML: skip downloading the body — the guard rejects it anyway.
                content_type = resp_headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type and not content_type.startswith(_HTML_CONTENT_TYPES):
                    return RawResponse(status, final_url, resp_headers, "")

                body = self._read_capped(response, url)
                text = body.decode(response.encoding or "utf-8", errors="replace")
                return RawResponse(status, final_url, resp_headers, text)
        except httpx.TimeoutException as exc:
            raise _FetchTimeoutError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise _FetchConnectionError(str(exc)) from exc

    def _read_capped(self, response: httpx.Response, url: str) -> bytes:
        """Stream the body, aborting (oversized) if it exceeds ``max_page_bytes``."""
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if self.max_page_bytes and total > self.max_page_bytes:
                logger.warning("skip %s: response exceeds %d bytes", url, self.max_page_bytes)
                raise _FetchOversizedError(url)
            chunks.append(chunk)
        return b"".join(chunks)


def make_fetcher(settings: Settings) -> BaseFetcher:
    """Select the fetcher mode from settings: static HTTP, or headless rendering.

    The rendering fetcher (and its browser dependency) is imported lazily so the
    default static path needs none of it. See ``docs/crawling.md``.
    """
    common: dict[str, object] = {
        "user_agent": settings.user_agent,
        "delay": settings.delay,
        "timeout": settings.timeout,
        "max_retries": settings.max_retries,
        "ignore_robots": settings.ignore_robots,
    }
    if settings.render:
        from .rendering_fetcher import RenderingFetcher

        return RenderingFetcher(render_timeout=settings.render_timeout, **common)
    return Fetcher(max_page_bytes=settings.max_page_bytes, **common)
