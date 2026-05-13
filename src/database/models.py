from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now_utc() -> datetime:
    return datetime.now(UTC)


class PomodoroRoom(Base):
    """A shared pomodoro timer anchored to a Discord channel.

    One active room per channel (enforced by the partial unique index). Multiple
    participants can join one room and each keeps their own per-session task.
    Closed rooms stay around for history; the partial index lets a channel
    start a fresh room after ``ended_at`` is filled in.
    """

    __tablename__ = "pomodoro_rooms"
    __table_args__ = (
        Index("ix_pomodoro_rooms_channel", "channel_id"),
        Index("ix_pomodoro_rooms_guild", "guild_id"),
        Index(
            "ix_pomodoro_rooms_bot_active",
            "bot_user_id",
            postgresql_where="ended_at IS NULL",
        ),
        Index(
            "ux_pomodoro_rooms_channel_active",
            "channel_id",
            unique=True,
            postgresql_where="ended_at IS NULL",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    # Which bot identity owns this room. Nullable for the rare cold-start
    # case where reconciliation runs before ``self.user`` is populated; in
    # normal flow every newly-created room carries the ID so a multi-bot
    # deploy can scope its reconciliation correctly.
    bot_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # ``message_id`` is the panel message — set once we've posted it and used
    # on restart to re-register the persistent view.
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # ``phase_message_id`` is the single live progress post that gets edited
    # across the room's lifetime. Persisted so orphan reconciliation after
    # a bot restart can fetch it and freeze the live ``<t:UNIX:R>`` line
    # (otherwise Discord keeps re-rendering it as "X 分前" forever).
    phase_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    work_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    short_break_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    long_break_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    long_break_every: Mapped[int] = mapped_column(Integer, nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    participants: Mapped[list[RoomParticipant]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )
    events: Mapped[list[RoomEvent]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )


class RoomParticipant(Base):
    """A single user's enrollment in a room.

    Rows are append-only: leaving sets ``left_at`` rather than deleting so we
    can reconstruct who was present when a phase completed. ``task`` is the
    participant's own current focus string — independent of other members.
    """

    __tablename__ = "room_participants"
    __table_args__ = (
        Index("ix_room_participants_room", "room_id"),
        Index("ix_room_participants_user", "user_id"),
        Index(
            "ux_room_participants_active",
            "room_id",
            "user_id",
            unique=True,
            postgresql_where="left_at IS NULL",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    room_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("pomodoro_rooms.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task: Mapped[str | None] = mapped_column(Text, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False
    )
    left_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    room: Mapped[PomodoroRoom] = relationship(back_populates="participants")


class Pomodoro(Base):
    """One completed work phase for one participant.

    Written at phase-end for every active participant. Keeping it per-user
    makes ``/pomo stats`` and future analytics straightforward regardless of
    how many people shared the room.
    """

    __tablename__ = "pomodoros"
    __table_args__ = (
        Index("idx_pomodoros_user_completed", "user_id", "completed_at"),
        Index("idx_pomodoros_guild_completed", "guild_id", "completed_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    room_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("pomodoro_rooms.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    task: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False
    )


class RoomEvent(Base):
    """Append-only outbox of room lifecycle events.

    Separate from the hot path so future consumers (VOICEVOX, analytics,
    co-op notifications) can tail it without touching writes on the room
    itself.
    """

    __tablename__ = "room_events"
    __table_args__ = (Index("idx_room_events_room_occurred", "room_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    room_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("pomodoro_rooms.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False
    )

    room: Mapped[PomodoroRoom] = relationship(back_populates="events")
