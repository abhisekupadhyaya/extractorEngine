"""The ``scrape_site`` entry point: argument parsing, wiring, and the run.

This is the only place logging is configured. Flags are parsed with the standard
library ``argparse`` (no extra dependency); explicitly-passed flags become
keyword overrides on :class:`~extractor_engine.config.Settings`, which is what
realizes the CLI > env > default precedence. See ``docs/configuration.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__, analytics
from .config import Settings
from .crawl.crawler import Crawler
from .crawl.fetcher import SeedDisallowedError, make_fetcher
from .storage import build_store

logger = logging.getLogger("extractor_engine")


def build_parser() -> argparse.ArgumentParser:
    """Build the ``scrape_site`` argument parser.

    Every flag defaults to ``None`` so the CLI can tell "passed" from "absent"
    and forward only explicit flags as settings overrides; the defaults shown in
    help text are the authoritative ones from :class:`Settings`.
    """
    parser = argparse.ArgumentParser(
        prog="scrape_site",
        description="Crawl one public website into clean, schema-consistent JSONL documents.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--start-url", dest="start_url", help="Seed URL the crawl starts from (required).")
    parser.add_argument("--max-pages", dest="max_pages", type=int, help="Hard cap on pages fetched (default 100).")
    parser.add_argument("--max-depth", dest="max_depth", type=int, help="Maximum link depth from the seed (default 5).")
    parser.add_argument("--output", dest="output", help="Path to the JSONL output file (default output.jsonl).")
    parser.add_argument("--delay", dest="delay", type=float, help="Seconds between requests (default 0.5).")
    parser.add_argument("--include", dest="include", help="Path regex; only matching URLs are crawled.")
    parser.add_argument("--exclude", dest="exclude", help="Path regex; matching URLs are excluded.")
    parser.add_argument("--user-agent", dest="user_agent", help="User-Agent string (default scraper-bot/1.0).")
    parser.add_argument(
        "--ignore-robots",
        dest="ignore_robots",
        action="store_true",
        default=None,
        help="Bypass robots.txt (use only with authority over the site).",
    )
    parser.add_argument(
        "--render",
        dest="render",
        action="store_true",
        default=None,
        help="Render JavaScript pages with a headless browser (needs the [render] extra).",
    )
    parser.add_argument(
        "--render-timeout",
        dest="render_timeout",
        type=float,
        help="Seconds to wait for a page to render (default 30; only with --render).",
    )
    parser.add_argument(
        "--no-conditional-get",
        dest="conditional_get",
        action="store_false",
        default=None,
        help="Disable conditional GET (If-Modified-Since) on re-crawls.",
    )
    parser.add_argument(
        "--stats-json",
        dest="stats_json",
        help="Path to write the run statistics as machine-readable JSON.",
    )
    parser.add_argument("--log-level", dest="log_level", help="Logging verbosity (default INFO).")
    return parser


def _configure_logging(level: str) -> None:
    """Configure root logging once, for the whole process."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run a crawl from CLI arguments; return a process exit code."""
    args = build_parser().parse_args(argv)
    overrides = {key: value for key, value in vars(args).items() if value is not None}
    settings = Settings(**overrides)

    _configure_logging(settings.log_level)

    if not settings.start_url:
        logger.error("--start-url is required (or set SCRAPER_START_URL)")
        return 2

    logger.info(
        "starting crawl of %s (max_pages=%d max_depth=%d)",
        settings.start_url,
        settings.max_pages,
        settings.max_depth,
    )
    store = build_store(settings)
    fetcher = make_fetcher(settings)

    try:
        with fetcher:
            stats = Crawler(settings, fetcher, store).run()
    except SeedDisallowedError as exc:
        logger.error("%s", exc)
        return 1

    if settings.stats_json:
        _write_json_atomic(settings.stats_json, stats.to_dict())
        logger.info("wrote run statistics to %s", settings.stats_json)
    _report(settings.output, stats.new_records)
    return 0


def _write_json_atomic(path: str, data: object) -> None:
    """Write ``data`` as JSON to ``path`` atomically (temp file then replace).

    Mirrors the output write in docs/storage-and-idempotency.md, so a crash
    mid-write never leaves a half-written stats file. See docs/observability.md.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, target)


def _report(output: str, new_records: int) -> None:
    """Log a corpus summary off the freshly written output (doubles as QA)."""
    summary = analytics.summarize_file(output)
    logger.info(
        "corpus: %d document(s), %d new this run, avg %.0f words",
        summary["document_count"],
        new_records,
        summary["avg_word_count"],
    )
    if summary["document_count"]:
        logger.info("  languages: %s", _format_dist(summary["language_distribution"]))
        logger.info("  content types: %s", _format_dist(summary["content_type_distribution"]))
        logger.info("  extraction layers: %s", _format_dist(summary["extraction_layer_distribution"]))


def _format_dist(distribution: object) -> str:
    if not isinstance(distribution, dict):
        return ""
    return ", ".join(f"{key}={count}" for key, count in sorted(distribution.items()))


if __name__ == "__main__":
    sys.exit(main())
