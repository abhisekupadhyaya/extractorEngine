"""extractor-engine: a small, production-minded single-site scraping pipeline.

The package is split along one boundary: a *pure* extraction engine
(:mod:`extractor_engine.engine`) with no I/O, wrapped by *dirty* orchestration
(:mod:`extractor_engine.crawl`, :mod:`extractor_engine.storage`, and the CLI).
See ``docs/architecture.md`` for the rationale.
"""

__version__ = "1.0.0"
