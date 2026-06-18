"""per-user product ownership

Revision ID: 0007_product_owner
Revises: 0006_user_auth_fields
Create Date: 2026-06-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0007_product_owner"
down_revision = "0006_user_auth_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("user_id", sa.Integer(), nullable=True))
    op.create_index("ix_products_user_id", "products", ["user_id"])
    op.create_foreign_key("fk_products_user", "products", "users",
                          ["user_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_products_user", "products", type_="foreignkey")
    op.drop_index("ix_products_user_id", table_name="products")
    op.drop_column("products", "user_id")
