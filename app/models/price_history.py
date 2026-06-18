from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.product_url import ProductURL


class PriceHistory(Base):
    """One observation of price + stock for a store listing."""

    __tablename__ = "price_history"
    __table_args__ = (
        Index("ix_price_history_url_checked", "product_url_id", "checked_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_url_id: Mapped[int] = mapped_column(
        ForeignKey("product_urls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    in_stock: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )

    product_url: Mapped["ProductURL"] = relationship(back_populates="price_history")
