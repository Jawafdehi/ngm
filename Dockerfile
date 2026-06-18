FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=2.3.2

WORKDIR /app

RUN pip install "poetry==${POETRY_VERSION}"

# Install dependencies first so code changes don't bust the dependency layer.
COPY pyproject.toml poetry.lock README.md ./
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-root --only main

COPY scrapy.cfg ./
COPY ngm/ ./ngm/
COPY scripts/ ./scripts/

# Spiders run via `scrapy crawl <name>`; pipelines via `python -m ngm.<module>`.
# CronJobs override command/args per spider.
CMD ["scrapy", "list"]
