"""mark in-app (sound) notifications as seen

Revision ID: 0010_notification_seen
Revises: 0009_user_oidc_subject
Create Date: 2026-06-15

"""
from alembic import op
import sqlalchemy as sa

revision = "0010_notification_seen"
down_revision = "0009_user_oidc_subject"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notification_log", sa.Column(
        "seen", sa.Boolean(), nullable=False, server_default=sa.text("0")))


def downgrade() -> None:
    op.drop_column("notification_log", "seen")
