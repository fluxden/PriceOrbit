"""Home Depot adapter — reads embedded Apollo state for ONLINE availability.

Home Depot renders availability client-side; the static DOM only has a "Select
store" widget, and the schema.org block (``thd-helmet__script--productStructureData``)
carries price but no availability. The real online signal lives in the Apollo
cache (``window.__APOLLO_STATE__``) on the product node::

    "availabilityType": {"type": "Shared", "discontinued": false,
                         "buyable": true, "status": false}

``buyable`` is whether the item can be purchased online; ``discontinued`` retires
it. (``status`` is a separate, non-stock flag and is intentionally ignored.)

Best-effort and defensive: only fills ``meta.in_stock`` from a definite signal and
never raises. Falls back to schema.org availability if a future page exposes it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.site_adapters.base import _availability_to_stock, deep_find, script_json

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

    from app.services.importer import ProductMetadata


def extract(soup: "BeautifulSoup", meta: "ProductMetadata") -> None:
    # The embedded online signal is more authoritative for Home Depot than a
    # generic schema.org/OG guess, so a definite value here overrides earlier layers.

    # 1. Apollo cache: availabilityType.buyable is the online-purchase signal.
    state = script_json(soup, marker="__APOLLO_STATE__")
    if state is not None:
        at = deep_find(state, "availabilityType")
        if isinstance(at, dict) and "buyable" in at:
            meta.in_stock = bool(at.get("buyable")) and not at.get("discontinued", False)
            return

    # 2. Fallback: schema.org availability, if a page ever exposes it here.
    data = script_json(soup, id="thd-helmet__script--productStructureData")
    if data is not None:
        avail = deep_find(data, "availability")
        if avail is not None:
            stock = _availability_to_stock(avail)
            if stock is not None:
                meta.in_stock = stock
