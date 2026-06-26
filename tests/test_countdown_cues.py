from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.core.phase import PhasePlan
from src.core.room_state import RoomState
from src.room_manager import RoomManager


def _state(*, plan: PhasePlan | None = None) -> RoomState:
    return RoomState(
        room_id=uuid4(),
        guild_id=999,
        channel_id=123,
        created_by=1,
        plan=plan or PhasePlan(1500, 300, 900, 4),
    )


def _manager_for(state: RoomState) -> tuple[RoomManager, MagicMock]:
    voice = MagicMock()
    voice.is_connected = MagicMock(return_value=True)
    voice.play_clip = AsyncMock(return_value=True)
    manager = RoomManager(default_plan=state.plan, voice_manager=voice)
    assert state.guild_id is not None
    manager._voice_room_by_guild[state.guild_id] = state.room_id
    return manager, voice


def _played_clip_names(voice: MagicMock) -> list[str]:
    return [call.args[1] for call in voice.play_clip.await_args_list]


def test_countdown_cue_played_flags_reset_on_phase_advance() -> None:
    state = _state()
    state.five_minutes_cue_played = True
    state.one_minute_cue_played = True

    state.advance_phase(count_completion=False)

    assert state.five_minutes_cue_played is False
    assert state.one_minute_cue_played is False


def test_countdown_cue_played_flags_reset_on_explicit_phase_reset() -> None:
    state = _state()
    state.five_minutes_cue_played = True
    state.one_minute_cue_played = True

    state.reset_current_phase()

    assert state.five_minutes_cue_played is False
    assert state.one_minute_cue_played is False


@pytest.mark.asyncio
async def test_maybe_play_five_minutes_cue_skipped_when_already_played() -> None:
    state = _state()
    manager, voice = _manager_for(state)
    state.five_minutes_cue_played = True

    assert await manager._maybe_play_five_minutes_cue(state) is False
    assert "five-minutes-left" not in _played_clip_names(voice)


@pytest.mark.asyncio
async def test_maybe_play_five_minutes_cue_skipped_for_short_phase() -> None:
    state = _state(plan=PhasePlan(300, 300, 900, 4))
    manager, voice = _manager_for(state)

    assert await manager._maybe_play_five_minutes_cue(state) is False
    assert state.five_minutes_cue_played is False
    assert "five-minutes-left" not in _played_clip_names(voice)


@pytest.mark.asyncio
async def test_maybe_play_five_minutes_cue_fires_in_final_five_minutes() -> None:
    plan = PhasePlan(1500, 300, 900, 4)
    state = _state(plan=plan)
    manager, voice = _manager_for(state)
    state.phase_started_at = datetime.now(UTC) - timedelta(
        seconds=plan.work_seconds - 299
    )

    assert await manager._maybe_play_five_minutes_cue(state) is True

    assert state.five_minutes_cue_played is True
    voice.play_clip.assert_any_await(999, "five-minutes-left")


@pytest.mark.asyncio
async def test_maybe_play_five_minutes_cue_skipped_in_final_minute() -> None:
    plan = PhasePlan(1500, 300, 900, 4)
    state = _state(plan=plan)
    manager, voice = _manager_for(state)
    state.phase_started_at = datetime.now(UTC) - timedelta(
        seconds=plan.work_seconds - 30
    )

    assert await manager._maybe_play_five_minutes_cue(state) is False

    assert state.five_minutes_cue_played is False
    assert "five-minutes-left" not in _played_clip_names(voice)
