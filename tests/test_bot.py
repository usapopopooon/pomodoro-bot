from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot import PomodoroBot


class _FakeVoiceClient:
    def __init__(self, channel: SimpleNamespace) -> None:
        self.channel = channel

    def is_connected(self) -> bool:
        return True


def _channel(channel_id: int, members: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(id=channel_id, members=members)


def _state(channel: SimpleNamespace | None) -> SimpleNamespace:
    return SimpleNamespace(channel=channel)


def _member(
    user_id: int,
    guild_id: int,
    voice_client: _FakeVoiceClient,
    *,
    is_bot: bool,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        bot=is_bot,
        guild=SimpleNamespace(id=guild_id, voice_client=voice_client),
    )


def _make_bot(monkeypatch: pytest.MonkeyPatch) -> PomodoroBot:
    import src.bot as bot_mod

    monkeypatch.setattr(bot_mod.discord, "VoiceClient", _FakeVoiceClient)
    bot = PomodoroBot()
    bot._connection.user = SimpleNamespace(id=999)
    bot.room_manager = MagicMock()
    bot.room_manager.end_voice_room_if_any = AsyncMock(return_value=None)
    bot.voice_manager = MagicMock()
    bot.voice_manager.disconnect = AsyncMock()
    bot._announce_voice_empty_shutdown = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_auto_disconnect_when_other_bot_leaves_bot_only_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot_channel = _channel(123, [SimpleNamespace(id=999, bot=True)])
    voice_client = _FakeVoiceClient(bot_channel)
    bot = _make_bot(monkeypatch)

    member = _member(222, 10, voice_client, is_bot=True)
    await bot.on_voice_state_update(member, _state(bot_channel), _state(None))

    bot.room_manager.end_voice_room_if_any.assert_awaited_once_with(
        10, reason="voice_empty"
    )
    bot.voice_manager.disconnect.assert_awaited_once_with(10)
    bot._announce_voice_empty_shutdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_end_when_self_moved_into_bot_only_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_channel = _channel(456, [SimpleNamespace(id=7, bot=False)])
    new_channel = _channel(123, [SimpleNamespace(id=999, bot=True)])
    voice_client = _FakeVoiceClient(new_channel)
    bot = _make_bot(monkeypatch)
    ended_state = object()
    bot.room_manager.end_voice_room_if_any.return_value = ended_state

    member = _member(999, 10, voice_client, is_bot=True)
    await bot.on_voice_state_update(member, _state(old_channel), _state(new_channel))

    bot.room_manager.end_voice_room_if_any.assert_awaited_once_with(
        10, reason="voice_empty"
    )
    bot.voice_manager.disconnect.assert_not_awaited()
    bot._announce_voice_empty_shutdown.assert_awaited_once_with(ended_state)


@pytest.mark.asyncio
async def test_initial_self_join_does_not_auto_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot_channel = _channel(123, [SimpleNamespace(id=999, bot=True)])
    voice_client = _FakeVoiceClient(bot_channel)
    bot = _make_bot(monkeypatch)

    member = _member(999, 10, voice_client, is_bot=True)
    await bot.on_voice_state_update(member, _state(None), _state(bot_channel))

    bot.room_manager.end_voice_room_if_any.assert_not_awaited()
    bot.voice_manager.disconnect.assert_not_awaited()
