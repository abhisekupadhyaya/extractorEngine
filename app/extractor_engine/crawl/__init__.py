"""Dirty orchestration: URL discovery and byte-fetching.

This subpackage is the only network layer. It hands raw HTML to the pure engine
and owns crawl state (the frontier, the seen-set) and politeness (robots.txt,
throttling, retries). See ``docs/crawling.md``.
"""
