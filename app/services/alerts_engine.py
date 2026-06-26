"""Alert evaluation — decide which of a product's rules fire on a check result.

Pure decision logic: no sending and no DB writes (those are Part 4). Conditions
are edge-triggered against the product's best (lowest) current price and its
aggregate stock state, so a condition that simply *stays* true doesn't re-fire;
a per-rule cooldown guards against rapid oscillation. Global pause / quiet hours
are exposed via :func:`notifications_muted` for the delivery step to gate on.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.models import AlertType
from app.services import settings_store

if TYPE_CHECKING:
    from app.models import Product
    from app.services.checker import ProductCheck


@dataclass
class AlertDecision:
    rule_id: int
    type: str
    channel: str
    account_id: int | None
    reason: str
    context: dict


def notifications_muted(cfg: dict, now: datetime | None = None) -> str | None:
    """Return a reason string if notifications are globally muted, else None."""
    ok, reason = settings_store.should_send(cfg, now)
    return None if ok else reason


def _best(values):
    vals = [v for v in values if v is not None]
    return min(vals) if vals else None


def _aggregate(product: "Product", check: "ProductCheck"):
    """Collapse per-store before/after values into product-level state."""
    checked = {r.url_id: r for r in check.results}
    prev_prices, new_prices = [], []
    prev_stock, new_stock, prev_info = [], [], []
    best_store = first_active = None
    best_price = None
    for u in product.urls:
        if not u.active:
            continue
        if first_active is None:
            first_active = u
        new_prices.append(u.last_price)
        new_stock.append(u.last_in_stock)
        if u.last_price is not None and (best_price is None or u.last_price < best_price):
            best_price, best_store = u.last_price, u
        r = checked.get(u.id)
        if r is not None and r.recorded:
            prev_prices.append(r.prev_price)
            prev_stock.append(r.prev_in_stock)
            prev_info.append(r.prev_in_stock is not None)
        else:  # unchecked or failed listing — unchanged from its stored value
            prev_prices.append(u.last_price)
            prev_stock.append(u.last_in_stock)
            prev_info.append(u.last_in_stock is not None)
    return {
        "prev_best": _best(prev_prices),
        "new_best": _best(new_prices),
        "prev_in_stock": any(s is True for s in prev_stock),
        "new_in_stock": any(s is True for s in new_stock),
        "prev_had_stock_info": any(prev_info),
        "ref_store": best_store or first_active,
    }


def _condition(rule, a: dict) -> bool:
    t, thr = rule.type, rule.threshold
    prev, new = a["prev_best"], a["new_best"]

    if t == AlertType.BACK_IN_STOCK:
        return a["prev_had_stock_info"] and not a["prev_in_stock"] and a["new_in_stock"]
    if t == AlertType.OUT_OF_STOCK:
        return a["prev_had_stock_info"] and a["prev_in_stock"] and not a["new_in_stock"]
    if t == AlertType.STOCK_CHANGE_ANY:
        return a["prev_had_stock_info"] and a["prev_in_stock"] != a["new_in_stock"]

    if new is None:
        return False
    if t == AlertType.PRICE_BELOW_TARGET:
        # edge: crosses from above-target (or unknown) to at/below target
        return thr is not None and (prev is None or prev > thr) and new <= thr

    if prev is None:  # change-based rules need a prior price
        return False
    if t == AlertType.PRICE_DROP_ANY:
        return new < prev
    if t == AlertType.PRICE_DROP_AMOUNT:
        return thr is not None and (prev - new) >= thr
    if t == AlertType.PRICE_DROP_PERCENT:
        return thr is not None and prev > 0 and (prev - new) / prev * 100 >= thr
    if t == AlertType.PRICE_INCREASE_ANY:
        return new > prev
    if t == AlertType.PRICE_INCREASE_AMOUNT:
        return thr is not None and (new - prev) >= thr
    if t == AlertType.PRICE_INCREASE_PERCENT:
        return thr is not None and prev > 0 and (new - prev) / prev * 100 >= thr
    if t == AlertType.PRICE_CHANGE_ANY:
        return new != prev
    return False


def _cooled_down(rule, now: datetime) -> bool:
    if rule.last_triggered_at is None:
        return True
    return (now - rule.last_triggered_at) >= timedelta(minutes=rule.cooldown_minutes or 0)


_STOCK_TYPES = (AlertType.BACK_IN_STOCK, AlertType.OUT_OF_STOCK, AlertType.STOCK_CHANGE_ANY)


def _direction(rule, prev, new, *, in_stock: bool | None = None) -> str:
    if rule.type in _STOCK_TYPES:
        return "back in stock" if in_stock else "out of stock"
    if prev is not None and new is not None:
        if new < prev:
            return "dropped"
        if new > prev:
            return "rose"
    return ""


def _context(product, rule, a: dict) -> dict:
    prev, new = a["prev_best"], a["new_best"]
    store = a["ref_store"]
    change = (new - prev) if (prev is not None and new is not None) else None
    pct = float((new - prev) / prev * 100) if (prev not in (None, 0) and new is not None) else None
    return {
        "product_name": product.name,
        "store_name": getattr(store, "store_name", None) if store else None,
        "current_price": new,
        "old_price": prev,
        "change_amount": abs(change) if change is not None else None,
        "percent_change": abs(pct) if pct is not None else None,
        "direction": _direction(rule, prev, new, in_stock=a["new_in_stock"]),
        "target_price": rule.threshold if rule.type == AlertType.PRICE_BELOW_TARGET else None,
        "currency": getattr(store, "currency", None) if store else None,
        "url": getattr(store, "url", None) if store else None,
        "in_stock": a["new_in_stock"],
    }


def _reason(rule, a: dict) -> str:
    prev, new = a["prev_best"], a["new_best"]
    if rule.type == AlertType.BACK_IN_STOCK:
        return "Back in stock"
    if rule.type == AlertType.OUT_OF_STOCK:
        return "Out of stock"
    if rule.type == AlertType.STOCK_CHANGE_ANY:
        return "Back in stock" if a["new_in_stock"] else "Out of stock"
    if rule.type == AlertType.PRICE_BELOW_TARGET:
        return f"Price {new} at or below target {rule.threshold}"
    if prev is not None and new is not None:
        verb = "dropped" if new < prev else "rose"
        return f"Price {verb} from {prev} to {new}"
    return rule.type


def evaluate_product(product: "Product", check: "ProductCheck", *,
                     now: datetime | None = None) -> list[AlertDecision]:
    """Return a decision for each enabled rule whose condition edge-fired."""
    now = now or datetime.utcnow()
    a = _aggregate(product, check)
    decisions: list[AlertDecision] = []
    for rule in getattr(product, "alert_rules", []) or []:
        if not rule.enabled:
            continue
        if not _condition(rule, a):
            continue
        if not _cooled_down(rule, now):
            continue
        decisions.append(AlertDecision(
            rule_id=rule.id, type=rule.type, channel=rule.channel,
            account_id=rule.alert_account_id, reason=_reason(rule, a),
            context=_context(product, rule, a),
        ))
    return decisions
