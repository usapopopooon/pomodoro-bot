"""VoiceManager tests — connect/play/disconnect bookkeeping.

We never spin up a real Discord voice connection in unit tests; instead the
``discord.VoiceChannel`` and ``discord.VoiceClient`` surfaces are stubbed
with ``MagicMock`` / ``AsyncMock`` so we can assert on the manager's state
machine without networking.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.voice_manager import VoiceManager


def _stub_voice_client(*, connected: bool = True, playing: bool = False) -> MagicMock:
    client = MagicMock()
    client.is_connected = MagicMock(return_value=connected)
    client.is_playing = MagicMock(return_value=playing)
    client.stop = MagicMock()
    client.disconnect = AsyncMock()
    client.move_to = AsyncMock()
    client.play = MagicMock()
    return client


def _stub_voice_channel(
    *, guild_id: int = 1, channel_id: int = 100, voice_client: MagicMock | None = None
) -> MagicMock:
    """Mimic ``discord.VoiceChannel`` with a stubbed ``connect`` coroutine."""
    channel = MagicMock()
    channel.id = channel_id
    channel.guild = SimpleNamespace(id=guild_id)
    if voice_client is None:
        voice_client = _stub_voice_client()
    channel.connect = AsyncMock(return_value=voice_client)
    return channel


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_caches_voice_client_per_guild(tmp_path: Path) -> None:
    mgr = VoiceManager(voices_dir=tmp_path)
    channel = _stub_voice_channel(guild_id=42)

    assert await mgr.connect(channel) is True
    assert mgr.is_connected(42) is True
    channel.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_reuses_existing_when_same_channel(tmp_path: Path) -> None:
    mgr = VoiceManager(voices_dir=tmp_path)
    voice_client = _stub_voice_client(connected=True)
    channel = _stub_voice_channel(
        guild_id=42, channel_id=100, voice_client=voice_client
    )

    await mgr.connect(channel)
    # Second connect to the same channel must NOT redial — Discord only
    # accepts one voice connection per guild.
    await mgr.connect(channel)
    channel.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_moves_to_new_channel_when_owner_switches(
    tmp_path: Path,
) -> None:
    mgr = VoiceManager(voices_dir=tmp_path)
    voice_client = _stub_voice_client(connected=True)
    voice_client.channel = SimpleNamespace(id=100)
    first = _stub_voice_channel(guild_id=42, channel_id=100, voice_client=voice_client)
    await mgr.connect(first)

    # Owner moved to channel 200 — manager should follow rather than dial fresh.
    second = _stub_voice_channel(guild_id=42, channel_id=200)
    assert await mgr.connect(second) is True
    voice_client.move_to.assert_awaited_once()
    second.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_pops_state_and_calls_client(tmp_path: Path) -> None:
    mgr = VoiceManager(voices_dir=tmp_path)
    voice_client = _stub_voice_client(connected=True)
    channel = _stub_voice_channel(guild_id=42, voice_client=voice_client)
    await mgr.connect(channel)

    await mgr.disconnect(42)
    assert mgr.is_connected(42) is False
    voice_client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_when_not_connected_is_noop(tmp_path: Path) -> None:
    mgr = VoiceManager(voices_dir=tmp_path)
    # Should not raise.
    await mgr.disconnect(999)
    assert mgr.is_connected(999) is False


@pytest.mark.asyncio
async def test_disconnect_all_clears_every_guild(tmp_path: Path) -> None:
    mgr = VoiceManager(voices_dir=tmp_path)
    a_client = _stub_voice_client()
    b_client = _stub_voice_client()
    await mgr.connect(_stub_voice_channel(guild_id=1, voice_client=a_client))
    await mgr.connect(_stub_voice_channel(guild_id=2, voice_client=b_client))

    await mgr.disconnect_all()
    assert mgr.is_connected(1) is False
    assert mgr.is_connected(2) is False
    a_client.disconnect.assert_awaited_once()
    b_client.disconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_play_clip_returns_false_when_not_connected(tmp_path: Path) -> None:
    mgr = VoiceManager(voices_dir=tmp_path)
    assert await mgr.play_clip(42, "start") is False


@pytest.mark.asyncio
async def test_play_clip_returns_false_when_file_missing(tmp_path: Path) -> None:
    mgr = VoiceManager(voices_dir=tmp_path)
    await mgr.connect(_stub_voice_channel(guild_id=42))
    # tmp_path holds no .wav, so the clip resolution must fail cleanly.
    assert await mgr.play_clip(42, "absent-clip") is False


@pytest.mark.asyncio
async def test_play_clip_invokes_client_play_with_after_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives the ``after`` callback synchronously to verify the wait wakes.

    discord.py calls ``after`` from an internal thread when playback ends;
    the manager bounces it onto the asyncio loop via ``call_soon_threadsafe``.
    Here we just invoke ``after`` directly — same effect.

    ``FFmpegPCMAudio`` is stubbed because it would otherwise spawn an
    ffmpeg subprocess even at construction time.
    """
    clip_path = tmp_path / "start.wav"
    clip_path.write_bytes(b"RIFF....WAVEfmt ")
    monkeypatch.setattr("src.voice_manager.discord.FFmpegPCMAudio", MagicMock())

    voice_client = _stub_voice_client(connected=True, playing=False)

    def _play(_source: object, *, after: object) -> None:
        # Simulate a fire-and-forget play that completes immediately.
        assert callable(after)
        after(None)

    voice_client.play.side_effect = _play

    mgr = VoiceManager(voices_dir=tmp_path)
    await mgr.connect(_stub_voice_channel(guild_id=42, voice_client=voice_client))

    assert await mgr.play_clip(42, "start") is True
    voice_client.play.assert_called_once()


@pytest.mark.asyncio
async def test_play_clip_stops_in_flight_clip_before_starting_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip_path = tmp_path / "alarm.wav"
    clip_path.write_bytes(b"RIFF....WAVEfmt ")
    monkeypatch.setattr("src.voice_manager.discord.FFmpegPCMAudio", MagicMock())

    voice_client = _stub_voice_client(connected=True, playing=True)

    def _play(_source: object, *, after: object) -> None:
        # Resolve immediately so the test doesn't deadlock waiting.
        after(None)

    voice_client.play.side_effect = _play
    mgr = VoiceManager(voices_dir=tmp_path)
    await mgr.connect(_stub_voice_channel(guild_id=42, voice_client=voice_client))

    await mgr.play_clip(42, "alarm")
    voice_client.stop.assert_called_once()
