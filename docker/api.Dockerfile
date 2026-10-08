# syntax=docker/dockerfile:1
# API image: FastAPI only. No GPU, no torch, no model weights, no HF_TOKEN.

FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=10 \
    PIP_ROOT_USER_ACTION=ignore
WORKDIR /srv
RUN useradd --create-home --uid 10001 app

# Production image.
FROM base AS runtime
COPY requirements/api.txt requirements/api.txt
# The cache mount keeps downloaded wheels between builds without baking them into the image.
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements/api.txt
COPY app ./app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

# Test/lint image. Source is bind-mounted at /srv by docker-compose.
FROM base AS dev
COPY requirements/ requirements/
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements/dev.txt
# Keep tool caches out of the bind-mounted source tree.
ENV RUFF_CACHE_DIR=/tmp/ruff-cache
USER app
