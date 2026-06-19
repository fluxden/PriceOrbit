# syntax=docker/dockerfile:1

# --- Build stage: install deps into a self-contained venv ---
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN python -m venv /opt/venv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --- Runtime stage ---
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# curl for healthchecks; setpriv (util-linux) drops root in the entrypoint.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN adduser --disabled-password --gecos "" appuser

COPY --from=builder /opt/venv /opt/venv
COPY . .

RUN mkdir -p /data/uploads /data/logs
RUN chmod +x /app/docker/entrypoint.sh
RUN chown -R appuser:appuser /app /data

# Stays root so the entrypoint can chown bind mounts, then drops to appuser.
ENTRYPOINT ["/app/docker/entrypoint.sh"]
