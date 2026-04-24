from __future__ import annotations

from datetime import timedelta

import discord

from src.constants import (
    PHASE_COLOR_ENDED,
    PROGRESS_BAR_EMPTY,
    PROGRESS_BAR_FILLED,
    PROGRESS_BAR_LENGTH,
)
from src.core.phase import Phase
from src.core.room_state import RoomState


def _progress_bar(ratio: float) -> str:
    filled = max(0, min(PROGRESS_BAR_LENGTH, round(ratio * PROGRESS_BAR_LENGTH)))
    empty = PROGRESS_BAR_LENGTH - filled
    return PROGRESS_BAR_FILLED * filled + PROGRESS_BAR_EMPTY * empty


def _format_clock(td: timedelta) -> str:
    total = max(0, int(td.total_seconds()))
    return f"{total // 60:02d}:{total % 60:02d}"


def _format_participants(state: RoomState) -> str:
    if not state.participants:
        return "_参加者はまだいません。🙋 参加 から始めましょう。_"

    lines: list[str] = []
    # Stable ordering: earliest joiner first.
    ordered = sorted(state.participants.values(), key=lambda p: p.joined_at)
    for p in ordered:
        marker = "👑" if p.user_id == state.created_by else "•"
        task = p.task if p.task else "—"
        lines.append(f"{marker} <@{p.user_id}> — {task}")
    return "\n".join(lines)


def room_embed(state: RoomState) -> discord.Embed:
    remaining = state.remaining()
    duration = timedelta(seconds=state.phase_duration_seconds)
    elapsed = duration - remaining
    total = duration.total_seconds()
    ratio = elapsed.total_seconds() / total if total else 0

    phase = state.phase
    title = f"🍅 ポモドーロルーム - {phase.label_ja}"
    if state.is_paused:
        title += "(一時停止中)"

    description_lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏱  {_format_clock(elapsed)} / {_format_clock(duration)}",
        f"{_progress_bar(ratio)}  {int(ratio * 100)}%",
        "",
        f"🍅 このラウンドの完了: {state.completed_work_phases} 個",
    ]

    embed = discord.Embed(
        title=title,
        description="\n".join(description_lines),
        color=phase.color,
    )
    embed.add_field(
        name=f"👥 参加者 ({len(state.participants)})",
        value=_format_participants(state),
        inline=False,
    )
    embed.set_footer(text=f"room: {state.room_id}")
    return embed


def ended_embed(state: RoomState, reason: str) -> discord.Embed:
    return discord.Embed(
        title="🍅 ルーム終了",
        description=(
            f"完了したラウンド: 🍅 × {state.completed_work_phases}\n"
            f"最終参加者: {len(state.participants)} 人\n"
            f"終了理由: `{reason}`"
        ),
        color=PHASE_COLOR_ENDED,
    )


def stats_embed(
    user: discord.abc.User, today: int, week: int, total: int
) -> discord.Embed:
    embed = discord.Embed(
        title=f"📊 {user.display_name} の完了数",
        color=0xF1C40F,
    )
    embed.add_field(name="今日", value=f"🍅 × {today}", inline=True)
    embed.add_field(name="今週", value=f"🍅 × {week}", inline=True)
    embed.add_field(name="累計", value=f"🍅 × {total}", inline=True)
    return embed


def transition_message(next_phase: Phase, completed: int) -> str:
    match next_phase:
        case Phase.WORK:
            return f"🍅 作業開始!(これまで {completed} 🍅)"
        case Phase.SHORT_BREAK:
            return f"☕ 短休憩です。(完了 {completed} 🍅)"
        case Phase.LONG_BREAK:
            return f"🛌 長休憩です。お疲れさま!(完了 {completed} 🍅)"
