"""The rendering fetcher: load a URL by driving a headless browser.

For sites whose content is injected by client-side JavaScript, a plain HTTP GET
returns an empty shell. The rendering fetcher navigates the page in a headless
browser and returns the **rendered DOM** as HTML, which the pure engine then
extracts from exactly as it would static HTML — the engine never knows which
fetcher loaded the page (see ``docs/crawling.md``).

The browser dependency (Playwright) is imported lazily and ships as the optional
``[render]`` install extra, so the default static path needs none of it. Selected
with ``--render``; bounded by ``--render-timeout``.
"""

from __future__ import annotations

import logging
from typing import Any

from .fetcher import (
    BaseFetcher,
    RawResponse,
    _FetchConnectionError,
    _FetchTimeoutError,
)

logger = logging.getLogger("extractor_engine.fetcher")


class RenderingFetcher(BaseFetcher):
    """A :class:`~extractor_engine.crawl.fetcher.BaseFetcher` that renders pages.

    Only :meth:`_load` differs from the static fetcher: it drives a headless
    Chromium via Playwright and returns the rendered DOM. All politeness, robots,
    retry, and reason-mapping behavior is inherited unchanged.
    """

    def __init__(self, *, render_timeout: float = 30.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.render_timeout = render_timeout
        self._playwright: Any = None
        self._browser: Any = None

    def _ensure_browser(self) -> None:
        """Start Playwright and launch headless Chromium on first use."""
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "--render requires the rendering extra; install with: pip install -e '.[render]' "
                "and then: playwright install --with-deps chromium"
            ) from exc
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        logger.info("rendering fetcher: launched headless browser")

    def _load(self, url: str, *, if_modified_since: str | None = None) -> RawResponse:
        """Navigate to ``url`` in a headless browser; return the rendered DOM.

        A render timeout maps to a ``timeout`` skip and any other navigation
        failure to a ``connection_error`` skip, via the base class.
        """
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        self._ensure_browser()
        page = self._browser.new_page(user_agent=self.user_agent)
        if if_modified_since:
            page.set_extra_http_headers({"If-Modified-Since": if_modified_since})
        try:
            response = page.goto(
                url, timeout=self.render_timeout * 1000, wait_until="networkidle"
            )
            html = page.content()
            final_url = page.url
            status = response.status if response is not None else 200
            raw_headers = response.headers if response is not None else {}
            headers = {key.lower(): value for key, value in raw_headers.items()}
            return RawResponse(status, final_url, headers, html)
        except PlaywrightTimeout as exc:
            raise _FetchTimeoutError(str(exc)) from exc
        except PlaywrightError as exc:
            raise _FetchConnectionError(str(exc)) from exc
        finally:
            page.close()

    def close(self) -> None:
        """Close the browser and Playwright, then the shared HTTP client."""
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        super().close()
