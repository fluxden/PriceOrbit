"""Web (HTML) routes for PriceOrbit."""
from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import os
import secrets

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.models import (
    AlertAccount,
    AlertChannel,
    AlertRule,
    AlertType,
    ImportStatus,
    NotificationLog,
    PriceHistory,
    Product,
    ProductURL,
    ScheduleKind,
    Tag,
    User,
)
from app.services import alerting, audit, auth, checker, health, notify, oidc, politeness, schedule, settings_store
from app.services.importer import ProductMetadata, import_from_url
from app.services.overview import build_rows, format_price, schedule_display
from app.services.product_detail import (
    MONEY_TRIGGERS,
    PERCENT_TRIGGERS,
    TRIGGER_CHOICES,
    build_detail,
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
router = APIRouter()

PAGE_SIZE = 24
SORT_OPTIONS = {
    "recent": "Recently added",
    "name": "Name (A–Z)",
    "price_asc": "Price (low to high)",
    "price_desc": "Price (high to low)",
    "drop": "Biggest drop",
}
FREQUENCY_CHOICES = [
    ("1", "Every 1 minute"), ("5", "Every 5 minutes"), ("15", "Every 15 minutes"),
    ("60", "Every hour"), ("360", "Every 6 hours"), ("720", "Every 12 hours"),
    ("1440", "Every day"), ("daily", "Daily at a set time"), ("custom", "Custom interval"),
]
CONDITION_CHOICES = [
    ("any_drop", "Any price drop"),
    ("below_target", "Price below target"),
    ("drop_percent", "Price drops by ≥ X%"),
    ("back_in_stock", "Back in stock"),
    ("none", "No alert for now"),
]
NAV_ITEMS = [
    ("home", "Home", "/"),
    ("price", "Price Tracking", "/price-tracking"),
    ("stock", "In-Stock Monitor", "/in-stock"),
    ("alerts", "Alerts", "/alerts"),
    ("settings", "Settings", "/settings"),
    ("admin", "Admin", "/admin"),
]


def _base_context(request: Request, active: str) -> dict:
    theme_style, theme_base, cur_user, login_on = "", "", None, False
    try:
        from app.database import SessionLocal
        _db = SessionLocal()
        try:
            _cfg = settings_store.get_public(_db)
            theme_style = settings_store.theme_css(_cfg)
            theme_base = _cfg.get("theme_base", "") or ""
            login_on = _cfg.get("login_enabled", "0") == "1"
            cur_user = auth.current_user(request, _db)
        finally:
            _db.close()
    except Exception:  # noqa: BLE001 — cosmetic/auth context must never block a page
        pass
    nav = NAV_ITEMS
    if login_on and cur_user is not None and cur_user.role != "admin":
        nav = [item for item in NAV_ITEMS if item[0] in ("home", "price", "stock")]
        nav = nav + [("profile", "Profile", "/profile")]
    elif login_on and cur_user is not None:
        nav = NAV_ITEMS + [("profile", "Profile", "/profile")]
    return {"request": request, "app_name": settings.app_name, "active": active,
            "nav_items": nav, "theme_style": theme_style, "theme_base": theme_base,
            "current_user": cur_user, "login_enabled": login_on}


def _sorted(rows: list, sort: str) -> list:
    if sort == "name":
        return sorted(rows, key=lambda r: r.name.casefold())
    if sort == "price_asc":
        return sorted(rows, key=lambda r: (r.best_price is None, r.best_price or 0))
    if sort == "price_desc":
        return sorted(rows, key=lambda r: (r.best_price is None, -(r.best_price or 0)))
    if sort == "drop":
        return sorted(rows, key=lambda r: (r.change_pct is None, r.change_pct or 0.0))
    return sorted(rows, key=lambda r: r.created_at, reverse=True)


def _scope_owner(request, db) -> int | None:
    """The user id to scope products to, or None for single-user (login disabled)."""
    try:
        if auth.login_enabled(db):
            u = auth.current_user(request, db)
            return u.id if u else -1   # -1 matches nothing (middleware should prevent this)
    except Exception:  # noqa: BLE001
        pass
    return None


def _client_ip(request) -> str | None:
    try:
        return request.client.host if request and request.client else None
    except Exception:  # noqa: BLE001
        return None


def _audit(request, db, action: str, detail: str | None = None) -> None:
    actor = "anonymous"
    try:
        u = auth.current_user(request, db)
        if u:
            actor = u.username
    except Exception:  # noqa: BLE001
        pass
    audit.log(db, action, actor=actor, detail=detail, ip=_client_ip(request))


def _list_context(request, db, *, active, monitor, tab, q, sort, store, tag, instock, page):
    tab = tab if tab in ("overview", "favorites") else "overview"
    sort = sort if sort in SORT_OPTIONS else "recent"

    all_rows = build_rows(db, monitor, owner_id=_scope_owner(request, db))
    store_options = sorted({d for r in all_rows for d in r.domains})
    tag_options = sorted({t["name"] for r in all_rows for t in r.tags})
    kpis = {
        "tracked": len(all_rows),
        "in_stock": sum(1 for r in all_rows if r.in_stock),
        "out_stock": sum(1 for r in all_rows if not r.in_stock),
        "price_down": sum(1 for r in all_rows if r.change_dir == "down"),
        "at_target": sum(1 for r in all_rows
                         if r.target_price and r.to_target_pct is not None and r.to_target_pct <= 0),
        "attention": sum(1 for r in all_rows if r.status in ("failing", "stale", "new")),
    }

    rows = all_rows
    if tab == "favorites":
        rows = [r for r in rows if r.is_favorite]
    if q:
        n = q.casefold()
        rows = [r for r in rows if n in r.name.casefold() or (r.model_number and n in r.model_number.casefold())]
    if store:
        rows = [r for r in rows if store in r.domains]
    if tag:
        rows = [r for r in rows if any(t["name"] == tag for t in r.tags)]
    if instock == "1":
        rows = [r for r in rows if r.in_stock]

    rows = _sorted(rows, sort)
    total = len(rows)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(page, 1), total_pages)
    page_rows = rows[(page - 1) * PAGE_SIZE : (page - 1) * PAGE_SIZE + PAGE_SIZE]

    qs = {"tab": tab, "sort": sort}
    for k, v in (("q", q), ("store", store), ("tag", tag)):
        if v:
            qs[k] = v
    if instock == "1":
        qs["instock"] = "1"

    ctx = _base_context(request, active)
    ctx.update({
        "tab": tab, "q": q, "sort": sort, "sort_options": SORT_OPTIONS,
        "store": store, "tag": tag, "instock": instock == "1",
        "store_options": store_options, "tag_options": tag_options,
        "rows": page_rows, "total": total,
        "favorites_count": sum(1 for r in all_rows if r.is_favorite),
        "page": page, "total_pages": total_pages, "qs_base": urlencode(qs),
        "kpis": kpis,
    })
    return ctx


# ----------------------------- List pages -----------------------------

@router.get("/", response_class=HTMLResponse)
@router.get("/home", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), tab: str = "overview", q: str = "",
         sort: str = "recent", store: str = "", tag: str = "", instock: str = "", page: int = 1,
         added: str | None = None, error: str | None = None):
    ctx = _list_context(request, db, active="home", monitor=None, tab=tab, q=q, sort=sort,
                        store=store, tag=tag, instock=instock, page=page)
    ctx.update({"flash_added": added == "1", "flash_error": error})
    return templates.TemplateResponse(request, "home.html", ctx)


@router.get("/price-tracking", response_class=HTMLResponse)
def price_tracking(request: Request, db: Session = Depends(get_db), tab: str = "overview", q: str = "",
                   sort: str = "recent", store: str = "", tag: str = "", instock: str = "", page: int = 1,
                   added: str | None = None, msg: str | None = None, error: str | None = None):
    ctx = _list_context(request, db, active="price", monitor="price", tab=tab, q=q, sort=sort,
                        store=store, tag=tag, instock=instock, page=page)
    ctx.update({"flash_added": added, "flash_msg": msg, "flash_error": error})
    return templates.TemplateResponse(request, "price_tracking.html", ctx)


# ----------------------------- Add Product -----------------------------

def _get_or_create_tags(db: Session, raw: str) -> list[Tag]:
    names = [t.strip() for t in (raw or "").split(",") if t.strip()]
    out: list[Tag] = []
    for name in dict.fromkeys(names):  # de-dupe, preserve order
        tag = db.execute(select(Tag).where(Tag.name == name)).scalar_one_or_none()
        if not tag:
            tag = Tag(name=name)
            db.add(tag)
        out.append(tag)
    return out


def _parse_decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None


def _schedule_from_form(frequency: str, custom_minutes: str, daily_time: str):
    """Returns (schedule_kind, check_interval_minutes, daily_check_time)."""
    floor = settings.min_check_interval_minutes
    if frequency == "daily":
        t = (daily_time or "09:00")[:5]
        return ScheduleKind.DAILY, None, t
    if frequency == "custom":
        try:
            mins = max(int(custom_minutes), floor)
        except (TypeError, ValueError):
            mins = 60
        return ScheduleKind.INTERVAL, mins, None
    try:
        mins = max(int(frequency), floor)
    except (TypeError, ValueError):
        mins = 60
    return ScheduleKind.INTERVAL, mins, None


def _add_form_context(request, db, **extra):
    accounts = db.execute(select(AlertAccount).where(AlertAccount.enabled == True)).scalars().all()  # noqa: E712
    tag_options = sorted({t.name for t in db.execute(select(Tag)).scalars().all()})
    ctx = _base_context(request, "price")
    ctx.update({
        "frequency_choices": FREQUENCY_CHOICES,
        "condition_choices": CONDITION_CHOICES,
        "accounts": accounts,
        "tag_options": tag_options,
        "form": {"frequency": "60", "condition": "any_drop", "monitor_price": True, "monitor_stock": False},
        "preview": None,
        "duplicate": None,
        "error": None,
    })
    ctx.update(extra)
    return ctx


