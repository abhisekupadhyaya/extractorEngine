"""The pure extraction engine: HTML + URL in, an enriched document object out.

Nothing in this subpackage performs I/O — no network, no disk, no clock. That
purity is what lets the quality-critical code be tested exhaustively on static
fixtures (see ``docs/architecture.md`` and ``docs/testing.md``).
"""
