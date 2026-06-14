"""Golden-file tests: pin the whole transform on real saved pages.

The single highest-leverage regression guard — any change that alters output on
representative real input is caught here. See ``docs/testing.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import GOLDEN_FETCHED_AT, GOLDEN_LAST_MODIFIED, load_fixture

from extractor_engine.engine.enricher import enrich, quality_gate
from extractor_engine.engine.extractor import extract
from extractor_engine.storage.base import StoreAction
from extractor_engine.storage.jsonl import JSONLStore

BOOK_URL = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/"
CATEGORY_URL = "https://books.toscrape.com/catalogue/category/books/poetry_23/"
# A second site — quotes.toscrape.com — confirms the generic pipeline is not
# overfit to one layout. See docs/testing.md.
QUOTES_HOME_URL = "https://quotes.toscrape.com/"
QUOTES_TAG_URL = "https://quotes.toscrape.com/tag/love/"


def _transform(html: str, url: str, last_modified: str | None = GOLDEN_LAST_MODIFIED):
    return enrich(
        extract(html, url),
        url=url,
        fetched_at=GOLDEN_FETCHED_AT,
        last_modified=last_modified,
    )


def test_book_page_matches_golden() -> None:
    """A real product page produces exactly the expected document object."""
    doc = _transform(load_fixture("book_a-light-in-the-attic.html"), BOOK_URL)
    expected = json.loads(load_fixture("book_a-light-in-the-attic.expected.json"))
    assert doc.model_dump() == expected


def test_book_page_is_kept_and_clean() -> None:
    """Spot-check the qualities the golden file pins, as readable assertions."""
    doc = _transform(load_fixture("book_a-light-in-the-attic.html"), BOOK_URL)
    assert quality_gate(doc) is True
    assert doc.title == "A Light in the Attic"
    assert doc.tags == ["Books", "Poetry"]
    assert doc.signals.content_type.value == "product_page"
    assert doc.signals.language == "en"
    # No navigation/breadcrumb/footer leaked into the body.
    for chrome in ("Home", "breadcrumb", "Tipping the Velvet", "© 2024", "Next"):
        assert chrome not in doc.body_text


def test_index_page_is_not_kept() -> None:
    """A category/listing page is classified index and dropped by the gate."""
    doc = _transform(load_fixture("index_poetry_category.html"), CATEGORY_URL)
    assert doc.signals.content_type.value == "index"
    assert quality_gate(doc) is False


# --------------------------------------------------------------------------- #
# Second site: quotes.toscrape.com
# --------------------------------------------------------------------------- #
def test_quotes_home_matches_golden() -> None:
    """A real quotes.toscrape content page produces the expected document.

    The pages carry no served date, so ``last_modified`` is ``None`` here.
    """
    doc = _transform(load_fixture("quotes_home.html"), QUOTES_HOME_URL, last_modified=None)
    expected = json.loads(load_fixture("quotes_home.expected.json"))
    assert doc.model_dump() == expected


def test_quotes_home_is_kept_with_tags() -> None:
    """The generic tag sources populate tags on a different site's markup."""
    doc = _transform(load_fixture("quotes_home.html"), QUOTES_HOME_URL, last_modified=None)
    assert quality_gate(doc) is True
    assert doc.tags  # tags populated from the quote tags — not empty
    assert "love" in doc.tags
    assert doc.signals.language == "en"


def test_quotes_tag_listing_is_not_kept() -> None:
    """A quotes tag page is a listing: classified index and dropped."""
    doc = _transform(load_fixture("quotes_tag_love.html"), QUOTES_TAG_URL, last_modified=None)
    assert doc.signals.content_type.value == "index"
    assert quality_gate(doc) is False


def test_author_article_matches_golden() -> None:
    """A JSON-LD-author page produces the expected document, author populated."""
    url = "https://example.com/blog/on-writing-testable-code/"
    doc = _transform(load_fixture("article_with_author.html"), url, last_modified=None)
    expected = json.loads(load_fixture("article_with_author.expected.json"))
    assert doc.model_dump() == expected
    assert doc.author == "Ada Lovelace"  # primary (first) of two co-authors


def test_toscrape_goldens_have_no_author() -> None:
    """The sandbox pages declare no author, so author is null (not invented)."""
    book = _transform(load_fixture("book_a-light-in-the-attic.html"), BOOK_URL)
    quotes = _transform(load_fixture("quotes_home.html"), QUOTES_HOME_URL, last_modified=None)
    assert book.author is None
    assert quotes.author is None


def test_quotes_rerun_adds_zero_records(tmp_path: Path) -> None:
    """Re-running over the same quotes content yields zero new records."""
    doc = _transform(load_fixture("quotes_home.html"), QUOTES_HOME_URL, last_modified=None)
    output = tmp_path / "quotes.jsonl"

    first = JSONLStore(output)
    assert first.handle(doc) == StoreAction.INSERT
    first.finalize()

    second = JSONLStore(output)  # seeds state from the file written above
    assert second.handle(doc) == StoreAction.SKIP  # unchanged -> zero new
    second.finalize()
    assert len(output.read_text().splitlines()) == 1
