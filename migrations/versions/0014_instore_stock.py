"""second stock dimension: in-store availability (drives the In-store bubble)

The existing in_stock / last_in_stock now means ONLINE availability; these add a
separate, nullable in-store signal (NULL = the store doesn't report it).

Revision ID: 0014_instore_stock
Revises: 0013_url_last_engine
Create Date: 2026-06-26

"""
from alembic import op
import sqlalchemy as sa

revision = "0014_instore_stock"
down_revision = "0013_url_last_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_urls", sa.Column("last_instore_in_stock", sa.Boolean(), nullable=True))
    op.add_column("price_history", sa.Column("instore_in_stock", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("price_history", "instore_in_stock")
    op.drop_column("product_urls", "last_instore_in_stock")
