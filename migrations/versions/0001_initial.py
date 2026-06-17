"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("model_number", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("image_url", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )

    op.create_table(
        "product_urls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("adapter", sa.String(length=128), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("check_interval_minutes", sa.Integer(), server_default=sa.text("60"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("last_in_stock", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_product_urls_product_id", "product_urls", ["product_id"])
    op.create_index("ix_product_urls_domain", "product_urls", ["domain"])

    op.create_table(
        "price_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("product_url_id", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("in_stock", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("checked_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["product_url_id"], ["product_urls.id"], ondelete="CASCADE"),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_price_history_product_url_id", "price_history", ["product_url_id"])
    op.create_index("ix_price_history_checked_at", "price_history", ["checked_at"])
    op.create_index("ix_price_history_url_checked", "price_history", ["product_url_id", "checked_at"])

    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("threshold", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("cooldown_minutes", sa.Integer(), server_default=sa.text("360"), nullable=False),
        sa.Column("last_triggered_at", sa.DateTime(), nullable=True),
        sa.Column("last_notified_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_alert_rules_product_id", "alert_rules", ["product_id"])

    op.create_table(
        "notification_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("alert_rule_id", sa.Integer(), nullable=True),
        sa.Column("product_url_id", sa.Integer(), nullable=True),
        sa.Column("channel", sa.String(length=16), nullable=True),
        sa.Column("subject", sa.String(length=512), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("success", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["alert_rule_id"], ["alert_rules.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_url_id"], ["product_urls.id"], ondelete="SET NULL"),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), server_default=sa.text("'admin'"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("username", name="uq_users_username"),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )

    op.create_table(
        "settings",
        sa.Column("setting_key", sa.String(length=128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("users")
    op.drop_table("notification_log")
    op.drop_index("ix_alert_rules_product_id", table_name="alert_rules")
    op.drop_table("alert_rules")
    op.drop_index("ix_price_history_url_checked", table_name="price_history")
    op.drop_index("ix_price_history_checked_at", table_name="price_history")
    op.drop_index("ix_price_history_product_url_id", table_name="price_history")
    op.drop_table("price_history")
    op.drop_index("ix_product_urls_domain", table_name="product_urls")
    op.drop_index("ix_product_urls_product_id", table_name="product_urls")
    op.drop_table("product_urls")
    op.drop_table("products")
