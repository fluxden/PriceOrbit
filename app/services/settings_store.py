"""DB-backed configuration for alert channels and message templates.

Values live in the ``settings`` key/value table and override the ``.env``
defaults exposed by :mod:`app.config`. Secret values (SMTP password, email API
key, Telegram bot token) are encrypted at rest with a key derived from
``APP_SECRET``. The Alerts page reads/writes through here; the notifier reads
the resolved config to actually send.
"""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings as env
from app.models import Setting

# Keys whose stored value is encrypted and never echoed back to the UI.
SECRET_KEYS = {"smtp_password", "email_api_key", "telegram_bot_token", "oidc_client_secret",
               "scrapedo_token"}

# Default message templates (the user's wording, made safe for increases too).
DEFAULT_TEMPLATES = {
    "tpl_price_subject": "{product_name} price {direction} to {current_price}",
    "tpl_price_body": "{product_name} price has {direction} {change_amount} to {current_price} on {datetime} at {store_name}.",
    "tpl_stock_subject": "{product_name} is back in stock",
    "tpl_stock_body": "{product_name} is in-stock at {store_name} {datetime} for {current_price}.",
}

# Placeholders offered as click-to-insert chips in the template editors.
PLACEHOLDERS = [
    "product_name", "store_name", "current_price", "old_price", "change_amount",
    "percent_change", "direction", "target_price", "datetime", "currency", "url",
]


def _defaults() -> dict[str, str]:
    """Baseline config seeded from environment values."""
    return {
        "email_method": "smtp",                       # smtp | api
        "smtp_host": env.smtp_host,
        "smtp_port": str(env.smtp_port),
        "smtp_user": env.smtp_user,
        "smtp_password": env.smtp_password,
        "smtp_from": env.smtp_from,
        "smtp_use_tls": "1" if env.smtp_use_tls else "0",
        "email_api_provider": "sendgrid",             # sendgrid | mailgun | resend | postmark
        "email_api_key": "",
        "email_api_from": "",
        "email_api_domain": "",                       # used by Mailgun
        "email_html": "0",
        "telegram_bot_token": env.telegram_bot_token,
        # scrape.do paid fallback engine (editable on the Settings page).
        # Seeded from env so an existing SCRAPEDO_TOKEN deployment stays active.
        "scrapedo_enabled": "1" if env.scrapedo_token else "0",
        "scrapedo_token": env.scrapedo_token,
        "scrapedo_render": "1" if env.scrapedo_render else "0",
        "scrapedo_super": "1" if env.scrapedo_super else "0",
        "scrapedo_geo": env.scrapedo_geo,
        "scrapedo_timeout_seconds": str(env.scrapedo_timeout_seconds),
        "scrapedo_monthly_credits": str(env.scrapedo_monthly_credits),
        "notifications_paused": "0",
        "quiet_enabled": "0",
        "quiet_start": "22:00",
        "quiet_end": "07:00",
        # logging (editable in Admin → Logs)
        "log_level": env.log_level,
        # general options
        "timezone": env.timezone,
        "date_format": "%b %d, %Y",
        "time_format": "24",                          # 12 | 24
        "default_currency": "USD",
        # authentication (Phase 2)
        "login_enabled": "0",
        "allow_local_login": "1",
        # OIDC single sign-on (Phase 3)
        "oidc_enabled": "0",
        "oidc_provider_name": "SSO",
        "oidc_issuer": "",
        "oidc_client_id": "",
        "oidc_client_secret": "",
        "oidc_scopes": "openid email profile",
        "oidc_auto_provision": "1",
        "oidc_default_role": "user",
        # login page customization (edited in Phase 2e)
        "login_heading": "",
        "login_subtext": "",
        "login_bg": "",
        "login_logo": "",
        # appearance (blank = use the built-in default token for that slot)
        "theme_base": "",                             # "" auto | light | dark
        "theme_accent": "",
        "theme_sidebar_bg": "",
        "theme_topbar_accent": "",
        "theme_link": "",
        **DEFAULT_TEMPLATES,
    }


# ---- encryption ----

