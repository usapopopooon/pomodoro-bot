from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import discord
import pytest

from src.ui.room_panel import RoomPanelView, TaskModal


def _manager_stub() -> MagicMock:
    return MagicMock()


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
    assert len(ids_v1) == 8
    assert len(ids_v2) == 8
