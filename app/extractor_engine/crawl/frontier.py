"""The breadth-first frontier and the URL canonicalization it is built on.

``canonicalize_url`` is critical: the canonical string is both the seen-set
key and the basis of the document ``id`` (``uuid5(canonical_url)``), so a
regression here is a regression in identity. The frontier walks the site
breadth-first from the seed, dedupes on canonical URLs, and applies the scope
filter. See ``docs/crawling.md``.
"""

from __future__ import annotations

import heapq
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

# Default index documents collapsed to their directory root.
_DEFAULT_DOCS = {"index.html", "index.htm", "index.php", "default.html", "default.htm"}

# Tracking parameters removed by the denylist (in addition to any ``utm_*`` key).
# A denylist, not strip-all, so genuinely distinct query-param pages survive.
_TRACKING_PARAMS = {
    "ref",
    "referrer",
    "gclid",
    "fbclid",
    "msclkid",
    "mc_eid",
    "mc_cid",
    "_ga",
    "_gl",
    "igshid",
    "sessionid",
    "phpsessid",
    "yclid",
    "dclid",
}

# Obvious binary / non-HTML extensions the frontier pre-filters before fetching.
_BINARY_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif", ".tiff",
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z", ".bz2",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ogg", ".wav",
    ".css", ".js", ".json", ".xml", ".rss", ".woff", ".woff2", ".ttf", ".eot",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv",
)

# Link schemes that are not crawlable pages.
_NON_HTTP_SCHEMES = ("mailto:", "javascript:", "tel:", "data:", "#")

# URL paths that look like a listing/navigation page rather than a content leaf.
# Used only as an intra-layer crawl-order tiebreak (see Frontier), so a bounded
# --max-pages budget is spent on emittable content before navigation.
_LISTING_URL = re.compile(
    r"(?:^|/)(?:category|categories|tag|tags|page|pages|search|browse|archive)(?:/|$|[-_])"
    r"|/page-\d+",
    re.IGNORECASE,
)


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered.startswith("utm_") or lowered in _TRACKING_PARAMS


def canonicalize_url(url: str) -> str:
    """Reduce a URL to its canonical form by the ordered rule in docs/crawling.md.

    Steps, in order: lowercase scheme/host and strip a leading ``www.``; drop the
    ``#fragment``; remove tracking parameters by denylist while keeping and
    sorting the rest; normalize the default document and trailing slash. The
    result is the string hashed into ``id`` and used as the seen-set key.
    """
    parts = urlsplit(url.strip())

    # 1. Lowercase scheme and host; strip a leading "www.".
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if parts.port and not _is_default_port(scheme, parts.port):
        netloc = f"{host}:{parts.port}"

    # 3. Normalize query parameters: drop tracking keys, keep and sort the rest.
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking_param(k)]
    kept.sort()
    query = urlencode(kept)

    # 4. Normalize the default document and trailing slash.
    path = _normalize_path(parts.path)

    # 2. The fragment is dropped by passing "" as the final component.
    return urlunsplit((scheme, netloc, path, query, ""))


def _is_default_port(scheme: str, port: int) -> bool:
    return (scheme == "http" and port == 80) or (scheme == "https" and port == 443)


def _normalize_path(path: str) -> str:
    """Strip a default index document to its directory root, then the trailing
    slash (except on the bare root ``/``).
    """
    segments = path.split("/")
    if segments and segments[-1].lower() in _DEFAULT_DOCS:
        segments[-1] = ""  # collapse to the directory root, keeping its slash
        path = "/".join(segments)
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def _has_binary_extension(path: str) -> bool:
    return path.lower().endswith(_BINARY_EXTENSIONS)


