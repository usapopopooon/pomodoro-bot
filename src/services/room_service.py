from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import (
    Pomodoro,
    PomodoroRoom,
    RoomEvent,
    RoomParticipant,
)


@dataclass(slots=True)
class StatsSummary:
    today: int
    this_week: int
    total: int


@dataclass(slots=True)
class OrphanRoom:
    """Minimal info needed to clean up a panel message after ``bot_restart``.

    Returned by ``mark_all_active_rooms_ended`` so the caller can fetch each
    panel and strip its buttons without needing ORM objects outside the
    originating session's lifetime.
    """

    room_id: UUID
    guild_id: int | None
    channel_id: int
    message_id: int | None


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------


async def create_room(
    session: AsyncSession,
    *,
    guild_id: int | None,
    channel_id: int,
    created_by: int,
    work_seconds: int,
    short_break_seconds: int,
    long_break_seconds: int,
    long_break_every: int,
    bot_user_id: int | None = None,
) -> PomodoroRoom:
    row = PomodoroRoom(
        bot_user_id=bot_user_id,
        guild_id=guild_id,
        channel_id=channel_id,
        created_by=created_by,
        work_seconds=work_seconds,
        short_break_seconds=short_break_seconds,
        long_break_seconds=long_break_seconds,
        long_break_every=long_break_every,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def set_room_message(
    session: AsyncSession, room_id: UUID, message_id: int
) -> None:
    row = await session.get(PomodoroRoom, room_id)
    if row is None:
        return
    row.message_id = message_id
    await session.commit()


async def update_owner(session: AsyncSession, room_id: UUID, new_owner_id: int) -> None:
    row = await session.get(PomodoroRoom, room_id)
    if row is None:
        return
    row.created_by = new_owner_id
    await session.commit()


async def update_room_plan(
    session: AsyncSession,
    room_id: UUID,
    *,
    work_seconds: int,
    short_break_seconds: int,
    long_break_seconds: int,
    long_break_every: int,
) -> None:
    row = await session.get(PomodoroRoom, room_id)
    if row is None:
        return
    row.work_seconds = work_seconds
    row.short_break_seconds = short_break_seconds
    row.long_break_seconds = long_break_seconds
    row.long_break_every = long_break_every
    await session.commit()


async def end_room(
    session: AsyncSession,
    room_id: UUID,
    *,
    reason: str,
    ended_at: datetime | None = None,
) -> None:
    row = await session.get(PomodoroRoom, room_id)
    if row is None or row.ended_at is not None:
        return
    now = ended_at or datetime.now(UTC)
    row.ended_at = now
    row.ended_reason = reason
    # Close any remaining active participations so the unique index stays sane.
    await session.execute(
        update(RoomParticipant)
        .where(
            RoomParticipant.room_id == room_id,
            RoomParticipant.left_at.is_(None),
        )
        .values(left_at=now)
    )
    await session.commit()


async def get_active_room_in_channel(
    session: AsyncSession, channel_id: int
) -> PomodoroRoom | None:
    result = await session.execute(
        select(PomodoroRoom).where(
            PomodoroRoom.channel_id == channel_id,
            PomodoroRoom.ended_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def get_active_rooms(
    session: AsyncSession, *, bot_user_id: int | None = None
) -> list[PomodoroRoom]:
    """Return active rooms, optionally scoped to a single bot identity.

    Multi-bot deploys pass ``bot_user_id`` so a restart of bot A doesn't
    sweep bot B's still-running rooms. Single-bot deploys leave it unset.
    """
    stmt = select(PomodoroRoom).where(PomodoroRoom.ended_at.is_(None))
    if bot_user_id is not None:
        stmt = stmt.where(PomodoroRoom.bot_user_id == bot_user_id)
    rows = await session.scalars(stmt)
    return list(rows)


async def mark_all_active_rooms_ended(
    session: AsyncSession, *, reason: str, bot_user_id: int | None = None
) -> list[OrphanRoom]:
    """End active rooms (optionally scoped to one bot); used on startup.

    In-memory state is lost on restart, so the room can't resume — close the
    rows explicitly so a fresh panel can be posted in the same channel. The
    returned ``OrphanRoom`` list lets the caller strip the dead panel
    messages' buttons so users don't hit "Interaction failed".

    Pass ``bot_user_id`` when running alongside other bot instances on the
    same DB so this reconciliation only touches its own orphaned rooms.
    """
    rows = await get_active_rooms(session, bot_user_id=bot_user_id)
    if not rows:
        return []
    orphans = [
        OrphanRoom(
            room_id=r.id,
            guild_id=r.guild_id,
            channel_id=r.channel_id,
            message_id=r.message_id,
        )
        for r in rows
    ]
    now = datetime.now(UTC)
    ids = [r.id for r in rows]
    await session.execute(
        update(PomodoroRoom)
        .where(PomodoroRoom.id.in_(ids))
        .values(ended_at=now, ended_reason=reason)
    )
    await session.execute(
        update(RoomParticipant)
        .where(
            RoomParticipant.room_id.in_(ids),
            RoomParticipant.left_at.is_(None),
        )
        .values(left_at=now)
    )
    await session.commit()
    return orphans


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------


async def find_active_participation_for_user(
    session: AsyncSession, user_id: int
) -> RoomParticipant | None:
    result = await session.execute(
        select(RoomParticipant).where(
            RoomParticipant.user_id == user_id,
            RoomParticipant.left_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def get_active_participants(
    session: AsyncSession, room_id: UUID
) -> list[RoomParticipant]:
    rows = await session.scalars(
        select(RoomParticipant).where(
            RoomParticipant.room_id == room_id,
            RoomParticipant.left_at.is_(None),
        )
    )
    return list(rows)


async def join_room(
    session: AsyncSession,
    *,
    room_id: UUID,
    user_id: int,
    task: str | None = None,
) -> RoomParticipant:
    # Close any stale participation for this user first — a user belongs to at
    # most one room at a time, so switching rooms implies leaving the old one.
    stale = await find_active_participation_for_user(session, user_id)
    if stale is not None and stale.room_id != room_id:
        stale.left_at = datetime.now(UTC)
    elif stale is not None and stale.room_id == room_id:
        # Already in this room — update task if given, no-op otherwise.
        if task is not None:
            stale.task = task
        await session.commit()
        await session.refresh(stale)
        return stale

    row = RoomParticipant(room_id=room_id, user_id=user_id, task=task)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def leave_room(session: AsyncSession, *, room_id: UUID, user_id: int) -> bool:
    row = await session.scalar(
        select(RoomParticipant).where(
            RoomParticipant.room_id == room_id,
            RoomParticipant.user_id == user_id,
            RoomParticipant.left_at.is_(None),
        )
    )
    if row is None:
        return False
    row.left_at = datetime.now(UTC)
    await session.commit()
    return True


async def set_participant_task(
    session: AsyncSession, *, room_id: UUID, user_id: int, task: str | None
) -> bool:
    row = await session.scalar(
        select(RoomParticipant).where(
            RoomParticipant.room_id == room_id,
            RoomParticipant.user_id == user_id,
            RoomParticipant.left_at.is_(None),
        )
    )
    if row is None:
        return False
    row.task = task
    await session.commit()
    return True


# ---------------------------------------------------------------------------
# Pomodoro records + events
# ---------------------------------------------------------------------------


async def record_pomodoros_for_active_participants(
    session: AsyncSession,
    *,
    room_id: UUID,
    duration_seconds: int,
) -> int:
    """Insert one ``pomodoros`` row per active participant; return the count."""
    room = await session.get(PomodoroRoom, room_id)
    if room is None:
        return 0
    participants = await get_active_participants(session, room_id)
    for p in participants:
        session.add(
            Pomodoro(
                room_id=room_id,
                user_id=p.user_id,
                guild_id=room.guild_id,
                task=p.task,
                duration_seconds=duration_seconds,
            )
        )
    await session.commit()
    return len(participants)


async def record_event(
    session: AsyncSession,
    *,
    room_id: UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        RoomEvent(
            room_id=room_id,
            event_type=event_type,
            payload=payload or {},
        )
    )
    await session.commit()


async def stats_for_user(
    session: AsyncSession,
    user_id: int,
    *,
    now: datetime | None = None,
) -> StatsSummary:
    now = now or datetime.now(UTC)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_week = start_of_today - timedelta(days=start_of_today.weekday())

    today = await session.scalar(
        select(func.count(Pomodoro.id)).where(
            Pomodoro.user_id == user_id,
            Pomodoro.completed_at >= start_of_today,
        )
    )
    week = await session.scalar(
        select(func.count(Pomodoro.id)).where(
            Pomodoro.user_id == user_id,
            Pomodoro.completed_at >= start_of_week,
        )
    )
    total = await session.scalar(
        select(func.count(Pomodoro.id)).where(Pomodoro.user_id == user_id)
    )
    return StatsSummary(today=today or 0, this_week=week or 0, total=total or 0)
