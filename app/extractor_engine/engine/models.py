"""The single data contract of the pipeline: the AI document object.

Every stage produces, enriches, or persists one of these. The model is the
authority on shape — ``docs/schema.json`` is generated from
:meth:`Document.model_json_schema`, so documentation and code cannot drift. See
``docs/data-model.md`` for the field-by-field reference and the missing-vs-empty
conventions encoded here.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ContentType(StrEnum):
    """Closed vocabulary for ``signals.content_type``.

    A closed enum means a consumer's filter never silently misses a typo'd or
    unexpected variant; an unfamiliar page is classified as :attr:`OTHER` rather
    than inventing a new label. See ``docs/data-model.md``.
    """

    PRODUCT_PAGE = "product_page"
    DOC_PAGE = "doc_page"
    ARTICLE = "article"
    INDEX = "index"
    OTHER = "other"


class Signals(BaseModel):
    """Derived quality signals, grouped so they can be versioned as one unit.

    Nesting these under ``signals`` is a contract choice, not a performance one:
    each JSONL record loads whole, so the grouping simply keeps identity fields
    separate from the derived "filter panel".
    """

    word_count: int = Field(ge=0, description="Whitespace-delimited token count of body_text.")
    char_count: int = Field(ge=0, description="Character length of body_text.")
    language: str = Field(
        description="ISO 639-1 language code, or 'und' when detection is not possible.",
    )
    content_type: ContentType = Field(description="Page kind (controlled vocabulary).")
    extraction_layer: str = Field(
        description="Which cascade layer produced body_text "
        "(semantic/library/density/crude); a consumer confidence signal."
    )
    is_mostly_code: bool = Field(description="Whether the page is predominantly code.")


class Document(BaseModel):
    """One kept web page, serialized as one line of JSONL.

    The shape is *hybrid*: identity and extracted-content fields live at the top
    level, while derived signals are grouped under :class:`Signals`. Collections
    default to empty (never null); genuinely optional scalars default to null.
    """

    id: str = Field(description="uuid5 of the canonical URL; stable identity / upsert key.")
    url: str = Field(description="The canonical URL of the page (the exact string hashed for id).")
    title: str = Field(description="Page title resolved by a precedence cascade; '' if none.")
    body_text: str = Field(description="Clean main content; nav/header/sidebar/footer removed.")
    author: str | None = Field(
        default=None, description="Primary author from generic declared sources, or null if none declared."
    )
    tags: list[str] = Field(default_factory=list, description="Topical labels; [] if none.")
    published_at: str | None = Field(
        default=None, description="Publication timestamp (ISO8601 UTC) or null if none exists."
    )
    modified_at: str | None = Field(
        default=None, description="Last-modified timestamp (ISO8601 UTC) or null."
    )
    fetched_at: str = Field(description="When the current version was captured; tz-aware UTC ISO8601.")
    content_hash: str = Field(description="sha256 hex of body_text; drives change detection.")
    signals: Signals = Field(description="Derived quality signals (see Signals).")
    extra: dict[str, object] = Field(
        default_factory=dict, description="Optional structured attributes (e.g. from JSON-LD); {} if none."
    )
