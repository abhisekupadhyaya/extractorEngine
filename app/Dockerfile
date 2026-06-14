# Minimal runtime image for the scrape_site CLI.
#
# Two build targets:
#   * default (slim) — the static HTTP fetcher; no browser, smallest image.
#   * render        — adds the [render] extra and a headless Chromium for --render.
#
# Build the slim default:   docker build -f app/Dockerfile --target default -t scraper .
# Build the rendering image: docker build -f app/Dockerfile --target render  -t scraper:render .

# --- shared base: install the package (its metadata pulls in runtime deps) ----
FROM python:3.13-slim AS base
WORKDIR /app
COPY app/pyproject.toml ./app/pyproject.toml
COPY app/extractor_engine ./app/extractor_engine

# --- default (slim) image: static fetcher only --------------------------------
FROM base AS default
RUN pip install --no-cache-dir ./app
# scrape_site writes the JSONL deliverable into the working directory by default.
ENTRYPOINT ["scrape_site"]

# --- render image: adds the headless browser for JavaScript-rendered sites ----
FROM base AS render
RUN pip install --no-cache-dir "./app[render]" \
    && playwright install --with-deps chromium
ENTRYPOINT ["scrape_site"]
