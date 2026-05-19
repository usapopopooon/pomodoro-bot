from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.core.phase import Phase, PhasePlan
from src.core.room_state import ParticipantState, RoomState
from src.room_manager import RoomManager


def _state_with_channel(channel: SimpleNamespace) -> RoomState:
    state = RoomState(
        room_id=uuid4(),
        guild_id=None,
        channel_id=123,
        created_by=1,
        plan=PhasePlan(10, 2, 4, 2),
        has_started=True,
    )
    state.message = SimpleNamespace(id=111, channel=channel)
    state.participants[1] = ParticipantState(user_id=1)
    state.notify_work = True
    return state


@pytest.mark.asyncio
async def test_phase_ping_replacement_is_serialized() -> None:
    manager = RoomManager(default_plan=PhasePlan(10, 2, 4, 2))
    channel = SimpleNamespace(id=123)
    send_started = asyncio.Event()
    release_first_send = asyncio.Event()
    messages: list[SimpleNamespace] = []

    async def send(*args: object, **kwargs: object) -> SimpleNamespace:
        msg = SimpleNamespace(id=200 + len(messages), delete=AsyncMock())
        messages.append(msg)
        if len(messages) == 1:
            send_started.set()
            await release_first_send.wait()
        return msg

    channel.send = AsyncMock(side_effect=send)
    state = _state_with_channel(channel)

    first = asyncio.create_task(manager._post_phase_ping(state))
    await send_started.wait()
    second = asyncio.create_task(manager._post_phase_ping(state))
    await asyncio.sleep(0)

    assert channel.send.await_count == 1

    release_first_send.set()
    await asyncio.gather(first, second)

    assert channel.send.await_count == 2
    messages[0].delete.assert_awaited_once()
    messages[1].delete.assert_not_awaited()
    assert state.last_phase_ping_message is messages[1]


@pytest.mark.asyncio
async def test_phase_ping_notify_off_clears_previous_without_reposting() -> None:
    manager = RoomManager(default_plan=PhasePlan(10, 2, 4, 2))
    first_ping = SimpleNamespace(id=200, delete=AsyncMock())
    channel = SimpleNamespace(id=123, send=AsyncMock())
    state = _state_with_channel(channel)
    state.last_phase_ping_message = first_ping
    state.phase = Phase.SHORT_BREAK

    await manager._post_phase_ping(state)

    first_ping.delete.assert_awaited_once()
    channel.send.assert_not_awaited()
    assert state.last_phase_ping_message is None
