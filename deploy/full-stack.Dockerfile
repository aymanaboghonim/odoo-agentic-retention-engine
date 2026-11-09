FROM python:3.14.9-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae

ENV UV_PROJECT_ENVIRONMENT=/opt/engine-venv \
    PYTHONPATH=/workspace/src \
    PYTHONUNBUFFERED=1
WORKDIR /workspace
COPY pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir uv==0.13.5 \
    && uv sync --locked --no-dev --group engine --group retrieval-benchmark --no-install-project
COPY src ./src
COPY scripts ./scripts
COPY policies ./policies
ENV PATH="/opt/engine-venv/bin:${PATH}"