def _fernet() -> Fernet:
    digest = hashlib.sha256((env.app_secret or "change-me").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode()).decode()


def _decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return token  # tolerate values stored before encryption was enabled


# ---- read / write ----

# LOGIN_TYPE env override → forced sign-in settings (recovery / anti-lockout).
_LOGIN_OVERRIDES = {
    "off": {"login_enabled": "0"},
    "none": {"login_enabled": "0"},
    "disabled": {"login_enabled": "0"},
    "standard": {"login_enabled": "1", "oidc_enabled": "0", "allow_local_login": "1"},
    "local": {"login_enabled": "1", "oidc_enabled": "0", "allow_local_login": "1"},
    "oidc": {"login_enabled": "1", "oidc_enabled": "1", "allow_local_login": "1"},
    "sso": {"login_enabled": "1", "oidc_enabled": "1", "allow_local_login": "1"},
}
_LOGIN_LABELS = {"off": "OFF", "none": "OFF", "disabled": "OFF",
                 "standard": "Standard", "local": "Standard",
                 "oidc": "OIDC", "sso": "OIDC"}
# LOGIN_TYPE values that would disable sign-in entirely.
_LOGIN_OFF = {"off", "none", "disabled"}


def _admin_exists(db: Session) -> bool:
    """True if any admin account exists. Used to stop LOGIN_TYPE=off from
    disabling sign-in once there's an admin to protect."""
    from app.models import User  # lazy import: keep module load free of cycles
    try:
        return (db.execute(
            select(func.count()).select_from(User).where(User.role == "admin")
        ).scalar() or 0) > 0
    except Exception:  # noqa: BLE001 - users table not ready (fresh install) → no admin yet
        return False


def login_type_override(db: Session | None = None) -> str:
    """Effective forced-login label ('OFF'|'Standard'|'OIDC') if LOGIN_TYPE is set,
    else ''. An 'off' override is reported as 'Standard' once an admin account
    exists — matching the security upgrade applied in :func:`get_config`."""
    label = _LOGIN_LABELS.get((env.login_type or "").strip().lower(), "")
    if label == "OFF" and db is not None and _admin_exists(db):
        return "Standard"
    return label


def get_config(db: Session) -> dict[str, str]:
    """Full resolved config with secrets DECRYPTED (for the notifier)."""
    cfg = _defaults()
    rows = db.execute(select(Setting)).scalars().all()
    for row in rows:
        if row.key in cfg or row.key in SECRET_KEYS:
            cfg[row.key] = _decrypt(row.value or "") if row.key in SECRET_KEYS else (row.value or "")
    # LOGIN_TYPE env wins over stored values so OIDC can't lock anyone out. One
    # exception, for security: an "off" override must NOT disable sign-in once an
    # admin account exists, or anyone could bypass auth by setting LOGIN_TYPE=off.
    # In that case force Standard (local) login instead. "off" still works as the
    # anti-lockout recovery switch on a fresh install (no admin yet).
    login_key = (env.login_type or "").strip().lower()
    override = _LOGIN_OVERRIDES.get(login_key)
    if override:
        if login_key in _LOGIN_OFF and _admin_exists(db):
            override = _LOGIN_OVERRIDES["standard"]
        cfg.update(override)
    return cfg


def get_public(db: Session) -> dict:
    """Config for templates: secret VALUES removed, replaced by ``*_is_set`` flags."""
    cfg = get_config(db)
    public = {k: v for k, v in cfg.items() if k not in SECRET_KEYS}
    for k in SECRET_KEYS:
        public[f"{k}_is_set"] = bool(cfg.get(k))
    # typed conveniences
    public["smtp_use_tls_b"] = cfg["smtp_use_tls"] == "1"
    public["email_html_b"] = cfg["email_html"] == "1"
    public["notifications_paused_b"] = cfg["notifications_paused"] == "1"
    public["quiet_enabled_b"] = cfg["quiet_enabled"] == "1"
    public["oidc_enabled_b"] = cfg.get("oidc_enabled") == "1"
    public["oidc_auto_provision_b"] = cfg.get("oidc_auto_provision") == "1"
    public["allow_local_login_b"] = cfg.get("allow_local_login", "1") == "1"
    public["scrapedo_enabled_b"] = cfg.get("scrapedo_enabled") == "1"
    public["scrapedo_render_b"] = cfg.get("scrapedo_render") == "1"
    public["scrapedo_super_b"] = cfg.get("scrapedo_super") == "1"
    return public


