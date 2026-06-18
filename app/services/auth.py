"""Authentication primitives — dependency-free (Python standard library only).

Password hashing uses PBKDF2-HMAC-SHA256 with a per-user salt. Sessions are a
compact HMAC-signed, base64url cookie keyed off APP_SECRET (no server-side store).
Both avoid extra dependencies and work offline.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User
from app.services import settings_store

# ---- password hashing (pbkdf2_sha256$iterations$salt_hex$hash_hex) ----

_PBKDF2_ITERATIONS = 240_000
_ALGO = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_ALGO}${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters_s))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# ---- signed session cookie ----

SESSION_COOKIE = "priceorbit_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


def _secret() -> bytes:
    return settings.app_secret.encode("utf-8")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_session(data: dict) -> str:
    payload = dict(data)
    payload.setdefault("iat", int(time.time()))
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def load_session(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expected = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(time.time()) - int(data.get("iat", 0)) > SESSION_MAX_AGE:
        return None
    return data


# ---- user helpers ----

def admin_exists(db: Session) -> bool:
    return db.execute(
        select(func.count()).select_from(User).where(User.role == "admin")
    ).scalar_one() > 0


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.execute(
        select(User).where(func.lower(User.username) == username.strip().lower())
    ).scalars().first()


def create_user(db: Session, username: str, password: str, role: str = "user",
                display_name: str | None = None, must_change_password: bool = False) -> User:
    user = User(
        username=username.strip(), password_hash=hash_password(password),
        role=role if role in ("admin", "user") else "user",
        display_name=(display_name or None), must_change_password=must_change_password,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def current_user(request, db: Session) -> User | None:
    """Resolve the signed session cookie to an active user, or None."""
    data = load_session(request.cookies.get(SESSION_COOKIE))
    if not data or "uid" not in data:
        return None
    user = db.get(User, data["uid"])
    if user is None or not user.is_active:
        return None
    return user


def login_enabled(db: Session) -> bool:
    cfg = settings_store.get_config(db)
    return cfg.get("login_enabled", "0") in ("1", "true", "True")


def active_admin_count(db: Session) -> int:
    return db.execute(
        select(func.count()).select_from(User)
        .where(User.role == "admin", User.is_active == True)  # noqa: E712
    ).scalar_one()


def set_password(db: Session, user: User, new_password: str, must_change: bool = False) -> None:
    user.password_hash = hash_password(new_password)
    user.must_change_password = must_change
    db.commit()


def list_users(db: Session) -> list[User]:
    return db.execute(select(User).order_by(User.role.desc(), User.username)).scalars().all()


def get_user_by_oidc_subject(db: Session, subject: str) -> User | None:
    if not subject:
        return None
    return db.execute(select(User).where(User.oidc_subject == subject)).scalars().first()


def create_oidc_user(db: Session, username: str, subject: str, role: str = "user",
                     display_name: str | None = None) -> User:
    """Create an SSO-provisioned user. Local password is random/unusable."""
    user = User(
        username=username.strip(), password_hash=hash_password(secrets.token_urlsafe(32)),
        role=role if role in ("admin", "user") else "user",
        display_name=(display_name or None), oidc_subject=subject,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
