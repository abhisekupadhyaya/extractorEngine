"""Centralized configuration: one settings object, resolved CLI > env > default.

Environment variables use the ``SCRAPER_`` prefix and an ``.env`` file is
supported. The optional storage backends (``POSTGRES_DSN``, ``MINIO_*``) are
read without the prefix and stay unset by default, so the pipeline runs as pure
JSONL with zero external services. CLI flags win because the CLI constructs this
object with explicitly-passed flags as keyword overrides — and init values
outrank environment values in pydantic-settings. See ``docs/configuration.md``.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime knobs for a crawl, with their built-in defaults."""

    model_config = SettingsConfigDict(
        env_prefix="SCRAPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Crawl bounds and politeness (mirror the CLI flags) ------------------- #
    start_url: str | None = Field(default=None, description="Seed URL the crawl starts from.")
    max_pages: int = Field(default=100, description="Hard cap on pages fetched (circuit breaker).")
    max_depth: int = Field(default=5, description="Maximum link depth from the seed.")
    output: str = Field(default="output.jsonl", description="Path to the JSONL output file.")
    delay: float = Field(default=0.5, description="Seconds to wait between requests.")
    include: str | None = Field(default=None, description="Path regex: only matching URLs are crawled.")
    exclude: str | None = Field(default=None, description="Path regex: matching URLs are excluded.")
    user_agent: str = Field(default="scraper-bot/1.0", description="User-Agent sent with every request.")
    ignore_robots: bool = Field(default=False, description="Bypass robots.txt (use with authority only).")
    log_level: str = Field(default="INFO", description="Logging verbosity; skips log at WARNING.")

    # --- HTTP knobs ----------------------------------------------------------- #
    timeout: float = Field(default=10.0, description="Per-request HTTP timeout, seconds.")
    max_retries: int = Field(default=2, description="Retry attempts for transient failures.")
    max_page_bytes: int = Field(
        default=5 * 1024 * 1024, description="Response-size cap in bytes; 0 disables."
    )

    # --- Extraction / enrichment thresholds ----------------------------------- #
    min_word_count: int = Field(default=25, description="Quality-gate floor and extractor length floor.")
    link_density_threshold: float = Field(
        default=0.4, description="Over-extraction / index-detection link-density cutoff."
    )
    code_ratio_threshold: float = Field(default=0.5, description="is_mostly_code cutoff.")

    # --- Optional state backends (unprefixed env vars) ------------------------ #
    postgres_dsn: str | None = Field(
        default=None,
        validation_alias=AliasChoices("POSTGRES_DSN", "SCRAPER_POSTGRES_DSN"),
        description="When set, enables the Postgres state backend.",
    )
