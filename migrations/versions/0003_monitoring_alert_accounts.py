"""monitoring schedule, target price, import status, and alert accounts

Revision ID: 0003_monitoring_alert_accounts
Revises: 0002_favorites_tags
Create Date: 2026-06-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0003_monitoring_alert_accounts"
down_revision = "0002_favorites_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Product monitoring + import fields
    op.add_column("products", sa.Column("track_price", sa.Boolean(), server_default=sa.text("1"), nullable=False))
    op.add_column("products", sa.Column("track_stock", sa.Boolean(), server_default=sa.text("0"), nullable=False))
    op.add_column("products", sa.Column("schedule_kind", sa.String(length=16), server_default=sa.text("'interval'"), nullable=False))
    op.add_column("products", sa.Column("check_interval_minutes", sa.Integer(), nullable=True))
    op.add_column("products", sa.Column("daily_check_time", sa.String(length=5), nullable=True))
    op.add_column("products", sa.Column("target_price", sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column("products", sa.Column("import_status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False))
    op.add_column("products", sa.Column("import_error", sa.Text(), nullable=True))

    # Alert accounts (configured notification destinations)
    op.create_table(
        "alert_accounts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("destination", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )

    # Link alert rules to an account
    op.add_column("alert_rules", sa.Column("alert_account_id", sa.Integer(), nullable=True))
    op.create_index("ix_alert_rules_alert_account_id", "alert_rules", ["alert_account_id"])
    op.create_foreign_key(
        "fk_alert_rules_account", "alert_rules", "alert_accounts",
        ["alert_account_id"], ["id"], ondelete="SET NULL",
    )

    # Existing products already have price history -> mark them as price-tracked
    # and imported so they appear correctly on the Price Tracking page.
    op.execute("UPDATE products SET import_status = 'imported' WHERE import_status = 'pending'")


def downgrade() -> None:
    op.drop_constraint("fk_alert_rules_account", "alert_rules", type_="foreignkey")
    op.drop_index("ix_alert_rules_alert_account_id", table_name="alert_rules")
    op.drop_column("alert_rules", "alert_account_id")
    op.drop_table("alert_accounts")
    for col in (
        "import_error", "import_status", "target_price", "daily_check_time",
        "check_interval_minutes", "schedule_kind", "track_stock", "track_price",
    ):
        op.drop_column("products", col)
