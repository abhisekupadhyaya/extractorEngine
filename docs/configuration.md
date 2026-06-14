# Configuration

This document lists the knobs that control a run: the CLI flags and the
environment variables, their defaults, and the precedence between them.
Configuration is centralized in a single settings object, so the same values are
available to the CLI and to the library code without hardcoded hosts or ports.

## Precedence

Configuration is resolved in this order, highest first:

```
CLI flag   >   environment variable   >   built-in default
```

A flag passed on the command line always wins. If a value is not given on the
command line, the corresponding environment variable is used. If neither is set,
the built-in default applies. Environment variables use the prefix `SCRAPER_` and an
`.env` file is supported.

## CLI flags

The entry point is `scrape_site`. A representative invocation:

```
scrape_site --start-url=https://books.toscrape.com/ \
            --max-pages=100 --max-depth=5 \
            --output=output.jsonl \
            --delay=0.5 --include=/catalogue/ \
            --user-agent="scraper-bot/1.0"
```

| Flag | Default | Meaning |
|---|---|---|
| `--start-url` | (required) | Seed URL the crawl starts from. |
| `--max-pages` | `100` | Hard cap on pages fetched (circuit breaker). |
| `--max-depth` | `5` | Maximum link depth from the seed (relevance bound). |
| `--output` | `output.jsonl` | Path to the JSONL output file. |
| `--delay` | `0.5` | Seconds to wait between requests (politeness). |
| `--include` | (none) | Path regex; only URLs whose path matches are crawled. |
| `--exclude` | (none) | Path regex; URLs whose path matches are excluded. |
| `--user-agent` | `scraper-bot/1.0` | User-Agent string sent with every request. |
| `--ignore-robots` | `false` | Escape hatch to bypass `robots.txt` (use only with authority over the site). |
| `--render` | `false` | Use the headless-browser rendering fetcher instead of the static HTTP fetcher, for client-rendered sites. Off by default. |
| `--render-timeout` | `30` | Seconds to wait for the page to render before giving up (only with `--render`). |
| `--no-conditional-get` | `false` | Disable conditional GET (`If-Modified-Since`) on re-crawls. Conditional GET is on by default. |
| `--stats-json` | (none) | Path to write the run statistics as machine-readable JSON, in addition to the printed summary. |
| `--log-level` | `INFO` | Logging verbosity. Skips are logged at `WARNING`. |

Exact flag names and any additional thresholds are parsed by the CLI's argument
parser (standard library, zero extra dependencies). Defaults shown here are the
intended values; the settings object is the authority at runtime.

## Environment variables

Environment variables mirror the flags (prefixed `SCRAPER_`) and additionally
configure the optional storage backends. The optional backends activate **only**
when their variables are present; absent them, the pipeline runs as pure JSONL
with no external services.

| Variable | Default | Meaning |
|---|---|---|
| `SCRAPER_MAX_PAGES` | `100` | Env-level default for `--max-pages`. |
| `SCRAPER_MAX_DEPTH` | `5` | Env-level default for `--max-depth`. |
| `SCRAPER_DELAY` | `0.5` | Env-level default for `--delay`. |
| `SCRAPER_USER_AGENT` | `scraper-bot/1.0` | Env-level default for `--user-agent`. |
| `POSTGRES_DSN` | (unset) | When set, enables the Postgres state backend (UPSERT on `id`). |
| `MINIO_*` | (unset) | When set, enables object storage of raw HTML for provenance. |

Thresholds used by extraction and enrichment (the minimum word count, the
link-density cutoff, the code ratio, the request timeout, and the optional
max-page-bytes cap) also have defaults on the settings object and can be
overridden through configuration; see [extraction.md](extraction.md),
[enrichment.md](enrichment.md), and [crawling.md](crawling.md) for what each one
controls.

## Notes

- **No hardcoded hosts or ports.** Storage endpoints are supplied entirely
  through environment variables.
- **An `.env` file is supported** for local development; values in it follow the
  same precedence (below CLI flags, above built-in defaults).
- **Rendering is an optional extra.** `--render` requires the headless-browser
  dependency, shipped as an optional install extra and a separate container image;
  the default static path needs none of it. See [crawling.md](crawling.md).
- **`--stats-json`** writes the same statistics shown in the printed run summary;
  see [observability.md](observability.md).
- See [storage-and-idempotency.md](storage-and-idempotency.md) for how the
  optional backends behave once enabled.
