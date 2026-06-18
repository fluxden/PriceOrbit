"""FastAPI web entrypoint for PriceOrbit."""
from __future__ import annotations

import os
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
    try:
        from app.database import SessionLocal
        from app.services import auth
        db = SessionLocal()
        try:
            if auth.login_enabled(db):
                user = auth.current_user(request, db)
                if user is None:
                    return RedirectResponse(f"/login?next={quote(path, safe='')}", status_code=303)
                if user.role != "admin" and path.startswith(_ADMIN_ONLY_PREFIXES):
                    return RedirectResponse("/profile?error=Admins+only", status_code=303)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — fail open so a settings/DB hiccup can't lock everyone out
        pass
    return await call_next(request)


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
