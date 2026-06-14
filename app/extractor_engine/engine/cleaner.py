"""The ordered cleaning pipeline that turns a selected HTML block into body_text.

The order is critical: chrome (``<nav>``/``<header>``/``<footer>``/``<aside>`` and
role/class-flagged UI widgets) must be removed *before* text is grabbed, or its
menu items and notice-banner text leak into ``body_text``. Link-dense furniture
that no chrome tag marks — recommendation strips, "related" carousels, link-list
footers — is removed *structurally* (by the shape of a block, never by matching
any site's wording). See ``docs/extraction.md``.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from bs4.element import Tag

# Elements that never carry main content.
_NON_CONTENT_TAGS = ("script", "style", "noscript")
# Site chrome: navigation and structural furniture around the content.
_CHROME_TAGS = ("nav", "header", "footer", "aside")
# Non-content UI widgets flagged by ARIA role — alerts, cookie/consent dialogs,
# banners, navigation. A generic category of furniture, matched without naming
# any site.
_CHROME_ROLE = re.compile(r"^(?:alert|alertdialog|dialog|banner|navigation|complementary)$", re.I)
# ...and the same generic category of UI furniture by conventional class names
# (alert / banner / cookie / consent / promo widgets). Related-content carousels
# are deliberately NOT matched here by name — they are removed structurally
# below, by shape, so the rule generalizes across sites and wordings.
_CHROME_CLASS = re.compile(
    r"\b(?:alert|banner|cookie|consent|gdpr|promo|toast|modal|popup)\b",
    re.I,
)

# Block-level containers considered for structural pruning of link-dense furniture.
_PRUNE_CONTAINERS = ("div", "section", "aside", "footer", "ul", "ol", "nav", "dl")

# Conservative defaults for the structural dual gate (also mirrored in
# config.Settings so a run can tune them). A descendant block is pruned only if
# its link density is at least this AND its non-link prose is below the floor.
DEFAULT_PRUNE_LINK_DENSITY = 0.5
DEFAULT_PRUNE_MIN_PROSE_WORDS = 20

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


def link_density(element: Tag) -> float:
    """Fraction of an element's text that lives inside ``<a>`` links.

    Returns ``1.0`` for an element with no text at all, so empty blocks read as
    fully non-content. This is the over-extraction symptom the extractor's
    validation checks and the link-density half of the structural prune's gate.
    """
    total = len(element.get_text(strip=True))
    if total == 0:
        return 1.0
    link_chars = sum(len(a.get_text(strip=True)) for a in element.find_all("a"))
    return link_chars / total


def _non_link_word_count(element: Tag) -> int:
    """Number of whitespace-delimited words *outside* any ``<a>`` link."""
    total = len(element.get_text(" ", strip=True).split())
    link_words = len(" ".join(a.get_text(" ", strip=True) for a in element.find_all("a")).split())
    return total - link_words


def clean_text(
    html: str,
    *,
    prune_link_density: float = DEFAULT_PRUNE_LINK_DENSITY,
    prune_min_prose_words: int = DEFAULT_PRUNE_MIN_PROSE_WORDS,
) -> str:
    """Clean a selected HTML block into plain ``body_text``.

    Runs the fixed pipeline from ``docs/extraction.md``: drop non-content
    elements, drop chrome (tags + role/class-flagged widgets) *before* grabbing
    text, structurally prune link-dense low-prose descendant blocks, strip
    remaining tags, decode entities, normalize whitespace, and drop boilerplate
    lines. Plain text (no tags) passes through unharmed, so library-extracted
    text can be funneled through the same normalizer.

    Args:
        html: An HTML fragment (or already-plain text) for the selected block.
        prune_link_density: A descendant block is a prune candidate when its link
            density is at least this.
        prune_min_prose_words: ...and when its non-link prose is below this floor.

    Returns:
        The cleaned main prose, with chrome and link-dense furniture removed and
        whitespace normalized.
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

    # Structurally prune link-dense, low-prose descendant blocks (recommendation
    # strips, "related"/"recently viewed" carousels, link-list footers) that no
    # chrome tag marks — by shape, never by wording. Never the selected block.
    _prune_link_dense_furniture(soup, prune_link_density, prune_min_prose_words)

    # 3 + 4. Strip remaining tags to text; BeautifulSoup decodes HTML entities.
    # A newline separator keeps block-level structure as line breaks.
    text = soup.get_text(separator="\n")

    return _normalize_whitespace_and_boilerplate(text)


def _prune_link_dense_furniture(
    soup: BeautifulSoup, max_link_density: float, min_prose_words: int
) -> None:
    """Remove descendant blocks that clear the dual gate, never the root block.

    The selected main block is the protected root — a page that is *wholly* a link
    list (a listing page) is handled upstream by the ``index`` classification and
    the quality gate, not by pruning its body to nothing. Only its descendant
    blocks are eligible. Among eligible blocks, only the **outermost** are removed
    (decomposing a block takes its nested blocks with it).
    """
    body = soup.body if isinstance(soup.body, Tag) else soup
    children = [child for child in body.children if isinstance(child, Tag)]
    # A single wrapping element is the selected block; otherwise the body is.
    root: Tag = children[0] if len(children) == 1 else body

    candidates = [
        tag
        for tag in root.find_all(_PRUNE_CONTAINERS)
        if tag is not root
        and link_density(tag) >= max_link_density
        and _non_link_word_count(tag) < min_prose_words
    ]
    outermost = [
        tag for tag in candidates if not any(other in tag.parents for other in candidates if other is not tag)
    ]
    for tag in outermost:
        tag.decompose()


def _normalize_whitespace_and_boilerplate(text: str) -> str:
    """Steps 5 and 6 of the pipeline: whitespace normalization, boilerplate drop."""
    # 5. Collapse intra-line whitespace and trim each line.
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]

    # 6. Drop boilerplate lines (cookie banners, skip links, lone copyright).
    kept = [line for line in lines if not (line and _BOILERPLATE_LINE.match(line))]

    # Reassemble, then collapse 3+ newlines to a paragraph break and trim ends.
    collapsed = _BLANK_LINES.sub("\n\n", "\n".join(kept))
    return collapsed.strip()