def scrapedo_settings(db: Session) -> dict:
    """Effective scrape.do config, typed, for the fetch engine.

    DB-stored values (set on the Settings page) win over the ``.env`` seed
    defaults. ``active`` is the gate the engine uses: a token must be present and
    the toggle on.
    """
    cfg = get_config(db)

    def _num(key, fallback, cast):
        try:
            return cast(cfg.get(key) or fallback)
        except (TypeError, ValueError):
            return fallback

    token = cfg.get("scrapedo_token", "")
    enabled = cfg.get("scrapedo_enabled") == "1"
    return {
        "token": token,
        "enabled": enabled,
        "active": bool(enabled and token),
        "render": cfg.get("scrapedo_render") == "1",
        "super": cfg.get("scrapedo_super") == "1",
        "geo": (cfg.get("scrapedo_geo") or "").strip(),
        "timeout": _num("scrapedo_timeout_seconds", env.scrapedo_timeout_seconds, float),
        "monthly_credits": _num("scrapedo_monthly_credits", env.scrapedo_monthly_credits, int),
    }


def set_values(db: Session, values: dict[str, str | None], *, keep_blank_secrets: bool = True) -> None:
    """Persist a batch of settings. Blank secret values are skipped (kept) when
    ``keep_blank_secrets`` so the UI never has to re-enter a password to save."""
    existing = {s.key: s for s in db.execute(select(Setting)).scalars().all()}
    for key, val in values.items():
        if key in SECRET_KEYS:
            if not val and keep_blank_secrets:
                continue
            val = _encrypt(val or "")
        if key in existing:
            existing[key].value = val
        else:
            db.add(Setting(key=key, value=val))
    db.commit()


# ---- templates + quiet hours ----

def render_template(tpl: str, ctx: dict) -> str:
    out = tpl or ""
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def sample_context() -> dict:
    return {
        "product_name": "NVIDIA GeForce RTX 5080",
        "store_name": "store-a.example.com",
        "current_price": "$1,049.99",
        "old_price": "$1,199.99",
        "change_amount": "$150.00",
        "percent_change": "12.5%",
        "direction": "dropped",
        "target_price": "$999.00",
        "datetime": datetime.now().strftime("%b %d, %Y %H:%M"),
        "currency": "USD",
        "url": "https://store-a.example.com/rtx-5080",
    }


_HHMM = re.compile(r"^\d{1,2}:\d{2}$")


def in_quiet_hours(cfg: dict, now: datetime | None = None) -> bool:
    if cfg.get("quiet_enabled") != "1":
        return False
    start, end = cfg.get("quiet_start", ""), cfg.get("quiet_end", "")
    if not (_HHMM.match(start) and _HHMM.match(end)):
        return False
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    sh, sm = map(int, start.split(":")); eh, em = map(int, end.split(":"))
    s, e = sh * 60 + sm, eh * 60 + em
    if s == e:
        return False
    return (s <= cur < e) if s < e else (cur >= s or cur < e)


def should_send(cfg: dict, now: datetime | None = None) -> tuple[bool, str]:
    """Gate used by the (future) alert engine; test sends bypass this."""
    if cfg.get("notifications_paused") == "1":
        return False, "All notifications are paused."
    if in_quiet_hours(cfg, now):
        return False, "Within quiet hours."
    return True, ""


# ---- general options: choices for the Settings page ----

CURRENCIES = [
    ("USD", "US Dollar ($)"), ("CAD", "Canadian Dollar ($)"), ("EUR", "Euro (€)"),
    ("GBP", "British Pound (£)"), ("PLN", "Polish Zloty (zł)"), ("AUD", "Australian Dollar ($)"),
    ("JPY", "Japanese Yen (¥)"), ("CHF", "Swiss Franc (Fr)"), ("SEK", "Swedish Krona (kr)"),
    ("NOK", "Norwegian Krone (kr)"), ("INR", "Indian Rupee (₹)"), ("BRL", "Brazilian Real (R$)"),
]

