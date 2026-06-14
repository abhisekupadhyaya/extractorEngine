"""Unit tests for ``clean_text`` — the ordered cleaning pipeline."""

from __future__ import annotations

from conftest import load_fixture

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


# --------------------------------------------------------------------------- #
# Structural pruning of link-dense furniture (see docs/extraction.md)
# --------------------------------------------------------------------------- #
def test_prunes_link_dense_low_prose_descendant() -> None:
    """A link-dense, low-prose descendant block (a 'related' / footer strip) is
    removed structurally — by shape, not by matching its wording."""
    prose = "This is the genuine main article body with plenty of real words here. " * 4
    html = (
        f"<article><p>{prose}</p>"
        '<div class="whatever">'
        + "".join(f'<a href="/b{i}">Some Book Title {i}</a>' for i in range(12))
        + "</div></article>"
    )
    out = clean_text(html)
    assert "genuine main article body" in out
    assert "Some Book Title" not in out  # the link-dense strip was pruned


def test_keeps_link_heavy_content_with_real_prose() -> None:
    """BLOCKER: a legitimately link-heavy *content* block survives because its
    non-link prose clears the floor (the dual gate needs BOTH conditions)."""
    out = clean_text(load_fixture("link_heavy_content.html"))
    # Real content with its commentary survives...
    assert "curated list of the resources" in out
    assert "CSS Tricks" in out
    assert "practical guides and real-world" in out
    assert "shaped how I write stylesheets" in out
    # ...while the link-only article-footer strip is pruned.
    assert "Edit this page" not in out
    assert "Report a problem" not in out


def test_never_prunes_the_selected_root_block() -> None:
    """A wholly link-dense block handed in as the root is NOT pruned to nothing —
    that case is the index classification's job upstream, not the cleaner's."""
    html = "<nav-root>" + "".join(f'<a href="/{i}">Link {i}</a>' for i in range(20)) + "</nav-root>"
    # Wrap in a single container so it is treated as the protected root block.
    out = clean_text(f"<section>{html}</section>")
    assert "Link 0" in out  # root text survives; nothing was emptied out
