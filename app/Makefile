# Developer entry points for the extractor-engine package.
# All targets use the project virtualenv interpreter by default; override PY to
# point at a different Python.
PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: install test lint typecheck schema run clean

install:  ## Editable install with dev extras.
	$(PIP) install -e ".[dev]"

test:  ## Run the test suite.
	$(PY) -m pytest

lint:  ## Lint with ruff.
	$(PY) -m ruff check extractor_engine ../tests

typecheck:  ## Static type-check with mypy.
	$(PY) -m mypy

schema:  ## Regenerate docs/schema.json from the pydantic model.
	$(PY) -m extractor_engine.schema > ../docs/schema.json
	@echo "wrote ../docs/schema.json"

run:  ## Run a small demo crawl against the sandbox site.
	$(PY) -m extractor_engine.cli --start-url=https://books.toscrape.com/ --max-pages=20 --output=output.jsonl

clean:  ## Remove build/cache artifacts.
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
