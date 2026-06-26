"""Walmart adapter — reads the embedded Next.js state for ONLINE availability.

Walmart hydrates the product page from a ``<script id="__NEXT_DATA__">`` JSON
blob; the rendered DOM also carries store-pickup widgets whose "out of stock"
text would fool the generic text scanner. We read the blob's
``availabilityStatus`` (the ship-to-home / online signal) directly instead.

The exact JSON path drifts over time, so everything here is best-effort and
defensive — it only *fills gaps* on ``meta`` and never raises. Validate against a
captured product-page HTML fixture before trusting it in production.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.site_adapters.base import deep_find, normalize_currency, parse_price, script_json

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

    from app.services.importer import ProductMetadata


def extract(soup: "BeautifulSoup", meta: "ProductMetadata") -> None:
    data = script_json(soup, id="__NEXT_DATA__")
    if data is None:
        return

    # availabilityStatus is the online (ship-to-home) signal: IN_STOCK |
    # OUT_OF_STOCK | RETIRED. Found on the product node within initialData. This
    # is more authoritative for Walmart than a generic schema.org/OG guess, so a
    # definite value overrides whatever the earlier layers set.
    status = deep_find(data, "availabilityStatus")
    if isinstance(status, str) and status.strip():
        meta.in_stock = status.strip().upper() == "IN_STOCK"

    if meta.price is None:
        cur = deep_find(data, "currentPrice")  # {price: 12.34, currencyUnit: "USD"}
        if isinstance(cur, dict):
            meta.price = parse_price(cur.get("price"))
            meta.currency = meta.currency or normalize_currency(cur.get("currencyUnit"))
