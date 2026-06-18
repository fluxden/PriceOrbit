"""per-listing monitoring health (attempt time, failures, last error)

Revision ID: 0011_url_health
Revises: 0010_notification_seen
Create Date: 2026-06-15

"""
from alembic import op
import sqlalchemy as sa

revision = "0011_url_health"
down_revision = "0010_notification_seen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_urls", sa.Column("last_attempt_at", sa.DateTime(), nullable=True))
    op.add_column("product_urls", sa.Column(
        "consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("product_urls", sa.Column("last_error", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("product_urls", "last_error")
    op.drop_column("product_urls", "consecutive_failures")
    op.drop_column("product_urls", "last_attempt_at")
