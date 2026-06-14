"""Integration test for across-run idempotency and the JSONL store.

Runs the full crawler twice over the same mocked site and asserts the second run
produces zero new records — the concrete proof of the insert/skip/update model in
``docs/storage-and-idempotency.md``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import httpx
import respx

from extractor_engine.config import Settings
from extractor_engine.crawl.crawler import Crawler
from extractor_engine.crawl.fetcher import Fetcher
from extractor_engine.engine.models import Document, Signals
from extractor_engine.storage.base import StoreAction
from extractor_engine.storage.jsonl import JSONLStore

BASE = "https://mini.test"
PROSE = "word " * 60  # comfortably over the 25-word keep floor


def _mock_site(article_b_body: str = PROSE) -> None:
    respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(404))
    home = '<html><body><nav><a href="/a">A</a><a href="/b">B</a></nav></body></html>'
    respx.get(f"{BASE}/").mock(return_value=httpx.Response(200, html=home))
    page_a = f"<html><body><article><h1>Alpha</h1><p>{PROSE}</p></article></body></html>"
    page_b = f"<html><body><article><h1>Beta</h1><p>{article_b_body}</p></article></body></html>"
    respx.get(f"{BASE}/a").mock(return_value=httpx.Response(200, html=page_a))
    respx.get(f"{BASE}/b").mock(return_value=httpx.Response(200, html=page_b))


def _run(output: Path):
    settings = Settings(start_url=f"{BASE}/", output=str(output), max_pages=10, delay=0.0)
    fetcher = Fetcher(delay=0.0, sleep=Mock())
    crawler = Crawler(settings, fetcher, JSONLStore(output), now=lambda: "2026-06-14T12:00:00Z")
    with fetcher:
        return crawler.run()


@respx.mock
def test_second_run_produces_zero_new_records(tmp_path: Path) -> None:
    _mock_site()
    output = tmp_path / "out.jsonl"

    first = _run(output)
    assert first.kept >= 2
    assert first.new_records == first.kept
    line_count = len(output.read_text().splitlines())

    second = _run(output)
    assert second.new_records == 0
    assert second.actions[StoreAction.SKIP] == first.kept
    assert len(output.read_text().splitlines()) == line_count  # corpus unchanged


@respx.mock
def test_changed_page_updates_not_duplicates(tmp_path: Path) -> None:
    _mock_site()
    output = tmp_path / "out.jsonl"
    _run(output)
    line_count = len(output.read_text().splitlines())

    # Re-mock with page B's body changed; its id is stable, its hash differs.
    respx.clear()
    _mock_site(article_b_body="completely different prose " * 20)
    second = _run(output)

    assert second.actions[StoreAction.UPDATE] == 1
    assert second.actions[StoreAction.INSERT] == 0
    assert len(output.read_text().splitlines()) == line_count  # replaced, not appended


# --------------------------------------------------------------------------- #
# JSONLStore unit-level three-case decision
# --------------------------------------------------------------------------- #
def _doc(doc_id: str, body: str) -> Document:
    import hashlib

    return Document(
        id=doc_id,
        url=f"https://x.com/{doc_id}",
        title="t",
        body_text=body,
        fetched_at="2026-06-14T12:00:00Z",
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        signals=Signals(
            word_count=30, char_count=len(body), language="en",
            content_type="article", is_mostly_code=False,
        ),
    )


def test_store_insert_skip_update(tmp_path: Path) -> None:
    output = tmp_path / "s.jsonl"

    store = JSONLStore(output)
    assert store.handle(_doc("x", "original body")) == StoreAction.INSERT
    store.finalize()

    # Re-seed from file: same content -> skip, changed content -> update.
    store2 = JSONLStore(output)
    assert store2.handle(_doc("x", "original body")) == StoreAction.SKIP
    assert store2.handle(_doc("x", "edited body")) == StoreAction.UPDATE
    store2.finalize()

    assert len(output.read_text().splitlines()) == 1  # still one record for id "x"
