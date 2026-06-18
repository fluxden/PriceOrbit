"""user email + customizable top-bar bubble (avatar, color, display)

Revision ID: 0012_user_profile_bubble
Revises: 0011_url_health
Create Date: 2026-06-18

"""
from alembic import op
import sqlalchemy as sa

revision = "0012_user_profile_bubble"
down_revision = "0011_url_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.String(length=512), nullable=True))
    op.add_column("users", sa.Column("bubble_color", sa.String(length=32), nullable=True))
    op.add_column("users", sa.Column(
        "bubble_transparent", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.add_column("users", sa.Column(
        "bubble_display", sa.String(length=16), nullable=False, server_default=sa.text("'name'")))


def downgrade() -> None:
    op.drop_column("users", "bubble_display")
    op.drop_column("users", "bubble_transparent")
    op.drop_column("users", "bubble_color")
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "email")
