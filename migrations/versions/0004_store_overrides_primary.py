"""primary listing, cached favicon, and per-store schedule overrides

Revision ID: 0004_store_overrides_primary
Revises: 0003_monitoring_alert_accounts
Create Date: 2026-06-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0004_store_overrides_primary"
down_revision = "0003_monitoring_alert_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_urls", sa.Column("is_primary", sa.Boolean(), server_default=sa.text("0"), nullable=False))
    op.add_column("product_urls", sa.Column("favicon_url", sa.String(length=1024), nullable=True))
    op.add_column("product_urls", sa.Column("schedule_kind", sa.String(length=16), nullable=True))
    op.add_column("product_urls", sa.Column("daily_check_time", sa.String(length=5), nullable=True))

    # Mark the earliest listing of each product as its primary store.
    op.execute(
        "UPDATE product_urls p "
        "JOIN (SELECT product_id, MIN(id) AS mid FROM product_urls GROUP BY product_id) m "
        "ON p.id = m.mid SET p.is_primary = 1"
    )


def downgrade() -> None:
    for col in ("daily_check_time", "schedule_kind", "favicon_url", "is_primary"):
        op.drop_column("product_urls", col)
