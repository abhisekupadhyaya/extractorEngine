"""Corpus analytics over a JSONL output file.

Reads the deliverable back and computes simple aggregate statistics — document
count, average length, and the language and content-type distributions. It
doubles as a QA tool: a quick scan of the distributions surfaces a misbehaving
run (e.g. a flood of ``index`` pages or ``und`` languages). Run as
``python -m extractor_engine.analytics output.jsonl``.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path


def summarize_file(path: str | Path) -> dict[str, object]:
    """Summarize the JSONL file at ``path`` (an empty summary if it is absent)."""
    file_path = Path(path)
    if not file_path.exists():
        return _summarize([])
    with file_path.open("r", encoding="utf-8") as handle:
        docs = [json.loads(line) for line in handle if line.strip()]
    return _summarize(docs)


def _summarize(docs: Iterable[dict[str, object]]) -> dict[str, object]:
    """Compute aggregate statistics over already-parsed document dicts."""
    docs = list(docs)
    count = len(docs)
    languages: dict[str, int] = defaultdict(int)
    content_types: dict[str, int] = defaultdict(int)
    total_words = 0
    total_chars = 0

    for doc in docs:
        signals = doc.get("signals", {}) if isinstance(doc, dict) else {}
        if isinstance(signals, dict):
            languages[str(signals.get("language", "und"))] += 1
            content_types[str(signals.get("content_type", "other"))] += 1
            total_words += int(signals.get("word_count", 0) or 0)
            total_chars += int(signals.get("char_count", 0) or 0)

    return {
        "document_count": count,
        "avg_word_count": (total_words / count) if count else 0.0,
        "avg_char_count": (total_chars / count) if count else 0.0,
        "language_distribution": dict(languages),
        "content_type_distribution": dict(content_types),
    }


def main(argv: list[str] | None = None) -> int:
    """Print a JSON summary of the given JSONL file to stdout."""
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m extractor_engine.analytics <output.jsonl>", file=sys.stderr)
        return 2
    print(json.dumps(summarize_file(args[0]), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
