FROM python:3.14.9-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae

ENV UV_PROJECT_ENVIRONMENT=/opt/s3-benchmark-venv
WORKDIR /workspace

COPY pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir uv==0.13.5 \
    && uv sync --locked --no-dev --group retrieval-benchmark --no-install-project

COPY scripts/s3_retrieval_benchmark.py ./scripts/s3_retrieval_benchmark.py
COPY evals/retrieval/s3_dataset.json ./evals/retrieval/s3_dataset.json

ENTRYPOINT ["uv", "run", "--locked", "--no-sync", "python", "scripts/s3_retrieval_benchmark.py"]
