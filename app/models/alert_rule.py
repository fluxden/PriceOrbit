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
    from app.models.alert_account import AlertAccount
    from app.models.product import Product


class AlertType:
    """Supported alert rule types (stored as strings)."""

    PRICE_DROP_ANY = "price_drop_any"          # any decrease vs the previous price
    PRICE_DROP_AMOUNT = "price_drop_amount"    # decrease of >= threshold (currency)
    PRICE_DROP_PERCENT = "price_drop_percent"  # decrease of >= threshold percent
    PRICE_BELOW_TARGET = "price_below_target"  # price <= threshold
    PRICE_INCREASE_ANY = "price_increase_any"  # any increase vs the previous price
    PRICE_INCREASE_AMOUNT = "price_increase_amount"    # increase of >= threshold (currency)
    PRICE_INCREASE_PERCENT = "price_increase_percent"  # increase of >= threshold percent
    PRICE_CHANGE_ANY = "price_change_any"      # any change up or down vs the previous price
    BACK_IN_STOCK = "back_in_stock"            # stock transitions to available
    OUT_OF_STOCK = "out_of_stock"              # stock transitions to unavailable
    STOCK_CHANGE_ANY = "stock_change_any"      # stock toggles in either direction

    ALL = (
        PRICE_DROP_ANY, PRICE_DROP_AMOUNT, PRICE_DROP_PERCENT, PRICE_BELOW_TARGET,
        PRICE_INCREASE_ANY, PRICE_INCREASE_AMOUNT, PRICE_INCREASE_PERCENT, PRICE_CHANGE_ANY,
        BACK_IN_STOCK, OUT_OF_STOCK, STOCK_CHANGE_ANY,
    )


class AlertChannel:
    """Supported notification channels (stored as strings)."""

    EMAIL = "email"
    TELEGRAM = "telegram"
    SOUND = "sound"  # in-app browser sound (no external account)

    ALL = (EMAIL, TELEGRAM, SOUND)


class AlertRule(Base):
    """A user-defined rule that triggers notifications for a product."""

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Meaning depends on type: target price (PRICE_BELOW_TARGET) or
    # percentage (PRICE_DROP_PERCENT). Unused for the other types.
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    # The configured destination this rule notifies (optional until set up).
    alert_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    cooldown_minutes: Mapped[int] = mapped_column(
        Integer, default=360, server_default=text("360"), nullable=False
    )
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_notified_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    product: Mapped["Product"] = relationship(back_populates="alert_rules")
    account: Mapped["AlertAccount | None"] = relationship(back_populates="rules")
