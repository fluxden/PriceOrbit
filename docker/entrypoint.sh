#!/usr/bin/env bash
set -e

# 1) Wait until the database accepts TCP connections.
python - <<'PY'
import os, socket, time

host = os.getenv("DB_HOST", "db")
port = int(os.getenv("DB_PORT", "3306"))

for attempt in range(1, 61):
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"[entrypoint] Database reachable at {host}:{port}")
            break
    except OSError:
        print(f"[entrypoint] Waiting for database {host}:{port} ... ({attempt}/60)")
        time.sleep(2)
else:
    raise SystemExit("[entrypoint] Database not reachable, giving up.")
PY

# 2) Apply database migrations.
echo "[entrypoint] Applying database migrations..."
alembic upgrade head

# 3) Start the scheduler worker in the background.
echo "[entrypoint] Starting worker..."
python -m app.worker &
worker_pid=$!

# 4) Start the web server in the background.
echo "[entrypoint] Starting web server..."
# --no-access-log: the app emits its own richer access log (with duration) to the
# shared log file; this avoids duplicate, less-detailed lines on stdout.
uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log &
web_pid=$!

# If either process exits, stop the container with its status.
wait -n "$worker_pid" "$web_pid"
exit $?
