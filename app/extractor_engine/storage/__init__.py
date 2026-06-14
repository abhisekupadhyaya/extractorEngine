"""Persistence: the JSONL deliverable plus an optional external state store.

The deliverable is always the JSONL file; optional backends (Postgres) activate
only when their environment variables are set and serve as resumable crawl
state, never as the deliverable. See ``docs/storage-and-idempotency.md``.
"""

from __future__ import annotations

import logging

from ..config import Settings
from .base import Store, StoreAction
from .jsonl import JSONLStore

logger = logging.getLogger("extractor_engine.storage")

__all__ = ["Store", "StoreAction", "JSONLStore", "build_store"]


def build_store(settings: Settings) -> Store:
    """Select the storage backend from settings.

    Always returns a :class:`JSONLStore` (the deliverable). When ``POSTGRES_DSN``
    is set, the JSONL store is composed with a Postgres state store that mirrors
    every kept document via ``UPSERT`` on ``id`` — the JSONL remains the artifact.
    """
    jsonl = JSONLStore(settings.output)
    if settings.postgres_dsn:
        from .postgres import PostgresStore  # imported lazily; optional dependency.

        logger.info("Postgres state backend enabled (UPSERT on id)")
        return _CompositeStore(primary=jsonl, mirrors=[PostgresStore(settings.postgres_dsn)])
    return jsonl


class _CompositeStore:
    """Routes the keep decision through the primary store and mirrors to others.

    The action (insert/skip/update) is decided once, by the primary JSONL store;
    each mirror is told to persist the same document so external state stays in
    lock-step with the deliverable.
    """

    def __init__(self, *, primary: Store, mirrors: list[Store]) -> None:
        self._primary = primary
        self._mirrors = mirrors

    def handle(self, doc: object) -> StoreAction:
        action = self._primary.handle(doc)  # type: ignore[arg-type]
        for mirror in self._mirrors:
            mirror.handle(doc)  # type: ignore[arg-type]
        return action

    def previous(self, doc_id: str) -> dict[str, object] | None:
        # Prior state is read from the primary (the deliverable), the source of truth.
        return self._primary.previous(doc_id)

    def finalize(self) -> None:
        self._primary.finalize()
        for mirror in self._mirrors:
            mirror.finalize()
