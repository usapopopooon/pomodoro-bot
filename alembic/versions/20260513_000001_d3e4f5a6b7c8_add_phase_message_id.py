"""add phase_message_id to pomodoro_rooms

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-05-13 00:00:01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | Sequence[str] | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Stores the single live phase-progress message id so orphan reconciliation
    # on bot restart can freeze its live ``<t:UNIX:R>`` line instead of leaving
    # it forever-incrementing as "X 分前".
    op.add_column(
        "pomodoro_rooms",
        sa.Column("phase_message_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pomodoro_rooms", "phase_message_id")
