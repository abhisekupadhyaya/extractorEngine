"""Shared test configuration and fixtures.

Language detection is seeded here (``DetectorFactory.seed = 0``) so golden-file
and signal tests produce identical results on every machine and run — the
determinism requirement from ``docs/testing.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langdetect import DetectorFactory

# Seed the language detector for deterministic results across machines/runs.
DetectorFactory.seed = 0

FIXTURES = Path(__file__).parent / "fixtures"

# Fixed values used by golden tests so output is fully deterministic.
GOLDEN_FETCHED_AT = "2026-06-14T12:00:00Z"
GOLDEN_LAST_MODIFIED = "Wed, 08 Feb 2023 21:02:32 GMT"


def load_fixture(name: str) -> str:
    """Read a fixture file's text by name."""
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to the test fixtures directory."""
    return FIXTURES
