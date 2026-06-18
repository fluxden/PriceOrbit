from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Setting(Base):
    """Simple key/value store for app settings (auth toggle, SMTP, Telegram...).

    The column is named ``setting_key`` to avoid the reserved word ``key``.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column("setting_key", String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
