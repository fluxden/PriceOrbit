"""Polite-monitoring helpers shared by the importer and the (later) scheduler.

Goal: never hammer a store. We enforce a per-domain minimum gap between hits,
add randomized jitter so checks don't fire in lockstep, and consult robots.txt.
The recurring scheduler (a later milestone) uses :func:`before_fetch` before
every scheduled check; the importer uses :func:`fetch` for one-off add-time
fetches.
"""
from __future__ import annotations

import importlib.util
import random
import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import httpx

from app.config import settings

_lock = threading.Lock()
_last_hit: dict[str, float] = {}
_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}

# Browser-like headers. Many stores (Akamai/Cloudflare-fronted) reject requests
# that don't look like a real browser, so we send the full set a browser would.
_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def _browser_headers() -> dict[str, str]:
    return {**_BROWSER_HEADERS, "User-Agent": settings.user_agent}


def domain_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def before_fetch(url: str) -> None:
    """Block until it's polite to hit this domain again (rate limit + jitter)."""
    domain = domain_of(url)
    with _lock:
        now = time.monotonic()
        wait = 0.0
        last = _last_hit.get(domain)
        if last is not None:
            elapsed = now - last
            if elapsed < settings.per_domain_min_seconds:
                wait = settings.per_domain_min_seconds - elapsed
        _last_hit[domain] = now + wait
    if wait > 0:
        time.sleep(wait)
    if settings.fetch_jitter_seconds > 0:
        time.sleep(random.uniform(0, settings.fetch_jitter_seconds))


# robots.txt should answer fast; if a host stalls it, fail open quickly instead
# of blocking the interactive import for the full fetch timeout.
_ROBOTS_TIMEOUT = 6.0


def allowed_by_robots(url: str) -> bool:
    """Best-effort robots.txt check; allow on any fetch/parse failure."""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    rp = _robots_cache.get(base)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        rp.allow_all = True  # default: allow unless robots.txt clearly forbids
        for engine in [e for e in engine_order() if e != "scrapedo"]:  # never spend credits on robots.txt
            try:
                resp = http_get(f"{base}/robots.txt", engine=engine, timeout=_ROBOTS_TIMEOUT)
            except Exception:  # noqa: BLE001 - try next engine, else stay allow_all
                continue
            if resp.status_code == 200:
                rp.allow_all = False
                rp.parse(resp.text.splitlines())
            break  # got a definitive (non-exception) answer
        _robots_cache[base] = rp
    try:
        return rp.can_fetch(settings.user_agent, url)
    except Exception:  # noqa: BLE001
        return True


def _curl_cffi_available() -> bool:
    return importlib.util.find_spec("curl_cffi") is not None


def _scrapedo_conf() -> dict:
    """Effective scrape.do config: DB-backed (Settings page) over env defaults.

    Read fresh per fetch so toggling the engine on the Settings page takes effect
    without a restart. Falls back to env if the DB is unavailable.
    """
    try:
        from app.database import SessionLocal
        from app.services import settings_store

        db = SessionLocal()
        try:
            return settings_store.scrapedo_settings(db)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - env fallback keeps fetches working
        token = settings.scrapedo_token
        return {
            "token": token, "enabled": bool(token), "active": bool(token),
            "render": settings.scrapedo_render, "super": settings.scrapedo_super,
            "geo": settings.scrapedo_geo, "timeout": settings.scrapedo_timeout_seconds,
            "monthly_credits": settings.scrapedo_monthly_credits,
        }


def scrapedo_active() -> bool:
    """Whether the paid scrape.do engine is configured and enabled."""
    return _scrapedo_conf()["active"]


# Engines tried, in order. "impersonate" (curl_cffi) sends a real browser
# TLS/HTTP2 fingerprint and clears most fingerprint-based anti-bot; plain
# "httpx" is the fallback (and handles the occasional site that rejects the
# impersonated fingerprint). "scrapedo" (paid API) is appended last when it's
# configured + enabled, so it only runs — and only costs credits — when the free
# engines are blocked (e.g. Akamai-protected stores).
def engine_order() -> list[str]:
    free = ["impersonate", "httpx"] if _curl_cffi_available() else ["httpx"]
    return free + ["scrapedo"] if scrapedo_active() else free


class FetchResult:
    """Minimal, engine-agnostic response (httpx and curl_cffi differ)."""

    __slots__ = ("status_code", "text")

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def http_get(url: str, *, engine: str = "httpx", timeout: float | None = None) -> FetchResult:
    """Single GET with the given engine and browser-like headers. No politeness."""
    if engine == "scrapedo":
        return _scrapedo_get(url, timeout)
    timeout = settings.fetch_timeout_seconds if timeout is None else timeout
    if engine == "impersonate":
        from curl_cffi import requests as creq  # lazy: keep import optional
        resp = creq.get(
            url, impersonate=settings.impersonate_profile or "chrome",
            timeout=timeout, allow_redirects=True,
        )
        return FetchResult(resp.status_code, resp.text)
    resp = httpx.get(
        url, timeout=timeout, headers=_browser_headers(), follow_redirects=True,
    )
    return FetchResult(resp.status_code, resp.text)


