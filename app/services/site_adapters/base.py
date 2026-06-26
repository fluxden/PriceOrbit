"""Per-store adapter contract + shared helpers for the adapter library.

A :class:`SiteAdapter` is a declarative recipe for reading one store (or one
platform) that the generic structured-data layers can't read on their own. Most
adapters are pure CSS selectors; stores that render availability/price from an
embedded JSON blob (Walmart, Home Depot) supply an ``extract`` callable that
parses that blob via :func:`script_json` / :func:`deep_find`.

This module is the stable home for the adapter contract. The low-level DOM/price
helpers it leans on still live in :mod:`app.services.importer`; importing them
here (rather than the reverse) keeps the dependency one-directional — the
importer pulls the adapter registry in lazily, so there is no import cycle.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from bs4 import BeautifulSoup

# Pure helpers owned by the importer (no adapter logic in them).
from app.services.importer import (
    _availability_to_stock,
    _sel_image,
    _sel_present,
    _sel_price,
    _sel_text,
    normalize_currency,
    parse_price,
)

if TYPE_CHECKING:  # avoid a runtime import cycle; only needed for typing
    from app.services.importer import ProductMetadata

__all__ = [
    "SiteAdapter",
    "apply_adapter",
    "script_json",
    "deep_find",
    # re-exported so store modules import everything from one place
    "_availability_to_stock",
    "normalize_currency",
    "parse_price",
]


@dataclass
class SiteAdapter:
    name: str
    domains: tuple = ()
    detect: object = None  # optional callable(soup) -> bool
    # Optional callable(soup, meta) for stores whose price/stock live in an
    # embedded JSON blob rather than the DOM. Runs before the selectors below and
    # only fills gaps (it must respect values already set on ``meta``).
    extract: Callable[[BeautifulSoup, "ProductMetadata"], None] | None = None
    name_sel: tuple = ()
    image_sel: tuple = ()
    price_sel: tuple = ()
    price_attr: str | None = None
    currency_sel: tuple = ()
    in_stock_sel: tuple = ()
    out_stock_sel: tuple = ()

    def matches(self, host: str, soup: BeautifulSoup) -> bool:
        if self.domains and any(d in host for d in self.domains):
            return True
        if self.detect is not None:
            try:
                return bool(self.detect(soup))
            except Exception:  # noqa: BLE001
                return False
        return False


def apply_adapter(adapter: SiteAdapter, soup: BeautifulSoup, meta: "ProductMetadata") -> None:
    """Fill gaps on ``meta`` from one adapter: JSON ``extract`` first, then selectors."""
    if adapter.extract is not None:
        try:
            adapter.extract(soup, meta)
        except Exception:  # noqa: BLE001 - a store's JSON shape can drift; never abort
            pass
    meta.name = meta.name or _sel_text(soup, adapter.name_sel)
    if not meta.image_url and adapter.image_sel:
        meta.image_url = _sel_image(soup, adapter.image_sel)
    if meta.price is None:
        price, cur = _sel_price(soup, adapter.price_sel, adapter.price_attr)
        if price is not None:
            meta.price = price
            meta.currency = meta.currency or cur
    if meta.currency is None and adapter.currency_sel:
        meta.currency = normalize_currency(_sel_text(soup, adapter.currency_sel))
    if meta.in_stock is None:
        if _sel_present(soup, adapter.out_stock_sel):
            meta.in_stock = False
        elif _sel_present(soup, adapter.in_stock_sel):
            meta.in_stock = True


# ---------------------------------------------------------------------------
# Embedded-JSON helpers (for stores that hydrate price/stock client-side)
# ---------------------------------------------------------------------------

def script_json(soup: BeautifulSoup, *, id: str | None = None,
                marker: str | None = None) -> object | None:
    """Decode the JSON body of a ``<script>`` tag.

    Match by element ``id`` (e.g. Next.js ``__NEXT_DATA__``) and/or by a
    substring ``marker`` in the script text (e.g. ``__APOLLO_STATE__``). Handles
    the common ``var x = {...};`` wrapper by slicing the first balanced object.
    Returns the decoded value, or ``None`` if nothing parsed.
    """
    for tag in soup.find_all("script"):
        if id is not None and tag.get("id") != id:
            continue
        raw = tag.string or tag.get_text() or ""
        if not raw.strip() or (marker is not None and marker not in raw):
            continue
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            sliced = _slice_object(raw)
            if sliced is not None:
                return sliced
    return None


def _slice_object(raw: str) -> object | None:
    """Parse the first balanced ``{...}`` in ``raw`` (for ``window.x = {...};``)."""
    start = raw.find("{")
    if start < 0:
        return None
    depth, in_str, esc, quote = 0, False, False, ""
    for i in range(start, len(raw)):
        c = raw[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
        elif c in "\"'":
            in_str, quote = True, c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def deep_find(obj: object, key: str) -> object | None:
    """First value for ``key`` found anywhere in a nested dict/list, else ``None``.

    Breadth-first so a shallow, page-level node (the main product) is reached
    before a deeply-nested one (a related item / variant).
    """
    queue = [obj]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            if key in node:
                return node[key]
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return None
