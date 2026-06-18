"""link users to an OIDC subject

Revision ID: 0009_user_oidc_subject
Revises: 0008_audit_events
Create Date: 2026-06-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0009_user_oidc_subject"
down_revision = "0008_audit_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("oidc_subject", sa.String(length=255), nullable=True))
    op.create_unique_constraint("uq_users_oidc_subject", "users", ["oidc_subject"])


def downgrade() -> None:
    op.drop_constraint("uq_users_oidc_subject", "users", type_="unique")
    op.drop_column("users", "oidc_subject")
