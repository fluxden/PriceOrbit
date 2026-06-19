FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Minimal system deps. (Scraping libs that need libxml2/etc. will be added
# alongside the scraping step so the base image stays small for now.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App source
COPY . .

# Non-root user. Strip any CRLF first so the shebang works even if the build
# context was checked out on Windows (env: 'bash\r': No such file or directory).
RUN sed -i 's/\r$//' /app/docker/entrypoint.sh \
    && chmod +x /app/docker/entrypoint.sh \
    && adduser --disabled-password --gecos "" appuser \
    && mkdir -p /data/uploads /data/logs \
    && chown -R appuser:appuser /app /data
USER appuser

# Entrypoint starts both the web server and the scheduler worker in one
# container; nothing needs to be passed from docker-compose.
ENTRYPOINT ["/app/docker/entrypoint.sh"]
