cat > Dockerfile << EOF
# Dockerfile
FROM python:3.10-slim AS runtime

#############################
# 1) Preliminaries as root  #
#############################
USER root

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Copy requirements first for layer caching
COPY requirements.txt .

# Install build and runtime dependencies, create venv, install Python packages, then remove build deps and clean cache
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc build-essential libpq-dev libffi-dev libssl-dev \
        curl postgresql-client redis-tools && \
    python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y --auto-remove gcc build-essential libpq-dev libffi-dev libssl-dev && \
    rm -rf /var/lib/apt/lists/*

# Ensure venv is on PATH
ENV PATH="/opt/venv/bin:$PATH"

#############################
# 2) Copy application code  #
#############################
WORKDIR /app

# Copy all source (including alembic/ directory)
COPY . .

# Copy standalone alembic.ini to /app so alembic -c alembic.ini works
COPY alembic/alembic.ini /app/alembic.ini

# Copy and make entrypoint executable
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

################################
# 3) Precompute embeddings     #
################################
RUN mkdir -p /app/data/embeddings && \
    chmod 700 /app/data/embeddings && \
    EMBEDDING_MODEL=text-embedding-3-large python scripts/precompute_embeddings.py \
      --kb-file app/services/responder/rag/knowledge_on.json \
      --out-file data/embeddings/knowledge_embedded_text-embedding-3-large.json && \
    EMBEDDING_MODEL=text-embedding-3-large python scripts/precompute_embeddings.py \
      --kb-file app/services/responder/rag/knowledge_off.json \
      --out-file data/embeddings/knowledge_embedded_text-embedding-3-large-offtopic.json

#############################
# 4) Drop to non-root user  #
#############################
RUN adduser --disabled-password --gecos '' appuser && \
    chown -R appuser:appuser /app /opt/venv
USER appuser

################################
# 5) Expose port & Entrypoint  #
################################
EXPOSE 8443
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-u", "main.py"]
EOF