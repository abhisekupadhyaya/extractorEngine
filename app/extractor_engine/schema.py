"""Emit the JSON Schema of the document object to stdout.

``make schema`` redirects this to ``docs/schema.json`` so the documented schema
is generated from the pydantic model and the two can never drift. Run as
``python -m extractor_engine.schema``.
"""

from __future__ import annotations

import json

from .engine.models import Document


def main() -> None:
    """Print the document JSON Schema as formatted JSON."""
    schema = Document.model_json_schema()
    print(json.dumps(schema, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
