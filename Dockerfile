FROM python:3.12-slim AS base

# Never run as root: a compromised dependency should not own the container.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[postgres]"

COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY tests/fixtures/ ./tests/fixtures/

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"

CMD ["event-arb", "serve"]
