from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import discord
import pytest

from src.core.phase import Phase, PhasePlan
from src.core.room_state import RoomState
from src.room_manager import OpResult
from src.ui.panel_views import (
    ControlPanelView,
    CycleSettingsModal,
    NotificationSettingsView,
    PhasePanelView,
    TaskModal,
)


def _manager_stub() -> MagicMock:
    return MagicMock()


def _find_button(view: discord.ui.View, *, custom_id: str) -> discord.ui.Button:
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.custom_id == custom_id:
            return child
    raise AssertionError(f"button with custom_id {custom_id!r} not found")


def _find_cp(
    view: ControlPanelView, *, room_id: UUID, action: str
) -> discord.ui.Button:
    return _find_button(view, custom_id=f"cp:{action}:{room_id}")


def _find_pp(view: PhasePanelView, *, room_id: UUID, action: str) -> discord.ui.Button:
    return _find_button(view, custom_id=f"pp:{action}:{room_id}")


def _fake_interaction(user_id: int) -> MagicMock:
    """Mimic a Discord interaction where ``defer`` flips ``is_done`` to True.

    Matches real behaviour: once an interaction is deferred, further
    responses must go through ``followup.send`` instead of
    ``response.send_message``. Without this side-effect the test's
    ``_ephemeral`` helper would wrongly take the ``send_message`` branch.
    """
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()

    async def _defer(*args: object, **kwargs: object) -> None:
        interaction.response.is_done = MagicMock(return_value=True)

    interaction.response.defer = AsyncMock(side_effect=_defer)
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


# discord.py 2.6's View and Modal __init__ call ``asyncio.get_running_loop()``
# to set up their internal stopped-future, so every test here has to run
# inside an event loop (even the "pure" layout ones).


# ---------------------------------------------------------------------------
# TaskModal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_modal_prefill_is_per_instance() -> None:
    m_a = TaskModal(_manager_stub(), uuid4(), prefill="math")
    m_b = TaskModal(_manager_stub(), uuid4(), prefill="english")
    assert m_a.task_input is not m_b.task_input
    assert m_a.task_input.default == "math"
    assert m_b.task_input.default == "english"


@pytest.mark.asyncio
async def test_task_modal_prefill_none_leaves_default_unset() -> None:
    m = TaskModal(_manager_stub(), uuid4(), prefill=None)
    assert m.task_input.default is None


# ---------------------------------------------------------------------------
# Control Panel layout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_panel_has_ten_buttons_and_room_specific_custom_ids() -> None:
    r1 = uuid4()
    r2 = uuid4()
    v1 = ControlPanelView(_manager_stub(), r1)
    v2 = ControlPanelView(_manager_stub(), r2)

    ids_v1 = {c.custom_id for c in v1.children if isinstance(c, discord.ui.Button)}
    ids_v2 = {c.custom_id for c in v2.children if isinstance(c, discord.ui.Button)}
    # join/leave/task/stats/help + start/cycle/notify/voice/end
    assert len(ids_v1) == 10
    assert len(ids_v2) == 10
    assert ids_v1.isdisjoint(ids_v2)
    assert all(cid.endswith(str(r1)) for cid in ids_v1 if cid)
    assert all(cid.endswith(str(r2)) for cid in ids_v2 if cid)


@pytest.mark.asyncio
async def test_help_button_sends_ephemeral_usage_embed() -> None:
    room_id = uuid4()
    view = ControlPanelView(_manager_stub(), room_id)
    interaction = _fake_interaction(user_id=42)

    help_btn = _find_cp(view, room_id=room_id, action="help")
    await help_btn.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert "使い方" in (embed.title or "")
    # Every button family should appear in the cheat-sheet so nothing is a
    # mystery to a first-time user.
    joined = "\n".join(f.value for f in embed.fields)
    for label in ("参加", "退出", "タスク", "統計", "開始", "時間設定", "通知", "終了"):
        assert label in joined


@pytest.mark.asyncio
async def test_control_panel_start_button_disabled_when_already_started() -> None:
    room_id = uuid4()
    not_started = ControlPanelView(_manager_stub(), room_id, has_started=False)
    running = ControlPanelView(_manager_stub(), room_id, has_started=True)

    start_setup = _find_cp(not_started, room_id=room_id, action="start")
    start_running = _find_cp(running, room_id=room_id, action="start")

    assert start_setup.disabled is False
    assert start_running.disabled is True
    assert start_running.label == "開始中"


