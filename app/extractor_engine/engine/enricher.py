"""Enrichment: derive signals and metadata, and apply the quality gate.

Takes an :class:`~extractor_engine.engine.extractor.ExtractionResult` plus the
canonical URL and a fetch timestamp, and returns a fully-populated
:class:`~extractor_engine.engine.models.Document`. Every rule is best-effort and
null/empty-safe, so a missing or malformed source never raises. Enrichment is
pure: no network, disk, or clock — the timestamp is passed in. See
``docs/enrichment.md``.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime

from bs4 import BeautifulSoup
from bs4.element import Tag
from langdetect import LangDetectException, detect

from .extractor import ExtractionResult, link_density
from .models import ContentType, Document, Signals

# Language detection needs at least this many words to be reliable. Intentionally
# lower than the quality-gate keep threshold (see docs/enrichment.md).
_LANG_MIN_WORDS = 20

# URL paths that signal a listing/navigation page rather than a content leaf.
_LISTING_PATH = re.compile(
    r"(?:^|/)(?:category|categories|tag|tags|page|pages|search|login|sign-?in|archive|browse)"
    r"(?:/|$|[-_])|/page-\d+",
    re.IGNORECASE,
)
# Documentation-style paths.
_DOC_PATH = re.compile(r"(?:^|/)(?:docs?|documentation|manual|guide|reference|api)(?:/|$)", re.IGNORECASE)
# Class names that indicate a product price / availability widget.
_PRODUCT_CLASS = re.compile(r"price|availability|add[-_]?to[-_]?cart|in[-_]?stock", re.IGNORECASE)

_URL_NAMESPACE = uuid.NAMESPACE_URL


def enrich(
    result: ExtractionResult,
    *,
    url: str,
    fetched_at: str,
    min_word_count: int = 25,
    link_density_threshold: float = 0.4,
    code_ratio_threshold: float = 0.5,
    last_modified: str | None = None,
) -> Document:
    """Build the enriched :class:`Document` for one extracted page.

    Args:
        result: The extractor output (title, body, soup, library metadata).
        url: The **canonical** URL — hashed into ``id`` and stored as ``url``.
        fetched_at: Capture timestamp (tz-aware UTC ISO8601 with ``Z``).
        min_word_count: Quality-gate floor; also language-detection context.
        link_density_threshold: Cutoff used by content-type classification.
        code_ratio_threshold: ``is_mostly_code`` cutoff.
        last_modified: Optional HTTP ``Last-Modified`` header value.

    Returns:
        A fully-populated document. Whether it is *kept* is a separate decision
        made by :func:`quality_gate`.
    """
    soup = result.soup
    body_text = result.body_text

    signals = Signals(
        word_count=len(body_text.split()),
        char_count=len(body_text),
        language=detect_language(body_text),
        content_type=classify_content_type(soup, url, link_density_threshold),
        is_mostly_code=is_mostly_code(soup, code_ratio_threshold),
    )
    published_at, modified_at = extract_dates(soup, last_modified)

    return Document(
        id=str(uuid.uuid5(_URL_NAMESPACE, url)),
        url=url,
        title=result.title,
        body_text=body_text,
        tags=extract_tags(soup, result.meta),
        published_at=published_at,
        modified_at=modified_at,
        fetched_at=fetched_at,
        content_hash=hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
        signals=signals,
        extra=extract_extra(soup),
    )


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
def detect_language(body_text: str) -> str:
    """Detect the ISO 639-1 language of ``body_text``, or ``"und"``.

    Runs only when there are enough words to classify reliably; any failure or
    too-short input yields ``"und"``. Never returns null.
    """
    if len(body_text.split()) < _LANG_MIN_WORDS:
        return "und"
    try:
        return detect(body_text).split("-")[0]
    except LangDetectException:
        return "und"


def classify_content_type(
    soup: BeautifulSoup, url: str, link_density_threshold: float
) -> ContentType:
    """Assign a ``content_type`` by a first-match-wins rule cascade.

    The page-level link density distinguishes a content leaf (e.g. a product
    detail page) from a listing that merely *contains* product cards — without
    it, every catalog listing would match the price-element rule. ``other`` is
    the mandatory fallback. See ``docs/enrichment.md``.
    """
    path = url
    dense = _page_link_density(soup) > link_density_threshold

    # 1. JSON-LD Product, or a price/availability widget on a non-listing page.
    if not dense and _has_product_signal(soup):
        return ContentType.PRODUCT_PAGE
    # 2. A docs-style path with article-like prose.
    if not dense and _DOC_PATH.search(path) and _article_like(soup):
        return ContentType.DOC_PAGE
    # 3. An <article> (non-listing) or a declared publication time.
    if (not dense and soup.find("article") is not None) or _meta_content(
        soup, "article:published_time"
    ):
        return ContentType.ARTICLE
    # 4. Link-dense, or a listing/pagination URL.
    if dense or _LISTING_PATH.search(path):
        return ContentType.INDEX
    # 5. Mandatory fallback.
    return ContentType.OTHER


def is_mostly_code(soup: BeautifulSoup, threshold: float) -> bool:
    """Whether ``<pre>``/``<code>`` text dominates the page above ``threshold``.

    Computed from the live markup because its input does not survive into the
    cleaned ``body_text``. ``<code>`` nested inside ``<pre>`` is counted once.
    """
    total = len(soup.get_text())
    if total == 0:
        return False
    code_chars = 0
    for element in soup.find_all(["pre", "code"]):
        if element.name == "code" and element.find_parent("pre") is not None:
            continue
        code_chars += len(element.get_text())
    return (code_chars / total) > threshold


def _page_link_density(soup: BeautifulSoup) -> float:
    """Link density over the page body, used for index detection."""
    body = soup.body if isinstance(soup.body, Tag) else None
    return link_density(body) if body is not None else 0.0


def _has_product_signal(soup: BeautifulSoup) -> bool:
    """True if the page carries a product marker (JSON-LD, OG, or a price widget)."""
    if _jsonld_has_type(soup, "Product"):
        return True
    if _meta_content(soup, "og:type") == "product":
        return True
    if soup.find("meta", attrs={"property": "product:price:amount"}) is not None:
        return True
    return soup.find(class_=_PRODUCT_CLASS) is not None


def _article_like(soup: BeautifulSoup) -> bool:
    """A coarse "this reads like prose" check for the doc_page rule."""
    if soup.find("article") is not None:
        return True
    return any(len(p.get_text(strip=True)) > 200 for p in soup.find_all("p"))


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #
def extract_tags(soup: BeautifulSoup, meta: dict[str, object]) -> list[str]:
    """Gather topical tags from standard web sources, in priority order.

    Sources: breadcrumb, ``<meta name=keywords>``, the library's parsed tags,
    OpenGraph ``article:tag``/``section``, and JSON-LD ``keywords``/``genre``.
    Results are trimmed and de-duplicated preserving first-seen order; ``[]`` if
    nothing is found.
    """
    collected: list[str] = []
    collected.extend(_breadcrumb_tags(soup))

    keywords = _meta_content(soup, "keywords")
    if keywords:
        collected.extend(keywords.split(","))

    for key in ("tags", "categories"):
        value = meta.get(key)
        if isinstance(value, list):
            collected.extend(str(item) for item in value)

    for meta_tag in soup.find_all("meta", attrs={"property": "article:tag"}):
        if isinstance(meta_tag, Tag):
            content = meta_tag.get("content")
            if isinstance(content, str):
                collected.append(content)
    section = _meta_content(soup, "article:section")
    if section:
        collected.append(section)

    collected.extend(_jsonld_tags(soup))
    return _dedupe(collected)


def _breadcrumb_tags(soup: BeautifulSoup) -> list[str]:
    """Tags from breadcrumb navigation: the linked ancestor crumbs.

    The current page (the trailing unlinked crumb) and a leading "Home" are not
    topical labels, so they are dropped. On the sandbox book pages this yields
    e.g. ``["Books", "Poetry"]``.
    """
    containers = list(soup.find_all(attrs={"itemtype": re.compile("BreadcrumbList", re.I)}))
    containers.extend(soup.find_all(class_=re.compile("breadcrumb", re.I)))
    for container in containers:
        if not isinstance(container, Tag):
            continue
        links = [a.get_text(strip=True) for a in container.find_all("a")]
        links = [text for text in links if text]
        if not links:
            continue
        if links[0].lower() in {"home", "homepage"}:
            links = links[1:]
        if links:
            return links
    return []


def _jsonld_tags(soup: BeautifulSoup) -> list[str]:
    """Tags from JSON-LD ``keywords`` / ``genre`` fields."""
    out: list[str] = []
    for block in _jsonld_objects(soup):
        for key in ("keywords", "genre"):
            value = block.get(key)
            if isinstance(value, str):
                out.extend(value.split(","))
            elif isinstance(value, list):
                out.extend(str(item) for item in value)
    return out


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def extract_dates(
    soup: BeautifulSoup, last_modified: str | None
) -> tuple[str | None, str | None]:
    """Resolve ``(published_at, modified_at)`` from *declared* and *served* date
    sources only, parsed to tz-aware UTC ISO8601. Either is ``None`` when no such
    source yields a parseable date.

    Only **declared** dates (JSON-LD ``datePublished``/``dateModified``,
    ``article:*_time`` meta, ``<time datetime>``) and the **served**
    ``Last-Modified`` header are accepted. **Guessed** dates from the extraction
    library's content heuristic are deliberately rejected: on a site with no real
    per-document dates the heuristic emits one site-wide date stamped onto every
    record, which is worse than an honest ``null`` (it fabricates a recency signal
    a downstream ranker would act on). An honest ``null`` is preferred.
    """
    published = _first_date(
        [
            _jsonld_date(soup, "datePublished"),
            _meta_content(soup, "article:published_time"),
            _time_datetime(soup),
        ]
    )
    modified = _first_date(
        [
            _jsonld_date(soup, "dateModified"),
            _meta_content(soup, "article:modified_time"),
            last_modified,
        ]
    )
    return published, modified


def _first_date(candidates: list[str | None]) -> str | None:
    for candidate in candidates:
        if candidate:
            parsed = _to_iso_utc(candidate)
            if parsed:
                return parsed
    return None


def _to_iso_utc(value: str) -> str | None:
    """Parse a date string to ``YYYY-MM-DDTHH:MM:SSZ`` (UTC), or ``None``."""
    text = value.strip()
    if not text:
        return None
    # ISO8601, tolerating a trailing 'Z'.
    dt: datetime | None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        dt = _parse_http_or_date(text)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_http_or_date(text: str) -> datetime | None:
    """Parse an RFC-1123 HTTP date or a bare ``YYYY-MM-DD``."""
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _time_datetime(soup: BeautifulSoup) -> str | None:
    tag = soup.find("time", attrs={"datetime": True})
    if isinstance(tag, Tag):
        value = tag.get("datetime")
        if isinstance(value, str):
            return value
    return None


def _jsonld_date(soup: BeautifulSoup, key: str) -> str | None:
    for block in _jsonld_objects(soup):
        value = block.get(key)
        if isinstance(value, str):
            return value
    return None


# --------------------------------------------------------------------------- #
# extra bag
# --------------------------------------------------------------------------- #
def extract_extra(soup: BeautifulSoup) -> dict[str, object]:
    """Lift optional structured attributes (price, rating) from JSON-LD.

    Kept as an open bag rather than promoted to first-class fields, so the model
    stays generic. ``{}`` when no structured data is present.
    """
    extra: dict[str, object] = {}
    for block in _jsonld_objects(soup):
        if not _is_type(block, "Product"):
            continue
        offers = block.get("offers")
        if isinstance(offers, dict):
            for key in ("price", "priceCurrency", "availability"):
                if key in offers:
                    extra[key] = offers[key]
        rating = block.get("aggregateRating")
        if isinstance(rating, dict):
            for src, dst in (("ratingValue", "rating"), ("reviewCount", "review_count")):
                if src in rating:
                    extra[dst] = rating[src]
    return extra


# --------------------------------------------------------------------------- #
# Quality gate
# --------------------------------------------------------------------------- #
def quality_gate(doc: Document, min_word_count: int = 25) -> bool:
    """The keep decision: emit the document only if it clears the gate.

    ``keep = word_count >= min_word_count AND content_type != index``. Drops
    stubs and navigation pages, embodying *fewer-clean over more-dirty*.
    """
    return (
        doc.signals.word_count >= min_word_count
        and doc.signals.content_type is not ContentType.INDEX
    )


# Public alias matching the name used in docs/architecture.md.
keep = quality_gate


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _meta_content(soup: BeautifulSoup, prop: str) -> str | None:
    """``content`` of a ``<meta property=...>`` or ``<meta name=...>`` tag."""
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    if isinstance(tag, Tag):
        content = tag.get("content")
        if isinstance(content, str):
            return content
    return None


def _dedupe(items: list[str]) -> list[str]:
    """Trim, drop empties, and de-duplicate preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = item.strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _jsonld_objects(soup: BeautifulSoup) -> list[dict[str, object]]:
    """All JSON-LD objects on the page, flattening ``@graph`` containers."""
    objects: list[dict[str, object]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                objects.extend(node for node in graph if isinstance(node, dict))
            else:
                objects.append(item)
    return objects


def _jsonld_has_type(soup: BeautifulSoup, type_name: str) -> bool:
    return any(_is_type(block, type_name) for block in _jsonld_objects(soup))


def _is_type(block: dict[str, object], type_name: str) -> bool:
    value = block.get("@type")
    if isinstance(value, str):
        return value == type_name
    if isinstance(value, list):
        return type_name in value
    return False
