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

from extractor_engine.config import Settings
from extractor_engine.crawl.fetcher import (
    BaseFetcher,
    Fetcher,
    FetchResult,
    FetchSkip,
    FetchSkipReason,
    RawResponse,
)
from extractor_engine.crawl.fetcher import make_fetcher as build_fetcher
from extractor_engine.crawl.rendering_fetcher import RenderingFetcher

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
    result = fetcher.fetch(f"{BASE}/down")
    assert result == FetchSkip(FetchSkipReason.HTTP_5XX)
    assert route.call_count == 3  # 1 initial + 2 retries


@respx.mock
def test_no_retry_on_404() -> None:
    _allow_robots()
    route = respx.get(f"{BASE}/missing").mock(return_value=httpx.Response(404))
    fetcher, _ = make_fetcher(max_retries=2)
    result = fetcher.fetch(f"{BASE}/missing")
    assert result == FetchSkip(FetchSkipReason.HTTP_4XX)
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
    result = fetcher.fetch(f"{BASE}/private/secret")
    assert result == FetchSkip(FetchSkipReason.ROBOTS_DISALLOWED)
    assert not content.called  # disallowed: never fetched


@respx.mock
def test_skips_non_html_content_type() -> None:
    _allow_robots()
    respx.get(f"{BASE}/data.json").mock(
        return_value=httpx.Response(200, json={"a": 1}, headers={"content-type": "application/json"})
    )
    fetcher, _ = make_fetcher()
    result = fetcher.fetch(f"{BASE}/data.json")
    assert result == FetchSkip(FetchSkipReason.NON_HTML)


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
def test_rate_limited_exhausts_to_typed_skip() -> None:
    _allow_robots()
    respx.get(f"{BASE}/busy").mock(return_value=httpx.Response(429, headers={"Retry-After": "0"}))
    fetcher, _ = make_fetcher(max_retries=1)
    assert fetcher.fetch(f"{BASE}/busy") == FetchSkip(FetchSkipReason.RATE_LIMITED)


@respx.mock
def test_timeout_exhausts_to_typed_skip() -> None:
    _allow_robots()
    respx.get(f"{BASE}/slow").mock(side_effect=httpx.TimeoutException("too slow"))
    fetcher, _ = make_fetcher(max_retries=1)
    assert fetcher.fetch(f"{BASE}/slow") == FetchSkip(FetchSkipReason.TIMEOUT)


@respx.mock
def test_oversized_response_is_typed_skip() -> None:
    _allow_robots()
    respx.get(f"{BASE}/big").mock(return_value=httpx.Response(200, html="<html>" + "x" * 5000 + "</html>"))
    fetcher, _ = make_fetcher(max_page_bytes=100)
    assert fetcher.fetch(f"{BASE}/big") == FetchSkip(FetchSkipReason.OVERSIZED)


@respx.mock
def test_conditional_get_304_is_not_modified_skip() -> None:
    """A conditional GET that returns 304 resolves to a not_modified skip, and the
    If-Modified-Since header is actually sent."""
    _allow_robots()
    route = respx.get(f"{BASE}/page").mock(return_value=httpx.Response(304))
    fetcher, _ = make_fetcher()
    result = fetcher.fetch(f"{BASE}/page", if_modified_since="Wed, 08 Feb 2023 21:02:32 GMT")
    assert result == FetchSkip(FetchSkipReason.NOT_MODIFIED)
    sent = route.calls.last.request.headers.get("if-modified-since")
    assert sent == "Wed, 08 Feb 2023 21:02:32 GMT"


def test_reason_error_vs_skip_classification() -> None:
    """Errors and intentional skips are partitioned for telemetry."""
    assert FetchSkipReason.HTTP_5XX.is_error and FetchSkipReason.TIMEOUT.is_error
    assert not FetchSkipReason.ROBOTS_DISALLOWED.is_error
    assert not FetchSkipReason.NOT_MODIFIED.is_error


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


# --------------------------------------------------------------------------- #
# Fetcher mode selection and the shared base contract (no browser needed)
# --------------------------------------------------------------------------- #
def test_make_fetcher_selects_static_by_default() -> None:
    fetcher = build_fetcher(Settings(start_url="https://x.test/", render=False))
    assert isinstance(fetcher, Fetcher)
    fetcher.close()


def test_make_fetcher_selects_rendering_when_render_set() -> None:
    fetcher = build_fetcher(Settings(start_url="https://x.test/", render=True))
    assert isinstance(fetcher, RenderingFetcher)
    fetcher.close()  # no browser was launched, so this only closes the HTTP client


class _StubFetcher(BaseFetcher):
    """A BaseFetcher whose _load returns a canned RawResponse (no network)."""

    def __init__(self, raw: RawResponse, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._raw = raw

    def _load(self, url: str, *, if_modified_since: str | None = None) -> RawResponse:
        return self._raw


def test_base_interpret_builds_result_from_raw() -> None:
    raw = RawResponse(200, "https://x.test/p", {"content-type": "text/html"}, "<p>hi</p>")
    fetcher = _StubFetcher(raw, ignore_robots=True, delay=0.0, sleep=Mock())
    result = fetcher.fetch("https://x.test/p")
    assert isinstance(result, FetchResult)
    assert result.html == "<p>hi</p>" and result.url == "https://x.test/p"
    fetcher.close()


def test_base_interpret_304_is_not_modified_skip() -> None:
    raw = RawResponse(304, "https://x.test/p", {}, "")
    fetcher = _StubFetcher(raw, ignore_robots=True, delay=0.0, sleep=Mock())
    assert fetcher.fetch("https://x.test/p") == FetchSkip(FetchSkipReason.NOT_MODIFIED)
    fetcher.close()
