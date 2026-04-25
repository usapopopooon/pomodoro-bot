"""Embed and message-content builders.

Two surfaces:

* **Control Panel** — persistent message that hosts configuration + roster.
  One per room, edited in place.
* **Phase message** — transient per-phase text message with an ASCII
  progress bar that ticks via periodic edits. A fresh message is posted
  on phase boundaries (natural end, skip, plan-reset). Pause / reset /
  periodic ticks edit the current message in place.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord

from src.constants import (
    PHASE_COLOR_ENDED,
    PROGRESS_BAR_EMPTY,
    PROGRESS_BAR_FILLED,
    PROGRESS_BAR_LENGTH,
)
from src.core.phase import Phase
from src.core.room_state import RoomState

# ---------------------------------------------------------------------------
# Control Panel (persistent message)
# ---------------------------------------------------------------------------


def _format_plan_summary(state: RoomState) -> str:
    p = state.plan
    return (
        f"{p.work_seconds // 60}分 作業 / "
        f"{p.short_break_seconds // 60}分 短休憩 / "
        f"{p.long_break_seconds // 60}分 長休憩 × "
        f"{p.long_break_every} サイクルで長休憩"
    )


def _format_participants(state: RoomState) -> str:
    if not state.participants:
        return "_まだ誰も参加していません。🙋 参加 を押してください。_"
    ordered = sorted(state.participants.values(), key=lambda p: p.joined_at)
    lines: list[str] = []
    for p in ordered:
        marker = "👑" if p.user_id == state.created_by else "•"
        task = p.task or "—"
        lines.append(f"{marker} <@{p.user_id}> — {task}")
    return "\n".join(lines)


def control_panel_embed(state: RoomState) -> discord.Embed:
    color = state.phase.color if state.has_started else 0x7289DA

    if state.has_started:
        status_line = f"**{state.phase.label_ja}** セッション実行中"
        if state.is_paused:
            status_line += "(一時停止中)"
    else:
        status_line = "**未開始** — オーナーが ▶️ 開始 を押すと始まります"

    embed = discord.Embed(
        title="🎛 ポモドーロ コントロールパネル",
        description=status_line,
        color=color,
    )
    embed.add_field(name="⏱ 時間設定", value=_format_plan_summary(state), inline=False)
    embed.add_field(
        name=f"👥 参加者 ({len(state.participants)})",
        value=_format_participants(state),
        inline=False,
    )
    embed.set_footer(text=f"room: {state.room_id}")
    return embed


def ended_embed(state: RoomState, reason: str) -> discord.Embed:
    return discord.Embed(
        title="🏁 ポモドーロ終了",
        description=(
            f"完了したラウンド: ✅ × {state.completed_work_phases}\n"
            f"最終参加者: {len(state.participants)} 人\n"
            f"終了理由: `{reason}`"
        ),
        color=PHASE_COLOR_ENDED,
    )


# ---------------------------------------------------------------------------
# Phase message (content only — no embed, no attachment)
# ---------------------------------------------------------------------------


_PHASE_ICON: dict[Phase, str] = {
    Phase.WORK: "⏰",
    Phase.SHORT_BREAK: "☕",
    Phase.LONG_BREAK: "🛌",
}


def _format_minutes(seconds: int) -> str:
    """Minute-granular clock: ``5分`` — matches the 1-minute refresh cadence.

    Sub-minute precision would only advance the bar (not the text) between
    refreshes, which reads inconsistently. Flooring to whole minutes keeps
    both in lockstep.
    """
    return f"{max(0, seconds) // 60}分"


def _progress_bar(ratio: float) -> str:
    ratio = max(0.0, min(1.0, ratio))
    filled = round(ratio * PROGRESS_BAR_LENGTH)
    return PROGRESS_BAR_FILLED * filled + PROGRESS_BAR_EMPTY * (
        PROGRESS_BAR_LENGTH - filled
    )


def phase_content(state: RoomState, *, now: datetime | None = None) -> str:
    """Two-line content for the phase message.

    Line 1: phase label + pause marker (if paused).
    Line 2: monospace ASCII progress bar with minute-granular elapsed/total
    (``5分 / 25分``) and a Discord relative timestamp (``<t:UNIX:R>``) that
    ticks on the client side. The timestamp is omitted while paused since
    a future instant would keep counting down regardless of pause state.
    """
    now = now or datetime.now(UTC)
    duration = state.phase_duration_seconds
    elapsed = int(max(0, state.elapsed(now).total_seconds()))
    remaining = max(0, duration - elapsed)
    ratio = (elapsed / duration) if duration else 0.0

    bar = _progress_bar(ratio)
    icon = _PHASE_ICON[state.phase]
    label = state.phase.label_ja

    header = f"{icon} **{label}**"
    if state.is_paused:
        header += " ⏸ **一時停止中**"

    bar_line = f"`{bar} {_format_minutes(elapsed)} / {_format_minutes(duration)}`"
    if not state.is_paused:
        end_unix = int((now + timedelta(seconds=remaining)).timestamp())
        bar_line += f" — 終了 <t:{end_unix}:R>"

    return f"{header}\n{bar_line}"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def stats_embed(
    user: discord.abc.User, today: int, week: int, total: int
) -> discord.Embed:
    embed = discord.Embed(
        title=f"📊 {user.display_name} の完了数",
        color=0xF1C40F,
    )
    embed.add_field(name="今日", value=f"✅ × {today}", inline=True)
    embed.add_field(name="今週", value=f"✅ × {week}", inline=True)
    embed.add_field(name="累計", value=f"✅ × {total}", inline=True)
    return embed