@router.get("/price-tracking/add", response_class=HTMLResponse)
def add_product_form(request: Request, db: Session = Depends(get_db), monitor: str = ""):
    extra = {}
    if monitor == "stock":
        extra["form"] = {"frequency": "60", "condition": "back_in_stock",
                         "monitor_price": True, "monitor_stock": True}
    return templates.TemplateResponse(request, "add_product.html", _add_form_context(request, db, **extra))


def _normalize_url(raw: str) -> str | None:
    raw = (raw or "").strip()
    p = urlparse(raw)
    if p.scheme in ("http", "https") and p.netloc:
        return raw
    return None


def _find_existing(db: Session, url: str, owner_id: int | None = None) -> Product | None:
    stmt = select(ProductURL).join(Product).where(ProductURL.url == url)
    if owner_id is not None:
        stmt = stmt.where(Product.user_id == owner_id)
    pu = db.execute(stmt).scalars().first()
    return pu.product if pu else None


@router.post("/price-tracking/add", response_class=HTMLResponse)
def add_product_submit(
    request: Request,
    db: Session = Depends(get_db),
    action: str = Form("preview"),
    url: str = Form(""),
    bulk_urls: str = Form(""),
    frequency: str = Form("60"),
    custom_minutes: str = Form(""),
    daily_time: str = Form(""),
    monitor_price: str = Form(""),
    monitor_stock: str = Form(""),
    target_price: str = Form(""),
    condition: str = Form("any_drop"),
    condition_percent: str = Form(""),
    account_ids: list[str] = Form(default=[]),
    tags: str = Form(""),
    # carried from the preview step
    pv_name: str = Form(""),
    pv_image: str = Form(""),
    pv_model: str = Form(""),
    pv_desc: str = Form(""),
    pv_price: str = Form(""),
    pv_currency: str = Form(""),
    pv_instock: str = Form(""),
):
    form = {
        "url": url, "bulk_urls": bulk_urls, "frequency": frequency, "custom_minutes": custom_minutes,
        "daily_time": daily_time, "monitor_price": monitor_price not in ("", "0"),
        "monitor_stock": monitor_stock not in ("", "0"), "target_price": target_price,
        "condition": condition, "condition_percent": condition_percent,
        "account_ids": account_ids, "tags": tags,
    }

    def render(**extra):
        return templates.TemplateResponse(request, "add_product.html", _add_form_context(request, db, form=form, **extra))

    kind, interval, dtime = _schedule_from_form(frequency, custom_minutes, daily_time)
    owner = _scope_owner(request, db)

    # ---- Bulk add: create many products as pending (imported on next check) ----
    if action == "bulk":
        urls = [u.strip() for u in bulk_urls.splitlines() if u.strip()]
        valid = [u for u in (_normalize_url(x) for x in urls) if u]
        created, skipped = 0, 0
        tag_objs = _get_or_create_tags(db, tags)
        for u in valid:
            if _find_existing(db, u, owner):
                skipped += 1
                continue
            domain = urlparse(u).netloc.lower().removeprefix("www.")
            p = Product(name=f"Pending — {domain}", track_price=form["monitor_price"],
                        track_stock=form["monitor_stock"], schedule_kind=kind,
                        check_interval_minutes=interval, daily_check_time=dtime, user_id=owner,
                        target_price=_parse_decimal(target_price), import_status=ImportStatus.PENDING)
            p.urls.append(ProductURL(url=u, domain=domain, store_name=domain))
            p.tags = list(tag_objs)
            _attach_alerts(db, p, condition, condition_percent, target_price, account_ids)
            db.add(p)
            created += 1
        db.commit()
        return RedirectResponse(
            f"/price-tracking?added={created}+added"
            + (f"%2C+{skipped}+already+tracked" if skipped else ""),
            status_code=303,
        )

    # ---- Single add: validate + duplicate check ----
    norm = _normalize_url(url)
    if not norm:
        return render(error="Enter a valid http(s) product URL.")
    existing = _find_existing(db, norm, owner)
    if existing:
        return render(duplicate=existing)

    # ---- Preview: fetch metadata, show confirm step ----
    if action == "preview":
        meta = import_from_url(norm, polite=False)
        return render(preview=meta, normalized_url=norm)

    # ---- Save: create the product (uses carried preview fields) ----
    domain = urlparse(norm).netloc.lower().removeprefix("www.")
    price = _parse_decimal(pv_price)
    instock = {"1": True, "0": False, "true": True, "false": False}.get(pv_instock.lower())
    imported = bool(pv_name)
    p = Product(
        user_id=owner,
        name=pv_name.strip() or f"Pending — {domain}",
        model_number=pv_model.strip() or None,
        description=pv_desc.strip() or None,
        image_url=pv_image.strip() or None,
        track_price=form["monitor_price"], track_stock=form["monitor_stock"],
        schedule_kind=kind, check_interval_minutes=interval, daily_check_time=dtime,
        target_price=_parse_decimal(target_price),
        import_status=ImportStatus.IMPORTED if imported else ImportStatus.PENDING,
    )
    pu = ProductURL(
        url=norm, domain=domain, store_name=domain,
        currency=(pv_currency.strip().upper() or None),
        baseline_price=price, last_price=price, last_in_stock=instock,
        last_checked_at=datetime.utcnow() if price is not None else None,
    )
    p.urls.append(pu)
    if price is not None:
        pu.price_history.append(PriceHistory(price=price, currency=pu.currency,
                                             in_stock=bool(instock), checked_at=datetime.utcnow()))
    p.tags = _get_or_create_tags(db, tags)
    _attach_alerts(db, p, condition, condition_percent, target_price, account_ids)
    db.add(p)
    db.commit()
    db.refresh(p)
    return RedirectResponse(f"/products/{p.id}?added=1", status_code=303)


def _attach_alerts(db, product, condition, percent, target_price, account_ids):
    """Create one alert rule per selected account for the chosen condition."""
    if condition in ("", "none") or not account_ids:
        return
    type_map = {
        "any_drop": (AlertType.PRICE_DROP_ANY, None),
        "below_target": (AlertType.PRICE_BELOW_TARGET, _parse_decimal(target_price)),
        "drop_percent": (AlertType.PRICE_DROP_PERCENT, _parse_decimal(percent)),
        "back_in_stock": (AlertType.BACK_IN_STOCK, None),
    }
    if condition not in type_map:
        return
    atype, threshold = type_map[condition]
    for aid in account_ids:
        try:
            account = db.get(AlertAccount, int(aid))
        except (TypeError, ValueError):
            account = None
        if not account:
            continue
        product.alert_rules.append(AlertRule(
            type=atype, threshold=threshold, channel=account.channel, alert_account_id=account.id
        ))


# ----------------------------- Quick add (topbar) -----------------------------

@router.post("/quick-add")
def quick_add(request: Request, url: str = Form(""), db: Session = Depends(get_db)):
    """Fast path from the topbar: create a pending product from a pasted link."""
    norm = _normalize_url(url)
    if not norm:
        return RedirectResponse("/?error=Enter+a+valid+http(s)+link", status_code=303)
    if _find_existing(db, norm):
        return RedirectResponse("/?error=You're+already+tracking+that+URL", status_code=303)
    domain = urlparse(norm).netloc.lower().removeprefix("www.")
    p = Product(name=f"Pending — {domain}", track_price=True, import_status=ImportStatus.PENDING,
                user_id=_scope_owner(request, db),
                check_interval_minutes=60)
    p.urls.append(ProductURL(url=norm, domain=domain, store_name=domain))
    db.add(p)
    db.commit()
    return RedirectResponse("/?added=1", status_code=303)


# ----------------------------- Row actions -----------------------------

def _redirect_back(request: Request) -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


