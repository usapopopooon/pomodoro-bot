from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.constants import (
    PHASE_COLOR_ENDED,
    PROGRESS_BAR_EMPTY,
    PROGRESS_BAR_FILLED,
    PROGRESS_BAR_LENGTH,
)
from src.core.phase import Phase, PhasePlan
from src.core.room_state import RoomState
from src.ui.embeds import (
    control_panel_embed,
    ended_embed,
    freeze_phase_content,
    phase_content,
    phase_ping_content,
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
    assert "オーナーが終了" in (embed.description or "")
    assert "× 3" in (embed.description or "")


def test_ended_embed_falls_back_to_raw_reason_for_unknown_code() -> None:
    embed = ended_embed(_state(), reason="something_new")
    assert "something_new" in (embed.description or "")


# ---------------------------------------------------------------------------
# Phase content (ASCII bar + Discord timestamp)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase", [Phase.WORK, Phase.SHORT_BREAK, Phase.LONG_BREAK])
def test_phase_content_non_empty_for_every_phase(phase: Phase) -> None:
    msg = phase_content(_state(phase=phase, has_started=True, elapsed_seconds=0))
    assert msg
    assert phase.label_ja in msg


def test_phase_content_bar_grows_with_elapsed_time() -> None:
    early = phase_content(_state(elapsed_seconds=30, has_started=True))
    late = phase_content(_state(elapsed_seconds=1200, has_started=True))
    assert early.count(PROGRESS_BAR_FILLED) < late.count(PROGRESS_BAR_FILLED)


def test_phase_content_bar_length_is_constant() -> None:
    msg = phase_content(_state(elapsed_seconds=500, has_started=True))
    # Count only bar chars inside the backticked line.
    bar_chars = PROGRESS_BAR_FILLED + PROGRESS_BAR_EMPTY
    only_bar = "".join(c for c in msg if c in bar_chars)
    assert len(only_bar) == PROGRESS_BAR_LENGTH


def test_phase_content_shows_minute_clock() -> None:
    # 315 seconds into a 1500-second WORK → floored to 5分 / 25分.
    msg = phase_content(_state(elapsed_seconds=315, has_started=True))
    assert "5分 / 25分" in msg


def test_phase_content_floors_sub_minute_elapsed_to_zero() -> None:
    # 45 seconds in → still "0分" until we cross the 1-minute mark, so the
    # clock and the per-minute refresh cadence stay in lockstep.
    msg = phase_content(_state(elapsed_seconds=45, has_started=True))
    assert "0分 / 25分" in msg


def test_phase_content_includes_discord_relative_timestamp_when_running() -> None:
    msg = phase_content(_state(elapsed_seconds=30, has_started=True))
    # <t:UNIX:R> for client-side live countdown
    assert "<t:" in msg
    assert ":R>" in msg


def test_phase_content_drops_timestamp_and_marks_paused_when_paused() -> None:
    msg = phase_content(_state(elapsed_seconds=30, has_started=True, paused=True))
    # Paused badge
    assert "一時停止" in msg
    # Discord relative timestamp would keep ticking regardless of pause,
    # so it's intentionally absent.
    assert "<t:" not in msg


def test_phase_content_drops_timestamp_once_phase_is_complete() -> None:
    # Bar is full — the timestamp would drift into "X minutes ago", which is
    # noise. Drop it at completion.
    msg = phase_content(_state(elapsed_seconds=1500, has_started=True))
    assert "<t:" not in msg


def test_phase_content_never_includes_mentions() -> None:
    # Mentions live in the separate ping message now — the live progress
    # message is edit-only, and edits never re-fire user pings, so any
    # mention here would be dead weight.
    state = _state(participants={1: "math", 2: None}, has_started=True)
    msg = phase_content(state)
    assert "||" not in msg
    assert "<@1>" not in msg
    assert "<@2>" not in msg


def test_phase_content_includes_cumulative_round_during_work() -> None:
    state = _state(phase=Phase.WORK, completed=2, has_started=True)
    msg = phase_content(state)
    # During WORK#3 (completed=2 of finished work phases): we're on round 3.
    assert "ラウンド : 3" in msg


def test_phase_content_round_during_break_reflects_just_finished_round() -> None:
    state = _state(phase=Phase.SHORT_BREAK, completed=2, has_started=True)
    msg = phase_content(state)
    # Break following WORK#2 (completed=2): still round 2 — the break
    # belongs to the round whose work just ended.
    assert "ラウンド : 2" in msg


def test_phase_ping_content_returns_spoiler_mention_when_notify_enabled() -> None:
    state = _state(participants={1: "math", 2: None}, has_started=True)
    # Notify defaults to off — owners opt in per phase.
    state.notify_work = True
    ping = phase_ping_content(state)
    assert ping is not None
    assert "||" in ping
    assert "<@1>" in ping
    assert "<@2>" in ping
    assert Phase.WORK.label_ja in ping


def test_phase_ping_content_returns_none_when_notify_disabled() -> None:
    # Notify defaults to off — this is the no-op case.
    state = _state(phase=Phase.SHORT_BREAK, participants={1: "math"}, has_started=True)
    assert phase_ping_content(state) is None


def test_phase_ping_content_returns_none_when_no_participants() -> None:
    state = _state(participants={}, has_started=True)
    state.notify_work = True
    assert phase_ping_content(state) is None


# ---------------------------------------------------------------------------
# freeze_phase_content — strips live ``<t:...:R>`` so old messages stop ticking
# ---------------------------------------------------------------------------


def test_freeze_phase_content_removes_live_timestamp_line() -> None:
    msg = phase_content(_state(elapsed_seconds=30, has_started=True))
    assert "<t:" in msg  # sanity: running phase has the live timestamp
    frozen = freeze_phase_content(msg)
    assert "<t:" not in frozen
    # The rest of the message survives so history is still readable.
    assert Phase.WORK.label_ja in frozen
    assert PROGRESS_BAR_FILLED in frozen or PROGRESS_BAR_EMPTY in frozen


def test_freeze_phase_content_is_noop_when_no_timestamp() -> None:
    msg = phase_content(_state(elapsed_seconds=1500, has_started=True))
    assert "<t:" not in msg
    assert freeze_phase_content(msg) == msg


# ---------------------------------------------------------------------------
# Stats embed
# ---------------------------------------------------------------------------


def test_stats_embed_has_three_fields() -> None:
    class _User:
        display_name = "alice"

    embed = stats_embed(_User(), today=2, week=5, total=100)  # type: ignore[arg-type]
    assert len(embed.fields) == 3
    assert {f.name for f in embed.fields} == {"今日", "今週", "累計"}