# ---------------------------------------------------------------------------
# Phase Panel layout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_panel_has_three_buttons() -> None:
    room_id = uuid4()
    view = PhasePanelView(_manager_stub(), room_id)
    button_ids = {
        c.custom_id for c in view.children if isinstance(c, discord.ui.Button)
    }
    assert len(button_ids) == 3
    actions = {cid.split(":", 2)[1] for cid in button_ids if cid}
    assert actions == {"present", "options", "stop"}
    assert all(cid.endswith(str(room_id)) for cid in button_ids if cid)


@pytest.mark.asyncio
async def test_phase_panel_present_forwards_to_join() -> None:
    room_id = uuid4()
    manager = MagicMock()
    manager.join = AsyncMock(return_value=OpResult.OK)
    view = PhasePanelView(manager, room_id)
    button = _find_pp(view, room_id=room_id, action="present")
    interaction = _fake_interaction(user_id=42)

    await button.callback(interaction)

    manager.join.assert_awaited_once_with(room_id, 42)


@pytest.mark.asyncio
async def test_phase_panel_present_reports_already_joined_gracefully() -> None:
    room_id = uuid4()
    manager = MagicMock()
    manager.join = AsyncMock(return_value=OpResult.ALREADY_JOINED)
    view = PhasePanelView(manager, room_id)
    button = _find_pp(view, room_id=room_id, action="present")
    interaction = _fake_interaction(user_id=42)

    await button.callback(interaction)

    interaction.followup.send.assert_called_once()
    msg = interaction.followup.send.call_args.args[0]
    assert "参加済み" in msg


@pytest.mark.asyncio
async def test_phase_panel_stop_forwards_to_end_by_owner() -> None:
    room_id = uuid4()
    manager = MagicMock()
    manager.end_by_owner = AsyncMock(return_value=OpResult.OK)
    view = PhasePanelView(manager, room_id)
    button = _find_pp(view, room_id=room_id, action="stop")
    interaction = _fake_interaction(user_id=1)

    await button.callback(interaction)

    manager.end_by_owner.assert_awaited_once_with(room_id, 1)


# ---------------------------------------------------------------------------
# Control Panel cycle button (owner-only, opens modal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cycle_settings_modal_uses_plan_defaults() -> None:
    plan = PhasePlan(
        work_seconds=25 * 60,
        short_break_seconds=5 * 60,
        long_break_seconds=15 * 60,
        long_break_every=4,
    )
    modal = CycleSettingsModal(_manager_stub(), uuid4(), plan)
    assert modal.work_input.default == "25"
    assert modal.short_break_input.default == "5"
    assert modal.long_break_input.default == "15"
    assert modal.long_every_input.default == "4"


@pytest.mark.asyncio
async def test_cycle_button_rejects_non_owner_before_opening_modal() -> None:
    room_id = uuid4()
    state = MagicMock()
    state.is_owner = MagicMock(return_value=False)
    state.plan = PhasePlan(25 * 60, 5 * 60, 15 * 60, 4)

    manager = MagicMock()
    manager.get = MagicMock(return_value=state)

    view = ControlPanelView(manager, room_id)
    button = _find_cp(view, room_id=room_id, action="cycle")
    interaction = _fake_interaction(user_id=999)

    await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_called_once()
    assert "オーナー" in interaction.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_cycle_button_opens_modal_for_owner() -> None:
    room_id = uuid4()
    state = MagicMock()
    state.is_owner = MagicMock(return_value=True)
    state.plan = PhasePlan(25 * 60, 5 * 60, 15 * 60, 4)

    manager = MagicMock()
    manager.get = MagicMock(return_value=state)

    view = ControlPanelView(manager, room_id)
    button = _find_cp(view, room_id=room_id, action="cycle")
    interaction = _fake_interaction(user_id=42)

    await button.callback(interaction)

    interaction.response.send_modal.assert_called_once()
    sent_modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(sent_modal, CycleSettingsModal)


@pytest.mark.asyncio
async def test_cycle_button_rejects_when_room_missing_from_memory() -> None:
    room_id = uuid4()
    manager = MagicMock()
    manager.get = MagicMock(return_value=None)

    view = ControlPanelView(manager, room_id)
    button = _find_cp(view, room_id=room_id, action="cycle")
    interaction = _fake_interaction(user_id=1)

    await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_called_once()
    assert "見つかりません" in interaction.response.send_message.call_args.args[0]


# ---------------------------------------------------------------------------
# CycleSettingsModal input validation
# ---------------------------------------------------------------------------


