"""Populate the database with sample products for previewing the UI.

Run inside Docker:
    docker compose run --rm web python -m app.seed

This clears existing products/tags and inserts a small, varied sample set:
price drops and rises, multi-store products, an out-of-stock item, a freshly
restocked item, favorites, and tags.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete

from app.database import SessionLocal
from app.models import (
    AlertRule,
    NotificationLog,
    PriceHistory,
    Product,
    ProductURL,
    Tag,
    product_tags,
)

NOW = datetime.utcnow()


def _history(url: ProductURL, points: list[tuple[int, str, bool]]) -> None:
    """points: list of (days_ago, price_str_or_empty, in_stock)."""
    for days_ago, price, in_stock in points:
        url.price_history.append(
            PriceHistory(
                price=Decimal(price) if price else None,
                currency=url.currency,
                in_stock=in_stock,
                checked_at=NOW - timedelta(days=days_ago),
            )
        )


def reset(db) -> None:
    db.execute(delete(NotificationLog))
    db.execute(delete(AlertRule))
    db.execute(delete(PriceHistory))
    db.execute(delete(product_tags))
    db.execute(delete(ProductURL))
    db.execute(delete(Product))
    db.execute(delete(Tag))
    db.commit()


def seed() -> None:
    db = SessionLocal()
    try:
        reset(db)

        tags = {
            "GPUs": Tag(name="GPUs", color="#6d5ae6"),
            "Audio": Tag(name="Audio", color="#0e9f6e"),
            "Displays": Tag(name="Displays", color="#0ea5e9"),
            "Gifts": Tag(name="Gifts", color="#e5484d"),
            "Home": Tag(name="Home", color="#b9760a"),
        }
        for t in tags.values():
            db.add(t)

        # 1) GPU — dropped, in stock, two stores, favorite
        p1 = Product(
            name="NVIDIA GeForce RTX 5080 Founders Edition",
            model_number="900-1G145",
            image_url=None,
            is_favorite=True,
            created_at=NOW - timedelta(days=30),
            track_price=True, import_status="imported",
            schedule_kind="interval", check_interval_minutes=15,
            target_price=Decimal("999.00"),
            tags=[tags["GPUs"]],
        )
        u1a = ProductURL(url="https://store-a.example.com/rtx-5080", domain="store-a.example.com",
                         store_name="store-a.example.com", currency="USD", baseline_price=Decimal("1199.00"),
                         last_price=Decimal("1049.99"), last_in_stock=True, last_checked_at=NOW)
        u1b = ProductURL(url="https://store-b.example.com/p/rtx5080", domain="store-b.example.com",
                         store_name="store-b.example.com", currency="USD", baseline_price=Decimal("1219.00"),
                         last_price=Decimal("1099.00"), last_in_stock=True, last_checked_at=NOW)
        _history(u1a, [(30, "1199.00", True), (14, "1149.00", True), (3, "1099.99", True), (0, "1049.99", True)])
        _history(u1b, [(30, "1219.00", True), (1, "1099.00", True)])
        p1.urls = [u1a, u1b]

        # 2) Headphones — rose slightly, in stock, favorite
        p2 = Product(
            name="Sony WH-1000XM6 Wireless Headphones",
            model_number="WH1000XM6/B",
            is_favorite=True,
            created_at=NOW - timedelta(days=21),
            track_price=True, import_status="imported", check_interval_minutes=60,
            tags=[tags["Audio"], tags["Gifts"]],
        )
        u2 = ProductURL(url="https://store-a.example.com/xm6", domain="store-a.example.com",
                        store_name="store-a.example.com", currency="USD", baseline_price=Decimal("399.99"),
                        last_price=Decimal("429.99"), last_in_stock=True, last_checked_at=NOW)
        _history(u2, [(21, "399.99", True), (7, "419.99", True), (0, "429.99", True)])
        p2.urls = [u2]

        # 3) Monitor — out of stock
        p3 = Product(
            name='LG UltraGear 27" OLED Gaming Monitor',
            model_number="27GS95QE",
            created_at=NOW - timedelta(days=12),
            track_price=True, track_stock=True, import_status="imported", check_interval_minutes=60,
            tags=[tags["Displays"]],
        )
        u3 = ProductURL(url="https://store-c.example.com/ultragear", domain="store-c.example.com",
                        store_name="store-c.example.com", currency="USD", baseline_price=Decimal("899.00"),
                        last_price=Decimal("849.00"), last_in_stock=False, last_checked_at=NOW)
        _history(u3, [(12, "899.00", True), (5, "849.00", True), (0, "849.00", False)])
        p3.urls = [u3]

        # 4) Coffee machine — freshly restocked (out -> in on last two points)
        p4 = Product(
            name="Breville Barista Express Espresso Machine",
            model_number="BES870XL",
            created_at=NOW - timedelta(days=40),
            track_price=True, import_status="imported",
            schedule_kind="daily", daily_check_time="08:00",
            tags=[tags["Home"], tags["Gifts"]],
        )
        u4 = ProductURL(url="https://store-b.example.com/barista-express", domain="store-b.example.com",
                        store_name="store-b.example.com", currency="USD", baseline_price=Decimal("699.95"),
                        last_price=Decimal("649.95"), last_in_stock=True, last_checked_at=NOW)
        _history(u4, [(40, "699.95", True), (10, "649.95", False), (0, "649.95", True)])
        p4.urls = [u4]

        # 5) Keyboard — flat price, in stock, EUR
        p5 = Product(
            name="Keychron Q1 Pro Mechanical Keyboard",
            model_number="Q1-Pro-QMK",
            created_at=NOW - timedelta(days=6),
            track_price=True, import_status="imported", check_interval_minutes=360,
        )
        u5 = ProductURL(url="https://store-d.example.eu/q1-pro", domain="store-d.example.eu",
                        store_name="store-d.example.eu", currency="EUR", baseline_price=Decimal("199.00"),
                        last_price=Decimal("199.00"), last_in_stock=True, last_checked_at=NOW)
        _history(u5, [(6, "199.00", True), (0, "199.00", True)])
        p5.urls = [u5]

        # 6) Pending quick-add (no price yet)
        p6 = Product(
            name="Pending — store-e.example.com",
            created_at=NOW - timedelta(hours=2),
            track_price=True, import_status="pending", check_interval_minutes=60,
        )
        u6 = ProductURL(url="https://store-e.example.com/some-product", domain="store-e.example.com",
                        store_name="store-e.example.com")
        p6.urls = [u6]

        # 7) Drone — big drop, in stock
        p7 = Product(
            name="DJI Mini 5 Pro Drone (Fly More Combo)",
            model_number="CP.MA.00000",
            created_at=NOW - timedelta(days=18),
            track_price=True, import_status="imported", check_interval_minutes=15,
            target_price=Decimal("749.00"),
            tags=[tags["Gifts"]],
        )
        u7 = ProductURL(url="https://store-a.example.com/mini-5-pro", domain="store-a.example.com",
                        store_name="store-a.example.com", currency="USD", baseline_price=Decimal("999.00"),
                        last_price=Decimal("799.00"), last_in_stock=True, last_checked_at=NOW)
        _history(u7, [(18, "999.00", True), (9, "899.00", True), (0, "799.00", True)])
        p7.urls = [u7]

        db.add_all([p1, p2, p3, p4, p5, p6, p7])
        db.commit()
        print("Seeded 7 sample products with history and tags.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
