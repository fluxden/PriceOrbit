"""Alert delivery — send the decisions from the evaluator and record them.

Renders each firing rule's template, sends it through the configured account
(with fallback) or queues an in-app browser-sound notification, writes a
``notification_log`` row, and advances the rule's cooldown state. Honours the
global pause / quiet-hours mute (muted alerts are logged, not sent).
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from app.models import AlertAccount, AlertChannel, AlertRule, AlertType, NotificationLog
from app.services import alerts_engine, notify, settings_store, timefmt
from app.services.overview import format_clock

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models import Product
    from app.services.checker import ProductCheck

_SYMBOLS = {
    "USD": "$", "CAD": "$", "AUD": "$", "NZD": "$", "EUR": "€", "GBP": "£",
    "JPY": "¥", "CNY": "¥", "INR": "₹", "BRL": "R$", "CHF": "Fr", "SEK": "kr",
    "NOK": "kr", "DKK": "kr", "PLN": "zł",
}


def _money(value, currency) -> str:
    if value is None:
        return ""
    sym = _SYMBOLS.get((currency or "").upper())
    return f"{sym}{value:,.2f}" if sym else f"{value:,.2f} {currency or ''}".strip()


def format_now(cfg: dict, now: datetime | None = None) -> str:
    """The ``{datetime}`` placeholder value: a naive-UTC instant rendered in the
    configured timezone with the Settings date format + 12/24-hour preference.
    Shared by real sends and the Alerts-page template preview so they match."""
    now = now or datetime.utcnow()
    local = timefmt.to_zone(now, timefmt.resolve_tz(cfg.get("timezone", "UTC")))
    date_fmt = cfg.get("date_format") or "%b %d, %Y"
    return f"{local.strftime(date_fmt)} {format_clock(local, cfg.get('time_format', '24'))}"


def _format_context(ctx: dict, now: datetime, cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    cur = ctx.get("currency")
    out = dict(ctx)
    out["current_price"] = _money(ctx.get("current_price"), cur)
    out["old_price"] = _money(ctx.get("old_price"), cur)
    out["change_amount"] = _money(ctx.get("change_amount"), cur)
    out["target_price"] = _money(ctx.get("target_price"), cur)
    pc = ctx.get("percent_change")
    out["percent_change"] = f"{pc:.1f}%" if pc is not None else ""
    out["datetime"] = format_now(cfg, now)
    return {k: ("" if v is None else v) for k, v in out.items()}


def _render(cfg: dict, decision, now: datetime) -> tuple[str, str]:
    is_stock = decision.type == AlertType.BACK_IN_STOCK
    subj_key = "tpl_stock_subject" if is_stock else "tpl_price_subject"
    body_key = "tpl_stock_body" if is_stock else "tpl_price_body"
    fmt = _format_context(decision.context, now, cfg)
    subject = settings_store.render_template(cfg.get(subj_key, ""), fmt)
    body = settings_store.render_template(cfg.get(body_key, ""), fmt)
    return subject, body


def _log(db, rule, channel, subject, message, *, success, error, seen) -> None:
    db.add(NotificationLog(
        alert_rule_id=rule.id, channel=channel, subject=subject[:512] if subject else subject,
        message=message, success=success, error=error, seen=seen,
    ))


def _mark_fired(rule, decision, now: datetime) -> None:
    rule.last_triggered_at = now
    price = decision.context.get("current_price")
    if price is not None:
        rule.last_notified_price = price


def deliver(db: "Session", product: "Product", check: "ProductCheck", *,
            cfg: dict | None = None, now: datetime | None = None) -> int:
    """Evaluate the product against ``check`` and deliver any firing alerts.

    Returns the number of alerts actually sent/queued (muted/failed excluded).
    """
    now = now or datetime.utcnow()
    decisions = alerts_engine.evaluate_product(product, check, now=now)
    if not decisions:
        return 0
    cfg = cfg or settings_store.get_config(db)
    muted = alerts_engine.notifications_muted(cfg, now)

    sent = 0
    for d in decisions:
        rule = db.get(AlertRule, d.rule_id)
        if rule is None:
            continue
        subject, body = _render(cfg, d, now)

        if muted:
            _log(db, rule, d.channel, subject, body, success=False,
                 error=f"Muted: {muted}", seen=True)
            continue

        if d.channel == AlertChannel.SOUND:
            # Queued for the browser to pick up and play; no external send.
            _log(db, rule, AlertChannel.SOUND, subject, body, success=True, error=None, seen=False)
            _mark_fired(rule, d, now)
            sent += 1
            continue

        account = db.get(AlertAccount, d.account_id) if d.account_id else None
        if account is None or not account.enabled:
            _log(db, rule, d.channel, subject, body, success=False,
                 error="No usable destination for this alert", seen=True)
            continue
        fallback = (db.get(AlertAccount, account.fallback_account_id)
                    if account.fallback_account_id else None)
        ok, msg, used = notify.send_with_fallback(cfg, account, fallback, subject, body)
        _log(db, rule, used.channel, subject, body, success=ok,
             error=None if ok else msg, seen=True)
        if ok:
            _mark_fired(rule, d, now)
            sent += 1

    db.commit()
    return sent
