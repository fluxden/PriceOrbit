from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.tag import product_tags

if TYPE_CHECKING:
    from app.models.alert_rule import AlertRule
    from app.models.product_url import ProductURL
    from app.models.tag import Tag


class ImportStatus:
    PENDING = "pending"
    IMPORTING = "importing"
    IMPORTED = "imported"
    FAILED = "failed"
    ALL = (PENDING, IMPORTING, IMPORTED, FAILED)


class ScheduleKind:
    INTERVAL = "interval"  # every N minutes (check_interval_minutes)
    DAILY = "daily"        # once per day at daily_check_time (HH:MM)
    ALL = (INTERVAL, DAILY)


class Product(Base):
    """A logical product, which may be sold at several stores (ProductURLs)."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    model_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_favorite: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )

    # Which monitor categories this product belongs to.
    track_price: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    track_stock: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )

    # Schedule (applies to all of the product's store listings).
    schedule_kind: Mapped[str] = mapped_column(
        String(16), default="interval", server_default=text("'interval'"), nullable=False
    )
    check_interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_check_time: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "HH:MM"

    # Optional desired price (drives the "below target" alert + progress hint).
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    # Metadata import lifecycle.
    import_status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default=text("'pending'"), nullable=False
    )
    import_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    urls: Mapped[list["ProductURL"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    alert_rules: Mapped[list["AlertRule"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    tags: Mapped[list["Tag"]] = relationship(
        secondary=product_tags, back_populates="products"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Product id={self.id} name={self.name!r}>"