DATE_FORMATS = [
    ("%b %d, %Y", "Jun 13, 2026"), ("%Y-%m-%d", "2026-06-13"),
    ("%d/%m/%Y", "13/06/2026"), ("%m/%d/%Y", "06/13/2026"), ("%d %b %Y", "13 Jun 2026"),
]

COMMON_TIMEZONES = [
    "UTC", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Toronto", "America/Sao_Paulo", "Europe/London", "Europe/Paris", "Europe/Berlin",
    "Europe/Warsaw", "Africa/Johannesburg", "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore",
    "Asia/Tokyo", "Australia/Sydney", "Pacific/Auckland",
]

# Curated theme presets (accent / sidebar bg / topbar accent / link).
THEME_PRESETS = [
    {"name": "Faded Blue", "theme_accent": "#2f6df0", "theme_sidebar_bg": "", "theme_topbar_accent": "#2f6df0", "theme_link": "#2f6df0"},
    {"name": "Cosmic Violet", "theme_accent": "#6d5ae6", "theme_sidebar_bg": "", "theme_topbar_accent": "#6d5ae6", "theme_link": "#6d5ae6"},
    {"name": "Emerald", "theme_accent": "#0e9f6e", "theme_sidebar_bg": "", "theme_topbar_accent": "#0e9f6e", "theme_link": "#0e9f6e"},
    {"name": "Ocean", "theme_accent": "#0ea5e9", "theme_sidebar_bg": "", "theme_topbar_accent": "#0284c7", "theme_link": "#0ea5e9"},
    {"name": "Sunset", "theme_accent": "#f0623a", "theme_sidebar_bg": "", "theme_topbar_accent": "#f0623a", "theme_link": "#e5484d"},
    {"name": "Graphite", "theme_accent": "#5b6472", "theme_sidebar_bg": "", "theme_topbar_accent": "#5b6472", "theme_link": "#5b6472"},
]

THEME_KEYS = ["theme_accent", "theme_sidebar_bg", "theme_topbar_accent", "theme_link"]


def theme_css(cfg: dict) -> str:
    """Build a CSS custom-property override string from the configured theme.

    Only emits properties the user actually set, so untouched slots keep their
    per-mode (light/dark) defaults. Accent also derives hover/weak via color-mix.
    """
    parts: list[str] = []
    accent = (cfg.get("theme_accent") or "").strip()
    if accent:
        parts.append(f"--accent:{accent}")
        parts.append(f"--accent-hover:color-mix(in srgb, {accent} 84%, #000)")
        parts.append(f"--accent-weak:color-mix(in srgb, {accent} 16%, var(--surface))")
    if (sb := (cfg.get("theme_sidebar_bg") or "").strip()):
        parts.append(f"--sidebar-bg:{sb}")
    if (tb := (cfg.get("theme_topbar_accent") or "").strip()):
        parts.append(f"--topbar-accent:{tb}")
    if (lk := (cfg.get("theme_link") or "").strip()):
        parts.append(f"--link:{lk}")
    return ";".join(parts)


# ---- backup / restore ----

# Keys that are not user backup data (derived/runtime) — excluded from export.
_NON_BACKUP = {"worker_heartbeat_at"}


def export_all(db: Session) -> dict:
    """All non-secret settings as a plain dict for JSON backup.

    Secrets (passwords, API key, bot token) are intentionally omitted and must be
    re-entered after a restore.
    """
    cfg = get_config(db)
    return {k: v for k, v in cfg.items() if k not in SECRET_KEYS and k not in _NON_BACKUP}


def import_values(db: Session, data: dict) -> int:
    """Restore settings from a backup dict. Unknown and secret keys are skipped."""
    allowed = set(_defaults().keys()) - SECRET_KEYS
    clean = {k: ("" if v is None else str(v)) for k, v in data.items()
             if k in allowed and k not in _NON_BACKUP}
    if clean:
        set_values(db, clean)
    return len(clean)