def _redirect_back_msg(request: Request, msg: str) -> RedirectResponse:
    """Redirect to the page the action came from, surfacing a flash message."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    ref = request.headers.get("referer") or "/price-tracking"
    parts = urlsplit(ref)
    query = dict(parse_qsl(parts.query))
    query["msg"] = msg
    return RedirectResponse(
        urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "")),
        status_code=303,
    )


def _get_owned(request, db, product_id):
    """Lightweight product fetch that respects per-user ownership."""
    p = db.get(Product, product_id)
    if p is None:
        return None
    owner = _scope_owner(request, db)
    if owner is not None and p.user_id != owner:
        return None
    return p


@router.post("/products/{product_id}/favorite")
def toggle_favorite(product_id: int, request: Request, db: Session = Depends(get_db)):
    p = _get_owned(request, db, product_id)
    if p:
        p.is_favorite = not p.is_favorite
        db.commit()
    return _redirect_back(request)


@router.post("/products/{product_id}/pause")
def toggle_pause(product_id: int, request: Request, db: Session = Depends(get_db)):
    p = _get_owned(request, db, product_id)
    if p and p.urls:
        any_active = any(u.active for u in p.urls)
        for u in p.urls:
            u.active = not any_active
        db.commit()
    return _redirect_back(request)


@router.post("/products/{product_id}/delete")
def delete_product(product_id: int, request: Request, db: Session = Depends(get_db)):
    p = _get_owned(request, db, product_id)
    if p:
        db.delete(p)
        db.commit()
    return _redirect_back(request)


@router.post("/products/{product_id}/check-now")
def check_now(product_id: int, request: Request, db: Session = Depends(get_db)):
    """Re-fetch every active listing now, recording price + stock."""
    p = _get_owned(request, db, product_id)
    if not p:
        return _redirect_back(request)
    summary = checker.check_product(db, p)
    try:
        alerting.deliver(db, p, summary)
    except Exception:  # noqa: BLE001 - alert delivery must not break a manual check
        pass
    return _redirect_back(request)


@router.post("/products/check-all")
def check_all_now(request: Request, db: Session = Depends(get_db)):
    """Check every monitored product the current user owns, right now."""
    owner = _scope_owner(request, db)
    stmt = select(Product).options(
        selectinload(Product.urls), selectinload(Product.alert_rules))
    if owner is not None:
        stmt = stmt.where(Product.user_id == owner)
    products = db.execute(stmt).scalars().unique().all()
    checked = 0
    for p in products:
        if not (p.track_price or p.track_stock):
            continue
        if not any(u.active for u in p.urls):
            continue
        try:
            summary = checker.check_product(db, p)
            alerting.deliver(db, p, summary)
        except Exception:  # noqa: BLE001 - one product must not abort the batch
            db.rollback()
            continue
        checked += 1
    return _redirect_back_msg(request, f"Checked {checked} product{'' if checked == 1 else 's'}")


@router.post("/products/bulk")
def bulk_action(request: Request, db: Session = Depends(get_db),
                action: str = Form(""), ids: list[str] = Form(default=[])):
    """Apply one action (check / pause / resume / delete) to the selected products."""
    owner = _scope_owner(request, db)
    pids = [int(i) for i in ids if i.isdigit()]
    if not pids or action not in ("check", "pause", "resume", "delete"):
        return _redirect_back_msg(request, "No products selected")
    n = 0
    for pid in pids:
        p = db.get(Product, pid)
        if p is None or (owner is not None and p.user_id != owner):
            continue
        if action == "delete":
            db.delete(p)
        elif action == "pause":
            for u in p.urls:
                u.active = False
        elif action == "resume":
            for u in p.urls:
                u.active = True
        elif action == "check":
            try:
                summary = checker.check_product(db, p)
                alerting.deliver(db, p, summary)
            except Exception:  # noqa: BLE001 - one product must not abort the batch
                db.rollback()
                continue
        n += 1
    db.commit()
    verb = {"check": "Checked", "pause": "Paused", "resume": "Resumed", "delete": "Deleted"}[action]
    return _redirect_back_msg(request, f"{verb} {n} product{'' if n == 1 else 's'}")


@router.get("/api/alerts/unseen")
def api_alerts_unseen(request: Request, db: Session = Depends(get_db)):
    """Unseen browser-sound alerts for the current user (polled by the UI)."""
    owner = _scope_owner(request, db)
    stmt = (select(NotificationLog)
            .join(AlertRule, NotificationLog.alert_rule_id == AlertRule.id)
            .join(Product, AlertRule.product_id == Product.id)
            .where(NotificationLog.channel == AlertChannel.SOUND,
                   NotificationLog.success == True,  # noqa: E712
                   NotificationLog.seen == False)     # noqa: E712
            .order_by(NotificationLog.created_at.desc()).limit(20))
    if owner is not None:
        stmt = stmt.where(Product.user_id == owner)
    rows = db.execute(stmt).scalars().all()
    items = [{"id": r.id, "subject": r.subject or "Price alert",
              "message": r.message or ""} for r in rows]
    return JSONResponse({"count": len(items), "items": items})


@router.post("/api/alerts/seen")
def api_alerts_seen(request: Request, db: Session = Depends(get_db)):
    owner = _scope_owner(request, db)
    stmt = (select(NotificationLog)
            .join(AlertRule, NotificationLog.alert_rule_id == AlertRule.id)
            .join(Product, AlertRule.product_id == Product.id)
            .where(NotificationLog.channel == AlertChannel.SOUND,
                   NotificationLog.seen == False))  # noqa: E712
    if owner is not None:
        stmt = stmt.where(Product.user_id == owner)
    n = 0
    for r in db.execute(stmt).scalars().all():
        r.seen = True
        n += 1
    db.commit()
    return JSONResponse({"ok": True, "marked": n})


EMAIL_PROVIDERS = [("sendgrid", "SendGrid"), ("mailgun", "Mailgun"), ("resend", "Resend"), ("postmark", "Postmark")]


def _log(db: Session, *, channel: str | None, subject: str, success: bool,
         error: str | None = None, product_url_id: int | None = None,
         alert_rule_id: int | None = None) -> None:
    db.add(NotificationLog(channel=channel, subject=subject[:512], success=success,
                           error=error, product_url_id=product_url_id, alert_rule_id=alert_rule_id))


def _alerts_context(request: Request, db: Session, *, detected_chats=None,
                    msg: str | None = None, error: str | None = None) -> dict:
    public = settings_store.get_public(db)
    accounts = db.execute(select(AlertAccount).order_by(AlertAccount.id)).scalars().all()
    sample = settings_store.sample_context()
    rendered = {
        "price_subject": settings_store.render_template(public.get("tpl_price_subject", ""), sample),
        "price_body": settings_store.render_template(public.get("tpl_price_body", ""), sample),
        "stock_subject": settings_store.render_template(public.get("tpl_stock_subject", ""), sample),
        "stock_body": settings_store.render_template(public.get("tpl_stock_body", ""), sample),
    }
    logs = db.execute(select(NotificationLog).order_by(NotificationLog.created_at.desc()).limit(25)).scalars().all()
    log_rows = []
    for n in logs:
        product = None
        if n.product_url_id:
            pu = db.get(ProductURL, n.product_url_id)
            if pu:
                product = pu.product.name if pu.product else (pu.store_name or pu.domain)
        log_rows.append({"created_at": n.created_at, "channel": n.channel, "subject": n.subject,
                         "success": n.success, "error": n.error, "product": product})
    ctx = _base_context(request, "alerts")
    ctx.update({
        "cfg": public, "accounts": accounts, "email_providers": EMAIL_PROVIDERS,
        "placeholders": settings_store.PLACEHOLDERS, "rendered": rendered, "logs": log_rows,
        "detected_chats": detected_chats, "flash_msg": msg, "flash_error": error,
    })
    return ctx


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, db: Session = Depends(get_db),
                msg: str | None = None, error: str | None = None):
    return templates.TemplateResponse(request, "alerts.html", _alerts_context(request, db, msg=msg, error=error))


@router.post("/alerts/email")
def save_email(request: Request, db: Session = Depends(get_db), email_method: str = Form("smtp"),
               smtp_host: str = Form(""), smtp_port: str = Form("587"), smtp_user: str = Form(""),
               smtp_password: str = Form(""), smtp_from: str = Form(""), smtp_use_tls: str = Form(""),
               email_api_provider: str = Form("sendgrid"), email_api_key: str = Form(""),
               email_api_from: str = Form(""), email_api_domain: str = Form(""), email_html: str = Form("")):
    settings_store.set_values(db, {
        "email_method": email_method if email_method in ("smtp", "api") else "smtp",
        "smtp_host": smtp_host.strip(), "smtp_port": smtp_port.strip() or "587",
        "smtp_user": smtp_user.strip(), "smtp_password": smtp_password,
        "smtp_from": smtp_from.strip(), "smtp_use_tls": "1" if smtp_use_tls else "0",
        "email_api_provider": email_api_provider, "email_api_key": email_api_key,
        "email_api_from": email_api_from.strip(), "email_api_domain": email_api_domain.strip(),
        "email_html": "1" if email_html else "0",
    })
    return RedirectResponse("/alerts?msg=Email+settings+saved", status_code=303)


@router.post("/alerts/telegram")
def save_telegram(db: Session = Depends(get_db), telegram_bot_token: str = Form("")):
    settings_store.set_values(db, {"telegram_bot_token": telegram_bot_token})
    return RedirectResponse("/alerts?msg=Telegram+settings+saved", status_code=303)


@router.post("/alerts/templates")
def save_templates(db: Session = Depends(get_db), tpl_price_subject: str = Form(""),
                   tpl_price_body: str = Form(""), tpl_stock_subject: str = Form(""),
                   tpl_stock_body: str = Form("")):
    settings_store.set_values(db, {
        "tpl_price_subject": tpl_price_subject, "tpl_price_body": tpl_price_body,
        "tpl_stock_subject": tpl_stock_subject, "tpl_stock_body": tpl_stock_body,
    })
    return RedirectResponse("/alerts?msg=Templates+saved", status_code=303)


@router.post("/alerts/controls")
def save_controls(db: Session = Depends(get_db), notifications_paused: str = Form(""),
                  quiet_enabled: str = Form(""), quiet_start: str = Form("22:00"),
                  quiet_end: str = Form("07:00")):
    settings_store.set_values(db, {
        "notifications_paused": "1" if notifications_paused else "0",
        "quiet_enabled": "1" if quiet_enabled else "0",
        "quiet_start": quiet_start, "quiet_end": quiet_end,
    })
    return RedirectResponse("/alerts?msg=Saved", status_code=303)


@router.post("/alerts/email/test")
def test_email(db: Session = Depends(get_db), test_to: str = Form("")):
    cfg = settings_store.get_config(db)
    ctx = settings_store.sample_context()
    subject = "[TEST] " + settings_store.render_template(cfg.get("tpl_price_subject", ""), ctx)
    body = settings_store.render_template(cfg.get("tpl_price_body", ""), ctx)
    ok, message = notify.send_email(cfg, test_to.strip(), subject, body)
    _log(db, channel="email", subject=subject, success=ok, error=None if ok else message)
    db.commit()
    return RedirectResponse(f"/alerts?{'msg' if ok else 'error'}=" + message.replace(" ", "+"), status_code=303)


@router.post("/alerts/telegram/test")
def test_telegram(db: Session = Depends(get_db), test_chat: str = Form("")):
    cfg = settings_store.get_config(db)
    ctx = settings_store.sample_context()
    text = settings_store.render_template(cfg.get("tpl_price_body", ""), ctx)
    ok, message = notify.send_telegram(cfg, test_chat.strip(), "[TEST] " + text)
    _log(db, channel="telegram", subject="[TEST] price alert", success=ok, error=None if ok else message)
    db.commit()
    return RedirectResponse(f"/alerts?{'msg' if ok else 'error'}=" + message.replace(" ", "+"), status_code=303)


@router.post("/alerts/telegram/detect", response_class=HTMLResponse)
def detect_telegram(request: Request, db: Session = Depends(get_db)):
    cfg = settings_store.get_config(db)
    ok, message, chats = notify.fetch_telegram_chats(cfg)
    return templates.TemplateResponse(
        request,
        "alerts.html",
        _alerts_context(request, db, detected_chats=chats, msg=message if ok else None, error=None if ok else message),
    )


@router.post("/alerts/accounts/add")
def add_account(db: Session = Depends(get_db), label: str = Form(""), channel: str = Form("email"),
                destination: str = Form(""), fallback_account_id: str = Form("")):
    if not label.strip():
        return RedirectResponse("/alerts?error=Give+the+account+a+label", status_code=303)
    if channel not in (AlertChannel.EMAIL, AlertChannel.TELEGRAM):
        channel = AlertChannel.EMAIL
    db.add(AlertAccount(label=label.strip(), channel=channel, destination=destination.strip() or None,
                        fallback_account_id=int(fallback_account_id) if fallback_account_id.isdigit() else None))
    db.commit()
    return RedirectResponse("/alerts?msg=Account+added", status_code=303)


@router.post("/alerts/accounts/{account_id}/edit")
def edit_account(account_id: int, db: Session = Depends(get_db), label: str = Form(""),
                 destination: str = Form(""), enabled: str = Form(""), fallback_account_id: str = Form("")):
    a = db.get(AlertAccount, account_id)
    if not a:
        return RedirectResponse("/alerts?error=Account+not+found", status_code=303)
    if label.strip():
        a.label = label.strip()
    a.destination = destination.strip() or None
    a.enabled = bool(enabled)
    fid = int(fallback_account_id) if fallback_account_id.isdigit() else None
    a.fallback_account_id = fid if fid != account_id else None  # no self-fallback
    db.commit()
    return RedirectResponse("/alerts?msg=Account+saved", status_code=303)


@router.post("/alerts/accounts/{account_id}/delete")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    a = db.get(AlertAccount, account_id)
    if a:
        db.delete(a)
        db.commit()
    return RedirectResponse("/alerts?msg=Account+removed", status_code=303)


@router.post("/alerts/accounts/{account_id}/test")
def test_alert(account_id: int, db: Session = Depends(get_db)):
    account = db.get(AlertAccount, account_id)
    if not account:
        return RedirectResponse("/alerts?error=Account+not+found", status_code=303)
    cfg = settings_store.get_config(db)
    ok, message = notify.send_test(cfg, account)
    if ok:
        account.last_verified_at = datetime.utcnow()
    _log(db, channel=account.channel, subject="[TEST] alert", success=ok, error=None if ok else message)
    db.commit()
    return RedirectResponse(f"/alerts?{'msg' if ok else 'error'}=" + message.replace(" ", "+"), status_code=303)


# ----------------------------- Placeholders -----------------------------

@router.post("/settings/security/admin")
def create_admin(db: Session = Depends(get_db), username: str = Form(""),
                 password: str = Form(""), confirm: str = Form("")):
    if auth.admin_exists(db):
        return RedirectResponse("/admin?error=An+admin+already+exists", status_code=303)
    username = username.strip()
    if len(username) < 3:
        return RedirectResponse("/admin?error=Username+must+be+at+least+3+characters", status_code=303)
    if len(password) < 8:
        return RedirectResponse("/admin?error=Password+must+be+at+least+8+characters", status_code=303)
    if password != confirm:
        return RedirectResponse("/admin?error=Passwords+do+not+match", status_code=303)
    if auth.get_user_by_username(db, username):
        return RedirectResponse("/admin?error=That+username+is+taken", status_code=303)
    auth.create_user(db, username, password, role="admin")
    audit.log(db, "admin.created", actor="system", detail=f"username={username}")
    return RedirectResponse("/admin?msg=Admin+created.+You+can+now+enable+login.", status_code=303)


@router.post("/settings/security/login")
def toggle_login(request: Request, db: Session = Depends(get_db), login_enabled: str = Form("")):
    if not auth.admin_exists(db):
        return RedirectResponse("/admin?error=Create+an+admin+user+first", status_code=303)
    enabling = login_enabled not in ("", "0")
    settings_store.set_values(db, {"login_enabled": "1" if enabling else "0"})
    if enabling:
        admin = db.execute(
            select(User).where(User.role == "admin", User.is_active == True)  # noqa: E712
            .order_by(User.id)
        ).scalars().first()
        if admin:
            db.execute(update(Product).where(Product.user_id.is_(None)).values(user_id=admin.id))
            db.commit()
    _audit(request, db, "login.enabled" if enabling else "login.disabled")
    state = "enabled" if enabling else "disabled"
    return RedirectResponse(f"/admin?msg=Login+{state}", status_code=303)


@router.post("/settings/security/oidc")
def save_oidc(request: Request, db: Session = Depends(get_db),
              oidc_enabled: str = Form(""), oidc_provider_name: str = Form("SSO"),
              oidc_issuer: str = Form(""), oidc_client_id: str = Form(""),
              oidc_client_secret: str = Form(""), oidc_scopes: str = Form("openid email profile"),
              oidc_auto_provision: str = Form(""), oidc_default_role: str = Form("user"),
              allow_local_login: str = Form("")):
    enabled = oidc_enabled not in ("", "0")
    allow_local = allow_local_login not in ("", "0")
    if not allow_local and not enabled:
        return RedirectResponse("/admin?error=Enable+SSO+before+disabling+local+login", status_code=303)
    if enabled and (not oidc_issuer.strip() or not oidc_client_id.strip()):
        return RedirectResponse("/admin?error=SSO+needs+an+issuer+URL+and+client+ID", status_code=303)
    values = {
        "oidc_enabled": "1" if enabled else "0",
        "oidc_provider_name": oidc_provider_name.strip() or "SSO",
        "oidc_issuer": oidc_issuer.strip().rstrip("/"),
        "oidc_client_id": oidc_client_id.strip(),
        "oidc_scopes": oidc_scopes.strip() or "openid email profile",
        "oidc_auto_provision": "1" if oidc_auto_provision not in ("", "0") else "0",
        "oidc_default_role": "admin" if oidc_default_role == "admin" else "user",
        "allow_local_login": "1" if allow_local else "0",
    }
    if oidc_client_secret.strip():
        values["oidc_client_secret"] = oidc_client_secret  # blank keeps the stored secret
    settings_store.set_values(db, values)
    _audit(request, db, "oidc.updated", detail="enabled" if enabled else "disabled")
    return RedirectResponse("/admin?msg=SSO+settings+saved", status_code=303)


_ALLOWED_IMG = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}
_MAX_UPLOAD = 2 * 1024 * 1024


def _save_upload(file: UploadFile | None, prefix: str) -> str | None:
    """Save an uploaded image; returns served path, or 'BADTYPE'/'TOOBIG', or None."""
    if file is None or not file.filename:
        return None
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in _ALLOWED_IMG:
        return "BADTYPE"
    data = file.file.read(_MAX_UPLOAD + 1)
    if len(data) > _MAX_UPLOAD:
        return "TOOBIG"
    os.makedirs(settings.uploads_dir, exist_ok=True)
    name = f"{prefix}-{secrets.token_hex(6)}{ext}"
    with open(os.path.join(settings.uploads_dir, name), "wb") as fh:
        fh.write(data)
    return f"/uploads/{name}"


@router.post("/settings/login")
def save_login_page(db: Session = Depends(get_db), heading: str = Form(""),
                    subtext: str = Form(""), bg_color: str = Form(""),
                    remove_logo: str = Form(""), remove_bg: str = Form(""),
                    logo: UploadFile = File(None), background: UploadFile = File(None)):
    values = {"login_heading": heading.strip(), "login_subtext": subtext.strip()}
    if remove_logo:
        values["login_logo"] = ""
    else:
        res = _save_upload(logo, "login-logo")
        if res == "BADTYPE":
            return RedirectResponse("/admin?error=Logo+must+be+an+image", status_code=303)
        if res == "TOOBIG":
            return RedirectResponse("/admin?error=Logo+exceeds+2+MB", status_code=303)
        if res:
            values["login_logo"] = res
    if remove_bg:
        values["login_bg"] = ""
    else:
        res = _save_upload(background, "login-bg")
        if res == "BADTYPE":
            return RedirectResponse("/admin?error=Background+must+be+an+image", status_code=303)
        if res == "TOOBIG":
            return RedirectResponse("/admin?error=Background+exceeds+2+MB", status_code=303)
        if res:
            values["login_bg"] = f"url('{res}')"
        elif bg_color.strip():
            values["login_bg"] = bg_color.strip()
    settings_store.set_values(db, values)
    return RedirectResponse("/admin?msg=Login+page+saved", status_code=303)


def _safe_next(nxt: str | None) -> str:
    """Only allow same-site absolute paths (avoid open-redirect)."""
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/"


def _login_context(request: Request, db: Session, **extra) -> dict:
    cfg = settings_store.get_public(db)
    ctx = {
        "request": request, "app_name": settings.app_name,
        "theme_style": settings_store.theme_css(cfg), "theme_base": cfg.get("theme_base", "") or "",
        "login_heading": cfg.get("login_heading", ""),
        "login_subtext": cfg.get("login_subtext", "") or "Sign in to continue.",
        "login_logo": cfg.get("login_logo", ""), "login_bg": cfg.get("login_bg", ""),
        "allow_local_login": cfg.get("allow_local_login", "1") == "1",
        "oidc_enabled": cfg.get("oidc_enabled", "0") == "1",
        "oidc_provider_name": cfg.get("oidc_provider_name") or "SSO",
        "next": "/", "error": None,
    }
    ctx.update(extra)
    return ctx


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db), next: str = "/"):
    if not auth.login_enabled(db):
        return RedirectResponse("/", status_code=303)
    if auth.current_user(request, db) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", _login_context(request, db, next=_safe_next(next)))


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, db: Session = Depends(get_db), username: str = Form(""),
                 password: str = Form(""), next: str = Form("/")):
    target = _safe_next(next)
    if not auth.login_enabled(db):
        return RedirectResponse("/", status_code=303)
    user = auth.get_user_by_username(db, username)
    if user is None or not user.is_active or not auth.verify_password(password, user.password_hash):
        audit.log(db, "login.failure", actor=(username.strip() or "(blank)"), ip=_client_ip(request))
        ctx = _login_context(request, db, next=target, error="Incorrect username or password.")
        return templates.TemplateResponse(request, "login.html", ctx, status_code=401)
    user.last_login_at = datetime.utcnow()
    db.commit()
    audit.log(db, "login.success", actor=user.username, ip=_client_ip(request))
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, auth.sign_session({"uid": user.id, "role": user.role}),
                    max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax")
    return resp


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    user = auth.current_user(request, db)
    if user is not None:
        audit.log(db, "logout", actor=user.username, ip=_client_ip(request))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


# ---- OIDC single sign-on ----

OIDC_COOKIE = "priceorbit_oidc"


@router.get("/login/oidc")
def oidc_start(request: Request, db: Session = Depends(get_db), next: str = "/"):
    if not auth.login_enabled(db):
        return RedirectResponse("/", status_code=303)
    cfg = settings_store.get_config(db)
    if cfg.get("oidc_enabled") != "1" or not cfg.get("oidc_issuer") or not cfg.get("oidc_client_id"):
        return RedirectResponse("/login?error=SSO+is+not+configured", status_code=303)
    try:
        meta = oidc.discovery(cfg["oidc_issuer"])
    except oidc.OIDCError as exc:
        return RedirectResponse("/login?error=" + quote(str(exc)), status_code=303)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = oidc.make_pkce()
    redirect_uri = str(request.url_for("oidc_callback"))
    url = oidc.authorization_url(meta, cfg["oidc_client_id"], redirect_uri,
                                 cfg.get("oidc_scopes", ""), state, nonce, challenge)
    resp = RedirectResponse(url, status_code=303)
    token = auth.sign_session({"k": "oidc", "state": state, "nonce": nonce,
                               "verifier": verifier, "next": _safe_next(next)})
    resp.set_cookie(OIDC_COOKIE, token, max_age=600, httponly=True, samesite="lax")
    return resp


@router.get("/auth/oidc/callback")
def oidc_callback(request: Request, db: Session = Depends(get_db),
                  code: str = "", state: str = "", error: str = ""):
    data = auth.load_session(request.cookies.get(OIDC_COOKIE))

    def fail(msg: str):
        audit.log(db, "login.failure", actor="(sso)", ip=_client_ip(request))
        r = RedirectResponse("/login?error=" + msg, status_code=303)
        r.delete_cookie(OIDC_COOKIE)
        return r

    if error:
        return fail("SSO+sign-in+was+cancelled")
    if not data or data.get("k") != "oidc" or not state or state != data.get("state"):
        return fail("SSO+state+mismatch%2C+please+retry")
    cfg = settings_store.get_config(db)
    try:
        meta = oidc.discovery(cfg["oidc_issuer"])
        redirect_uri = str(request.url_for("oidc_callback"))
        tokens = oidc.exchange_code(meta, cfg["oidc_client_id"], cfg.get("oidc_client_secret", ""),
                                    redirect_uri, code, data["verifier"])
        claims = oidc.decode_id_token(tokens["id_token"]) if tokens.get("id_token") else {}
        if claims:
            oidc.validate_claims(claims, cfg["oidc_issuer"], cfg["oidc_client_id"], data.get("nonce", ""))
        merged = {**oidc.userinfo(meta, tokens.get("access_token", "")), **claims}
        subject, username, display = oidc.derive_identity(merged)
        if not subject:
            return fail("SSO+returned+no+identity")
    except oidc.OIDCError as exc:
        return fail(quote(str(exc))[:160])
    except Exception:  # noqa: BLE001
        return fail("SSO+sign-in+failed")

    user = auth.get_user_by_oidc_subject(db, subject)
    if user is None:
        existing = auth.get_user_by_username(db, username)
        if existing is not None:
            existing.oidc_subject = subject
            db.commit()
            user = existing
        elif cfg.get("oidc_auto_provision", "1") == "1":
            role = "admin" if cfg.get("oidc_default_role") == "admin" else "user"
            user = auth.create_oidc_user(db, username, subject, role=role, display_name=display)
        else:
            return fail("No+account+is+linked+to+this+SSO+identity")
    if not user.is_active:
        return fail("Your+account+is+disabled")

    user.last_login_at = datetime.utcnow()
    db.commit()
    audit.log(db, "login.success", actor=user.username, detail="via SSO", ip=_client_ip(request))
    resp = RedirectResponse(_safe_next(data.get("next", "/")), status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, auth.sign_session({"uid": user.id, "role": user.role}),
                    max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax")
    resp.delete_cookie(OIDC_COOKIE)
    return resp


# ---- Users management (admin-only; gated by middleware) ----

@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db),
               msg: str | None = None, error: str | None = None):
    ctx = _base_context(request, "settings")
    ctx.update({"users": auth.list_users(db), "admin_count": auth.active_admin_count(db),
                "audit_events": audit.recent(db, limit=50), "audit_labels": audit.LABELS,
                "flash_msg": msg, "flash_error": error})
    return templates.TemplateResponse(request, "users.html", ctx)


@router.post("/users/add")
def users_add(request: Request, db: Session = Depends(get_db), username: str = Form(""),
              password: str = Form(""), role: str = Form("user")):
    username = username.strip()
    if len(username) < 3:
        return RedirectResponse("/users?error=Username+must+be+at+least+3+characters", status_code=303)
    if len(password) < 8:
        return RedirectResponse("/users?error=Password+must+be+at+least+8+characters", status_code=303)
    if auth.get_user_by_username(db, username):
        return RedirectResponse("/users?error=That+username+is+taken", status_code=303)
    auth.create_user(db, username, password, role=("admin" if role == "admin" else "user"),
                     must_change_password=True)
    _audit(request, db, "user.created", detail=f"{username} ({'admin' if role == 'admin' else 'user'})")
    return RedirectResponse("/users?msg=User+created", status_code=303)


@router.post("/users/{user_id}/role")
def users_role(user_id: int, request: Request, db: Session = Depends(get_db), role: str = Form("user")):
    u = db.get(User, user_id)
    if u is None:
        return RedirectResponse("/users?error=User+not+found", status_code=303)
    if u.role == "admin" and role != "admin" and auth.active_admin_count(db) <= 1:
        return RedirectResponse("/users?error=Can%27t+demote+the+last+admin", status_code=303)
    u.role = "admin" if role == "admin" else "user"
    db.commit()
    _audit(request, db, "user.role_changed", detail=f"{u.username} -> {u.role}")
    return RedirectResponse("/users?msg=Role+updated", status_code=303)


@router.post("/users/{user_id}/active")
def users_active(request: Request, user_id: int, db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    cur = auth.current_user(request, db)
    if u is None:
        return RedirectResponse("/users?error=User+not+found", status_code=303)
    if cur is not None and u.id == cur.id and u.is_active:
        return RedirectResponse("/users?error=You+can%27t+disable+yourself", status_code=303)
    if u.is_active and u.role == "admin" and auth.active_admin_count(db) <= 1:
        return RedirectResponse("/users?error=Can%27t+disable+the+last+admin", status_code=303)
    u.is_active = not u.is_active
    db.commit()
    _audit(request, db, "user.activated" if u.is_active else "user.deactivated", detail=u.username)
    return RedirectResponse("/users?msg=User+updated", status_code=303)


@router.post("/users/{user_id}/reset-password")
def users_reset_password(user_id: int, request: Request, db: Session = Depends(get_db), password: str = Form("")):
    u = db.get(User, user_id)
    if u is None:
        return RedirectResponse("/users?error=User+not+found", status_code=303)
    if len(password) < 8:
        return RedirectResponse("/users?error=Password+must+be+at+least+8+characters", status_code=303)
    auth.set_password(db, u, password, must_change=True)
    _audit(request, db, "user.password_reset", detail=u.username)
    return RedirectResponse("/users?msg=Password+reset", status_code=303)


@router.post("/users/{user_id}/delete")
def users_delete(request: Request, user_id: int, db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    cur = auth.current_user(request, db)
    if u is None:
        return RedirectResponse("/users?error=User+not+found", status_code=303)
    if cur is not None and u.id == cur.id:
        return RedirectResponse("/users?error=You+can%27t+delete+yourself", status_code=303)
    if u.role == "admin" and auth.active_admin_count(db) <= 1:
        return RedirectResponse("/users?error=Can%27t+delete+the+last+admin", status_code=303)
    uname = u.username
    owned = db.execute(select(Product).where(Product.user_id == u.id)).scalars().all()
    for prod in owned:
        db.delete(prod)
    db.delete(u)
    db.commit()
    _audit(request, db, "user.deleted", detail=f"{uname} ({len(owned)} products removed)")
    return RedirectResponse("/users?msg=User+deleted", status_code=303)


# ---- Self-service profile (any signed-in user) ----

@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db),
                 msg: str | None = None, error: str | None = None):
    cur = auth.current_user(request, db)
    if cur is None:
        return RedirectResponse("/", status_code=303)
    ctx = _base_context(request, "profile")
    is_last_admin = cur.role == "admin" and auth.active_admin_count(db) <= 1
    ctx.update({"profile_user": cur, "is_last_admin": is_last_admin,
                "flash_msg": msg, "flash_error": error})
    return templates.TemplateResponse(request, "profile.html", ctx)


@router.post("/profile/name")
def profile_name(request: Request, db: Session = Depends(get_db), display_name: str = Form("")):
    cur = auth.current_user(request, db)
    if cur is None:
        return RedirectResponse("/", status_code=303)
    cur.display_name = display_name.strip() or None
    db.commit()
    return RedirectResponse("/profile?msg=Profile+saved", status_code=303)


@router.post("/profile/password")
def profile_password(request: Request, db: Session = Depends(get_db), current: str = Form(""),
                     new: str = Form(""), confirm: str = Form("")):
    cur = auth.current_user(request, db)
    if cur is None:
        return RedirectResponse("/", status_code=303)
    if not auth.verify_password(current, cur.password_hash):
        return RedirectResponse("/profile?error=Current+password+is+incorrect", status_code=303)
    if len(new) < 8:
        return RedirectResponse("/profile?error=New+password+must+be+at+least+8+characters", status_code=303)
    if new != confirm:
        return RedirectResponse("/profile?error=New+passwords+do+not+match", status_code=303)
    auth.set_password(db, cur, new, must_change=False)
    _audit(request, db, "profile.password_changed", detail=cur.username)
    return RedirectResponse("/profile?msg=Password+changed", status_code=303)


@router.get("/profile/export.json")
def profile_export(request: Request, db: Session = Depends(get_db)):
    import json

    cur = auth.current_user(request, db)
    if cur is None:
        return RedirectResponse("/", status_code=303)
    owner = _scope_owner(request, db)
    stmt = select(Product).options(
        selectinload(Product.urls).selectinload(ProductURL.price_history),
        selectinload(Product.tags),
    )
    if owner is not None:
        stmt = stmt.where(Product.user_id == owner)
    products = db.execute(stmt).scalars().all()
    payload = {
        "account": {
            "username": cur.username, "display_name": cur.display_name, "role": cur.role,
            "last_login_at": cur.last_login_at.isoformat() if cur.last_login_at else None,
        },
        "exported_at": datetime.utcnow().isoformat(),
        "products": [{
            "name": p.name, "model_number": p.model_number, "description": p.description,
            "target_price": str(p.target_price) if p.target_price is not None else None,
            "track_price": p.track_price, "track_stock": p.track_stock,
            "tags": [t.name for t in p.tags],
            "stores": [{
                "url": u.url, "store_name": u.store_name, "currency": u.currency,
                "is_primary": u.is_primary,
                "last_price": str(u.last_price) if u.last_price is not None else None,
                "history": [{"checked_at": h.checked_at.isoformat(),
                             "price": str(h.price) if h.price is not None else None,
                             "in_stock": bool(h.in_stock)} for h in u.price_history],
            } for u in p.urls],
        } for p in products],
    }
    _audit(request, db, "data.exported", detail=f"{len(products)} products")
    return PlainTextResponse(
        json.dumps(payload, indent=2), media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="priceorbit-my-data.json"'},
    )


@router.post("/profile/delete")
def profile_delete(request: Request, db: Session = Depends(get_db), password: str = Form("")):
    cur = auth.current_user(request, db)
    if cur is None:
        return RedirectResponse("/", status_code=303)
    if cur.role == "admin" and auth.active_admin_count(db) <= 1:
        return RedirectResponse("/profile?error=The+last+admin+can%27t+delete+their+own+account", status_code=303)
    if not auth.verify_password(password, cur.password_hash):
        return RedirectResponse("/profile?error=Password+is+incorrect", status_code=303)
    uname = cur.username
    owned = db.execute(select(Product).where(Product.user_id == cur.id)).scalars().all()
    n = len(owned)
    for prod in owned:
        db.delete(prod)
    db.delete(cur)
    db.commit()
    audit.log(db, "account.deleted", actor=uname, detail=f"{n} products removed", ip=_client_ip(request))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


_PLACEHOLDERS = {
    "/in-stock": ("stock", "In-Stock Monitor", "Watch availability and get notified the moment something restocks."),
    "/settings": ("settings", "Settings", "Configure notifications, scheduling, and optional sign-in."),
}


def _placeholder(request: Request, path: str, **extra) -> HTMLResponse:
    active, title, blurb = _PLACEHOLDERS[path]
    ctx = _base_context(request, active)
    ctx.update({"title": title, "blurb": blurb})
    ctx.update(extra)
    return templates.TemplateResponse(request, "_placeholder.html", ctx)


@router.get("/in-stock", response_class=HTMLResponse)
def in_stock_page(request: Request, db: Session = Depends(get_db), tab: str = "overview", q: str = "",
                  sort: str = "recent", store: str = "", tag: str = "", instock: str = "", page: int = 1,
                  added: str | None = None, msg: str | None = None, error: str | None = None):
    ctx = _list_context(request, db, active="stock", monitor="stock", tab=tab, q=q, sort=sort,
                        store=store, tag=tag, instock=instock, page=page)
    ctx.update({"flash_added": added, "flash_msg": msg, "flash_error": error})
    return templates.TemplateResponse(request, "in_stock.html", ctx)


_NOTIF_PAGE = 50


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, db: Session = Depends(get_db),
                       channel: str = "", outcome: str = "", product: str = "", page: int = 1):
    """Notification history across the user's products, with filters."""
    owner = _scope_owner(request, db)
    base = (select(NotificationLog, Product.name.label("pname"), Product.id.label("pid"))
            .join(AlertRule, NotificationLog.alert_rule_id == AlertRule.id)
            .join(Product, AlertRule.product_id == Product.id))
    if owner is not None:
        base = base.where(Product.user_id == owner)
    if channel in ("email", "telegram", "sound"):
        base = base.where(NotificationLog.channel == channel)
    if outcome == "sent":
        base = base.where(NotificationLog.success == True)  # noqa: E712
    elif outcome == "failed":
        base = base.where(NotificationLog.success == False,  # noqa: E712
                          ~NotificationLog.error.like("Muted%"))
    elif outcome == "suppressed":
        base = base.where(NotificationLog.success == False,  # noqa: E712
                          NotificationLog.error.like("Muted%"))
    if product.isdigit():
        base = base.where(Product.id == int(product))

    total = db.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    page = max(1, page)
    total_pages = max(1, (total + _NOTIF_PAGE - 1) // _NOTIF_PAGE)
    rows = db.execute(
        base.order_by(NotificationLog.created_at.desc())
        .limit(_NOTIF_PAGE).offset((page - 1) * _NOTIF_PAGE)
    ).all()
    entries = []
    for log, pname, pid in rows:
        if log.success:
            oc = "sent"
        elif (log.error or "").startswith("Muted"):
            oc = "suppressed"
        else:
            oc = "failed"
        entries.append({
            "created_at": log.created_at, "product": pname, "product_id": pid,
            "channel": log.channel, "outcome": oc, "subject": log.subject,
            "message": log.message, "error": log.error,
        })

    popts_stmt = select(Product.id, Product.name)
    if owner is not None:
        popts_stmt = popts_stmt.where(Product.user_id == owner)
    product_options = [{"id": r[0], "name": r[1]}
                       for r in db.execute(popts_stmt.order_by(Product.name)).all()]

    from urllib.parse import urlencode
    qs = {k: v for k, v in (("channel", channel), ("outcome", outcome), ("product", product)) if v}
    ctx = _base_context(request, "notifications")
    ctx.update({
        "entries": entries, "total": total, "page": page, "total_pages": total_pages,
        "channel": channel, "outcome": outcome, "product": product,
        "product_options": product_options, "qs_base": urlencode(qs),
    })
    return templates.TemplateResponse(request, "notifications.html", ctx)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db),
                  msg: str | None = None, error: str | None = None):
    cfg = settings_store.get_public(db)
    ctx = _base_context(request, "settings")
    ctx.update({
        "cfg": cfg,
        "currencies": settings_store.CURRENCIES,
        "date_formats": settings_store.DATE_FORMATS,
        "timezones": settings_store.COMMON_TIMEZONES,
        "theme_presets": settings_store.THEME_PRESETS,
        "scrapedo": politeness.scrapedo_usage(),
        "current_user": auth.current_user(request, db),
        "flash_msg": msg, "flash_error": error,
    })
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db),
               msg: str | None = None, error: str | None = None):
    cfg = settings_store.get_public(db)
    ctx = _base_context(request, "admin")
    ctx.update({
        "cfg": cfg,
        "status": _system_status(db),
        "admin_exists": auth.admin_exists(db),
        "login_locked": settings_store.login_type_override(db),
        "current_user": auth.current_user(request, db),
        "flash_msg": msg, "flash_error": error,
    })
    return templates.TemplateResponse(request, "admin.html", ctx)


