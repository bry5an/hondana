# syntax=docker/dockerfile:1.7
# Hondana server image. Debian (glibc), not Alpine: MeCab/fugashi publish no musl wheels.
# Build: docker build -t hondana .   Run: see docker/compose.yaml and docs/docker.md.

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first, so a code change doesn't reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project
COPY . /app
# The project itself installs editable, so epublib/ finds templates/ and static/ beside it.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.title="Hondana" \
      org.opencontainers.image.description="A self-hosted library for learning Japanese from your own EPUBs and audiobooks" \
      org.opencontainers.image.source="https://github.com/bry5an/hondana" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"
COPY --from=build /app /app
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    HONDANA_HOME=/config \
    HONDANA_DATA_DIR=/data \
    HONDANA_EPUB_DIR=/books \
    HONDANA_AUDIOBOOK_DIRS=/audiobooks \
    HONDANA_IN_CONTAINER=1 \
    HONDANA_SUPERVISED=1
# Writable by the default user; compose's `user:` (PUID/PGID) needs its own ownership.
RUN mkdir -p /config /data /books /audiobooks /inbox \
 && chown 1000:1000 /config /data /books /audiobooks /inbox
VOLUME ["/config", "/data"]
EXPOSE 8000
USER 1000:1000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('HONDANA_PORT', '8000'), timeout=4)"]
CMD ["hondana", "serve"]
