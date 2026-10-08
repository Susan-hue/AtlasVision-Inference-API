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

# Real model. Built on the GPU host only: torch and its CUDA libraries are several GB.
# The torch wheels bundle CUDA 13.0 (driver 580 or newer); this base adds no CUDA libraries.
FROM nvidia/cuda:13.0.3-base-ubuntu24.04 AS gpu
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=120 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HF_HOME=/models/hf \
    USE_FAKE_MODEL=false
# Make the NVIDIA runtime refuse to start the container on a driver too old for CUDA 13.0.
# The base image's default also admits older data-centre drivers through forward-compatibility
# libraries, which this image does not ship.
ENV NVIDIA_REQUIRE_CUDA="cuda>=13.0"
WORKDIR /srv
RUN uv venv --python 3.11 /opt/venv
COPY requirements/worker-gpu.txt requirements/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --require-hashes -r requirements/worker-gpu.txt
# The SDK README's Colab notes uninstall torchao: when present, transformers imports it and
# version mismatches break model loading. Nothing here depends on it; keep it that way.
RUN python -c "import importlib.util as u, sys; sys.exit('torchao must not be installed' if u.find_spec('torchao') else 0)"
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /models/hf \
    && chown app /models/hf
COPY app ./app
USER app
CMD ["python", "-m", "app.worker"]