@router.get("/admin/logs", response_class=HTMLResponse)
def admin_logs(request: Request, db: Session = Depends(get_db),
               lines: int = 300, msg: str | None = None, error: str | None = None):
    from app import logsetup
    level = settings_store.get_config(db).get("log_level") or settings.log_level
    lines = max(50, min(lines, 2000))
    entries = logsetup.tail(settings.log_file, lines)
    ctx = _base_context(request, "admin")
    ctx.update({
        "log_level": level,
        "level_names": logsetup.LEVEL_NAMES,
        "log_lines": list(reversed(entries)),   # newest first
        "log_count": len(entries),
        "lines": lines,
        "log_file": settings.log_file,
        "current_user": auth.current_user(request, db),
        "flash_msg": msg, "flash_error": error,
    })
    return templates.TemplateResponse(request, "admin_logs.html", ctx)


@router.post("/admin/logs/level")
def set_log_level(db: Session = Depends(get_db), log_level: str = Form("info")):
    from app import logsetup
    lvl = (log_level or "").lower()
    if lvl not in logsetup.LEVELS:
        return RedirectResponse("/admin/logs?error=Invalid+log+level", status_code=303)
    settings_store.set_values(db, {"log_level": lvl})
    logsetup.set_level(lvl)  # apply to web now; the worker picks it up on its next heartbeat
    return RedirectResponse(f"/admin/logs?msg=Log+level+set+to+{lvl}", status_code=303)


