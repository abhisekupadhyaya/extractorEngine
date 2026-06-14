"""Tests for the validate-then-cascade extractor and title resolution.

These target the critical robustness layer: a layer that over-extracts
(too link-dense) or under-extracts (too short) is rejected, and the cascade
falls through. See ``docs/extraction.md``.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from extractor_engine.engine.extractor import extract, link_density, resolve_title

# A clean prose paragraph (>25 words) used as the "good" block to fall through to.
PROSE = (
    "This is the genuine main article content of the page. It is a long paragraph "
    "of real prose that easily clears the minimum word count threshold and carries "
    "no navigation links whatsoever, so it passes validation cleanly."
)


def test_over_extraction_rejected_on_link_density() -> None:
    """A link-dense semantic block is rejected; the cascade finds the prose."""
    links = "".join(f'<a href="/{i}">link number {i}</a> ' for i in range(40))
    html = f"""
    <html><body>
      <article>{links}</article>
      <div id="main"><p>{PROSE}</p></div>
    </body></html>
    """
    result = extract(html, "https://x.com/page")
    assert result.body_layer != "semantic"  # the article was rejected
    assert "genuine main article content" in result.body_text
    assert "link number 1" not in result.body_text


def test_under_extraction_rejected_on_length() -> None:
    """A too-short semantic block is rejected; the cascade finds the longer prose."""
    html = f"""
    <html><body>
      <article>Tiny stub.</article>
      <div id="main"><p>{PROSE}</p></div>
    </body></html>
    """
    result = extract(html, "https://x.com/page")
    assert result.body_layer != "semantic"
    assert "genuine main article content" in result.body_text


def test_crude_floor_never_raises_and_returns_something() -> None:
    """Even when every layer fails validation, layer 4 returns a result."""
    result = extract("<html><body><p>Just a few words here.</p></body></html>", "https://x.com/p")
    assert result.body_layer == "crude"
    assert "Just a few words here." in result.body_text


def test_link_density_helper() -> None:
    dense = BeautifulSoup('<div><a href="/">aaaa</a>bb</div>', "lxml").div
    assert link_density(dense) == 4 / 6
    empty = BeautifulSoup("<div></div>", "lxml").div
    assert link_density(empty) == 1.0  # no text reads as fully non-content


class TestTitleResolution:
    def test_og_title_wins(self) -> None:
        html = (
            '<html><head><meta property="og:title" content="OG Title">'
            "<title>Tag Title | Site</title></head><body><h1>H1 Title</h1></body></html>"
        )
        assert resolve_title(BeautifulSoup(html, "lxml"), {}, "https://x.com/p") == "OG Title"

    def test_h1_when_no_og(self) -> None:
        html = "<html><head><title>Tag | Site</title></head><body><h1>The H1</h1></body></html>"
        assert resolve_title(BeautifulSoup(html, "lxml"), {}, "https://x.com/p") == "The H1"

    def test_title_tag_suffix_stripped(self) -> None:
        html = "<html><head><title>Real Title | My Site Name</title></head><body></body></html>"
        assert resolve_title(BeautifulSoup(html, "lxml"), {}, "https://x.com/p") == "Real Title"

    def test_slug_derived_last_resort(self) -> None:
        html = "<html><body></body></html>"
        title = resolve_title(BeautifulSoup(html, "lxml"), {}, "https://x.com/a-light-in-the-attic_1000/")
        assert title == "A Light In The Attic"

    def test_empty_when_nothing_usable(self) -> None:
        html = "<html><body></body></html>"
        assert resolve_title(BeautifulSoup(html, "lxml"), {}, "https://x.com/") == ""
