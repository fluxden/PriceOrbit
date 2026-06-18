"""alert account verified timestamp + fallback account

Revision ID: 0005_alert_acct_verify_fallback
Revises: 0004_store_overrides_primary
Create Date: 2026-06-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0005_alert_acct_verify_fallback"
down_revision = "0004_store_overrides_primary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("alert_accounts", sa.Column("last_verified_at", sa.DateTime(), nullable=True))
    op.add_column("alert_accounts", sa.Column("fallback_account_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_alert_accounts_fallback", "alert_accounts", "alert_accounts",
        ["fallback_account_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_alert_accounts_fallback", "alert_accounts", type_="foreignkey")
    op.drop_column("alert_accounts", "fallback_account_id")
    op.drop_column("alert_accounts", "last_verified_at")