def _system_status(db: Session) -> dict:
    db_ok, migration = True, "unknown"
    try:
        db.execute(text("SELECT 1"))
        row = db.execute(text("SELECT version_num FROM alembic_version")).first()
        migration = row[0] if row else "none"
    except Exception:  # noqa: BLE001
        db_ok = False
    cfg = settings_store.get_config(db)
    now = datetime.utcnow()

    # Worker liveness from the heartbeat the scheduler writes (~every 5 min).
    hb_iso = cfg.get("worker_heartbeat_at", "")
    hb_age_min = None
    worker_online = False
    if hb_iso:
        try:
            hb = datetime.fromisoformat(hb_iso)
            if hb.tzinfo is not None:
                hb = hb.replace(tzinfo=None)
            hb_age_min = (now - hb).total_seconds() / 60.0
            worker_online = hb_age_min < 12.0
        except Exception:  # noqa: BLE001
            pass
    try:
        worker_jobs = int(cfg.get("worker_jobs"))
    except (TypeError, ValueError):
        worker_jobs = None

    # Monitoring rollup across all products (counts only; no names, to avoid leak).
    monitored = failing = 0
    next_runs: list[datetime] = []
    try:
        products = db.execute(
            select(Product).options(selectinload(Product.urls))
        ).scalars().unique().all()
        for p in products:
            if not (p.track_price or p.track_stock):
                continue
            active = [u for u in p.urls if u.active]
            if not active:
                continue
            monitored += 1
            if max((u.consecutive_failures or 0) for u in active) >= health.FAIL_THRESHOLD:
                failing += 1
            attempts = [u.last_attempt_at for u in p.urls if u.last_attempt_at]
            checks = [u.last_checked_at for u in p.urls if u.last_checked_at]
            anchor = max(attempts) if attempts else (max(checks) if checks else None)
            nr = schedule.next_run(now, p.schedule_kind, p.check_interval_minutes,
                                   p.daily_check_time, anchor,
                                   settings.default_check_interval_minutes)
            if nr:
                next_runs.append(nr)
    except Exception:  # noqa: BLE001
        pass

    return {
        "db_ok": db_ok, "migration": migration,
        "db_engine": settings.db_driver, "db_host": settings.db_host,
        "db_name": settings.db_name, "app_version": settings.app_version,
        "timezone": cfg.get("timezone", "UTC"),
        "worker_heartbeat": hb_iso,
        "worker_online": worker_online,
        "worker_heartbeat_age_min": hb_age_min,
        "worker_started_at": cfg.get("worker_started_at", ""),
        "worker_jobs": worker_jobs,
        "monitored_count": monitored,
        "failing_count": failing,
        "next_run": min(next_runs) if next_runs else None,
    }