async def _submit_cycle_modal(
    *, work: str, short_break: str, long_break: str, long_every: str
) -> MagicMock:
    plan = PhasePlan(25 * 60, 5 * 60, 15 * 60, 4)
    manager = MagicMock()
    manager.update_plan = AsyncMock(return_value=OpResult.OK)
    modal = CycleSettingsModal(manager, uuid4(), plan)

    def _fake_input(raw: str) -> MagicMock:
        m = MagicMock()
        m.value = raw
        return m

    modal.work_input = _fake_input(work)
    modal.short_break_input = _fake_input(short_break)
    modal.long_break_input = _fake_input(long_break)
    modal.long_every_input = _fake_input(long_every)

    interaction = _fake_interaction(user_id=1)
    await modal.on_submit(interaction)
    return interaction


@pytest.mark.asyncio
async def test_cycle_modal_accepts_valid_input() -> None:
    interaction = await _submit_cycle_modal(
        work="30", short_break="7", long_break="20", long_every="3"
    )
    interaction.response.send_message.assert_called_once()
    assert "時間設定" in interaction.response.send_message.call_args.args[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("work", "0"),
        ("work", "999"),
        ("short_break", "0"),
        ("short_break", "61"),
        ("long_break", "0"),
        ("long_break", "121"),
        ("long_every", "0"),
        ("long_every", "13"),
    ],
)
@pytest.mark.asyncio
async def test_cycle_modal_rejects_out_of_range(field: str, value: str) -> None:
    defaults = {"work": "25", "short_break": "5", "long_break": "15", "long_every": "4"}
    defaults[field] = value
    interaction = await _submit_cycle_modal(**defaults)
    interaction.response.send_message.assert_called_once()
    msg = interaction.response.send_message.call_args.args[0]
    assert "指定してください" in msg


@pytest.mark.asyncio
async def test_cycle_modal_rejects_non_numeric_input() -> None:
    interaction = await _submit_cycle_modal(
        work="abc", short_break="5", long_break="15", long_every="4"
    )
    interaction.response.send_message.assert_called_once()
    assert "数字" in interaction.response.send_message.call_args.args[0]


# ---------------------------------------------------------------------------
# Notification settings view
# ---------------------------------------------------------------------------


def _state_for_notify(*, work: bool, short_: bool, long_: bool) -> RoomState:
    state = RoomState(
        room_id=uuid4(),
        guild_id=None,
        channel_id=1,
        created_by=1,
        plan=PhasePlan(1500, 300, 900, 4),
    )
    state.notify_work = work
    state.notify_short_break = short_
    state.notify_long_break = long_
    return state


@pytest.mark.asyncio
async def test_notification_settings_labels_reflect_current_state() -> None:
    state = _state_for_notify(work=True, short_=False, long_=True)
    view = NotificationSettingsView(_manager_stub(), uuid4(), state)

    expected = {
        "ns:work": ("作業: ON", "🔔", discord.ButtonStyle.success),
        "ns:short": ("短休憩: OFF", "🔕", discord.ButtonStyle.secondary),
        "ns:long": ("長休憩: ON", "🔔", discord.ButtonStyle.success),
    }
    seen: dict[str, tuple[str, str, discord.ButtonStyle]] = {}
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.custom_id in expected:
            assert child.emoji is not None
            seen[child.custom_id] = (
                child.label or "",
                child.emoji.name,
                child.style,
            )
    assert seen == expected


@pytest.mark.asyncio
async def test_notification_toggle_flips_state_and_updates_view() -> None:
    state = _state_for_notify(work=True, short_=True, long_=True)

    manager = MagicMock()
    manager.get.return_value = state
    manager.set_notify = AsyncMock(return_value=OpResult.OK)
    # The view re-reads state via ``manager.get`` after toggling, so reflect
    # the would-be flag flip in the stub for the second read.
    seq = [state]

    def _flip(*_: object, **kwargs: object) -> OpResult:
        phase = kwargs["phase"]
        if phase is Phase.WORK:
            state.notify_work = bool(kwargs["enabled"])
        return OpResult.OK

    manager.set_notify.side_effect = _flip
    manager.get.side_effect = lambda _rid: seq[0]

    view = NotificationSettingsView(manager, uuid4(), state)
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = 1
    interaction.response = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.is_done.return_value = False

    await view._toggle(interaction, Phase.WORK)

    manager.set_notify.assert_awaited_once()
    kwargs = manager.set_notify.await_args.kwargs
    assert kwargs["phase"] is Phase.WORK
    assert kwargs["enabled"] is False  # was True → toggle off
    interaction.response.edit_message.assert_awaited_once()

    work_btn = _find_button(view, custom_id="ns:work")
    assert work_btn.label == "作業: OFF"
    assert work_btn.style is discord.ButtonStyle.secondary
