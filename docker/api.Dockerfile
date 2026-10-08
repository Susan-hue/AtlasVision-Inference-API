# syntax=docker/dockerfile:1

FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=10 \
    PIP_ROOT_USER_ACTION=ignore
WORKDIR /srv
RUN useradd --create-home --uid 10001 app

FROM base AS runtime
COPY requirements/api.txt requirements/sdk.txt requirements/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements/api.txt && pip install --no-deps -r requirements/sdk.txt
COPY app ./app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS dev
COPY requirements/ requirements/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements/dev.txt && pip install --no-deps -r requirements/sdk.txt
ENV RUFF_CACHE_DIR=/tmp/ruff-cache
USER app
