from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.price_history import PriceHistory
    from app.models.product import Product


class ProductURL(Base):
    """A single store listing for a product, with its own check schedule."""

    __tablename__ = "product_urls"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    adapter: Mapped[str | None] = mapped_column(String(128), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    check_interval_minutes: Mapped[int] = mapped_column(
        Integer, default=60, server_default=text("60"), nullable=False
    )
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    # The primary listing defines the product's identity (name/image/description).
    is_primary: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    favicon_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Per-store schedule override. When schedule_kind is NULL the store inherits
    # the product's schedule; otherwise it uses these fields.
    schedule_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    daily_check_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    # Price recorded when the listing was first tracked (used for the
    # "change since added" badge on the overview).
    baseline_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Monitoring health: last_checked_at is the last SUCCESS; these track attempts/failures.
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Fetch engine that satisfied the last successful check ("impersonate" |
    # "httpx" | "scrapedo"). "scrapedo" drives the paid-API badge on the cards.
    last_engine: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    last_in_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    product: Mapped["Product"] = relationship(back_populates="urls")
    price_history: Mapped[list["PriceHistory"]] = relationship(
        back_populates="product_url", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ProductURL id={self.id} domain={self.domain!r}>"
