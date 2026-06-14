"""The default zero-infrastructure JSONL store.

Seeds its state from the existing output file, applies the three-case
insert/skip/update decision per document, and writes the full corpus once,
atomically, at the end (temp file + ``os.replace``) so a crash mid-write never
leaves a corrupt deliverable. This whole-file rewrite holds the corpus in memory,
which suits the bounded single-site scope here. See
``docs/storage-and-idempotency.md``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from ..engine.models import Document
from .base import StoreAction

logger = logging.getLogger("extractor_engine.storage.jsonl")


class JSONLStore:
    """An idempotent JSONL store keyed on document ``id``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        # id -> (content_hash, document-dict), insertion-ordered for stable output.
        self._records: dict[str, tuple[str, dict[str, object]]] = {}
        self._counts = {action: 0 for action in StoreAction}
        self._seed_from_existing()

    def _seed_from_existing(self) -> None:
        """Reconstruct state from a prior run's output file, if present."""
        if not self._path.exists():
            return
        loaded = 0
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("ignoring malformed line while seeding state")
                    continue
                doc_id = doc.get("id")
                content_hash = doc.get("content_hash")
                if isinstance(doc_id, str) and isinstance(content_hash, str):
                    self._records[doc_id] = (content_hash, doc)
                    loaded += 1
        if loaded:
            logger.info("seeded %d existing record(s) from %s", loaded, self._path)

    def handle(self, doc: Document) -> StoreAction:
        """Apply the insert / skip / update decision for one document."""
        existing = self._records.get(doc.id)
        if existing is None:
            action = StoreAction.INSERT
        elif existing[0] == doc.content_hash:
            # Unchanged: keep the stored record untouched (fetched_at is not refreshed).
            self._counts[StoreAction.SKIP] += 1
            return StoreAction.SKIP
        else:
            action = StoreAction.UPDATE

        self._records[doc.id] = (doc.content_hash, doc.model_dump())
        self._counts[action] += 1
        return action

    def previous(self, doc_id: str) -> dict[str, object] | None:
        """The stored record for ``doc_id`` (prior run's, before this run rewrites it)."""
        existing = self._records.get(doc_id)
        return existing[1] if existing is not None else None

    def finalize(self) -> None:
        """Write the whole corpus atomically: temp file then ``os.replace``."""
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", encoding="utf-8") as handle:
            for _content_hash, doc in self._records.values():
                handle.write(json.dumps(doc, ensure_ascii=False))
                handle.write("\n")
        os.replace(tmp_path, self._path)
        logger.info(
            "wrote %d document(s) to %s (insert=%d update=%d skip=%d)",
            len(self._records),
            self._path,
            self._counts[StoreAction.INSERT],
            self._counts[StoreAction.UPDATE],
            self._counts[StoreAction.SKIP],
        )

    @property
    def counts(self) -> dict[StoreAction, int]:
        """Per-action tallies for run reporting."""
        return dict(self._counts)
