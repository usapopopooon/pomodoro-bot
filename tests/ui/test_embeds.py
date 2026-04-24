from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import discord
import pytest

from src.constants import (
    PHASE_COLOR_ENDED,
    PHASE_COLOR_LONG_BREAK,
    PHASE_COLOR_SHORT_BREAK,
    PHASE_COLOR_WORK,
    PROGRESS_BAR_EMPTY,
    PROGRESS_BAR_FILLED,
    PROGRESS_BAR_LENGTH,
)
from src.core.phase import Phase, PhasePlan
from src.core.room_state import RoomState
from src.ui.embeds import (
    ended_embed,
    room_embed,
    stats_embed,
    transition_message,
)


def _state(
    *,
    phase: Phase = Phase.WORK,
    elapsed_seconds: int = 0,
    completed: int = 0,
    participants: dict[int, str | None] | None = None,
    owner: int = 1,
    paused: bool = False,
) -> RoomState:
    plan = PhasePlan(
        work_seconds=1500,
        short_break_seconds=300,
        long_break_seconds=900,
        long_break_every=4,
    )
    state = RoomState(
        room_id=uuid4(),
        guild_id=None,
        channel_id=1,
        created_by=owner,
        plan=plan,
    )
    state.phase = phase
    state.phase_started_at = datetime.now(UTC) - timedelta(seconds=elapsed_seconds)
    state.completed_work_phases = completed
    for uid, task in (participants or {}).items():
        state.add_participant(uid, task=task)
    if paused:
        state.pause()
    return state


def test_room_embed_color_matches_phase() -> None:
    assert room_embed(_state(phase=Phase.WORK)).color.value == PHASE_COLOR_WORK
    assert (
        room_embed(_state(phase=Phase.SHORT_BREAK)).color.value
        == PHASE_COLOR_SHORT_BREAK
    )
    assert (
        room_embed(_state(phase=Phase.LONG_BREAK)).color.value == PHASE_COLOR_LONG_BREAK
    )


def test_room_embed_progress_bar_length_is_fixed() -> None:
    embed = room_embed(_state(phase=Phase.WORK, elapsed_seconds=750))
    description = embed.description or ""
    bar_chars = PROGRESS_BAR_FILLED + PROGRESS_BAR_EMPTY
    bar_line = next(
        line for line in description.splitlines() if any(c in line for c in bar_chars)
    )
    bar_only = "".join(c for c in bar_line if c in bar_chars)
    assert len(bar_only) == PROGRESS_BAR_LENGTH


def test_room_embed_progress_ratio_is_monotonic() -> None:
    early = room_embed(_state(phase=Phase.WORK, elapsed_seconds=10))
    late = room_embed(_state(phase=Phase.WORK, elapsed_seconds=1200))

    def _filled_count(embed: discord.Embed) -> int:
        desc = embed.description or ""
        return sum(line.count(PROGRESS_BAR_FILLED) for line in desc.splitlines())

    assert _filled_count(late) > _filled_count(early)


def test_room_embed_shows_pause_marker_when_paused() -> None:
    embed = room_embed(_state(paused=True))
    assert "一時停止" in (embed.title or "")


def test_room_embed_lists_participants_with_crown_for_owner() -> None:
    embed = room_embed(
        _state(
            owner=1,
            participants={1: "math", 2: "english", 3: None},
        )
    )
    participants_field = next(f for f in embed.fields if "参加者" in f.name)
    assert "👑" in participants_field.value  # owner marker
    assert "<@1>" in participants_field.value
    assert "<@2>" in participants_field.value
    assert "math" in participants_field.value
    assert "english" in participants_field.value
    # User 3 has no task → rendered as "—"
    assert "<@3>" in participants_field.value


def test_room_embed_handles_empty_participants_gracefully() -> None:
    embed = room_embed(_state(participants={}))
    participants_field = next(f for f in embed.fields if "参加者" in f.name)
    assert "参加者" in participants_field.name
    assert "(0)" in participants_field.name


def test_ended_embed_uses_ended_color_and_reason() -> None:
    embed = ended_embed(
        _state(completed=3, participants={1: "math"}), reason="owner_ended"
    )
    assert embed.color.value == PHASE_COLOR_ENDED
    assert "owner_ended" in (embed.description or "")
    assert "× 3" in (embed.description or "")


def test_stats_embed_has_three_fields() -> None:
    class _User:
        display_name = "alice"

    embed = stats_embed(_User(), today=2, week=5, total=100)  # type: ignore[arg-type]
    assert len(embed.fields) == 3
    assert {f.name for f in embed.fields} == {"今日", "今週", "累計"}


@pytest.mark.parametrize("phase", [Phase.WORK, Phase.SHORT_BREAK, Phase.LONG_BREAK])
def test_transition_message_is_non_empty_for_every_phase(phase: Phase) -> None:
    msg = transition_message(phase, completed=2)
    assert msg
    assert "2" in msg
