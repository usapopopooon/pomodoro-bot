from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.constants import PHASE_COLOR_ENDED
from src.core.phase import Phase, PhasePlan
from src.core.room_state import RoomState
from src.ui.embeds import (
    control_panel_embed,
    ended_embed,
    phase_announcement_content,
    stats_embed,
)


def _state(
    *,
    phase: Phase = Phase.WORK,
    elapsed_seconds: int = 0,
    completed: int = 0,
    participants: dict[int, str | None] | None = None,
    owner: int = 1,
    paused: bool = False,
    has_started: bool = True,
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
    state.has_started = has_started
    state.phase = phase
    state.phase_started_at = datetime.now(UTC) - timedelta(seconds=elapsed_seconds)
    state.completed_work_phases = completed
    for uid, task in (participants or {}).items():
        state.add_participant(uid, task=task)
    if paused:
        state.pause()
    return state


# ---------------------------------------------------------------------------
# Control Panel embed
# ---------------------------------------------------------------------------


def test_control_panel_shows_not_started_hint_before_start() -> None:
    embed = control_panel_embed(_state(has_started=False))
    assert "未開始" in (embed.description or "")
    assert "開始" in (embed.description or "")


def test_control_panel_shows_running_phase_label_after_start() -> None:
    embed = control_panel_embed(_state(phase=Phase.WORK, has_started=True))
    # Phase label appears in description
    assert Phase.WORK.label_ja in (embed.description or "")


def test_control_panel_shows_pause_marker_when_paused() -> None:
    embed = control_panel_embed(_state(phase=Phase.WORK, has_started=True, paused=True))
    assert "一時停止" in (embed.description or "")


def test_control_panel_lists_plan_summary() -> None:
    embed = control_panel_embed(_state())
    plan_field = next(f for f in embed.fields if "時間設定" in f.name)
    assert "25分 作業" in plan_field.value
    assert "5分 短休憩" in plan_field.value
    assert "15分 長休憩" in plan_field.value
    assert "× 4" in plan_field.value


def test_control_panel_lists_participants_with_crown() -> None:
    embed = control_panel_embed(
        _state(owner=1, participants={1: "math", 2: "english", 3: None})
    )
    participants_field = next(f for f in embed.fields if "参加者" in f.name)
    assert "👑" in participants_field.value
    assert "<@1>" in participants_field.value
    assert "<@2>" in participants_field.value
    assert "<@3>" in participants_field.value
    assert "math" in participants_field.value
    assert "english" in participants_field.value


def test_control_panel_handles_empty_participants_gracefully() -> None:
    embed = control_panel_embed(_state(participants={}))
    participants_field = next(f for f in embed.fields if "参加者" in f.name)
    assert "(0)" in participants_field.name


# ---------------------------------------------------------------------------
# Ended embed
# ---------------------------------------------------------------------------


def test_ended_embed_uses_ended_color_and_reason() -> None:
    embed = ended_embed(
        _state(completed=3, participants={1: "math"}), reason="owner_ended"
    )
    assert embed.color.value == PHASE_COLOR_ENDED
    assert "owner_ended" in (embed.description or "")
    assert "× 3" in (embed.description or "")


# ---------------------------------------------------------------------------
# Phase announcement content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase", [Phase.WORK, Phase.SHORT_BREAK, Phase.LONG_BREAK])
def test_phase_announcement_content_non_empty_for_every_phase(phase: Phase) -> None:
    msg = phase_announcement_content(
        phase=phase, phase_minutes=25, next_phase_minutes=5
    )
    assert msg
    # Phase label in some form appears in the message
    assert phase.label_ja in msg or phase.name.replace("_", " ").lower() in msg.lower()
    # Mentions both durations
    assert "25" in msg
    assert "5" in msg


def test_phase_announcement_content_mentions_next_phase_for_work() -> None:
    msg = phase_announcement_content(
        phase=Phase.WORK, phase_minutes=25, next_phase_minutes=5
    )
    assert "休憩" in msg


# ---------------------------------------------------------------------------
# Stats embed
# ---------------------------------------------------------------------------


def test_stats_embed_has_three_fields() -> None:
    class _User:
        display_name = "alice"

    embed = stats_embed(_User(), today=2, week=5, total=100)  # type: ignore[arg-type]
    assert len(embed.fields) == 3
    assert {f.name for f in embed.fields} == {"今日", "今週", "累計"}
