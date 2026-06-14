"""Unit tests for enrichment: signals, classification, tags, dates, gate.

Each rule is exercised including its empty/missing case, enforcing the
null/empty-safe contracts from ``docs/data-model.md``.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from extractor_engine.engine.enricher import (
    classify_content_type,
    detect_language,
    extract_dates,
    extract_extra,
    extract_tags,
    is_mostly_code,
    quality_gate,
)
from extractor_engine.engine.models import ContentType, Document, Signals

THRESHOLD = 0.4


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


# --------------------------------------------------------------------------- #
# content_type
# --------------------------------------------------------------------------- #
class TestClassifyContentType:
    def test_product_page_from_price_widget(self) -> None:
        html = (
            '<html><body><article><h1>Book</h1>'
            '<p class="price_color">£51.77</p><p class="instock availability">In stock</p>'
            "<p>A real description of the product goes here.</p></article></body></html>"
        )
        assert classify_content_type(soup(html), "https://x.com/book_1", THRESHOLD) == ContentType.PRODUCT_PAGE

    def test_link_dense_listing_is_index(self) -> None:
        links = "".join(f'<a href="/{i}">Book Title Number {i}</a>' for i in range(40))
        html = f'<html><body><section>{links}<p class="price_color">£1</p></section></body></html>'
        assert classify_content_type(soup(html), "https://x.com/category/poetry", THRESHOLD) == ContentType.INDEX

    def test_listing_url_pattern_is_index(self) -> None:
        html = "<html><body><p>short</p></body></html>"
        assert classify_content_type(soup(html), "https://x.com/category/books", THRESHOLD) == ContentType.INDEX

    def test_article_element_is_article(self) -> None:
        html = "<html><body><article><p>" + ("word " * 60) + "</p></article></body></html>"
        assert classify_content_type(soup(html), "https://x.com/blog/post", THRESHOLD) == ContentType.ARTICLE

    def test_doc_path_is_doc_page(self) -> None:
        html = "<html><body><article><p>" + ("doc " * 60) + "</p></article></body></html>"
        assert classify_content_type(soup(html), "https://x.com/docs/install", THRESHOLD) == ContentType.DOC_PAGE

    def test_unknown_falls_back_to_other(self) -> None:
        html = "<html><body><div><p>Some standalone prose with no markers at all.</p></div></body></html>"
        assert classify_content_type(soup(html), "https://x.com/about", THRESHOLD) == ContentType.OTHER


# --------------------------------------------------------------------------- #
# is_mostly_code
# --------------------------------------------------------------------------- #
class TestIsMostlyCode:
    def test_code_heavy_page(self) -> None:
        html = "<html><body><p>Hi</p><pre>" + ("x = 1\n" * 50) + "</pre></body></html>"
        assert is_mostly_code(soup(html), 0.5) is True

    def test_prose_page(self) -> None:
        html = "<html><body><p>" + ("prose " * 100) + "</p><code>x</code></body></html>"
        assert is_mostly_code(soup(html), 0.5) is False

    def test_nested_code_in_pre_counted_once(self) -> None:
        html = (
            "<html><body><p>some prose text here for balance and length</p>"
            "<pre><code>" + ("a" * 30) + "</code></pre></body></html>"
        )
        # Should not double-count; with substantial prose this stays under threshold.
        assert is_mostly_code(soup(html), 0.5) is False

    def test_empty_page(self) -> None:
        assert is_mostly_code(soup("<html></html>"), 0.5) is False


# --------------------------------------------------------------------------- #
# language
# --------------------------------------------------------------------------- #
class TestDetectLanguage:
    def test_english_prose(self) -> None:
        text = (
            "The quick brown fox jumps over the lazy dog and then runs across "
            "the wide green field every single morning."
        )
        assert detect_language(text) == "en"

    def test_too_short_is_undetermined(self) -> None:
        assert detect_language("two words") == "und"

    def test_empty_is_undetermined(self) -> None:
        assert detect_language("") == "und"


# --------------------------------------------------------------------------- #
# tags
# --------------------------------------------------------------------------- #
class TestExtractTags:
    def test_breadcrumb_drops_home_and_current(self) -> None:
        html = """<ul class="breadcrumb">
            <li><a href="/">Home</a></li>
            <li><a href="/books">Books</a></li>
            <li><a href="/poetry">Poetry</a></li>
            <li class="active">A Light in the Attic</li>
        </ul>"""
        assert extract_tags(soup(html), {}) == ["Books", "Poetry"]

    def test_meta_keywords(self) -> None:
        html = '<html><head><meta name="keywords" content="python, scraping, rag"></head><body></body></html>'
        assert extract_tags(soup(html), {}) == ["python", "scraping", "rag"]

    def test_library_tags_merged_and_deduped(self) -> None:
        html = '<html><head><meta name="keywords" content="poetry"></head></html>'
        assert extract_tags(soup(html), {"tags": ["poetry", "verse"]}) == ["poetry", "verse"]

    def test_no_sources_yields_empty(self) -> None:
        assert extract_tags(soup("<html><body><p>x</p></body></html>"), {}) == []


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #
class TestExtractDates:
    def test_guessed_date_rejected_served_modified_kept(self) -> None:
        # A non-standard <meta name="created"> is a content-heuristic ("guessed")
        # source and is rejected; the served Last-Modified header is accepted.
        html = '<html><head><meta name="created" content="24th Jun 2016 09:29"></head></html>'
        published, modified = extract_dates(soup(html), "Wed, 08 Feb 2023 21:02:32 GMT")
        assert published is None
        assert modified == "2023-02-08T21:02:32Z"

    def test_article_published_time_iso(self) -> None:
        html = '<html><head><meta property="article:published_time" content="2021-03-04T08:00:00+00:00"></head></html>'
        published, _ = extract_dates(soup(html), None)
        assert published == "2021-03-04T08:00:00Z"

    def test_jsonld_date_published_accepted(self) -> None:
        ld = '{"@type":"Article","datePublished":"2020-01-02"}'
        html = f'<html><head><script type="application/ld+json">{ld}</script></head></html>'
        published, _ = extract_dates(soup(html), None)
        assert published == "2020-01-02T00:00:00Z"

    def test_no_dates_are_null(self) -> None:
        published, modified = extract_dates(soup("<html><body></body></html>"), None)
        assert published is None and modified is None


# --------------------------------------------------------------------------- #
# extra
# --------------------------------------------------------------------------- #
class TestExtractExtra:
    def test_jsonld_product_offers(self) -> None:
        html = """<html><head><script type="application/ld+json">
        {"@type": "Product", "offers": {"price": "19.99", "priceCurrency": "USD"},
         "aggregateRating": {"ratingValue": "4.5", "reviewCount": "10"}}
        </script></head></html>"""
        extra = extract_extra(soup(html))
        assert extra == {"price": "19.99", "priceCurrency": "USD", "rating": "4.5", "review_count": "10"}

    def test_no_structured_data_is_empty(self) -> None:
        assert extract_extra(soup("<html><body><p>x</p></body></html>")) == {}


# --------------------------------------------------------------------------- #
# quality gate
# --------------------------------------------------------------------------- #
def _doc(word_count: int, content_type: ContentType) -> Document:
    return Document(
        id="i",
        url="https://x.com/p",
        title="t",
        body_text="b",
        fetched_at="2026-06-14T12:00:00Z",
        content_hash="h",
        signals=Signals(
            word_count=word_count, char_count=10, language="en",
            content_type=content_type, is_mostly_code=False,
        ),
    )


class TestQualityGate:
    @pytest.mark.parametrize(
        "word_count,content_type,expected",
        [
            (100, ContentType.PRODUCT_PAGE, True),
            (100, ContentType.ARTICLE, True),
            (24, ContentType.PRODUCT_PAGE, False),  # too short
            (25, ContentType.PRODUCT_PAGE, True),  # exactly the floor
            (500, ContentType.INDEX, False),  # index is never kept
        ],
    )
    def test_gate(self, word_count: int, content_type: ContentType, expected: bool) -> None:
        assert quality_gate(_doc(word_count, content_type)) is expected
