"""add bot_user_id to pomodoro_rooms

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-04-26 00:00:01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Multi-bot deployments use this to scope startup reconciliation to the
    # rooms a given bot identity owns. Nullable so existing rows survive.
    op.add_column(
        "pomodoro_rooms",
        sa.Column("bot_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_pomodoro_rooms_bot_active",
        "pomodoro_rooms",
        ["bot_user_id"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_pomodoro_rooms_bot_active", table_name="pomodoro_rooms")
    op.drop_column("pomodoro_rooms", "bot_user_id")
