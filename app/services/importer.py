"""First-pass product metadata importer.

Extracts name, image, model, description, price, currency, and stock from a
product page using structured data, in priority order:

  1. JSON-LD (schema.org Product)        - most reliable when present
  2. Microdata (schema.org itemprop)     - common on older catalogs
  3. OpenGraph / product meta tags       - widely available
  4. Platform adapters (WooCommerce / Shopify / Magento) - per-platform selectors
  5. Generic CSS selectors + text regex  - last-resort price / stock
  6. <title> fallback for the name

Each layer only fills gaps left by the previous one, so the order above is the
precedence. The HTML parsing (:func:`extract_metadata`) is separated from the
network fetch (:func:`import_from_url`) so it can be unit-tested offline, and the
value-normalization helpers (price / currency / availability) are pure functions.
A headless-browser fallback for fully JS-rendered stores is documented but not
bundled; this gets the large majority of static and server-rendered pages right.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.config import settings
from app.logsetup import TRACE
from app.services.politeness import (
    allowed_by_robots,
    before_fetch,
    domain_of,
    engine_order,
    http_get,
    scrapedo_active,
    scrapedo_render_enabled,
)

log = logging.getLogger("importer")

_AVAIL_IN = ("instock", "in_stock", "limitedavailability", "presale", "preorder",
             "backorder", "onlineonly", "instoreonly", "available")
_AVAIL_OUT = ("outofstock", "out_of_stock", "soldout", "sold_out", "discontinued",
              "unavailable", "notify", "comingsoon")


@dataclass
class ProductMetadata:
    ok: bool = False
    blocked: bool = False  # page was an anti-bot challenge, not the product page
    error: str | None = None
    name: str | None = None
    image_url: str | None = None
    model_number: str | None = None
    description: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    # in_stock is the ONLINE availability; instore_in_stock is the separate
    # in-store signal, left None unless a per-store adapter can read one.
    in_stock: bool | None = None
    instore_in_stock: bool | None = None
    store_name: str | None = None
    icon_url: str | None = None
    # Fetch engine that produced this result ("impersonate" | "httpx" |
    # "scrapedo"). "scrapedo" means the paid API was hit and credits were spent.
    engine: str | None = None


def parse_price(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    s = str(value).strip()
    if not s:
        return None
    s = re.sub(r"[^0-9.,]", "", s)
    if "," in s and "." in s:
        # Assume the last separator is the decimal point.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # Single comma: decimal if it looks like ",dd", else thousands.
        if re.search(r",\d{2}$", s):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _availability_to_stock(value) -> bool | None:
    if value is None:
        return None
    s = str(value).lower()
    if any(tok in s for tok in _AVAIL_OUT):
        return False
    if any(tok in s for tok in _AVAIL_IN):
        return True
    return None


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _image_url(value) -> str | None:
    value = _first(value)
    if isinstance(value, dict):
        return value.get("url")
    return value if isinstance(value, str) else None


def _model_value(value) -> str | None:
    """schema.org ``model`` may be plain text or a nested ProductModel object."""
    value = _first(value)
    if isinstance(value, dict):
        return value.get("name") or value.get("model") or value.get("value")
    return value if isinstance(value, str) else None


def _iter_jsonld_products(data):
    """Yield schema.org Product (and ProductGroup) dicts from nested JSON-LD.

    Descends into ``@graph`` and into ``ProductGroup.hasVariant`` so stores that
    model a configurable product as a group of variant Products (e.g. Bambu Lab,
    many modern Shopify/Hydrogen storefronts) still expose price/availability via
    a variant. The ProductGroup is yielded before its variants so its cleaner
    group-level name wins, while the first variant supplies the price/stock; the
    variant order is preserved so the page's default (first) variant is used.
    """
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            if "@graph" in node:
                stack.extend(node["@graph"] if isinstance(node["@graph"], list) else [node["@graph"]])
            variants = node.get("hasVariant")
            if variants:
                # reversed: LIFO pop then restores document order (first variant first)
                stack.extend(reversed(variants) if isinstance(variants, list) else [variants])
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(isinstance(x, str) and x.lower() in ("product", "productgroup") for x in types):
                yield node


def _from_jsonld(soup: BeautifulSoup, meta: ProductMetadata) -> None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for product in _iter_jsonld_products(data):
            meta.name = meta.name or product.get("name")
            meta.image_url = meta.image_url or _image_url(product.get("image"))
            meta.model_number = (meta.model_number or _model_value(product.get("model"))
                                 or product.get("mpn") or product.get("sku"))
            desc = product.get("description")
            if desc and not meta.description:
                meta.description = str(desc)[:2000]
            offers = _first(product.get("offers"))
            if isinstance(offers, dict):
                # Price/currency may sit directly on the Offer or in a nested
                # priceSpecification (schema.org allows both; e.g. store.ui.com).
                spec = _first(offers.get("priceSpecification"))
                spec = spec if isinstance(spec, dict) else {}
                if meta.price is None:
                    meta.price = parse_price(
                        offers.get("price") or offers.get("lowPrice")
                        or spec.get("price") or spec.get("minPrice"))
                meta.currency = (meta.currency or offers.get("priceCurrency")
                                 or spec.get("priceCurrency"))
                if meta.in_stock is None:
                    meta.in_stock = _availability_to_stock(offers.get("availability"))
            if meta.name and meta.price is not None:
                return


def _meta_content(soup: BeautifulSoup, *keys: str) -> str | None:
    for key in keys:
        tag = soup.find("meta", attrs={"property": key}) or soup.find(
            "meta", attrs={"name": key}
        )
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def _from_opengraph(soup: BeautifulSoup, meta: ProductMetadata) -> None:
    meta.name = meta.name or _meta_content(soup, "og:title", "twitter:title")
    meta.image_url = meta.image_url or _meta_content(soup, "og:image", "twitter:image")
    meta.description = meta.description or _meta_content(soup, "og:description", "description")
    if meta.price is None:
        meta.price = parse_price(
            _meta_content(soup, "product:price:amount", "og:price:amount", "twitter:data1")
        )
    meta.currency = meta.currency or _meta_content(
        soup, "product:price:currency", "og:price:currency"
    )
    if meta.in_stock is None:
        meta.in_stock = _availability_to_stock(
            _meta_content(soup, "product:availability", "og:availability")
        )


# ---------------------------------------------------------------------------
# Currency + free-text price helpers (pure; unit-tested offline)
# ---------------------------------------------------------------------------

# Symbol -> ISO. Ambiguous symbols ($, ¥, kr) map to the most common currency;
# an explicit ISO code in the data always wins over a symbol guess.
_CURRENCY_SYMBOLS = [
    ("US$", "USD"), ("C$", "CAD"), ("A$", "AUD"), ("R$", "BRL"), ("NZ$", "NZD"),
    ("zł", "PLN"), ("Fr", "CHF"), ("kr", "SEK"), ("$", "USD"), ("£", "GBP"),
    ("€", "EUR"), ("¥", "JPY"), ("₹", "INR"),
]
_ISO_RE = re.compile(r"\b([A-Z]{3})\b")


def normalize_currency(value) -> str | None:
    """Map a currency code or symbol to a 3-letter ISO code (best effort)."""
    if not value:
        return None
    s = str(value).strip()
    if re.fullmatch(r"[A-Za-z]{3}", s):
        return s.upper()
    for sym, code in _CURRENCY_SYMBOLS:
        if sym in s:
            return code
    return None


def currency_from_text(text) -> str | None:
    if not text:
        return None
    for sym, code in _CURRENCY_SYMBOLS:
        if sym in text:
            return code
    m = _ISO_RE.search(text)
    return m.group(1) if m else None


_PRICE_RE = re.compile(
    r"(?:US\$|C\$|A\$|R\$|NZ\$|[$£€¥₹]|zł|kr|Fr)?\s*"
    r"(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{2})?|\d+(?:[.,]\d{1,2})?)"
)


def price_from_text(text):
    """Parse the first price-looking number from free text -> (Decimal|None, cur|None)."""
    if not text:
        return None, None
    m = _PRICE_RE.search(text)
    if not m:
        return None, None
    return parse_price(m.group(1)), currency_from_text(text)


# ---------------------------------------------------------------------------
# Microdata (schema.org itemprop)
# ---------------------------------------------------------------------------

def _itemprop(soup: BeautifulSoup, prop: str) -> str | None:
    el = soup.find(attrs={"itemprop": prop})
    if not el:
        return None
    if el.get("content"):
        return el["content"]
    if el.name in ("link", "a") and el.get("href"):
        return el["href"]
    return el.get_text(" ", strip=True) or None


def _from_microdata(soup: BeautifulSoup, meta: ProductMetadata) -> None:
    meta.name = meta.name or _itemprop(soup, "name")
    meta.image_url = meta.image_url or _itemprop(soup, "image")
    meta.model_number = (meta.model_number or _itemprop(soup, "model")
                         or _itemprop(soup, "mpn") or _itemprop(soup, "sku"))
    if meta.description is None:
        desc = _itemprop(soup, "description")
        if desc:
            meta.description = desc[:2000]
    if meta.price is None:
        meta.price = parse_price(_itemprop(soup, "price") or _itemprop(soup, "lowPrice"))
    meta.currency = meta.currency or normalize_currency(_itemprop(soup, "priceCurrency"))
    if meta.in_stock is None:
        meta.in_stock = _availability_to_stock(_itemprop(soup, "availability"))


# ---------------------------------------------------------------------------
# Shared DOM / selector helpers + generic fallback
#
# The per-store adapter recipes that lean on these helpers live in
# ``app.services.site_adapters`` and are pulled in lazily by ``extract_metadata``
# (a deferred import keeps the dependency one-directional and cycle-free).
# ---------------------------------------------------------------------------

def _select_one(soup, sel):
    try:
        return soup.select_one(sel)
    except Exception:  # noqa: BLE001 - tolerate exotic selectors
        return None


def _sel_text(soup, selectors):
    for sel in selectors:
        el = _select_one(soup, sel)
        if el:
            txt = el.get("content") or el.get_text(" ", strip=True)
            if txt:
                return txt.strip()
    return None


def _sel_price(soup, selectors, attr):
    for sel in selectors:
        el = _select_one(soup, sel)
        if not el:
            continue
        if attr and el.get(attr):
            p = parse_price(el[attr])
            if p is not None:
                return p, None
        raw = el.get("content") or el.get_text(" ", strip=True)
        p = parse_price(raw)
        if p is not None:
            return p, currency_from_text(raw if isinstance(raw, str) else "")
    return None, None


def _sel_present(soup, selectors) -> bool:
    return any(_select_one(soup, sel) for sel in selectors)


def _img_from_dynamic(value: str) -> str | None:
    """Amazon ``data-a-dynamic-image`` is a JSON map of ``url -> [w, h]``; pick the
    highest-resolution URL."""
    try:
        mapping = json.loads(value)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(mapping, dict) or not mapping:
        return None

    def _area(item):
        dims = item[1]
        if isinstance(dims, list) and len(dims) == 2:
            try:
                return int(dims[0]) * int(dims[1])
            except (TypeError, ValueError):
                return 0
        return 0

    return max(mapping.items(), key=_area)[0]


def _img_from_srcset(value: str) -> str | None:
    """First URL from a ``srcset`` / ``data-srcset`` candidate list."""
    first = value.split(",")[0].strip()
    return first.split()[0] if first else None


# Attribute precedence when reading an <img>/<meta>/<link>: full-res hi-res first,
# then lazy-load data-* attrs, then the plain src/href/content.
_IMG_ATTRS = ("data-old-hires", "src", "data-src", "data-image", "content", "href")


def _element_image(el) -> str | None:
    """Best image URL from a single element across the common attribute carriers."""
    if el is None:
        return None
    dyn = el.get("data-a-dynamic-image")
    if dyn:
        url = _img_from_dynamic(dyn)
        if url:
            return url
    for attr in _IMG_ATTRS:
        v = el.get(attr)
        if v and v.strip() and not v.strip().startswith("data:"):
            return v.strip()
    srcset = el.get("srcset") or el.get("data-srcset")
    if srcset:
        return _img_from_srcset(srcset)
    return None


def _sel_image(soup, selectors) -> str | None:
    for sel in selectors:
        url = _element_image(_select_one(soup, sel))
        if url:
            return url
    return None


_GENERIC_PRICE_SEL = (
    "meta[itemprop='price']", "[itemprop='price']", "[data-price]", "[data-product-price]",
    ".product-price", ".current-price", ".sale-price", ".price", "#price", ".price-tag",
)
_GENERIC_IMAGE_SEL = (
    "link[rel='image_src']", "[itemprop='image']", "#main-image img", ".product-image img",
    ".product__image img", ".product-media img", ".product-gallery img",
    ".product-single__photo img", "figure.product img", ".gallery img",
)
_BUY_TOKENS = ("add to cart", "add to bag", "add to basket", "add to trolley", "buy now")
_OOS_TOKENS = ("sold out", "out of stock", "notify me", "email when available",
               "currently unavailable", "coming soon")

# An enabled primary buy CTA — the strongest "available (online)" signal.
_BUY_CTA_SEL = (
    "#add-to-cart-button:not([disabled])", "#buy-now-button:not([disabled])",
    "form[action*='/cart/add'] [type='submit']:not([disabled])",
    "button[name='add']:not([disabled])", "[data-testid='add-to-cart']:not([disabled])",
    "button.add-to-cart-button:not([disabled])", ".product-form__submit:not([disabled])",
    ".add-to-cart:not([disabled])",
)
# A disabled / sold-out primary CTA — a scoped negative signal.
_OOS_CTA_SEL = (
    "#add-to-cart-button[disabled]", "#buy-now-button[disabled]",
    "button[name='add'][disabled]", "button.add-to-cart-button[disabled]",
    ".product-form__buttons [disabled]", ".sold-out",
)
# Elements that carry a *scoped* availability statement (not the whole page).
_AVAIL_SCOPE_SEL = (
    "[itemprop='availability']", ".availability", ".product-availability",
    ".availability-message", ".stock", "[data-stock-status]",
)


def _text_stock(soup: BeautifulSoup) -> bool | None:
    """Infer stock conservatively when nothing structured said so.

    Order matters: a present, *enabled* add-to-cart CTA is the strongest positive
    signal and wins outright. Negative signals are only trusted when scoped to the
    buy box / a dedicated availability element — never a page-wide button sweep,
    which used to flip the whole product OOS off an unrelated "notify me" /
    "out of stock" widget (store-pickup modules, recommended carousels, variants).
    Returns ``None`` (unknown) rather than guessing when no scoped signal is found.
    """
    if any(_select_one(soup, sel) for sel in _BUY_CTA_SEL):
        return True
    if any(_select_one(soup, sel) for sel in _OOS_CTA_SEL):
        return False
    for sel in _AVAIL_SCOPE_SEL:
        el = _select_one(soup, sel)
        if not el:
            continue
        txt = el.get_text(" ", strip=True).lower()
        if not txt:
            continue
        if any(tok in txt for tok in _OOS_TOKENS):
            return False
        if any(tok in txt for tok in _BUY_TOKENS):
            return True
    return None


# Matches a spec-table label cell that holds a manufacturer model number, e.g.
# "Model", "Model #", "Model Number", "Manufacturer Part Number", "Mfr Part No".
# Anchored so only a label-only cell matches (not a paragraph starting "Model...").
_MODEL_LABEL_RE = re.compile(
    r"^\s*(?:mfr\.?\s*)?(?:manufacturer\s*)?(?:model|part)"
    r"\s*(?:#|no\.?|number|name)?\s*:?\s*$",
    re.I,
)


def _from_spec_table(soup: BeautifulSoup, meta: ProductMetadata) -> None:
    """Last-resort model number from a spec table/list (Home Depot, Best Buy, …).

    Finds a label cell whose whole text is a model label and takes the adjacent
    value element. Covers ``th/td``, ``dt/dd`` and ``div/span`` label-value pairs
    where the model lives only in an unstructured spec section.
    """
    if meta.model_number:
        return
    for label in soup.find_all(("th", "dt", "td", "div", "span", "li")):
        txt = label.get_text(" ", strip=True)
        if not txt or len(txt) > 40 or not _MODEL_LABEL_RE.match(txt):
            continue
        sib = label.find_next_sibling()
        if not sib:
            continue
        val = sib.get_text(" ", strip=True)
        if val and val.lower() != txt.lower() and 1 < len(val) <= 120:
            meta.model_number = val
            return


def _from_selectors(soup: BeautifulSoup, meta: ProductMetadata) -> None:
    """Generic, platform-agnostic last-resort price + stock."""
    if meta.price is None:
        for sel in _GENERIC_PRICE_SEL:
            el = _select_one(soup, sel)
            if not el:
                continue
            raw = el.get("content") or el.get("data-price") or el.get("data-product-price") \
                or el.get_text(" ", strip=True)
            price = parse_price(raw)
            if price is not None:
                meta.price = price
                if meta.currency is None and isinstance(raw, str):
                    meta.currency = currency_from_text(raw)
                break
    if not meta.image_url:
        meta.image_url = _sel_image(soup, _GENERIC_IMAGE_SEL)
    if meta.in_stock is None:
        meta.in_stock = _text_stock(soup)


# Anti-bot interstitials that some stores serve with HTTP 200 in place of the
# product page: Walmart's PerimeterX "Robot or Human?", and the Cloudflare /
# Incapsula / DataDome equivalents. Without detecting these, the <title> fallback
# below grabs the challenge heading ("Robot or Human?") as the product name,
# meta.ok flips True, and import_from_url stops on that junk instead of escalating
# to the next engine / scrape.do.
_BLOCK_TITLE_RE = re.compile(
    r"robot or human|are you (?:a )?(?:human|robot)|access denied|"
    r"attention required|pardon our interruption|just a moment|security check|"
    r"verify you are (?:a )?human|request unsuccessful",
    re.I,
)
_BLOCK_MARKERS = (
    "px-captcha", "/_px/", "perimeterx",          # PerimeterX (Walmart)
    "cf-chl", "cf_chl", "challenge-platform",       # Cloudflare
    "_incapsula_resource", "incapsula incident",    # Imperva / Incapsula
    "captcha-delivery.com", "datadome",             # DataDome
)


def _is_block_page(soup: BeautifulSoup, html: str, meta: ProductMetadata) -> bool:
    """True when the HTML is an anti-bot challenge rather than a product page.

    Title match is high-signal on its own. Body markers only count when no real
    price was found, so a legit page that merely embeds a CAPTCHA widget elsewhere
    is not misflagged.
    """
    title = soup.title.string.strip() if (soup.title and soup.title.string) else ""
    if title and _BLOCK_TITLE_RE.search(title):
        return True
    if meta.price is None:
        low = html.lower()
        return any(tok in low for tok in _BLOCK_MARKERS)
    return False


def extract_metadata(html: str, url: str) -> ProductMetadata:
    """Pure parser — no network. Returns best-effort product metadata."""
    meta = ProductMetadata(store_name=domain_of(url))
    soup = BeautifulSoup(html or "", "html.parser")

    _from_jsonld(soup, meta)
    _from_microdata(soup, meta)
    _from_opengraph(soup, meta)

    # Per-store adapter library (lazy import breaks the import cycle: the adapter
    # modules reuse this module's pure helpers).
    from app.services.site_adapters import apply_adapter, match_adapter

    adapter = match_adapter(domain_of(url), soup)
    if adapter is not None:
        apply_adapter(adapter, soup, meta)

    _from_selectors(soup, meta)
    _from_spec_table(soup, meta)

    if not meta.name and soup.title and soup.title.string:
        meta.name = soup.title.string.strip()[:300]

    # Site icon for the Tracked Stores box (resolved to an absolute URL).
    icon_href = None
    for rel in ("icon", "shortcut icon", "apple-touch-icon", "apple-touch-icon-precomposed"):
        link = soup.find("link", attrs={"rel": lambda v, r=rel: v and r in " ".join(v).lower() if isinstance(v, list) else (v and r in v.lower())})
        if link and link.get("href"):
            icon_href = link["href"]
            break
    meta.icon_url = urljoin(url, icon_href) if icon_href else urljoin(url, "/favicon.ico")

    if meta.name:
        meta.name = re.sub(r"\s+", " ", meta.name).strip()
    if meta.image_url:
        meta.image_url = urljoin(url, meta.image_url)
    if meta.currency:
        meta.currency = (normalize_currency(meta.currency) or meta.currency.strip().upper())[:8]

    if _is_block_page(soup, html or "", meta):
        meta.blocked = True
        meta.ok = False
        meta.error = "The store served an anti-bot challenge instead of the product page."
        return meta

    meta.ok = bool(meta.name)
    if not meta.ok:
        meta.error = "Could not find product details on the page."
    return meta


# Statuses that mean an anti-bot / CDN edge refused us rather than the origin
# answering. 502/504 and Cloudflare's 520-527 are gateway failures retailers like
# Best Buy (Akamai) return to scrapers — treat them as "blocked" so we fall
# through to the next engine (scrape.do) and surface the right guidance, not a
# bare "HTTP 502".
_BLOCK_STATUS = (401, 403, 429, 503, 502, 504, 520, 521, 522, 523, 524, 525, 526, 527)


def import_from_url(url: str, *, polite: bool = True) -> ProductMetadata:
    """Fetch ``url`` and extract metadata. Network-dependent.

    Tries each fetch engine in order (browser-impersonating curl_cffi first,
    then httpx) and returns the first response that yields usable product
    details, so a site blocking one engine's fingerprint can still be read by
    the other. ``polite=False`` skips the per-domain rate-limit + jitter (used by
    the interactive "Add product" flow); the scheduler keeps the default.
    """
    dom = domain_of(url)
    if settings.respect_robots and not allowed_by_robots(url):
        return ProductMetadata(ok=False, error="Blocked by robots.txt", store_name=dom)
    if polite:
        before_fetch(url)

    blocked: int | None = None
    challenged = False  # a 200 anti-bot interstitial (not an HTTP block status)
    http_err: int | None = None
    last_exc: Exception | None = None

    def _fetch(engine: str, render: bool | None = None):
        """Run one engine and classify the response. Returns a usable FetchResult
        or None, recording the failure reason (block / http error / exception) so
        the final message can explain what happened."""
        nonlocal blocked, http_err, last_exc
        label = engine + (" +render" if render else "")
        try:
            resp = http_get(url, engine=engine, render=render)
        except Exception as exc:  # noqa: BLE001 - try the next engine, then report
            log.debug("engine %s failed for %s: %s", label, dom, exc)
            last_exc = exc
            return None
        log.log(TRACE, "engine %s -> HTTP %s (%d bytes) for %s",
                label, resp.status_code, len(resp.text or ""), dom)
        if resp.status_code in _BLOCK_STATUS:
            blocked = resp.status_code
            return None
        if resp.status_code >= 400:
            http_err = http_err or resp.status_code
            return None
        return resp

    def _scrapedo_meta():
        """scrape.do no-render first, escalating to a headless render only when the
        cheap fetch found no price (a genuinely JS-rendered store). Falls back to a
        name-only result if neither attempt yields a price."""
        nonlocal challenged
        best = None
        for render in ([None, True] if scrapedo_render_enabled() else [None]):
            resp = _fetch("scrapedo", render=render)
            if resp is None:
                continue
            m = extract_metadata(resp.text, url)
            if m.blocked:
                challenged = True
                continue
            if m.ok and m.price is not None:
                return m
            if m.ok and best is None:
                best = m
        return best

    for engine in engine_order():
        if engine == "scrapedo":
            meta = _scrapedo_meta()
        else:
            resp = _fetch(engine)
            meta = extract_metadata(resp.text, url) if resp is not None else None
        if meta is not None and meta.blocked:
            challenged = True
            log.debug("import: %s served an anti-bot challenge via %s", dom, engine)
            meta = None  # don't accept a challenge page; try the next engine
        if meta is not None and meta.ok:
            meta.engine = engine
            log.debug("import ok via %s: %r price=%s %s", engine, meta.name, meta.price, meta.currency)
            return meta  # has at least a name; good enough to add + monitor

    # When the free engines can't read a store and scrape.do isn't active,
    # point the user at it — that's the supported way to handle anti-bot retailers.
    scrapedo_on = scrapedo_active()
    configure_hint = (
        "" if scrapedo_on else
        " To import from anti-bot-protected retailers like this one, enable the"
        " scrape.do API on the Settings page."
    )

    if blocked is not None or challenged:
        where = f"HTTP {blocked}" if blocked is not None else "an anti-bot challenge page"
        msg = f"The store blocked automated access ({where})."
        if scrapedo_on:
            msg += " scrape.do could not get past it either."
        return ProductMetadata(ok=False, store_name=dom, error=msg + configure_hint)
    if http_err is not None:
        return ProductMetadata(ok=False, store_name=dom,
                               error=f"The store returned HTTP {http_err}." + configure_hint)
    if last_exc is not None:
        return ProductMetadata(ok=False, error=f"Could not fetch the page: {last_exc}", store_name=dom)
    return ProductMetadata(
        ok=False, store_name=dom,
        error=("Could not find product details on the page. The store may render "
               "prices with JavaScript." + configure_hint))
