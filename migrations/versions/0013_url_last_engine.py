"""per-listing last fetch engine (drives the scrape.do API badge)

Revision ID: 0013_url_last_engine
Revises: 0012_user_profile_bubble
Create Date: 2026-06-26

"""
from alembic import op
import sqlalchemy as sa

revision = "0013_url_last_engine"
down_revision = "0012_user_profile_bubble"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_urls", sa.Column("last_engine", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("product_urls", "last_engine")
