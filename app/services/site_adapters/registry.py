"""The adapter registry — declarative per-store / per-platform recipes.

A growing fallback library for stores the generic structured-data layers can't
read on their own. Add an entry here only when a store pulls wrong; most stores
never need one. Domain-matched adapters (Amazon, Best Buy, Walmart, Home Depot)
win over platform-detected ones (WooCommerce / Shopify / Magento), so order
matters: specific stores first, generic platforms last.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from app.services.importer import _select_one
from app.services.site_adapters import homedepot, walmart
from app.services.site_adapters.base import SiteAdapter


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
        name="Walmart",
        domains=("walmart.com", "walmart.ca"),
        extract=walmart.extract,
        name_sel=("h1[itemprop='name']", "h1#main-title", "h1.prod-ProductTitle", "h1"),
        image_sel=("img[data-testid='hero-image']", ".hover-zoom-hero-image img"),
    ),
    SiteAdapter(
        name="Home Depot",
        domains=("homedepot.com", "homedepot.ca"),
        extract=homedepot.extract,
        name_sel=("h1.product-details__title", "h1[class*='product-title']",
                  "h1.sui-h4-bold", "h1"),
        image_sel=("img.mediagallery__mainimage", "img[data-testid='product-image']"),
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


def match_adapter(host: str, soup: BeautifulSoup) -> SiteAdapter | None:
    """First adapter whose domain or detector matches this page, else ``None``."""
    for adapter in ADAPTERS:
        if adapter.matches(host, soup):
            return adapter
    return None
