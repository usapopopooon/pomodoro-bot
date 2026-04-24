"""Two persistent views for the LionBot-style layout.

* :class:`ControlPanelView` — one per room, attached to the Control Panel
  message. Owner-centric buttons: Start (setup only) / Edit (plan modal) /
  Stop. Participants use 🙋 参加 and 🚪 退出 here too.
* :class:`PhasePanelView` — attached to each phase-transition message.
  Three buttons only: ✅ Present / ⚙ Options / 🛑 Stop. Options opens an
  ephemeral follow-up with pause/skip/reset actions for the owner.

Both views bake the ``room_id`` into every button's ``custom_id`` so
multiple concurrent rooms never cross-dispatch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import discord

from src.core.phase import PhasePlan
from src.ui.embeds import stats_embed

if TYPE_CHECKING:
    from src.room_manager import RoomManager

from src.room_manager import OpResult

REJECT_MESSAGES: dict[OpResult, str] = {
    OpResult.NOT_OWNER: "この操作はオーナーのみ可能です。",
    OpResult.NOT_A_PARTICIPANT: "先に 🙋 参加 してください。",
    OpResult.ALREADY_JOINED: "すでに参加しています。",
    OpResult.ALREADY_STARTED: "すでに開始されています。",
    OpResult.NOT_YET_STARTED: (
        "まだ開始されていません。Control Panel の ▶️ 開始 を押してください。"
    ),
    OpResult.ROOM_NOT_FOUND: (
        "このポモドーロは見つかりません。`/pomo` で作り直してください。"
    ),
    OpResult.ANOTHER_ROOM: (
        "他のポモドーロに参加中です。そちらを退出してから参加してください。"
    ),
}


async def _ephemeral(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def _reply(
    interaction: discord.Interaction, result: OpResult, *, ok_text: str
) -> None:
    if result is OpResult.OK:
        await _ephemeral(interaction, ok_text)
    else:
        await _ephemeral(
            interaction, REJECT_MESSAGES.get(result, "操作に失敗しました。")
        )


# ---------------------------------------------------------------------------
# Modals (task + cycle settings)
# ---------------------------------------------------------------------------


class TaskModal(discord.ui.Modal):
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
        task = value if value else None
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


class CycleSettingsModal(discord.ui.Modal):
    _FIELDS: tuple[tuple[str, str, int, int], ...] = (
        ("work", "作業時間", 1, 180),
        ("short_break", "短休憩", 1, 60),
        ("long_break", "長休憩", 1, 120),
        ("long_every", "長休憩の頻度", 1, 12),
    )

    def __init__(self, manager: RoomManager, room_id: UUID, plan: PhasePlan) -> None:
        super().__init__(title="時間設定を編集", timeout=300)
        self._manager = manager
        self._room_id = room_id
        self.work_input: discord.ui.TextInput[CycleSettingsModal] = (
            discord.ui.TextInput(
                label="作業時間(分: 1-180)",
                required=True,
                max_length=3,
                default=str(plan.work_seconds // 60),
            )
        )
        self.short_break_input: discord.ui.TextInput[CycleSettingsModal] = (
            discord.ui.TextInput(
                label="短休憩(分: 1-60)",
                required=True,
                max_length=2,
                default=str(plan.short_break_seconds // 60),
            )
        )
        self.long_break_input: discord.ui.TextInput[CycleSettingsModal] = (
            discord.ui.TextInput(
                label="長休憩(分: 1-120)",
                required=True,
                max_length=3,
                default=str(plan.long_break_seconds // 60),
            )
        )
        self.long_every_input: discord.ui.TextInput[CycleSettingsModal] = (
            discord.ui.TextInput(
                label="長休憩の頻度(1-12)",
                required=True,
                max_length=2,
                default=str(plan.long_break_every),
            )
        )
        self.add_item(self.work_input)
        self.add_item(self.short_break_input)
        self.add_item(self.long_break_input)
        self.add_item(self.long_every_input)

    def _parse_int(
        self, raw: str, *, label: str, low: int, high: int
    ) -> tuple[int | None, str | None]:
        try:
            value = int(raw.strip())
        except ValueError:
            return None, "数字で入力してください。"
        if not (low <= value <= high):
            unit = "" if label == "長休憩の頻度" else " 分"
            return None, f"{label}は {low}〜{high}{unit} で指定してください。"
        return value, None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_values = {
            "work": self.work_input.value,
            "short_break": self.short_break_input.value,
            "long_break": self.long_break_input.value,
            "long_every": self.long_every_input.value,
        }
        parsed: dict[str, int] = {}
        for key, label, low, high in self._FIELDS:
            value, err = self._parse_int(
                raw_values[key], label=label, low=low, high=high
            )
            if err is not None:
                await _ephemeral(interaction, err)
                return
            assert value is not None
            parsed[key] = value

        plan = PhasePlan(
            work_seconds=parsed["work"] * 60,
            short_break_seconds=parsed["short_break"] * 60,
            long_break_seconds=parsed["long_break"] * 60,
            long_break_every=parsed["long_every"],
        )
        result = await self._manager.update_plan(
            self._room_id, interaction.user.id, plan=plan
        )
        if result is OpResult.OK:
            await _ephemeral(interaction, "時間設定を更新しました。")
        else:
            await _ephemeral(
                interaction, REJECT_MESSAGES.get(result, "操作に失敗しました。")
            )


# ---------------------------------------------------------------------------
# Control Panel (persistent, one per room)
# ---------------------------------------------------------------------------


class ControlPanelView(discord.ui.View):
    """Persistent control panel attached to the Control Panel message.

    Layout:
        Row 0 (everyone):  [🙋 参加] [🚪 退出] [✍️ タスク] [📊 統計]
        Row 1 (owner):     [▶️ 開始] [⚙️ 時間設定] [🛑 終了]

    ``has_started`` only affects the Start button's enabled state — the
    button itself is always present so the layout doesn't shift.
    """

    def __init__(
        self, manager: RoomManager, room_id: UUID, *, has_started: bool = False
    ) -> None:
        super().__init__(timeout=None)
        self._manager = manager
        self._room_id = room_id
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id:
                child.custom_id = f"{child.custom_id}:{room_id}"
            # Disable Start once the timer is already running.
            if (
                has_started
                and isinstance(child, discord.ui.Button)
                and child.custom_id
                and child.custom_id.startswith("cp:start:")
            ):
                child.disabled = True
                child.label = "開始中"

    # Row 0 ------------------------------------------------------------

    @discord.ui.button(
        label="参加",
        emoji="🙋",
        style=discord.ButtonStyle.success,
        custom_id="cp:join",
        row=0,
    )
    async def join_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[ControlPanelView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.join(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="参加しました 🍅")

    @discord.ui.button(
        label="退出",
        emoji="🚪",
        style=discord.ButtonStyle.secondary,
        custom_id="cp:leave",
        row=0,
    )
    async def leave_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[ControlPanelView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.leave(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="退出しました。")

    @discord.ui.button(
        label="タスク",
        emoji="✍️",
        style=discord.ButtonStyle.secondary,
        custom_id="cp:task",
        row=0,
    )
    async def task_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[ControlPanelView],
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
        custom_id="cp:stats",
        row=0,
    )
    async def stats_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[ControlPanelView],
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

    # Row 1 (owner) ----------------------------------------------------

    @discord.ui.button(
        label="開始",
        emoji="▶️",
        style=discord.ButtonStyle.success,
        custom_id="cp:start",
        row=1,
    )
    async def start_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[ControlPanelView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.begin_phases(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="タイマーを開始しました 🍅")

    @discord.ui.button(
        label="時間設定",
        emoji="⚙️",
        style=discord.ButtonStyle.secondary,
        custom_id="cp:cycle",
        row=1,
    )
    async def cycle_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[ControlPanelView],
    ) -> None:
        state = self._manager.get(self._room_id)
        if state is None:
            await _ephemeral(interaction, REJECT_MESSAGES[OpResult.ROOM_NOT_FOUND])
            return
        if not state.is_owner(interaction.user.id):
            await _ephemeral(interaction, REJECT_MESSAGES[OpResult.NOT_OWNER])
            return
        await interaction.response.send_modal(
            CycleSettingsModal(self._manager, self._room_id, state.plan)
        )

    @discord.ui.button(
        label="終了",
        emoji="🛑",
        style=discord.ButtonStyle.danger,
        custom_id="cp:end",
        row=1,
    )
    async def end_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[ControlPanelView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.end_by_owner(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="ポモドーロを終了しました。")


# ---------------------------------------------------------------------------
# Phase Panel (new message per phase; LionBot-style 3-button layout)
# ---------------------------------------------------------------------------


class PhasePanelView(discord.ui.View):
    """Three-button panel attached to each phase-transition message.

    Mirrors LionBot's ``[✅ Present] [Options] [Stop]`` layout. Options
    opens an ephemeral follow-up with the owner's pause/skip/reset
    controls; non-owners see a read-only view of the state instead.
    """

    def __init__(self, manager: RoomManager, room_id: UUID) -> None:
        super().__init__(timeout=None)
        self._manager = manager
        self._room_id = room_id
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id:
                child.custom_id = f"{child.custom_id}:{room_id}"

    @discord.ui.button(
        label="Present",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="pp:present",
        row=0,
    )
    async def present_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PhasePanelView],
    ) -> None:
        """Mark the caller as present for this round.

        Under the hood this is the same as the Control Panel's 🙋 参加
        button — it adds the user as a participant if not already, and
        acknowledges if they are.
        """
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.join(self._room_id, interaction.user.id)
        if result is OpResult.ALREADY_JOINED:
            await _ephemeral(interaction, "参加済みです。引き続き頑張りましょう 🍅")
        else:
            await _reply(interaction, result, ok_text="参加しました 🍅")

    @discord.ui.button(
        label="Options",
        style=discord.ButtonStyle.primary,
        custom_id="pp:options",
        row=0,
    )
    async def options_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PhasePanelView],
    ) -> None:
        state = self._manager.get(self._room_id)
        if state is None:
            await _ephemeral(interaction, REJECT_MESSAGES[OpResult.ROOM_NOT_FOUND])
            return
        is_owner = state.is_owner(interaction.user.id)
        await interaction.response.send_message(
            content=(
                "**オーナーオプション**" if is_owner else "**状態**(オーナーのみ操作可)"
            ),
            view=OptionsView(self._manager, self._room_id, is_owner=is_owner),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Stop",
        emoji="🛑",
        style=discord.ButtonStyle.danger,
        custom_id="pp:stop",
        row=0,
    )
    async def stop_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PhasePanelView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.end_by_owner(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="ポモドーロを終了しました。")


# ---------------------------------------------------------------------------
# Options — ephemeral sub-view for pause/skip/reset (from Phase Panel)
# ---------------------------------------------------------------------------


class OptionsView(discord.ui.View):
    """Ephemeral actions revealed by pressing "Options" on a Phase Panel.

    Not persistent — it lives only on the interaction's ephemeral reply.
    Non-owners see the buttons but every action rejects with
    ``NOT_OWNER`` via the manager guard.
    """

    def __init__(self, manager: RoomManager, room_id: UUID, *, is_owner: bool) -> None:
        super().__init__(timeout=180)
        self._manager = manager
        self._room_id = room_id
        # Visually hint which buttons are actionable — non-owners see them
        # disabled so they don't trigger rejection dialogs.
        if not is_owner:
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True

    @discord.ui.button(label="一時停止", emoji="⏸", style=discord.ButtonStyle.primary)
    async def pause(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[OptionsView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.toggle_pause(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="状態を切り替えました。")

    @discord.ui.button(label="スキップ", emoji="⏭", style=discord.ButtonStyle.primary)
    async def skip(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[OptionsView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.skip(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="次のフェーズへ進めました。")

    @discord.ui.button(
        label="リセット", emoji="🔄", style=discord.ButtonStyle.secondary
    )
    async def reset(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[OptionsView],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        result = await self._manager.reset(self._room_id, interaction.user.id)
        await _reply(interaction, result, ok_text="このフェーズをリセットしました。")

    @discord.ui.button(label="時間設定", emoji="⚙️", style=discord.ButtonStyle.secondary)
    async def cycle(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[OptionsView],
    ) -> None:
        state = self._manager.get(self._room_id)
        if state is None:
            await _ephemeral(interaction, REJECT_MESSAGES[OpResult.ROOM_NOT_FOUND])
            return
        if not state.is_owner(interaction.user.id):
            await _ephemeral(interaction, REJECT_MESSAGES[OpResult.NOT_OWNER])
            return
        await interaction.response.send_modal(
            CycleSettingsModal(self._manager, self._room_id, state.plan)
        )
