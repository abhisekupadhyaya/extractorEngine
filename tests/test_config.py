"""Tests for configuration precedence: CLI > env > default."""

from __future__ import annotations

import pytest

from extractor_engine.config import Settings


def test_built_in_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCRAPER_MAX_PAGES", raising=False)
    assert Settings(start_url="http://x").max_pages == 100


def test_env_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPER_MAX_PAGES", "55")
    assert Settings(start_url="http://x").max_pages == 55


def test_cli_override_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicitly-passed flag (init kwarg) wins over the environment."""
    monkeypatch.setenv("SCRAPER_MAX_PAGES", "55")
    assert Settings(start_url="http://x", max_pages=7).max_pages == 7


def test_postgres_dsn_is_unprefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@localhost/db")
    assert Settings(start_url="http://x").postgres_dsn == "postgresql://u:p@localhost/db"


def test_optional_backends_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    assert Settings(start_url="http://x").postgres_dsn is None
