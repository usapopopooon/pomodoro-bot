from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import (
    Pomodoro,
    PomodoroRoom,
    RoomEvent,
    RoomParticipant,
)
from src.services import room_service as svc


async def _mk_room(session: AsyncSession, **overrides) -> PomodoroRoom:  # type: ignore[no-untyped-def]
    defaults = {
        "guild_id": 42,
        "channel_id": 100,
        "created_by": 1,
        "work_seconds": 10,
        "short_break_seconds": 2,
        "long_break_seconds": 4,
        "long_break_every": 2,
    }
    defaults.update(overrides)
    return await svc.create_room(session, **defaults)


@pytest.mark.asyncio
async def test_create_room_persists_fields(db_session: AsyncSession) -> None:
    row = await _mk_room(db_session, channel_id=123, created_by=42)
    assert row.id is not None
    assert row.channel_id == 123
    assert row.created_by == 42
    assert row.ended_at is None


@pytest.mark.asyncio
async def test_get_active_room_in_channel_only_returns_active(
    db_session: AsyncSession,
) -> None:
    row = await _mk_room(db_session, channel_id=777)
    found = await svc.get_active_room_in_channel(db_session, 777)
    assert found is not None and found.id == row.id

    await svc.end_room(db_session, row.id, reason="test")
    assert await svc.get_active_room_in_channel(db_session, 777) is None


@pytest.mark.asyncio
async def test_end_room_also_closes_active_participants(
    db_session: AsyncSession,
) -> None:
    room = await _mk_room(db_session)
    await svc.join_room(db_session, room_id=room.id, user_id=10, task="a")
    await svc.join_room(db_session, room_id=room.id, user_id=20, task="b")

    await svc.end_room(db_session, room.id, reason="owner_ended")

    participants = (await db_session.execute(select(RoomParticipant))).scalars().all()
    assert len(participants) == 2
    assert all(p.left_at is not None for p in participants)


@pytest.mark.asyncio
async def test_end_room_is_idempotent(db_session: AsyncSession) -> None:
    room = await _mk_room(db_session)
    await svc.end_room(db_session, room.id, reason="first")
    await svc.end_room(db_session, room.id, reason="second")
    refreshed = await db_session.get(PomodoroRoom, room.id)
    assert refreshed is not None
    assert refreshed.ended_reason == "first"


@pytest.mark.asyncio
async def test_mark_all_active_rooms_ended_returns_orphan_info(
    db_session: AsyncSession,
) -> None:
    r1 = await _mk_room(db_session, channel_id=1, guild_id=10)
    r2 = await _mk_room(db_session, channel_id=2, guild_id=20)
    # Set message_id on r1 so we can verify it's propagated.
    await svc.set_room_message(db_session, r1.id, message_id=999)
    # Pre-closed room shouldn't be touched.
    r3 = await _mk_room(db_session, channel_id=3)
    await svc.end_room(db_session, r3.id, reason="earlier")

    orphans = await svc.mark_all_active_rooms_ended(db_session, reason="bot_restart")
    by_room = {o.room_id: o for o in orphans}
    assert set(by_room) == {r1.id, r2.id}
    assert by_room[r1.id].channel_id == 1
    assert by_room[r1.id].guild_id == 10
    assert by_room[r1.id].message_id == 999
    assert by_room[r2.id].message_id is None

    fresh = await db_session.get(PomodoroRoom, r1.id)
    assert fresh is not None
    assert fresh.ended_reason == "bot_restart"


@pytest.mark.asyncio
async def test_join_room_is_idempotent_and_updates_task(
    db_session: AsyncSession,
) -> None:
    room = await _mk_room(db_session)
    p1 = await svc.join_room(db_session, room_id=room.id, user_id=5, task="x")
    p2 = await svc.join_room(db_session, room_id=room.id, user_id=5, task="y")
    assert p1.id == p2.id
    assert p2.task == "y"


