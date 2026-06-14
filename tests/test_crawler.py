"""Telemetry tests for the crawler: layer / drop / fetch-outcome tallies.

Drives the full crawler against a mocked mini-site and asserts the run-state
statistics described in ``docs/observability.md``: extraction-layer counts over
every processed page, the drop-reason histogram, and fetch outcomes split into
errors and intentional skips.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import httpx
import respx

from extractor_engine.config import Settings
from extractor_engine.crawl.crawler import Crawler, _to_http_date
from extractor_engine.crawl.fetcher import Fetcher, FetchResult
from extractor_engine.crawl.frontier import canonicalize_url
from extractor_engine.storage.jsonl import JSONLStore

BASE = "https://site.test"
PROSE = "word " * 60  # comfortably over the 25-word keep floor

# A hub that is link-dense (an index, dropped) and links to every other page.
HUB = (
    "<html><body><nav>"
    + "".join(f'<a href="/{p}">{p}</a>' for p in ("good", "short", "list", "missing", "feed"))
    + "</nav></body></html>"
)
GOOD = f"<html><body><article><h1>Good</h1><p>{PROSE}</p></article></body></html>"
SHORT = "<html><body><article><h1>Tiny</h1><p>only a few words here</p></article></body></html>"
LIST = (
    "<html><body><section>"
    + "".join(f'<a href="/x{i}">item {i}</a>' for i in range(40))
    + "</section></body></html>"
)


def _mock_site() -> None:
    respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, html=HUB))
    respx.get(f"{BASE}/good").mock(return_value=httpx.Response(200, html=GOOD))
    respx.get(f"{BASE}/short").mock(return_value=httpx.Response(200, html=SHORT))
    respx.get(f"{BASE}/list").mock(return_value=httpx.Response(200, html=LIST))
    respx.get(f"{BASE}/missing").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/feed").mock(
        return_value=httpx.Response(200, json={"a": 1}, headers={"content-type": "application/json"})
    )


@respx.mock
def test_run_statistics_buckets(tmp_path: Path) -> None:
    _mock_site()
    output = tmp_path / "out.jsonl"
    # max_depth=1 keeps the crawl to the hub and its direct links (the listing
    # page's own item links sit at depth 2 and are never enqueued).
    settings = Settings(start_url=f"{BASE}/", output=str(output), max_pages=10, max_depth=1, delay=0.0)
    fetcher = Fetcher(delay=0.0, sleep=Mock())
    with fetcher:
        stats = Crawler(settings, fetcher, JSONLStore(output), now=lambda: "2026-06-14T12:00:00Z").run()

    # Hub + good + short + list are fetched; missing (404) and feed (non-HTML) are skips.
    assert stats.pages_fetched == 4
    assert stats.kept == 1  # only /good clears the gate
    assert stats.dropped == 3  # hub (index), list (index), short (too_short)
    assert stats.drops == {"index": 2, "too_short": 1}
    assert stats.fetch_outcomes == {"http_4xx": 1, "non_html": 1}
    # Every processed page contributed exactly one extraction-layer tally.
    assert sum(stats.layers.values()) == 4

    # Errors and intentional skips are partitioned for the report / stats file.
    report = stats.to_dict()
    assert report["fetch_outcomes"] == {
        "errors": {"http_4xx": 1},
        "intentional_skips": {"non_html": 1},
    }
    assert report["drop_reasons"] == {"index": 2, "too_short": 1}


# --------------------------------------------------------------------------- #
# Conditional GET (If-Modified-Since -> 304 -> not_modified skip)
# --------------------------------------------------------------------------- #
ARTICLE = f"<html><body><article><h1>A</h1><p>{PROSE}</p></article></body></html>"


@respx.mock
def test_conditional_get_skips_unchanged_on_recrawl(tmp_path: Path) -> None:
    respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(404))

    def handler(request: httpx.Request) -> httpx.Response:
        # On a re-crawl the crawler sends If-Modified-Since; reply 304 (unchanged).
        if request.headers.get("if-modified-since"):
            return httpx.Response(304)
        return httpx.Response(
            200, html=ARTICLE, headers={"Last-Modified": "Wed, 08 Feb 2023 21:02:32 GMT"}
        )

    respx.get(f"{BASE}/a").mock(side_effect=handler)
    output = tmp_path / "out.jsonl"

    def run():
        settings = Settings(start_url=f"{BASE}/a", output=str(output), max_pages=5, delay=0.0)
        fetcher = Fetcher(delay=0.0, sleep=Mock())
        with fetcher:
            return Crawler(settings, fetcher, JSONLStore(output), now=lambda: "2026-06-14T12:00:00Z").run()

    first = run()
    assert first.kept == 1 and first.new_records == 1

    second = run()
    assert second.fetch_outcomes.get("not_modified") == 1  # the 304 was a typed skip
    assert second.pages_fetched == 0  # body never transferred — not a fetched page
    assert second.new_records == 0  # corpus unchanged


def test_to_http_date_roundtrip() -> None:
    assert _to_http_date("2023-02-08T21:02:32Z") == "Wed, 08 Feb 2023 21:02:32 GMT"
    assert _to_http_date("not-a-date") is None


# --------------------------------------------------------------------------- #
# max_pages is a circuit breaker on fetch ATTEMPTS, not just kept pages
# --------------------------------------------------------------------------- #
DEAD_HUB = (
    "<html><body><nav>"
    + "".join(f'<a href="/p{i}">p{i}</a>' for i in range(10))
    + "</nav></body></html>"
)


@respx.mock
def test_max_pages_caps_fetch_attempts(tmp_path: Path) -> None:
    respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, html=DEAD_HUB))
    for i in range(10):
        respx.get(f"{BASE}/p{i}").mock(return_value=httpx.Response(404))

    output = tmp_path / "out.jsonl"
    settings = Settings(start_url=f"{BASE}/", output=str(output), max_pages=4, max_depth=1, delay=0.0)
    fetcher = Fetcher(delay=0.0, sleep=Mock())
    with fetcher:
        stats = Crawler(settings, fetcher, JSONLStore(output), now=lambda: "2026-06-14T12:00:00Z").run()

    # Every attempt — the hub plus the 404 skips — consumes a budget slot, so the
    # run stops after max_pages requests rather than attempting all 10 dead links.
    assert stats.pages_fetched + sum(stats.fetch_outcomes.values()) == settings.max_pages == 4
    assert stats.fetch_outcomes["http_4xx"] == 3  # 4 attempts (hub + 3); the other 7 never requested


# --------------------------------------------------------------------------- #
# A redirect target already queued is not fetched a second time
# --------------------------------------------------------------------------- #
class _ScriptedFetcher:
    """A minimal fetcher returning canned results and recording fetch() calls."""

    def __init__(self, pages: dict[str, FetchResult]) -> None:
        self._pages = pages
        self.calls: list[str] = []

    def is_allowed(self, url: str) -> bool:
        return True

    def fetch(self, url: str, *, if_modified_since: str | None = None) -> FetchResult:
        self.calls.append(url)
        return self._pages[url]


def test_redirect_target_not_fetched_twice(tmp_path: Path) -> None:
    hub = canonicalize_url(f"{BASE}/hub")
    a = canonicalize_url(f"{BASE}/a")
    b = canonicalize_url(f"{BASE}/b")
    hub_html = '<html><body><nav><a href="/a">a</a><a href="/b">b</a></nav></body></html>'
    b_html = f"<html><body><article><h1>B</h1><p>{PROSE}</p></article></body></html>"

    fetcher = _ScriptedFetcher(
        {
            hub: FetchResult(url=hub, html=hub_html, last_modified=None),
            a: FetchResult(url=b, html=b_html, last_modified=None),  # /a redirects to /b
            b: FetchResult(url=b, html=b_html, last_modified=None),
        }
    )
    output = tmp_path / "out.jsonl"
    settings = Settings(start_url=f"{BASE}/hub", output=str(output), max_pages=10, max_depth=1, delay=0.0)
    stats = Crawler(settings, fetcher, JSONLStore(output), now=lambda: "2026-06-14T12:00:00Z").run()  # type: ignore[arg-type]

    # /a (redirecting to /b) is fetched; /b's own stale queue entry is discarded
    # because /b was already handled via the redirect — it is never fetched directly.
    assert fetcher.calls == [hub, a]
    assert b not in fetcher.calls
    assert stats.kept == 1 and stats.new_records == 1  # /b kept once; hub is an index, dropped
