"""RoomManager tests — multi-participant, multi-room, owner-only gates.

DB-backed (service calls commit real rows) but the Discord-facing objects are
stubbed with ``SimpleNamespace`` + ``AsyncMock``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
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


def _manager(*, every: int = 2) -> RoomManager:
    return RoomManager(default_plan=PhasePlan(10, 2, 4, every))


def _fake_channel(channel_id: int = 555) -> SimpleNamespace:
    """Channel stub whose ``send`` returns message-like objects.

    ``RoomManager._post_phase_start_message`` stores ``channel.send(...)``'s
    return value as ``state.last_phase_message`` and later reads ``.content``
    off it for the freeze-on-transition edit. A plain ``AsyncMock`` returns a
    ``MagicMock`` that auto-creates ``.content`` as another mock, which then
    breaks ``str.split``. Returning a proper ``SimpleNamespace`` with a real
    string mirrors Discord's actual Message shape closely enough for these
    tests.
    """
    channel = SimpleNamespace(id=channel_id)

    def _fresh_sent_message(*args: object, **kwargs: object) -> SimpleNamespace:
        content = kwargs.get("content", "")
        return SimpleNamespace(
            id=9999,
            edit=AsyncMock(),
            content=content if isinstance(content, str) else "",
        )

    channel.send = AsyncMock(side_effect=_fresh_sent_message)
    return channel


def _fake_message(channel: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(id=1234, edit=AsyncMock(), channel=channel, content="")


async def _spawn_room(
    manager: RoomManager,
    *,
    creator: int,
    channel_id: int = 555,
    running: bool = True,
):  # type: ignore[no-untyped-def]
    """Create a room in setup state and attach a fake Control Panel message.

    By default we flip ``has_started=True`` (without spawning the real
    phase-loop task) so owner-only ops — pause / skip / reset / update_plan
    — are accepted. Pass ``running=False`` when testing the setup-state
    behaviour explicitly.
    """
    channel = _fake_channel(channel_id)
    state = await manager.create_setup(
        guild_id=None,
        channel_id=channel_id,
        created_by=creator,
    )
    state.message = _fake_message(channel)
    if running:
        state.has_started = True
    return state, channel


# ---------------------------------------------------------------------------
# Basic lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_setup_persists_row_and_registers() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1, channel_id=999, running=False)
    try:
        assert manager.get(state.room_id) is state
        assert state.has_started is False  # still in setup

        async with async_session() as db:
            rows = (await db.execute(select(PomodoroRoom))).scalars().all()
            assert len(rows) == 1
            assert rows[0].channel_id == 999
            assert rows[0].created_by == 1
            assert rows[0].ended_at is None
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_begin_phases_flips_has_started_and_starts_loop_task() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1, channel_id=999, running=False)
    try:
        assert await manager.begin_phases(state.room_id, 1) is OpResult.OK
        assert state.has_started is True
        # Cancel the task we just kicked off to keep tests clean.
        if state.task_handle is not None:
            state.task_handle.cancel()
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_begin_phases_rejected_for_non_owner() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1, running=False)
    try:
        assert await manager.begin_phases(state.room_id, 999) is OpResult.NOT_OWNER
        assert state.has_started is False
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_begin_phases_rejects_double_start() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1, running=True)
    try:
        assert await manager.begin_phases(state.room_id, 1) is OpResult.ALREADY_STARTED
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
        assert await manager.join(r2.room_id, 9) is OpResult.OK
        assert not r1.has_participant(9)
        assert r2.has_participant(9)
    finally:
        await manager.end(r1.room_id, reason="test")
        await manager.end(r2.room_id, reason="test")


@pytest.mark.asyncio
async def test_join_switch_auto_ends_previous_room_when_it_becomes_empty() -> None:
    manager = _manager()
    r1, _ = await _spawn_room(manager, creator=1, channel_id=11)
    r2, _ = await _spawn_room(manager, creator=2, channel_id=22)
    await manager.join(r1.room_id, 9)
    try:
        assert await manager.join(r2.room_id, 9) is OpResult.OK
        assert manager.get(r1.room_id) is None
        async with async_session() as db:
            old_room = await db.get(PomodoroRoom, r1.room_id)
            assert old_room is not None
            assert old_room.ended_reason == "auto_empty"
    finally:
        await manager.end(r2.room_id, reason="test")


@pytest.mark.asyncio
async def test_join_switch_owner_transfers_ownership_in_previous_room() -> None:
    manager = _manager()
    r1, _ = await _spawn_room(manager, creator=1, channel_id=31)
    r2, _ = await _spawn_room(manager, creator=9, channel_id=32)
    await manager.join(r1.room_id, 1)
    await manager.join(r1.room_id, 2)
    try:
        assert await manager.join(r2.room_id, 1) is OpResult.OK
        assert r1.created_by == 2
        async with async_session() as db:
            old_room = await db.get(PomodoroRoom, r1.room_id)
            assert old_room is not None
            assert old_room.created_by == 2
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
    await manager.join(state.room_id, 1)
    await manager.join(state.room_id, 2)
    await manager.join(state.room_id, 3)
    try:
        assert await manager.leave(state.room_id, 1) is OpResult.OK
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
async def test_pause_rejected_before_timer_started() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1, running=False)
    try:
        assert await manager.toggle_pause(state.room_id, 1) is OpResult.NOT_YET_STARTED
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_skip_rejected_before_timer_started() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1, running=False)
    try:
        assert await manager.skip(state.room_id, 1) is OpResult.NOT_YET_STARTED
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_reset_rejected_before_timer_started() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1, running=False)
    try:
        assert await manager.reset(state.room_id, 1) is OpResult.NOT_YET_STARTED
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_skip_owner_advances_without_counting_completion() -> None:
    manager = _manager()
    state, channel = await _spawn_room(manager, creator=1)
    await manager.join(state.room_id, 1)
    # Ignore any sends that happened during setup — we only want to verify
    # that skip itself posts a fresh phase message via channel.send.
    channel.send.reset_mock()
    try:
        assert await manager.skip(state.room_id, 1) is OpResult.OK
        assert state.phase is Phase.SHORT_BREAK
        assert state.completed_work_phases == 0
        async with async_session() as db:
            pomos = (await db.execute(select(Pomodoro))).scalars().all()
            assert pomos == []
        # Skip is a phase boundary → new phase message posted.
        assert channel.send.await_count == 1
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_reset_owner_keeps_current_phase_and_rewinds_timer() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    await manager.join(state.room_id, 1)
    try:
        assert await manager.skip(state.room_id, 1) is OpResult.OK
        assert state.phase is Phase.SHORT_BREAK

        before = datetime.now(UTC) - timedelta(seconds=1)
        state.phase_started_at = before
        assert await manager.reset(state.room_id, 1) is OpResult.OK

        assert state.phase is Phase.SHORT_BREAK
        assert state.phase_started_at > before
        assert not state.is_paused
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_update_plan_owner_updates_room_cycle_and_resets_round() -> None:
    manager = _manager()
    state, channel = await _spawn_room(manager, creator=1)
    await manager.join(state.room_id, 1)
    channel.send.reset_mock()
    try:
        before = datetime.now(UTC) - timedelta(seconds=1)
        state.phase = Phase.SHORT_BREAK
        state.phase_started_at = before
        state.completed_work_phases = 2

        plan = PhasePlan(
            work_seconds=30 * 60,
            short_break_seconds=7 * 60,
            long_break_seconds=20 * 60,
            long_break_every=3,
        )
        assert await manager.update_plan(state.room_id, 1, plan=plan) is OpResult.OK

        assert state.plan == plan
        assert state.phase is Phase.WORK
        assert state.completed_work_phases == 0
        assert state.phase_started_at > before

        async with async_session() as db:
            room = await db.get(PomodoroRoom, state.room_id)
            assert room is not None
            assert room.work_seconds == 30 * 60
            assert room.short_break_seconds == 7 * 60
            assert room.long_break_seconds == 20 * 60
            assert room.long_break_every == 3
        # Update during running resets to WORK 1 → post fresh phase message.
        assert channel.send.await_count == 1
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_update_plan_during_setup_does_not_touch_timing() -> None:
    """Before the timer starts, updating the plan persists the new values
    but must not mutate phase/completed counters (there's nothing to
    "reset" in setup state) and must NOT post a phase message — no
    timer is running yet.
    """
    manager = _manager()
    state, channel = await _spawn_room(manager, creator=1, running=False)
    channel.send.reset_mock()
    try:
        original_start = state.phase_started_at
        plan = PhasePlan(30 * 60, 7 * 60, 20 * 60, 3)
        assert await manager.update_plan(state.room_id, 1, plan=plan) is OpResult.OK
        assert state.plan == plan
        # Setup state: phase_started_at is unchanged.
        assert state.phase_started_at == original_start
        # No phase message posted while in setup.
        assert channel.send.await_count == 0
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_update_plan_prevents_surprise_long_break() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    await manager.join(state.room_id, 1)
    try:
        state.completed_work_phases = 3
        plan = PhasePlan(
            work_seconds=25 * 60,
            short_break_seconds=5 * 60,
            long_break_seconds=15 * 60,
            long_break_every=2,
        )
        assert await manager.update_plan(state.room_id, 1, plan=plan) is OpResult.OK
        assert state.completed_work_phases == 0
    finally:
        await manager.end(state.room_id, reason="test")


@pytest.mark.asyncio
async def test_update_plan_rejected_for_non_owner() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    await manager.join(state.room_id, 2)
    try:
        plan = PhasePlan(20 * 60, 5 * 60, 10 * 60, 4)
        assert (
            await manager.update_plan(state.room_id, 2, plan=plan) is OpResult.NOT_OWNER
        )
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


@pytest.mark.asyncio
async def test_set_notify_owner_only_and_per_phase() -> None:
    manager = _manager()
    state, _ = await _spawn_room(manager, creator=1)
    await manager.join(state.room_id, 2)
    try:
        # Defaults are all on so no one misses transitions.
        assert state.notify_work is True
        assert state.notify_short_break is True
        assert state.notify_long_break is True

        # Non-owner cannot toggle.
        result = await manager.set_notify(
            state.room_id, 2, phase=Phase.WORK, enabled=False
        )
        assert result is OpResult.NOT_OWNER
        assert state.notify_work is True

        # Owner toggles only the requested phase.
        result = await manager.set_notify(
            state.room_id, 1, phase=Phase.SHORT_BREAK, enabled=False
        )
        assert result is OpResult.OK
        assert state.notify_short_break is False
        assert state.notify_work is True
        assert state.notify_long_break is True
    finally:
        await manager.end(state.room_id, reason="test")


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
    await manager.leave(state.room_id, 3)

    try:
        await manager._handle_phase_end(state)
        assert state.phase is Phase.SHORT_BREAK
        assert state.completed_work_phases == 1

        async with async_session() as db:
            pomos = (await db.execute(select(Pomodoro))).scalars().all()
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
    manager = _manager()
    r1, _ = await _spawn_room(manager, creator=1, channel_id=111)
    r2, _ = await _spawn_room(manager, creator=2, channel_id=222)
    try:
        assert await manager.join(r1.room_id, 100) is OpResult.OK
        assert await manager.join(r2.room_id, 200) is OpResult.OK

        results = await asyncio.wait_for(
            asyncio.gather(
                manager.join(r2.room_id, 100),
                manager.join(r1.room_id, 200),
            ),
            timeout=5.0,
        )
        assert results == [OpResult.OK, OpResult.OK]

        assert r1.has_participant(200) and not r1.has_participant(100)
        assert r2.has_participant(100) and not r2.has_participant(200)
    finally:
        await manager.end(r1.room_id, reason="test")
        await manager.end(r2.room_id, reason="test")


@pytest.mark.asyncio
async def test_create_setup_raises_integrity_error_for_second_room_in_channel() -> None:
    manager = _manager()
    r1, _ = await _spawn_room(manager, creator=1, channel_id=500)
    try:
        with pytest.raises(IntegrityError):
            await manager.create_setup(guild_id=None, channel_id=500, created_by=2)
        assert manager.get(r1.room_id) is r1
        async with async_session() as db:
            rows = (await db.execute(select(PomodoroRoom))).scalars().all()
            assert len(rows) == 1
    finally:
        await manager.end(r1.room_id, reason="test")


@pytest.mark.asyncio
async def test_ending_old_room_frees_channel_for_a_new_one() -> None:
    manager = _manager()
    r1, _ = await _spawn_room(manager, creator=1, channel_id=777)
    await manager.join(r1.room_id, 1)

    await manager.end(r1.room_id, reason="superseded")
    assert manager.get(r1.room_id) is None

    r2 = await manager.create_setup(guild_id=None, channel_id=777, created_by=2)
    try:
        assert r2.room_id != r1.room_id
        assert r2.created_by == 2

        async with async_session() as db:
            old = await db.get(PomodoroRoom, r1.room_id)
            new = await db.get(PomodoroRoom, r2.room_id)
            assert old is not None and old.ended_reason == "superseded"
            assert new is not None and new.ended_at is None
    finally:
        await manager.end(r2.room_id, reason="test")


# ---------------------------------------------------------------------------
# Regression: the natural-timeout branch of _run_phase_loop must call
# _handle_phase_end and advance the phase. We force this by giving WORK a
# 0-second duration and letting the loop's wait_for time out immediately.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_loop_advances_phase_on_natural_timeout() -> None:
    # Zero-length WORK → the loop's wait_for returns TimeoutError on the
    # very first iteration, which is what triggers ``_handle_phase_end``.
    manager = RoomManager(
        default_plan=PhasePlan(
            work_seconds=0,
            short_break_seconds=3600,
            long_break_seconds=3600,
            long_break_every=4,
        ),
    )
    state, _ = await _spawn_room(manager, creator=1, running=False)
    await manager.join(state.room_id, 1, task="focus")

    try:
        assert await manager.begin_phases(state.room_id, 1) is OpResult.OK

        # Give the loop a moment to process the timeout → phase end.
        # Polling is intentional here: we're watching an externally-
        # scheduled task flip state. A sync primitive would require
        # threading test-only instrumentation into production code.
        async def _wait_for_advance() -> None:
            while state.phase is Phase.WORK:  # noqa: ASYNC110
                await asyncio.sleep(0.02)

        await asyncio.wait_for(_wait_for_advance(), timeout=3.0)

        # WORK naturally ended → SHORT_BREAK, completed count ticked up,
        # and a pomodoro row landed in the DB.
        assert state.phase is Phase.SHORT_BREAK
        assert state.completed_work_phases == 1
        async with async_session() as db:
            pomos = (await db.execute(select(Pomodoro))).scalars().all()
            assert len(pomos) == 1
            assert pomos[0].user_id == 1
            assert pomos[0].task == "focus"
    finally:
        await manager.end(state.room_id, reason="test")
