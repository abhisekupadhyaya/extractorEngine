"""The ordered cleaning pipeline that turns a selected HTML block into body_text.

The order is critical: chrome (``<nav>``/``<header>``/``<footer>``/``<aside>``)
must be removed *before* text is grabbed, or its menu items and footer links leak
into ``body_text``. See ``docs/extraction.md``.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

# Elements that never carry main content.
_NON_CONTENT_TAGS = ("script", "style", "noscript")
# Site chrome: navigation and structural furniture around the content.
_CHROME_TAGS = ("nav", "header", "footer", "aside")
# Non-content UI widgets flagged by ARIA role — alerts, cookie/consent dialogs,
# banners. A generic category of furniture, matched without naming any site.
_CHROME_ROLE = re.compile(r"^(?:alert|alertdialog|dialog|banner|navigation|complementary)$", re.I)
# ...and the same widgets by conventional class names, plus related-content
# carousels / "recently viewed" / recommendation strips (non-content furniture).
_CHROME_CLASS = re.compile(
    r"\b(?:alert|banner|cookie|consent|gdpr|promo|toast|modal|popup"
    r"|carousel|related|recommend|recently|upsell|also-bought)\b",
    re.I,
)

# Headings that introduce a trailing related-content block (carousel text that
# survives into the library extractor's flattened output, where tag/class removal
# can't reach it). Matched only in the latter part of the body and, when found,
# everything from the heading onward is dropped — so main content is never cut.
_RELATED_HEADING = re.compile(
    r"^(?:products?\s+you\s+recently\s+viewed"
    r"|you\s+(?:might|may)\s+also\s+like"
    r"|related\s+(?:products?|posts?|articles?|items?|reads?)"
    r"|recommended(?:\s+for\s+you)?"
    r"|customers\s+also\s+(?:bought|viewed))\b",
    re.I,
)
# Minimum real-content words before a related-content heading for it to be trimmed
# (so a page that is mostly a link list up front is never cut).
_MIN_CONTENT_WORDS = 40

# Runs of spaces/tabs (but not newlines) collapse to a single space.
_SPACES = re.compile(r"[^\S\n]+")
# Three-or-more consecutive newlines collapse down to a paragraph break (two).
_BLANK_LINES = re.compile(r"\n{3,}")

# Boilerplate lines that survive tag-stripping but are not content. Matched
# case-insensitively against a whole trimmed line.
_BOILERPLATE_LINE = re.compile(
    r"""
    ^\s*(?:
        (?:.*\bcookie(?:s)?\b.*(?:\baccept\b|\bpolicy\b|\bconsent\b|\bwe\ use\b).*)  # cookie banners
      | (?:accept\ (?:all\ )?cookies?)
      | (?:skip\ to\ (?:main\ )?content)                                            # skip links
      | (?:skip\ to\ navigation)
      | (?:(?:©|\(c\)|copyright)\s.*)                                          # lone copyright lines
      | (?:all\ rights\ reserved\.?)
    )\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def clean_text(html: str) -> str:
    """Clean a selected HTML block into plain ``body_text``.

    Runs the fixed pipeline from ``docs/extraction.md``: drop non-content
    elements, drop chrome *before* grabbing text, strip remaining tags, decode
    entities, normalize whitespace, and drop boilerplate lines. Plain text (no
    tags) passes through unharmed, so library-extracted text can be funneled
    through the same normalizer.

    Args:
        html: An HTML fragment (or already-plain text) for the selected block.

    Returns:
        The cleaned main prose, with chrome removed and whitespace normalized.
    """
    soup = BeautifulSoup(html, "lxml")

    # 1 + 2. Drop non-content and chrome elements before any text is grabbed.
    for tag in soup.find_all([*_NON_CONTENT_TAGS, *_CHROME_TAGS]):
        tag.decompose()
    # Also drop role/class-flagged UI chrome (alert/cookie/banner/promo widgets) —
    # a generic category of non-content furniture, matched without naming any site.
    for element in soup.find_all(attrs={"role": _CHROME_ROLE}):
        element.decompose()
    for element in soup.find_all(class_=_CHROME_CLASS):
        element.decompose()

    # 3 + 4. Strip remaining tags to text; BeautifulSoup decodes HTML entities.
    # A newline separator keeps block-level structure as line breaks.
    text = soup.get_text(separator="\n")

    return _normalize_whitespace_and_boilerplate(text)


def _normalize_whitespace_and_boilerplate(text: str) -> str:
    """Steps 5 and 6 of the pipeline: whitespace normalization, boilerplate drop."""
    # 5. Collapse intra-line whitespace and trim each line.
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]

    # 6. Drop boilerplate lines (cookie banners, skip links, lone copyright).
    kept = [line for line in lines if not (line and _BOILERPLATE_LINE.match(line))]

    # 7. Trim a trailing related-content block ("recently viewed" carousels etc.).
    kept = _trim_trailing_related(kept)

    # Reassemble, then collapse 3+ newlines to a paragraph break and trim ends.
    collapsed = _BLANK_LINES.sub("\n\n", "\n".join(kept))
    return collapsed.strip()


def _trim_trailing_related(lines: list[str]) -> list[str]:
    """Drop a trailing related-content block, if present.

    Looks for a related-content heading (carousel, "recently viewed", "you may
    also like", ...) and, if found *after enough real content*, drops it and
    everything after it. The gate is the word count preceding the heading (not its
    line position, which a large carousel would skew), so main content is never
    cut: a page that is mostly a link list up front is left untouched.
    """
    words_before = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and words_before >= _MIN_CONTENT_WORDS and _RELATED_HEADING.match(stripped):
            return lines[:i]
        words_before += len(stripped.split())
    return lines
