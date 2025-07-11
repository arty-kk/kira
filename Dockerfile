#Dockerfile

FROM python:3.10-slim AS runtime

# ─── System deps ──────────────────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        build-essential \
        libpq-dev \
        libffi-dev \
        libssl-dev \
        curl \
        postgresql-client \
        redis-tools && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ─── Python env ──────────────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# ─── Install requirements ────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ─── Copy code + entrypoint ──────────────────────────────────────────
WORKDIR /app
COPY . .

# Сценарий точка входа, который сперва прогоняет миграции, потом стартует
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# ─── Precompute embeddings ───────────────────────────────────────────
RUN EMBEDDING_MODEL=text-embedding-3-large \
    python scripts/precompute_embeddings.py \
      --kb-file app/services/responder/rag/knowledge_on.json \
      --out-file data/embeddings/knowledge_embedded_text-embedding-3-large.json

# 2) off-topic
RUN EMBEDDING_MODEL=text-embedding-3-large \
    python scripts/precompute_embeddings.py \
      --kb-file app/services/responder/rag/knowledge_off.json \
      --out-file data/embeddings/knowledge_embedded_text-embedding-3-large-offtopic.json

# ─── Drop privileges ─────────────────────────────────────────────────
RUN adduser --disabled-password --gecos '' appuser
USER appuser

# ─── Ports & cmd ────────────────────────────────────────────────────
EXPOSE 8443
CMD ["main.py"]