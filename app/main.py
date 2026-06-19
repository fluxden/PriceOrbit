"""FastAPI web entrypoint for PriceOrbit."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app import models  # noqa: F401  (ensures all tables register on Base.metadata)
from app.config import settings
from app.database import engine
from app.web.routes import router as web_router

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

app = FastAPI(title=settings.app_name, version=settings.app_version)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
os.makedirs(settings.uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.uploads_dir), name="uploads")
app.include_router(web_router)

_AUTH_EXEMPT = {"/login", "/logout", "/health"}
_ADMIN_ONLY_PREFIXES = ("/users", "/settings", "/alerts", "/admin")

# Request access log -> shared log file (the Admin → Logs table parses these).
# Static assets and the health probe are skipped to keep the log readable.
_access_log = logging.getLogger("access")
_ACCESS_SKIP = ("/static", "/uploads", "/favicon")

# Auth/middleware errors. Surfaced (not swallowed) so a broken schema or DB
# hiccup that disrupts sign-in is visible in Admin → Logs instead of silent.
_auth_log = logging.getLogger("auth")


@app.on_event("startup")
def _init_logging() -> None:
    """Configure logging from the stored level (falling back to the env default)."""
    from app.logsetup import configure

    level = settings.log_level
    try:
        from app.database import SessionLocal
        from app.services import settings_store
        db = SessionLocal()
        try:
            level = settings_store.get_config(db).get("log_level") or level
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - never block startup over logging config
        pass
    configure(level, settings.log_file)


@app.middleware("http")
async def require_login(request, call_next):
    """When login is enabled: require a session, and gate admin-only areas."""
    from urllib.parse import quote

    from starlette.responses import RedirectResponse

    path = request.url.path
    if path.startswith(("/static", "/uploads", "/login/oidc", "/auth/oidc")) or path in _AUTH_EXEMPT:
        return await call_next(request)

    from app.database import SessionLocal
    from app.services import auth

    db = SessionLocal()
    try:
        # Whether sign-in is on. A transient failure reading this is ambiguous —
        # we don't know if auth is required — so fail OPEN to avoid locking
        # everyone out over a DB hiccup (anti-lockout). The error is logged.
        try:
            login_on = auth.login_enabled(db)
        except Exception:  # noqa: BLE001
            _auth_log.exception("require_login: reading login_enabled failed for %s; allowing through", path)
            return await call_next(request)

        if login_on:
            # Sign-in IS required. If the user can't be resolved — e.g. a schema
            # mismatch from a migration that never applied — fail CLOSED and send
            # to /login. Failing open here would silently bypass authentication.
            try:
                user = auth.current_user(request, db)
            except Exception:  # noqa: BLE001
                _auth_log.exception("require_login: user lookup failed for %s; denying (sign-in required)", path)
                return RedirectResponse(f"/login?next={quote(path, safe='')}", status_code=303)
            if user is None:
                return RedirectResponse(f"/login?next={quote(path, safe='')}", status_code=303)
            if user.role != "admin" and path.startswith(_ADMIN_ONLY_PREFIXES):
                return RedirectResponse("/profile?error=Admins+only", status_code=303)
    finally:
        db.close()
    return await call_next(request)


# Added after require_login so it wraps it (last-added middleware is outermost),
# letting it time and log auth redirects too.
@app.middleware("http")
async def access_logging(request, call_next):
    """Log one structured line per request: ``<client> <method> <path> <status>
    <duration>ms``. 4xx are WARN, 5xx are ERROR, so the level reflects health."""
    start = time.perf_counter()
    response = await call_next(request)
    path = request.url.path
    if not path.startswith(_ACCESS_SKIP) and path != "/health":
        dur_ms = (time.perf_counter() - start) * 1000.0
        xff = request.headers.get("x-forwarded-for", "")
        client = xff.split(",")[0].strip() if xff else (
            request.client.host if request.client else "-")
        status = response.status_code
        level = logging.INFO if status < 400 else (
            logging.WARNING if status < 500 else logging.ERROR)
        _access_log.log(level, "%s %s %s %d %.1fms",
                        client, request.method, path, status, dur_ms)
    return response


@app.get("/health")
def health() -> dict:
    """Liveness + database connectivity check."""
    database = "ok"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - report any connectivity error
        database = f"error: {exc}"
    return {"status": "ok", "app": settings.app_name, "database": database}
