# Minimal runtime image for the scrape_site CLI.
# The engine is pure Python; the only system need is the lxml/trafilatura stack,
# which ships as manylinux wheels, so no compiler toolchain is required.
FROM python:3.13-slim

WORKDIR /app

# Install the package (its own metadata pulls in the runtime deps).
COPY app/pyproject.toml ./app/pyproject.toml
COPY app/extractor_engine ./app/extractor_engine

RUN pip install --no-cache-dir ./app

# scrape_site writes the JSONL deliverable into the working directory by default.
ENTRYPOINT ["scrape_site"]
