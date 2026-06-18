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
)

log = logging.getLogger("importer")

_AVAIL_IN = ("instock", "in_stock", "limitedavailability", "presale", "preorder",
             "backorder", "onlineonly", "instoreonly", "available")
_AVAIL_OUT = ("outofstock", "out_of_stock", "soldout", "sold_out", "discontinued",
              "unavailable", "notify", "comingsoon")


@dataclass
class ProductMetadata:
    ok: bool = False
    error: str | None = None
    name: str | None = None
    image_url: str | None = None
    model_number: str | None = None
    description: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    in_stock: bool | None = None
    store_name: str | None = None
    icon_url: str | None = None


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
# Platform adapters (declarative per-platform selectors) + generic fallback
# ---------------------------------------------------------------------------

@dataclass
class SiteAdapter:
    name: str
    domains: tuple = ()
    detect: object = None  # optional callable(soup) -> bool
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


def _apply_adapter(adapter: SiteAdapter, soup: BeautifulSoup, meta: ProductMetadata) -> None:
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


def _detect_woocommerce(soup):
    return bool(_select_one(soup, "body.woocommerce, body.woocommerce-page, .woocommerce"))


def _detect_shopify(soup):
    if _select_one(soup, "form[action*='/cart/add']"):
        return True
    for s in soup.find_all("script"):
        if "Shopify" in (s.string or "") or "shopify" in (s.get("src") or ""):
            return True
    return False


def _detect_magento(soup):
    return bool(_select_one(soup, "[data-price-amount], body[class*='catalog-product']"))


ADAPTERS = [
    SiteAdapter(
        name="Amazon",
        domains=("amazon.",),
        name_sel=("#productTitle", "h1#title"),
        # Main gallery image. data-old-hires / data-a-dynamic-image carry the
        # full-res URL(s); plain src is a low-res / sprite fallback.
        image_sel=("#landingImage", "#imgBlkFront", "#ebooksImgBlkFront",
                   "#main-image", "#imgTagWrapperId img"),
        # Buy-box ids first so we don't grab a struck-through list price; the
        # full price text lives in the screen-reader span ".a-offscreen".
        price_sel=(
            "#corePrice_feature_div .a-price .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "#corePrice_desktop .a-price .a-offscreen",
            "#price_inside_buybox",
            "#priceblock_ourprice", "#priceblock_dealprice", "#priceblock_saleprice",
            "span.a-price span.a-offscreen", ".a-price .a-offscreen",
        ),
        in_stock_sel=("#add-to-cart-button", "#buy-now-button"),
        out_stock_sel=("#outOfStock", "#availability .a-color-state"),
    ),
    SiteAdapter(
        name="Best Buy",
        domains=("bestbuy.com", "bestbuy.ca"),
        name_sel=("h1.heading-5", ".sku-title h1", "h1[data-testid='product-title']"),
        image_sel=("img.primary-image", ".primary-image img", ".primary-image",
                   ".shop-media-gallery img", "img[data-testid='product-image']"),
        price_sel=(".priceView-hero-price span[aria-hidden='true']",
                   ".priceView-customer-price span[aria-hidden='true']",
                   ".priceView-hero-price span", ".priceView-customer-price span"),
        in_stock_sel=("button.add-to-cart-button:not([disabled])",),
        out_stock_sel=("button.add-to-cart-button[disabled]",
                       ".fulfillment-add-to-cart-button button[disabled]"),
    ),
    SiteAdapter(
        name="WooCommerce",
        detect=_detect_woocommerce,
        name_sel=("h1.product_title", ".product_title"),
        price_sel=("p.price ins .woocommerce-Price-amount", "p.price .woocommerce-Price-amount",
                   ".summary .price .woocommerce-Price-amount", ".woocommerce-Price-amount.amount"),
        in_stock_sel=("p.stock.in-stock", ".stock.in-stock"),
        out_stock_sel=("p.stock.out-of-stock", ".stock.out-of-stock"),
    ),
    SiteAdapter(
        name="Shopify",
        detect=_detect_shopify,
        name_sel=("h1.product__title", ".product__title", "h1.product-single__title"),
        price_sel=("[data-product-price]", ".price__regular .price-item--regular",
                   ".product__price", ".price-item--regular"),
        out_stock_sel=(".sold-out", "button[name='add'][disabled]", ".product-form__buttons [disabled]"),
        in_stock_sel=("form[action*='/cart/add'] [type='submit']", ".product-form__submit"),
    ),
    SiteAdapter(
        name="Magento",
        detect=_detect_magento,
        name_sel=("h1.page-title .base", "h1.page-title", "[data-ui-id='page-title-wrapper']"),
        price_sel=(".price-wrapper[data-price-amount]", "[data-price-type='finalPrice']", "[data-price-amount]"),
        price_attr="data-price-amount",
        in_stock_sel=(".stock.available", "div.available"),
        out_stock_sel=(".stock.unavailable", "div.unavailable"),
    ),
]


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


def _text_stock(soup: BeautifulSoup) -> bool | None:
    """Infer stock from button / badge text when nothing structured said so."""
    blob = " | ".join(
        el.get_text(" ", strip=True).lower()
        for el in soup.select("button, a.button, a.btn, [type='submit'], .availability, .stock")
        if el.get_text(strip=True)
    )
    if any(tok in blob for tok in _OOS_TOKENS):
        return False
    if any(tok in blob for tok in _BUY_TOKENS):
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


def extract_metadata(html: str, url: str) -> ProductMetadata:
    """Pure parser — no network. Returns best-effort product metadata."""
    meta = ProductMetadata(store_name=domain_of(url))
    soup = BeautifulSoup(html or "", "html.parser")

    _from_jsonld(soup, meta)
    _from_microdata(soup, meta)
    _from_opengraph(soup, meta)

    host = domain_of(url)
    for adapter in ADAPTERS:
        if adapter.matches(host, soup):
            _apply_adapter(adapter, soup, meta)
            break

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
    http_err: int | None = None
    last_exc: Exception | None = None
    for engine in engine_order():
        log.debug("import %s via %s", url, engine)
        try:
            resp = http_get(url, engine=engine)
        except Exception as exc:  # noqa: BLE001 - try the next engine, then report
            log.debug("engine %s failed for %s: %s", engine, dom, exc)
            last_exc = exc
            continue
        log.log(TRACE, "engine %s -> HTTP %s (%d bytes) for %s",
                engine, resp.status_code, len(resp.text or ""), dom)
        if resp.status_code in _BLOCK_STATUS:
            blocked = resp.status_code
            continue
        if resp.status_code >= 400:
            http_err = http_err or resp.status_code
            continue
        meta = extract_metadata(resp.text, url)
        if meta.ok:
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

    if blocked is not None:
        msg = f"The store blocked automated access (HTTP {blocked})."
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