@pytest.mark.asyncio
async def test_join_switches_user_to_new_room(db_session: AsyncSession) -> None:
    r1 = await _mk_room(db_session, channel_id=1)
    r2 = await _mk_room(db_session, channel_id=2)

    await svc.join_room(db_session, room_id=r1.id, user_id=9, task="old")
    await svc.join_room(db_session, room_id=r2.id, user_id=9, task="new")

    stale = await db_session.scalar(
        select(RoomParticipant).where(
            RoomParticipant.room_id == r1.id, RoomParticipant.user_id == 9
        )
    )
    assert stale is not None and stale.left_at is not None

    active = await svc.find_active_participation_for_user(db_session, 9)
    assert active is not None and active.room_id == r2.id


@pytest.mark.asyncio
async def test_leave_room_sets_left_at(db_session: AsyncSession) -> None:
    room = await _mk_room(db_session)
    await svc.join_room(db_session, room_id=room.id, user_id=3)
    assert await svc.leave_room(db_session, room_id=room.id, user_id=3)
    assert not await svc.leave_room(db_session, room_id=room.id, user_id=3)


@pytest.mark.asyncio
async def test_set_participant_task_updates_or_noops(
    db_session: AsyncSession,
) -> None:
    room = await _mk_room(db_session)
    await svc.join_room(db_session, room_id=room.id, user_id=1, task="a")
    assert await svc.set_participant_task(
        db_session, room_id=room.id, user_id=1, task="b"
    )
    assert not await svc.set_participant_task(
        db_session, room_id=room.id, user_id=999, task="x"
    )


@pytest.mark.asyncio
async def test_record_pomodoros_for_active_participants_writes_per_user(
    db_session: AsyncSession,
) -> None:
    room = await _mk_room(db_session, guild_id=55)
    await svc.join_room(db_session, room_id=room.id, user_id=1, task="math")
    await svc.join_room(db_session, room_id=room.id, user_id=2, task="english")
    # User 3 joined then left — should NOT be credited.
    await svc.join_room(db_session, room_id=room.id, user_id=3, task="late")
    await svc.leave_room(db_session, room_id=room.id, user_id=3)

    credited = await svc.record_pomodoros_for_active_participants(
        db_session, room_id=room.id, duration_seconds=1500
    )
    assert credited == 2

    rows = (await db_session.execute(select(Pomodoro))).scalars().all()
    assert len(rows) == 2
    by_user = {r.user_id: r for r in rows}
    assert by_user[1].task == "math"
    assert by_user[2].task == "english"
    assert all(r.guild_id == 55 for r in rows)
    assert all(r.duration_seconds == 1500 for r in rows)


@pytest.mark.asyncio
async def test_record_event_stores_payload(db_session: AsyncSession) -> None:
    room = await _mk_room(db_session)
    await svc.record_event(
        db_session,
        room_id=room.id,
        event_type="phase_completed",
        payload={"to": "short_break", "credited_users": 2},
    )
    stored = (
        await db_session.execute(select(RoomEvent).where(RoomEvent.room_id == room.id))
    ).scalar_one()
    assert stored.event_type == "phase_completed"
    assert stored.payload == {"to": "short_break", "credited_users": 2}


@pytest.mark.asyncio
async def test_stats_bucket_today_this_week_total(
    db_session: AsyncSession,
) -> None:
    room = await _mk_room(db_session)
    now = datetime.now(UTC)
    older = now - timedelta(days=14)

    db_session.add_all(
        [
            Pomodoro(
                room_id=room.id,
                user_id=1,
                duration_seconds=10,
                completed_at=now,
            ),
            Pomodoro(
                room_id=room.id,
                user_id=1,
                duration_seconds=10,
                completed_at=now - timedelta(hours=2),
            ),
            Pomodoro(
                room_id=room.id,
                user_id=1,
                duration_seconds=10,
                completed_at=older,
            ),
        ]
    )
    await db_session.commit()

    summary = await svc.stats_for_user(db_session, user_id=1)
    assert summary.total == 3
    assert summary.today >= 1
    assert summary.this_week >= 2
    assert summary.this_week <= summary.total


@pytest.mark.asyncio
async def test_stats_with_no_pomodoros(db_session: AsyncSession) -> None:
    summary = await svc.stats_for_user(db_session, user_id=99999)
    assert summary.today == 0
    assert summary.this_week == 0
    assert summary.total == 0
