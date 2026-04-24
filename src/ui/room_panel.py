from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import discord

from src.ui.embeds import stats_embed

if TYPE_CHECKING:
    from src.room_manager import RoomManager

from src.room_manager import OpResult

REJECT_MESSAGES: dict[OpResult, str] = {
    OpResult.NOT_OWNER: "この操作はルームのオーナーのみ可能です。",
    OpResult.NOT_A_PARTICIPANT: "このルームに参加してから操作してください。",
    OpResult.ALREADY_JOINED: "すでに参加しています。",
    OpResult.ROOM_NOT_FOUND: (
        "このルームは見つかりません。パネルを作り直してください。"
    ),
    OpResult.ANOTHER_ROOM: (
        "他のルームに参加中です。そちらを退出してから参加してください。"
    ),
}


async def _ephemeral(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def _reply(
    interaction: discord.Interaction,
    result: OpResult,
    *,
    ok_text: str,
) -> None:
    if result is OpResult.OK:
        await _ephemeral(interaction, ok_text)
    else:
        await _ephemeral(
            interaction, REJECT_MESSAGES.get(result, "操作に失敗しました。")
        )


class TaskModal(discord.ui.Modal):
    """Lets a participant set their own task for the round.

    The ``TextInput`` is built per-instance and added via ``add_item`` rather
    than declared as a class attribute. Class-level UI items in discord.py are
    shared templates; mutating ``.default`` on them (to prefill with the
    caller's current task) can leak across concurrent modal opens. Building
    it in ``__init__`` keeps every invocation fully isolated.
    """

    def __init__(
        self, manager: RoomManager, room_id: UUID, prefill: str | None
    ) -> None:
        super().__init__(title="タスクを編集", timeout=300)
        self._manager = manager
        self._room_id = room_id
        self.task_input: discord.ui.TextInput[TaskModal] = discord.ui.TextInput(
            label="今のタスク",
            placeholder="例: 数学、英語、コードレビュー … 空で未設定",
            required=False,
            max_length=100,
            default=prefill or None,
        )
        self.add_item(self.task_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.task_input.value.strip()
        task: str | None = value if value else None
        result = await self._manager.set_task(
            self._room_id, interaction.user.id, task=task
        )
        if result is OpResult.OK:
            label = task or "(未設定)"
            await _ephemeral(interaction, f"タスクを更新しました: **{label}**")
        else:
            await _ephemeral(
                interaction, REJECT_MESSAGES.get(result, "操作に失敗しました。")
            )


class RoomPanelView(discord.ui.View):
    """Panel buttons. Persistent: each button's ``custom_id`` embeds ``room_id``.

    The static ``custom_id`` set on each decorator is a template — we suffix it
    with the real room id in ``__init__`` so multiple rooms never collide.
    """

    def __init__(self, manager: RoomManager, room_id: UUID) -> None:
        super().__init__(timeout=None)
        self._manager = manager
        self._room_id = room_id
        # Make custom_ids room-specific.
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id:
                child.custom_id = f"{child.custom_id}:{room_id}"

    # Row 0 — participant actions (any user)
    @discord.ui.button(
        label="参加",
        emoji="🙋",
        style=discord.ButtonStyle.success,
        custom_id="pomo:join",
        row=0,
    )
    async def join_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RoomPanelView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.join(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="参加しました 🍅")

    @discord.ui.button(
        label="退出",
        emoji="🚪",
        style=discord.ButtonStyle.secondary,
        custom_id="pomo:leave",
        row=0,
    )
    async def leave_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RoomPanelView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.leave(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="退出しました。")

    @discord.ui.button(
        label="タスク",
        emoji="✍️",
        style=discord.ButtonStyle.secondary,
        custom_id="pomo:task",
        row=0,
    )
    async def task_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RoomPanelView],
    ) -> None:
        state = self._manager.get(self._room_id)
        if state is None:
            await _ephemeral(interaction, REJECT_MESSAGES[OpResult.ROOM_NOT_FOUND])
            return
        participant = state.participants.get(interaction.user.id)
        if participant is None:
            await _ephemeral(interaction, REJECT_MESSAGES[OpResult.NOT_A_PARTICIPANT])
            return
        await interaction.response.send_modal(
            TaskModal(self._manager, self._room_id, prefill=participant.task)
        )

    @discord.ui.button(
        label="統計",
        emoji="📊",
        style=discord.ButtonStyle.secondary,
        custom_id="pomo:stats",
        row=0,
    )
    async def stats_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RoomPanelView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        from src.database.engine import async_session
        from src.services import room_service as svc

        async with async_session() as session:
            summary = await svc.stats_for_user(session, interaction.user.id)
        await interaction.followup.send(
            embed=stats_embed(
                interaction.user, summary.today, summary.this_week, summary.total
            ),
            ephemeral=True,
        )

    # Row 1 — owner-only controls
    @discord.ui.button(
        label="一時停止",
        emoji="⏸",
        style=discord.ButtonStyle.primary,
        custom_id="pomo:pause",
        row=1,
    )
    async def pause_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RoomPanelView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.toggle_pause(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="状態を切り替えました。")

    @discord.ui.button(
        label="スキップ",
        emoji="⏭",
        style=discord.ButtonStyle.primary,
        custom_id="pomo:skip",
        row=1,
    )
    async def skip_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RoomPanelView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.skip(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="次のフェーズへ進めました。")

    @discord.ui.button(
        label="リセット",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        custom_id="pomo:reset",
        row=1,
    )
    async def reset_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RoomPanelView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.reset(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="このフェーズをリセットしました。")

    @discord.ui.button(
        label="終了",
        emoji="🛑",
        style=discord.ButtonStyle.danger,
        custom_id="pomo:end",
        row=1,
    )
    async def end_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RoomPanelView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.end_by_owner(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="ルームを終了しました。")
