"""Centralized configuration: one settings object, resolved CLI > env > default.

Environment variables use the ``SCRAPER_`` prefix and an ``.env`` file is
supported. The optional Postgres state backend (``POSTGRES_DSN``) is read
without the prefix and stays unset by default, so the pipeline runs as pure
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
    max_pages: int = Field(
        default=100, description="Hard cap on pages handled per run, fetched or revalidated (304)."
    )
    max_depth: int = Field(default=5, description="Maximum link depth from the seed.")
    output: str = Field(default="output.jsonl", description="Path to the JSONL output file.")
    delay: float = Field(default=0.5, description="Seconds to wait between requests.")
    include: str | None = Field(default=None, description="Path regex: only matching URLs are crawled.")
    exclude: str | None = Field(default=None, description="Path regex: matching URLs are excluded.")
    user_agent: str = Field(default="scraper-bot/1.0", description="User-Agent sent with every request.")
    ignore_robots: bool = Field(default=False, description="Bypass robots.txt (use with authority only).")
    stats_json: str | None = Field(
        default=None, description="Path to write run statistics as JSON, alongside the printed summary."
    )
    log_level: str = Field(default="INFO", description="Logging verbosity; skips log at WARNING.")

    # --- Rendering (opt-in; needs the [render] extra) ------------------------- #
    render: bool = Field(default=False, description="Use the headless-browser rendering fetcher.")
    render_timeout: float = Field(default=30.0, description="Seconds to wait for a page to render.")

    # --- Conditional GET ------------------------------------------------------ #
    conditional_get: bool = Field(
        default=True, description="Send If-Modified-Since on re-crawls (304 -> skip)."
    )

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
    # Structural pruning of link-dense furniture inside the selected block (a
    # descendant block is pruned only if it clears BOTH gates). Conservative
    # defaults that favor keeping content; see docs/extraction.md.
    prune_link_density: float = Field(
        default=0.5, description="Prune a descendant block whose link density is at least this."
    )
    prune_min_prose_words: int = Field(
        default=20, description="...and whose non-link prose is below this word count."
    )

    # --- Optional state backends (unprefixed env vars) ------------------------ #
    postgres_dsn: str | None = Field(
        default=None,
        validation_alias=AliasChoices("POSTGRES_DSN", "SCRAPER_POSTGRES_DSN"),
        description="When set, enables the Postgres state backend.",
    )
