"""Golden-file tests: pin the whole transform on real saved pages.

The single highest-leverage regression guard — any change that alters output on
representative real input is caught here. See ``docs/testing.md``.
"""

from __future__ import annotations

import json

from conftest import GOLDEN_FETCHED_AT, GOLDEN_LAST_MODIFIED, load_fixture

from extractor_engine.engine.enricher import enrich, quality_gate
from extractor_engine.engine.extractor import extract

BOOK_URL = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/"
CATEGORY_URL = "https://books.toscrape.com/catalogue/category/books/poetry_23/"


def _transform(html: str, url: str):
    return enrich(
        extract(html, url),
        url=url,
        fetched_at=GOLDEN_FETCHED_AT,
        last_modified=GOLDEN_LAST_MODIFIED,
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
