"""Main-content extraction: the layered validate-then-cascade.

The design goal is robustness to site changes, so the extractor is generic
rather than tuned to one site's selectors. It tries four layers in order and
accepts the first whose output *passes validation* — rejecting over-extraction
(too link-dense) and under-extraction (too short). Layer 4 is a guaranteed floor
that always returns something. The extraction library is parsed once; its
metadata is reused by enrichment regardless of which body layer wins. See
``docs/extraction.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import unquote, urlparse

import trafilatura
from bs4 import BeautifulSoup
from bs4.element import Tag

from .cleaner import (
    DEFAULT_PRUNE_LINK_DENSITY,
    DEFAULT_PRUNE_MIN_PROSE_WORDS,
    clean_text,
    link_density,
)

# ``link_density`` is defined in cleaner (shared by the prune) and re-exported
# here, so ``extractor.link_density`` stays the canonical reference for callers.
__all__ = ["ExtractionResult", "extract", "link_density", "resolve_title"]

# Block-level containers considered by the density heuristic (layer 3).
_DENSITY_CONTAINERS = ("article", "section", "main", "div")
# Trailing "_<id>" suffix on a URL slug, e.g. "a-light-in-the-attic_1000".
_SLUG_ID_SUFFIX = re.compile(r"_\d+$")
# Default index documents stripped when deriving a slug from a directory URL.
_DEFAULT_DOCS = {"index.html", "index.htm", "index.php", "default.html", "default.htm"}
# Separators that introduce a trailing " | Site Name" suffix on a <title>.
_TITLE_SUFFIX = re.compile(r"\s+[|–—\-]\s+.*$")


@dataclass
class ExtractionResult:
    """Everything enrichment needs off a single parse of one page.

    Body selection and metadata extraction are separate decisions taken off the
    same parse, so ``soup`` and ``meta`` are carried alongside the resolved
    ``title`` and ``body_text`` regardless of which body layer won.
    """

    title: str
    body_text: str
    soup: BeautifulSoup
    #: Normalized metadata dict from the extraction library (title/text/date/tags).
    meta: dict[str, object] = field(default_factory=dict)
    #: Which cascade layer produced the body, for logging / quality metrics.
    body_layer: str = ""


def extract(
    html: str,
    url: str,
    *,
    min_word_count: int = 25,
    link_density_threshold: float = 0.4,
    prune_link_density: float = DEFAULT_PRUNE_LINK_DENSITY,
    prune_min_prose_words: int = DEFAULT_PRUNE_MIN_PROSE_WORDS,
) -> ExtractionResult:
    """Extract a resolved title and clean ``body_text`` from a page.

    Args:
        html: The raw HTML of the page.
        url: The page URL (used for title slug-derivation and library parsing).
        min_word_count: Under-extraction floor; a candidate with fewer words is
            rejected and the cascade falls through.
        link_density_threshold: Over-extraction cutoff; a candidate whose links
            exceed this fraction of its text is rejected.
        prune_link_density: Structural-prune link-density gate (see cleaner).
        prune_min_prose_words: Structural-prune non-link prose floor (see cleaner).

    Returns:
        An :class:`ExtractionResult` carrying the title, body text, the parsed
        soup, and the library metadata for downstream enrichment.
    """
    soup = BeautifulSoup(html, "lxml")
    meta = _library_metadata(html, url)

    body_text, layer = _select_body(
        soup,
        meta,
        min_word_count=min_word_count,
        link_density_threshold=link_density_threshold,
        prune_link_density=prune_link_density,
        prune_min_prose_words=prune_min_prose_words,
    )
    title = resolve_title(soup, meta, url)
    return ExtractionResult(
        title=title, body_text=body_text, soup=soup, meta=meta, body_layer=layer
    )


def _library_metadata(html: str, url: str) -> dict[str, object]:
    """Parse the page once with the extraction library and normalize the result.

    Date extraction is restricted to machine-readable sources (no extensive
    plain-text guessing), keeping dates to genuinely declared values. Any failure
    yields an empty dict; metadata is always best-effort.
    """
    try:
        doc = trafilatura.bare_extraction(
            html,
            url=url,
            with_metadata=True,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            date_extraction_params={"extensive_search": False, "original_date": True},
        )
    except Exception:  # noqa: BLE001 — metadata is best-effort; never fatal.
        return {}
    if doc is None:
        return {}
    data = doc.as_dict() if hasattr(doc, "as_dict") else doc
    return cast("dict[str, object]", data) if isinstance(data, dict) else {}


def _select_body(
    soup: BeautifulSoup,
    meta: dict[str, object],
    *,
    min_word_count: int,
    link_density_threshold: float,
    prune_link_density: float,
    prune_min_prose_words: int,
) -> tuple[str, str]:
    """Run the four-layer cascade; return the first passing body and its layer.

    Layer 4 (crude) is the floor and is returned unconditionally if nothing else
    passes, so a page with any body at all always yields a result.
    """

    def passes(text: str, link_density_value: float) -> bool:
        return len(text.split()) >= min_word_count and link_density_value <= link_density_threshold

    def clean(html_or_text: str) -> str:
        return clean_text(
            html_or_text,
            prune_link_density=prune_link_density,
            prune_min_prose_words=prune_min_prose_words,
        )

    # Layer 1 — Semantic HTML5: main / [role=main] / article.
    semantic = _semantic_block(soup)
    if semantic is not None:
        text = clean(str(semantic))
        if passes(text, link_density(semantic)):
            return text, "semantic"

    # Layer 2 — the bought extractor's text (already boilerplate-stripped).
    library_text = meta.get("text")
    if isinstance(library_text, str) and library_text.strip():
        text = clean(library_text)
        if passes(text, 0.0):
            return text, "library"

    # Layer 3 — density heuristic: the block with the most non-link text.
    densest = _densest_block(soup)
    if densest is not None:
        text = clean(str(densest))
        if passes(text, link_density(densest)):
            return text, "density"

    # Layer 4 — crude fallback: strip chrome, take the body text. Always returns.
    body = soup.body or soup
    return clean(str(body)), "crude"


def _semantic_block(soup: BeautifulSoup) -> Tag | None:
    """Find the first semantic main-content element, if any."""
    main = soup.find("main")
    if isinstance(main, Tag):
        return main
    role_main = soup.find(attrs={"role": "main"})
    if isinstance(role_main, Tag):
        return role_main
    article = soup.find("article")
    return article if isinstance(article, Tag) else None


def _densest_block(soup: BeautifulSoup) -> Tag | None:
    """Pick the block-level container with the most non-link text.

    Non-link text length (``total * (1 - link_density)``) rewards prose-heavy
    blocks and penalizes navigation, which is the signal the heuristic wants.
    """
    best: Tag | None = None
    best_score = 0.0
    for tag in soup.find_all(_DENSITY_CONTAINERS):
        if not isinstance(tag, Tag):
            continue
        total = len(tag.get_text(strip=True))
        if total == 0:
            continue
        score = total * (1.0 - link_density(tag))
        if score > best_score:
            best, best_score = tag, score
    return best


def resolve_title(soup: BeautifulSoup, meta: dict[str, object], url: str) -> str:
    """Resolve the page title by precedence cascade; the first non-empty wins.

    Order: ``og:title`` -> ``<h1>`` -> library title -> ``<title>`` (site suffix
    stripped) -> slug-derived -> ``""``. The result is always a string.
    """
    library_title = meta.get("title")
    candidates: tuple[str | None, ...] = (
        _meta_content(soup, "og:title"),
        _first_text(soup, "h1"),
        library_title if isinstance(library_title, str) else None,
        _title_tag(soup),
        _slug_title(url),
    )
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def _meta_content(soup: BeautifulSoup, prop: str) -> str | None:
    """Return the ``content`` of a ``<meta property=...>`` (or ``name=...``) tag."""
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    if isinstance(tag, Tag):
        content = tag.get("content")
        if isinstance(content, str):
            return content
    return None


def _first_text(soup: BeautifulSoup, name: str) -> str | None:
    tag = soup.find(name)
    return tag.get_text(strip=True) if isinstance(tag, Tag) else None


def _title_tag(soup: BeautifulSoup) -> str | None:
    """The ``<title>`` text with any trailing ``" | Site Name"`` suffix removed."""
    tag = soup.find("title")
    if not isinstance(tag, Tag):
        return None
    return _TITLE_SUFFIX.sub("", tag.get_text(strip=True)).strip()


def _slug_title(url: str) -> str | None:
    """Best-effort title from the URL slug: strip ``_<id>``, de-dash, title-case.

    On a non-slug URL this yields junk that the caller treats as a last resort
    before falling through to ``""``.
    """
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    segment = path.rsplit("/", 1)[-1]
    if segment.lower() in _DEFAULT_DOCS:
        # Directory-style URL: derive from the parent path segment instead.
        parent = path.rsplit("/", 2)
        segment = parent[-2] if len(parent) >= 2 else ""
    segment = unquote(segment)
    segment = re.sub(r"\.(html?|php|aspx?)$", "", segment, flags=re.IGNORECASE)
    segment = _SLUG_ID_SUFFIX.sub("", segment)
    words = segment.replace("-", " ").replace("_", " ").strip()
    return words.title() if words else None
