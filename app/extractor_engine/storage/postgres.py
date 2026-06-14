"""Optional Postgres state backend (activated by ``POSTGRES_DSN``).

This is *crawl state* for resumability and production-mindedness, not the
deliverable — the JSONL file remains the artifact. Documents are mirrored here
via ``UPSERT`` on ``id``, so unchanged pages are no-ops and changed pages replace
in place. Requires the ``postgres`` extra (``psycopg``). See
``docs/storage-and-idempotency.md``.
"""

from __future__ import annotations

import json
import logging

from ..engine.models import Document
from .base import StoreAction

logger = logging.getLogger("extractor_engine.storage.postgres")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS documents (
    id            TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL,
    document      JSONB NOT NULL
)
"""

_UPSERT = """
INSERT INTO documents (id, url, content_hash, fetched_at, document)
VALUES (%(id)s, %(url)s, %(content_hash)s, %(fetched_at)s, %(document)s)
ON CONFLICT (id) DO UPDATE SET
    url          = EXCLUDED.url,
    content_hash = EXCLUDED.content_hash,
    fetched_at   = EXCLUDED.fetched_at,
    document     = EXCLUDED.document
WHERE documents.content_hash IS DISTINCT FROM EXCLUDED.content_hash
"""


class PostgresStore:
    """Mirrors kept documents into a Postgres table via UPSERT on ``id``."""

    def __init__(self, dsn: str) -> None:
        import psycopg  # local import: only needed when the backend is enabled.

        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)

    def handle(self, doc: Document) -> StoreAction:
        """UPSERT one document. The action reported is always ``UPDATE`` here;
        the authoritative insert/skip/update decision is made by the primary
        JSONL store, with which this backend is composed.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                _UPSERT,
                {
                    "id": doc.id,
                    "url": doc.url,
                    "content_hash": doc.content_hash,
                    "fetched_at": doc.fetched_at,
                    "document": json.dumps(doc.model_dump(), ensure_ascii=False),
                },
            )
        return StoreAction.UPDATE

    def finalize(self) -> None:
        """Close the connection; UPSERTs are committed eagerly (autocommit)."""
        self._conn.close()
