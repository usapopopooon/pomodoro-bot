"""RoomManager tests — multi-participant, multi-room, owner-only gates.

DB-backed (service calls commit real rows) but the Discord-facing objects are
stubbed with ``SimpleNamespace`` + ``AsyncMock``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from src.core.phase import Phase, PhasePlan
from src.database.engine import async_session, check_database_connection, engine
from src.database.models import Pomodoro, PomodoroRoom, RoomParticipant
from src.room_manager import OpResult, RoomManager


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _require_database() -> AsyncGenerator[None, None]:
    if not await check_database_connection(timeout=3.0):
        pytest.skip("database not reachable", allow_module_level=True)
    yield
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _truncate() -> AsyncGenerator[None, None]:
    yield
    async with async_session() as cleanup:
        await cleanup.execute(
            text(
                "TRUNCATE pomodoro_rooms, room_participants, "
                "pomodoros, room_events RESTART IDENTITY CASCADE"
            )
        )
        await cleanup.commit()


def _manager(*, tick_seconds: int = 3600, every: int = 2) -> RoomManager:
    # A huge tick keeps the background loop dormant during unit tests.
    return RoomManager(
        default_plan=PhasePlan(10, 2, 4, every),
        tick_seconds=tick_seconds,
    )


def _fake_channel(channel_id: int = 555) -> SimpleNamespace:
    return SimpleNamespace(id=channel_id, send=AsyncMock())


def _fake_message(channel: SimpleNamespace) -> SimpleNamespace:
    msg = SimpleNamespace(id=1234, edit=AsyncMock(), channel=channel)
    return msg


async def _spawn_room(manager: RoomManager, *, creator: int, channel_id: int = 555):  # type: ignore[no-untyped-def]
    channel = _fake_channel(channel_id)
    state = await manager.create_and_start(
        guild_id=None,
        channel_id=channel_id,
        created_by=creator,
        channel=channel,
    )
    state.message = _fake_message(channel)
    return state, channel


# ---------------------------------------------------------------------------
# Basic lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_start_persists_row_and_registers() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1, channel_id=999)
    try:
        assert manager.get(state.room_id) is state

        async with async_session() as db:
            rows = (await db.execute(select(PomodoroRoom))).scalars().all()
            assert len(rows) == 1
            assert rows[0].channel_id == 999
            assert rows[0].created_by == 1
            assert rows[0].ended_at is None
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_end_closes_row_and_participants() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    await manager.join(state.room_id, 2)
    await manager.end(state.room_id, reason="owner_ended")

    assert manager.get(state.room_id) is None
    async with async_session() as db:
        room = await db.get(PomodoroRoom, state.room_id)
        assert room is not None
        assert room.ended_reason == "owner_ended"
        parts = (await db.execute(select(RoomParticipant))).scalars().all()
        assert all(p.left_at is not None for p in parts)


# ---------------------------------------------------------------------------
# Join / leave / task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_adds_to_memory_and_db() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    try:
        assert await manager.join(state.room_id, 2) is OpResult.OK
        assert state.has_participant(2)
        async with async_session() as db:
            active = (
                await db.execute(
                    select(RoomParticipant).where(
                        RoomParticipant.room_id == state.room_id,
                        RoomParticipant.user_id == 2,
                    )
                )
            ).scalar_one()
            assert active.left_at is None
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_join_when_already_in_returns_already_joined() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    try:
        await manager.join(state.room_id, 2)
        assert await manager.join(state.room_id, 2) is OpResult.ALREADY_JOINED
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_join_switches_user_across_rooms() -> None:
    manager = _manager()
    r1, _ = await _spawn_room(manager, creator=1, channel_id=1)
    r2, _ = await _spawn_room(manager, creator=2, channel_id=2)
    try:
        await manager.join(r1.room_id, 9)
        assert r1.has_participant(9)
        # Joining r2 should evict from r1 automatically.
        assert await manager.join(r2.room_id, 9) is OpResult.OK
        assert not r1.has_participant(9)
        assert r2.has_participant(9)
    finally:
        await manager.end(r1.room_id, reason="test")
        await manager.end(r2.room_id, reason="test")


@pytest.mark.asyncio
async def test_leave_non_participant_returns_not_a_participant() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    try:
        assert await manager.leave(state.room_id, 99) is OpResult.NOT_A_PARTICIPANT
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_leave_transfers_ownership_to_earliest_remaining() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    await manager.join(state.room_id, 1)  # owner joins too
    await manager.join(state.room_id, 2)
    await manager.join(state.room_id, 3)
    try:
        assert await manager.leave(state.room_id, 1) is OpResult.OK
        # Heir is the earliest remaining — user 2 joined before user 3.
        assert state.created_by == 2
        async with async_session() as db:
            room = await db.get(PomodoroRoom, state.room_id)
            assert room is not None
            assert room.created_by == 2
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_leave_auto_ends_room_when_last_participant_gone() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    await manager.join(state.room_id, 1)
    await manager.leave(state.room_id, 1)

    assert manager.get(state.room_id) is None
    async with async_session() as db:
        room = await db.get(PomodoroRoom, state.room_id)
        assert room is not None
        assert room.ended_reason == "auto_empty"


@pytest.mark.asyncio
async def test_set_task_requires_participation() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    try:
        assert (
            await manager.set_task(state.room_id, 99, task="x")
            is OpResult.NOT_A_PARTICIPANT
        )
        await manager.join(state.room_id, 2)
        assert await manager.set_task(state.room_id, 2, task="math") is OpResult.OK
        assert state.participants[2].task == "math"
    finally:
        await manager.end(state.room_id, reason="test")


# ---------------------------------------------------------------------------
# Owner-only controls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_rejected_for_non_owner() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    await manager.join(state.room_id, 2)
    try:
        assert await manager.toggle_pause(state.room_id, 2) is OpResult.NOT_OWNER
        assert not state.is_paused
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_pause_by_owner_toggles() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    try:
        assert await manager.toggle_pause(state.room_id, 1) is OpResult.OK
        assert state.is_paused
        assert await manager.toggle_pause(state.room_id, 1) is OpResult.OK
        assert not state.is_paused
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_skip_owner_advances_without_counting_completion() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    await manager.join(state.room_id, 1)
    try:
        assert await manager.skip(state.room_id, 1) is OpResult.OK
        assert state.phase is Phase.SHORT_BREAK
        assert state.completed_work_phases == 0
        async with async_session() as db:
            pomos = (await db.execute(select(Pomodoro))).scalars().all()
            assert pomos == []
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_end_by_owner_only_accepts_owner() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    await manager.join(state.room_id, 2)
    assert await manager.end_by_owner(state.room_id, 2) is OpResult.NOT_OWNER
    assert manager.get(state.room_id) is state
    assert await manager.end_by_owner(state.room_id, 1) is OpResult.OK
    assert manager.get(state.room_id) is None


# ---------------------------------------------------------------------------
# Phase completion credits every active participant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_phase_end_credits_each_active_participant() -> None:
    manager = _manager()
    state, channel = await _spawn_room(manager, creator=1)
    await manager.join(state.room_id, 1, task="focus")
    await manager.join(state.room_id, 2, task="study")
    await manager.join(state.room_id, 3)
    await manager.leave(state.room_id, 3)  # left before phase end

    try:
        await manager._handle_phase_end(state, channel)
        assert state.phase is Phase.SHORT_BREAK
        assert state.completed_work_phases == 1

        async with async_session() as db:
            pomos = (await db.execute(select(Pomodoro))).scalars().all()
            # Only users 1 and 2 should be credited (3 left first).
            assert sorted(p.user_id for p in pomos) == [1, 2]
            tasks = {p.user_id: p.task for p in pomos}
            assert tasks[1] == "focus"
            assert tasks[2] == "study"
    finally:
        await manager.end(state.room_id, reason="test")


# ---------------------------------------------------------------------------
# Multi-room isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_rooms_run_independently() -> None:
    manager = _manager()
    r1, _ = await _spawn_room(manager, creator=1, channel_id=111)
    r2, _ = await _spawn_room(manager, creator=2, channel_id=222)
    try:
        await manager.toggle_pause(r1.room_id, 1)
        assert r1.is_paused
        assert not r2.is_paused
    finally:
        await manager.end(r1.room_id, reason="test")
        await manager.end(r2.room_id, reason="test")


# ---------------------------------------------------------------------------
# Regression: concurrent cross-room joins must not deadlock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simultaneous_cross_room_joins_do_not_deadlock() -> None:
    """Two users swapping rooms in opposite directions used to deadlock
    because ``join`` held the target room's lock while acquiring the other
    room's lock via ``_evict_from_other_rooms``. After the fix, eviction
    runs outside the target lock and the swap always completes.
    """
    manager = _manager()
    r1, _ = await _spawn_room(manager, creator=1, channel_id=111)
    r2, _ = await _spawn_room(manager, creator=2, channel_id=222)
    try:
        # Seed: user 100 in r1, user 200 in r2.
        assert await manager.join(r1.room_id, 100) is OpResult.OK
        assert await manager.join(r2.room_id, 200) is OpResult.OK

        # Swap them concurrently. A 5s wait_for is a generous upper bound;
        # with the deadlock still present this hangs forever.
        results = await asyncio.wait_for(
            asyncio.gather(
                manager.join(r2.room_id, 100),
                manager.join(r1.room_id, 200),
            ),
            timeout=5.0,
        )
        assert results == [OpResult.OK, OpResult.OK]

        # Final in-memory state: users ended up in the other room.
        assert r1.has_participant(200) and not r1.has_participant(100)
        assert r2.has_participant(100) and not r2.has_participant(200)
    finally:
        await manager.end(r1.room_id, reason="test")
        await manager.end(r2.room_id, reason="test")


@pytest.mark.asyncio
async def test_create_and_start_raises_integrity_error_for_second_room_in_channel() -> (
    None
):
    """The partial unique index must bounce a second active room in the same
    channel. The cog wraps this in a friendly ephemeral message."""
    manager = _manager()
    r1, _ = await _spawn_room(manager, creator=1, channel_id=500)
    try:
        channel = _fake_channel(500)
        with pytest.raises(IntegrityError):
            await manager.create_and_start(
                guild_id=None,
                channel_id=500,
                created_by=2,
                channel=channel,
            )
        # First room must still be intact.
        assert manager.get(r1.room_id) is r1
        async with async_session() as db:
            rows = (await db.execute(select(PomodoroRoom))).scalars().all()
            assert len(rows) == 1
    finally:
        await manager.end(r1.room_id, reason="test")
