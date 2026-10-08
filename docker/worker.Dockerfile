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

# No torch: this image can only run FakeAtlasVision.
FROM base AS fake
COPY requirements/base.txt requirements/sdk.txt requirements/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements/base.txt && pip install --no-deps -r requirements/sdk.txt
COPY app ./app
USER app
ENV USE_FAKE_MODEL=true
CMD ["python", "-m", "app.worker"]
