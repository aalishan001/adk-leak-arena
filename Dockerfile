FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed_cache

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# adk-memshield comes from PyPI; override only to test an unreleased build.
ARG MEMSHIELD_SPEC="adk-memshield>=0.2.0,<0.3"

# Install PyPI dependencies including adk-memshield and google-adk, plus the optional
# Qdrant and Mem0 targets.
RUN pip install --no-cache-dir \
    "${MEMSHIELD_SPEC}" \
    "google-adk>=2.12.0,<3" \
    "fastapi>=0.115.0" \
    "uvicorn>=0.32.0" \
    "litellm>=1.50.0" \
    "httpx>=0.27.0" \
    "psycopg[binary,pool]>=3.2.0" \
    "pydantic>=2.7.0" \
    "fastembed>=0.4.0" \
    "numpy>=1.26" \
    "qdrant-client>=1.20,<2" \
    "mem0ai>=2.2,<3" \
    "ollama>=0.4" \
    "pytest>=8.0.0" \
    "pytest-asyncio>=0.24.0"

# Pre-cache FastEmbed ONNX model (BAAI/bge-small-en-v1.5) for zero-latency offline startup
RUN mkdir -p /opt/fastembed_cache && \
    python -c "from fastembed import TextEmbedding; list(TextEmbedding('BAAI/bge-small-en-v1.5').embed(['adk-leak-arena warmup']))"

COPY pyproject.toml README.md LICENSE ./
COPY sql ./sql
COPY src ./src
COPY tests ./tests

RUN pip install --no-cache-dir --no-deps -e .

# Run as an unprivileged user (uid 10001, matching deploy/). Writable state lives in /tmp.
RUN useradd --uid 10001 --user-group --no-create-home --home-dir /tmp arena     && chown -R 10001:10001 /opt/fastembed_cache
USER 10001
ENV HOME=/tmp ARENA_STATE_DIR=/tmp/adk-leak-arena

EXPOSE 8090

CMD ["leak-arena", "serve", "--host", "0.0.0.0", "--port", "8090"]
