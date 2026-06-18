"""favorites, tags, and url baseline price

Revision ID: 0002_favorites_tags
Revises: 0001_initial
Create Date: 2026-06-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0002_favorites_tags"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("is_favorite", sa.Boolean(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "product_urls",
        sa.Column("baseline_price", sa.Numeric(precision=12, scale=2), nullable=True),
    )

    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("color", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("name", name="uq_tags_name"),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )

    op.create_table(
        "product_tags",
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("product_id", "tag_id"),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )


def downgrade() -> None:
    op.drop_table("product_tags")
    op.drop_table("tags")
    op.drop_column("product_urls", "baseline_price")
    op.drop_column("products", "is_favorite")
