"""The storage contract: the ``Store`` protocol and the three-case action enum.

A store consumes kept :class:`~extractor_engine.engine.models.Document` objects
one at a time, deciding insert / skip / update by comparing ``content_hash``
against prior state, and flushes the result on ``finalize``. See
``docs/storage-and-idempotency.md``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..engine.models import Document


class StoreAction(StrEnum):
    """The outcome of handling one document, per the across-run idempotency table."""

    INSERT = "insert"  # id never seen before
    SKIP = "skip"  # id seen, content_hash unchanged
    UPDATE = "update"  # id seen, content_hash differs


@runtime_checkable
class Store(Protocol):
    """Persists documents idempotently keyed on ``id``."""

    def handle(self, doc: Document) -> StoreAction:
        """Insert, skip, or update one document; return which happened."""
        ...

    def finalize(self) -> None:
        """Flush all accumulated state to its backing medium (atomically)."""
        ...
