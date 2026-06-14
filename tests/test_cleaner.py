"""Unit tests for ``clean_text`` — the ordered cleaning pipeline."""

from __future__ import annotations

from extractor_engine.engine.cleaner import clean_text


def test_drops_chrome_before_grabbing_text() -> None:
    """Nav/header/footer/aside text must not leak into the body."""
    html = """
    <div>
      <nav><a href="/">Home</a> <a href="/about">About</a></nav>
      <header>Site Banner</header>
      <p>The real article content lives here.</p>
      <aside>Related links sidebar</aside>
      <footer>Copyright 2024 ACME</footer>
    </div>
    """
    out = clean_text(html)
    assert "The real article content lives here." in out
    for chrome in ("Home", "About", "Site Banner", "Related links sidebar", "Copyright 2024"):
        assert chrome not in out


def test_drops_non_content_elements() -> None:
    html = "<div><script>var x=1;</script><style>.a{}</style><p>Visible text body here.</p></div>"
    out = clean_text(html)
    assert out == "Visible text body here."


def test_decodes_html_entities() -> None:
    out = clean_text("<p>Tom &amp; Jerry &lt;3 &quot;quotes&quot;</p>")
    assert out == 'Tom & Jerry <3 "quotes"'


def test_normalizes_whitespace() -> None:
    out = clean_text("<p>too    many     spaces</p>\n\n\n\n<p>and lines</p>")
    assert "too many spaces" in out
    assert "\n\n\n" not in out  # 3+ newlines collapsed to a paragraph break


def test_drops_boilerplate_lines() -> None:
    html = """
    <div>
      <p>Skip to content</p>
      <p>We use cookies to improve your experience. Accept cookies?</p>
      <p>Genuine paragraph of real content worth keeping.</p>
      <p>© 2024 ACME Corp</p>
    </div>
    """
    out = clean_text(html)
    assert "Genuine paragraph of real content worth keeping." in out
    assert "Skip to content" not in out
    assert "cookies" not in out.lower()
    assert "©" not in out


def test_plain_text_passes_through() -> None:
    """Already-plain text (the library layer's output) normalizes unharmed."""
    assert clean_text("A Light in the Attic\n\nIt's a poem.") == "A Light in the Attic\n\nIt's a poem."


def test_empty_input() -> None:
    assert clean_text("") == ""

def test_trims_trailing_related_content_block() -> None:
    """A 'recently viewed' / related block after real content is dropped, but the
    content before it is kept."""
    content = "This is the genuine main article body with plenty of real words here. " * 5
    html = f"<div><p>{content}</p><p>Products you recently viewed</p><p>Book A</p><p>Book B</p></div>"
    out = clean_text(html)
    assert "genuine main article body" in out
    assert "recently viewed" not in out.lower()
    assert "Book A" not in out


def test_does_not_trim_related_heading_without_enough_content() -> None:
    """If a related heading appears before any substantial content, it is NOT
    treated as a trailing block (the page might *be* a link list)."""
    html = "<div><p>You may also like</p><p>Book A</p><p>Book B</p></div>"
    out = clean_text(html)
    assert "You may also like" in out