@router.post("/settings/general")
def save_general(db: Session = Depends(get_db), timezone: str = Form("UTC"),
                 date_format: str = Form("%b %d, %Y"), time_format: str = Form("24"),
                 default_currency: str = Form("USD")):
    settings_store.set_values(db, {
        "timezone": timezone.strip() or "UTC", "date_format": date_format,
        "time_format": time_format if time_format in ("12", "24") else "24",
        "default_currency": default_currency,
    })
    return RedirectResponse("/settings?msg=General+settings+saved", status_code=303)


@router.post("/settings/scrapedo")
def save_scrapedo(db: Session = Depends(get_db),
                  scrapedo_enabled: str | None = Form(None),
                  scrapedo_token: str = Form(""),
                  scrapedo_render: str | None = Form(None),
                  scrapedo_super: str | None = Form(None),
                  scrapedo_geo: str = Form(""),
                  scrapedo_timeout_seconds: str = Form(""),
                  scrapedo_monthly_credits: str = Form("")):
    try:
        timeout = float(scrapedo_timeout_seconds)
        if timeout <= 0:
            raise ValueError
    except (TypeError, ValueError):
        timeout = settings.scrapedo_timeout_seconds
    try:
        credits = int(scrapedo_monthly_credits)
        if credits < 0:
            raise ValueError
    except (TypeError, ValueError):
        credits = settings.scrapedo_monthly_credits
    settings_store.set_values(db, {
        "scrapedo_enabled": "1" if scrapedo_enabled else "0",
        "scrapedo_token": scrapedo_token.strip(),  # secret; blank keeps the saved one
        "scrapedo_render": "1" if scrapedo_render else "0",
        "scrapedo_super": "1" if scrapedo_super else "0",
        "scrapedo_geo": scrapedo_geo.strip().upper(),
        "scrapedo_timeout_seconds": str(timeout),
        "scrapedo_monthly_credits": str(credits),
    })
    return RedirectResponse("/settings?msg=Scraping+API+settings+saved", status_code=303)


