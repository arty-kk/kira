#Dockerfile
FROM python:3.11-slim AS runtime

USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc build-essential libpq-dev libffi-dev libssl-dev \
        curl postgresql-client redis-tools tzdata ffmpeg procps && \
    python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y --auto-remove gcc build-essential libpq-dev libffi-dev libssl-dev && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY . .

RUN adduser --disabled-password --gecos '' appuser && \
    mkdir -p /app/data/embeddings && \
    chown -R appuser:appuser /app /opt/venv && \
    chmod 700 /app/data/embeddings

USER appuser

EXPOSE 8443
CMD ["python", "-u", "main.py"]
