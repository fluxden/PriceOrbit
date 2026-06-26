"""Run a price/stock check for one listing or a whole product.

Thin orchestration over the importer: politeness and robots handling live inside
``import_from_url``. This module records a ``price_history`` point, refreshes each
listing's ``last_*`` fields, and reports before/after values so the alert engine
(Part 3/4) can detect drops and restocks without re-querying. Network-dependent
at runtime; pure given an injected ``fetcher``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Callable

from app.models import ImportStatus, PriceHistory
from app.services.importer import import_from_url

if TYPE_CHECKING:  # avoid importing ORM mapped classes at runtime
    from sqlalchemy.orm import Session

    from app.models import Product, ProductURL
    from app.services.importer import ProductMetadata


@dataclass
class UrlCheck:
    """Outcome of checking a single listing."""

    url_id: int
    ok: bool = False
    recorded: bool = False
    error: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    in_stock: bool | None = None
    prev_price: Decimal | None = None
    prev_in_stock: bool | None = None
    # product-level enrichment captured from the fetch
    name: str | None = None
    image_url: str | None = None
    model_number: str | None = None

    @property
    def price_changed(self) -> bool:
        return (self.recorded and self.price is not None
                and self.prev_price is not None and self.price != self.prev_price)

    @property
    def dropped(self) -> bool:
        return self.price_changed and self.price < self.prev_price

    @property
    def rose(self) -> bool:
        return self.price_changed and self.price > self.prev_price

    @property
    def restocked(self) -> bool:
        return self.recorded and bool(self.in_stock) and self.prev_in_stock is False

    @property
    def went_oos(self) -> bool:
        return self.recorded and self.in_stock is False and bool(self.prev_in_stock)


@dataclass
class ProductCheck:
    """Outcome of checking every active listing on a product."""

    product_id: int
    checked: int = 0
    ok: int = 0
    error: str | None = None
    results: list[UrlCheck] = field(default_factory=list)

    @property
    def any_ok(self) -> bool:
        return self.ok > 0


def _record_failure(pu: "ProductURL", now: datetime, error: str | None) -> None:
    pu.last_attempt_at = now
    pu.consecutive_failures = (pu.consecutive_failures or 0) + 1
    pu.last_error = (error or "Unknown error")[:512]


def check_url(db: "Session", pu: "ProductURL", *,
              fetcher: Callable[[str], "ProductMetadata"] = import_from_url) -> UrlCheck:
    """Fetch one listing, record a history point, and refresh its ``last_*`` fields."""
    res = UrlCheck(url_id=pu.id, prev_price=pu.last_price, prev_in_stock=pu.last_in_stock)
    now = datetime.utcnow()
    try:
        meta = fetcher(pu.url)
    except Exception as exc:  # noqa: BLE001 - never let one listing abort a run
        res.error = f"Check error: {exc}"
        _record_failure(pu, now, res.error)
        return res
    if not meta.ok:
        res.error = meta.error
        _record_failure(pu, now, res.error)
        return res

    res.ok = True
    res.name, res.image_url, res.model_number = meta.name, meta.image_url, meta.model_number
    if meta.price is not None:
        pu.last_price = meta.price
        if pu.baseline_price is None:
            pu.baseline_price = meta.price
    if meta.currency:
        pu.currency = meta.currency
    if meta.in_stock is not None:
        pu.last_in_stock = meta.in_stock
    pu.last_checked_at = now
    pu.last_attempt_at = now
    pu.consecutive_failures = 0
    pu.last_error = None
    pu.last_engine = meta.engine
    pu.price_history.append(PriceHistory(
        price=meta.price, currency=pu.currency,
        in_stock=bool(meta.in_stock), checked_at=now,
    ))
    res.price, res.currency, res.in_stock, res.recorded = meta.price, pu.currency, meta.in_stock, True
    return res


def check_product(db: "Session", product: "Product", *, commit: bool = True,
                  fetcher: Callable[[str], "ProductMetadata"] = import_from_url) -> ProductCheck:
    """Check every active listing on ``product`` and update its import status."""
    summary = ProductCheck(product_id=product.id)
    product.import_status = ImportStatus.IMPORTING
    if commit:
        db.commit()

    last_err: str | None = None
    for pu in product.urls:
        if not pu.active:
            continue
        r = check_url(db, pu, fetcher=fetcher)
        summary.results.append(r)
        summary.checked += 1
        if r.ok:
            summary.ok += 1
            if not product.image_url and r.image_url:
                product.image_url = r.image_url
            if product.name.startswith("Pending") and r.name:
                product.name = r.name
            if not product.model_number and r.model_number:
                product.model_number = r.model_number
        else:
            last_err = r.error

    product.import_status = ImportStatus.IMPORTED if summary.any_ok else ImportStatus.FAILED
    product.import_error = None if summary.any_ok else last_err
    summary.error = None if summary.any_ok else last_err
    if commit:
        db.commit()
    return summary
