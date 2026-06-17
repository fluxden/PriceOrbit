"""Build the per-product summary rows shown on the overview pages.

Each product can have several store listings (ProductURLs). We collapse those
into a single row: the lowest current in-stock price and the store offering it,
an overall in-stock flag, a "change since added" figure, a "back in stock" flag
derived from recent history, plus schedule / target / import details used by the
Price Tracking page.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import PriceHistory, Product, ProductURL
from app.services import health

_CURRENCY_SYMBOLS = {
    "USD": "$", "CAD": "$", "AUD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹",
}

_INTERVAL_LABELS = {1: "1m", 5: "5m", 15: "15m", 60: "1h", 360: "6h", 720: "12h", 1440: "1d"}


def format_price(price: Decimal | None, currency: str | None) -> str:
    if price is None:
        return "—"
    symbol = _CURRENCY_SYMBOLS.get((currency or "").upper())
    amount = f"{price:,.2f}"
    if symbol:
        return f"{symbol}{amount}"
    return f"{currency} {amount}" if currency else amount


def format_clock(value, time_format: str = "24") -> str:
    """Render a time as "HH:MM" (24h) or "h:MM AM/PM" (12h).

    Accepts an "HH:MM" string or anything with .hour/.minute (e.g. a datetime),
    so the same helper drives schedule labels and template time output. Honours
    the 12/24-hour preference from the Settings page.
    """
    if value is None or value == "":
        return ""
    if hasattr(value, "hour"):
        h, m = value.hour, value.minute
    else:
        try:
            h, m = (int(x) for x in str(value).split(":")[:2])
        except (TypeError, ValueError):
            return str(value)
    if str(time_format) == "12":
        suffix = "AM" if h < 12 else "PM"
        return f"{(h % 12) or 12}:{m:02d} {suffix}"
    return f"{h:02d}:{m:02d}"


def schedule_display(product: Product, time_format: str = "24") -> str:
    if product.schedule_kind == "daily" and product.daily_check_time:
        return f"Daily {format_clock(product.daily_check_time, time_format)}"
    mins = product.check_interval_minutes
    if not mins:
        return "Not scheduled"
    return f"Every {_INTERVAL_LABELS.get(mins, f'{mins}m')}"


@dataclass
class OverviewRow:
    id: int
    name: str
    model_number: str | None
    image_url: str | None
    is_favorite: bool
    created_at: datetime
    track_price: bool = True
    track_stock: bool = False
    import_status: str = "imported"
    tags: list[dict] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    best_price: Decimal | None = None
    price_display: str = "—"
    currency: str | None = None
    best_store: str | None = None
    store_count: int = 0
    in_stock: bool = False
    recently_restocked: bool = False
    change_pct: float | None = None
    change_dir: str = "none"  # down | up | flat | none
    paused: bool = False
    schedule: str = ""
    last_checked: datetime | None = None
    target_price: Decimal | None = None
    target_display: str | None = None
    to_target_pct: float | None = None  # how far current sits above target (%)
    status: str = "ok"   # monitoring health: ok | stale | failing | paused | off | new
    failing: bool = False
    stale: bool = False


def _winning_url(urls: list[ProductURL]) -> ProductURL | None:
    pool = [u for u in urls if u.active] or list(urls)
    priced = [u for u in pool if u.last_price is not None]
    if not priced:
        return None
    in_stock = [u for u in priced if u.last_in_stock]
    candidates = in_stock or priced
    return min(candidates, key=lambda u: u.last_price)


def _recently_restocked(db: Session, url: ProductURL) -> bool:
    states = db.execute(
        select(PriceHistory.in_stock)
        .where(PriceHistory.product_url_id == url.id)
        .order_by(PriceHistory.checked_at.desc())
        .limit(2)
    ).scalars().all()
    return len(states) >= 2 and bool(states[0]) and not bool(states[1])


def build_rows(db: Session, monitor: str | None = None, owner_id: int | None = None,
               time_format: str = "24") -> list[OverviewRow]:
    """monitor: None (all), 'price' (track_price), or 'stock' (track_stock)."""
    now = datetime.utcnow()
    products = (
        db.execute(
            select(Product).options(
                selectinload(Product.urls), selectinload(Product.tags)
            )
        )
        .scalars()
        .unique()
        .all()
    )

    rows: list[OverviewRow] = []
    for p in products:
        if owner_id is not None and p.user_id != owner_id:
            continue
        if monitor == "price" and not p.track_price:
            continue
        if monitor == "stock" and not p.track_stock:
            continue

        win = _winning_url(p.urls)
        in_stock = any(bool(u.last_in_stock) for u in p.urls)
        paused = bool(p.urls) and all(not u.active for u in p.urls)
        checks = [u.last_checked_at for u in p.urls if u.last_checked_at]
        last_checked = max(checks) if checks else None

        active_urls = [u for u in p.urls if u.active]
        cf = max((u.consecutive_failures or 0) for u in active_urls) if active_urls else 0
        attempts = [u.last_attempt_at for u in p.urls if u.last_attempt_at]
        last_attempt = max(attempts) if attempts else None
        hs = health.classify(
            now, monitored=bool(p.track_price or p.track_stock),
            has_active_url=bool(active_urls), paused=paused,
            last_checked=last_checked, last_attempt=last_attempt, consecutive_failures=cf,
            kind=p.schedule_kind, interval_minutes=p.check_interval_minutes,
            daily_time=p.daily_check_time, default_interval=settings.default_check_interval_minutes)

        change_pct = None
        change_dir = "none"
        if win and win.last_price is not None and win.baseline_price:
            base = win.baseline_price
            if base and base != 0:
                change_pct = float((win.last_price - base) / base * Decimal(100))
                change_dir = "down" if change_pct < -0.05 else "up" if change_pct > 0.05 else "flat"

        restocked = bool(win and win.last_in_stock and _recently_restocked(db, win))

        to_target_pct = None
        if p.target_price and win and win.last_price is not None and p.target_price != 0:
            to_target_pct = float((win.last_price - p.target_price) / p.target_price * Decimal(100))

        rows.append(
            OverviewRow(
                id=p.id,
                name=p.name,
                model_number=p.model_number,
                image_url=p.image_url,
                is_favorite=p.is_favorite,
                created_at=p.created_at,
                track_price=p.track_price,
                track_stock=p.track_stock,
                import_status=p.import_status,
                tags=[{"name": t.name, "color": t.color} for t in p.tags],
                domains=sorted({u.domain for u in p.urls if u.domain}),
                best_price=win.last_price if win else None,
                price_display=format_price(
                    win.last_price if win else None, win.currency if win else None
                ),
                currency=win.currency if win else None,
                best_store=(win.store_name or win.domain) if win else None,
                store_count=len(p.urls),
                in_stock=in_stock,
                recently_restocked=restocked,
                change_pct=change_pct,
                change_dir=change_dir,
                paused=paused,
                schedule=schedule_display(p, time_format),
                last_checked=last_checked,
                target_price=p.target_price,
                target_display=format_price(p.target_price, win.currency if win else None)
                if p.target_price
                else None,
                to_target_pct=to_target_pct,
                status=hs.status,
                failing=hs.failing,
                stale=hs.stale,
            )
        )
    return rows
