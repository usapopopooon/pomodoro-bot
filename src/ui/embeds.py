"""Embed and message-content builders.

Two surfaces:

* **Control Panel** — persistent message that hosts configuration + roster.
  One per room, edited in place.
* **Phase announcement** — transient per-phase text message (with a timer
  image attached). A fresh message is posted each time the phase flips.

The LionBot reference uses plain text content + an image attachment for the
phase surface, and an embed-with-fields for the control surface; we match
that split.
"""

from __future__ import annotations

import discord

from src.constants import PHASE_COLOR_ENDED
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
    """Config + roster embed shown at the Control Panel message.

    While in *setup* state the primary affordance is the Start button; while
    *running* the embed shows the current phase. The embed color switches
    between phase-color-when-running and a neutral blue when in setup.
    """
    color = state.phase.color if state.has_started else 0x7289DA

    if state.has_started:
        status_line = f"**{state.phase.label_ja}** セッション実行中"
        if state.is_paused:
            status_line += "(一時停止中)"
    else:
        status_line = "**未開始** — オーナーが ▶️ 開始 を押すと始まります"

    embed = discord.Embed(
        title="🍅 ポモドーロ コントロールパネル",
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
        title="🍅 ポモドーロ終了",
        description=(
            f"完了したラウンド: 🍅 × {state.completed_work_phases}\n"
            f"最終参加者: {len(state.participants)} 人\n"
            f"終了理由: `{reason}`"
        ),
        color=PHASE_COLOR_ENDED,
    )


# ---------------------------------------------------------------------------
# Phase-transition message (content + attachment; no embed)
# ---------------------------------------------------------------------------


def phase_announcement_content(
    *,
    phase: Phase,
    phase_minutes: int,
    next_phase_minutes: int,
) -> str:
    """Plain-text content for the phase-transition message.

    Matches LionBot's format ``"... is now in FOCUS! BREAK starts 25分後"``.
    The image attachment carries the visual; the text explains what happened
    in terms accessible to screen readers and inline previews.
    """
    if phase is Phase.WORK:
        return (
            f"🍅 **{phase.label_ja}** フェーズ開始! "
            f"({phase_minutes} 分間集中 → **休憩 {next_phase_minutes} 分**)"
        )
    if phase is Phase.SHORT_BREAK:
        return (
            f"☕ **{phase.label_ja}** です。お疲れさま! "
            f"({phase_minutes} 分 → 次は **作業 {next_phase_minutes} 分**)"
        )
    return (
        f"🛌 **{phase.label_ja}** です。しっかり休んでください! "
        f"({phase_minutes} 分 → 次は **作業 {next_phase_minutes} 分**)"
    )


# ---------------------------------------------------------------------------
# Stats (kept from previous revision)
# ---------------------------------------------------------------------------


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
