"""Mocked-network tests for the fetcher — the single dirty layer.

Exercises the error-handling taxonomy from ``docs/crawling.md`` against simulated
responses: retry on 5xx, no retry on 404, honor ``Retry-After`` on 429, and
respect ``robots.txt``. The network is mocked with respx and sleeps are stubbed,
so the suite is fast and deterministic.
"""

from __future__ import annotations

from unittest.mock import Mock

import httpx
import respx

from extractor_engine.crawl.fetcher import Fetcher

BASE = "https://site.test"
HTML = '<html><body><p>content</p></body></html>'


def make_fetcher(**kwargs: object) -> tuple[Fetcher, Mock]:
    """A fetcher with a no-op sleep (captured) and zero throttle delay."""
    sleep = Mock()
    fetcher = Fetcher(delay=0.0, sleep=sleep, **kwargs)  # type: ignore[arg-type]
    return fetcher, sleep


def _allow_robots() -> None:
    """Mock robots.txt to allow everything (404 → no rules)."""
    respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(404))


@respx.mock
def test_retry_on_5xx_then_succeeds() -> None:
    _allow_robots()
    route = respx.get(f"{BASE}/page").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, html=HTML),
        ]
    )
    fetcher, sleep = make_fetcher(max_retries=2)
    result = fetcher.fetch(f"{BASE}/page")
    assert result is not None and "content" in result.html
    assert route.call_count == 3
    assert sleep.called  # backed off between attempts


@respx.mock
def test_5xx_exhausts_retries_then_skips() -> None:
    _allow_robots()
    route = respx.get(f"{BASE}/down").mock(return_value=httpx.Response(503))
    fetcher, _ = make_fetcher(max_retries=2)
    assert fetcher.fetch(f"{BASE}/down") is None
    assert route.call_count == 3  # 1 initial + 2 retries


@respx.mock
def test_no_retry_on_404() -> None:
    _allow_robots()
    route = respx.get(f"{BASE}/missing").mock(return_value=httpx.Response(404))
    fetcher, _ = make_fetcher(max_retries=2)
    assert fetcher.fetch(f"{BASE}/missing") is None
    assert route.call_count == 1  # 4xx is terminal — no retry


@respx.mock
def test_honor_retry_after_on_429() -> None:
    _allow_robots()
    route = respx.get(f"{BASE}/throttled").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, html=HTML),
        ]
    )
    fetcher, sleep = make_fetcher(max_retries=2)
    result = fetcher.fetch(f"{BASE}/throttled")
    assert result is not None
    assert route.call_count == 2
    assert any(call.args and call.args[0] == 7.0 for call in sleep.call_args_list)


@respx.mock
def test_respects_robots_disallow() -> None:
    respx.get(f"{BASE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private")
    )
    content = respx.get(f"{BASE}/private/secret").mock(return_value=httpx.Response(200, html=HTML))
    fetcher, _ = make_fetcher()
    assert fetcher.fetch(f"{BASE}/private/secret") is None
    assert not content.called  # disallowed: never fetched


@respx.mock
def test_skips_non_html_content_type() -> None:
    _allow_robots()
    respx.get(f"{BASE}/data.json").mock(
        return_value=httpx.Response(200, json={"a": 1}, headers={"content-type": "application/json"})
    )
    fetcher, _ = make_fetcher()
    assert fetcher.fetch(f"{BASE}/data.json") is None


@respx.mock
def test_returns_final_url_after_redirect() -> None:
    """A redirect must yield the FINAL url, not the requested one — it is the
    basis for the document id and relative-link resolution."""
    _allow_robots()
    respx.get(f"{BASE}/old").mock(
        return_value=httpx.Response(301, headers={"Location": f"{BASE}/new/"})
    )
    respx.get(f"{BASE}/new/").mock(return_value=httpx.Response(200, html=HTML))
    fetcher, _ = make_fetcher()
    result = fetcher.fetch(f"{BASE}/old")
    assert result is not None
    assert result.url == f"{BASE}/new/"  # final, not the requested /old


@respx.mock
def test_successful_fetch_returns_last_modified() -> None:
    _allow_robots()
    respx.get(f"{BASE}/page").mock(
        return_value=httpx.Response(200, html=HTML, headers={"Last-Modified": "Wed, 08 Feb 2023 21:02:32 GMT"})
    )
    fetcher, _ = make_fetcher()
    result = fetcher.fetch(f"{BASE}/page")
    assert result is not None
    assert result.last_modified == "Wed, 08 Feb 2023 21:02:32 GMT"
