"""Table-driven tests for ``canonicalize_url`` — the identity-critical function.

Canonicalization underpins the document ``id`` and the seen-set, so a regression
here is a regression in identity. Each row exercises one step of the ordered rule
in ``docs/crawling.md``.
"""

from __future__ import annotations

import pytest

from extractor_engine.crawl.frontier import canonicalize_url

CASES = [
    # (description, input, expected)
    ("lowercases scheme and host", "HTTP://Books.ToScrape.COM/Path", "http://books.toscrape.com/Path"),
    ("preserves path case", "https://x.com/A/B", "https://x.com/A/B"),
    ("strips leading www", "https://www.example.com/p", "https://example.com/p"),
    ("drops the fragment", "https://x.com/p#section", "https://x.com/p"),
    ("drops fragment but keeps query", "https://x.com/p?a=1#frag", "https://x.com/p?a=1"),
    ("removes utm_* params", "https://x.com/p?utm_source=news&id=5", "https://x.com/p?id=5"),
    ("removes denylisted tracking params", "https://x.com/p?ref=email&fbclid=abc&id=5", "https://x.com/p?id=5"),
    ("keeps non-tracking params", "https://x.com/p?id=1000", "https://x.com/p?id=1000"),
    ("distinguishes distinct query values", "https://x.com/p?id=1001", "https://x.com/p?id=1001"),
    ("sorts remaining params by key", "https://x.com/p?b=2&a=1", "https://x.com/p?a=1&b=2"),
    ("sorts after dropping tracking", "https://x.com/p?z=9&utm_medium=x&a=1", "https://x.com/p?a=1&z=9"),
    ("strips index.html to dir root then slash", "https://x.com/dir/index.html", "https://x.com/dir"),
    ("strips index.php", "https://x.com/a/b/index.php", "https://x.com/a/b"),
    ("strips default.htm", "https://x.com/a/default.htm", "https://x.com/a"),
    ("root index.html becomes bare root", "https://x.com/index.html", "https://x.com/"),
    ("strips trailing slash", "https://x.com/a/b/", "https://x.com/a/b"),
    ("keeps the bare root slash", "https://x.com/", "https://x.com/"),
    ("empty path becomes root", "https://x.com", "https://x.com/"),
    ("drops default https port", "https://x.com:443/p", "https://x.com/p"),
    ("drops default http port", "http://x.com:80/p", "http://x.com/p"),
    ("keeps a non-default port", "https://x.com:8443/p", "https://x.com:8443/p"),
    (
        "combined: www, fragment, tracking, sort, default-doc",
        "HTTPS://WWW.X.com/dir/index.html?utm_source=a&b=2&a=1#top",
        "https://x.com/dir?a=1&b=2",
    ),
]


@pytest.mark.parametrize("description,url,expected", CASES, ids=[c[0] for c in CASES])
def test_canonicalize_url(description: str, url: str, expected: str) -> None:
    assert canonicalize_url(url) == expected


def test_equivalent_urls_canonicalize_identically() -> None:
    """The motivating invariant: tracking/fragment/order variants collapse to one."""
    base = "https://x.com/p?id=5"
    variants = [
        "https://x.com/p?id=5#section",
        "https://x.com/p?id=5&ref=email",
        "https://www.x.com/p?id=5",
        "https://x.com/p?id=5&utm_campaign=spring",
    ]
    assert all(canonicalize_url(v) == base for v in variants)


def test_distinct_query_pages_stay_distinct() -> None:
    """A denylist (not strip-all) keeps genuinely different catalog pages apart."""
    assert canonicalize_url("https://x.com/p?id=1000") != canonicalize_url("https://x.com/p?id=1001")