@router.post("/settings/theme")
def save_theme(db: Session = Depends(get_db), theme_base: str = Form(""),
               theme_accent: str = Form(""), theme_sidebar_bg: str = Form(""),
               theme_topbar_accent: str = Form(""), theme_link: str = Form("")):
    settings_store.set_values(db, {
        "theme_base": theme_base if theme_base in ("", "light", "dark") else "",
        "theme_accent": theme_accent.strip(), "theme_sidebar_bg": theme_sidebar_bg.strip(),
        "theme_topbar_accent": theme_topbar_accent.strip(), "theme_link": theme_link.strip(),
    })
    return RedirectResponse("/settings?msg=Appearance+saved", status_code=303)


@router.post("/settings/theme/reset")
def reset_theme(db: Session = Depends(get_db)):
    settings_store.set_values(db, {k: "" for k in
                                   ["theme_base", "theme_accent", "theme_sidebar_bg",
                                    "theme_topbar_accent", "theme_link"]})
    return RedirectResponse("/settings?msg=Theme+reset+to+default", status_code=303)


@router.get("/settings/export.json")
def export_settings(db: Session = Depends(get_db)):
    import json
    data = settings_store.export_all(db)
    return PlainTextResponse(
        json.dumps(data, indent=2), media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="priceorbit-settings.json"'},
    )


@router.post("/settings/import")
def import_settings(db: Session = Depends(get_db), payload: str = Form("")):
    import json
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except Exception:  # noqa: BLE001
        return RedirectResponse("/settings?error=Could+not+parse+that+JSON", status_code=303)
    n = settings_store.import_values(db, data)
    return RedirectResponse(f"/settings?msg=Restored+{n}+settings", status_code=303)


@router.get("/settings/backup/products.json")
def export_products(request: Request, db: Session = Depends(get_db)):
    import json
    owner = _scope_owner(request, db)
    stmt = select(Product).options(
        selectinload(Product.urls).selectinload(ProductURL.price_history),
        selectinload(Product.tags),
    )
    if owner is not None:
        stmt = stmt.where(Product.user_id == owner)
    products = db.execute(stmt).scalars().all()
    _audit(request, db, "data.exported", detail="products backup (admin)")
    out = []
    for p in products:
        out.append({
            "name": p.name, "model_number": p.model_number, "description": p.description,
            "target_price": str(p.target_price) if p.target_price is not None else None,
            "track_price": p.track_price, "track_stock": p.track_stock,
            "tags": [t.name for t in p.tags],
            "stores": [{
                "url": u.url, "store_name": u.store_name, "currency": u.currency,
                "is_primary": u.is_primary, "last_price": str(u.last_price) if u.last_price is not None else None,
                "history": [{"checked_at": h.checked_at.isoformat(),
                             "price": str(h.price) if h.price is not None else None,
                             "in_stock": bool(h.in_stock)} for h in u.price_history],
            } for u in p.urls],
        })
    return PlainTextResponse(
        json.dumps(out, indent=2), media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="priceorbit-products.json"'},
    )


def _load_product(request, db: Session, product_id: int) -> Product | None:
    p = db.execute(
        select(Product).where(Product.id == product_id).options(
            selectinload(Product.urls).selectinload(ProductURL.price_history),
            selectinload(Product.tags),
            selectinload(Product.alert_rules).selectinload(AlertRule.account),
        )
    ).scalars().first()
    if p is None:
        return None
    owner = _scope_owner(request, db)
    if owner is not None and p.user_id != owner:
        return None  # not yours -> treated as not found
    return p


@router.get("/products/{product_id}", response_class=HTMLResponse)
def product_detail(product_id: int, request: Request, db: Session = Depends(get_db),
                   added: str | None = None, msg: str | None = None, error: str | None = None):
    p = _load_product(request, db, product_id)
    if p is None:
        ctx = _base_context(request, "price")
        ctx.update({"title": "Product not found", "blurb": "This product may have been deleted."})
        return templates.TemplateResponse(request, "_placeholder.html", ctx)
    detail = build_detail(db, p)
    ctx = _base_context(request, "price")
    ctx.update({
        "detail": detail,
        "frequency_choices": FREQUENCY_CHOICES,
        "trigger_choices": TRIGGER_CHOICES,
        "product_schedule_label": schedule_display(p),
        "target_display": format_price(p.target_price, detail.currency) if p.target_price else None,
        "tags_text": ", ".join(t.name for t in p.tags),
        "flash_added": added == "1", "flash_msg": msg, "flash_error": error,
    })
    return templates.TemplateResponse(request, "product_detail.html", ctx)


def _detail_redirect(product_id: int, **params) -> RedirectResponse:
    suffix = ("?" + urlencode(params)) if params else ""
    return RedirectResponse(f"/products/{product_id}{suffix}", status_code=303)


def _favicon_for(domain: str | None) -> str | None:
    return f"https://{domain}/favicon.ico" if domain else None


# ---- Edit product identity / schedule ----

