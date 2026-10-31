"""create channels table

Revision ID: 0001
Revises:
Create Date: 2026-10-31

"""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channels",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("channel_name", sa.Text(), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("broken_url", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("broken_reason", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("last_post_id", sa.Integer()),
        sa.Column("last_post_at", sa.DateTime(timezone=True)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "next_check_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("fail_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "not_found_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("last_not_found_at", sa.DateTime(timezone=True)),
        sa.Column("last_not_found_proxy", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.Column("channel_id", sa.BigInteger()),
        sa.Column("channel_url", sa.Text()),
    )
    op.create_index(
        "channels_due_idx",
        "channels",
        ["next_check_at"],
        postgresql_where=sa.text("is_active AND NOT broken_url"),
    )
    op.create_index(
        "channels_channel_id_uq",
        "channels",
        ["channel_id"],
        unique=True,
        postgresql_where=sa.text("channel_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("channels_channel_id_uq", table_name="channels")
    op.drop_index("channels_due_idx", table_name="channels")
    op.drop_table("channels")
