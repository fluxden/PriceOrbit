from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.alert_rule import AlertRule


class AlertAccount(Base):
    """A configured notification destination the user can attach to alerts.

    The sending credentials (SMTP server, bot token) live in settings/env; this
    record names a destination (an email recipient or a Telegram chat) and the
    channel it uses. Created/managed on the Alerts configuration page.
    """

    __tablename__ = "alert_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # email | telegram
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    destination: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    # Set when a test send succeeds — drives the "verified" badge.
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Optional backup account tried when this one fails to send.
    fallback_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_accounts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    rules: Mapped[list["AlertRule"]] = relationship(back_populates="account")
    fallback: Mapped["AlertAccount | None"] = relationship(
        "AlertAccount", remote_side="AlertAccount.id", uselist=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AlertAccount id={self.id} {self.channel}:{self.label!r}>"
