"""Lightweight security audit log."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEvent

LABELS = {
    "login.success": "Signed in",
    "login.failure": "Failed sign-in",
    "logout": "Signed out",
    "admin.created": "Admin account created",
    "login.enabled": "Login requirement enabled",
    "login.disabled": "Login requirement disabled",
    "user.created": "User created",
    "user.role_changed": "Role changed",
    "user.activated": "User enabled",
    "user.deactivated": "User disabled",
    "user.password_reset": "Password reset",
    "user.deleted": "User deleted",
    "profile.password_changed": "Password changed",
    "account.deleted": "Account self-deleted",
    "data.exported": "Data exported",
    "oidc.updated": "SSO settings changed",
}


def log(db: Session, action: str, actor: str = "system",
        detail: str | None = None, ip: str | None = None) -> None:
    """Write an audit entry. Never raises — auditing must not break the action."""
    try:
        db.add(AuditEvent(action=action, actor=(actor or "system")[:255], detail=detail, ip=ip))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def recent(db: Session, limit: int = 50) -> list[AuditEvent]:
    return db.execute(
        select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit)
    ).scalars().all()