@router.post("/products/{product_id}/details")
def edit_details(product_id: int, request: Request, db: Session = Depends(get_db), name: str = Form(""),
                 model_number: str = Form(""), description: str = Form(""),
                 target_price: str = Form(""), tags: str = Form("")):
    p = _load_product(request, db, product_id)
    if p is None:
        return _detail_redirect(product_id, error="Product not found")
    if name.strip():
        p.name = name.strip()
    p.model_number = model_number.strip() or None
    p.description = description.strip() or None
    p.target_price = _parse_decimal(target_price)
    p.tags = _get_or_create_tags(db, tags)
    db.commit()
    return _detail_redirect(product_id, msg="Saved")


@router.post("/products/{product_id}/schedule")
def edit_schedule(product_id: int, request: Request, db: Session = Depends(get_db), frequency: str = Form("60"),
                  custom_minutes: str = Form(""), daily_time: str = Form(""),
                  monitor_price: str = Form(""), monitor_stock: str = Form("")):
    p = _load_product(request, db, product_id)
    if p is None:
        return _detail_redirect(product_id, error="Product not found")
    kind, interval, dtime = _schedule_from_form(frequency, custom_minutes, daily_time)
    p.schedule_kind, p.check_interval_minutes, p.daily_check_time = kind, interval, dtime
    p.track_price = monitor_price not in ("", "0")
    p.track_stock = monitor_stock not in ("", "0")
    db.commit()
    return _detail_redirect(product_id, msg="Schedule updated")


# ---- Store management ----

@router.post("/products/{product_id}/stores/add")
def add_store(product_id: int, request: Request, db: Session = Depends(get_db), url: str = Form("")):
    p = _load_product(request, db, product_id)
    if p is None:
        return _detail_redirect(product_id, error="Product not found")
    norm = _normalize_url(url)
    if not norm:
        return _detail_redirect(product_id, error="Enter a valid http(s) URL")
    if any(u.url == norm for u in p.urls):
        return _detail_redirect(product_id, error="That URL is already a store for this product")
    domain = urlparse(norm).netloc.lower().removeprefix("www.")
    meta = import_from_url(norm, polite=False)
    pu = ProductURL(
        url=norm, domain=domain, store_name=meta.store_name or domain,
        favicon_url=_favicon_for(domain), currency=(meta.currency or p.urls[0].currency if p.urls else meta.currency),
        is_primary=(len(p.urls) == 0),
    )
    if meta.ok and meta.price is not None:
        pu.baseline_price = pu.last_price = meta.price
        pu.last_in_stock = meta.in_stock
        pu.last_checked_at = datetime.utcnow()
        pu.price_history.append(PriceHistory(price=meta.price, currency=pu.currency,
                                             in_stock=bool(meta.in_stock), checked_at=datetime.utcnow()))
    p.urls.append(pu)
    db.commit()
    return _detail_redirect(product_id, msg="Store added")


def _store_schedule(frequency: str, custom_minutes: str, daily_time: str):
    """Per-store override. 'inherit' clears the override (uses product schedule)."""
    if frequency in ("", "inherit"):
        return None, None
    kind, interval, dtime = _schedule_from_form(frequency, custom_minutes, daily_time)
    if kind == ScheduleKind.DAILY:
        return "daily", ("daily", None, dtime)
    return "interval", ("interval", interval, None)


@router.post("/products/{product_id}/stores/{url_id}/edit")
def edit_store(product_id: int, url_id: int, request: Request, db: Session = Depends(get_db), url: str = Form(""),
               frequency: str = Form("inherit"), custom_minutes: str = Form(""),
               daily_time: str = Form(""), make_primary: str = Form("")):
    p = _load_product(request, db, product_id)
    pu = next((u for u in p.urls if u.id == url_id), None) if p else None
    if not pu:
        return _detail_redirect(product_id, error="Store not found")
    norm = _normalize_url(url)
    if norm:
        pu.url = norm
        pu.domain = urlparse(norm).netloc.lower().removeprefix("www.")
        pu.favicon_url = _favicon_for(pu.domain)
    kind, sched = _store_schedule(frequency, custom_minutes, daily_time)
    if kind is None:
        pu.schedule_kind = None  # inherit product schedule
    else:
        _, interval, dtime = sched
        pu.schedule_kind = kind
        if interval:
            pu.check_interval_minutes = interval
        pu.daily_check_time = dtime
    if make_primary not in ("", "0"):
        for u in p.urls:
            u.is_primary = (u.id == url_id)
    db.commit()
    return _detail_redirect(product_id, msg="Store updated")


@router.post("/products/{product_id}/stores/{url_id}/check-now")
def check_store_now(product_id: int, url_id: int, request: Request, db: Session = Depends(get_db)):
    p = _load_product(request, db, product_id)
    pu = next((u for u in p.urls if u.id == url_id), None) if p else None
    if not pu:
        return _detail_redirect(product_id, error="Store not found")
    r = checker.check_url(db, pu)
    db.commit()
    if r.ok:
        return _detail_redirect(product_id, msg="Checked")
    return _detail_redirect(product_id, error=r.error or "Check failed")


@router.post("/products/{product_id}/stores/{url_id}/pause")
def pause_store(product_id: int, url_id: int, request: Request, db: Session = Depends(get_db)):
    p = _load_product(request, db, product_id)
    pu = next((u for u in p.urls if u.id == url_id), None) if p else None
    if pu:
        pu.active = not pu.active
        db.commit()
    return _detail_redirect(product_id)


@router.post("/products/{product_id}/stores/{url_id}/correct-price")
def correct_price(product_id: int, url_id: int, request: Request, db: Session = Depends(get_db), price: str = Form("")):
    p = _load_product(request, db, product_id)
    pu = next((u for u in p.urls if u.id == url_id), None) if p else None
    if not pu:
        return _detail_redirect(product_id, error="Store not found")
    val = _parse_decimal(price)
    if val is None:
        return _detail_redirect(product_id, error="Enter a valid price")
    pu.last_price = val
    latest = max(pu.price_history, key=lambda h: h.checked_at, default=None)
    if latest is not None:
        latest.price = val
    else:
        pu.price_history.append(PriceHistory(price=val, currency=pu.currency,
                                             in_stock=bool(pu.last_in_stock), checked_at=datetime.utcnow()))
    db.commit()
    return _detail_redirect(product_id, msg="Price corrected")


@router.post("/products/{product_id}/stores/{url_id}/remove")
def remove_store(product_id: int, url_id: int, request: Request, db: Session = Depends(get_db)):
    p = _load_product(request, db, product_id)
    pu = next((u for u in p.urls if u.id == url_id), None) if p else None
    if not pu:
        return _detail_redirect(product_id, error="Store not found")
    was_primary = pu.is_primary
    p.urls.remove(pu)
    db.delete(pu)
    db.flush()
    if was_primary and p.urls:
        earliest = min(p.urls, key=lambda u: u.id)
        earliest.is_primary = True
    db.commit()
    return _detail_redirect(product_id, msg="Store removed")


# ---- Alert rules ----

@router.post("/products/{product_id}/alerts/add")
def add_alert_rule(product_id: int, request: Request, db: Session = Depends(get_db),
                   trigger: str = Form(""), threshold: str = Form(""),
                   account_ids: list[str] = Form(default=[]), sound: str = Form("")):
    p = _load_product(request, db, product_id)
    if p is None:
        return _detail_redirect(product_id, error="Product not found")
    if trigger not in AlertType.ALL:
        return _detail_redirect(product_id, error="Pick an alert trigger")
    if not account_ids and not sound:
        return _detail_redirect(product_id, error="Pick at least one destination")
    thresh = _parse_decimal(threshold) if trigger in (MONEY_TRIGGERS | PERCENT_TRIGGERS) else None
    if trigger in (MONEY_TRIGGERS | PERCENT_TRIGGERS) and thresh is None:
        return _detail_redirect(product_id, error="This trigger needs a threshold value")
    for aid in account_ids:
        account = db.get(AlertAccount, int(aid)) if aid.isdigit() else None
        if account:
            p.alert_rules.append(AlertRule(type=trigger, threshold=thresh,
                                           channel=account.channel, alert_account_id=account.id))
    if sound:
        p.alert_rules.append(AlertRule(type=trigger, threshold=thresh,
                                       channel=AlertChannel.SOUND, alert_account_id=None))
    db.commit()
    return _detail_redirect(product_id, msg="Alert added")


@router.post("/products/{product_id}/alerts/{rule_id}/toggle")
def toggle_alert_rule(product_id: int, rule_id: int, request: Request, db: Session = Depends(get_db)):
    owned = _get_owned(request, db, product_id)
    r = db.get(AlertRule, rule_id)
    if owned and r and r.product_id == product_id:
        r.enabled = not r.enabled
        db.commit()
    return _detail_redirect(product_id)


@router.post("/products/{product_id}/alerts/{rule_id}/delete")
def delete_alert_rule(product_id: int, rule_id: int, request: Request, db: Session = Depends(get_db)):
    owned = _get_owned(request, db, product_id)
    r = db.get(AlertRule, rule_id)
    if owned and r and r.product_id == product_id:
        db.delete(r)
        db.commit()
    return _detail_redirect(product_id, msg="Alert removed")


# ---- Export ----

@router.get("/products/{product_id}/history.csv")
def export_history(product_id: int, request: Request, db: Session = Depends(get_db)):
    import csv
    import io

    p = _load_product(request, db, product_id)
    if p is None:
        return PlainTextResponse("Product not found", status_code=404)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["checked_at", "store", "domain", "price", "currency", "in_stock"])
    rows = []
    for u in p.urls:
        for h in u.price_history:
            rows.append((h.checked_at, u.store_name or u.domain or "", u.domain or "",
                         h.price, h.currency or u.currency or "", int(bool(h.in_stock))))
    for r in sorted(rows, key=lambda x: x[0]):
        writer.writerow([r[0].isoformat(), r[1], r[2], r[3] if r[3] is not None else "", r[4], r[5]])
    filename = f"price-history-{product_id}.csv"
    return PlainTextResponse(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
