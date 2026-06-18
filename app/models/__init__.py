"""SQLAlchemy models.

Importing this package registers every table on ``Base.metadata`` so that both
the application and Alembic see the full schema.
"""
from app.database import Base
from app.models.alert_account import AlertAccount
from app.models.audit_event import AuditEvent
from app.models.alert_rule import AlertChannel, AlertRule, AlertType
from app.models.notification_log import NotificationLog
from app.models.price_history import PriceHistory
from app.models.product import ImportStatus, Product, ScheduleKind
from app.models.product_url import ProductURL
from app.models.setting import Setting
from app.models.tag import Tag, product_tags
from app.models.user import User

__all__ = [
    "Base",
    "Product",
    "ImportStatus",
    "ScheduleKind",
    "ProductURL",
    "PriceHistory",
    "AlertRule",
    "AlertType",
    "AlertChannel",
    "AlertAccount",
    "AuditEvent",
    "NotificationLog",
    "User",
    "Setting",
    "Tag",
    "product_tags",
]