def _scrapedo_get(url: str, timeout: float | None) -> FetchResult:
    """Fetch via the scrape.do API (residential proxies + headless render)."""
    conf = _scrapedo_conf()
    params = {"token": conf["token"], "url": url}
    if conf["render"]:
        params["render"] = "true"
    if conf["super"]:
        params["super"] = "true"
    if conf["geo"]:
        params["geoCode"] = conf["geo"]
    resp = httpx.get(
        "https://api.scrape.do/",
        params=params,  # httpx URL-encodes the target url value
        timeout=conf["timeout"] if timeout is None else timeout,
        follow_redirects=True,
    )
    if resp.status_code < 400:  # scrape.do charges only for fulfilled requests
        _record_scrapedo_usage(_scrapedo_cost(conf))
    return FetchResult(resp.status_code, resp.text)


def _scrapedo_cost(conf: dict) -> int:
    """Estimated credits for one scrape.do call given the configured mode."""
    r, s = conf["render"], conf["super"]
    if r and s:
        return 25          # residential + render
    if s:
        return 10          # residential only
    if r:
        return 5           # render only
    return 1               # basic datacenter


_USAGE_KEYS = ("scrapedo_period", "scrapedo_credits", "scrapedo_requests")


def _read_usage_rows(db) -> dict[str, str]:
    """Raw scrape.do counter rows (get_config filters non-default keys)."""
    from sqlalchemy import select  # lazy: keep this low-level module import-light
    from app.models import Setting

    rows = db.execute(select(Setting).where(Setting.key.in_(_USAGE_KEYS))).scalars().all()
    return {r.key: (r.value or "") for r in rows}


def _record_scrapedo_usage(cost: int) -> None:
    """Accumulate this month's scrape.do credit/request usage (best-effort)."""
    try:
        import datetime as _dt

        from app.database import SessionLocal
        from app.services import settings_store

        period = _dt.datetime.utcnow().strftime("%Y-%m")
        db = SessionLocal()
        try:
            rows = _read_usage_rows(db)
            if rows.get("scrapedo_period") == period:
                credits = int(rows.get("scrapedo_credits") or 0) + cost
                reqs = int(rows.get("scrapedo_requests") or 0) + 1
            else:  # new month → reset
                credits, reqs = cost, 1
            settings_store.set_values(db, {
                "scrapedo_period": period,
                "scrapedo_credits": str(credits),
                "scrapedo_requests": str(reqs),
            })
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - never break a fetch over usage telemetry
        pass


# scrape.do account usage, lightly cached (the /info endpoint is free and doesn't
# touch credits/concurrency, but is rate-limited to 10/min).
_usage_cache: tuple[float, dict] | None = None
_USAGE_TTL = 60.0


def _scrapedo_info_api(token: str) -> dict:
    """Live /info lookup (cached). Free-tier tokens report zeros here."""
    global _usage_cache
    now = time.monotonic()
    if _usage_cache and (now - _usage_cache[0]) < _USAGE_TTL:
        return _usage_cache[1]
    data: dict = {}
    try:
        resp = httpx.get("https://api.scrape.do/info",
                         params={"token": token}, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json() or {}
    except Exception:  # noqa: BLE001 - best-effort
        data = {}
    _usage_cache = (now, data)
    return data


def _scrapedo_tracked() -> tuple[int, int]:
    """(credits, requests) used this month, from local tracking. (0, 0) if none."""
    try:
        import datetime as _dt

        from app.database import SessionLocal

        period = _dt.datetime.utcnow().strftime("%Y-%m")
        db = SessionLocal()
        try:
            rows = _read_usage_rows(db)
        finally:
            db.close()
        if rows.get("scrapedo_period") == period:
            return int(rows.get("scrapedo_credits") or 0), int(rows.get("scrapedo_requests") or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0, 0


def scrapedo_usage() -> dict | None:
    """Usage summary for the Settings page, or None when no token is configured.

    Shown whenever a token is saved (even if the engine is toggled off) so the
    credit meter stays visible. Prefers scrape.do's /info numbers when the account
    reports them (paid plans); otherwise falls back to locally-tracked, estimated
    usage (free tier, whose credits the API does not expose).
    """
    conf = _scrapedo_conf()
    if not conf["token"]:
        return None
    api = _scrapedo_info_api(conf["token"])
    max_monthly = api.get("MaxMonthlyRequest") or 0
    out = {
        "ok": True,
        "active": conf["active"],
        "enabled": conf["enabled"],
        "account_active": bool(api.get("IsActive")),
        "concurrent": api.get("ConcurrentRequest") or None,
        "remaining_concurrent": api.get("RemainingConcurrentRequest"),
    }
    if isinstance(max_monthly, int) and max_monthly > 0:
        rem = api.get("RemainingMonthlyRequest")
        used = max_monthly - rem if isinstance(rem, int) else None
        out.update({
            "source": "api", "limit": max_monthly, "remaining": rem, "used": used,
            "used_pct": round(used / max_monthly * 100, 1) if used is not None else None,
            "requests": None,
        })
    else:  # free tier — use local estimate
        used, reqs = _scrapedo_tracked()
        limit = conf["monthly_credits"] or 0
        out.update({
            "source": "local", "limit": limit, "used": used, "requests": reqs,
            "remaining": max(limit - used, 0) if limit else None,
            "used_pct": round(used / limit * 100, 1) if limit else None,
        })
    return out
