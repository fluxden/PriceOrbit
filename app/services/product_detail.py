"""Assemble everything the product detail page needs in one place.

Collapses a product's listings and price history into: per-store chart series,
price statistics, a deal indicator, the Tracked Stores rows, alert rules with
human labels, and recent alert history.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    AlertAccount,
    AlertRule,
    AlertType,
    NotificationLog,
    PriceHistory,
    Product,
    ProductURL,
)
from app.services.overview import _INTERVAL_LABELS, format_clock, format_price
from app.config import settings
from app.services import health, timefmt

# A small, theme-friendly palette for chart lines (cycles if more stores).
SERIES_COLORS = ["#2f6df0", "#0ea5e9", "#0e9f6e", "#7c5cff", "#e5774d", "#14b8a6"]

_TRIGGER_LABELS = {
    AlertType.PRICE_DROP_ANY: "Any price drop",
    AlertType.PRICE_DROP_AMOUNT: "Drops by amount",
    AlertType.PRICE_DROP_PERCENT: "Drops by percent",
    AlertType.PRICE_BELOW_TARGET: "At or below target",
    AlertType.PRICE_INCREASE_ANY: "Any price increase",
    AlertType.PRICE_INCREASE_AMOUNT: "Increases by amount",
    AlertType.PRICE_INCREASE_PERCENT: "Increases by percent",
    AlertType.BACK_IN_STOCK: "Back in stock",
}


def store_schedule_display(url: ProductURL, product: Product, time_format: str = "24") -> tuple[str, bool]:
    """Returns (label, is_override). Inherits the product schedule when unset."""
    if url.schedule_kind == "daily" and url.daily_check_time:
        return f"Daily {format_clock(url.daily_check_time, time_format)}", True
    if url.schedule_kind == "interval":
        return f"Every {_INTERVAL_LABELS.get(url.check_interval_minutes, f'{url.check_interval_minutes}m')}", True
    return "Default", False


def _trigger_threshold_display(rule: AlertRule, currency: str | None) -> str:
    if rule.type in (AlertType.PRICE_DROP_PERCENT, AlertType.PRICE_INCREASE_PERCENT):
        return f"{rule.threshold:g}%" if rule.threshold is not None else "—"
    if rule.type in (AlertType.PRICE_DROP_AMOUNT, AlertType.PRICE_INCREASE_AMOUNT, AlertType.PRICE_BELOW_TARGET):
        return format_price(rule.threshold, currency) if rule.threshold is not None else "—"
    return "—"


@dataclass
class ProductDetail:
    product: Product
    in_stock: bool = False
    uses_api: bool = False  # an active listing's last check went through scrape.do (paid)
    currency: str | None = None
    mixed_currency: bool = False
    stores: list[dict] = field(default_factory=list)
    series: list[dict] = field(default_factory=list)
    oos_spans: list[list[str]] = field(default_factory=list)
    tz: str = "UTC"
    stats: dict = field(default_factory=dict)
    best_store: str | None = None
    target_price: Decimal | None = None
    target_value: float | None = None
    deal: dict = field(default_factory=dict)
    rules: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    accounts: list[AlertAccount] = field(default_factory=list)
    health: dict = field(default_factory=dict)


def build_detail(db: Session, product: Product, time_format: str = "24",
                 tz_name: str = "UTC") -> ProductDetail:
    urls = sorted(product.urls, key=lambda u: (not u.is_primary, u.id))
    detail = ProductDetail(product=product, tz=timefmt.tz_name(tz_name))

    # Display currency = primary store's, else the most common across listings.
    currencies = [u.currency for u in urls if u.currency]
    primary = next((u for u in urls if u.is_primary), urls[0] if urls else None)
    detail.currency = (primary.currency if primary and primary.currency else
                       (Counter(currencies).most_common(1)[0][0] if currencies else None))
    detail.mixed_currency = len(set(currencies)) > 1
    detail.in_stock = any(bool(u.last_in_stock) for u in urls)
    detail.uses_api = any(u.last_engine == "scrapedo" for u in urls if u.active)
    detail.target_price = product.target_price
    detail.target_value = float(product.target_price) if product.target_price is not None else None

    # Per-store chart series + Tracked Stores rows.
    all_prices: list[float] = []
    current_best: tuple[Decimal, str] | None = None
    for i, u in enumerate(urls):
        points = sorted(u.price_history, key=lambda h: h.checked_at)
        series_points = [
            {"t": timefmt.utc_iso(h.checked_at), "p": float(h.price), "s": bool(h.in_stock)}
            for h in points if h.price is not None
        ]
        if (not detail.mixed_currency) or (u.currency == detail.currency):
            all_prices.extend(pt["p"] for pt in series_points)
        sched_label, is_override = store_schedule_display(u, product, time_format)
        detail.series.append({
            "id": u.id, "store": u.store_name or u.domain or f"Store {i+1}",
            "color": SERIES_COLORS[i % len(SERIES_COLORS)], "points": series_points,
        })
        detail.stores.append({
            "id": u.id, "store": u.store_name or u.domain or f"Store {i+1}",
            "domain": u.domain, "url": u.url, "favicon_url": u.favicon_url,
            "color": SERIES_COLORS[i % len(SERIES_COLORS)],
            "is_primary": u.is_primary, "active": u.active, "currency": u.currency,
            "last_price": u.last_price, "last_price_display": format_price(u.last_price, u.currency),
            "last_in_stock": u.last_in_stock, "last_checked_at": u.last_checked_at,
            "schedule_label": sched_label, "schedule_override": is_override,
            "schedule_kind": u.schedule_kind or "", "check_interval_minutes": u.check_interval_minutes,
            "daily_check_time": u.daily_check_time or "",
            "point_count": len(series_points),
            "consecutive_failures": u.consecutive_failures or 0,
            "last_error": u.last_error,
            "last_attempt_at": u.last_attempt_at,
        })
        if u.last_price is not None and u.active and (current_best is None or u.last_price < current_best[0]):
            if u.last_in_stock or current_best is None:
                current_best = (u.last_price, u.store_name or u.domain or "")

    # Out-of-stock shading derived from the primary store's history.
    if primary:
        detail.oos_spans = _oos_spans(sorted(primary.price_history, key=lambda h: h.checked_at))

    # Price statistics (within the display currency).
    if all_prices:
        low, high = min(all_prices), max(all_prices)
        avg = sum(all_prices) / len(all_prices)
        current = float(current_best[0]) if current_best else all_prices[-1]
        detail.best_store = current_best[1] if current_best else None
        detail.stats = {
            "low": format_price(Decimal(str(low)), detail.currency),
            "high": format_price(Decimal(str(high)), detail.currency),
            "avg": format_price(Decimal(str(round(avg, 2))), detail.currency),
            "current": format_price(Decimal(str(current)), detail.currency),
            "points": len(all_prices),
        }
        # Deal indicator: how close the current best is to the historical low.
        if low > 0:
            pct_above = (current - low) / low * 100
            detail.deal = {
                "pct_above_low": round(pct_above, 1),
                "near_low": pct_above <= 3.0,
                "is_low": current <= low,
            }

    # Alert rules with friendly labels.
    for r in sorted(product.alert_rules, key=lambda r: r.id):
        detail.rules.append({
            "id": r.id, "type": r.type, "type_label": _TRIGGER_LABELS.get(r.type, r.type),
            "threshold_display": _trigger_threshold_display(r, detail.currency),
            "channel": r.channel, "enabled": r.enabled,
            "account_label": r.account.label if r.account else "(no account)",
        })

    # Recent alert history for this product (via its rules).
    rule_ids = [r.id for r in product.alert_rules]
    if rule_ids:
        logs = db.execute(
            select(NotificationLog)
            .where(NotificationLog.alert_rule_id.in_(rule_ids))
            .order_by(NotificationLog.created_at.desc())
            .limit(15)
        ).scalars().all()
        detail.history = [
            {"created_at": l.created_at, "channel": l.channel, "subject": l.subject,
             "success": l.success, "error": l.error}
            for l in logs
        ]

    detail.accounts = db.execute(
        select(AlertAccount).where(AlertAccount.enabled == True)  # noqa: E712
    ).scalars().all()

    # Monitoring health summary (mirrors the Price Tracking list classification).
    now = datetime.utcnow()
    active = [u for u in urls if u.active]
    checks = [u.last_checked_at for u in urls if u.last_checked_at]
    attempts = [u.last_attempt_at for u in urls if u.last_attempt_at]
    cf = max((u.consecutive_failures or 0) for u in active) if active else 0
    last_err = next((u.last_error for u in active if u.last_error), None)
    hs = health.classify(
        now, monitored=bool(product.track_price or product.track_stock),
        has_active_url=bool(active), paused=bool(urls) and not active,
        last_checked=max(checks) if checks else None,
        last_attempt=max(attempts) if attempts else None,
        consecutive_failures=cf, kind=product.schedule_kind,
        interval_minutes=product.check_interval_minutes,
        daily_time=product.daily_check_time,
        default_interval=settings.default_check_interval_minutes, last_error=last_err)
    detail.health = {
        "status": hs.status, "stale": hs.stale, "failing": hs.failing,
        "consecutive_failures": hs.consecutive_failures, "next_run": hs.next_run,
        "last_checked": hs.last_checked, "last_error": hs.last_error,
    }
    return detail


def _oos_spans(points: list[PriceHistory]) -> list[list[str]]:
    """Spans [start_iso, end_iso] where the store was out of stock."""
    spans: list[list[str]] = []
    start: datetime | None = None
    for h in points:
        if not h.in_stock and start is None:
            start = h.checked_at
        elif h.in_stock and start is not None:
            spans.append([timefmt.utc_iso(start), timefmt.utc_iso(h.checked_at)])
            start = None
    if start is not None and points:
        spans.append([timefmt.utc_iso(start), timefmt.utc_iso(points[-1].checked_at)])
    return spans


# (value, label, threshold_kind) for the "add alert rule" form.
# threshold_kind: "" (none) | "money" | "percent"
TRIGGER_CHOICES = [
    (AlertType.PRICE_DROP_ANY, "Price drops (any amount)", ""),
    (AlertType.PRICE_DROP_AMOUNT, "Price drops by at least ($)", "money"),
    (AlertType.PRICE_DROP_PERCENT, "Price drops by at least (%)", "percent"),
    (AlertType.PRICE_BELOW_TARGET, "Price at or below target ($)", "money"),
    (AlertType.PRICE_INCREASE_ANY, "Price increases (any amount)", ""),
    (AlertType.PRICE_INCREASE_PERCENT, "Price increases by at least (%)", "percent"),
    (AlertType.BACK_IN_STOCK, "Back in stock", ""),
]
MONEY_TRIGGERS = {AlertType.PRICE_DROP_AMOUNT, AlertType.PRICE_BELOW_TARGET}
PERCENT_TRIGGERS = {AlertType.PRICE_DROP_PERCENT, AlertType.PRICE_INCREASE_PERCENT}
