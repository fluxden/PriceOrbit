"""user auth fields: is_active, display_name, must_change_password, last_login_at

Revision ID: 0006_user_auth_fields
Revises: 0005_alert_acct_verify_fallback
Create Date: 2026-06-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0006_user_auth_fields"
down_revision = "0005_alert_acct_verify_fallback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False))
    op.add_column("users", sa.Column("display_name", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), server_default=sa.text("0"), nullable=False))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    for col in ("last_login_at", "must_change_password", "display_name", "is_active"):
        op.drop_column("users", col)
