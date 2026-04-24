from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import discord
import pytest

from src.core.phase import PhasePlan
from src.room_manager import OpResult
from src.ui.room_panel import CycleSettingsModal, RoomPanelView, TaskModal


def _manager_stub() -> MagicMock:
    return MagicMock()


def _find_button(
    view: RoomPanelView, *, room_id: UUID, action: str
) -> discord.ui.Button:
    expected = f"pomo:{action}:{room_id}"
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.custom_id == expected:
            return child
    raise AssertionError(f"button with custom_id {expected!r} not found")


def _fake_interaction(user_id: int) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


# discord.py 2.6's View and Modal __init__ call asyncio.get_running_loop() to
# set up their internal stopped-future, so every test here has to run inside
# an event loop.


@pytest.mark.asyncio
async def test_task_modal_prefill_is_per_instance() -> None:
    """Two modals opened in parallel must not share their ``default`` value.

    Originally the ``TextInput`` was a class attribute; mutating ``.default``
    on one instance leaked into the other. Building it in ``__init__`` keeps
    each modal's input isolated.
    """
    m_a = TaskModal(_manager_stub(), uuid4(), prefill="math")
    m_b = TaskModal(_manager_stub(), uuid4(), prefill="english")

    assert m_a.task_input is not m_b.task_input
    assert m_a.task_input.default == "math"
    assert m_b.task_input.default == "english"


@pytest.mark.asyncio
async def test_task_modal_prefill_none_leaves_default_unset() -> None:
    m = TaskModal(_manager_stub(), uuid4(), prefill=None)
    assert m.task_input.default is None


@pytest.mark.asyncio
async def test_room_panel_view_custom_ids_are_room_specific() -> None:
    """Two views attached to different rooms must not share ``custom_id``s.

    Without the per-instance rewrite, persistent-view dispatch would fire the
    wrong view's callback when multiple rooms are active at once.
    """
    r1 = uuid4()
    r2 = uuid4()
    v1 = RoomPanelView(_manager_stub(), r1)
    v2 = RoomPanelView(_manager_stub(), r2)

    ids_v1 = {c.custom_id for c in v1.children if isinstance(c, discord.ui.Button)}
    ids_v2 = {c.custom_id for c in v2.children if isinstance(c, discord.ui.Button)}
    assert ids_v1.isdisjoint(ids_v2)
    assert all(cid.endswith(str(r1)) for cid in ids_v1 if cid)
    assert all(cid.endswith(str(r2)) for cid in ids_v2 if cid)
    assert len(ids_v1) == 9
    assert len(ids_v2) == 9


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


# ---------------------------------------------------------------------------
# View-side guards on ⚙️ 時間設定 (owner-only)
# ---------------------------------------------------------------------------
#
# The cycle button can't defer-then-reject like the other owner buttons
# because ``send_modal`` and ``defer`` are mutually exclusive. So the
# NOT_OWNER / ROOM_NOT_FOUND guards live in the view itself, which makes
# them worth testing alongside the manager-level guard.


@pytest.mark.asyncio
async def test_cycle_button_rejects_non_owner_before_opening_modal() -> None:
    room_id = uuid4()
    state = MagicMock()
    state.is_owner = MagicMock(return_value=False)
    state.plan = PhasePlan(25 * 60, 5 * 60, 15 * 60, 4)

    manager = MagicMock()
    manager.get = MagicMock(return_value=state)

    view = RoomPanelView(manager, room_id)
    button = _find_button(view, room_id=room_id, action="cycle")
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

    view = RoomPanelView(manager, room_id)
    button = _find_button(view, room_id=room_id, action="cycle")
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

    view = RoomPanelView(manager, room_id)
    button = _find_button(view, room_id=room_id, action="cycle")
    interaction = _fake_interaction(user_id=1)

    await button.callback(interaction)

    interaction.response.send_modal.assert_not_called()
    interaction.response.send_message.assert_called_once()
    assert "見つかりません" in interaction.response.send_message.call_args.args[0]


# ---------------------------------------------------------------------------
# CycleSettingsModal input validation
# ---------------------------------------------------------------------------


async def _submit_cycle_modal(
    *,
    work: str,
    short_break: str,
    long_break: str,
    long_every: str,
) -> MagicMock:
    """Drive ``CycleSettingsModal.on_submit`` with text-input overrides.

    discord.py wires ``TextInput.value`` to the actual user submission; for
    unit tests we set it via ``_underlying_values`` fallback by pointing
    ``.value`` at a plain string. The simplest is to swap the whole
    ``TextInput`` with a MagicMock whose ``.value`` returns what we want.
    """
    plan = PhasePlan(25 * 60, 5 * 60, 15 * 60, 4)
    manager = MagicMock()
    manager.update_plan = AsyncMock(return_value=OpResult.OK)
    modal = CycleSettingsModal(manager, uuid4(), plan)

    def _fake_input(raw: str) -> MagicMock:
        mock = MagicMock()
        mock.value = raw
        return mock

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
    # OK → ephemeral confirmation, no error message
    interaction.response.send_message.assert_called_once()
    assert "ラウンド" in interaction.response.send_message.call_args.args[0]


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
    # Rejection message should mention the range hint.
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