class Frontier:
    """BFS frontier of canonical URLs with depth bounds and the scope filter.

    The frontier answers two questions: what to crawl next (``pop``) and whether
    a discovered URL is in scope. It never enqueues a URL twice — the seen-set is
    keyed on the canonical string — which is what satisfies the within-run
    no-duplicate-fetch guarantee.

    Traversal is breadth-first by depth. Within a single depth layer, content
    URLs are visited before listing URLs (an ``index``-looking-path tiebreak), so
    a bounded ``--max-pages`` budget is spent on emittable content rather than on
    navigation pages that are crawled-but-not-kept. The insertion sequence is the
    final tiebreak, which keeps ordering deterministic (and thus re-runs
    idempotent). See ``docs/crawling.md``.
    """

    def __init__(
        self,
        seed_url: str,
        *,
        max_depth: int = 5,
        include: str | None = None,
        exclude: str | None = None,
    ) -> None:
        canonical_seed = canonicalize_url(seed_url)
        self.scope_host = urlsplit(canonical_seed).hostname or ""
        self.max_depth = max_depth
        self._include = re.compile(include) if include else None
        self._exclude = re.compile(exclude) if exclude else None
        self._seen: set[str] = set()
        # URLs actually fetched-and-processed. ``_seen`` means "enqueued";
        # ``_visited`` means "already handled". The distinction lets ``pop``
        # discard a stale queue entry for a URL that was already processed via a
        # redirect, instead of fetching it a second time.
        self._visited: set[str] = set()
        # Min-heap keyed (depth, listing_rank, insertion_seq); url carried last.
        self._heap: list[tuple[int, int, int, str]] = []
        self._seq = 0
        self.add(canonical_seed, 0)

    def add(self, canonical_url: str, depth: int) -> bool:
        """Enqueue a canonical URL at ``depth`` if unseen. Returns whether added."""
        if canonical_url in self._seen:
            return False
        self._seen.add(canonical_url)
        listing_rank = 1 if _LISTING_URL.search(urlsplit(canonical_url).path or "/") else 0
        heapq.heappush(self._heap, (depth, listing_rank, self._seq, canonical_url))
        self._seq += 1
        return True

    def mark_seen(self, canonical_url: str) -> None:
        """Record a canonical URL as seen without enqueueing it.

        Used for a redirect's final URL: the target has been processed via the
        URL we requested, so it must not be fetched again if another page links
        to it later.
        """
        self._seen.add(canonical_url)

    def mark_visited(self, canonical_url: str) -> None:
        """Record a canonical URL as fetched-and-processed.

        Distinct from :meth:`mark_seen` (= enqueued): this lets :meth:`pop`'s
        caller discard a stale queue entry for a URL already handled via a
        redirect, rather than fetching it a second time.
        """
        self._visited.add(canonical_url)

    def visited(self, canonical_url: str) -> bool:
        """Whether ``canonical_url`` has already been fetched and processed."""
        return canonical_url in self._visited

    def pop(self) -> tuple[str, int]:
        """Pop the next ``(canonical_url, depth)``: lowest depth first, content
        before listings within a depth.
        """
        depth, _rank, _seq, url = heapq.heappop(self._heap)
        return url, depth

    def __bool__(self) -> bool:
        return bool(self._heap)

    def in_scope(self, canonical_url: str) -> bool:
        """Whether a canonical URL belongs to the crawl: same host (``www``
        aliased, set during init), optional include/exclude path regex.
        Subdomains are not auto-included.
        """
        parts = urlsplit(canonical_url)
        if parts.scheme not in ("http", "https"):
            return False
        if (parts.hostname or "") != self.scope_host:
            return False
        if _has_binary_extension(parts.path):
            return False
        path = parts.path or "/"
        if self._include and not self._include.search(path):
            return False
        if self._exclude and self._exclude.search(path):
            return False
        return True

    def discover(self, base_url: str, html: str, depth: int) -> int:
        """Parse ``<a href>`` links, canonicalize, scope-filter, and enqueue.

        Links are resolved against ``base_url``, reduced to canonical form, and
        enqueued at ``depth + 1`` when in scope, unseen, and within ``max_depth``.
        Runs for every fetched page, including crawl-but-not-keep pages. Returns
        the number of URLs newly enqueued.
        """
        next_depth = depth + 1
        if next_depth > self.max_depth:
            return 0
        soup = BeautifulSoup(html, "lxml")
        added = 0
        for anchor in soup.find_all("a", href=True):
            if not isinstance(anchor, Tag):
                continue
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            href = href.strip()
            if not href or href.lower().startswith(_NON_HTTP_SCHEMES):
                continue
            canonical = canonicalize_url(urljoin(base_url, href))
            if self.in_scope(canonical) and self.add(canonical, next_depth):
                added += 1
        return added
