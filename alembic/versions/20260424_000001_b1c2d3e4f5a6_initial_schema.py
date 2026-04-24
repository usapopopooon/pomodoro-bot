"""initial schema

Revision ID: b1c2d3e4f5a6
Revises:
Create Date: 2026-04-24 00:00:01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b1c2d3e4f5a6"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pomodoro_rooms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("work_seconds", sa.Integer(), nullable=False),
        sa.Column("short_break_seconds", sa.Integer(), nullable=False),
        sa.Column("long_break_seconds", sa.Integer(), nullable=False),
        sa.Column("long_break_every", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_reason", sa.String(length=32), nullable=True),
    )
    op.create_index("ix_pomodoro_rooms_channel", "pomodoro_rooms", ["channel_id"])
    op.create_index("ix_pomodoro_rooms_guild", "pomodoro_rooms", ["guild_id"])
    # Only one active room per channel; closed rooms don't contend.
    op.create_index(
        "ux_pomodoro_rooms_channel_active",
        "pomodoro_rooms",
        ["channel_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    op.create_table(
        "room_participants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "room_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pomodoro_rooms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("task", sa.Text(), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_room_participants_room", "room_participants", ["room_id"])
    op.create_index("ix_room_participants_user", "room_participants", ["user_id"])
    # Prevent double-joining the same room.
    op.create_index(
        "ux_room_participants_active",
        "room_participants",
        ["room_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("left_at IS NULL"),
    )

    op.create_table(
        "pomodoros",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "room_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pomodoro_rooms.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=True),
        sa.Column("task", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_pomodoros_user_completed", "pomodoros", ["user_id", "completed_at"]
    )
    op.create_index(
        "idx_pomodoros_guild_completed", "pomodoros", ["guild_id", "completed_at"]
    )

    op.create_table(
        "room_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "room_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pomodoro_rooms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_room_events_room_occurred", "room_events", ["room_id", "occurred_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_room_events_room_occurred", table_name="room_events")
    op.drop_table("room_events")
    op.drop_index("idx_pomodoros_guild_completed", table_name="pomodoros")
    op.drop_index("idx_pomodoros_user_completed", table_name="pomodoros")
    op.drop_table("pomodoros")
    op.drop_index("ux_room_participants_active", table_name="room_participants")
    op.drop_index("ix_room_participants_user", table_name="room_participants")
    op.drop_index("ix_room_participants_room", table_name="room_participants")
    op.drop_table("room_participants")
    op.drop_index("ux_pomodoro_rooms_channel_active", table_name="pomodoro_rooms")
    op.drop_index("ix_pomodoro_rooms_guild", table_name="pomodoro_rooms")
    op.drop_index("ix_pomodoro_rooms_channel", table_name="pomodoro_rooms")
    op.drop_table("pomodoro_rooms")
