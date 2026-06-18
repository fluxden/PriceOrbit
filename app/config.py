"""Application configuration loaded from environment variables / .env."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Application
    app_name: str = "PriceOrbit"
    app_version: str = "0.5.0"
    app_secret: str = "change-me"
    timezone: str = "UTC"
    uploads_dir: str = "/data/uploads"
    # Worker scheduling
    default_check_interval_minutes: int = 60
    scheduler_reconcile_minutes: int = 2
    check_jitter_seconds: int = 30

    # Database
    db_driver: str = "mysql+pymysql"
    db_host: str = "db"
    db_port: int = 3306
    db_name: str = "priceorbit"
    db_user: str = "priceorbit"
    db_password: str = "priceorbit"

    # Monitoring / polite scraping.
    # A real browser User-Agent is the default: most stores reject unknown
    # bot UAs (403 / empty challenge pages). Override via the USER_AGENT env var.
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    # Browser profile curl_cffi impersonates (TLS/JA3 + HTTP2 fingerprint).
    # See curl_cffi docs for valid values (e.g. "chrome", "chrome124", "safari").
    impersonate_profile: str = "chrome"
    # Honour robots.txt. Off by default: this is a self-hosted monitor fetching
    # user-chosen product URLs (not a crawler), and many stores' robots.txt would
    # otherwise block the exact pages the user wants to track. The per-domain
    # rate limit below still applies. Set RESPECT_ROBOTS=1 to re-enable.
    respect_robots: bool = False

    # Scrape.do fallback engine (optional, paid). Only used when the free engines
    # (curl_cffi, httpx) are blocked — so credits are spent only on hard,
    # anti-bot-protected stores (e.g. Akamai/Home Depot). Disabled when the token
    # is empty. Get a token at https://scrape.do (free tier available).
    scrapedo_token: str = ""
    # Allow escalating to a headless JS render. scrape.do is tried no-render first
    # (cheaper, and enough for anti-bot stores whose static HTML carries the price,
    # e.g. Best Buy); render is used only when that no-render fetch finds no price.
    scrapedo_render: bool = True
    scrapedo_super: bool = True        # residential/mobile proxies (needed for Akamai)
    scrapedo_geo: str = "US"           # geoCode; blank to let scrape.do choose
    scrapedo_timeout_seconds: float = 70.0  # render + residential is slow
    # Assumed monthly credit allowance, used for the usage meter on the Settings
    # page. scrape.do's /info API only reports real numbers for paid plans, so for
    # the free tier (1,000 credits/mo) usage is tracked locally against this.
    scrapedo_monthly_credits: int = 1000
    fetch_timeout_seconds: float = 15.0
    min_check_interval_minutes: int = 1      # floor enforced on schedules
    per_domain_min_seconds: int = 20         # politeness: gap between hits per host
    fetch_jitter_seconds: int = 20           # randomized delay added to checks

    # Optional sign-in override (recovery / anti-lockout). When set, it forces the
    # login mode regardless of what's stored in the database — so a misconfigured
    # OIDC can never lock you out: set LOGIN_TYPE=OFF (or Standard) in the
    # environment and redeploy to get back in. Leave empty to use the Admin UI
    # settings. Case-insensitive.
    #   OFF      → no sign-in required — BUT only while no admin account exists.
    #              Once an admin is created, OFF is upgraded to Standard so auth
    #              can't be bypassed by setting the env var. It still disables a
    #              broken OIDC (falls back to local login), which is the recovery.
    #   Standard → local username/password only (OIDC disabled)
    #   OIDC     → OIDC enabled (local login kept enabled as a fallback)
    login_type: str = ""

    # Logging. Initial level (also editable at runtime in Admin → Logs, which
    # persists to the database and overrides this). Both the web and worker
    # processes write to log_file so the Logs page can show their combined output.
    log_level: str = "info"   # fatal | error | warn | info | debug | trace
    log_file: str = "/data/app.log"

    # Notifications (optional; configured later via the Alerts page / env)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    telegram_bot_token: str = ""

    @property
    def database_url(self) -> str:
        return (
            f"{self.db_driver}://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
